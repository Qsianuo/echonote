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
# PRD v1.1 describes the target product v1.0.0, and v1.0.1 is the patch that
# ships it (content-hashed asset keys + full frontend test coverage).
# Every public surface (README, FastAPI version, screenshots) must agree with
# APP_VERSION, never with the document.
# --------------------------------------------------------------------------

# 构建时由 packaging/build.ps1 的 -Version 注入。以前这里写死一个字符串，
# build.ps1 的 -Version 只改 zip 文件名，于是出现过「包名 1.0.3、界面 1.0.2」
# 的两套版本号——发版时最容易误判自己验的是哪一版。
APP_VERSION = os.getenv("ECHONOTE_APP_VERSION", "1.0.3").strip() or "1.0.3"
APP_STAGE = "release"


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
# A segment is only confirmed after VAD_MIN_SPEECH_MS of voice, and the audio
# before that confirmation used to be thrown away — which clipped quiet onsets
# (soft vowels, unvoiced consonants) by up to ~240 ms. Keep this much audio
# from just before the trigger and prepend it to the utterance instead.
VAD_PREROLL_MS = _env_int("ECHONOTE_VAD_PREROLL_MS", 300)

# How much trailing audio to keep when a segment is closed. The old hard-coded
# 150 ms was tuned for long-form speech with clean pauses; spoken Chinese ends
# with a falling tone and a soft coda ("呢", "啊", trailing nasal) that often
# runs 200-400 ms *below* the VAD gate. A 150 ms tail cut those syllables off,
# which is what left single-character rows ("文") hanging at the end of a
# sentence. 1000 ms (1 s) covers the natural decay and then some, so a
# trailing particle or a soft last word is never lost to the cut; anything
# still lower than the gate by a wide margin is trimmed anyway (see
# ``Segmenter._finalize``).
VAD_TAIL_KEEP_MS = _env_int("ECHONOTE_VAD_TAIL_KEEP_MS", 1000)

# A final whose text equals the previous one, on a segment *shorter* than
# this, is one hallucination blip rather than two sentences. Measured in the
# field: room noise split into ~1 s segments, each decoding to "转写。" on
# `tiny`, which rendered as the same row three times (00:13 / 00:20 / 00:21)
# and read as a stuck app. The duration guard is what keeps the rule from
# eating real speech: a sentence a person actually says twice, after a pause,
# is longer than 1.5 s and keeps both rows — dropping the second would lose
# words from the transcript.
REPEAT_MAX_DURATION_S = _env_float("ECHONOTE_REPEAT_MAX_DURATION_S", 1.5)

# Level instrumentation for "I spoke and nothing appeared" reports.
#
# Without it the only evidence is a transcript with rows missing, which cannot
# distinguish the three causes: the mic delivers a quiet signal, the VAD gate
# sits above the speech level, or the decode is filtered afterwards. Logs the
# peak frame RMS and the live threshold once a second, plus one line per
# segment (duration, text length, why it was dropped). Off by default because
# it is one line a second.
VAD_DEBUG = _env_flag("ECHONOTE_VAD_DEBUG")

# --------------------------------------------------------------------------
# Recording modes（个人 / 会议）
#
# A mode is a named set of *segmentation* overrides applied when a recording
# session starts: ``StreamingSession._run`` builds one ``Segmenter`` per
# session, so a switch lands on the next recording without touching the one
# in flight. Keys must name ``Segmenter.__init__`` parameters; anything
# omitted keeps the ``VAD_*`` defaults above.
#
# ``meeting`` exists because a room is the opposite of desk dictation: the
# speaker is farther away (quieter) while keyboards, chairs and the HVAC are
# closer (louder). Raising the absolute gate would hurt exactly the speaker
# we are trying to hear, so the meeting preset instead raises
# ``gate_ratio`` — how far above the *measured room floor* a frame must
# stand to count as voice — and demands a longer run of consecutive speech
# before a segment is confirmed, which eats the short bursts (a clack, a
# cough) that pass any energy gate.
# --------------------------------------------------------------------------

