"""EchoNote configuration.

All values can be overridden with environment variables. Nothing here reads
the network or the filesystem at import time (except a cheap ``is_file``
probe for a locally cached model), so tests can import this module freely.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from . import models

log = logging.getLogger("echonote.config")


def is_frozen() -> bool:
    """True when running from a PyInstaller build (see ``packaging/``)."""
    return bool(getattr(sys, "frozen", False))


def _bundle_dir() -> Path:
    """Root of read-only resources shipped inside the build.

    One-file builds unpack to ``sys._MEIPASS``; one-dir builds keep the
    resources next to the executable, which is also what ``_MEIPASS`` points
    at there. Falling back to the executable's folder keeps a third layout
    (app/ next to the exe) working without a spec-file change.
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    """Writable root, next to the executable in a portable build.

    Models must never land in ``_MEIPASS``: in a one-file build that folder is
    a temp dir wiped on exit, so the 460 MB download would repeat on every
    launch.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# Read-only bundled assets (frontend).
WEB_DIR = _bundle_dir() / "web"

# Writable root: models/ and logs/ live here.
ROOT_DIR = _data_dir()

# --------------------------------------------------------------------------
# Release identity
#
# The document version and the product version are intentionally separate:
# PRD v1.1 describes the target product v1.0.0, while what can be shipped
# today is the v0.9.0 tech preview. Every public surface (README, FastAPI
# version, screenshots) must agree with APP_VERSION, never with the document.
# --------------------------------------------------------------------------

APP_VERSION = "0.11.1"
APP_STAGE = "tech-preview"
PROTOCOL_VERSION = "2"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --------------------------------------------------------------------------
# Speech recognition
# --------------------------------------------------------------------------

MODEL_DIR = os.getenv("ECHONOTE_MODEL_DIR", str(ROOT_DIR / "models"))
def _sticky_choice() -> str | None:
    """The model the user picked in the UI last time, if any.

    The dropdown used to forget the choice on every restart, which made an
    explicit pick feel like it did not stick. The env var still wins: it is the
    operator's override, the file is the user's preference.
    """
    path = Path(MODEL_DIR) / "choice.txt"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


MODEL_CHOICE = os.getenv("ECHONOTE_MODEL", "").strip() or _sticky_choice()
LANGUAGE = os.getenv("ECHONOTE_LANGUAGE", "zh")

# ``ECHONOTE_OFFLINE=1`` forbids *any* network access: a missing model then
# fails fast with a "where to put the files" message instead of hanging on a
# 1.6 GB download. It is off by default only so that a brand-new install can
# still fetch its model once; loading a model that is already on disk never
# touches the network either way, which is what ``offline_ready`` reports.
def offline_mode() -> bool:
    """True when the app must not touch the network at all.

    Read from the environment on every call rather than cached at import, so
    the desktop entry can honour ``--offline`` after ``config`` was imported.
    """
    return _env_flag("ECHONOTE_OFFLINE")


def model_target() -> models.Target:
    """Resolve which model to load. Cheap enough to call per request."""
    return models.resolve(MODEL_DIR, MODEL_CHOICE, offline=offline_mode())


def remember_model(key: str | None) -> None:
    """Persist the user's model choice so a restart does not undo it."""
    path = Path(MODEL_DIR) / "choice.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if key:
            path.write_text(key + "\n", encoding="utf-8")
        elif path.exists():
            path.unlink()
    except OSError:
        log.warning("Could not persist the model choice to %s", path)


def _default_model() -> str:
    """Loader argument for the resolved model (path when it exists)."""
    return model_target().loader_value


MODEL_NAME = _default_model()

# Audio
SAMPLE_RATE = 16000

