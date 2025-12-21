import os
import json
import base64
import logging
import asyncio
from typing import Dict, Any, List, AsyncIterator, Optional
from twilio.rest import Client

import boto3
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

import numpy as np
from scipy.signal import resample_poly
import g711

from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
)
from aws_sdk_bedrock_runtime.config import (
    Config as BedrockConfig,
    HTTPAuthSchemeResolver,
    SigV4AuthScheme,
)
from smithy_aws_core.credentials_resolvers.environment import (
    EnvironmentCredentialsResolver,
)

# -----------------------------------------------------------------------------
# Logging + environment configuration
# -----------------------------------------------------------------------------
def _log_parent_call_sid(call_sid: str) -> None:
    """
    Logs ParentCallSid (if any) for a given CallSid.
    """
    acct = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")

    if not acct or not token or not call_sid:
        log.info(f"[twilio] CallSid={call_sid} (missing TWILIO creds or CallSid)")
        return

    try:
        client = Client(acct, token)
        call = client.calls(call_sid).fetch()

        log.info(
            f"[twilio] CallSid={call.sid} ParentCallSid={call.parent_call_sid} "
            f"Status={call.status} Direction={call.direction}"
        )
    except Exception as e:
        log.info(f"[twilio] Failed to fetch ParentCallSid for CallSid={call_sid}: {e}")


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("renamed-novasonic-app")
# Phase 2 role config
AGENT_ROLE = os.getenv("AGENT_ROLE", "A").upper()   # "A" or "B"
PEER_DIAL_TO = os.getenv("PEER_DIAL_TO", "")        # only used when AGENT_ROLE == "A"
MAX_SECONDS = int(os.getenv("BRIDGE_MAX_SECONDS", "60"))
# kick off first spoken audio (Agent A only)
KICKOFF_TEXT = os.getenv("KICKOFF_TEXT", "").strip()

REGION_NAME = os.getenv("AWS_REGION", "us-east-1")
MODEL_ARN_OR_ID = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-2-sonic-v1:0")

VOICE_NAME = os.getenv("AGENT_VOICE", "tiffany")
BASE_SYSTEM_INSTRUCTIONS = os.getenv(
    "AGENT_SYSTEM_PROMPT",
    "You are a helpful, friendly voice assistant named tiffany. "
    "Speak clearly, keep responses brief, and be polite.",
)

NOVA_IN_HZ = 16000     # Nova Sonic audioInput sample rate (PCM16)
NOVA_OUT_HZ = 24000    # Nova Sonic audioOutput sample rate (PCM16)
TWILIO_HZ = 8000        # Twilio Media Streams μ-law 8 kHz

PUBLIC_WSS_BASE = os.getenv("WSS_VOICE_URL", "wss://unrancoured-marled-quiana.ngrok-free.dev")


# -----------------------------------------------------------------------------
# Audio conversion: Twilio μ-law 8kHz <-> Nova Sonic PCM16 (16k in, 24k out)
# -----------------------------------------------------------------------------

def _twilio_b64_ulaw_to_pcm16_16k(b64_payload: str) -> bytes:
    """
    Twilio media.payload (base64 μ-law / 8k) -> PCM16 mono @ 16 kHz.

    Pipeline:
        base64 μ-law 8kHz -> float32 [-1,1] 8kHz -> resample -> float32 16kHz -> PCM16 LE
    """
    ulaw = base64.b64decode(b64_payload)
    audio_8k = g711.decode_ulaw(ulaw).astype(np.float32)

    audio_16k = resample_poly(audio_8k, up=2, down=1).astype(np.float32)
    audio_16k = np.clip(audio_16k, -1.0, 1.0)

    pcm_i16 = (audio_16k * 32767.0).astype("<i2")
    return pcm_i16.tobytes()