MODES: dict[str, dict] = {
    "personal": {},
    "meeting": {
        "gate_ratio": 4.0,
        "hangover_ms": 1200,
        "min_speech_ms": 300,
        "preroll_ms": 400,
    },
}
DEFAULT_MODE = "personal"


def _sticky_mode() -> str:
    """The mode the user last picked, if any (same pattern as the backend)."""
    path = Path(MODEL_DIR) / "mode.txt"
    try:
        value = path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""
    return value if value in MODES else ""


MODE_CHOICE = os.getenv("ECHONOTE_MODE", "").strip().lower() or _sticky_mode()

# Read on every session start; ``POST /api/mode`` mutates it (and the sticky
# file), so a mode switch never needs a process restart.
ACTIVE_MODE = MODE_CHOICE or DEFAULT_MODE


def remember_mode(name: str | None) -> None:
    """Persist the mode choice so a restart does not undo it."""
    path = Path(MODEL_DIR) / "mode.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if name:
            path.write_text(name.strip().lower() + "\n", encoding="utf-8")
        elif path.exists():
            path.unlink()
    except OSError:
        log.warning("Could not persist the mode choice to %s", path)


def mode_overrides() -> dict:
    """``Segmenter`` kwargs for the active mode."""
    return dict(MODES.get(ACTIVE_MODE) or {})

# --------------------------------------------------------------------------
# Remote ASR backend (OpenAI-compatible /audio/transcriptions)
# --------------------------------------------------------------------------
#
# Why: large-v3-turbo runs at rtf 2.48 on a 4-core CPU, and the live + refine
# models compete for the same cores. A hosted Whisper answers the same audio in
# well under a second.
#
# Why it is off unless configured: it sends the recording off the machine.
# That contradicts the "audio never leaves this computer" claim the UI used to
# make, so it is strictly opt-in and the UI reports which backend is live.
#
# Defaults point at Groq, whose free tier covers 2000 requests and 8 hours of
# audio per day. Any OpenAI-compatible endpoint works: set the model name to
# whatever the provider calls it.
ASR_REMOTE_URL = os.getenv(
    "ECHONOTE_ASR_REMOTE_URL",
    "https://api.groq.com/openai/v1/audio/transcriptions",
).strip()
def _remote_key() -> str:
    """API key from the environment, else from ``models/asr-remote.key``.

    The file exists because setting an environment variable on Windows is a
    per-window affair: close the terminal and the key is gone, which reads as
    "the remote backend randomly stopped working". ``models/`` is gitignored,
    so a key kept there cannot be committed by accident.
    """
    raw = os.getenv("ECHONOTE_ASR_REMOTE_KEY", "").strip()
    if raw:
        return raw
    try:
        return (
            Path(MODEL_DIR) / "asr-remote.key"
        ).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


ASR_REMOTE_KEY = _remote_key()

ASR_REMOTE_MODEL = os.getenv(
    "ECHONOTE_ASR_REMOTE_MODEL", "whisper-large-v3-turbo"
).strip()
# Sent as ``prompt``. Empty by default: a hosted Whisper continues from the
# prompt when the audio does not match it, which is how a session ends up
# full of "转写。". ``language`` is passed separately and is enough to pin
# Simplified Chinese.
ASR_REMOTE_PROMPT = os.getenv("ECHONOTE_ASR_REMOTE_PROMPT", "").strip()
ASR_REMOTE_TIMEOUT_S = _env_float("ECHONOTE_ASR_REMOTE_TIMEOUT_S", 20.0)
# Live captions must fail fast: a 20 s timeout on a partial would stall the
# queue behind it.
ASR_REMOTE_LIVE_TIMEOUT_S = _env_float("ECHONOTE_ASR_REMOTE_LIVE_TIMEOUT_S", 6.0)
ASR_REMOTE_FAIL_THRESHOLD = _env_int("ECHONOTE_ASR_REMOTE_FAIL_THRESHOLD", 3)
ASR_REMOTE_COOLDOWN_S = _env_float("ECHONOTE_ASR_REMOTE_COOLDOWN_S", 30.0)
# True when both the endpoint and a key are present. Kept as a separate flag
# so a half-filled config silently stays local instead of failing every call.
ASR_REMOTE = bool(ASR_REMOTE_URL and ASR_REMOTE_KEY)


