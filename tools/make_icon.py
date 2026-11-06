"""Draw the app icon (a violet tile with a download arrow) and save it as a multi-size .ico.

Pure standard library: shapes are rasterized with 4x4 supersampling, each size is drawn
from the same vector description, and every size is stored as a PNG inside the .ico.

    uv run python tools/make_icon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "stremio_dl" / "assets" / "stremio-dl.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)
SAMPLES = 4
TOP = (138, 92, 246)
BOTTOM = (88, 40, 214)
WHITE = (255, 255, 255)


def in_round_rect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    dx = max(abs(x - cx) - ((x1 - x0) / 2 - r), 0.0)
    dy = max(abs(y - cy) - ((y1 - y0) / 2 - r), 0.0)
    return dx * dx + dy * dy <= r * r


def in_triangle(x: float, y: float, a, b, c) -> bool:
    def side(p, q):
        return (x - q[0]) * (p[1] - q[1]) - (p[0] - q[0]) * (y - q[1])

    d1, d2, d3 = side(a, b), side(b, c), side(c, a)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def in_glyph(x: float, y: float) -> bool:
    shaft = in_round_rect(x, y, 0.425, 0.17, 0.575, 0.52, 0.03)
    head = in_triangle(x, y, (0.235, 0.45), (0.765, 0.45), (0.5, 0.71))
    tray = in_round_rect(x, y, 0.22, 0.775, 0.78, 0.865, 0.045)
    return shaft or head or tray


def render(size: int) -> bytes:
    rows = bytearray()
    step = 1 / (size * SAMPLES)
    for py in range(size):
        rows.append(0)  # PNG filter type: none
        for px in range(size):
            r = g = b = a = 0.0
            for sy in range(SAMPLES):
                y = (py * SAMPLES + sy + 0.5) * step
                for sx in range(SAMPLES):
                    x = (px * SAMPLES + sx + 0.5) * step
                    if not in_round_rect(x, y, 0.03, 0.03, 0.97, 0.97, 0.21):
                        continue
                    if in_glyph(x, y):
                        color = WHITE
                    else:
                        color = tuple(t + (u - t) * y for t, u in zip(TOP, BOTTOM, strict=True))
                    r, g, b, a = r + color[0], g + color[1], b + color[2], a + 1
            n = SAMPLES * SAMPLES
            if a:
                rows += bytes((round(r / a), round(g / a), round(b / a), round(255 * a / n)))
            else:
                rows += b"\0\0\0\0"
    return png(size, bytes(rows))


def png(size: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def ico(images: list[tuple[int, bytes]]) -> bytes:
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, data in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the directory entry
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(data for _, data in images)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(ico([(size, render(size)) for size in SIZES]))
    (OUT.with_suffix(".png")).write_bytes(render(256))  # preview for the README / GitHub
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