# Voice activity detection (energy based).
# NOTE: hangover/min-speech are expressed in milliseconds but consumed as
# whole 30 ms frames, so the effective values are 900 ms and 240 ms.
# Both values divide evenly by 30 ms on purpose: the earlier 650/280 ms
# settings silently became 630/270 ms, which made the documentation disagree
# with the runtime. See docs/consistency-checklist.md.
#
# Segmentation is an accuracy knob, not just a latency knob: Whisper degrades
# badly on fragments, so we cut only after a long pause and allow long
# utterances instead of slicing every 15 s.
VAD_THRESHOLD = _env_float("ECHONOTE_VAD_THRESHOLD", 0.010)
VAD_HANGOVER_MS = _env_int("ECHONOTE_VAD_HANGOVER_MS", 900)
VAD_MIN_SPEECH_MS = _env_int("ECHONOTE_VAD_MIN_SPEECH_MS", 240)
VAD_MAX_SEGMENT_S = _env_float("ECHONOTE_VAD_MAX_SEGMENT_S", 30)

# --------------------------------------------------------------------------
# Decoding
#
# These defaults favour accuracy over the last 200 ms. Partial captions stay
# greedy (they are thrown away as soon as the final arrives), while the final
# pass uses beam search with temperature fallback, which is what actually
# removes repeated or truncated Chinese sentences.
# --------------------------------------------------------------------------

ASR_BEAM_SIZE = _env_int("ECHONOTE_BEAM_SIZE", 5)
ASR_BEST_OF = _env_int("ECHONOTE_BEST_OF", 5)
ASR_PATIENCE = _env_float("ECHONOTE_PATIENCE", 1.0)
# ctranslate2's 0 means "decide from the machine"; overridable for benchmarks
# so a latency comparison is not polluted by thread-pool variance.
ASR_CPU_THREADS = _env_int("ECHONOTE_CPU_THREADS", 0)
# Temperatures handed to one decoding pass. faster-whisper accepts a tuple and
# retries a window at the next value when the result looks bad.
#
# Measured on this project's 30 s sample: the full ladder silently truncated
# the transcript to the first third (33 chars instead of 99) and was 40%
# slower, with medium and identical text at 0.0. Until there is a corpus that
# shows otherwise, the default is a single pass; the ladder stays available
# behind ECHONOTE_TEMP_FALLBACK=1 for experiments.
ASR_TEMPERATURES: tuple[float, ...] = (
    (0.0, 0.2, 0.4, 0.6, 0.8, 1.0) if _env_flag("ECHONOTE_TEMP_FALLBACK") else (0.0,)
)
ASR_NO_SPEECH_THRESHOLD = _env_float("ECHONOTE_NO_SPEECH_THRESHOLD", 0.6)
ASR_LOG_PROB_THRESHOLD = _env_float("ECHONOTE_LOG_PROB_THRESHOLD", -1.0)
ASR_COMPRESSION_RATIO_THRESHOLD = _env_float(
    "ECHONOTE_COMPRESSION_RATIO_THRESHOLD", 2.4
)
# Carry context across utterances. Off for partials: a half sentence should
# not bias the next one.
ASR_CONDITION_ON_PREVIOUS = _env_flag("ECHONOTE_CONDITION_ON_PREVIOUS", default=True)

# Content-neutral, but pins the output to Simplified Chinese with punctuation.
# Longer prompts leak their own wording into the transcript.
ASR_PROMPT_ZH = os.getenv(
    "ECHONOTE_PROMPT_ZH",
    "以下是普通话录音，请使用简体中文和标点符号转写。",
).strip()

# The same idea, cut to the part that only sets the *language* and not the
# task. Small models answer the instruction instead of transcribing, so they
# get this one: measured on `tiny`, the full prompt produced "请使用简体中文和
# 标点符号转写。" as a caption, while this keeps the output Simplified without
# anything to echo. Dropping the prompt entirely makes `tiny` write Traditional
# Chinese ("這是回生筆記的實實…"), which is why this exists at all.
ASR_PROMPT_ZH_SHORT = os.getenv(
    "ECHONOTE_PROMPT_ZH_SHORT",
    "以下是普通话录音。",
).strip()

