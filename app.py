import os
import json
import base64
import logging
import asyncio
from typing import Dict, Any, List, AsyncIterator, Optional
import time
import boto3
import pathlib
import uuid
import db_utils as db
from contextlib import asynccontextmanager
from audio_helpers import _twilio_b64_ulaw_to_pcm16_16k, _pcm16_24k_to_twilio_b64_ulaw

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates

# import numpy as np
# from scipy.signal import resample_poly
# import g711

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

from twilio.rest import Client as TwilioClient


# Logging + environment configuration


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("novasonic-app")

REGION_NAME = os.getenv("AWS_REGION", "us-east-1")
MODEL_ARN_OR_ID = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-2-sonic-v1:0")

VOICE_NAME = os.getenv("AGENT_VOICE", "tiffany")
BASE_SYSTEM_INSTRUCTIONS = os.getenv(
    "AGENT_SYSTEM_PROMPT",
    "You are a helpful, friendly voice assistant named Sally." 
    "You like Lord of the rings. Respond to matthew's responses that are about Lord of the rings."
    "Speak clearly, keep responses brief, and be polite.",
)

NOVA_IN_HZ = 16000     # Nova Sonic audioInput sample rate (PCM16)
NOVA_OUT_HZ = 24000    # Nova Sonic audioOutput sample rate (PCM16)
TWILIO_HZ = 8000        # Twilio Media Streams μ-law 8 kHz




PUBLIC_WSS_BASE = os.getenv("WSS_VOICE_URL") # public URL base for the websocket agent endpoint 
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") # public URL base for the twilio entry point


TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")  # your Twilio phone number
DATABASE_URL = os.getenv("DATABASE_URL", "")
AGENT_NAME = os.getenv("AGENT_NAME", "A")
APP_INSTANCE = "A"
RUN_ID = os.getenv("RUN_ID", "")
SCHEMA_SQL = (pathlib.Path(__file__).resolve().parent / "schema.sql").read_text(encoding="utf-8")



#wrapper class for the novasonic streaming api that manages context windows - convo log
class VoiceAgent:
    """
    Minimal Nova Sonic bidirectional client.

    - Sends a system prompt once
    - Streams user audio (PCM16 16kHz)
    - Yields agent audio (PCM16 24kHz)
    - Maintains a conversation_log list[dict] with what was said
    """

    def __init__(self, region: str, model_id: str, voice_id: str, system_text: str, talk_first: bool = True):
        self._region = region
        self._model_id = model_id
        self._voice = voice_id
        self._system_text = system_text

        self._sdk_client: Optional[BedrockRuntimeClient] = None
        self._bidi_stream = None

        self._prompt_key = "main"
        self._inject_n = 0
        self._open_audio_content: Optional[str] = None

        self._out_audio_q: "asyncio.Queue[bytes | None]" = asyncio.Queue()
        self._stop_flag = asyncio.Event()
        self._reader_task: Optional[asyncio.Task] = None

        self._active_role: Optional[str] = None
        self._text_accumulator: str = ""

        # Public conversation context
        self.conversation_log: List[Dict[str, Any]] = []

        self._talk_first = talk_first


    # handles real time turns of the ongoing transcript,inserting role,content,etc to db

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


        entry = {
            "ts": time.time(),
            "app_instance": AGENT_NAME, 
            "role": role,
            "content": content,
                    }
        self.conversation_log.append(entry)
        print(entry)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                db.insert_message(
                    session_id=getattr(self, "session_id", "unknown"),
                    call_sid=getattr(self, "call_sid", None),
                    stream_sid=getattr(self, "stream_sid", None),
                    app_instance=AGENT_NAME,
                    role=role,
                    content=content,
                    ts_epoch=entry["ts"],
                )
            )
        except RuntimeError:
            pass

                                            



    async def inject_user_text(self, text: str):
        """
        Inject message as user role to NovaSonic. No audio, just text.  
        Simulates user saying something without them actually saying something.

        """
        promptName = self._prompt_key
        self._inject_n += 1
        contentName = f"injection-{self._inject_n}"
        #transmits -> novasonic
        await self._emit_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": promptName,
                        "contentName": contentName,
                        "type": "TEXT",
                        "interactive": True,
                        "role": "USER",
                        "textInputConfiguration": {"mediaType": "text/plain"}
                    }
                }
            }
        )
        await self._emit_event(
            {
                "event": {
                    "textInput": {
                        "promptName": promptName,
                        "contentName": contentName,
                        "content": text
                    }
                }
            }
        )
        await self._emit_event(
            {
                "event": {
                    "contentEnd": {
                        "promptName": promptName,
                        "contentName": contentName
                    }
                }
            }
        )


    ##not being used currently - would be used if deployed to aws 

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

    #starts connection to novasonic api
    #referenced from the novasonic doc + llm
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

        # make agent talk first
        if self._talk_first:
            await self.inject_user_text("hello")

    #formatting events that we send to novasonic
    async def _emit_event(self, body: dict) -> None:
        if not self._bidi_stream:
            return

        raw = json.dumps(body).encode("utf-8")
        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=raw)
        )
        await self._bidi_stream.input_stream.send(chunk)
    #system prompting for the agent
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

    # audio input and output based off novasonic github sample

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
    # referenced from novasonic docs to ensure audio output is formatted correctly   

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

    #manages the literal audio stream    

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
        if self._reader_task:
            self._reader_task.cancel()
        if self._bidi_stream:
            try:
                await self._bidi_stream.close()
            except Exception:
                pass






























