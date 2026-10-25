"""Regenerate the static-asset cache key from the assets' own content.

The key used to be hand-bumped in four places at once — ``index.html``'s
``?v=`` query strings, ``app.js``'s ``ASSET_VERSION``, and the ``?v=``
specifiers inside ``native-boot.js`` / ``native-engine.js``. Human memory
failed twice in a row: ``c9fd883`` and ``db33ef4`` bumped the HTML but left
``ASSET_VERSION`` behind, and the two native modules drifted onto *different*
keys — which makes the browser treat ``native-vad.js?v=l`` and
``native-vad.js?v=g`` as two modules, giving two ``MODES`` objects and two
``Segmenter`` classes.

Deriving the key from content removes the memory step: edit any asset and the
key goes stale, ``--check`` names the carrier still holding the old number, and
rewriting is one command that cannot forget a carrier.

Usage::

    python tools/bump_asset_version.py           # rewrite every carrier
    python tools/bump_asset_version.py --check   # exit 1 if any carrier is stale
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

# Assets whose content the key represents. Anything the browser is asked to
# re-fetch because it changed belongs here; a suffix rule means a new module is
# covered the day it lands, not the day someone remembers to add it.
HASHED_SUFFIXES = {".js", ".css", ".json"}
CARRIER_SUFFIXES = {".html", ".js"}

# ``index.html`` carries the key for everything else, so hashing it would feed
# the key into its own input.
NOT_HASHED = {"index.html"}

VERSION_PARAM = re.compile(r"\?v=[0-9A-Za-z._-]+")
ASSET_LITERAL = re.compile(r'(const\s+ASSET_VERSION\s*=\s*")[^"]*(")')


def _text(path: Path) -> str:
    """Read an asset as text with line endings normalised for hashing.

    Git may hand a Windows clone CRLF and CI LF; without normalisation the same
    commit would produce two different keys.
    """
    return path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")


def _neutralise(text: str) -> str:
    """Blank out the key itself so the digest depends only on real content."""
    text = VERSION_PARAM.sub("?v=", text)
    text = ASSET_LITERAL.sub(r"\1\2", text)
    return text


def hashed_assets(web: Path = WEB) -> list[Path]:
    return sorted(
        path
        for path in web.iterdir()
        if path.is_file() and path.suffix in HASHED_SUFFIXES and path.name not in NOT_HASHED
    )


def compute_key(web: Path = WEB) -> str:
    digest = hashlib.sha256()
    for path in hashed_assets(web):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_neutralise(_text(path)).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:10]


def carriers(web: Path = WEB) -> list[Path]:
    """Files that hold a key and therefore have to be rewritten."""
    found = []
    for path in sorted(web.iterdir()):
        if not path.is_file() or path.suffix not in CARRIER_SUFFIXES:
            continue
        text = _text(path)
        if VERSION_PARAM.search(text) or ASSET_LITERAL.search(text):
            found.append(path)
    return found


def apply_key(text: str, key: str) -> str:
    text = VERSION_PARAM.sub(f"?v={key}", text)
    text = ASSET_LITERAL.sub(lambda match: f"{match.group(1)}{key}{match.group(2)}", text)
    return text


def stale_carriers(web: Path = WEB) -> list[Path]:
    """Carriers that do not already hold the key derived from content."""
    key = compute_key(web)
    return [path for path in carriers(web) if apply_key(_text(path), key) != _text(path)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Derive the static-asset cache key from content.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report stale carriers and exit 1 instead of rewriting them",
    )
    parser.add_argument("--web", type=Path, default=WEB, help="assets directory (default: web/)")
    args = parser.parse_args(argv)

    key = compute_key(args.web)
    stale = stale_carriers(args.web)

    if args.check:
        if not stale:
            print(f"asset key {key} is up to date")
            return 0
        print(f"stale asset key: content hashes to {key}, but these still disagree:")
        for path in stale:
            print(f"  {path.relative_to(args.web.parent)}")
        print("fix: python tools/bump_asset_version.py")
        return 1

    if not stale:
        print(f"asset key {key} is up to date")
        return 0

    for path in stale:
        # newline="" keeps whatever CRLF/LF the file already had.
        path.write_text(apply_key(path.read_text(encoding="utf-8"), key), encoding="utf-8", newline="")
        print(f"  {path.relative_to(args.web.parent)} -> {key}")
    print(f"asset key {key} written to {len(stale)} carrier(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
