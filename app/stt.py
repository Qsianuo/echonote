"""Local speech-to-text engine backed by faster-whisper.

The model is loaded once and shared by all sessions. Loading is
**recoverable**: a failure stores a sanitised reason, clears itself on the
next successful attempt, and can be retried without restarting the process
(PRD FR-036).

The engine is intentionally defensive at import time — ``faster_whisper`` is
imported inside :func:`load` so that API tests, linting and the version
matrix (RG-14) do not need the model stack installed.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from . import config, models
from . import asr_remote
from . import asr_sherpa

log = logging.getLogger("echonote.stt")

MAX_AUTO_RETRIES = 3
AUTO_RETRY_DELAY_S = 60.0

# Live caption gate. Buffers shorter than this are checked against
# ``LIVE_MIN_LOGPROB``: that is where the model invents words instead of
# reading them. The threshold is measured, not guessed — on ``base`` with the
# sample utterance, every *genuine* reading of a 1.2-2.0 s buffer scored
#   -1.09 (1.2 s "你好,这……")  -0.85 (1.4 s)  -0.71 (1.6 s)  -0.33 (2.0 s)
# so a gate at -0.85 was throwing away correct captions. -1.2 keeps all of
# them and still catches a decode that is inventing rather than reading.
# (The 1.0 s row of the same table was the *prompt* echoed back at -0.09 with
# full confidence — confidence alone cannot spot that; ``_strip_echo`` does.)
LIVE_SHORT_BUFFER_S = 2.0
LIVE_MIN_LOGPROB = -1.2

_model = None
_load_lock = threading.Lock()
_infer_lock = threading.Lock()
_ready = threading.Event()
_error: str | None = None
_loading = False
_pending_loads = 0
_attempts = 0
_auto_retries = 0
_last_failure_at = 0.0
_scheduler_started = False
_shutdown = threading.Event()
# Bumped on every switch so an in-flight load cannot overwrite a newer choice.
_generation = 0
_loaded_key: str | None = None
# Set when the failure needs a human (model not on disk while offline).
# Auto-retry would just repeat the same certain failure forever.
_retry_blocked = False
# Last few final-pass real-time factors. Median, not mean: one slow utterance
# caused by a background build should not relabel the model as unusable.
_rtf_samples: deque[float] = deque(maxlen=5)

# --------------------------------------------------------------------------
# Live (low-latency) engine
#
# A second, smaller model runs the captions while the user is still speaking;
# the accurate model above only decodes each *finished* utterance. They are
# separate instances with separate locks on purpose: sharing one lock would
# make a 4 s final decode starve the 0.9 s live decode, which is exactly the
# stutter this architecture removes.
# --------------------------------------------------------------------------

_live_model = None
_live_key: str | None = None
_live_error: str | None = None
_live_ready = threading.Event()
_live_load_lock = threading.Lock()
_live_infer_lock = threading.Lock()
# Wall time of recent live decodes. The UI shows this instead of the RTF of
# finals, because "captions appear 0.9 s after you speak" is the number a user
# can actually feel.
_live_calls: deque[float] = deque(maxlen=9)


def rtf() -> float | None:
    """Median real-time factor of recent finals, or None before the first."""
    if not _rtf_samples:
        return None
    values = sorted(_rtf_samples)
    return round(values[len(values) // 2], 2)


# --------------------------------------------------------------------------
# Live engine resolution
# --------------------------------------------------------------------------

# Preference order for the live caption model: the fastest one whose Chinese
# output is still readable. ``tiny`` is deliberately last — it transcribes
# Mandarin into Traditional characters, which looks broken rather than fast.
_LIVE_PREFERENCE = models.LIVE_PREFERENCE


def live_target() -> models.Target:
    """Which model should produce the live caption.

    Resolution:
    1. ``ECHONOTE_LIVE_MODEL`` when that model is installed;
    2. the fastest installed model that is **not larger** than the chosen one;
    3. otherwise the chosen model itself.

    Rule 2 is size-capped on purpose. Picking the 75 MB tier used to leave the
    caption on the 145 MB model and then skip refinement because "the final
    model is smaller", so the 75 MB model never ran at all — the user chose a
    model and got a different one. Capping at the chosen size makes an explicit
    choice mean what it says: pick ``tiny`` and ``tiny`` draws the caption.
    """
    explicit = config.LIVE_MODEL
    if explicit:
        target = models.resolve(config.MODEL_DIR, explicit)
        if target.path is not None:
            return target

    main = config.model_target()
    ceiling = _size_of(main)
    installed = {
        spec.key
        for spec in models.available(config.MODEL_DIR)
        if spec.size_mb <= ceiling
    }
    for key in _LIVE_PREFERENCE:
        if key in installed:
            return models.resolve(config.MODEL_DIR, key)
    return main


def live_is_separate() -> bool:
    """True when the live caption model is not the final model."""
    if config.ASR_BACKEND == "sherpa":
        # One streaming recognizer serves both roles. There is no second
        # model, so the size comparison below would compare two whisper
        # models that are not even loaded — and "refine" would then re-decode
        # every utterance with the same weights it was just decoded with.
        return False
    main = config.model_target()
    live = live_target()
    if live.key != main.key:
        return True
    # Same key but a different resolved path (a hand-placed copy) still means
    # one model, one instance.
    return live.path != main.path


def load_live() -> bool:
    """Load the live model if it differs from the already-loaded one.

    Returns True when a live-capable model is available — either a dedicated
    one or the main model. Failure is never fatal: the caller falls back to
    the main engine, which is slower but correct.
    """
    global _live_model, _live_key, _live_error

    if config.ASR_BACKEND == "sherpa":
        # One streaming recognizer serves both the live and the final role.
        with _live_load_lock:
            _live_model = None
            _live_key = asr_sherpa.label()
            _live_error = None
            _live_ready.set()
        return True

    if config.ASR_REMOTE:
        # The live model would never run, so loading 145 MB of weights for it
        # is pure waste — and the whole point of the switch is to stop paying
        # that cost.
        with _live_load_lock:
            _live_model = None
            _live_key = asr_remote.model_label()
            _live_error = None
            _live_ready.set()
        return True

    main = config.model_target()
    target = live_target()
    if target.key == main.key and target.path == main.path:
        # One model serves both roles; drop any dedicated instance so the
        # memory is not held twice.
        with _live_load_lock:
            _live_model = None
            _live_key = None
            _live_error = None
            _live_ready.clear()
        return _ready.is_set()

    with _live_load_lock:
        if _live_model is not None and _live_key == target.key:
            return True
        if target.path is None:
            _live_error = "live model not on disk"
            log.warning("Live model %s missing; falling back to %s", target.key, main.key)
            return False
        try:
            from faster_whisper import WhisperModel

            models.apply_offline_env()
            log.info("Loading live model '%s' (%s)", target.key, target.label)
            started = time.perf_counter()
            _live_model = WhisperModel(
                target.loader_value,
                device="cpu",
                compute_type="int8",
                download_root=config.MODEL_DIR,
                local_files_only=True,
                cpu_threads=config.ASR_CPU_THREADS,
            )
            _live_key = target.key
            _live_error = None
            _live_ready.set()
            # Warm the encoder before the first real utterance. A cold first
            # decode took 2.8 s instead of 0.9 s in measurement, and that cost
            # landed exactly on the "first caption" number.
            try:
                _live_model.transcribe(
                    np.zeros(config.SAMPLE_RATE, dtype=np.float32),
                    language="zh",
                    beam_size=1,
                    without_timestamps=True,
                )
            except Exception:  # noqa: BLE001 - a warm-up must never fail a load
                log.debug("Live warm-up decode failed", exc_info=True)
            log.info(
                "Live model ready: %s in %.2fs", target.key, time.perf_counter() - started
            )
            return True
        except Exception as exc:  # noqa: BLE001 - degrade, never crash
            _live_error = sanitize_error(exc)
            _live_model = None
            _live_ready.clear()
            log.exception("Failed to load live model")
            return False

# Absolute paths deeper than two levels leak the user's directory layout
# into the UI (FR-020 AC2). URLs keep their host because they are useful.
_ABS_PATH = re.compile(
    r"(?:[A-Za-z]:\\(?:[^\s\\\"'<>|]+\\){2,}[^\s\\\"'<>|]+"
    r"|/(?:[\w.\-]+/){2,}[\w.\-]+)"
)
_QUERY = re.compile(r"\?[^\s\"']*")


def _basename(path: str) -> str:
    """Last path segment, for both separators, on every platform.

    ``pathlib.Path(...).name`` is wrong here: on Linux a backslash is an
    ordinary filename character, so ``Path("C:\\Users\\alice\\model.bin").name``
    returns the whole string and the username leaks into the UI. That passed
    on Windows and failed on Linux CI.
    """
    tail = re.split(r"[\\/]", path)
    return tail[-1] if tail and tail[-1] else "…"


def sanitize_error(message: object) -> str:
    """Make an exception message safe to show in the UI.

    Removes query strings, collapses absolute paths to their basename and
    caps the length. Never returns a traceback.
    """
    text = str(message or "").strip()
    if not text:
        return "未知错误"
    text = _QUERY.sub("", text)
    text = _ABS_PATH.sub(lambda match: _basename(match.group(0)), text)
    text = " ".join(text.split())
    return text[:300]


def load() -> bool:
    """Load the model once. Returns True when the model is usable.

    Safe to call from any thread; concurrent callers serialise on the lock.
    """
    global _model, _error, _loading, _attempts, _auto_retries, _last_failure_at
    global _loaded_key, _retry_blocked

    if config.ASR_BACKEND == "sherpa":
        # Explicit backend choice wins over everything: a zipformer needs no
        # faster-whisper weights, so nothing else should be loaded for it.
        ok = asr_sherpa.load()
        _loaded_key = asr_sherpa.label()
        if ok:
            _error = None
            _ready.set()
        else:
            _error = "sherpa-onnx 模型未就绪：请检查 models/sherpa-zipformer-zh 下的四个文件"
            _ready.clear()
            _retry_blocked = True
            log.error("%s", _error)
        return ok

    if config.ASR_REMOTE:
        # Nothing to load: the decoder sits at the other end of an HTTP call.
        # Marking ready here (rather than skipping the load entirely) keeps
        # every caller's ``is_ready`` gate working unchanged.
        _loaded_key = asr_remote.model_label()
        _error = None
        _ready.set()
        return True

    if config.ASR_REMOTE_ONLY:
        # Recognition was explicitly pinned to the remote backend and the
        # backend is not configured. Loading a local model here would be the
        # silent fallback this mode exists to prevent, so fail with something
        # the UI can show instead.
        _error = (
            "远程识别已启用（ECHONOTE_ASR_REMOTE_ONLY=1），但缺少 API Key。"
            "请设置 ECHONOTE_ASR_REMOTE_KEY，或写入 models/asr-remote.key。"
        )
        _loaded_key = None
        _ready.clear()
        _retry_blocked = True
        log.error("%s", _error)
        return False

    generation = _generation
    with _load_lock:
        if _model is not None:
            return True

        _loading = True
        _attempts += 1
        _error = None
        try:
            target = config.model_target()
            if target.path is None:
                # No usable model on disk. When offline we refuse immediately:
                # blocking for minutes on a download that cannot succeed is
                # the worst possible failure mode for an air-gapped machine.
                hint = models.missing_hint(config.MODEL_DIR, target)
                if config.offline_mode():
                    _error = hint
                    _last_failure_at = time.monotonic()
                    # Nothing changes until a human moves files, so stop the
                    # auto-retry loop from burning a tick every 60 s forever.
                    _retry_blocked = True
                    log.warning("Offline mode with no local model: %s", hint)
                    return False
                log.info("Model %s not on disk, one-time download allowed", target.key)

            # Blocks any accidental hub call from inside faster-whisper. Set
            # before the import so the library picks it up at module load.
            models.apply_offline_env()

            from faster_whisper import WhisperModel

            log.info(
                "Loading Whisper model '%s' (%s, %s)...",
                target.key,
                target.label,
                target.source,
            )
            # Threads stay on "let ctranslate2 decide". The two decodes are
            # kept apart by the session (refinement only starts while the user
            # is silent, see StreamingSession._refine_loop) rather than by
            # starving one of them: a half-threaded refinement still overlapped
            # the live decode, it was just slower at it.
            model = WhisperModel(
                target.loader_value,
                device="cpu",
                compute_type="int8",
                download_root=config.MODEL_DIR,
                local_files_only=target.path is not None,
                cpu_threads=config.ASR_CPU_THREADS,
            )
            if generation != _generation:
                # A switch happened while this load was running; the newer
                # request owns the output, so do not resurrect the old model.
                log.info("Discarding stale model load (superseded by a switch)")
                return False

            _model = model
            _loaded_key = target.key
            _error = None
            _auto_retries = 0
            _ready.set()
            log.info("Whisper model ready: %s", target.key)
            # The live caption engine is a second, smaller model. Loading it
            # here keeps "model ready" a single promise for the UI: by the
            # time it flips to ready, both roles are filled.
            load_live()
            return True
        except Exception as exc:  # noqa: BLE001 - the message is the product surface
            _error = sanitize_error(exc)
            _last_failure_at = time.monotonic()
            log.exception("Failed to load Whisper model")
            return False
        finally:
            _loading = False


def retry() -> dict:
    """Manual retry entry point (``POST /api/retry``).

    Resets the automatic-retry budget so a user who fixed their environment
    gets a fresh set of attempts. Returns immediately; poll ``status()``.
    """
    global _auto_retries, _pending_loads, _retry_blocked

    if _ready.is_set():
        return status()

    _auto_retries = 0
    # A human asked, so allow the attempt even if the automatic policy gave up.
    _retry_blocked = False
    # Never block on _load_lock here: an in-flight load can take minutes and
    # the UI needs an immediate answer ("正在重试").
    if not _loading:
        _pending_loads += 1
        threading.Thread(target=_load_tracked, name="whisper-retry", daemon=True).start()
    return status()


def _load_tracked() -> None:
    global _pending_loads
    try:
        load()
    finally:
        _pending_loads = max(0, _pending_loads - 1)


def switch(model_key: str) -> dict:
    """Swap to another **installed** model without restarting the process.

    Switching from the UI must never start a download: someone in the middle
    of a meeting should not discover a 1.6 GB pull. Missing models are
    rejected with the folder they should be placed in.
    """
    global _model, _error, _pending_loads, _generation, _retry_blocked

    target = models.resolve(config.MODEL_DIR, model_key)
    if target.path is None:
        return {"ok": False, "error": models.missing_hint(config.MODEL_DIR, target)}

    current = config.model_target()
    if target.path == current.path and _ready.is_set() and _loaded_key == target.key:
        return {"ok": True, "switching": False, "key": target.key}

    # No lock here: an in-flight load can hold it for minutes, and the HTTP
    # request must answer immediately. The generation counter makes the old
    # load discard its own result instead.
    _generation += 1
    config.MODEL_CHOICE = model_key
    config.remember_model(model_key)
    _model = None
    _ready.clear()
    _error = None
    _retry_blocked = False
    # The live role may now resolve to a different model, so drop the old
    # instance instead of keeping a caption engine the user just replaced.
    _drop_live()
    _pending_loads += 1
    threading.Thread(target=_load_tracked, name="whisper-switch", daemon=True).start()
    log.info("Switching model to %s", target.key)
    return {"ok": True, "switching": True, "key": target.key}


def _drop_live() -> None:
    global _live_model, _live_key, _live_error

    with _live_load_lock:
        _live_model = None
        _live_key = None
        _live_error = None
        _live_ready.clear()


def maybe_auto_retry() -> bool:
    """One scheduling tick of the auto-retry policy (FR-036 AC4).

    Returns True when a retry was started. Stops after
    ``MAX_AUTO_RETRIES`` consecutive failures so an offline machine does not
    spin forever; the manual button keeps working afterwards.
    """
    global _auto_retries

    if _ready.is_set() or _error is None or _loading or _pending_loads:
        return False
    if _retry_blocked:
        return False
    if _auto_retries >= MAX_AUTO_RETRIES:
        return False
    if time.monotonic() - _last_failure_at < AUTO_RETRY_DELAY_S:
        return False

    _auto_retries += 1
    log.info("Auto-retrying model load (%d/%d)", _auto_retries, MAX_AUTO_RETRIES)
    _load_tracked()
    return True


def _retry_scheduler() -> None:
    """Background loop that calls :func:`maybe_auto_retry` every second."""
    while not _shutdown.wait(1.0):
        try:
            maybe_auto_retry()
        except Exception:  # pragma: no cover - the scheduler must never die
            log.exception("Auto-retry tick failed")


def preload() -> None:
    """Warm up the model in the background at server startup."""
    global _scheduler_started, _pending_loads

    _pending_loads += 1
    threading.Thread(target=_load_tracked, name="whisper-preload", daemon=True).start()

    if not _scheduler_started:
        _scheduler_started = True
        threading.Thread(target=_retry_scheduler, name="whisper-retry-loop", daemon=True).start()


def shutdown() -> None:
    _shutdown.set()


def is_ready() -> bool:
    return _ready.is_set()


def status() -> dict:
    attempts = _attempts
    target = config.model_target()
    live = live_target()
    result = {
        "ready": _ready.is_set(),
        "model": target.loader_value,
        "error": _error,
        "retrying": bool(_loading or _pending_loads),
        "attempts": attempts,
        "auto_retry_exhausted": bool(_error is not None and _auto_retries >= MAX_AUTO_RETRIES),
        # Progress hint for the UI ("正在重试（第 n 次）").
        "retry_count": _auto_retries,
        "model_label": target.label,
        "model_key": target.key,
        # True means the weights sit on this disk, so recognition works with
        # the network unplugged (FR-046 AC1).
        "offline_ready": target.path is not None,
        # Measured, not estimated: see :func:`rtf`.
        "rtf": rtf(),
        # Live caption role: which model produces the "边说边出字" line, and
        # whether a second pass will replace it with a more accurate version.
        "live_model": live.loader_value,
        "live_model_key": live.key,
        "live_model_label": live.label,
        "live_separate": live_is_separate(),
        "live_ready": bool(_live_ready.is_set() or _ready.is_set()),
        "live_error": _live_error,
        "refine": refine_enabled(),
        # Why the second pass is off, so the UI can say something better than
        # "nothing happens when I pick a model".
        "refine_blocked": refine_blocked_reason(),
        "live_ms": live_ms(),
    }
    if config.ASR_BACKEND == "sherpa":
        # Same reasoning as the remote branch: the faster-whisper names would
        # describe a model that is not loaded and will not be used.
        result["backend"] = "sherpa"
        result["model"] = asr_sherpa.label()
        result["model_key"] = "sherpa"
        result["model_label"] = "sherpa 流式"
        result["live_model"] = asr_sherpa.label()
        result["live_model_key"] = "sherpa"
        result["live_model_label"] = "sherpa 流式"
        result["rtf"] = None
        result["offline_ready"] = asr_sherpa.is_installed()
    elif config.ASR_REMOTE:
        # The local model names are meaningless here: nothing was loaded, and
        # reporting "base" while an HTTP call does the work would send anyone
        # debugging this in the wrong direction.
        result.update(asr_remote.status())
        result["backend"] = "remote"
        result["model"] = config.ASR_REMOTE_MODEL
        result["model_key"] = "remote"
        result["model_label"] = "远程"
        result["live_model"] = config.ASR_REMOTE_MODEL
        result["live_model_key"] = "remote"
        result["live_model_label"] = "远程"
        result["rtf"] = None
        # Recognition now needs the network, so the offline promise does not
        # hold. Saying otherwise would be a lie the UI repeats to the user.
        result["offline_ready"] = False
    else:
        result["backend"] = "remote" if config.ASR_REMOTE_ONLY else "local"
        if config.ASR_REMOTE_ONLY:
            # Pinned to remote but unusable: report the backend the user chose
            # (so the UI stops claiming "本地推理") and let ``error``/``ready``
            # carry the reason. Silently reporting "local" is what made this
            # invisible in the first place.
            result["model"] = config.ASR_REMOTE_MODEL
            result["model_key"] = "remote"
            result["model_label"] = "远程"
            result["live_model"] = config.ASR_REMOTE_MODEL
            result["live_model_key"] = "remote"
            result["live_model_label"] = "远程"
            result["rtf"] = None
            result["offline_ready"] = False
    return result


# ``tiny`` cannot follow the full instruction: it *answers* it, which is how
# "请使用简体中文和标点符号转写。" ended up on screen as a caption. It still
# needs the language hint though — with no prompt at all it writes Traditional
# Chinese ("這是回生筆記的實實…"). The short prompt does exactly one job.
_SHORT_PROMPT_KEYS = frozenset({"tiny"})


def _prompt_for(lang: str | None, model_key: str | None) -> str | None:
    """The ``initial_prompt`` to use for this language and model."""
    if lang != "zh":
        return None
    if model_key in _SHORT_PROMPT_KEYS:
        return config.ASR_PROMPT_ZH_SHORT or None
    return config.ASR_PROMPT_ZH or None


def _size_of(target: models.Target) -> int:
    return target.spec.size_mb if target.spec is not None else 0


def refine_enabled() -> bool:
    """True when finals get a second, more accurate pass.

    Only useful when that pass runs a *better* model than the live caption;
    otherwise it would burn a second decode for the same text. The size check
    is not cosmetic: picking the 75 MB experiment tier as the final model used
    to let it rewrite the 145 MB live model's output, and the caption went
    from "今天天气特别差。" to "请点减减减请普通话录音" *after* being marked
    已精修. A refinement may only ever improve the text.
    """
    if not (config.REFINE_FINALS and config.LIVE_CAPTIONS and live_is_separate()):
        return False
    return _size_of(config.model_target()) > _size_of(live_target())


def refine_blocked_reason() -> str | None:
    """Why refinement is off, or ``None`` when it is on (or switched off).

    ``final_model_not_bigger`` is the one the user can act on: they picked a
    model smaller than the live caption model, so the second pass would make
    the text worse and is skipped.
    """
    if not (config.REFINE_FINALS and config.LIVE_CAPTIONS):
        return "disabled"
    if not live_is_separate():
        return "single_model"
    if _size_of(config.model_target()) <= _size_of(live_target()):
        return "final_model_not_bigger"
    return None


def _live_engine():
    """The model used for live captions: the dedicated one, else the main."""
    if _live_model is not None and _live_ready.is_set():
        return _live_model, _live_infer_lock
    return _model, _infer_lock


# --------------------------------------------------------------------------
# Peak normalisation (experimental)
#
# Off by default (``config.PEAK_NORMALIZE``). It exists to rescue distant or
# very quiet speech, where the recorder's level is far below what the model
# was trained on. It is not a free win: faster-whisper's log-mel front end
# already normalises, so the usable part of the gain may be small, while every
# gain also lifts the noise floor. Measure CER before turning it on.
# --------------------------------------------------------------------------

# Below this peak the buffer carries no speech at all, only the recorder's
# self-noise. Multiplying that by 8 would hand the decoder a loud hiss and it
# would start inventing words — the exact failure this guard prevents.
SILENCE_PEAK = 1e-4


def _peak_normalize(samples: np.ndarray, target: float, max_gain: float) -> np.ndarray:
    """Scale ``samples`` so its peak reaches ``target``, capped at ``max_gain``.

    Amplification only. Three guards, each one there because the naive version
    made something worse:

    * empty buffer → returned untouched (nothing to measure);
    * ``peak <= SILENCE_PEAK`` → returned untouched, so silence and room noise
      are never amplified into something the decoder can hallucinate from;
    * ``gain <= 1.0`` → returned untouched. Attenuating a healthy recording
      would lower its level *and* its SNR, which is the opposite of the goal.
    """
    if samples is None or samples.size == 0:
        return samples
    peak = float(np.max(np.abs(samples)))
    if peak <= SILENCE_PEAK:
        return samples
    gain = min(target / peak, max_gain)
    if gain <= 1.0:
        return samples
    return (samples * gain).astype(np.float32, copy=False)


def _prepare(audio: np.ndarray) -> np.ndarray:
    """Apply the experimental pre-processing the config asks for.

    Both ASR entry points go through here so the live caption and the final
    transcript of the same sentence are decoded from the same buffer; feeding
    one path normalised audio and the other raw audio is how the two versions
    of one sentence end up disagreeing.
    """
    if not config.PEAK_NORMALIZE:
        return audio
    return _peak_normalize(audio, config.PEAK_TARGET, config.PEAK_MAX_GAIN)


def transcribe_live(
    audio: np.ndarray,
    language: str | None = None,
    hotwords: str | None = None,
) -> str:
    """Decode one live caption. Optimised for latency, not for the archive.

    Greedy, no temperature fallback, no cross-utterance conditioning: on a
    4-core CPU a beam search costs more than the encoder for the small live
    models, and a provisional caption is replaced by the final anyway.
    """
    if audio is None or len(audio) < config.SAMPLE_RATE * 0.25:
        return ""

    lang = None if not language or language == "auto" else language
    audio = _prepare(audio)

    if config.ASR_BACKEND == "sherpa":
        text = asr_sherpa.transcribe(audio, lang, hotwords=hotwords or config.HOTWORDS)
        return "" if text is None else _clean(text, None)

    if config.ASR_REMOTE:
        remote = asr_remote.transcribe(audio, lang, live=True)
        if remote is not None:
            # No prompt was sent, so there is nothing to strip an echo of.
            return _clean(remote, None)
        if config.ASR_REMOTE_ONLY:
            # Strict mode: a failed remote call produces no caption. Falling
            # back would put the audio on the CPU the user asked us to spare.
            return ""

    if config.ASR_REMOTE_ONLY and not config.ASR_REMOTE:
        return ""  # pinned to remote, but not configured — see stt.load()

    model, lock = _live_engine()
    if model is None:
        return ""

    prompt = _prompt_for(lang, _live_key)

    with lock:
        started = time.perf_counter()
        segments, _info = model.transcribe(
            audio,
            language=lang,
            beam_size=1,
            best_of=1,
            patience=1.0,
            temperature=0.0,
            vad_filter=False,
            condition_on_previous_text=False,
            without_timestamps=True,
            initial_prompt=prompt,
            # The glossary matters most here: this is the text the user reads
            # while speaking, and it is the only pass that runs when the
            # chosen model is fast enough to also be the live one.
            hotwords=hotwords or config.HOTWORDS,
            no_speech_threshold=config.ASR_NO_SPEECH_THRESHOLD,
            log_prob_threshold=config.ASR_LOG_PROB_THRESHOLD,
            compression_ratio_threshold=config.ASR_COMPRESSION_RATIO_THRESHOLD,
        )
        parts = [(segment.text, segment.avg_logprob) for segment in segments]
        elapsed = time.perf_counter() - started

    _live_calls.append(elapsed)

    text = "".join(item[0] for item in parts).strip()
    text = _clean(text, prompt)
    if not text:
        return ""

    # A short buffer is where Whisper invents words instead of transcribing
    # them ("以后,这是" out of 1.2 s of "你好,这是"). The giveaway is confidence,
    # not length: the real readings of the same clip sat around -0.2, the
    # inventions below -1.0. Only gate short buffers — a full utterance is
    # trusted even when it is quiet.
    confidence = min(item[1] for item in parts if item[0].strip())
    if len(audio) / config.SAMPLE_RATE < LIVE_SHORT_BUFFER_S and confidence < LIVE_MIN_LOGPROB:
        log.debug("Dropping low-confidence live caption (%+.2f): %s", confidence, text)
        return ""
    return text


def live_ms() -> int | None:
    """Median wall time of the last live decodes, in milliseconds."""
    if config.ASR_REMOTE:
        # Same number, measured on the other side of the wire: what the user
        # cares about is how long a caption took, not who computed it.
        return asr_remote.median_ms()
    if not _live_calls:
        return None
    values = sorted(_live_calls)
    return int(values[len(values) // 2] * 1000)



def transcribe(
    audio: np.ndarray,
    language: str | None = None,
    fast: bool = False,
    hotwords: str | None = None,
) -> str:
    """Transcribe mono float32 audio in [-1, 1] sampled at 16 kHz.

    ``fast=True`` is used for live partial captions (lower latency); the final
    transcript uses a wider beam search for better accuracy.

    Returns an empty string when the model is unavailable. Callers should
    gate on :func:`is_ready` and surface ``model_not_ready`` themselves.
    """
    if audio is None or len(audio) < config.SAMPLE_RATE * 0.2:
        return ""

    lang = None if not language or language == "auto" else language
    audio = _prepare(audio)

    if config.ASR_BACKEND == "sherpa":
        text = asr_sherpa.transcribe(audio, lang, hotwords=hotwords or config.HOTWORDS)
        return "" if text is None else _clean(text, None)

    if config.ASR_REMOTE:
        # Deliberately outside ``_infer_lock``: no CPU work happens here, so
        # holding the local inference lock would serialise network calls for
        # no reason.
        remote = asr_remote.transcribe(audio, lang)
        if remote is not None:
            return _clean(remote, None)
        if config.ASR_REMOTE_ONLY:
            return ""  # strict mode: no local fallback

    if config.ASR_REMOTE_ONLY and not config.ASR_REMOTE:
        return ""  # pinned to remote, but not configured — see stt.load()

    model = _model
    if model is None or not _ready.is_set():
        return ""

    prompt = _prompt_for(lang, _loaded_key)

    with _infer_lock:
        started = time.perf_counter()
        segments, _info = model.transcribe(
            audio,
            language=lang,
            # Partials are thrown away as soon as the final lands, so they stay
            # greedy. The final pass pays for beam search because that is where
            # repeated / truncated Chinese sentences actually get fixed.
            beam_size=1 if fast else config.ASR_BEAM_SIZE,
            best_of=1 if fast else config.ASR_BEST_OF,
            patience=1.0 if fast else config.ASR_PATIENCE,
            temperature=0.0 if fast else config.ASR_TEMPERATURES,
            vad_filter=False,
            condition_on_previous_text=False if fast else config.ASR_CONDITION_ON_PREVIOUS,
            without_timestamps=True,
            initial_prompt=prompt,
            # Domain words bias the choice between homophones ("回声" vs
            # "回生") without giving the model a sentence it can answer, which
            # is the failure mode of putting them in the prompt.
            hotwords=hotwords or config.HOTWORDS,
            no_speech_threshold=config.ASR_NO_SPEECH_THRESHOLD,
            log_prob_threshold=config.ASR_LOG_PROB_THRESHOLD,
            compression_ratio_threshold=config.ASR_COMPRESSION_RATIO_THRESHOLD,
        )
        text = "".join(segment.text for segment in segments).strip()
        elapsed = time.perf_counter() - started

    if not fast:
        # Real-time factor of the final pass, measured on real audio. The UI
        # needs it to tell the truth when a bigger model cannot keep up on a
        # 4-core laptop, instead of the user guessing why captions lag.
        duration = len(audio) / config.SAMPLE_RATE
        if duration > 0:
            _rtf_samples.append(elapsed / duration)

    return _clean(text, prompt)


def _clean(text: str, prompt: str | None = None) -> str:
    """Drop non-speech artefacts and echoed prompt text.

    ``initial_prompt`` is an *instruction* ("以下是普通话录音…"). On very
    short or quiet audio Whisper continues that instruction instead of
    transcribing it, so the caption briefly reads like a prompt echo. That
    looked like a real bug in the first live measurement: the first caption
    of a 0.6 s buffer was the prompt itself.
    """
    text = (text or "").strip()
    # Whisper occasionally emits bracketed sound annotations; drop them.
    if text in ("[BLANK_AUDIO]", "(空白音频)", "[Silence]", "谢谢观看", "谢谢大家观看"):
        return ""
    text = _collapse_repetition(text)
    if prompt:
        text = _strip_echo(text, prompt).strip()
    if _is_hallucination(text):
        return ""
    return text


def _core_chars(text: str) -> str:
    """``text`` with punctuation and whitespace removed, for shape comparisons."""
    return re.sub(r"[\s\W_]+", "", text or "")


def same_line(a: str, b: str) -> bool:
    """True when two finals are the same line wearing different punctuation.

    The repeat guard compared raw strings, so one noise blip that decoded to
    "转写。" and then to "转写。 转写" counted as two different sentences and
    both reached the transcript. Comparing shapes catches that without
    touching the duration guard, which is what keeps genuinely repeated
    sentences apart.
    """
    ca, cb = _core_chars(a), _core_chars(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    short, long_ = sorted((ca, cb), key=len)
    return long_.replace(short, "") == ""


def _is_hallucination(text: str) -> bool:
    """True when the whole line is caption boilerplate, not speech.

    The test is deliberately all-or-nothing: strip every known boilerplate
    word and require that nothing is left. A real sentence survives because
    it contains words the list does not know — "这句话里的转写是真的" keeps
    its remaining characters. What this catches is the loop seen in the field,
    where a noise-only segment decodes to "转写。" and the next one to
    "转写。 转写" or "符号转写。", none of which an exact-match list would hit.

    Longer strings are skipped outright: a 30-character line is a sentence,
    and running the word-stripping over it would only invite false positives.
    """
    core = _core_chars(text)
    if not core or len(core) > 12:
        return False
    rest = core
    # Longest first, so "语音转写" is consumed as a unit rather than leaving
    # "语音" behind after "转写" matches.
    for word in sorted(config.ASR_HALLUCINATION_TEXTS, key=len, reverse=True):
        if word in rest:
            rest = rest.replace(word, "")
    return not rest


def _collapse_repetition(text: str) -> str:
    """Collapse a decode that got stuck repeating one unit.

    Measured, not hypothetical: `tiny` with a glossary that does not match the
    audio produced "记记记记…" for 70 characters and "劇劇劇劇…" for 80, and the
    caption grew without bound. faster-whisper's compression_ratio guard only
    fires when a retry is available, and this decode path runs a single
    temperature, so nothing else stopped it.

    A unit repeated five or more times is a loop, not speech. Five, not four,
    so that "哈哈哈哈" (four) survives as laughter while the measured 70-character
    runs do not.
    """
    # Below five characters there is no room for five repeats, so the regex
    # cannot fire anyway; skip it rather than pay for the scan.
    if len(text) < 5:
        return text
    return re.sub(r"(.{1,8}?)(?:\1){4,}", r"\1", text)


# Shortest run of the instruction that counts as an echo. "普通话录音" (5) is
# short enough to appear in real speech; "请使用简体中文" (7) is not.
ECHO_MIN_CHARS = 6


def _longest_shared_run(text: str, prompt: str, minimum: int) -> str:
    """Longest substring of ``text`` that also occurs in ``prompt``."""
    best = ""
    for start in range(len(text)):
        for end in range(len(text), start + len(best), -1):
            if end - start < minimum:
                break
            piece = text[start:end]
            if piece in prompt:
                if len(piece) > len(best):
                    best = piece
                break
    return best


def _strip_echo(text: str, prompt: str) -> str:
    """Remove the instruction the model echoed back into the transcript.

    The echo is not always a prefix. A prefix check was all this did, and it
    missed both shapes seen in the wild: ``tiny`` answered with the *tail* of
    the instruction ("请使用简体中文和标点符号转写。") and with a garbled join
    ("请点减减减请普通话录音"). Both contain a long verbatim run of the prompt,
    so the rule is "remove long shared runs", not "remove the prefix".

    Only the echoed runs are removed; whatever the user actually said stays,
    even when the echo was the longer part.
    """
    prompt = prompt.strip()
    if not text or not prompt:
        return text

    remaining = text
    while True:
        run = _longest_shared_run(remaining, prompt, ECHO_MIN_CHARS)
        if not run:
            break
        remaining = remaining.replace(run, "", 1)

    # Only the punctuation left dangling at the *front* of what remains is
    # dropped; a trailing "。" is part of the sentence the user actually said.
    return remaining.lstrip(" ，,。.;；、!！?？")