@asynccontextmanager
async def lifespan(app: FastAPI):
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")
    await db.init_pool(DATABASE_URL)
    await db.ensure_schema(SCHEMA_SQL)
    yield
    await db.close_pool()




####routes

api = FastAPI(lifespan=lifespan)
api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

# streamSid -> agent
active_calls: Dict[str, VoiceAgent] = {}

def _speaker_from_role(app_instance: str, role: str) -> str:
    if role == "user":
        return "user"
    if role == "system_prompt":
        return "system"
    return f"agent_{app_instance}"





@api.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "example_number": "+155555555",
    })

@api.get("/sessions")
async def sessions():
    return {"sessions": await db.list_sessions(limit=50)}

@api.get("/sessions/{session_id}")
async def get_session(session_id: str):
    msgs = await db.fetch_session(session_id)
    for m in msgs:
        m["speaker"] = _speaker_from_role(m.get("app_instance", "?"), m.get("role", ""))
    return {"session_id": session_id, "messages": msgs}


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
    """
    Twilio Voice webhook: respond with TwiML that connects to agent WebSocket.
    """
    ws_url = f"{PUBLIC_WSS_BASE}/voice_agent"
    twiml = f"""
<Response>
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

                call_sid = start.get("callSid")  
                base_session = call_sid or str(uuid.uuid4())
                session_id = f"{RUN_ID}:{base_session}" if RUN_ID else base_session
                #instantiate the voice agent and pass in params
                bot = VoiceAgent(
                    region=REGION_NAME,
                    model_id=MODEL_ARN_OR_ID,
                    voice_id=VOICE_NAME,
                    system_text=BASE_SYSTEM_INSTRUCTIONS,
                    talk_first=False
                )

                bot.session_id = session_id
                bot.call_sid = call_sid
                bot.stream_sid = sid
                active_calls[sid] = bot

                await bot.begin()
                await bot.ensure_audio_input_open()

                play_task = asyncio.create_task(_playback_to_twilio(sid, bot))

            elif evt_type == "media":
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
        if bot:
            await bot.close_audio_input()
            await bot.shutdown()
        if play_task:
            play_task.cancel()
        if sid and sid in active_calls:
            del active_calls[sid]
        await ws.close()








##purely twilio api - referenced from the twilio docs
def trigger_outbound_call(to_number: str) -> str:
    """
    Triggers an outbound call via Twilio and points it at our TwiML webhook
    (/twilio_entrypoint) so the callee gets connected to the same websocket agent.

    Returns:
        Twilio Call SID
    """
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        raise RuntimeError("Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER env vars")

    if not PUBLIC_BASE_URL:
        raise RuntimeError("Missing PUBLIC_BASE_URL")

    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    # Important: this must be a PUBLICLY reachable URL that returns TwiML
    twiml_url = f"{PUBLIC_BASE_URL}/twilio_entrypoint"

    call = client.calls.create(
        to=to_number,
        from_=TWILIO_FROM_NUMBER,
        url=twiml_url,
        method="POST",  # your endpoint supports POST
    )

    return call.sid



##mechanism to trigger the func through index.html
@api.post("/start_outbound_call")
async def start_outbound_call(payload: Dict[str, Any]):
    """
    Trigger an outbound call that connects to the existing voice agent pipeline.

    Request JSON:
      {"to": "+15551234567"}

    Response:
      {"call_sid": "..."}
    """
    to_number = (payload or {}).get("to")
    if not to_number:
        raise HTTPException(status_code=400, detail="Missing 'to' in JSON body")

    try:
        call_sid = trigger_outbound_call(to_number)
        return {"ok": True, "to": to_number, "call_sid": call_sid}
    except Exception as e:
        log.error(f"Failed to start outbound call: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))















if __name__ == "__main__":
    import uvicorn

    listen_port = int(os.getenv("PORT", 8080))
    uvicorn.run(api, host="0.0.0.0", port=listen_port, log_level="info")