def _pcm16_24k_to_twilio_b64_ulaw(pcm24k: bytes) -> str:
    """
    PCM16 mono 24k bytes (from Nova Sonic 'pcm16' output) -> Twilio media.payload.

    Pipeline:
        PCM16 LE 24kHz -> float32 [-1,1] 24kHz -> resample -> float32 8kHz -> μ-law -> base64
    """
    audio_24k = np.frombuffer(pcm24k, dtype="<i2").astype(np.float32) / 32768.0
    audio_8k = resample_poly(audio_24k, up=1, down=3).astype(np.float32)
    audio_8k = np.clip(audio_8k, -1.0, 1.0)

    ulaw = g711.encode_ulaw(audio_8k)
    return base64.b64encode(ulaw).decode("ascii")


# -----------------------------------------------------------------------------
# Minimal Nova Sonic bidirectional client with context capture
# -----------------------------------------------------------------------------

class VoiceAgent:
    """
    Minimal Nova Sonic bidirectional client.

    - Sends a system prompt once
    - Streams user audio (PCM16 16kHz)
    - Yields agent audio (PCM16 24kHz)
    - Maintains a conversation_log list[dict] with what was said
    """

    def __init__(self, region: str, model_id: str, voice_id: str, system_text: str):
        self._region = region
        self._model_id = model_id
        self._voice = voice_id
        self._system_text = system_text

        self._sdk_client: Optional[BedrockRuntimeClient] = None
        self._bidi_stream = None

        self._prompt_key = "main"
        self._open_audio_content: Optional[str] = None

        self._out_audio_q: "asyncio.Queue[bytes | None]" = asyncio.Queue()
        self._stop_flag = asyncio.Event()
        self._reader_task: Optional[asyncio.Task] = None

        self._active_role: Optional[str] = None
        self._text_accumulator: str = ""

        # Public conversation context
        self.conversation_log: List[Dict[str, Any]] = []

    # ---- conversation bookkeeping ----

    def _record(self, role: str, content: str) -> None:
        if self.conversation_log:
            try:
                last = self.conversation_log[-1]
                if last.get("role") == role and last.get("content") == content:
                    log.debug("Skipping duplicate conversation entry")
                    return
            except Exception:
                pass
            try:
                prev = self.conversation_log[-2]
                if prev.get("role") == role and prev.get("content") == content:
                    log.debug("Skipping duplicate conversation entry (prev)")
                    return
            except Exception:
                pass

        self.conversation_log.append({"role": role, "content": content})  


    # ---- AWS auth wiring ----

    def _mirror_boto3_creds_to_env(self) -> None:
        """
        Mirror credentials from boto3 into environment so the experimental SDK can use them.
        """
        sess = boto3.Session(region_name=self._region)
        creds = sess.get_credentials()
        if not creds:
            raise RuntimeError("No AWS credentials available via boto3")

        frozen = creds.get_frozen_credentials()
        os.environ["AWS_ACCESS_KEY_ID"] = frozen.access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
        if frozen.token:
            os.environ["AWS_SESSION_TOKEN"] = frozen.token

    # ---- session lifecycle ----

    async def begin(self) -> None:
        """
        Open the bidirectional stream, configure prompt/audio output, send system prompt,
        and start the background response reader.
        """
        self._mirror_boto3_creds_to_env()

        cfg = BedrockConfig(
            endpoint_uri=f"https://bedrock-runtime.{self._region}.amazonaws.com",
            region=self._region,
            aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
            http_auth_scheme_resolver=HTTPAuthSchemeResolver(),
            http_auth_schemes={"aws.auth#sigv4": SigV4AuthScheme()},
        )

        self._sdk_client = BedrockRuntimeClient(config=cfg)

        self._bidi_stream = await self._sdk_client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=self._model_id)
        )

        # sessionStart
        await self._emit_event(
            {
                "event": {
                    "sessionStart": {
                        "inferenceConfiguration": {
                            "maxTokens": 1024,
                            "topP": 0.9,
                            "temperature": 0.3,
                        },
                        "turnDetectionConfiguration": {
                            "endpointingSensitivity": "HIGH",
                        },
                    }
                }
            }
        )

        # promptStart (audio + text output configs)
        await self._emit_event(
            {
                "event": {
                    "promptStart": {
                        "promptName": self._prompt_key,
                        "textOutputConfiguration": {"mediaType": "text/plain"},
                        "audioOutputConfiguration": {
                            "mediaType": "audio/lpcm",
                            "sampleRateHertz": NOVA_OUT_HZ,
                            "sampleSizeBits": 16,
                            "channelCount": 1,
                            "voiceId": self._voice,
                            "encoding": "base64",
                            "audioType": "SPEECH",
                        },
                    }
                }
            }
        )

        # system prompt
        await self._send_system_block(self._system_text)

        # background reader
        self._reader_task = asyncio.create_task(self._read_loop())
    
    #phase 2 initiate convo by sending text to nova (agents wont start talking until they hear something)
    async def send_kickoff_text(self, text: str) -> None:
        """
        One-time USER text to trigger Nova to speak first.
        After kickoff, the conversation is audio-driven via Twilio streams.
        """
        if not text or self._stop_flag.is_set():
            return

        content_name = "kickoff-1"
        self._record("user", text)

        await self._emit_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": self._prompt_key,
                        "contentName": content_name,
                        "type": "TEXT",
                        "interactive": True,
                        "role": "USER",
                        "textInputConfiguration": {"mediaType": "text/plain"},
                    }
                }
            }
        )

        await self._emit_event(
            {
                "event": {
                    "textInput": {
                        "promptName": self._prompt_key,
                        "contentName": content_name,
                        "content": text,
                    }
                }
            }
        )

        await self._emit_event(
            {
                "event": {
                    "contentEnd": {
                        "promptName": self._prompt_key,
                        "contentName": content_name,
                    }
                }
            }
        )


    async def _emit_event(self, body: dict) -> None:
        if not self._bidi_stream:
            return

        raw = json.dumps(body).encode("utf-8")
        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=raw)
        )
        await self._bidi_stream.input_stream.send(chunk)


    async def _send_system_block(self, text: str) -> None:
        if not text:
            return

        content_name = "system-1"
        self._record("system_prompt", text)

        await self._emit_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": self._prompt_key,
                        "contentName": content_name,
                        "type": "TEXT",
                        "interactive": False,
                        "role": "SYSTEM",
                        "textInputConfiguration": {"mediaType": "text/plain"},
                    }
                }
            }
        )

        await self._emit_event(
            {
                "event": {
                    "textInput": {
                        "promptName": self._prompt_key,
                        "contentName": content_name,
                        "content": text,
                    }
                }
            }
        )

        await self._emit_event(
            {"event": {"contentEnd": {"promptName": self._prompt_key, "contentName": content_name}}}
        )

    # ---- audio input / output ----

    async def ensure_audio_input_open(self) -> None:
        if self._open_audio_content:
            return

        self._open_audio_content = "audio-user"

        await self._emit_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": self._prompt_key,
                        "contentName": self._open_audio_content,
                        "type": "AUDIO",
                        "interactive": True,
                        "role": "USER",
                        "audioInputConfiguration": {
                            "mediaType": "audio/lpcm",
                            "sampleRateHertz": NOVA_IN_HZ,
                            "sampleSizeBits": 16,
                            "channelCount": 1,
                            "audioType": "SPEECH",
                            "encoding": "base64",
                        },
                    }
                }
            }
        )

    async def push_audio(self, pcm16_16k: bytes) -> None:
        if not self._open_audio_content or self._stop_flag.is_set():
            return

        audio_b64 = base64.b64encode(pcm16_16k).decode("ascii")
        await self._emit_event(
            {
                "event": {
                    "audioInput": {
                        "promptName": self._prompt_key,
                        "contentName": self._open_audio_content,
                        "content": audio_b64,
                    }
                }
            }
        )

    async def close_audio_input(self) -> None:
        if not self._open_audio_content:
            return

        await self._emit_event(
            {
                "event": {
                    "contentEnd": {
                        "promptName": self._prompt_key,
                        "contentName": self._open_audio_content,
                    }
                }
            }
        )
        self._open_audio_content = None

    async def _read_loop(self) -> None:
        """
        Read events from Nova Sonic and:
        - enqueue audioOutput chunks
        - update conversation_log based on textOutput events, committed on contentEnd
        """
        if not self._bidi_stream:
            return

        n_audio = 0
        n_text = 0
        n_events = 0

        try:
            while not self._stop_flag.is_set():
                out_pair = await self._bidi_stream.await_output()
                out_stream = out_pair[1]

                msg = await out_stream.receive()
                if not getattr(msg, "value", None) or not getattr(msg.value, "bytes_", None):
                    continue

                payload = json.loads(msg.value.bytes_.decode("utf-8"))
                evt = payload.get("event", {})
                if not evt:
                    continue

                n_events += 1

                # contentStart: capture role + reset accumulator
                if "contentStart" in evt:
                    cs = evt["contentStart"]
                    role = cs.get("role")
                    if role:
                        self._active_role = role
                        self._text_accumulator = ""

                # audioOutput: forward to queue
                if "audioOutput" in evt:
                    audio_b64 = evt["audioOutput"].get("content")
                    if audio_b64:
                        n_audio += 1
                        await self._out_audio_q.put(base64.b64decode(audio_b64))

                # textOutput: accumulate
                if "textOutput" in evt:
                    txt = evt["textOutput"].get("content", "")
                    if txt:
                        n_text += 1
                        self._text_accumulator += txt

                # contentEnd: commit accumulated text as one message
                if "contentEnd" in evt:
                    if self._text_accumulator:
                        role = self._active_role or "ASSISTANT"
                        if role == "USER":
                            self._record("user", self._text_accumulator)
                        elif role == "SYSTEM":
                            self._record("system_prompt", self._text_accumulator)
                        else:
                            self._record("agent", self._text_accumulator)

                        self._text_accumulator = ""

        except (OSError, ValueError) as e:
            # Normal when the stream is closing and the reader is mid-receive
            if not self._stop_flag.is_set():
                log.error(f"Error in Nova Sonic response processing: {e}", exc_info=True)
        except Exception as e:
            log.error(f"Error in Nova Sonic response processing: {e}", exc_info=True)

        finally:
            log.info(
                "Nova Sonic stream closed. Events=%d, audio_chunks=%d, text_outputs=%d",
                n_events,
                n_audio,
                n_text,
            )
            await self._out_audio_q.put(None)

    async def audio_stream(self) -> AsyncIterator[bytes]:
        while not self._stop_flag.is_set():
            item = await self._out_audio_q.get()
            if item is None:
                break
            yield item

    async def shutdown(self) -> None:
        self._stop_flag.set()

    # Close stream first to unblock receives
        if self._bidi_stream:
            try:
                await self._bidi_stream.close()
            except Exception:
                pass

    # Let reader exit naturally; only cancel if it hangs
        if self._reader_task:
            try:
                await asyncio.wait_for(self._reader_task, timeout=2.0)
            except asyncio.TimeoutError:
                self._reader_task.cancel()
            except Exception:
                pass



