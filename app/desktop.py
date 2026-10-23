"""Entry point for the downloadable desktop build.

``python -m app`` is the developer path: it expects a terminal and prints logs.
This module is what the packaged ``EchoNote.exe`` runs, and it carries the
things that separate "a web page someone opened" from "an application":

1. an application *window* — the bundled Edge/Chrome is launched with
   ``--app=``, so there is no address bar, no tab strip and no bookmark bar,
   and the taskbar shows EchoNote rather than a browser;
2. no console window — the build is windowed, so the first thing a user sees
   is the interface, not a wall of uvicorn lines (logs go to ``logs/``);
3. a single instance — launching again focuses the window that is already
   open instead of stacking up a second one;
4. a readable banner and a real error dialog for the cases where something
   fails before the UI can report it.

Everything here is also available to developers: set ``ECHONOTE_NO_BROWSER=1``
to suppress the window, ``ECHONOTE_NO_APP_MODE=1`` to fall back to a normal
browser tab, and ``ECHONOTE_OPEN_DELAY_S`` to wait longer before opening it.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import config

log = logging.getLogger("echonote.desktop")

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008
_SW_RESTORE = 9

# Edge ships with Windows 10/11, so it is the first choice: `--app` gives a
# chrome-less window backed by the profile the user already trusts, which means
# the microphone permission granted once keeps working.
_EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
_CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
)


def _base_url() -> str:
    host = "127.0.0.1" if config.HOST in {"0.0.0.0", "::"} else config.HOST
    return f"http://{host}:{config.PORT}"


def _port_busy() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", config.PORT)) == 0


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------
# Console handling
#
# The build stays a console binary: a windowed one has sys.stdout == None,
# which would break `--self-test` capture in CI and the progress that
# fetch-model.bat prints. What a double-click user should not see is the
# *window*, so it is hidden at startup — but only when this process owns the
# console. Launched from a shell there are two processes attached (the shell
# and us), and there the window is the point.
# --------------------------------------------------------------------------

_console_hidden = False


def _hide_own_console() -> bool:
    """Hide the console window a double-click creates, keeping stdout alive."""
    global _console_hidden
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return False
        # Two or more processes on this console means a shell spawned us.
        buffer = (ctypes.c_uint * 4)()
        count = kernel32.GetConsoleProcessList(buffer, 4)
        if count > 1:
            return False
        user32.ShowWindow(hwnd, 0)  # SW_HIDE
        _console_hidden = True
        return True
    except Exception:
        return False


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


def _log_setup() -> Path | None:
    """Send logs to a file, because a windowed build has nowhere else for them.

    Called before ``app.main`` is imported so ``basicConfig`` there sees a
    handler already installed and does not add a dead stream handler.
    """
    try:
        logs = Path(config.ROOT_DIR) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / "echonote.log"
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        return path
    except OSError:
        return None


def _fatal(message: str) -> None:
    """Report a startup failure where the user can actually see it.

    With the console hidden, a bare ``print`` is invisible — which is exactly
    the "I double-clicked and nothing happened" complaint. A message box is the
    only surface left.
    """
    log.error(message)
    print(message)
    if sys.platform != "win32" or not (_console_hidden or sys.stdout is None):
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, message, f"EchoNote {config.APP_VERSION}", 0x10  # MB_ICONERROR
        )
    except Exception:
        pass


# --------------------------------------------------------------------------
# Window handling
# --------------------------------------------------------------------------


def _registry_browser(exe_name: str) -> str | None:
    """Resolve a browser through App Paths, for non-standard installs."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, key) as handle:
                    value = winreg.QueryValue(handle, None)
                if value and Path(value).is_file():
                    return value
            except OSError:
                continue
    except Exception:
        pass
    return None


def _app_mode_browser() -> str | None:
    """Path to a Chromium browser that supports ``--app=``, if any."""
    if sys.platform != "win32":
        return None
    for candidate in _EDGE_PATHS + _CHROME_PATHS:
        if Path(candidate).is_file():
            return candidate
    return _registry_browser("msedge.exe") or _registry_browser("chrome.exe")


def _focus_existing(title_hint: str = "EchoNote") -> bool:
    """Bring an already-running EchoNote window to the front.

    A second double-click used to open another tab, which reads as "the app
    forgot it was running". Enumerating top-level windows is the only way to
    reach a window this process does not own.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found: list[int] = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _param):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if title_hint in buffer.value and user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
                    return False  # stop at the first match
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        if not found:
            return False
        hwnd = found[0]
        user32.ShowWindow(hwnd, _SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _open_window(url: str) -> None:
    """Open the UI as an application window, falling back to a browser tab."""
    if _flag("ECHONOTE_NO_APP_MODE"):
        webbrowser.open(url)
        return

    browser = _app_mode_browser()
    if browser:
        try:
            subprocess.Popen(
                [browser, f"--app={url}", "--window-size=1200,900"],
                creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
            log.info("Opened application window via %s", browser)
            return
        except OSError as exc:
            log.warning("Application window failed (%s), falling back to a tab", exc)

    try:
        webbrowser.open(url)
        log.info("Opened %s in the default browser", url)
    except Exception:
        pass


def _wait_server(url: str, timeout: float) -> str:
    """Block until the HTTP server answers, then return the URL to open.

    Deliberately *not* waiting for the model: the page renders its own
    "正在准备模型…" state, and staring at nothing through the two minutes a
    first run spends downloading weights is the worst possible introduction.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return url
        except Exception:
            pass
        time.sleep(0.15)
    return url