def _sticky_backend() -> str:
    """The engine the user last asked for, remembered across restarts.

    Same reasoning as ``_sticky_choice`` for the model dropdown, but the stakes
    are higher: an explicit "run this on the server" that evaporates on the
    next restart does not just reset a dropdown, it puts the CPU back on local
    inference while the user believes the work was offloaded. The env var
    still wins — it is the operator's override, the file is the preference.
    """
    path = Path(MODEL_DIR) / "asr-backend.txt"
    try:
        return path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""


_BACKEND_CHOICE = os.getenv("ECHONOTE_ASR_BACKEND", "").strip().lower() or _sticky_backend()


def remember_backend(name: str | None) -> None:
    """Persist the backend choice so a restart does not undo it."""
    path = Path(MODEL_DIR) / "asr-backend.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if name:
            path.write_text(name.strip().lower() + "\n", encoding="utf-8")
        elif path.exists():
            path.unlink()
    except OSError:
        log.warning("Could not persist the backend choice to %s", path)


# Strict mode: never fall back to the local model, and refuse to start
# recognition when the remote backend is not configured.
#
# Without it, a missing key silently degrades to local inference — the UI says
# "本地推理", the CPU stays pegged, and nothing tells the user their audio is
# being recognised on the machine they were trying to offload. Silent
# degradation is the wrong default when the whole point of the switch is "do
# not run this locally".
_STRICT_ENV = _env_flag("ECHONOTE_ASR_REMOTE_ONLY")
_PINNED_REMOTE = _BACKEND_CHOICE == "remote"

ASR_REMOTE_ONLY = _STRICT_ENV or _PINNED_REMOTE
# The combination that must surface as an error rather than a quiet fallback.
ASR_REMOTE_MISCONFIGURED = ASR_REMOTE_ONLY and not ASR_REMOTE

# --------------------------------------------------------------------------
# Local streaming ASR (sherpa-onnx zipformer)
# --------------------------------------------------------------------------
#
# A zipformer transducer is a purpose-built streaming ASR model, not a language
# model: 161 MB int8, no decoder-side LLM, rtf around 0.1 on CPU. It trades
# some accuracy on unusual vocabulary for a 20x lower CPU cost than
# ``large-v3-turbo``, which is the whole reason it exists here.
SHERPA_MODEL_DIR = os.getenv(
    "ECHONOTE_SHERPA_DIR", str(Path(MODEL_DIR) / "sherpa-zipformer-zh")
)
# Two threads already runs at several times real time; more just fights the
# browser for cores.
SHERPA_THREADS = _env_int("ECHONOTE_SHERPA_THREADS", 2)

# --------------------------------------------------------------------------
# Local offline ASR (sherpa-onnx SenseVoice)
# --------------------------------------------------------------------------
#
# SenseVoice-small is a non-streaming multilingual model (zh/en/ja/ko/yue,
# ~230 MB int8) from the FunASR family — the engine behind the recurring
# "中文比 Whisper 准" recommendation. It takes the *final* role only: it has
# no streaming mode, so with this backend the live caption stays on the
# zipformer (or the whisper live model), the same split the remote backend
# uses minus the network. Fetched with
# ``python tools/fetch_model.py --model sensevoice``.
SENSEVOICE_MODEL_DIR = os.getenv(
    "ECHONOTE_SENSEVOICE_DIR", str(Path(MODEL_DIR) / models.SENSEVOICE_DIRNAME)
)
# Same reasoning as SHERPA_THREADS: more threads just fight the browser.
SENSEVOICE_THREADS = _env_int("ECHONOTE_SENSEVOICE_THREADS", 2)
# Inverse text normalisation: "二零二六" -> "2026". Default on — these are notes.
SENSEVOICE_USE_ITN = _env_flag("ECHONOTE_SENSEVOICE_ITN", default=True)

