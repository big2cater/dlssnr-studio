"""Build nr_offline.exe: the offline host + DLSSNR-AMD core objects.

    python3 tools/build_core.py && python3 tools/build_offline.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

from build_core import CLANG_CL, OBJ, ROOT, SRC, common_flags

HOST = ROOT / "src/nr_offline.cpp"
EXE = ROOT / "build/nr_offline.exe"
VULKAN_LIB = pathlib.Path(r"C:\VulkanSDK\1.4.357.0\Lib")

CORE_OBJECTS = [
    "nr_runtime.obj",
    "nr_native_plan.obj",
    "nr_renodx.obj",
    "nr_wilsjo2.obj",
]


def main() -> int:
    host_obj = OBJ / "nr_offline.obj"
    flags = common_flags() + ["/I" + str(ROOT / "third_party")]
    r = subprocess.run(
        [str(CLANG_CL), *flags, "/c", str(HOST), "/Fo:" + str(host_obj)],
        capture_output=True, text=True, errors="replace",
    )
    print(f"[{'OK' if r.returncode == 0 else 'FAIL'}] compile nr_offline.cpp")
    if r.returncode:
        print("\n".join(r.stderr.splitlines()[:40]))
        return 1

    objs = [str(OBJ / n) for n in CORE_OBJECTS] + [str(host_obj)]
    link = [
        str(CLANG_CL), "/Fe:" + str(EXE), *objs, "/link",
        "/LIBPATH:" + str(VULKAN_LIB), "vulkan-1.lib", "advapi32.lib",
    ]
    q = subprocess.run(link, capture_output=True, text=True, errors="replace")
    print(f"[{'OK' if q.returncode == 0 else 'FAIL'}] link nr_offline.exe")
    if q.returncode:
        print("\n".join((q.stdout + q.stderr).splitlines()[:60]))
        return 1
    print(f"built: {EXE} ({EXE.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
