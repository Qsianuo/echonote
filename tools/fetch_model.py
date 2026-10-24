#!/usr/bin/env python
"""Download a speech model once, so the app can run fully offline afterwards.

This is a thin CLI over :func:`app.fetch.download`. It is the only entry point
a user needs before going offline:

    python tools/fetch_model.py --list
    python tools/fetch_model.py                      # default pair (live + final)
    python tools/fetch_model.py --model medium
    python tools/fetch_model.py --all

The packaged ``EchoNote.exe --fetch-model`` runs the same code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import fetch, models  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download an EchoNote model")
    parser.add_argument(
        "--model",
        default=None,
        help="catalog key, or 'sensevoice' for the offline SenseVoice backend "
        "(default: the pair the app runs on — a fast live "
        "model plus the default final model)",
    )
    parser.add_argument("--all", action="store_true", help="fetch every catalog entry")
    parser.add_argument(
        "--model-dir",
        default=str(ROOT / "models"),
        help="where models live (default: ./models)",
    )
    parser.add_argument("--endpoint", default=None, help="default: https://hf-mirror.com")
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    parser.add_argument("--list", action="store_true", help="list the catalog")
    args = parser.parse_args(argv)

    model_dir = Path(args.model_dir)
    if args.list:
        print(f"models in {model_dir}")
        for spec in models.CATALOG:
            present = models.is_complete(models.local_dir(model_dir, spec))
            mark = "*" if spec.key in models.default_pair() else " "
            print(
                f" {mark} {spec.key:<14} {spec.label:<6} {fetch.human(spec.size_mb):>8}  "
                f"{'present' if present else '-':<8} {spec.note}"
            )
        sensevoice_dir = model_dir / models.SENSEVOICE_DIRNAME
        sensevoice_present = all(
            (sensevoice_dir / name).is_file() for name in models.SENSEVOICE_FILES
        )
        print(
            f"   {'sensevoice':<14} {'中文':<6} {'~230 MB':>8}  "
            f"{'present' if sensevoice_present else '-':<8} "
            "SenseVoice 定稿档（ECHONOTE_ASR_BACKEND=sensevoice 时使用）"
        )
        print(
            "\n* = part of the default pair (live caption model + final model)."
            " Pass --model <key> to fetch one specific model."
        )
        return 0

    if args.model == "sensevoice":
        # Not part of the whisper catalog: it is a backend of its own
        # (ECHONOTE_ASR_BACKEND=sensevoice) with a different file layout, so
        # it bypasses the ModelSpec loop entirely.
        fetch.prepare_env(args.endpoint)
        model_dir.mkdir(parents=True, exist_ok=True)
        ok = fetch.download_sensevoice(model_dir, endpoint=args.endpoint, force=args.force)
        print()
        if ok:
            print("Model ready. The app needs no network from here on.")
        return 0 if ok else 1

    if args.all:
        specs = list(models.CATALOG)
    elif args.model is None:
        # v0.11 runs two models: a small one for the live caption and the
        # default one for the accurate pass. Fetching only the default would
        # silently degrade live captions to the accurate model's speed, which
        # is the exact complaint this release fixes.
        specs = [models.BY_KEY[key] for key in models.default_pair()]
    elif args.model in models.BY_KEY:
        specs = [models.BY_KEY[args.model]]
    else:
        print(f"unknown model {args.model!r}; try --list")
        return 2

    fetch.prepare_env(args.endpoint)
    model_dir.mkdir(parents=True, exist_ok=True)

    ok = True
    for spec in specs:
        print(f"\n[{spec.key}] {spec.label}")
        ok = fetch.download(spec, model_dir, force=args.force) and ok

    print()
    if ok:
        print("Model ready. The app needs no network from here on.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
