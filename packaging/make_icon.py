"""Generate ``echonote.ico`` without pulling in Pillow.

The mark matches ``web/favicon.svg``: a navy rounded square with a teal
waveform. Only the standard library is used — the release build already has a
large enough dependency surface without adding an image library for one file.

Run from the repo root:  python packaging/make_icon.py
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

NAVY = (0x0A, 0x19, 0x2F)
TEAL = (0x64, 0xFF, 0xDA)
SIZES = (16, 24, 32, 48, 64, 128, 256)

# Bar centres and heights, taken from the SVG paths (x, half-height).
BARS = (
    (0.1875, 0.078),
    (0.3125, 0.188),
    (0.4375, 0.297),
    (0.5625, 0.203),
    (0.6875, 0.125),
    (0.8125, 0.078),
)
BAR_HALF_WIDTH = 0.031  # 4 px of 64
CORNER = 0.25  # 16 of 64


def _rounded_rect_alpha(x: float, y: float, size: int, radius: float) -> float:
    """Coverage of a rounded square at pixel (x, y), antialiased by sampling."""
    total = 0.0
    samples = 3
    for sy in range(samples):
        for sx in range(samples):
            px = x + (sx + 0.5) / samples
            py = y + (sy + 0.5) / samples
            cx = min(max(px, radius), size - radius)
            cy = min(max(py, radius), size - radius)
            dx = px - cx
            dy = py - cy
            if math.hypot(dx, dy) <= radius:
                total += 1.0
    return total / (samples * samples)


def render(size: int) -> bytes:
    """Return BGRA pixels, top-down, for one icon size."""
    radius = CORNER * size
    pixels = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            cover = _rounded_rect_alpha(x, y, size, radius)
            alpha = int(round(255 * cover))
            if alpha == 0:
                continue
            r, g, b = NAVY
            # Waveform bars, drawn as vertical rounded strokes.
            for cx_frac, half_frac in BARS:
                cx = cx_frac * size
                half = half_frac * size
                half_w = max(BAR_HALF_WIDTH * size, size / 32)
                if abs(x + 0.5 - cx) > half_w:
                    continue
                if abs(y + 0.5 - size / 2) > half:
                    continue
                r, g, b = TEAL
            offset = (y * size + x) * 4
            pixels[offset] = b
            pixels[offset + 1] = g
            pixels[offset + 2] = r
            pixels[offset + 3] = alpha
    return bytes(pixels)


def bmp_payload(size: int) -> bytes:
    """BITMAPINFOHEADER + bottom-up BGRA + the 1bpp AND mask ICO requires."""
    top_down = render(size)
    rows = []
    for y in range(size - 1, -1, -1):  # BMP stores bottom-up
        rows.append(top_down[y * size * 4 : (y + 1) * size * 4])
    pixels = b"".join(rows)

    mask_row = ((size + 31) // 32) * 4  # padded to 4 bytes
    mask = bytes(mask_row * size)

    header = struct.pack(
        "<IiiHHIIiiII",
        40,  # biSize
        size,  # biWidth
        size * 2,  # biHeight: XOR + AND
        1,  # biPlanes
        32,  # biBitCount
        0,  # biCompression (BI_RGB)
        len(pixels) + len(mask),  # biSizeImage
        0,
        0,
        0,
        0,
    )
    return header + pixels + mask


def build(path: Path) -> None:
    images = [(size, bmp_payload(size)) for size in SIZES]
    count = len(images)
    out = bytearray()
    out += struct.pack("<HHH", 0, 1, count)  # ICONDIR

    offset = 6 + 16 * count
    for size, payload in images:
        dim = 0 if size >= 256 else size
        out += struct.pack(
            "<BBBBHHII",
            dim,  # width (0 means 256)
            dim,  # height
            0,  # palette colours
            0,  # reserved
            1,  # colour planes
            32,  # bits per pixel
            len(payload),
            offset,
        )
        offset += len(payload)

    for _size, payload in images:
        out += payload

    path.write_bytes(bytes(out))


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "echonote.ico"
    build(target)
    print(f"wrote {target} ({target.stat().st_size} bytes, {len(SIZES)} sizes)")
