"""Measure the effect of NR and the temporal path on a video.

    python3 tools/check_video.py input.mp4 temporal.mp4 plain.mp4
"""
from __future__ import annotations

import subprocess
import sys


def dimensions(path: str) -> tuple[int, int]:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0", path,
    ], text=True)
    w, h = out.strip().split(",")[:2]
    return int(w), int(h)


def frames(path: str, w: int, h: int) -> list[bytes]:
    raw = subprocess.check_output(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgba", "-"])
    size = w * h * 4
    return [raw[i:i + size] for i in range(0, len(raw) - size + 1, size)]


def mae(a: bytes, b: bytes) -> float:
    # Sample one byte per 4 pixels: enough for an effect/jitter metric and
    # keeps this dependency-free script fast on long clips.
    sa, sb = a[::16], b[::16]
    return sum(abs(x - y) for x, y in zip(sa, sb)) / len(sa)


def patch(frame: bytes, w: int, pw: int, ph: int) -> bytes:
    out = bytearray()
    for y in range(ph):
        off = y * w * 4
        out += frame[off:off + pw * 4]
    return bytes(out)


def jitter(fs: list[bytes], w: int, pw: int, ph: int) -> float:
    if len(fs) < 2:
        return 0.0
    return sum(mae(patch(fs[i], w, pw, ph), patch(fs[i - 1], w, pw, ph))
               for i in range(2, len(fs))) / (len(fs) - 2)


def main() -> int:
    w, h = dimensions(sys.argv[1])
    pw = ph = max(16, min(w, h) // 4)
    src, temporal, plain = (frames(p, w, h) for p in sys.argv[1:4])
    print(f"size {w}x{h}  static patch {pw}x{ph}")
    print(f"frames: input={len(src)} temporal={len(temporal)} plain={len(plain)}")
    n = min(len(src), len(temporal), len(plain))
    print(f"input  vs temporal : MAE {sum(mae(src[i], temporal[i]) for i in range(n))/n:.3f}/255")
    print(f"input  vs plain    : MAE {sum(mae(src[i], plain[i]) for i in range(n))/n:.3f}/255")
    print(f"temporal vs plain  : MAE {sum(mae(temporal[i], plain[i]) for i in range(n))/n:.3f}/255")
    print(f"static-patch jitter: input {jitter(src,w,pw,ph):.3f}  "
          f"temporal {jitter(temporal,w,pw,ph):.3f}  plain {jitter(plain,w,pw,ph):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
