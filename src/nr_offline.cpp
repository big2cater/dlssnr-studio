// nr_offline - offline image neural rendering on AMD, using DLSSNR-AMD's
// Vulkan network runtime directly instead of a game's swap chain.
//
// Front-end shape follows dlss5-visual-enhancer's VE_CLI (render / devices),
// while the back end is mochizuki0323/DLSSNR-AMD v0.0.8.1 windows/src/core.
//
//   nr_offline.exe render photo.png -o out.png --model small --intensity 1.0
//   nr_offline.exe devices
//
// No NVIDIA runtime, no CUDA, no NGX: the network is the Vulkan compute
// shaders shipped in the DLSSNR-AMD Windows package.
#define STB_IMAGE_IMPLEMENTATION
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include "stb_image.h"
#include "stb_image_write.h"

#include "nr_runtime.hpp"
#include "nrvk.hpp"
#include "nr_aco_setup.hpp"

#include <vulkan/vulkan.h>

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <vector>

using namespace nr;

namespace {

struct Options {
    std::string command = "render";
    std::string input, output, root, model = "small";
    float intensity = 1.0f, local_tone = 1.0f, local_structure = 1.0f, skin_structure = -1.0f;
    int style = 0, passes = 1;
    bool automatic_mask = true;
    bool aco = false;
    // Feed the runtime R8G8B8A8_UNORM directly instead of expanding to RGBA32F
    // on the CPU: a 4K frame is 33 MB instead of 132 MB, both ways.
    bool rgba8 = false;
};

Options parse(int argc, char** argv) {
    Options o;
    if (argc < 2) throw std::runtime_error("usage: nr_offline.exe render <input.png> -o <output.png> [options] | devices");
    o.command = argv[1];
    for (int i = 2; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) throw std::runtime_error("missing value after " + a);
            return argv[++i];
        };
        if (a == "-o" || a == "--output") o.output = next();
        else if (a == "--root") o.root = next();
        else if (a == "--model") o.model = next();
        else if (a == "--intensity") o.intensity = std::stof(next());
        else if (a == "--style") o.style = std::stoi(next());
        else if (a == "--passes") o.passes = std::stoi(next());
        else if (a == "--local-tone") o.local_tone = std::stof(next());
        else if (a == "--local-structure") o.local_structure = std::stof(next());
        else if (a == "--skin-structure") o.skin_structure = std::stof(next());
        else if (a == "--no-automask") o.automatic_mask = false;
        else if (a == "--aco") o.aco = true;
        else if (a == "--rgba8") o.rgba8 = true;
        else if (!a.empty() && a[0] != '-' && o.input.empty()) o.input = a;
        else throw std::runtime_error("unknown argument: " + a);
    }
    if (o.command == "render") {
        if (o.input.empty() || o.output.empty())
            throw std::runtime_error("render needs <input.png> and -o <output.png>");
        if (o.style < 0 || o.style > 2) throw std::runtime_error("--style must be 0, 1 or 2");
        if (o.passes < 1 || o.passes > 4) throw std::runtime_error("--passes must be 1..4");
    }
    return o;
}

std::string default_root(const char* argv0) {
    if (const char* env = std::getenv("NR_ROOT")) return env;
    std::filesystem::path p = std::filesystem::absolute(argv0).parent_path().parent_path() / "runtime";
    return p.string();
}

void vk_check(VkResult r, const char* what) {
    if (r != VK_SUCCESS) throw std::runtime_error(std::string(what) + ": VkResult=" + std::to_string(r));
}

uint32_t memory_type(const VkPhysicalDeviceMemoryProperties& mem, VkMemoryPropertyFlags props, uint32_t bits) {
    for (uint32_t i = 0; i < mem.memoryTypeCount; ++i)
        if ((bits & (1u << i)) && (mem.memoryTypes[i].propertyFlags & props) == props) return i;
    throw std::runtime_error("no matching Vulkan memory type");
}

struct Image {
    VkImage image{};
    VkDeviceMemory memory{};
    void destroy(VkDevice d) {
        if (image) vkDestroyImage(d, image, nullptr);
        if (memory) vkFreeMemory(d, memory, nullptr);
    }
};

