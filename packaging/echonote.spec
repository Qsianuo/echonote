# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the portable Windows build.

The hard part is not our own code, it is the native stack under it:
ctranslate2 (inference), onnxruntime (VAD) and av (audio) all ship DLLs that
PyInstaller cannot discover from imports alone. ``collect_all`` walks each
package and pulls data files, binaries and hidden imports together.

Build:  .venv\\Scripts\\pyinstaller packaging\\echonote.spec --noconfirm
Output: dist\\EchoNote\\EchoNote.exe   (one-dir; one-file would unpack ~300 MB
        to %TEMP% on every launch and make startup feel broken)
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

ROOT = Path(SPECPATH).parent  # noqa: F821 - injected by PyInstaller

datas = [(str(ROOT / "web"), "web")]
binaries = []
hiddenimports = []

# Native stack: DLLs + data (e.g. faster_whisper/assets/silero_vad_v6.onnx).
for package in ("faster_whisper", "ctranslate2", "onnxruntime", "tokenizers", "av", "huggingface_hub"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Version lookups at import time (importlib.metadata) need the wheel metadata.
for dist in ("faster-whisper", "ctranslate2", "onnxruntime", "tokenizers", "huggingface_hub", "av"):
    datas += copy_metadata(dist)

# uvicorn resolves its loop/protocol implementations by string at runtime.
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "websockets.legacy",
    "websockets.legacy.server",
    "app.desktop",
    # Imported lazily from desktop._fetch_model, so PyInstaller's static
    # analysis cannot see it.
    "app.fetch",
    "app.models",
]

excludes = [
    "tkinter",
    "matplotlib",
    "pandas",
    "scipy",
    "torch",
    "transformers",
    "IPython",
    "pytest",
    "PyInstaller",
    "setuptools",
    "pip",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EchoNote",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # keep the console: it is where model-load progress shows
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="EchoNote",
)
