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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import models  # noqa: E402


def model_cached(model: str, model_dir: Path) -> bool:
    """True when the resolved model is already on disk.

    Delegates to :mod:`app.models` so preflight and the server can never
    disagree about what "cached" means.
    """
    return models.resolve(model_dir, model).path is not None


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
    parser.add_argument("--model", default=os.getenv("ECHONOTE_MODEL") or models.DEFAULT_KEY)
    parser.add_argument(
        "--model-dir",
        default=os.getenv("ECHONOTE_MODEL_DIR", str(ROOT / "models")),
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    model_dir = Path(args.model_dir)

    if model_cached(args.model, model_dir):
        target = models.resolve(model_dir, args.model)
        print(f"[EchoNote] 模型已在本机：{target.key}（{target.label}）→ {target.path}")
        return 0

    host = endpoint_host()
    spec = models.resolve(model_dir, args.model).spec
    size = f"约 {spec.size_mb} MB" if spec else "数百 MB"
    print(f"[EchoNote] 模型未就绪，首次使用需要联网下载（{size}，来源 {host}）…")

    if reachable(host, 443, args.timeout):
        return 0

    print(
        "[EchoNote] 无法连接模型下载站点，且本机没有可用模型。\n"
        f"  · 联网下载一次：python tools/fetch_model.py --model {args.model}\n"
        f"  · 或把模型目录放到 {model_dir}\n"
        "  · 国内网络默认已走 https://hf-mirror.com，可用 HF_ENDPOINT 覆盖\n"
        "  · 也可以直接指向本地模型目录：set ECHONOTE_MODEL=D:\\models\\faster-whisper-large-v3-turbo",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
