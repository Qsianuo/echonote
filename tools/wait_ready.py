"""Wait until ``/api/ready`` reports the model is usable, then open a browser.

Opening the page before the model is ready is the v1.0 behaviour that made
first runs look broken, so the launcher calls this in parallel with uvicorn
(PRD FR-043 AC1).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import webbrowser

LOCAL_BINDS = {"0.0.0.0", "::", ""}


def read_ready(url: str, timeout: float = 2.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait for EchoNote readiness")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--open", action="store_true", help="open the browser once ready")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/api/ready"
    page_host = "127.0.0.1" if args.host in LOCAL_BINDS else args.host
    page = f"http://{page_host}:{args.port}"

    started = time.monotonic()
    deadline = started + args.timeout
    last_note = 0.0

    while time.monotonic() < deadline:
        try:
            payload = read_ready(url)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            payload = None

        if payload and payload.get("ready"):
            if not args.quiet:
                print(f"[EchoNote] 模型已就绪（{time.monotonic() - started:.0f} s）")
            if args.open:
                webbrowser.open(page)
            return 0

        now = time.monotonic()
        if not args.quiet and now - last_note >= 5:
            last_note = now
            detail = ""
            if payload and payload.get("error"):
                detail = f"（{payload['error']}）"
            print(f"[EchoNote] 正在加载模型…已用 {now - started:.0f} s{detail}")

        time.sleep(0.5)

    print(
        f"[EchoNote] 等待模型就绪超时（{args.timeout:.0f} s）。"
        "请查看服务端日志，或访问 /api/ready 确认状态。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
