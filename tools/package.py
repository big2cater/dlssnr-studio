"""Package the whole app into one double-clickable exe.

    python3 tools/package.py             # build NRStudio.exe in the project root
    python3 tools/package.py --skip-app  # reuse the last PyInstaller output

Layout of the produced file (see tools/launcher.cpp for the reader):

    [ launcher.exe ][ payload.zip ][ 32-byte footer ]

The payload is the PyInstaller onedir output plus the C++ hosts, the model
runtime and a private ffmpeg. Everything the app needs at run time is inside,
so the shipped exe does not care what is on PATH or whether the source tree is
anywhere near it. On first launch the launcher unpacks the payload once into
%LOCALAPPDATA%\\NRStudio\\<build id> and starts the real program from there.

Only files that are actually read at run time go in: vendor/ (upstream sources),
models/ (superseded weights) and build/obj are left out.
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import shutil
import struct
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENTRY = ROOT / "app/nr_app.py"
LAUNCHER_SRC = ROOT / "tools/launcher.cpp"
LAUNCHER = ROOT / "build/launcher.exe"
APP_DIST = ROOT / "dist/nr-app"
STAGE = ROOT / "build/stage"
PAYLOAD = ROOT / "build/payload.zip"
OUT = ROOT / "NRStudio.exe"
SEVENZIP = pathlib.Path(r"C:\Program Files\7-Zip\7z.exe")
CLANG_CL = pathlib.Path(r"C:\Program Files\LLVM\bin\clang-cl.exe")
LLVM_RC = pathlib.Path(r"C:\Program Files\LLVM\bin\llvm-rc.exe")
ICON = ROOT / "build/app.ico"
ICON_RES = ROOT / "build/app.res"

MAGIC = b"NRPACK01"
FOOTER = struct.Struct("<8sQ8sQ")  # magic, payload size, build id, reserved

# sample clips the bundled --selftest/--uitest render; the 4K ones stay out
SAMPLES = ["photo.jpg", "testvfr_audio.mp4", "input.mp4"]


def human(size: int) -> str:
    return f"{size / 1048576:.1f} MB"


def build_icon() -> int:
    """Draw the .ico (no binary asset is checked in) and compile it to a .res."""
    r = subprocess.run([sys.executable, str(ROOT / "tools/make_icon.py")],
                       capture_output=True, text=True, errors="replace")
    if r.returncode or not ICON.is_file():
        print("\n".join((r.stdout + r.stderr).splitlines()[:20]))
        print("icon generation failed (continuing without one)")
        return 0

    # llvm-rc resolves the ICON path relative to the .rc file, so both live in build/.
    rc = ICON.parent / "app.rc"
    rc.write_text('1 ICON "app.ico"\n', encoding="utf-8")
    r = subprocess.run([str(LLVM_RC), "/fo", str(ICON_RES), str(rc)],
                       capture_output=True, text=True, errors="replace")
    if r.returncode:
        print("\n".join((r.stdout + r.stderr).splitlines()[:20]))
        print("resource compile failed (continuing without an icon)")
        ICON_RES.unlink(missing_ok=True)
    return 0


def build_launcher() -> int:
    """Compile the self-extracting stub."""
    cmd = [
        str(CLANG_CL), "/std:c++17", "/O2", "/EHsc", "/DUNICODE", "/D_UNICODE", "/W4",
        str(LAUNCHER_SRC), "/Fe:" + str(LAUNCHER), "/link", "/SUBSYSTEM:WINDOWS",
        "user32.lib", "gdi32.lib", "shell32.lib", "comctl32.lib", "ole32.lib",
    ]
    if ICON_RES.is_file():
        cmd.insert(cmd.index("/link"), str(ICON_RES))  # a .res is an input, not a link flag
    print("clang-cl launcher ...")
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode:
        print("\n".join((r.stdout + r.stderr).splitlines()[:40]))
        return 1
    print(f"  {LAUNCHER.name}  {human(LAUNCHER.stat().st_size)}")
    return 0


def build_app() -> int:
    """PyInstaller onedir build of the window."""
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir", "--windowed", "--name", "nr-app",
        "--contents-directory", "_internal",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build_pyi"),
        "--paths", str(ROOT / "tools"),
        "--paths", str(ROOT / "app"),
        "--hidden-import", "ve_cli",
        "--hidden-import", "nr_video",
        "--hidden-import", "_paths",
        "--hidden-import", "_tools",
        "--hidden-import", "_media",
        "--hidden-import", "_proc",
        "--add-data", f"{ROOT / 'presets'};presets",
    ]
    if ICON.is_file():
        cmd += ["--icon", str(ICON)]
    cmd.append(str(ENTRY))
    print("pyinstaller (onedir) ...")
    r = subprocess.run(cmd)
    if r.returncode:
        return r.returncode
    exe = APP_DIST / "nr-app.exe"
    if not exe.is_file():
        print(f"pyinstaller finished but {exe} is missing")
        return 1
    return 0


def copy_tree(src: pathlib.Path, dst: pathlib.Path) -> int:
    if not src.is_dir():
        print(f"  ! missing {src}")
        return 0
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())


def stage() -> int:
    """Assemble the directory that becomes the payload."""
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    print("staging ...")
    shutil.copytree(APP_DIST, STAGE, dirs_exist_ok=True)

    # C++ hosts, model runtime, private ffmpeg
    total = 0
    for name in ("build",):
        (STAGE / name).mkdir(exist_ok=True)
        for exe in ("nr_offline.exe", "nr_stream.exe"):
            src = ROOT / name / exe
            if not src.is_file():
                print(f"  ! missing {src} (run tools/build_offline.py / build_stream.py)")
                return 1
            shutil.copy2(src, STAGE / name / exe)
            total += src.stat().st_size

    # Models and shaders. The .bak tree next to it is a stale upstream copy.
    runtime_dst = STAGE / "runtime"
    runtime_dst.mkdir()
    total += copy_tree(ROOT / "runtime/dlssnr-amd", runtime_dst / "dlssnr-amd")
    ini = ROOT / "runtime/dlssnr-amd.ini"
    if ini.is_file():
        shutil.copy2(ini, runtime_dst / ini.name)

    # ffmpeg: the shared build needs its DLLs beside the exe. ffplay is a player
    # we never call, and avdevice is imported by ffmpeg.exe itself, so it stays.
    ff_src = ROOT / "vendor/ffmpeg/bin"
    if ff_src.is_dir():
        ff_dst = STAGE / "vendor/ffmpeg/bin"
        ff_dst.mkdir(parents=True)
        for item in ff_src.iterdir():
            if item.is_file() and item.name.lower() != "ffplay.exe":
                shutil.copy2(item, ff_dst / item.name)
                total += item.stat().st_size
    else:
        print(f"  ! {ff_src} not found: the exe will need ffmpeg on PATH")

    for name in SAMPLES:
        src = ROOT / "samples" / name
        if src.is_file():
            (STAGE / "samples").mkdir(exist_ok=True)
            shutil.copy2(src, STAGE / "samples" / name)

    files = sum(1 for f in STAGE.rglob("*") if f.is_file())
    print(f"  {files} files staged")
    return 0


def pack(mx: int) -> int:
    if not SEVENZIP.is_file():
        print(f"7-Zip not found at {SEVENZIP}")
        return 1
    if PAYLOAD.exists():
        PAYLOAD.unlink()
    cmd = [
        str(SEVENZIP), "a", "-tzip", f"-mx={mx}", "-mcu=on", "-bso0", "-bsp0",
        str(PAYLOAD), ".",
    ]
    print(f"compressing (-mx={mx}) ...")
    started = time.monotonic()
    r = subprocess.run(cmd, cwd=STAGE)
    if r.returncode or not PAYLOAD.is_file():
        print("7z failed")
        return 1
    print(f"  payload.zip  {human(PAYLOAD.stat().st_size)}  "
          f"({time.monotonic() - started:.0f}s)")
    return 0


def build_id(path: pathlib.Path) -> str:
    """Content hash: a rebuilt app gets a new directory, an unchanged one reuses it."""
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:8]


def assemble(ident: str) -> int:
    size = PAYLOAD.stat().st_size
    print(f"assembling {OUT.name} ...")
    with open(OUT, "wb") as out:
        for part in (LAUNCHER, PAYLOAD):
            with open(part, "rb") as handle:
                shutil.copyfileobj(handle, out, 8 << 20)
        out.write(FOOTER.pack(MAGIC, size, ident.encode("ascii"), 0))

    # Read it back the way the launcher does; a bad footer means a dead exe.
    with open(OUT, "rb") as handle:
        handle.seek(-FOOTER.size, 2)
        magic, stored, stored_id, _ = FOOTER.unpack(handle.read(FOOTER.size))
    expected = LAUNCHER.stat().st_size + size + FOOTER.size
    if magic != MAGIC or stored != size or OUT.stat().st_size != expected:
        print("footer check failed")
        return 1
    print(f"  {OUT}  {human(OUT.stat().st_size)}  build {ident}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-app", action="store_true", help="reuse dist/nr-app")
    ap.add_argument("--skip-launcher", action="store_true", help="reuse build/launcher.exe")
    ap.add_argument("--mx", type=int, default=1, help="zip level 0..9 (0 = store only)")
    ap.add_argument("--keep-stage", action="store_true", help="keep build/stage for inspection")
    a = ap.parse_args()

    if not ENTRY.is_file():
        print(f"missing entry point: {ENTRY}")
        return 2
    build_icon()
    if not a.skip_launcher and build_launcher():
        return 1
    if not a.skip_app and build_app():
        return 1
    if stage():
        return 1
    if pack(a.mx):
        return 1
    ident = build_id(PAYLOAD)
    if assemble(ident):
        return 1
    if not a.keep_stage:
        shutil.rmtree(STAGE, ignore_errors=True)
    print("\ndone. double-click it; the first run unpacks once, later runs open at once.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
