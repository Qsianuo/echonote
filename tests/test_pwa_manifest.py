"""The installable-window metadata has to stay in sync with the files on disk.

A manifest that points at a missing icon does not fail loudly — the browser
just falls back to a generic tile, which is exactly the "this looks like a
website" problem the manifest exists to fix. Pin the pairing here.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
MANIFEST = WEB / "manifest.json"


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_is_valid_json_with_the_fields_install_needs():
    data = load()
    for field in ("name", "short_name", "start_url", "scope", "display", "icons"):
        assert field in data, f"manifest is missing {field}"
    assert data["display"] == "standalone", "without standalone it opens as a tab"


def test_every_declared_icon_exists_and_is_square_png():
    data = load()
    assert data["icons"], "a manifest with no icons cannot be installed"
    for icon in data["icons"]:
        path = WEB / icon["src"].lstrip("/")
        assert path.is_file(), f"manifest points at a missing file: {path}"
        header = path.read_bytes()[:8]
        assert header == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
        width, height = icon["sizes"].split("x")
        assert width == height, "icons must be square"


def test_installed_window_and_taskbar_colours_match_the_ui():
    # A white theme-color flashes a white bar before the dark UI paints.
    data = load()
    assert data["theme_color"].lower() == "#060d1a"
    assert data["background_color"].lower() == "#060d1a"


def test_index_html_links_the_manifest_and_theme_colour():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'rel="manifest"' in html
    assert 'name="theme-color"' in html
    assert "icon-192.png" in html, "apple-touch-icon is needed for macOS/iOS"


def test_start_url_is_root_so_a_deep_link_does_not_capture_the_scope():
    data = load()
    assert data["start_url"] == "/"
    assert data["scope"] == "/"
