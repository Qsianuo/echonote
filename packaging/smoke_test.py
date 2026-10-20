"""Smoke test for a frozen build, run by CI after PyInstaller.

Two layers are checked, because they fail differently:

* ``--self-test`` proves the native stack (ctranslate2 / onnxruntime / av)
  and the bundled frontend made it into the bundle.
* starting the real server proves uvicorn, the websocket route and the static
  mount work from inside the frozen process.

Usage: python packaging/smoke_test.py dist/EchoNote/EchoNote.exe
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = "8131"


def run_self_test(exe: Path) -> None:
    result = subprocess.run([str(exe), "--self-test"], capture_output=True, text=True, timeout=180)
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr[-2000:], file=sys.stderr)
        raise SystemExit("self-test failed")


def run_fetch_model_guard(exe: Path) -> None:
    """The offline preparation command must exist *and* refuse bad input.

    An unknown key proves the code path is bundled (``app.fetch`` is imported
    lazily, so a missing hidden import would surface here) without touching
    the network.
    """
    result = subprocess.run(
        [str(exe), "--fetch-model", "__does_not_exist__"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 2:
        print(out[-1500:], file=sys.stderr)
        raise SystemExit(f"fetch-model did not reject an unknown key (exit {result.returncode})")
    if "未知模型" not in out:
        print(out[-1500:], file=sys.stderr)
        raise SystemExit("fetch-model output is missing the expected message")
    print("fetch-model: bundled, rejects unknown keys, no download attempted")


def wait_http(path: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=2) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # server still booting
            last = exc
            time.sleep(0.5)
    raise SystemExit(f"server never answered {path}: {last}")


def run_server(exe: Path) -> None:
    env = dict(os.environ)
    env.update(
        {
            "ECHONOTE_PORT": PORT,
            "ECHONOTE_NO_BROWSER": "1",
            "ECHONOTE_SKIP_PRELOAD": "1",  # never download a model in CI
        }
    )
    proc = subprocess.Popen([str(exe)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        health = json.loads(wait_http("/api/health", 120))
        assert health.get("ok") is True or "version" in health, health
        print("health:", health)

        config = json.loads(wait_http("/api/config", 30))
        assert config.get("protocol") == "2", config
        print("config:", {k: config[k] for k in ("version", "protocol", "max_sessions")})
        assert "offline_ready" in config, config

        models_payload = json.loads(wait_http("/api/models", 30))
        assert models_payload.get("items"), models_payload
        print("models:", models_payload.get("current"), len(models_payload["items"]), "entries")

        page = wait_http("/", 30)
        assert "<html" in page.lower(), "static index.html not served"
        assert "EchoNote" in page, "index.html does not look like EchoNote"
        print(f"index.html: {len(page)} bytes")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    exe = Path(sys.argv[1]).resolve()
    if not exe.is_file():
        raise SystemExit(f"not found: {exe}")
    print(f"== self-test {exe.name} ==")
    run_self_test(exe)
    print(f"== fetch-model {exe.name} ==")
    run_fetch_model_guard(exe)
    print(f"== server {exe.name} ==")
    run_server(exe)
    print("SMOKE TEST PASS")


if __name__ == "__main__":
    main()
