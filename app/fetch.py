"""The one place in the app that downloads a model.

Kept in its own module so that ``app/models.py`` stays network-free and so the
packaged executable and ``tools/fetch_model.py`` share exactly one
implementation (a difference between them would only show up on a user's
machine, which is the worst place to find it).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from . import models

# huggingface.co is unreachable on some networks while the mirror works, and
# the mirror cannot proxy the Xet CDN. Both facts are baked into the defaults
# because users hit them on first run, before they can read any docs.
DEFAULT_ENDPOINT = "https://hf-mirror.com"


def prepare_env(endpoint: str | None = None) -> str:
    """Configure the Hub client for a download. Returns the endpoint used.

    Deliberately does **not** set ``HF_HUB_OFFLINE``: this is the download
    path, it is supposed to reach the network.
    """
    chosen = (endpoint or os.getenv("HF_ENDPOINT") or DEFAULT_ENDPOINT).strip()
    os.environ["HF_ENDPOINT"] = chosen
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.pop("HF_HUB_OFFLINE", None)
    return chosen


def human(mb: float) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def download_sensevoice(
    model_dir: Path | str,
    *,
    endpoint: str | None = None,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> bool:
    """Fetch the SenseVoice int8 onnx next to the whisper models.

    Separate from :func:`download` because the completeness rule differs: a
    usable SenseVoice folder is ``model.int8.onnx`` + ``tokens.txt``, not the
    CT2 pair, and only the int8 weights are wanted — the fp32 ``model.onnx``
    in the same repo triples the download for no accuracy gain on CPU.

    The completeness check reads ``model_dir`` directly instead of
    ``config.SENSEVOICE_MODEL_DIR`` so that a fetch with a custom
    ``--model-dir`` reports about the folder it actually wrote.
    """
    target = Path(model_dir) / models.SENSEVOICE_DIRNAME
    complete = all((target / name).is_file() for name in models.SENSEVOICE_FILES)
    if complete and not force:
        log(f"  already present: {target} ({human(folder_size(target) / 2**20)})")
        return True

    print_endpoint = prepare_env(endpoint)
    log(f"  endpoint : {print_endpoint}")
    log(f"  repo     : {models.SENSEVOICE_REPO}")
    log(f"  target   : {target}")
    log("  size     : ~230 MB")

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        log("  ERROR: huggingface_hub is missing (pip install -r requirements.txt)")
        return False

    started = time.time()
    try:
        snapshot_download(
            repo_id=models.SENSEVOICE_REPO,
            local_dir=str(target),
            allow_patterns=list(models.SENSEVOICE_FILES),
            max_workers=2,
        )
    except Exception as exc:  # noqa: BLE001 - shown to the user verbatim
        log(f"  ERROR: {type(exc).__name__}: {exc}")
        log("  Fix: check the network, or copy a model folder in by hand:")
        log(f"       {target}")
        return False

    if not all((target / name).is_file() for name in models.SENSEVOICE_FILES):
        log("  ERROR: download finished but the folder is incomplete")
        for name in models.SENSEVOICE_FILES:
            ok = (target / name).is_file()
            log(f"         {name}: {'ok' if ok else 'MISSING'}")
        return False

    log(f"  done in {time.time() - started:.0f}s, {human(folder_size(target) / 2**20)} on disk")
    return True


def download(
    spec: models.ModelSpec,
    model_dir: Path | str,
    *,
    endpoint: str | None = None,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> bool:
    """Fetch ``spec`` into ``model_dir``. Idempotent unless ``force``."""
    target = models.local_dir(model_dir, spec)
    if models.is_complete(target) and not force:
        log(f"  already present: {target} ({human(folder_size(target) / 2**20)})")
        return True

    print_endpoint = prepare_env(endpoint)
    log(f"  endpoint : {print_endpoint}")
    log(f"  repo     : {spec.repo}")
    log(f"  target   : {target}")
    log(f"  size     : ~{human(spec.size_mb)}")

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        log("  ERROR: huggingface_hub is missing (pip install -r requirements.txt)")
        return False

    started = time.time()
    try:
        snapshot_download(
            repo_id=spec.repo,
            local_dir=str(target),
            allow_patterns=["*.bin", "*.json", "*.txt"],
            max_workers=2,
        )
    except Exception as exc:  # noqa: BLE001 - shown to the user verbatim
        log(f"  ERROR: {type(exc).__name__}: {exc}")
        log("  Fix: check the network, or copy a model folder in by hand:")
        log(f"       {target}")
        return False

    if not models.is_complete(target):
        log("  ERROR: download finished but the folder is incomplete")
        for name in models.REQUIRED_FILES:
            ok = (target / name).is_file()
            log(f"         {name}: {'ok' if ok else 'MISSING'}")
        return False

    log(f"  done in {time.time() - started:.0f}s, {human(folder_size(target) / 2**20)} on disk")
    return True