# 离线翻译（可选）。模型放在 models/mt-<src>-<tgt>/，缺模型时功能自动关掉，
# 转写路径完全不受影响。Opus-MT 77M int8，实测单句 50~100 ms。
MT_ENABLED = _env_flag("ECHONOTE_MT", default=True)
# Marian 必须走 beam search：beam_size=1 会退化出重复短语（实测整行
# 「real-real-real-real-re-re-V」）。4 是 transformers 给这些模型的默认值。
MT_BEAM_SIZE = max(1, _env_int("ECHONOTE_MT_BEAM_SIZE", 4))
MT_MAX_DECODING_TOKENS = max(16, _env_int("ECHONOTE_MT_MAX_TOKENS", 256))

# Which engine actually decodes: "whisper" (faster-whisper), "sherpa"
# (streaming zipformer), "sensevoice" (offline SenseVoice finals, live caption
# on another engine) or "remote" (OpenAI-compatible endpoint). Explicit env
# wins, then the user's remembered choice, then a configured remote endpoint,
# then Whisper. Persisting the choice matters: without it a restart silently
# reverts to local inference while the UI still says what the user picked.
ASR_BACKEND = _BACKEND_CHOICE or ("remote" if ASR_REMOTE else "whisper")

# Live captions stay local even when the final transcript goes remote.
#
# This is structural, not cosmetic. The live path re-uploads the growing
# buffer every LIVE_STEP_S, so one utterance sends the same audio dozens of
# times: at the ~46 requests/minute this pipeline actually produces, a
# 2000-request/day free tier is gone in about 43 minutes and the upstream
# traffic is O(n^2). Keeping the caption local also means a slow or blocked
# endpoint degrades the final text instead of the thing the user is watching,
# and it removes the 6 s stall before the fallback fires.
#
# Strict mode overrides it: "never run here" has to mean never.
#
# Two different switches, deliberately not the same one:
#
# * ``ECHONOTE_ASR_REMOTE_ONLY=1`` is an operator statement — no inference on
#   this machine at all, caption included — so it forces the caption remote.
# * Pinning ``remote`` in ``models/asr-backend.txt`` only says where the final
#   text comes from. A local caption is a preview that the final replaces, not
#   the transcript, so it stays on the cheap local model.
ASR_LIVE_REMOTE = _env_flag("ECHONOTE_ASR_LIVE_REMOTE") or _STRICT_ENV




# Peak normalisation, experimental and **off by default**.
#
# Distant or quiet speech is the weakest point of the current pipeline, and
# scaling the buffer up to a fixed peak is the cheap fix a reference
# implementation uses. It is not switched on because faster-whisper already
# normalises inside its log-mel front end, so the gain may be mostly overlap
# and the risk (lifting room noise along with the voice) is real. Turn it on
# only after measuring CER with and without it on real recordings:
# ``ECHONOTE_PEAK_NORMALIZE=1``.
#
# ``PEAK_MAX_GAIN`` caps how far a nearly silent buffer is lifted: 8x is
# ~+18 dB, enough for a person across the room and not enough to turn hiss
# into something the decoder reads as speech.
PEAK_NORMALIZE = _env_flag("ECHONOTE_PEAK_NORMALIZE")
PEAK_TARGET = 0.95
PEAK_MAX_GAIN = 8.0

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

# Caption boilerplate Whisper emits when a segment carries energy but no
# speech. Measured in the field on `tiny`: room noise split by the VAD into
# ~1 s blips, each one decoding to "转写。" — the model continuing the subtitle
# convention it was trained on, not transcribing anything.
#
# These words are only dropped when the *whole* line is built out of them
# (see ``stt._is_hallucination``), so a real sentence that happens to contain
# "转写" or "订阅" survives untouched. The variants matter: the same session
# produced "转写。", "符号转写。" and "转写。 转写", and an exact-match list
# caught none of them.
ASR_HALLUCINATION_TEXTS = (
    "转写",
    "转写符号",
    "符号转写",
    "语音转写",
    "字幕",
    "本字幕",
    "中文字幕",
    "字幕由",
    "字幕制作",
    "谢谢观看",
    "谢谢大家观看",
    "感谢观看",
    "请不吝点赞",
    "请点赞",
    "点赞",
    "订阅",
    "关注",
    "转发",
    "打赏",
)

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