struct Buffer {
    VkBuffer buffer{};
    VkDeviceMemory memory{};
    void* mapped{};
    void destroy(VkDevice d) {
        if (buffer) vkDestroyBuffer(d, buffer, nullptr);
        if (memory) vkFreeMemory(d, memory, nullptr);
    }
};

Buffer make_buffer(VkDevice d, const VkPhysicalDeviceMemoryProperties& mem, VkDeviceSize bytes, bool host,
                   bool cached = false) {
    Buffer b;
    VkBufferCreateInfo ci{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    ci.size = bytes; ci.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    ci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    vk_check(vkCreateBuffer(d, &ci, nullptr, &b.buffer), "vkCreateBuffer");
    VkMemoryRequirements req{};
    vkGetBufferMemoryRequirements(d, b.buffer, &req);
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = req.size;
    VkMemoryPropertyFlags props =
        host ? VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT
             : VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT;
    if (cached) props |= VK_MEMORY_PROPERTY_HOST_CACHED_BIT;
    try {
        ai.memoryTypeIndex = memory_type(mem, props, req.memoryTypeBits);
    } catch (const std::exception&) {
        // No cached host-visible heap on this device: the readback just stays
        // slower, which is what it was before.
        ai.memoryTypeIndex = memory_type(mem, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                              VK_MEMORY_PROPERTY_HOST_COHERENT_BIT, req.memoryTypeBits);
    }
    if (std::getenv("NR_IO_DIAG"))
        std::printf("buffer %s: %.1f MB type %u\n", cached ? "readback" : host ? "upload" : "device",
                    double(bytes) / 1048576.0, ai.memoryTypeIndex);
    vk_check(vkAllocateMemory(d, &ai, nullptr, &b.memory), "vkAllocateMemory(staging)");
    vk_check(vkBindBufferMemory(d, b.buffer, b.memory, 0), "vkBindBufferMemory");
    if (host) vk_check(vkMapMemory(d, b.memory, 0, bytes, 0, &b.mapped), "vkMapMemory");
    return b;
}

Image make_image(VkDevice d, const VkPhysicalDeviceMemoryProperties& mem, uint32_t w, uint32_t h,
                 VkFormat format) {
    Image im;
    VkImageCreateInfo ci{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ci.imageType = VK_IMAGE_TYPE_2D;
    ci.format = format;
    ci.extent = {w, h, 1};
    ci.mipLevels = 1; ci.arrayLayers = 1; ci.samples = VK_SAMPLE_COUNT_1_BIT;
    ci.tiling = VK_IMAGE_TILING_OPTIMAL;
    ci.usage = VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT |
               VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_STORAGE_BIT;
    ci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    ci.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    vk_check(vkCreateImage(d, &ci, nullptr, &im.image), "vkCreateImage");
    VkMemoryRequirements req{};
    vkGetImageMemoryRequirements(d, im.image, &req);
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = req.size;
    ai.memoryTypeIndex = memory_type(mem, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, req.memoryTypeBits);
    vk_check(vkAllocateMemory(d, &ai, nullptr, &im.memory), "vkAllocateMemory(image)");
    vk_check(vkBindImageMemory(d, im.image, im.memory, 0), "vkBindImageMemory");
    return im;
}

void barrier(VkCommandBuffer cmd, VkImage image, VkImageLayout from, VkImageLayout to,
             VkPipelineStageFlags src_stage, VkPipelineStageFlags dst_stage,
             VkAccessFlags src_access, VkAccessFlags dst_access) {
    VkImageMemoryBarrier b{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    b.oldLayout = from; b.newLayout = to;
    b.srcAccessMask = src_access; b.dstAccessMask = dst_access;
    b.image = image;
    b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    vkCmdPipelineBarrier(cmd, src_stage, dst_stage, 0, 0, nullptr, 0, nullptr, 1, &b);
}

int run_devices() {
    VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
    app.pApplicationName = "nr_offline"; app.apiVersion = VK_API_VERSION_1_3;
    VkInstanceCreateInfo ii{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    ii.pApplicationInfo = &app;
    VkInstance instance{};
    vk_check(vkCreateInstance(&ii, nullptr, &instance), "vkCreateInstance");
    uint32_t n = 0;
    vkEnumeratePhysicalDevices(instance, &n, nullptr);
    std::vector<VkPhysicalDevice> devs(n);
    vkEnumeratePhysicalDevices(instance, &n, devs.data());
    std::printf("index  vendor  device  name\n");
    for (uint32_t i = 0; i < n; ++i) {
        VkPhysicalDeviceProperties p{};
        vkGetPhysicalDeviceProperties(devs[i], &p);
        std::printf("%5u  0x%04x  0x%04x  %s\n", i, p.vendorID, p.deviceID, p.deviceName);
    }
    vkDestroyInstance(instance, nullptr);
    return 0;
}

int run_render(const Options& o, const std::string& root) {
    int w = 0, h = 0, channels = 0;
    stbi_uc* src = stbi_load(o.input.c_str(), &w, &h, &channels, 4);
    if (!src) throw std::runtime_error("cannot decode image: " + o.input);
    const size_t pixels = size_t(w) * size_t(h);
    const VkFormat frame_format = o.rgba8 ? VK_FORMAT_R8G8B8A8_UNORM : VK_FORMAT_R32G32B32A32_SFLOAT;
    const VkDeviceSize bytes = o.rgba8 ? VkDeviceSize(pixels) * 4
                                       : VkDeviceSize(pixels) * 4 * sizeof(float);
    // The float path is the only one that needs a 132 MB conversion buffer.
    std::vector<float> rgba;
    if (!o.rgba8) {
        rgba.resize(pixels * 4);
        for (size_t i = 0; i < pixels * 4; ++i) rgba[i] = float(src[i]) / 255.0f;
    }
    std::printf("input: %s (%dx%d)\n", o.input.c_str(), w, h);

    if (o.aco) {
        std::string msg;
        if (enable_aco(root, &msg)) {
            std::printf("aco: %s\n", msg.c_str());
        } else {
            // A missing or partial bundle must not abort the render: the LLPC
            // path is a little slower but produces the same picture. configure()
            // was never called, so nothing requests VK_KHR_pipeline_binary.
            std::printf("aco: %s -- falling back to LLPC\n", msg.c_str());
        }
    }

    nrvk::Context ctx;
    ctx.create();
    std::printf("gpu: %s | %s | %s\n", ctx.gpu_name.c_str(), ctx.driver_name.c_str(), ctx.target_name());
    const VkDevice d = ctx.device;

    HostDevice host{};
    host.instance = ctx.instance;
    host.physical = ctx.physical;
    host.device = d;
    host.queue = ctx.queue;
    host.queue_family = ctx.family;
    host.buffer_device_address = ctx.buffer_device_address;
    host.arch = ctx.arch;   // 0.0.9: GpuArch replaces the rdna3 flag

    RuntimeConfig cfg;
    cfg.root = root;
    cfg.width = uint32_t(w);
    cfg.height = uint32_t(h);
    cfg.colour_format = frame_format;
    // 1=CC_SMALL_FP8, 0=CC_SILVER_AARDWOLD, -1=read [Network] Model from dlssnr-amd.ini
    cfg.network = o.model == "small" ? 1 : o.model == "original" ? 0 : -1;
    std::printf("model: %s | root: %s\n", o.model.c_str(), root.c_str());

    // Runtime owns pipelines and pipeline caches, so it has to be destroyed
    // before the device. This scope makes that ordering explicit; the device is
    // torn down right after it (Context has no destructor of its own).
    {
    Runtime runtime(host, cfg);   // uploads weights, builds the pipeline chain
    std::printf("runtime built; recording one frame\n");

    Image image = make_image(d, ctx.mem, uint32_t(w), uint32_t(h), frame_format);
    Buffer upload = make_buffer(d, ctx.mem, bytes, true);
    // The CPU reads this back; without HOST_CACHED the driver hands back
    // write-combined memory, which reads at a few hundred MB/s.
    Buffer readback = make_buffer(d, ctx.mem, bytes, true, /*cached=*/true);
    std::memcpy(upload.mapped, o.rgba8 ? static_cast<const void*>(src) : static_cast<const void*>(rgba.data()),
                size_t(bytes));
    stbi_image_free(src);

    VkCommandPool pool{};
    VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    pci.queueFamilyIndex = ctx.family;
    vk_check(vkCreateCommandPool(d, &pci, nullptr, &pool), "vkCreateCommandPool");
    VkCommandBuffer cmd{};
    VkCommandBufferAllocateInfo cai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    cai.commandPool = pool; cai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY; cai.commandBufferCount = 1;
    vk_check(vkAllocateCommandBuffers(d, &cai, &cmd), "vkAllocateCommandBuffers");
    VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    vk_check(vkBeginCommandBuffer(cmd, &bi), "vkBeginCommandBuffer");

    barrier(cmd, image.image, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
            VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, VK_ACCESS_TRANSFER_WRITE_BIT);
    VkBufferImageCopy region{};
    region.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    region.imageExtent = {uint32_t(w), uint32_t(h), 1};
    vkCmdCopyBufferToImage(cmd, upload.buffer, image.image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region);
    barrier(cmd, image.image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL,
            VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
            VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT);

    ColourFrame frame{};
    frame.image = image.image;
    frame.format = frame_format;
    frame.width = uint32_t(w); frame.height = uint32_t(h);
    frame.before = frame.after = VK_IMAGE_LAYOUT_GENERAL;
    frame.usage = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_STORAGE_BIT |
                  VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;

    Controls controls;
    controls.intensity = o.intensity;
    controls.style = o.style;
    controls.passes = o.passes;
    controls.local_tone = o.local_tone;
    controls.local_structure = o.local_structure;
    controls.skin_structure = o.skin_structure;
    controls.automatic_mask = o.automatic_mask;
    controls.noise_every_frame = false;
    controls.seed_fixed = true;
    controls.feature = 1;

    const RecordResult result = runtime.record(cmd, frame, controls);
    std::printf("network dispatches: %u, applied: %s\n", result.network_dispatches, result.applied ? "yes" : "no");

    barrier(cmd, image.image, VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
            VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
    vkCmdCopyImageToBuffer(cmd, image.image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, readback.buffer, 1, &region);
    vk_check(vkEndCommandBuffer(cmd), "vkEndCommandBuffer");

    VkFence fence{};
    VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    vk_check(vkCreateFence(d, &fi, nullptr, &fence), "vkCreateFence");
    VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    si.commandBufferCount = 1; si.pCommandBuffers = &cmd;
    vk_check(vkQueueSubmit(ctx.queue, 1, &si, fence), "vkQueueSubmit");
    vk_check(vkWaitForFences(d, 1, &fence, VK_TRUE, 600000000000ull), "vkWaitForFences");

    // RGBA8 already holds the bytes the encoder wants; the float path converts.
    std::vector<stbi_uc> converted;
    const stbi_uc* out = static_cast<const stbi_uc*>(readback.mapped);
    if (!o.rgba8) {
        std::memcpy(rgba.data(), readback.mapped, size_t(bytes));
        converted.resize(pixels * 4);
        for (size_t i = 0; i < pixels * 4; ++i)
            converted[i] = stbi_uc(std::clamp(rgba[i], 0.0f, 1.0f) * 255.0f + 0.5f);
        out = converted.data();
    }
    // Choose the encoder from the output extension, like VE_CLI's export
    // settings. Anything unknown falls back to PNG (the lossless default).
    std::string ext = std::filesystem::path(o.output).extension().string();
    std::transform(ext.begin(), ext.end(), ext.begin(),
                   [](unsigned char c) { return char(std::tolower(c)); });
    int written = 0;
    if (ext == ".jpg" || ext == ".jpeg") written = stbi_write_jpg(o.output.c_str(), w, h, 4, out, 95);
    else if (ext == ".bmp") written = stbi_write_bmp(o.output.c_str(), w, h, 4, out);
    else written = stbi_write_png(o.output.c_str(), w, h, 4, out, w * 4);
    if (!written) throw std::runtime_error("cannot write image: " + o.output);
    std::printf("output: %s (%s)\n", o.output.c_str(), ext.empty() ? "png" : ext.c_str() + 1);

    vkDestroyFence(d, fence, nullptr);
    vkFreeCommandBuffers(d, pool, 1, &cmd);
    vkDestroyCommandPool(d, pool, nullptr);
    readback.destroy(d); upload.destroy(d); image.destroy(d);
    }   // Runtime gone: safe to drop the device now
    ctx.destroy();
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        const Options o = parse(argc, argv);
        if (o.command == "devices") return run_devices();
        if (o.command != "render") throw std::runtime_error("unknown command: " + o.command);
        return run_render(o, o.root.empty() ? default_root(argv[0]) : o.root);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "nr_offline: %s\n", e.what());
        return 1;
    }
}
