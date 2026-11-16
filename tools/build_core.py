"""Compile DLSSNR-AMD v0.0.9 windows/src/core into objects for the offline host.

Uses clang-cl (MSVC ABI) instead of upstream's mingw-w64 cross compiler.
The defines come from the vendored windows/build/arch/rdna4.sh, so the host
matches the prebuilt SPIR-V shipped in the 0.0.9 Windows package.

    python3 tools/build_core.py
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "vendor" / "DLSSNR-AMD-0.0.9"
OBJ = ROOT / "build" / "obj"
CLANG_CL = pathlib.Path(r"C:\Program Files\LLVM\bin\clang-cl.exe")
VULKAN_INCLUDE = pathlib.Path(r"C:\VulkanSDK\1.4.357.0\Include")

UNITS = [
    "nr_runtime.cpp",
    "nr_native_plan.cpp",
    "nr_renodx.cpp",
    "nr_wilsjo2.cpp",
]


def product_defines() -> list[str]:
    arch = (SRC / "windows/build/arch/rdna4.sh").read_text(encoding="utf-8")
    return ["/D" + k + "=" + v for k, v in re.findall(r"-D([A-Za-z0-9_]+)=([0-9]+)", arch)]


def common_flags() -> list[str]:
    return [
        "/std:c++17", "/O2", "/DNDEBUG", "/EHsc", "/W0",
        # 0.0.9 narrows a VkImageAspectFlagBits in a braced init (nr_renodx.cpp);
        # upstream builds it with g++ where this is tolerated, clang rejects it.
        "-Wno-c++11-narrowing",
        "/D_CRT_SECURE_NO_WARNINGS", "/DNR_INT4=1",
        '/DNR_BUILD_STAMP="offline-0.0.9"',
        "/I" + str(ROOT / "compat"),
        "/I" + str(VULKAN_INCLUDE),
        "/I" + str(SRC / "windows/src"),
        "/I" + str(SRC / "windows/src/core"),
        "/I" + str(SRC / "windows/src/pe"),
        "/I" + str(ROOT / "build/generated"),
        *product_defines(),
    ]


def main() -> int:
    OBJ.mkdir(parents=True, exist_ok=True)
    flags = common_flags()
    failed = []
    for unit in UNITS:
        source = SRC / "windows/src/core" / unit
        target = OBJ / (unit.replace(".cpp", ".obj"))
        cmd = [str(CLANG_CL), *flags, "/c", str(source), "/Fo:" + str(target)]
        result = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        status = "OK" if result.returncode == 0 else "FAIL"
        print(f"[{status}] {unit} -> {target.name} ({target.stat().st_size if target.exists() else 0} bytes)")
        if result.returncode:
            failed.append(unit)
            print("\n".join(result.stderr.splitlines()[:30]))
    print(f"defines: {len(product_defines())} from windows/build/arch/rdna4.sh")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
