"""Guards on the desktop shell: the parts of "feels like an app" that are
easy to break silently.

Nothing here touches a real window or the registry — the point is to pin the
decisions (which browser, which URL, which flags) so a later refactor cannot
quietly turn the application window back into a browser tab.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config, desktop  # noqa: E402


def test_base_url_rewrites_wildcard_hosts_to_loopback(monkeypatch):
    # --app= needs an address the browser can reach; 0.0.0.0 is not one.
    monkeypatch.setattr(config, "HOST", "0.0.0.0", raising=False)
    monkeypatch.setattr(config, "PORT", 8000, raising=False)
    assert desktop._base_url() == "http://127.0.0.1:8000"

    monkeypatch.setattr(config, "HOST", "::", raising=False)
    assert desktop._base_url() == "http://127.0.0.1:8000"

    monkeypatch.setattr(config, "HOST", "127.0.0.1", raising=False)
    monkeypatch.setattr(config, "PORT", 8123, raising=False)
    assert desktop._base_url() == "http://127.0.0.1:8123"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_flag_accepts_the_usual_truthy_spellings(monkeypatch, value):
    monkeypatch.setenv("ECHONOTE_TEST_FLAG", value)
    assert desktop._flag("ECHONOTE_TEST_FLAG") is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_flag_rejects_everything_else(monkeypatch, value):
    monkeypatch.setenv("ECHONOTE_TEST_FLAG", value)
    assert desktop._flag("ECHONOTE_TEST_FLAG") is False


def test_flag_is_false_when_unset(monkeypatch):
    monkeypatch.delenv("ECHONOTE_TEST_FLAG", raising=False)
    assert desktop._flag("ECHONOTE_TEST_FLAG") is False


def test_app_mode_browser_is_either_a_real_path_or_none():
    # On a machine without Chromium this must be None, not a broken command:
    # _open_window falls back to a browser tab in that case.
    found = desktop._app_mode_browser()
    if found is not None:
        assert Path(found).is_file()
        assert Path(found).name.lower() in {"msedge.exe", "chrome.exe"}


def test_focus_existing_never_raises_and_returns_bool():
    # Whether a window exists depends on the machine; only the contract matters.
    assert isinstance(desktop._focus_existing("EchoNote-no-such-window-xyz"), bool)


def test_log_setup_writes_next_to_the_executable_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path, raising=False)
    path = desktop._log_setup()
    assert path is not None
    assert path.parent == tmp_path / "logs"
    assert path.exists()


def test_log_setup_returns_none_when_the_directory_is_unwritable(tmp_path, monkeypatch):
    # A read-only install location must not crash startup; it only loses logs.
    blocked = tmp_path / "readonly"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(config, "ROOT_DIR", blocked, raising=False)
    assert desktop._log_setup() is None


def test_hide_own_console_is_a_no_op_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert desktop._hide_own_console() is False


def test_watchdog_exits_when_asked_to(monkeypatch):
    # ECHONOTE_NO_IDLE_EXIT must return rather than spin, so tests and the
    # packaged --self-test path do not hang.
    monkeypatch.setenv("ECHONOTE_NO_IDLE_EXIT", "1")
    desktop._watch_for_last_window(grace_s=0.0)
    assert os.getenv("ECHONOTE_NO_IDLE_EXIT") == "1"
