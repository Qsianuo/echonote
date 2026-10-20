"""Entry point for the downloadable desktop build.

``python -m app`` is the developer path: it expects a terminal and prints logs.
This module is what the packaged ``EchoNote.exe`` runs. It adds the three
things a double-click user needs:

1. a readable banner (they did not ask for a console full of uvicorn lines),
2. the browser opening on its own once the model is actually ready,
3. a friendly exit when the app is already running instead of a bind error.

Everything it does is also available to developers: set ``ECHONOTE_NO_BROWSER=1``
to suppress the tab, and ``ECHONOTE_OPEN_DELAY_S`` to wait longer before it.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from . import config


def _base_url() -> str:
    host = "127.0.0.1" if config.HOST in {"0.0.0.0", "::"} else config.HOST
    return f"http://{host}:{config.PORT}"


def _port_busy() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", config.PORT)) == 0


def _wait_ready(url: str, timeout: float) -> str:
    """Block until /api/ready says ready, then return the URL to open.

    Returns early if the server reports a terminal model error, so the user
    sees the retry UI instead of staring at a blank tab.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/ready", timeout=2) as resp:
                data = json.loads(resp.read())
            if data.get("ready"):
                return url
            if data.get("auto_retry_exhausted"):
                return url
        except Exception:
            pass
        time.sleep(0.4)
    return url


def _open_browser_later(url: str) -> None:
    if os.getenv("ECHONOTE_NO_BROWSER", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    try:
        timeout = float(os.getenv("ECHONOTE_OPEN_DELAY_S") or 240)
    except ValueError:
        timeout = 240.0
    target = _wait_ready(url, max(10.0, timeout))
    try:
        webbrowser.open(target)
    except Exception:
        pass


def _fix_console_encoding() -> None:
    """Make Chinese output readable in the default Windows console.

    A frozen console app inherits the OEM code page (cp936 on zh-CN), so UTF-8
    text prints as mojibake — the very first thing a user sees. Switching the
    console to UTF-8 and reconfiguring the streams fixes both our banner and
    uvicorn's log lines.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _banner() -> None:
    target = config.model_target()
    bar = "=" * 58
    print(bar)
    print(f"  EchoNote 声记  v{config.APP_VERSION}  ({config.APP_STAGE})")
    print("  语音识别在本机完成，音频不会离开这台电脑")
    print(bar)
    print(f"  界面地址：{_base_url()}")
    if _model_on_disk():
        print(f"  模型：{target.label}（{target.key}）已就绪 · 可离线使用")
    elif config.offline_mode():
        print(f"  模型：未找到 {target.key}，当前为离线模式")
        print(f"        请把模型放入 {config.MODEL_DIR}")
    else:
        print(f"  模型：未找到 {target.key}（约 {target.spec.size_mb if target.spec else 1600} MB）")
        print("        联网时会自动下载；也可用 --mirror 走国内镜像")
    print("  关闭这个窗口即可退出 EchoNote")
    print(bar)
    sys.stdout.flush()


def _model_on_disk() -> bool:
    """True when the weights are local, so we can promise offline operation."""
    return config.model_target().path is not None


def _fetch_model(argv: list[str]) -> int:
    """``--fetch-model [key]``: the offline preparation step.

    Exists so a packaged user never needs Python installed just to obtain the
    weights; ``fetch-model.bat`` next to the exe calls this.
    """
    from . import fetch, models

    key = models.DEFAULT_KEY
    for item in argv[argv.index("--fetch-model") + 1 :]:
        if not item.startswith("-"):
            key = item.strip()
            break

    spec = models.BY_KEY.get(key)
    if spec is None:
        print(f"未知模型 {key!r}。可用：{', '.join(models.BY_KEY)}")
        return 2

    print("=" * 58)
    print(f"  EchoNote 声记  v{config.APP_VERSION}  模型下载")
    print("=" * 58)
    print(f"  目标目录：{config.MODEL_DIR}")
    print("  只需执行一次，之后断网也能用")
    print("=" * 58)
    ok = fetch.download(spec, config.MODEL_DIR, force="--force" in argv)
    print()
    print("完成。现在可以断网使用。" if ok else "下载未完成，请检查网络后重试。")
    return 0 if ok else 1


def _self_test() -> int:
    """``--self-test``: prove the native stack and bundled UI actually load.

    Used by CI right after freezing, and by users whose app fails to start.
    """
    import importlib

    problems = 0
    print(f"EchoNote {config.APP_VERSION} self-test")
    print(f"  frozen           : {config.is_frozen()}")
    print(f"  python           : {sys.version.split()[0]}")

    for name in ("ctranslate2", "onnxruntime", "faster_whisper", "av", "numpy", "tokenizers"):
        try:
            module = importlib.import_module(name)
            print(f"  {name:17}: {getattr(module, '__version__', 'ok')}")
        except Exception as exc:  # pragma: no cover - only fails on broken builds
            problems += 1
            print(f"  {name:17}: FAILED ({type(exc).__name__}: {exc})")

    index = config.WEB_DIR / "index.html"
    print(f"  web/index.html   : {'ok' if index.is_file() else 'MISSING'}")
    if not index.is_file():
        problems += 1

    print(f"  model on disk    : {'yes' if _model_on_disk() else 'no (will download on first run)'}")
    print("RESULT:", "PASS" if problems == 0 else f"FAIL ({problems} problem(s))")
    return 0 if problems == 0 else 1


def main() -> int:
    _fix_console_encoding()

    # Domestic networks often stall on huggingface.co; the mirror is opt-in so
    # we never silently redirect someone's downloads.
    if "--mirror" in sys.argv:
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    # Air-gapped machines: refuse any network attempt and say where the files
    # belong, instead of hanging on a download that cannot succeed.
    if "--offline" in sys.argv:
        os.environ["ECHONOTE_OFFLINE"] = "1"

    if "--self-test" in sys.argv:
        return _self_test()
    if "--version" in sys.argv:
        print(f"EchoNote {config.APP_VERSION} ({config.APP_STAGE})")
        return 0
    if "--fetch-model" in sys.argv:
        return _fetch_model(sys.argv)

    url = _base_url()

    # Double-clicking twice is the most common user mistake. Reuse the running
    # instance instead of crashing on "address already in use".
    if _port_busy():
        print(f"EchoNote 已在运行，正在打开 {url}")
        _wait_ready(url, 10)
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return 0

    _banner()
    threading.Thread(target=_open_browser_later, args=(url,), daemon=True).start()

    try:
        from .main import run

        run()
    except KeyboardInterrupt:
        print("\n已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