# Partial captions.
#
# ``LIVE_STEP_S`` is how much *new* audio must arrive before the live caption
# is decoded again. It is an audio gate, not a wall-clock throttle: on this
# hardware one decode is the floor for latency, so asking for less audio than
# one decode costs only makes the queue deeper, never faster.
#
# The value is a compromise. Too small and every decode redoes audio it
# already covered (pure waste on a 4-core CPU); too large and the caption
# updates in visible jumps.
LIVE_STEP_S = _env_float("ECHONOTE_LIVE_STEP_S", 0.6)

# Live captions decode the utterance *while it grows* instead of waiting for
# the pause. Off restores the v0.10 behaviour (one decode per utterance).
LIVE_CAPTIONS = not _env_flag("ECHONOTE_DISABLE_LIVE")

# Which model produces the live caption. Empty means "the fastest installed
# model that is still readable", resolved in ``stt.live_target``. A separate,
# smaller live model is what makes captions appear in ~1 s instead of ~4 s;
# the accurate model then re-decodes each finished utterance in the background.
LIVE_MODEL = os.getenv("ECHONOTE_LIVE_MODEL", "").strip() or None

# Refine a finished utterance with the accurate model (emits a ``refine``
# message that replaces the provisional final). Only meaningful when the live
# model and the final model differ.
REFINE_FINALS = not _env_flag("ECHONOTE_DISABLE_REFINE")

# How many finished utterances may wait for their accurate pass. Measured on
# the 4-core target: the accurate pass takes ~4-6 s, one utterance arrives
# every ~5 s, so a depth of three rides out a burst without holding more than
# a few MB of audio (each entry is at most 30 s of float32).
# Accuracy beats latency here: with the accurate model at ~1.7x realtime, a
# three-item queue dropped refinements during ordinary speech, which silently
# left the *exported* text at live-model quality. The backlog is now measured
# in seconds of held audio (~0.95 MB per 15 s), not in items, so a slow model
# queues instead of losing work.
REFINE_BACKLOG_SECONDS = max(30.0, _env_float("ECHONOTE_REFINE_BACKLOG_S", 240.0))

# Upper bound on how long a closing session waits for pending refinements
# before it says "done". Anything still queued keeps its provisional text.
# 25 s covers two sentences of the accurate pass; longer would make "停止"
# feel stuck, shorter would export live-model text.
REFINE_DRAIN_TIMEOUT_S = _env_float("ECHONOTE_REFINE_DRAIN_TIMEOUT_S", 25.0)

# Upper bound on that wait. The drain scales with what is actually queued
# (the accurate pass is ~2.5x realtime), so a two-sentence backlog is waited
# for instead of exported at live-model quality.
REFINE_DRAIN_MAX_S = _env_float("ECHONOTE_REFINE_DRAIN_MAX_S", 60.0)

# Domain words (names, product terms, jargon) the decoder should prefer.
# Passed as ``hotwords``, not as part of the instruction prompt: hotwords bias
# the token choice without the model being able to "answer" them, which is how
# "回声笔记" survives a model that would otherwise write "回生笔记".
# A starter glossary, not a guess about the user's life: this is the app's own
# name, the one word every first recording says.
#
# Deliberately *one* term. hotwords are a decoder prompt, and `tiny` continues
# from them when the next spoken word does not match: with "回声笔记 语音识别"
# it emitted "语音识别 课今天天气…" — a stray character, deterministically, on
# every run. A term the user does not actually say makes the transcript worse,
# so the default stays minimal and everything else comes from the user.
DEFAULT_HOTWORDS = "回声笔记"

def _glossary_file() -> str | None:
    """Read ``models/hotwords.txt`` if the user keeps one there.

    A file beats a compiled-in default for the same reason the UI field beats
    the env var: the glossary is domain knowledge, and domain knowledge does
    not ship inside the binary. One term per line, ``#`` starts a comment.
    """
    path = Path(MODEL_DIR) / "hotwords.txt"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    words: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words.extend(line.split())
    return " ".join(words) or None


