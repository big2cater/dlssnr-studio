// Enable DLSSNR-AMD's ACO (precompiled machine code) path for an offline host.
//
// Mirrors windows/src/pe/nr_pe_config.cpp (0.0.9: the same call, line ~1914):
//   nr::binary::configure(bundle, {bundle/shell, data/shaders, data/shaders/temporal},
//                         cache, aliases from shell-aliases.txt)
// It must run BEFORE nrvk::Context::create(), because Features::prepare()
// decides whether to request VK_KHR_pipeline_binary while building the device.
#pragma once

#include "nrvk.hpp"  // pulls in nr_pipeline_binary.hpp -> nr::binary::configure

#include <filesystem>
#include <fstream>
#include <map>
#include <string>
#include <vector>

inline bool enable_aco(const std::string& root, std::string* why = nullptr) {
    const std::filesystem::path data = std::filesystem::path(root) / "dlssnr-amd";
    const std::filesystem::path bundle = data / "aco";
    const std::string bundle_s = bundle.generic_string();
    const std::string shaders_s = (data / "shaders").generic_string();
    const std::string temporal_s = (data / "shaders" / "temporal").generic_string();
    const std::string cache_s = (data / "aco-cache").generic_string();

    if (!std::filesystem::is_directory(bundle / "records") ||
        !std::filesystem::is_directory(bundle / "shaders")) {
        if (why) *why = "ACO bundle incomplete (records/ or shaders/ missing): " + bundle_s;
        return false;
    }
    std::map<std::string, std::string> aliases;
    std::ifstream aliases_file(bundle / "shell-aliases.txt");
    for (std::string a, b; aliases_file >> a >> b;) aliases[a] = b;

    std::error_code ec;
    std::filesystem::create_directories(cache_s, ec);

    // For RDNA4 the native Windows SPIR-V act as templates; bundle/shell is
    // kept for parity with upstream (RDNA3's template set lives there).
    const size_t alias_count = aliases.size();
    nr::binary::configure(bundle_s,
                          {bundle_s + "/shell", shaders_s, temporal_s},
                          cache_s, std::move(aliases));
    if (why) *why = "ACO bundle " + bundle_s + ", cache " + cache_s +
                    ", " + std::to_string(alias_count) + " shell aliases";
    return true;
}