# -----------------------------------------------------------------------------
# FastAPI app + routes
# -----------------------------------------------------------------------------

api = FastAPI()
api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# streamSid -> agent
active_calls: Dict[str, VoiceAgent] = {}


@api.get("/context/{stream_sid}")
async def fetch_conversation(stream_sid: str):
    """
    Inspect the conversation_log for a given call.
    """
    client = active_calls.get(stream_sid)
    if not client:
        return {"stream_sid": stream_sid, "context_window": []}
    return {"stream_sid": stream_sid, "context_window": client.conversation_log}

@api.post("/twilio_entrypoint")
async def twilio_entrypoint(req: Request):
    form = await req.form()
    call_sid = form.get("CallSid", "")
    log.info(f"[twilio webhook] CallSid={call_sid} From={form.get('From')} To={form.get('To')} Direction={form.get('Direction')}")

    _log_parent_call_sid(call_sid)


    ws_url = f"{PUBLIC_WSS_BASE}/voice_agent"

    say = ""
    if os.getenv("AUTO_START", "0") == "1":
        # Twilio speaks into the call (no AWS Polly needed)
        say = '<Say voice="Polly.Joanna">Hello. Please start with a short question.</Say>'

    twiml = f"""
<Response>
  {say}
  <Connect>
    <Stream url="{ws_url}" />
  </Connect>
</Response>
""".strip()
    return PlainTextResponse(content=twiml, media_type="text/xml")