# Precedence: env var (operator) > hotwords.txt (this machine) > built-in.
HOTWORDS = (
    os.getenv("ECHONOTE_HOTWORDS", "").strip()
    or _glossary_file()
    or DEFAULT_HOTWORDS
) or None

# faster-whisper truncates hotwords to ``max_length // 2`` tokens (224 for the
# multilingual tokenizer) and does it *silently*, so a long glossary loses its
# tail with no visible symptom. Chinese is roughly one token per character plus
# the separators, so ~200 characters is where the cut starts. Measured: 52
# photography terms = 171 characters = 221 tokens.
HOTWORDS_TOKEN_BUDGET = 224

if HOTWORDS and len(HOTWORDS) > 200:
    log.warning(
        "词表 %d 个字符，超过 faster-whisper 的 %d token 上限，尾部会被静默丢弃；"
        "请删到约 50 个词以内",
        len(HOTWORDS),
        HOTWORDS_TOKEN_BUDGET,
    )

# A glossary longer than this is a document, not a glossary; the decoder
# prompt is a few hundred tokens at most.
HOTWORDS_MAX_CHARS = max(20, _env_int("ECHONOTE_HOTWORDS_MAX_CHARS", 200))

# --------------------------------------------------------------------------
# Protocol limits (PRD §7.1, FR-038, D-06, D-12)
# --------------------------------------------------------------------------

# One upstream frame: >=100 ms of audio, <=32 KiB.
MIN_FRAME_MS = 100
MIN_FRAME_BYTES = int(SAMPLE_RATE * MIN_FRAME_MS / 1000) * 2  # 3200 B
MAX_FRAME_BYTES = 32 * 1024

# Server-side audio backlog cap: 60 s of audio, drop-oldest on overflow.
AUDIO_QUEUE_MAX_SECONDS = 60.0
AUDIO_QUEUE_MAX_FRAMES = int(AUDIO_QUEUE_MAX_SECONDS * 1000 / MIN_FRAME_MS)  # 600

MAX_CONCURRENT_SESSIONS = 2
BUSY_RETRY_AFTER_S = 10

# How long a closed connection may keep the worker draining its tail.
SESSION_TAIL_TIMEOUT_S = 30.0

# Soft recording limit. 0 = unlimited (the UI must say so).
MAX_RECORD_MINUTES = max(0, _env_int("ECHONOTE_MAX_RECORD_MINUTES", 90))
MAX_RECORD_WARN_RATIO = 0.8

# Load the model at startup. Tests set ECHONOTE_SKIP_PRELOAD=1 so the suite
# never downloads a model.
PRELOAD = not _env_flag("ECHONOTE_SKIP_PRELOAD")

# Client-side memory budget for the in-flight recording (FR-045).
RECORDING_MEMORY_BUDGET_BYTES = 200 * 1024 * 1024

# --------------------------------------------------------------------------
# Server / security
# --------------------------------------------------------------------------

HOST = os.getenv("ECHONOTE_HOST", "127.0.0.1")
PORT = _env_int("ECHONOTE_PORT", 8000)

ALLOW_NONLOCAL = _env_flag("ECHONOTE_ALLOW_NONLOCAL")
ACCESS_TOKEN = os.getenv("ECHONOTE_ACCESS_TOKEN", "").strip()
ALLOWED_ORIGINS = frozenset(
    item.strip().rstrip("/")
    for item in os.getenv("ECHONOTE_ALLOWED_ORIGINS", "").split(",")
    if item.strip()
)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}


def is_loopback(host: str) -> bool:
    """True when ``host`` only accepts connections from this machine."""
    value = (host or "").strip().lower()
    if value in _LOOPBACK_HOSTS:
        return True
    return value.startswith("127.")