# 精修让行的三段线（2026-10-28 加入）。
#
# 原来的让行是「一刀切」：只要还在说话或前台解码，精修线程就完全不跑。实测
# 一场 33 分钟的连续讲课因此一路涨到 240 秒上限，被迫丢掉最老的几段，那些
# 段落永远停在流式文本（无标点、句子碎）——用户看到的就是「精修落后」。
# 现在按积压程度分三档：
#   * 积压 ≤ REFINE_YIELD_BELOW_S：维持原样，完全让给实时字幕（首屏延迟优先）
#   * 介于两者之间：限速让行，跑一段睡一小会，既不拖慢实时也不让队列涨上去
#   * ≥ REFINE_URGENT_BACKLOG_S：不再让行，先把队列压下来（实时可能抖一下）
REFINE_YIELD_BELOW_S = max(0.0, _env_float("ECHONOTE_REFINE_YIELD_BELOW_S", 60.0))
REFINE_URGENT_BACKLOG_S = max(
    REFINE_YIELD_BELOW_S + 1.0, _env_float("ECHONOTE_REFINE_URGENT_BACKLOG_S", 180.0)
)
# 限速时每处理完一段睡多久：按被处理音频长度的比例，最多 0.3 s。
REFINE_THROTTLE_SLEEP_S = max(0.0, _env_float("ECHONOTE_REFINE_THROTTLE_SLEEP_S", 0.3))
REFINE_THROTTLE_RATIO = max(0.0, _env_float("ECHONOTE_REFINE_THROTTLE_RATIO", 0.2))

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
# Deliberately empty by default. hotwords are a decoder prompt, and `tiny`
# continues from them when the next spoken word does not match: with
# "回声笔记 语音识别" it emitted "语音识别 课今天天气…" — a stray character,
# deterministically, on every run. A term the user does not actually say makes
# the transcript worse, so the glossary now starts empty and only ever contains
# what the user types for a specific recording.
#
# Field report behind this change: a photography glossary left in the box
# (焦距 光圈 景深 …) turned a whole session into "转写。" rows — the decoder
# followed the prompt instead of the audio. Empty is the safe default.
DEFAULT_HOTWORDS = ""

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


# --------------------------------------------------------------------------
# Term glossary（定稿后纠错用，和 HOTWORDS 是两件事）
#
# hotwords 是*解码提示*，词必须在模型词表里——流式 zipformer 只有 2002 个
# token，「焦」「曝」「匹」「伺」都不在表里，所以工业术语喂热词会被静默丢弃
# （实测 18 个候选词丢 7 个）。术语表是*解码之后*的同音替换，不受词表限制：
# 「脚距」→「焦距」、「暴光」→「曝光」。
#
# 文件：models/glossary.txt，一行一个词，# 开头注释。
# --------------------------------------------------------------------------
def _terms_file() -> list[str]:
    path = Path(MODEL_DIR) / "glossary.txt"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    terms: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        terms.extend(word for word in line.split() if len(word) >= 2)
    return terms


GLOSSARY_TERMS = _terms_file()

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

# 并发会话上限。"打开页面就占一个名额"（WS 一建立即占槽），本地单人工具 2 个
# 够用；云端体验页多人同时在线时 2 个会瞬间打满，后来者一律收到 busy。
MAX_CONCURRENT_SESSIONS = max(1, _env_int("ECHONOTE_MAX_SESSIONS", 2))
BUSY_RETRY_AFTER_S = _env_int("ECHONOTE_BUSY_RETRY_AFTER_S", 10)

# How long a closed connection may keep the worker draining its tail.
SESSION_TAIL_TIMEOUT_S = 30.0

# Soft recording limit. 0 = unlimited (the UI must say so).
MAX_RECORD_MINUTES = max(0, _env_int("ECHONOTE_MAX_RECORD_MINUTES", 90))
MAX_RECORD_WARN_RATIO = 0.8

# Load the model at startup. Tests set ECHONOTE_SKIP_PRELOAD=1 so the suite
# never downloads a model.
PRELOAD = not _env_flag("ECHONOTE_SKIP_PRELOAD")

# The `timing` message is always sent; this only adds a greppable
# ``[TURN_TIMING]`` log line, which is how a benchmark run is compared later
# without a browser attached.
TIMING_LOG = _env_flag("ECHONOTE_TIMING_LOG")

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
