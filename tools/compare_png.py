"""Compare two 8-bit RGBA PNGs without third-party packages.

    python3 tools/compare_png.py samples/input.png outputs/out.png
"""
from __future__ import annotations

import struct
import sys
import zlib


def read_rgba(path: str) -> tuple[int, int, bytes]:
    data = open(path, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    pos, idat, w, h, depth, colour = 8, bytearray(), 0, 0, 0, 0
    while pos < len(data):
        n = struct.unpack_from(">I", data, pos)[0]
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + n]
        if tag == b"IHDR":
            w, h, depth, colour = struct.unpack_from(">IIBB", payload, 0)
        elif tag == b"IDAT":
            idat += payload
        elif tag == b"IEND":
            break
        pos += 12 + n
    assert depth == 8 and colour in (2, 6), f"{path}: need 8-bit RGB/RGBA, got depth={depth} colour={colour}"
    bpp = 3 if colour == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = w * bpp
    rgba = bytearray(h * w * 4)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1: line[i] = (line[i] + a) & 255
            elif f == 2: line[i] = (line[i] + b) & 255
            elif f == 3: line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif f == 4:
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
            elif f != 0:
                raise ValueError(f"unknown PNG filter {f}")
        for x in range(w):
            src = x * bpp
            dst = (y * w + x) * 4
            rgba[dst:dst + 3] = line[src:src + 3]
            rgba[dst + 3] = line[src + 3] if bpp == 4 else 255
        prev = line
    return w, h, bytes(rgba)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    w1, h1, a = read_rgba(sys.argv[1])
    w2, h2, b = read_rgba(sys.argv[2])
    if (w1, h1) != (w2, h2):
        print(f"size mismatch: {w1}x{h1} vs {w2}x{h2}")
        return 1
    total = w1 * h1 * 4
    diff = [abs(x - y) for x, y in zip(a, b)]
    pixels = w1 * h1
    changed = sum(1 for i in range(0, total, 4) if any(diff[i:i + 4]))
    print(f"{w1}x{h1}  MAE={sum(diff)/total:.3f}/255  max={max(diff)}  "
          f"changed pixels={changed}/{pixels} ({100.0*changed/pixels:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
