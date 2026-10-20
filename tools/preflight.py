"""Fail fast when the model is neither cached nor downloadable.

Run before the server starts so a cold, offline machine gets a readable
reason in seconds instead of a hung "loading model" screen (PRD FR-043 AC3).
Exit codes: 0 = can start, 3 = cannot start (caller should stop).
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


def model_cached(model: str, model_dir: Path) -> bool:
    """True when ``model`` is already on disk (local dir or HF cache)."""
    direct = Path(model)
    if direct.is_dir() and (direct / "model.bin").is_file():
        return True
    if direct.is_file():
        return True
    if model.startswith(("/", "./", ".\\")) or ":" in model[:3]:
        return False

    candidates = [
        model_dir / f"faster-whisper-{model}",
        model_dir / f"models--Systran--faster-whisper-{model}",
        model_dir / f"models--guillaumekln--faster-whisper-{model}",
    ]
    for candidate in candidates:
        if (candidate / "model.bin").is_file():
            return True
        if (candidate / "snapshots").is_dir():
            return True
    return False


def endpoint_host() -> str:
    """Honour HF_ENDPOINT so mirror users are not told they are offline."""
    endpoint = os.getenv("HF_ENDPOINT", "https://huggingface.co").strip()
    parsed = urlparse(endpoint if "//" in endpoint else f"//{endpoint}")
    return parsed.hostname or "huggingface.co"


def reachable(host: str, port: int = 443, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="EchoNote preflight checks")
    parser.add_argument("--model", default=os.getenv("ECHONOTE_MODEL") or "small")
    parser.add_argument(
        "--model-dir",
        default=os.getenv("ECHONOTE_MODEL_DIR", str(ROOT / "models")),
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    model_dir = Path(args.model_dir)

    if model_cached(args.model, model_dir):
        print(f"[EchoNote] 模型已缓存：{args.model}")
        return 0

    host = endpoint_host()
    print(f"[EchoNote] 模型未缓存，首次运行需要联网下载（约 500 MB，来源 {host}）…")

    if reachable(host, 443, args.timeout):
        return 0

    print(
        "[EchoNote] 无法连接模型下载站点，且本机没有缓存模型。\n"
        f"  · 需要联网下载约 500 MB，或提前把模型放到 {model_dir}\n"
        "  · 国内网络可设置镜像：set HF_ENDPOINT=https://hf-mirror.com\n"
        "  · 也可以直接指向本地模型目录：set ECHONOTE_MODEL=D:\\models\\faster-whisper-small",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
