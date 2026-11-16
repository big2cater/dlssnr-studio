"""Path resolution that works both from source and from a frozen exe.

When PyInstaller bundles the app, ``__file__`` points inside the temporary
extraction directory and ``sys.executable`` is our own exe. Everything heavy
(build/*.exe, runtime/, presets/, outputs/) stays next to the exe, so
that directory, not the bundle, is the project root. Small static assets (the
web page) are bundled and read from sys._MEIPASS.
"""
from __future__ import annotations

import pathlib
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> pathlib.Path:
    """Where bundled data files live (the _MEIPASS temp dir when frozen)."""
    return pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).resolve().parents[1]))


def project_root() -> pathlib.Path:
    """Directory holding build/, runtime/ and the other assets next to the exe."""
    if is_frozen():
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parents[1]


def presets_dir() -> pathlib.Path:
    """User presets next to the exe win; bundled copies are the fallback."""
    external = project_root() / "presets"
    if external.is_dir() and any(external.glob("*.json")):
        return external
    bundled = bundle_dir() / "presets"
    if bundled.is_dir():
        return bundled
    return external
