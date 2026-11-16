// Minimal dirent.h compatibility shim for MSVC / clang-cl builds of
// DLSSNR-AMD's windows/src/core.
//
// Upstream telemetry.hpp only uses opendir/readdir/closedir to enumerate
// /sys/class/drm/cardN/device/hwmon on Linux. On Windows that tree does not
// exist, so Telemetry::find() must simply find nothing. Returning null from
// opendir() gives exactly the upstream behaviour (no telemetry) without
// touching the vendored sources.
#pragma once

#include <cstddef>

struct dirent {
    char d_name[260];
};

struct DIR {
    int unused;
};

inline DIR* opendir(const char*) { return nullptr; }
inline dirent* readdir(DIR*) { return nullptr; }
inline int closedir(DIR*) { return 0; }
