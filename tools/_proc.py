"""Spawn helper processes without flashing console windows.

The GUI ships as a windowed exe, so it has no console of its own. Windows then
gives every console child (ffmpeg, ffprobe, the renderer) a brand new console
window: it flashes on screen, and can even fail with ERROR_NO_DATA (0x800700e8)
when the child inherits an invalid stdin handle. Every spawn goes through here
so the flag is set in one place.

All helpers forward to subprocess; only the defaults differ.
"""
from __future__ import annotations

import os
import subprocess

# CREATE_NO_WINDOW: the child runs without a console window at all.
CREATE_NO_WINDOW = 0x0800_0000
_FLAGS = CREATE_NO_WINDOW if os.name == "nt" else 0


def _prepare(kwargs: dict) -> dict:
    kwargs.setdefault("creationflags", _FLAGS)
    return kwargs


def run(cmd, **kwargs):
    # A windowed parent has no usable stdin, and a child that inherits the
    # invalid handle can fail with ERROR_NO_DATA (0x800700e8) before it does any
    # work. Helpers never read stdin, so give them an empty one. popen() is left
    # alone: the pipeline stages pass their stdin explicitly.
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, **_prepare(kwargs))


def check_output(cmd, **kwargs):
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.check_output(cmd, **_prepare(kwargs))


def popen(cmd, **kwargs):
    return subprocess.Popen(cmd, **_prepare(kwargs))
