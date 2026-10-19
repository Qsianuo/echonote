"""EchoNote configuration.

All values can be overridden with environment variables.
"""

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"

def _default_model() -> str:
    """Use a locally downloaded model directory when one is present.

    This avoids re-downloading from Hugging Face (which can stall on some
    networks) after ``models/faster-whisper-small`` has been fetched manually.
    """
    local = ROOT_DIR / "models" / "faster-whisper-small"
    if (local / "model.bin").is_file():
        return str(local)
    return "small"


# Speech recognition
MODEL_NAME = os.getenv("ECHONOTE_MODEL") or _default_model()
MODEL_DIR = os.getenv("ECHONOTE_MODEL_DIR", str(ROOT_DIR / "models"))
LANGUAGE = os.getenv("ECHONOTE_LANGUAGE", "zh")

# Audio
SAMPLE_RATE = 16000

# Voice activity detection (energy based)
VAD_THRESHOLD = float(os.getenv("ECHONOTE_VAD_THRESHOLD", "0.010"))
VAD_HANGOVER_MS = int(os.getenv("ECHONOTE_VAD_HANGOVER_MS", "650"))
VAD_MIN_SPEECH_MS = int(os.getenv("ECHONOTE_VAD_MIN_SPEECH_MS", "280"))
VAD_MAX_SEGMENT_S = float(os.getenv("ECHONOTE_VAD_MAX_SEGMENT_S", "15"))
PARTIAL_INTERVAL_S = float(os.getenv("ECHONOTE_PARTIAL_INTERVAL_S", "1.2"))

# Server
HOST = os.getenv("ECHONOTE_HOST", "127.0.0.1")
PORT = int(os.getenv("ECHONOTE_PORT", "8000"))
