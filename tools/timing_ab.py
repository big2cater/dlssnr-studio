"""A/B the GPU cost of LLPC-compiled shaders vs imported ACO machine code.

    python3 tools/timing_ab.py samples/minimax_h3.mp4 samples/photo_static.mp4

Reads the [timing] line that nr_stream.exe prints from upstream's own
VK_QUERY_TYPE_TIMESTAMP ring (whole recording + network-only), and also times
the whole process so the two independent measures can be cross-checked.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATTERN = re.compile(
    r"\[timing\] frames=(\d+) last_gpu=([\d.]+)ms avg_gpu=([\d.]+)ms avg_network=([\d.]+)ms")


def one(clip: pathlib.Path, mode: str, extra_env: dict[str, str] | None = None,
        max_frames: int = 0) -> dict:
    out = ROOT / "outputs" / f"timing_ab_{clip.stem}_{mode}.mp4"
    cmd = [sys.executable, str(ROOT / "tools/nr_video.py"), str(clip),
           "-o", str(out), "--noise-every-frame", "--crf", "20"]
    if max_frames:
        cmd += ["--max-frames", str(max_frames)]
    if mode == "aco":
        cmd.append("--aco")
    env = None
    if extra_env:
        import os
        env = os.environ.copy()
        env.update(extra_env)
    t0 = time.monotonic()
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=env)
    wall = time.monotonic() - t0
    log = pathlib.Path(str(out) + ".nr_stream.log")
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    m = PATTERN.search(text)
    build = re.search(r"graph build ([\d.]+)s", text)
    result = {"clip": clip.name, "mode": mode, "wall": wall, "rc": r.returncode,
              "build": float(build.group(1)) if build else 0.0}
    if m:
        result.update(frames=int(m.group(1)), last=float(m.group(2)),
                      avg=float(m.group(3)), net=float(m.group(4)))
    else:
        result.update(frames=0, last=0.0, avg=0.0, net=0.0)
        result["error"] = (r.stderr or r.stdout)[-600:]
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("clips", nargs="+")
    ap.add_argument("--diag", action="store_true", help="enable NR_LAYER_TIMING_DIAG raw stamps")
    ap.add_argument("--repeats", type=int, default=1, help="measured runs per mode")
    ap.add_argument("--warmup", type=int, default=0, help="discarded runs per mode")
    ap.add_argument("--max-frames", type=int, default=0, help="cap frames per run")
    a = ap.parse_args()
    env = {"NR_LAYER_TIMING_DIAG": "1"} if a.diag else None

    rows = []
    for raw in a.clips:
        clip = pathlib.Path(raw)
        if not clip.is_absolute():
            clip = (ROOT / clip).resolve()
        for mode in ("llpc", "aco"):
            for _ in range(a.warmup):
                one(clip, mode, env, a.max_frames)
            for k in range(a.repeats):
                r = one(clip, mode, env, a.max_frames)
                r["repeat"] = k + 1
                rows.append(r)
                if r["frames"]:
                    print(f"{r['clip']:22s} {r['mode']:4s} #{k+1} frames={r['frames']:3d}  "
                          f"network={r['net']:7.3f} ms  whole_gpu={r['avg']:7.3f} ms  "
                          f"last={r['last']:7.3f} ms  build={r['build']:5.2f}s  wall={r['wall']:6.2f} s")
                else:
                    print(f"{r['clip']:22s} {r['mode']:4s} #{k+1} FAILED rc={r['rc']}: "
                          f"{r.get('error','')[:300]}")

    print("\n--- dispersion per mode (network ms) ---")
    for raw in a.clips:
        name = pathlib.Path(raw).name
        for mode in ("llpc", "aco"):
            vals = sorted(x["net"] for x in rows if x["clip"] == name and x["mode"] == mode and x["net"] > 0)
            if vals:
                med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals)//2 - 1] + vals[len(vals)//2]) / 2
                print(f"{name:22s} {mode:4s} n={len(vals)} min={vals[0]:.3f} "
                      f"median={med:.3f} max={vals[-1]:.3f} spread={(vals[-1]-vals[0]):.3f}")
    print("\n--- per-clip comparison (medians) ---")
    medians: dict[tuple[str, str], float] = {}
    for raw in a.clips:
        name = pathlib.Path(raw).name
        for mode in ("llpc", "aco"):
            vals = sorted(x["net"] for x in rows if x["clip"] == name and x["mode"] == mode and x["net"] > 0)
            if vals:
                medians[(name, mode)] = (vals[len(vals)//2] if len(vals) % 2
                                         else (vals[len(vals)//2 - 1] + vals[len(vals)//2]) / 2)
        if (name, "llpc") in medians and (name, "aco") in medians:
            l, c = medians[(name, "llpc")], medians[(name, "aco")]
            print(f"{name}: ACO/LLPC = {c/l:.2f}x  ({c:.3f} vs {l:.3f} ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
