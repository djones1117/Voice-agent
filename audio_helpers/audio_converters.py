import numpy as np
import g711
import base64
from scipy.signal import resample_poly
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
