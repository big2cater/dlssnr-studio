"""Locate helper binaries and build the arguments that re-enter this app's CLI.

A packaged build ships ffmpeg/ffprobe in vendor/ffmpeg/bin next to the exe, so
the app never depends on whatever ffmpeg happens to sit on PATH (or on there
being one at all). Source checkouts fall back to PATH.

The second problem this module solves: the window runs every job by executing
the same binary again in CLI mode. From source that means ``python tools/ve_cli.py``,
which needs the real path on disk. Frozen, tools/ is not shipped, so the child
gets the bare module name and ``nr_app.dispatch`` imports it out of the bundle.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> pathlib.Path:
    """Directory holding build/, runtime/ and vendor/ next to the exe."""
    if is_frozen():
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parents[1]


def tool(name: str) -> str:
    """Absolute path to a bundled helper, else whatever PATH offers."""
    exe = name + (".exe" if os.name == "nt" else "")
    for base in (app_root() / "vendor/ffmpeg/bin", app_root() / "vendor/ffmpeg"):
        candidate = base / exe
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name) or name


def child_script(script: pathlib.Path) -> str:
    """First argument that makes a child process run ``script``.

    Frozen: the bundle has no tools/ on disk, so pass the bare name and let
    dispatch() import the module out of the archive. Source: the real path,
    because the interpreter has to open the file.
    """
    return script.name if is_frozen() else str(script)


def subprocess_env() -> dict:
    """Environment for child processes, with the bundled ffmpeg discoverable.

    Some children (a nested nr_video.py started by ve_cli.py) call ``tool()``
    themselves; prepending the vendor directory means a plain PATH lookup
    would find the same ffmpeg the parent picked.
    """
    env = dict(os.environ)
    vendor = app_root() / "vendor/ffmpeg/bin"
    if vendor.is_dir():
        env["PATH"] = str(vendor) + os.pathsep + env.get("PATH", "")
    return env