def _open_window_later(url: str) -> None:
    if _flag("ECHONOTE_NO_BROWSER"):
        return
    try:
        timeout = float(os.getenv("ECHONOTE_OPEN_DELAY_S") or 60)
    except ValueError:
        timeout = 60.0
    target = _wait_server(url, max(5.0, timeout))
    _open_window(target)


def _watch_for_last_window(grace_s: float = 6.0) -> None:
    """Exit once the last UI window has been gone for a few seconds.

    Hiding the console took away the only obvious way to quit, so closing the
    window has to mean something. A grace period is not politeness: a reload
    drops the WebSocket and reconnects about a second later, and exiting on
    that would make every refresh look like a crash.
    """
    from . import main as app_main

    if _flag("ECHONOTE_NO_IDLE_EXIT"):
        return

    deadline_guard = time.monotonic() + 60.0  # never exit before the UI opened once
    empty_since: float | None = None
    while True:
        time.sleep(1.0)
        try:
            count = app_main.active_sessions()
        except Exception:
            return
        now = time.monotonic()
        if count > 0:
            empty_since = None
            deadline_guard = now + 60.0
            continue
        if now < deadline_guard:
            continue
        if empty_since is None:
            empty_since = now
        elif now - empty_since >= grace_s:
            log.info("Last window closed; exiting")
            os._exit(0)


# --------------------------------------------------------------------------
# CLI surfaces
# --------------------------------------------------------------------------


def _banner() -> None:
    target = config.model_target()
    lines = ["=" * 58]
    lines.append(f"  EchoNote 声记  v{config.APP_VERSION}  ({config.APP_STAGE})")
    lines.append("  语音识别在本机完成，音频不会离开这台电脑")
    lines.append("=" * 58)
    lines.append(f"  界面地址：{_base_url()}")
    if _model_on_disk():
        lines.append(f"  模型：{target.label}（{target.key}）已就绪 · 可离线使用")
    elif config.offline_mode():
        lines.append(f"  模型：未找到 {target.key}，当前为离线模式")
        lines.append(f"        请把模型放入 {config.MODEL_DIR}")
    else:
        size = target.spec.size_mb if target.spec else 1600
        lines.append(f"  模型：未找到 {target.key}（约 {size} MB）")
        lines.append("        联网时会自动下载；也可用 --mirror 走国内镜像")
    lines.append("  关闭窗口即可退出 EchoNote")
    lines.append("=" * 58)
    for line in lines:
        print(line)
        log.info(line.strip())
    if sys.stdout is not None:
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

    keys = list(models.default_pair())
    for item in argv[argv.index("--fetch-model") + 1 :]:
        if not item.startswith("-"):
            keys = [item.strip()]
            break

    specs = []
    for key in keys:
        spec = models.BY_KEY.get(key)
        if spec is None:
            _fatal(f"未知模型 {key!r}。可用：{', '.join(models.BY_KEY)}")
            return 2
        specs.append(spec)

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

    browser = _app_mode_browser()
    print(f"  app-mode window  : {browser or 'not found (will use a browser tab)'}")

    print(f"  model on disk    : {'yes' if _model_on_disk() else 'no (will download on first run)'}")
    print("RESULT:", "PASS" if problems == 0 else f"FAIL ({problems} problem(s))")
    return 0 if problems == 0 else 1


def main() -> int:
    _fix_console_encoding()
    # Hide the console window before anything slow happens, so a double-click
    # goes straight to the interface. Skipped when a shell launched us (there
    # the window is where --fetch-model progress belongs) and in dev runs.
    if config.is_frozen():
        _hide_own_console()

    log_path = _log_setup()

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

    # Double-clicking twice is the most common user mistake. Focus the window
    # that is already open rather than stacking a second one on top of it.
    if _port_busy():
        log.info("Already running; focusing the existing window")
        if not _focus_existing():
            _open_window(url)
        return 0

    _banner()
    if log_path is None:
        log.warning("Log directory is not writable; startup messages go nowhere")

    threading.Thread(target=_open_window_later, args=(url,), daemon=True).start()
    if config.is_frozen():
        threading.Thread(target=_watch_for_last_window, daemon=True).start()

    try:
        from .main import run

        run()
    except KeyboardInterrupt:
        print("\n已退出。")
    except Exception as exc:  # pragma: no cover - last-resort startup guard
        log.exception("Fatal startup error")
        _fatal(f"EchoNote 启动失败：{exc}\n\n日志：{log_path or '(无)'}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
