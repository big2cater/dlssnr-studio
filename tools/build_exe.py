"""Build the dev quick-start exe with PyInstaller.

    python3 tools/build_exe.py              # build + copy to the project root
    python3 tools/build_exe.py --selftest   # then run the headless checks

This onefile exe is for development only: it expects build/nr_offline.exe,
build/nr_stream.exe, runtime/ and an ffmpeg on PATH to sit next to it, so it
only works inside the project tree. The file you actually hand out is built by
tools/package.py, which packs everything into one double-clickable NRStudio.exe
and unpacks it into %LOCALAPPDATA% on first launch.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENTRY = ROOT / "app/nr_app.py"
BUILT = ROOT / "dist/nr-app.exe"
DEST = ROOT / "nr-app.exe"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="run --selftest and --uitest afterwards")
    a = ap.parse_args()

    if not ENTRY.is_file():
        print(f"missing entry point: {ENTRY}")
        return 2

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "nr-app",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build_pyi"),
        "--paths", str(ROOT / "tools"),
        "--paths", str(ROOT / "app"),
        "--hidden-import", "ve_cli",
        "--hidden-import", "nr_video",
        "--hidden-import", "_paths",
        "--add-data", f"{ROOT / 'presets'};presets",
        str(ENTRY),
    ]
    print("pyinstaller ...")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        return rc
    if not BUILT.is_file():
        print("build reported success but dist/nr-app.exe is missing")
        return 1

    shutil.copy2(BUILT, DEST)
    print(f"exe: {DEST}  ({DEST.stat().st_size / 1048576:.1f} MB)")
    print("runtime needs: build/nr_offline.exe, build/nr_stream.exe, runtime/ and ffmpeg on PATH")

    if a.selftest:
        for flag in ("--selftest", "--uitest"):
            print(f"\n{flag}:")
            rc = subprocess.run([str(DEST), flag]).returncode
            if rc != 0:
                return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
