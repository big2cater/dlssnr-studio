"""Batch front end shaped after dlss5-visual-enhancer's VE_CLI.

    python3 tools/ve_cli.py devices
    python3 tools/ve_cli.py render samples/photo.jpg -o outputs/batch
    python3 tools/ve_cli.py render samples -o outputs/batch --recursive --preset presets/portrait.json
    python3 tools/ve_cli.py render samples -o outputs/batch --dry-run
    python3 tools/ve_cli.py render samples -o outputs/batch --json --quiet

Images go through build/nr_offline.exe, videos through tools/nr_video.py.
Everything else (PNG/JPG/BMP output, temporal NR, audio) is handled by those.
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import subprocess
import sys

import _proc  # hidden-window spawns (see _proc.py)
import time

try:  # frozen exe: the project root is next to the exe, not the bundle
    from _paths import project_root as _project_root
    ROOT = _project_root()
except Exception:
    ROOT = pathlib.Path(__file__).resolve().parents[1]
try:  # frozen: tools/ is inside the bundle, so children re-enter by module name
    from _tools import child_script as _child_script
except Exception:  # pragma: no cover - source checkout without tools/ on sys.path
    def _child_script(script: pathlib.Path) -> str:
        return str(script)
IMAGE_EXE = ROOT / "build/nr_offline.exe"
VIDEO_TOOL = ROOT / "tools/nr_video.py"
DEFAULT_ROOT = ROOT / "runtime"
DEFAULT_OUTPUT = ROOT / "outputs/batch"

from _media import IMAGE_EXT, MEDIA_EXT, VIDEO_EXT  # noqa: E402  (single definition)

DEFAULT_PRESET = {
    "neural_rendering": {
        "model": "small",
        "intensity": 1.0,
        "style": 0,
        "passes": 1,
        "local_tone": 1.0,
        "local_structure": 1.0,
        "skin_structure": -1.0,
        "automatic_mask": True,
    },
    "video": {
        "temporal": True,
        "noise_every_frame": False,
        "codec": "libx264",
        "crf": 18,
        # Variable-frame-rate sources are resampled to a constant rate so the
        # clip keeps its duration and the audio stays in sync.
        "vfr": "cfr",
        "max_seconds": 0.0,
    },
    "output": {"suffix": "_visual_enhancer", "image_ext": "png"},
    "runtime": {"aco": False, "batch": 1, "rgba8": False},
}


def load_preset(path: str | None) -> dict:
    cfg = copy.deepcopy(DEFAULT_PRESET)
    if not path:
        return cfg
    raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    for section in ("neural_rendering", "video", "output", "runtime"):
        if isinstance(raw.get(section), dict):
            cfg[section].update(raw[section])
    # Flat keys are accepted too, so a small hand-written preset stays simple.
    for key, value in raw.items():
        if key in cfg:
            continue
        for section in ("neural_rendering", "video", "output", "runtime"):
            if key in cfg[section]:
                cfg[section][key] = value
                break
    return cfg


def collect(paths: list[str], recursive: bool) -> list[tuple[pathlib.Path, str]]:
    found: list[tuple[pathlib.Path, str]] = []
    seen: set[pathlib.Path] = set()
    for raw in paths:
        p = pathlib.Path(raw)
        if not p.is_absolute():
            p = (pathlib.Path.cwd() / p).resolve()
        candidates: list[pathlib.Path] = []
        if p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            candidates = [x for x in it if x.is_file()]
        elif p.is_file():
            candidates = [p]
        else:
            print(f"warning: not found: {p}", file=sys.stderr)
        for f in sorted(candidates):
            ext = f.suffix.lower()
            kind = "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None
            if kind and f not in seen:
                seen.add(f)
                found.append((f, kind))
    return found


def decide_output(inp: pathlib.Path, out_arg: pathlib.Path, kind: str,
                  single: bool, cfg: dict, overwrite: bool,
                  reserved: set[pathlib.Path]) -> pathlib.Path:
    if single and out_arg.suffix:
        reserved.add(out_arg)
        return out_arg
    ext = ".mp4" if kind == "video" else "." + str(cfg["output"]["image_ext"]).lstrip(".")
    stem = inp.stem + str(cfg["output"]["suffix"])
    candidate = out_arg / (stem + ext)
    n = 2
    # Names planned by this run always conflict (two inputs can share a stem),
    # even with --overwrite and even in --dry-run. --overwrite only permits
    # replacing files that were already on disk before this run.
    while candidate in reserved or (not overwrite and candidate.exists()):
        candidate = out_arg / f"{stem}_{n}{ext}"
        n += 1
    reserved.add(candidate)
    return candidate


def image_command(inp, out, cfg, root) -> list[str]:
    nr = cfg["neural_rendering"]
    cmd = [str(IMAGE_EXE), "render", str(inp), "-o", str(out), "--root", str(root),
           "--model", str(nr["model"]), "--intensity", str(nr["intensity"]),
           "--style", str(nr["style"]), "--passes", str(nr["passes"]),
           "--local-tone", str(nr["local_tone"]), "--local-structure", str(nr["local_structure"]),
           "--skin-structure", str(nr["skin_structure"])]
    if not nr["automatic_mask"]:
        cmd.append("--no-automask")
    if cfg["runtime"]["aco"]:
        cmd.append("--aco")
    if cfg["runtime"]["rgba8"]:
        cmd.append("--rgba8")
    return cmd


def video_command(inp, out, cfg, root) -> list[str]:
    nr, v = cfg["neural_rendering"], cfg["video"]
    cmd = [sys.executable, _child_script(VIDEO_TOOL), str(inp), "-o", str(out), "--root", str(root),
           "--model", str(nr["model"]), "--intensity", str(nr["intensity"]),
           "--style", str(nr["style"]), "--passes", str(nr["passes"]),
           "--local-tone", str(nr["local_tone"]), "--local-structure", str(nr["local_structure"]),
           "--skin-structure", str(nr["skin_structure"]),
           "--codec", str(v["codec"]), "--crf", str(v["crf"])]
    if not nr["automatic_mask"]:
        cmd.append("--no-automask")
    if not v["temporal"]:
        cmd.append("--no-temporal")
    if v["noise_every_frame"]:
        cmd.append("--noise-every-frame")
    if v.get("vfr", "cfr") == "as-is":
        cmd += ["--vfr", "as-is"]
    if float(v.get("max_seconds") or 0) > 0:
        cmd += ["--max-seconds", f"{float(v['max_seconds']):.3f}"]
    if cfg["runtime"]["aco"]:
        cmd.append("--aco")
    if cfg["runtime"].get("rgba8"):
        cmd.append("--rgba8")
    cmd += ["--batch", str(cfg["runtime"].get("batch", 1))]
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["render", "devices"])
    ap.add_argument("paths", nargs="*")
    ap.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT))
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--preset")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--skip-existing", action="store_true",
                    help="skip a job when its base output name already exists (default: auto-number like VE_CLI)")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    # Explicit overrides win over the preset.
    ap.add_argument("--model", choices=["small", "original", "auto"])
    ap.add_argument("--intensity", type=float)
    ap.add_argument("--style", type=int)
    ap.add_argument("--passes", type=int)
    ap.add_argument("--local-tone", type=float)
    ap.add_argument("--local-structure", type=float)
    ap.add_argument("--skin-structure", type=float)
    ap.add_argument("--no-automask", action="store_true")
    ap.add_argument("--no-temporal", action="store_true")
    ap.add_argument("--noise-every-frame", action="store_true")
    ap.add_argument("--codec")
    ap.add_argument("--crf", type=int)
    ap.add_argument("--max-seconds", type=float,
                    help="video: only output the first N seconds (0 = whole clip)")
    ap.add_argument("--vfr", choices=["cfr", "as-is"],
                    help="video: variable-frame-rate handling (default cfr keeps duration/audio in sync)")
    ap.add_argument("--aco", action="store_true", help="use precompiled ACO machine code")
    ap.add_argument("--no-aco", action="store_true")
    ap.add_argument("--batch", type=int, help="video: frames per submit (1..32)")
    ap.add_argument("--rgba8", action="store_true", help="video: RGBA8 direct path (faster, bit-identical)")
    a = ap.parse_args()

    if a.command == "devices":
        return _proc.run([str(IMAGE_EXE), "devices"]).returncode
    if not a.paths:
        print("render needs at least one input path", file=sys.stderr)
        return 2
    cfg = load_preset(a.preset)
    for key in ("model", "intensity", "style", "passes", "local_tone", "local_structure", "skin_structure"):
        val = getattr(a, key)
        if val is not None:
            cfg["neural_rendering"][key] = val
    if a.no_automask:
        cfg["neural_rendering"]["automatic_mask"] = False
    if a.no_temporal:
        cfg["video"]["temporal"] = False
    if a.noise_every_frame:
        cfg["video"]["noise_every_frame"] = True
    if a.codec:
        cfg["video"]["codec"] = a.codec
    if a.crf is not None:
        cfg["video"]["crf"] = a.crf
    if a.vfr:
        cfg["video"]["vfr"] = a.vfr
    if a.max_seconds is not None:
        cfg["video"]["max_seconds"] = max(0.0, a.max_seconds)
    if a.aco:
        cfg["runtime"]["aco"] = True
    if a.no_aco:
        cfg["runtime"]["aco"] = False
    if a.batch is not None:
        cfg["runtime"]["batch"] = a.batch
    if a.rgba8:
        cfg["runtime"]["rgba8"] = True

    jobs = collect(a.paths, a.recursive)
    if not jobs:
        print("no supported media found", file=sys.stderr)
        return 2
    out_arg = pathlib.Path(a.output)
    if not out_arg.is_absolute():
        out_arg = (pathlib.Path.cwd() / out_arg).resolve()
    single = len(jobs) == 1
    if not (single and out_arg.suffix):
        out_arg.mkdir(parents=True, exist_ok=True)

    results = []
    reserved: set[pathlib.Path] = set()
    started = time.monotonic()
    for inp, kind in jobs:
        ext0 = ".mp4" if kind == "video" else "." + str(cfg["output"]["image_ext"]).lstrip(".")
        base = out_arg if (single and out_arg.suffix) else out_arg / (inp.stem + str(cfg["output"]["suffix"]) + ext0)
        if a.skip_existing and not a.overwrite and base.exists():
            results.append({"input": str(inp), "output": str(base), "kind": kind, "status": "skipped"})
            if not a.quiet:
                print(f"[skip] {base.name} exists")
            continue
        out = decide_output(inp, out_arg, kind, single, cfg, a.overwrite, reserved)
        cmd = image_command(inp, out, cfg, a.root) if kind == "image" else video_command(inp, out, cfg, a.root)
        item = {"input": str(inp), "output": str(out), "kind": kind, "command": cmd}
        if a.dry_run:
            item["status"] = "dry-run"
            results.append(item)
            if not a.quiet:
                print(f"[dry-run] {kind:5s} {inp.name} -> {out}")
            continue
        t0 = time.monotonic()
        r = _proc.run(cmd, capture_output=True, text=True, errors="replace")
        item["seconds"] = round(time.monotonic() - t0, 2)
        item["status"] = "ok" if r.returncode == 0 else "failed"
        if r.returncode:
            item["error"] = (r.stderr or r.stdout)[-1200:]
        results.append(item)
        if not a.quiet:
            tag = "ok" if r.returncode == 0 else "FAIL"
            print(f"[{tag}] {kind:5s} {inp.name} -> {out.name}  {item['seconds']}s")
            if r.returncode:
                print(item["error"], file=sys.stderr)

    summary = {
        "jobs": len(results),
        "ok": sum(1 for x in results if x["status"] == "ok"),
        "failed": sum(1 for x in results if x["status"] == "failed"),
        "skipped": sum(1 for x in results if x["status"] == "skipped"),
        "dry_run": a.dry_run,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "results": results,
    }
    if a.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    elif not a.quiet:
        print(f"done: {summary['ok']} ok / {summary['failed']} failed / "
              f"{summary['skipped']} skipped in {summary['elapsed_seconds']}s")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