@api.websocket("/voice_agent")
async def voice_agent(ws: WebSocket):
    await ws.accept()
    log.info("Twilio WebSocket connected")

    sid: Optional[str] = None
    bot: Optional[VoiceAgent] = None
    play_task: Optional[asyncio.Task] = None

    last_media_ts = asyncio.get_event_loop().time()
    keepalive_task: Optional[asyncio.Task] = None

    async def _nova_keepalive():
        # 20ms silence @ 16k PCM16 => 320 samples * 2 bytes = 640 bytes
        silence_pcm16_16k = b"\x00" * 640
        while True:
            await asyncio.sleep(1.0)
            if bot and not bot._stop_flag.is_set():
                now = asyncio.get_event_loop().time()
                # only push silence if nothing has arrived recently
                if (now - last_media_ts) > 1.5:
                    await bot.push_audio(silence_pcm16_16k)


    async def _playback_to_twilio(local_sid: str, client: VoiceAgent):
        """
        Read PCM16 24kHz audio from Nova Sonic and send μ-law frames back to Twilio.
        """
        try:
            FRAME_MS = 20
            BYTES_PER_SAMPLE = 1  # μ-law is 8-bit
            bytes_per_frame = int(TWILIO_HZ * FRAME_MS / 1000 * BYTES_PER_SAMPLE)

            async for pcm24 in client.audio_stream():
                b64_ulaw = _pcm16_24k_to_twilio_b64_ulaw(pcm24)
                ulaw_bytes = base64.b64decode(b64_ulaw)

                idx = 0
                while idx < len(ulaw_bytes):
                    frame = ulaw_bytes[idx : idx + bytes_per_frame]
                    if not frame:
                        break

                    frame_b64 = base64.b64encode(frame).decode("ascii")
                    await ws.send_json(
                        {
                            "event": "media",
                            "streamSid": local_sid,
                            "media": {"payload": frame_b64},
                        }
                    )
                    idx += bytes_per_frame

        except Exception as e:
            log.error(f"Error forwarding audio to Twilio: {e}", exc_info=True)

    try:
        while True:
            raw = await ws.receive_text()
            packet = json.loads(raw)
            evt_type = packet.get("event")

            if evt_type == "start":
                start = packet.get("start", {})
                sid = start.get("streamSid")
                log.info(f"Stream START: streamSid={sid}")

                bot = VoiceAgent(
                    region=REGION_NAME,
                    model_id=MODEL_ARN_OR_ID,
                    voice_id=VOICE_NAME,
                    system_text=BASE_SYSTEM_INSTRUCTIONS,
                )
                active_calls[sid] = bot

                await bot.begin()
                await bot.ensure_audio_input_open()

                ##phase 2 to initiate convo between two agents
                if AGENT_ROLE == "A" and KICKOFF_TEXT:
                    await bot.send_kickoff_text(KICKOFF_TEXT)

                keepalive_task = asyncio.create_task(_nova_keepalive())


                play_task = asyncio.create_task(_playback_to_twilio(sid, bot))

            elif evt_type == "media":
                last_media_ts = asyncio.get_event_loop().time()
                if not bot or not sid:
                    continue

                b64_payload = packet.get("media", {}).get("payload")
                if not b64_payload:
                    continue

                pcm16_16k = _twilio_b64_ulaw_to_pcm16_16k(b64_payload)
                await bot.push_audio(pcm16_16k)

            elif evt_type == "stop":
                log.info(f"Stream STOP: streamSid={sid}")
                break

    except WebSocketDisconnect:
        log.info("WebSocket disconnected")
    except Exception as e:
        log.error(f"Error in WebSocket handler: {e}", exc_info=True)
    finally:
        if  keepalive_task:
            keepalive_task.cancel()

        if  play_task:
            play_task.cancel()

        if bot:
            await bot.close_audio_input()
            await bot.shutdown()

        if sid:
           active_calls.pop(sid, None)




if __name__ == "__main__":
    import uvicorn

    listen_port = int(os.getenv("PORT", 8080))
    uvicorn.run(api, host="0.0.0.0", port=listen_port, log_level="info")
