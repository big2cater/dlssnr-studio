"""Build nr_stream.exe: the temporal frame-stream host.

    python3 tools/build_core.py && python3 tools/build_stream.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

from build_core import CLANG_CL, OBJ, ROOT, common_flags

HOST = ROOT / "src/nr_stream.cpp"
EXE = ROOT / "build/nr_stream.exe"
VULKAN_LIB = pathlib.Path(r"C:\VulkanSDK\1.4.357.0\Lib")

CORE_OBJECTS = ["nr_runtime.obj", "nr_native_plan.obj", "nr_renodx.obj", "nr_wilsjo2.obj"]


def main() -> int:
    host_obj = OBJ / "nr_stream.obj"
    r = subprocess.run(
        [str(CLANG_CL), *common_flags(), "/c", str(HOST), "/Fo:" + str(host_obj)],
        capture_output=True, text=True, errors="replace",
    )
    print(f"[{'OK' if r.returncode == 0 else 'FAIL'}] compile nr_stream.cpp")
    if r.returncode:
        print("\n".join(r.stderr.splitlines()[:40]))
        return 1

    objs = [str(OBJ / n) for n in CORE_OBJECTS] + [str(host_obj)]
    q = subprocess.run(
        [str(CLANG_CL), "/Fe:" + str(EXE), *objs, "/link",
         "/LIBPATH:" + str(VULKAN_LIB), "vulkan-1.lib", "advapi32.lib"],
        capture_output=True, text=True, errors="replace",
    )
    print(f"[{'OK' if q.returncode == 0 else 'FAIL'}] link nr_stream.exe")
    if q.returncode:
        print("\n".join((q.stdout + q.stderr).splitlines()[:60]))
        return 1
    print(f"built: {EXE} ({EXE.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
