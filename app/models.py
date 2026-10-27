"""Model catalog and offline-first resolution.

The product promise is "recognise on this machine with no network". That is
only true if the app *never* depends on a live download at startup, so the
resolution order here is deliberately local-first:

1. an explicit path or catalog key from ``ECHONOTE_MODEL``, if it exists;
2. otherwise the best model already present under ``models/``;
3. otherwise the default catalog entry, which will need a one-time download.

Nothing in this module touches the network. Downloads live in
``tools/fetch_model.py`` so that an offline machine has no code path that
tries to reach Hugging Face.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """One selectable model.

    ``dirname`` is the folder name used under ``models/``. It is also what the
    UI shows, so it must stay human readable.
    """

    key: str
    repo: str
    dirname: str
    label: str
    size_mb: int
    tier: str
    note: str
    # 必须落盘的文件。留空表示按 whisper 的约定（``REQUIRED_FILES``）；
    # 翻译模型要额外的 SentencePiece 分词器，所以自己列一份。
    required: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        return f"{self.label}（{self.key}）"

    @property
    def required_files(self) -> tuple[str, ...]:
        return self.required or REQUIRED_FILES


# Ordered best-quality first: ``resolve()`` walks this order to pick whatever
# the machine already has, so a user who drops in a bigger model gets it
# automatically without changing any setting.
CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec(
        key="large-v3",
        repo="Systran/faster-whisper-large-v3",
        dirname="faster-whisper-large-v3",
        label="最高精度",
        size_mb=3090,
        tier="quality",
        note="中文识别最准，4 核 CPU 上明显慢于实时，只建议短句转写",
    ),
    ModelSpec(
        key="large-v3-turbo",
        repo="deepdml/faster-whisper-large-v3-turbo-ct2",
        dirname="faster-whisper-large-v3-turbo",
        label="高精度",
        size_mb=1620,
        tier="quality",
        note="中文最准的实用档，但 4 核 CPU 上单句定稿约 12 秒，只建议短录音",
    ),
    ModelSpec(
        key="medium",
        repo="Systran/faster-whisper-medium",
        dirname="faster-whisper-medium",
        label="均衡",
        size_mb=1530,
        tier="balanced",
        note="默认档：本机样本 CER 0.0%（与 turbo 并列），单句精修约 12 s",
    ),
    ModelSpec(
        key="small",
        repo="Systran/faster-whisper-small",
        dirname="faster-whisper-small",
        label="快速",
        size_mb=464,
        tier="speed",
        note="单句定稿约 4 秒、CER 3.4%：要精修跟得上说话时选它，牺牲一个字换速度",
    ),
    ModelSpec(
        key="base",
        repo="Systran/faster-whisper-base",
        dirname="faster-whisper-base",
        label="极速",
        size_mb=145,
        tier="speed",
        note="「边说边出字」用的低延迟档：本机单次解码约 0.9 s，准确率低于 small",
    ),
    ModelSpec(
        key="tiny",
        repo="Systran/faster-whisper-tiny",
        dirname="faster-whisper-tiny",
        label="试验",
        size_mb=75,
        tier="speed",
        note="本机样本 CER 27.6%；填了专有名词词表后降到 13.8%（仍低于 base），会编词",
    ),
)

# The model that produces the *final* transcript. It is no longer the same
# model that draws the live caption: v3 pairs it with the fastest installed
# model (see ``stt.live_target``) so a 4-core machine can show text within a
# second without giving up accuracy on the archive.
#
# v0.11.1 puts accuracy first. Measured with the exact final-pass settings the
# app uses (beam 5, condition_on_previous_text, Chinese prompt), 8.95 s sample:
#   small            3.66 s   CER  3.4%   ("时时语音识别")
#   medium          12.08 s   CER  0.0%
#   large-v3-turbo  15.65 s   CER  0.0%
# ``small`` is only 3.4% off, but that single error is exactly the kind a
# note-taker cannot recover from context. ``medium`` and ``turbo`` both read
# the sample perfectly; ``medium`` is 23% faster (12 s vs 16 s per sentence on
# four cores) and 90 MB smaller, so it is the default and ``turbo`` stays one
# dropdown click away for anyone who wants the newer distill.
#
# Note the cost, because it is real: at 12 s per sentence the accurate pass
# runs at ~2.4x realtime, so a long monologue queues. That is the price of the
# last 3.4% and it is now bounded by REFINE_BACKLOG_SECONDS instead of silently
# dropping work.
DEFAULT_KEY = "medium"

# Preference order for the model that draws the live caption (``stt.live_target``).
# Fastest first: the caption is provisional and gets rewritten, so a wrong word
# costs less than a late one.
LIVE_PREFERENCE = ("base", "small", "tiny")

BY_KEY: dict[str, ModelSpec] = {spec.key: spec for spec in CATALOG}


# The SenseVoice backend lives outside the whisper CATALOG on purpose — it is
# a backend value of ``ASR_BACKEND`` (like the zipformer), not a dropdown
# entry. Its folder name, repo and file list still belong here so that
# ``config``, the fetcher and ``asr_sensevoice`` quote one set of strings
# instead of three copies that can drift apart.
SENSEVOICE_DIRNAME = "sherpa-sense-voice-small"
SENSEVOICE_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
SENSEVOICE_FILES = ("model.int8.onnx", "tokens.txt")


# --------------------------------------------------------------------------
# Offline translation (Opus-MT 77M, ctranslate2 int8)
# --------------------------------------------------------------------------
#
# 和 SenseVoice 一样不进 whisper CATALOG：它不是语音模型，而是「定稿之后」
# 的一层，界面上也不会出现在「模型」下拉里。但目录名、仓库与文件清单必须
# 只写一份——``translate`` 拿它找模型，``fetch`` 拿它下载，两处引用同一份
# 才不会出现「能下载但读不到」的错位。
#
# 每个方向约 80 MB（int8）。实测定稿后单句 50~100 ms。
MT_REQUIRED_FILES = ("model.bin", "config.json", "source.spm", "target.spm")

MT_CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec(
        key="mt-zh-en",
        repo="jiangzhuo9357/opus-mt-zh-en-ct2",
        dirname="mt-zh-en",
        label="中译英",
        size_mb=80,
        tier="translate",
        note="离线翻译：中文原文 → 英文译文，约 80 MB",
        required=MT_REQUIRED_FILES,
    ),
    ModelSpec(
        key="mt-en-zh",
        repo="jiangzhuo9357/opus-mt-en-zh-ct2",
        dirname="mt-en-zh",
        label="英译中",
        size_mb=80,
        tier="translate",
        note="离线翻译：英文原文 → 中文译文，约 80 MB",
        required=MT_REQUIRED_FILES,
    ),
)

MT_BY_KEY: dict[str, ModelSpec] = {spec.key: spec for spec in MT_CATALOG}

# (原文语言, 译文语言) -> spec。界面的「译文语言」下拉按此顺序呈现，
# 所以这里是产品顺序，不是字典序。
MT_PAIRS: dict[tuple[str, str], ModelSpec] = {
    ("zh", "en"): MT_BY_KEY["mt-zh-en"],
    ("en", "zh"): MT_BY_KEY["mt-en-zh"],
}


def spec_by_key(key: str) -> ModelSpec | None:
    """按 key 找 spec，语音与翻译两条目录都查。

    ``--fetch-model`` 是用户唯一会手打 key 的地方，查找逻辑收在这里，
    免得 CLI 与 exe 各写一遍、又各自漏掉翻译档。
    """
    return BY_KEY.get(key) or MT_BY_KEY.get(key)


def is_spec_complete(spec: ModelSpec, model_dir: Path | str) -> bool:
    """按 spec 自己的文件清单判断模型是否齐备。

    ``is_complete`` 只认 whisper 的两个文件；翻译模型少了 ``source.spm``
    会「下载成功但一调用就崩」，所以校验必须跟着 spec 走。
    """
    root = Path(model_dir) / spec.dirname
    if not root.is_dir():
        return False
    for name in spec.required_files:
        if not (root / name).is_file():
            return False
    try:
        return (root / "model.bin").stat().st_size >= _MIN_WEIGHT_BYTES
    except OSError:
        return False


def default_pair() -> tuple[str, ...]:
    """The two keys a fresh install needs: one for captions, one for finals.

    ``fetch_model.py`` downloads exactly this set by default, so a user who
    runs one command gets the architecture the latency numbers describe.
    """
    live = next((key for key in LIVE_PREFERENCE if key in BY_KEY), DEFAULT_KEY)
    return tuple(dict.fromkeys((live, DEFAULT_KEY)))
# Directory name -> spec, so a hand-placed ``models/faster-whisper-medium``
# resolves even when the user never sets ECHONOTE_MODEL.
BY_DIRNAME: dict[str, ModelSpec] = {spec.dirname: spec for spec in CATALOG}

# A converted CT2 model always ships these two files. Checking for the weights
# alone is not enough: an interrupted download leaves a truncated model.bin.
REQUIRED_FILES = ("model.bin", "config.json")
_MIN_WEIGHT_BYTES = 1 << 20  # 1 MiB: anything smaller is not a real model


def is_complete(path: Path | str | None) -> bool:
    """True when ``path`` holds a usable converted model."""
    if not path:
        return False
    root = Path(path)
    if not root.is_dir():
        return False
    for name in REQUIRED_FILES:
        if not (root / name).is_file():
            return False
    try:
        return (root / "model.bin").stat().st_size >= _MIN_WEIGHT_BYTES
    except OSError:
        return False


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """What the loader should hand to ``WhisperModel``.

    ``path`` is set when a complete local model was found; in that case the
    loader passes the path and faster-whisper performs no hub lookup at all.
    ``key`` is kept for reporting even when the model came from a bare path.
    """

    key: str
    path: str | None
    spec: ModelSpec | None
    source: str  # "env-path" | "env-key" | "local" | "fallback"

    @property
    def ready_offline(self) -> bool:
        return self.path is not None

    @property
    def label(self) -> str:
        if self.spec is not None:
            return self.spec.label
        return Path(self.path or self.key).name

    @property
    def loader_value(self) -> str:
        """Argument for ``WhisperModel``: local dir when available."""
        return self.path or (self.spec.repo if self.spec else self.key)

    def describe(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "path": self.path,
            "source": self.source,
            "tier": self.spec.tier if self.spec else "custom",
            "size_mb": self.spec.size_mb if self.spec else None,
            "offline_ready": self.ready_offline,
        }


def local_dir(model_dir: Path | str, spec: ModelSpec) -> Path:
    return Path(model_dir) / spec.dirname


def available(model_dir: Path | str) -> list[ModelSpec]:
    """Catalog entries already complete on this machine, best first."""
    return [spec for spec in CATALOG if is_complete(local_dir(model_dir, spec))]


# Tiers that can keep up with speech on a 4-core laptop. Measured on an
# i5-8300H: small RTF 0.48, large-v3-turbo RTF 1.52. They only matter as a
# tie-breaker when the declared default is not installed.
REALTIME_TIERS = frozenset({"speed", "balanced"})


def default_local(model_dir: Path | str) -> ModelSpec | None:
    """Which installed model to load when the user has not chosen one.

    Order of preference:

    1. the declared default (``DEFAULT_KEY``) if it is installed — an explicit
       product decision beats a heuristic;
    2. otherwise the best installed model that can still run in real time;
    3. otherwise whatever is installed (someone copied 3 GB in by hand).
    """
    found = available(model_dir)
    if not found:
        return None
    for spec in found:
        if spec.key == DEFAULT_KEY:
            return spec
    for spec in found:
        if spec.tier in REALTIME_TIERS:
            return spec
    return found[0]


def resolve(
    model_dir: Path | str,
    explicit: str | None = None,
    *,
    offline: bool = False,
) -> Target:
    """Decide which model to load.

    ``explicit`` may be a catalog key, a directory path, or any name
    faster-whisper understands (``small.en``, a HF repo id). The last case is
    only allowed when ``offline`` is false, because it implies a download.
    """
    value = (explicit or "").strip()

    if value:
        candidate = Path(value)
        if candidate.is_dir():
            return Target(
                key=candidate.name,
                path=str(candidate) if is_complete(candidate) else None,
                spec=BY_DIRNAME.get(candidate.name),
                source="env-path",
            )
        if value in BY_KEY:
            spec = BY_KEY[value]
            path = local_dir(model_dir, spec)
            return Target(
                key=spec.key,
                path=str(path) if is_complete(path) else None,
                spec=spec,
                source="env-key",
            )
        # Not a key and not a directory: treat as a faster-whisper name/repo.
        return Target(key=value, path=None, spec=None, source="env-key")

    found = default_local(model_dir)
    if found is not None:
        return Target(
            key=found.key,
            path=str(local_dir(model_dir, found)),
            spec=found,
            source="local",
        )

    spec = BY_KEY[DEFAULT_KEY]
    return Target(key=spec.key, path=None, spec=spec, source="fallback")


def missing_hint(model_dir: Path | str, target: Target) -> str:
    """Actionable one-paragraph message for the UI (FR-020 style)."""
    root = Path(model_dir)
    spec = target.spec
    if spec is not None:
        where = local_dir(root, spec)
        return (
            f"未找到模型 {spec.key}（{spec.size_mb} MB）。"
            f"请把模型目录放到 {where}，或联网运行一次 "
            f"python tools/fetch_model.py --model {spec.key}"
        )
    return (
        f"未找到模型 {target.key}。请把它放到 {root} 下，"
        "或用 python tools/fetch_model.py 下载后再离线运行"
    )


def offline_env() -> dict[str, str]:
    """Environment that makes the Hub client refuse network access.

    Set before ``faster_whisper`` is imported so that even an accidental
    ``repo_id`` argument cannot trigger a download.
    """
    return {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_XET": "1",
    }


def apply_offline_env() -> None:
    for key, value in offline_env().items():
        os.environ.setdefault(key, value)


def apply_online_env() -> None:
    """Undo :func:`apply_offline_env` before an authorised one-time download.

    ``setdefault`` means a flag left over from an earlier load survives, and
    the hub client then refuses the very download the caller just decided to
    allow — ``OfflineModeIsEnabled`` on a machine that is plainly online.
    Popping is the only way back in the same process.
    """
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        os.environ.pop(key, None)
