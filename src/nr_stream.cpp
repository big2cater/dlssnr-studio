// nr_stream - raw RGBA8 frame stream through DLSSNR-AMD's temporal NR path.
//
// stdin  : W*H*4 bytes per frame, repeated until EOF (binary)
// stdout : W*H*4 bytes per frame, same order (binary, via a saved fd)
// stderr : all diagnostics, including the upstream runtime's own prints
//
//   nr_stream.exe --width 512 --height 288 --model small --intensity 1.0
//
// Stays separate from nr_offline.exe on purpose: the single-frame host is
// already validated, this one adds TemporalConfig + record_temporal so a video
// gets motion estimation and history blending instead of independent frames.
#include "nr_runtime.hpp"
#include "nrvk.hpp"
#include "nr_aco_setup.hpp"

#include <vulkan/vulkan.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <io.h>
#include <fcntl.h>
#ifdef _WIN32
#include <windows.h>
#endif
#include <stdexcept>
#include <string>
#include <vector>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <deque>

using namespace nr;

namespace {

struct Options {
    uint32_t width = 0, height = 0;
    std::string root, model = "small";
    float intensity = 1.0f, local_tone = 1.0f, local_structure = 1.0f, skin_structure = -1.0f;
    int style = 0, passes = 1;
    bool automatic_mask = true;
    bool temporal = true;
    // The DLL advances its pre-block noise seed every frame. That shimmers on
    // a still picture unless temporal history averages it out, so it belongs
    // to video semantics and stays off for the single-frame host.
    bool noise_every_frame = false;
    bool aco = false;
    uint32_t max_frames = 0;
    // Progress lines every N frames. 30 matches upstream's status cadence and is
    // far too coarse for a 48-frame 4K clip, so callers that show a progress bar
    // ask for a smaller step.
    uint32_t progress_every = 30;
    // Frames recorded into one command buffer / one submit. 1 keeps the
    // original one-frame-one-fence behaviour.
    uint32_t batch = 1;
    // A reader thread pulls from the decoder and a writer thread pushes to the
    // encoder, so those two waits overlap the GPU work instead of adding to it.
    bool no_pipeline = false;
    bool io_bench = false;
    // Feed RGBA8 straight into the runtime (it accepts R8G8B8A8_UNORM) instead
    // of expanding to RGBA32F on the CPU. Cuts 4x the staging traffic and all
    // per-pixel float conversions.
    bool rgba8 = false;
};

Options parse(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) throw std::runtime_error("missing value after " + a);
            return argv[++i];
        };
        if (a == "--width") o.width = std::stoul(next());
        else if (a == "--height") o.height = std::stoul(next());
        else if (a == "--root") o.root = next();
        else if (a == "--model") o.model = next();
        else if (a == "--intensity") o.intensity = std::stof(next());
        else if (a == "--style") o.style = std::stoi(next());
        else if (a == "--passes") o.passes = std::stoi(next());
        else if (a == "--local-tone") o.local_tone = std::stof(next());
        else if (a == "--local-structure") o.local_structure = std::stof(next());
        else if (a == "--skin-structure") o.skin_structure = std::stof(next());
        else if (a == "--max-frames") o.max_frames = std::stoul(next());
        else if (a == "--no-automask") o.automatic_mask = false;
        else if (a == "--no-temporal") o.temporal = false;
        else if (a == "--noise-every-frame") o.noise_every_frame = true;
        else if (a == "--aco") o.aco = true;
        else if (a == "--batch") o.batch = std::stoul(next());
        else if (a == "--progress-every") o.progress_every = std::stoul(next());
        else if (a == "--no-pipeline") o.no_pipeline = true;
        else if (a == "--rgba8") o.rgba8 = true;
        else if (a == "--io-bench") o.io_bench = true;
        else throw std::runtime_error("unknown argument: " + a);
    }
    if (!o.width || !o.height) throw std::runtime_error("--width and --height are required");
    if (o.style < 0 || o.style > 2) throw std::runtime_error("--style must be 0, 1 or 2");
    if (o.passes < 1 || o.passes > 4) throw std::runtime_error("--passes must be 1..4");
    if (o.batch < 1 || o.batch > 32) throw std::runtime_error("--batch must be 1..32");
    if (o.progress_every < 1) throw std::runtime_error("--progress-every must be >= 1");
    return o;
}

std::string default_root(const char* argv0) {
    if (const char* env = std::getenv("NR_ROOT")) return env;
    return (std::filesystem::absolute(argv0).parent_path().parent_path() / "runtime").string();
}

void vk_check(VkResult r, const char* what) {
    if (r != VK_SUCCESS) throw std::runtime_error(std::string(what) + ": VkResult=" + std::to_string(r));
}

uint32_t memory_type(const VkPhysicalDeviceMemoryProperties& mem, VkMemoryPropertyFlags props, uint32_t bits) {
    for (uint32_t i = 0; i < mem.memoryTypeCount; ++i)
        if ((bits & (1u << i)) && (mem.memoryTypes[i].propertyFlags & props) == props) return i;
    throw std::runtime_error("no matching Vulkan memory type");
}

struct Staging {
    VkBuffer buffer{};
    VkDeviceMemory memory{};
    void* mapped{};
    void destroy(VkDevice d) {
        if (buffer) vkDestroyBuffer(d, buffer, nullptr);
        if (memory) vkFreeMemory(d, memory, nullptr);
    }
};

struct Resource {
    VkImage image{};
    VkDeviceMemory image_memory{};
    Staging upload{}, readback{};
    void destroy(VkDevice d) {
        upload.destroy(d);
        readback.destroy(d);
        if (image) vkDestroyImage(d, image, nullptr);
        if (image_memory) vkFreeMemory(d, image_memory, nullptr);
    }
};

// `cached` matters more than it looks: the mapped pointer is read by the CPU.
// Without HOST_CACHED the driver may hand back write-combined memory, which is
// great for streaming writes but reads back at a few hundred MB/s. At 4K a
// frame is 33 MB, so streaming straight out of uncached memory made a 1.5 GB
// readout take ~8 s. Upload wants write-combined; readback wants cached.
Staging make_staging(VkDevice d, const VkPhysicalDeviceMemoryProperties& mem, VkDeviceSize bytes,
                     bool cached) {
    Staging s;
    VkBufferCreateInfo bi{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bi.size = bytes;
    bi.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    bi.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    vk_check(vkCreateBuffer(d, &bi, nullptr, &s.buffer), "vkCreateBuffer");
    VkMemoryRequirements need{};
    vkGetBufferMemoryRequirements(d, s.buffer, &need);
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = need.size;
    VkMemoryPropertyFlags props = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                  VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    if (cached) props |= VK_MEMORY_PROPERTY_HOST_CACHED_BIT;
    uint32_t type = 0;
    try {
        type = memory_type(mem, props, need.memoryTypeBits);
    } catch (const std::exception&) {
        // No cached host-visible type on this device: fall back to plain
        // coherent memory and keep working, just slower.
        type = memory_type(mem, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                VK_MEMORY_PROPERTY_HOST_COHERENT_BIT, need.memoryTypeBits);
    }
    ai.memoryTypeIndex = type;
    if (std::getenv("NR_IO_DIAG"))
        std::fprintf(stderr, "staging %s: %.1f MB type %u props 0x%x\n",
                     cached ? "readback" : "upload", double(bytes) / (1024.0 * 1024.0), type,
                     unsigned(mem.memoryTypes[type].propertyFlags));
    vk_check(vkAllocateMemory(d, &ai, nullptr, &s.memory), "vkAllocateMemory(staging)");
    vk_check(vkBindBufferMemory(d, s.buffer, s.memory, 0), "vkBindBufferMemory");
    vk_check(vkMapMemory(d, s.memory, 0, bytes, 0, &s.mapped), "vkMapMemory");
    return s;
}

Resource make_resources(VkDevice d, const VkPhysicalDeviceMemoryProperties& mem,
                        uint32_t w, uint32_t h, VkDeviceSize frame_bytes, uint32_t readback_slots,
                        uint32_t upload_slots, VkFormat format) {
    Resource r;
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
    vk_check(vkCreateImage(d, &ci, nullptr, &r.image), "vkCreateImage");
    VkMemoryRequirements ri{};
    vkGetImageMemoryRequirements(d, r.image, &ri);
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = ri.size;
    ai.memoryTypeIndex = memory_type(mem, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, ri.memoryTypeBits);
    vk_check(vkAllocateMemory(d, &ai, nullptr, &r.image_memory), "vkAllocateMemory(image)");
    vk_check(vkBindImageMemory(d, r.image, r.image_memory, 0), "vkBindImageMemory");

    // One host-visible buffer per direction. Upload needs as many regions as a
    // batch records before its single submit (exactly one in the pipelined,
    // one-frame-per-fence path, where the buffer is safe to reuse as soon as the
    // fence clears). Readback needs one region per in-flight frame, because the
    // writer thread may still be draining an older one.
    r.upload = make_staging(d, mem, frame_bytes * VkDeviceSize(upload_slots), false);
    r.readback = make_staging(d, mem, frame_bytes * VkDeviceSize(readback_slots), true);
    return r;
}

void barrier(VkCommandBuffer cmd, VkImage image, VkImageLayout from, VkImageLayout to,
             VkPipelineStageFlags src_stage, VkPipelineStageFlags dst_stage,
             VkAccessFlags src_access, VkAccessFlags dst_access) {
    VkImageMemoryBarrier bar{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    bar.oldLayout = from; bar.newLayout = to;
    bar.srcAccessMask = src_access; bar.dstAccessMask = dst_access;
    bar.image = image;
    bar.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    vkCmdPipelineBarrier(cmd, src_stage, dst_stage, 0, 0, nullptr, 0, nullptr, 1, &bar);
}

#ifndef _WIN32
#error "nr_stream currently targets Windows (Win32 ReadFile/WriteFile + Vulkan SDK layout)"
#endif

// The CRT's _read/_write go through its own per-call path and, on a Windows
// pipe, end up issuing many small transfers. A 4K RGBA8 frame is 33 MB, so at
// 4K that dominates everything (measured: 1.5 GB through the CRT took 15 s on
// stdout, 98 s on stdin versus 2.6 s straight from a file). Talk to the kernel
// object directly instead, and only fall back to the CRT elsewhere.
#ifdef _WIN32
// Chunk size for pipe transfers. A whole 4K frame is 33 MB; handing the kernel
// one giant WriteFile makes it fall back to tiny internal transfers, so keep
// requests at a size the pipe can move in bulk. NR_IO_CHUNK overrides it for
// experiments.
size_t io_chunk() {
    static const size_t chunk = [] {
        if (const char* v = std::getenv("NR_IO_CHUNK")) {
            const long n = std::strtol(v, nullptr, 10);
            if (n > 0) return size_t(n);
        }
        return size_t(256) << 10;
    }();
    return chunk;
}

size_t read_exact(int fd, uint8_t* dst, size_t bytes) {
    const HANDLE h = reinterpret_cast<HANDLE>(_get_osfhandle(fd));
    const size_t chunk = io_chunk();
    size_t done = 0;
    while (done < bytes) {
        DWORD want = DWORD(std::min(bytes - done, chunk));
        DWORD got = 0;
        if (!ReadFile(h, dst + done, want, &got, nullptr) || got == 0) break;
        done += size_t(got);
    }
    return done;
}

void write_exact(int fd, const uint8_t* src, size_t bytes) {
    const HANDLE h = reinterpret_cast<HANDLE>(_get_osfhandle(fd));
    const size_t chunk = io_chunk();
    size_t done = 0;
    while (done < bytes) {
        DWORD want = DWORD(std::min(bytes - done, chunk));
        DWORD put = 0;
        if (!WriteFile(h, src + done, want, &put, nullptr) || put == 0)
            throw std::runtime_error("failed writing frame stream");
        done += size_t(put);
    }
}
#endif

// Pure I/O probe: copies frames from stdin to stdout with no Vulkan involved.
// Used to separate pipe cost from GPU cost when a resolution gets slow.
int io_bench(const Options& o, int out_fd) {
    const size_t frame_bytes = size_t(o.width) * size_t(o.height) * 4;
    std::vector<uint8_t> buf(frame_bytes);
    uint32_t frames = 0;
    const auto t0 = std::chrono::steady_clock::now();
    for (;;) {
        if (o.max_frames && frames >= o.max_frames) break;
        if (read_exact(_fileno(stdin), buf.data(), frame_bytes) != frame_bytes) break;
        write_exact(out_fd, buf.data(), frame_bytes);
        ++frames;
    }
    const double secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    const double mb = double(frames) * double(frame_bytes) / (1024.0 * 1024.0);
    std::fprintf(stderr, "io-bench: %u frames, %.1f MB in, %.2f s (%.0f MB/s)\n",
                 frames, mb, secs, secs > 0 ? mb / secs : 0.0);
    return 0;
}

int run(const Options& o, const std::string& root, int out_fd) {
    const uint32_t w = o.width, h = o.height;
    const size_t pixels = size_t(w) * size_t(h);
    const size_t frame_bytes = pixels * 4;   // RGBA8 on the wire
    const VkFormat image_format = o.rgba8 ? VK_FORMAT_R8G8B8A8_UNORM
                                          : VK_FORMAT_R32G32B32A32_SFLOAT;
    const VkDeviceSize gpu_bytes = o.rgba8 ? VkDeviceSize(frame_bytes)
                                           : VkDeviceSize(pixels) * 4 * sizeof(float);

    // Where the startup seconds go. A 48-frame 4K clip spends more time getting
    // ready than rendering, so this stays available behind an env var.
    const auto t_start = std::chrono::steady_clock::now();
    auto last_mark = t_start;
    const bool start_diag = std::getenv("NR_STARTUP_DIAG") != nullptr;
    bool first_frame_marked = false;
    const auto mark = [&](const char* what) {
        if (!start_diag) return;
        const auto now = std::chrono::steady_clock::now();
        std::fprintf(stderr, "[startup] %-12s %7.1f ms   total %7.1f ms\n", what,
                     std::chrono::duration<double, std::milli>(now - last_mark).count(),
                     std::chrono::duration<double, std::milli>(now - t_start).count());
        last_mark = now;
    };

    if (o.aco) {
        std::string msg;
        if (enable_aco(root, &msg)) {
            std::fprintf(stderr, "aco: %s\n", msg.c_str());
        } else {
            std::fprintf(stderr, "aco: %s -- falling back to LLPC\n", msg.c_str());
        }
    }

    mark("aco");
    nrvk::Context ctx;
    ctx.create();
    mark("context");
    std::fprintf(stderr, "gpu: %s | %s | %s\n", ctx.gpu_name.c_str(), ctx.driver_name.c_str(), ctx.target_name());
    const VkDevice d = ctx.device;

    HostDevice host{};
    host.instance = ctx.instance; host.physical = ctx.physical; host.device = d;
    host.queue = ctx.queue; host.queue_family = ctx.family;
    host.buffer_device_address = ctx.buffer_device_address; host.arch = ctx.arch;   // 0.0.9: GpuArch replaces the rdna3 flag

    RuntimeConfig cfg;
    cfg.root = root; cfg.width = w; cfg.height = h;
    cfg.colour_format = image_format;
    cfg.network = o.model == "small" ? 1 : o.model == "original" ? 0 : -1;

    TemporalConfig temporal;
    temporal.enable = o.temporal;
    temporal.history_strength = 1.0f;
    std::fprintf(stderr, "stream: %ux%u model=%s temporal=%s batch=%u format=%s root=%s\n",
                 w, h, o.model.c_str(), o.temporal ? "on" : "off", o.batch,
                 o.rgba8 ? "RGBA8_UNORM" : "RGBA32F", root.c_str());

    Runtime runtime(host, cfg, ControlMaskConfig{}, temporal);
    mark("runtime");

    // Only the straight-through 8-bit single-frame path is pipelined. batch>1
    // and the float path keep the already-validated serial loop; --no-pipeline
    // is the A/B switch.
    constexpr uint32_t kPipelineSlots = 3;
    // rgba8 is not optional here: the writer copies readback bytes straight out,
    // with no float->u8 conversion, so a 32F frame would be written as garbage.
    const bool use_pipeline = !o.no_pipeline && o.rgba8 && o.batch == 1 && !o.io_bench;
    const uint32_t staging_slots = use_pipeline ? kPipelineSlots : o.batch;
    std::fprintf(stderr, "pipeline: %s (slots=%u)\n", use_pipeline ? "on" : "off", staging_slots);
    // Upload: one region is enough once batch==1; readback needs one per slot.
    const uint32_t upload_slots = use_pipeline ? 1u : o.batch;
    Resource res = make_resources(d, ctx.mem, w, h, gpu_bytes, staging_slots, upload_slots,
                                  image_format);
    mark("resources");
    VkCommandPool pool{};
    VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    pci.queueFamilyIndex = ctx.family;
    pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    vk_check(vkCreateCommandPool(d, &pci, nullptr, &pool), "vkCreateCommandPool");
    VkCommandBuffer cmd{};
    VkCommandBufferAllocateInfo cai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    cai.commandPool = pool; cai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY; cai.commandBufferCount = 1;
    vk_check(vkAllocateCommandBuffers(d, &cai, &cmd), "vkAllocateCommandBuffers");
    VkFence fence{};
    VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    vk_check(vkCreateFence(d, &fi, nullptr, &fence), "vkCreateFence");

    mark("commands");
    ColourFrame frame{};
    frame.image = res.image;
    frame.format = image_format;
    frame.width = w; frame.height = h;
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
    controls.noise_every_frame = o.noise_every_frame;
    controls.seed_fixed = !o.noise_every_frame;
    controls.feature = 1;

    uint32_t index = 0, submits = 0;
    const uint32_t batch = o.batch;

    // Wall-clock cost of one iteration of the frame loop, kept per phase so a
    // slow stage can be named instead of guessed at. The GPU timestamp pair
    // (last_gpu_ms) only covers the compute in the middle.
    constexpr uint32_t kWallWindow = 24;   // local structs cannot hold static members
    struct WallProbe {
        double read{}, gpu{}, write{};
        uint64_t frames{};
        double per_frame() const { return frames ? (read + gpu + write) / double(frames) : 0.0; }

        // Rolling window for the live number. The first frames carry the graph
        // build, so a cumulative average would read several times too high for
        // the first second and then crawl back down.
        double recent[kWallWindow]{};
        uint32_t slot{}, filled{};
        void push(double ms) {
            recent[slot] = ms;
            slot = (slot + 1) % kWallWindow;
            if (filled < kWallWindow) ++filled;
        }
        double live() const {
            if (!filled) return 0.0;
            double sum = 0.0;
            for (uint32_t i = 0; i < filled; ++i) sum += recent[i];
            return sum / double(filled);
        }
    } wall_probe;
    if (use_pipeline) {
        // Three frames can be in flight: the reader pulls from the decoder, the
        // main thread drives the GPU one frame at a time (single queue, single
        // fence -- temporal history must stay strictly ordered), and the writer
        // drains finished frames to the encoder. A readback slot is recycled
        // only after the writer has copied it out, so no frame is overwritten.
        struct PipeQ {
            std::mutex m;
            std::condition_variable cv;
            std::deque<std::vector<uint8_t>> ready, spare;
            std::deque<uint32_t> out_slots, free_slots;
            bool input_done = false, stop = false;
        };
        // Shared: the reader is detached so a blocking read can never keep the
        // process alive when --max-frames stops the loop early. It only touches
        // stdin and this queue, never any Vulkan object.
        auto q = std::make_shared<PipeQ>();
        for (uint32_t i = 0; i < kPipelineSlots; ++i) {
            q->spare.emplace_back(frame_bytes);
            q->free_slots.push_back(i);
        }
        const int in_fd = _fileno(stdin);

        std::thread reader([q, in_fd, frame_bytes] {   // detached: no dangling references
            for (;;) {
                std::vector<uint8_t> buf;
                {
                    std::unique_lock<std::mutex> lk(q->m);
                    q->cv.wait(lk, [&, q] { return q->stop || !q->spare.empty(); });
                    if (q->stop) return;
                    buf = std::move(q->spare.front());
                    q->spare.pop_front();
                }
                // A blocking read is fine here: this thread is detached, so a
                // producer that never finishes cannot hold up shutdown. Polling
                // with PeekNamedPipe instead cost ~15ms per frame on Windows
                // (the 1ms sleep rounds up to a timer tick) and made small
                // clips nearly twice as slow.
                if (read_exact(in_fd, buf.data(), frame_bytes) != frame_bytes) {
                    std::lock_guard<std::mutex> lk(q->m);
                    q->input_done = true;
                    q->cv.notify_all();
                    return;
                }
                {
                    std::lock_guard<std::mutex> lk(q->m);
                    q->ready.push_back(std::move(buf));
                    q->cv.notify_all();
                }
            }
        });

        // A write failure (the encoder died, the disk filled up) must not escape
        // a thread: an uncaught exception there calls std::terminate, which looks
        // like a random crash and prints nothing useful.
        std::atomic<bool> write_failed{false};
        std::string write_error;
        std::thread writer([&, q] {
            try {
            for (;;) {
                uint32_t slot = 0;
                {
                    std::unique_lock<std::mutex> lk(q->m);
                    q->cv.wait(lk, [&, q] { return !q->out_slots.empty() || q->stop; });
                    if (q->out_slots.empty()) return;   // stopped and drained
                    slot = q->out_slots.front();
                    q->out_slots.pop_front();
                }
                const uint8_t* src = static_cast<const uint8_t*>(res.readback.mapped)
                                     + size_t(slot) * size_t(gpu_bytes);
                write_exact(out_fd, src, frame_bytes);
                {
                    std::lock_guard<std::mutex> lk(q->m);
                    q->free_slots.push_back(slot);
                    q->cv.notify_all();
                }
            }
            } catch (const std::exception& e) {
                std::lock_guard<std::mutex> lk(q->m);
                write_error = e.what();
                write_failed = true;
                q->stop = true;
                q->cv.notify_all();
            }
        });

        reader.detach();

        using clk = std::chrono::steady_clock;
        auto prev_done = clk::now();
        bool have_prev = false;

        for (;;) {
            if (o.max_frames && index >= o.max_frames) break;
            std::vector<uint8_t> in;
            uint32_t slot = 0;
            {
                std::unique_lock<std::mutex> lk(q->m);
                q->cv.wait(lk, [&, q] { return !q->ready.empty() || q->input_done || q->stop; });
                if (q->ready.empty()) break;
                in = std::move(q->ready.front());
                q->ready.pop_front();
                q->cv.notify_all();
            }
            {
                std::unique_lock<std::mutex> lk(q->m);
                q->cv.wait(lk, [&, q] { return !q->free_slots.empty() || q->stop; });
                if (q->free_slots.empty()) break;   // writer failed: stop cleanly
                slot = q->free_slots.front();
                q->free_slots.pop_front();
            }

            const auto t_begin = clk::now();
            std::memcpy(static_cast<uint8_t*>(res.upload.mapped), in.data(), frame_bytes);
            {
                std::lock_guard<std::mutex> lk(q->m);
                q->spare.push_back(std::move(in));
                q->cv.notify_all();
            }
            const auto t_read = clk::now();

            vk_check(vkResetCommandBuffer(cmd, 0), "vkResetCommandBuffer");
            VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
            bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
            vk_check(vkBeginCommandBuffer(cmd, &bi), "vkBeginCommandBuffer");

            bool applied = false;
            uint32_t dispatches = 0;
            VkBufferImageCopy region{};
            region.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
            region.imageExtent = {w, h, 1};
            region.bufferOffset = 0;   // single upload region, reused every frame
            // UNDEFINED is deliberate: res.image is a per-frame transfer target
            // that this copy fully overwrites. Temporal history lives in the
            // Runtime's own attachments, so nothing is lost here.
            barrier(cmd, res.image, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                    VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                    0, VK_ACCESS_TRANSFER_WRITE_BIT);
            vkCmdCopyBufferToImage(cmd, res.upload.buffer, res.image,
                                   VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region);
            barrier(cmd, res.image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL,
                    VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                    VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT);

            TemporalFrame tf;
            tf.reset = index == 0;
            tf.feature = 1;
            if (o.temporal) {
                const TemporalResult tr = runtime.record_temporal(cmd, frame, controls, tf);
                applied = tr.frame.applied;
                dispatches = tr.frame.network_dispatches;
                if (index < 3)
                    std::fprintf(stderr, "frame %u: history=%s motion=%s\n", index,
                                 tr.history_consumed ? "yes" : "no", tr.motion_provider);
            } else {
                const RecordResult rr = runtime.record(cmd, frame, controls);
                applied = rr.applied;
                dispatches = rr.network_dispatches;
            }
            barrier(cmd, res.image, VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                    VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                    VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
            region.bufferOffset = VkDeviceSize(slot) * gpu_bytes;   // readback: one per slot
            vkCmdCopyImageToBuffer(cmd, res.image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                                   res.readback.buffer, 1, &region);
            vk_check(vkEndCommandBuffer(cmd), "vkEndCommandBuffer");

            vk_check(vkResetFences(d, 1, &fence), "vkResetFences");
            VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
            si.commandBufferCount = 1; si.pCommandBuffers = &cmd;
            vk_check(vkQueueSubmit(ctx.queue, 1, &si, fence), "vkQueueSubmit");
            vk_check(vkWaitForFences(d, 1, &fence, VK_TRUE, 600000000000ull), "vkWaitForFences");
            const auto t_gpu = clk::now();
            if (!first_frame_marked) { mark("first-frame"); first_frame_marked = true; }
            {
                std::lock_guard<std::mutex> lk(q->m);
                q->out_slots.push_back(slot);   // FIFO: output order == input order
                q->cv.notify_all();
            }

            // With I/O off the critical path, the honest per-frame cost is the
            // interval between completed frames, not the sum of main-thread
            // phases (the writer is running concurrently with the next frame).
            const auto ms = [](clk::time_point a, clk::time_point b) {
                return std::chrono::duration<double, std::milli>(b - a).count();
            };
            const double r = ms(t_begin, t_read), g = ms(t_read, t_gpu);
            wall_probe.read += r;
            wall_probe.gpu += g;
            wall_probe.frames += 1;
            // Skip the first few intervals: they contain the graph build and
            // would sit in the rolling window for the whole clip.
            if (have_prev && index >= 10) wall_probe.push(ms(prev_done, t_gpu));
            prev_done = t_gpu;
            have_prev = true;

            ++index;
            ++submits;
            if (index % o.progress_every == 0) {
                const float gpu_ms = runtime.last_gpu_ms();
                const float avg_ms = runtime.average_gpu_ms();
                const double wall = wall_probe.live();
                if (gpu_ms > 0.0f)
                    std::fprintf(stderr, "frame %u: dispatches=%u applied=%s gpu=%.2fms avg=%.2fms "
                                         "wall=%.2fms\n",
                                 index - 1, dispatches, applied ? "yes" : "no", gpu_ms, avg_ms, wall);
                else
                    std::fprintf(stderr, "frame %u: dispatches=%u applied=%s wall=%.2fms\n",
                                 index - 1, dispatches, applied ? "yes" : "no", wall);
            }
        }

        {
            std::lock_guard<std::mutex> lk(q->m);
            q->stop = true;
            q->cv.notify_all();
        }
        // join only the writer: every queued frame must reach the encoder. The
        // reader is detached; a blocked read must not hold up shutdown.
        writer.join();
        if (write_failed) {
            std::fprintf(stderr, "stream: %s\n", write_error.c_str());
            return 1;
        }
    } else {
    // Only the serial loop needs these; at 4K they are 31.6 MB + 31.6 MB + 126 MB,
    // which the pipelined path would otherwise allocate and never touch.
    std::vector<uint8_t> in_frame(frame_bytes), out_frame(frame_bytes);
    std::vector<float> rgba(pixels * 4);
    for (;;) {
        if (o.max_frames && index >= o.max_frames) break;

        // Fill one batch of upload regions before recording anything.
        uint32_t count = 0;
        const auto t_begin = std::chrono::steady_clock::now();
        for (; count < batch; ++count) {
            if (o.max_frames && index + count >= o.max_frames) break;
            const size_t got = read_exact(_fileno(stdin), in_frame.data(), frame_bytes);
            if (got == 0) break;
            if (got != frame_bytes) {
                std::fprintf(stderr, "trailing partial frame (%zu/%zu bytes), ignored\n", got, frame_bytes);
                break;
            }
            uint8_t* dst = static_cast<uint8_t*>(res.upload.mapped)
                           + size_t(count) * size_t(gpu_bytes);
            // Reading the pipe straight into the staged buffer looks tempting but
            // is slower: that memory is write-combined, so the CPU's writes crawl
            // (~12.6ms vs 9.4ms for 4K). A heap read plus memcpy wins.
            if (o.rgba8) {
                std::memcpy(dst, in_frame.data(), frame_bytes);
            } else {
                for (size_t i = 0; i < pixels * 4; ++i) rgba[i] = float(in_frame[i]) / 255.0f;
                std::memcpy(dst, rgba.data(), size_t(gpu_bytes));
            }
        }
        if (count == 0) break;

        const auto t_read = std::chrono::steady_clock::now();
        vk_check(vkResetCommandBuffer(cmd, 0), "vkResetCommandBuffer");
        VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
        bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        vk_check(vkBeginCommandBuffer(cmd, &bi), "vkBeginCommandBuffer");

        bool applied = false;
        uint32_t dispatches = 0;
        for (uint32_t s = 0; s < count; ++s) {
            VkBufferImageCopy region{};
            region.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
            region.imageExtent = {w, h, 1};
            region.bufferOffset = VkDeviceSize(s) * gpu_bytes;

            // UNDEFINED is a legal oldLayout: the frame is fully overwritten,
            // so the previous frame's contents are intentionally discarded.
            barrier(cmd, res.image, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                    VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                    0, VK_ACCESS_TRANSFER_WRITE_BIT);
            vkCmdCopyBufferToImage(cmd, res.upload.buffer, res.image,
                                   VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region);
            barrier(cmd, res.image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL,
                    VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                    VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT);

            TemporalFrame tf;
            tf.reset = index + s == 0;   // only the very first frame of the stream
            tf.feature = 1;
            if (o.temporal) {
                const TemporalResult tr = runtime.record_temporal(cmd, frame, controls, tf);
                applied = tr.frame.applied;
                dispatches = tr.frame.network_dispatches;
                if (index + s < 3)
                    std::fprintf(stderr, "frame %u: history=%s motion=%s\n", index + s,
                                 tr.history_consumed ? "yes" : "no", tr.motion_provider);
            } else {
                const RecordResult rr = runtime.record(cmd, frame, controls);
                applied = rr.applied;
                dispatches = rr.network_dispatches;
            }

            region.bufferOffset = VkDeviceSize(s) * gpu_bytes;
            barrier(cmd, res.image, VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                    VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                    VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
            vkCmdCopyImageToBuffer(cmd, res.image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                                   res.readback.buffer, 1, &region);
        }
        vk_check(vkEndCommandBuffer(cmd), "vkEndCommandBuffer");

        // One submit and one fence for the whole batch: the GPU runs the frames
        // back-to-back instead of idling behind a per-frame CPU round trip.
        vk_check(vkResetFences(d, 1, &fence), "vkResetFences");
        VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
        si.commandBufferCount = 1; si.pCommandBuffers = &cmd;
        vk_check(vkQueueSubmit(ctx.queue, 1, &si, fence), "vkQueueSubmit");
        vk_check(vkWaitForFences(d, 1, &fence, VK_TRUE, 600000000000ull), "vkWaitForFences");
        const auto t_gpu = std::chrono::steady_clock::now();
        if (!first_frame_marked) { mark("first-frame"); first_frame_marked = true; }

        for (uint32_t s = 0; s < count; ++s) {
            const uint8_t* src = static_cast<const uint8_t*>(res.readback.mapped)
                                 + size_t(s) * size_t(gpu_bytes);
            const uint8_t* bytes = src;
            if (!o.rgba8) {
                std::memcpy(rgba.data(), src, size_t(gpu_bytes));
                for (size_t i = 0; i < pixels * 4; ++i)
                    out_frame[i] = uint8_t(std::clamp(rgba[i], 0.0f, 1.0f) * 255.0f + 0.5f);
                bytes = out_frame.data();
            }
            write_exact(out_fd, bytes, frame_bytes);
        }
        const auto t_written = std::chrono::steady_clock::now();

        {
            // Wall-clock split for the frames just processed. gpu_ms above is the
            // GPU's own timestamp pair, which excludes everything this loop does
            // around it: waiting for the decoder to fill the pipe, the upload
            // memcpy, the fence wait on the CPU, and waiting for the encoder to
            // drain. Those four are serialised here, so the honest per-frame cost
            // is their sum, not the GPU number.
            using clk = std::chrono::steady_clock;
            const auto ms = [](clk::time_point a, clk::time_point b) {
                return std::chrono::duration<double, std::milli>(b - a).count();
            };
            const double r = ms(t_begin, t_read);      // 读整帧 + 拷进上传缓冲
            const double g = ms(t_read, t_gpu);         // 录制 + 提交 + 等 GPU
            const double w = ms(t_gpu, t_written);      // 等编码器收下这帧
            wall_probe.read += r;
            wall_probe.gpu += g;
            wall_probe.write += w;
            wall_probe.frames += count;
            // batch > 1 shares one fence, so only the wall per frame is pushed.
            for (uint32_t i = 0; i < count; ++i) wall_probe.push((r + g + w) / double(count));
        }

        index += count;
        ++submits;
        if (index % o.progress_every == 0) {
            // The per-frame GPU time is already tracked by the runtime (the same
            // numbers the closing [timing] line reports); putting it on the
            // progress line is what lets the GUI show a live latency.
            const float gpu_ms = runtime.last_gpu_ms();
            const float avg_ms = runtime.average_gpu_ms();
            // wall is always available (it is measured here), while the GPU pair
            // needs the 4-slot query ring to fill, so the first frames have only
            // the wall number. Emitting them separately keeps the second column
            // useful from frame one.
            if (gpu_ms > 0.0f)
                std::fprintf(stderr, "frame %u: dispatches=%u applied=%s gpu=%.2fms avg=%.2fms "
                                     "wall=%.2fms\n",
                             index - 1, dispatches, applied ? "yes" : "no", gpu_ms, avg_ms,
                             wall_probe.live());
            else
                std::fprintf(stderr, "frame %u: dispatches=%u applied=%s wall=%.2fms\n",
                             index - 1, dispatches, applied ? "yes" : "no",
                             wall_probe.per_frame());
        }
    }
    }
    std::fprintf(stderr, "stream done: %u frames, %u submits, batch=%u\n", index, submits, batch);
    if (wall_probe.frames)
        std::fprintf(stderr,
                     "[wall] per-frame %.2fms = read+copy %.2f + gpu %.2f + write %.2f\n",
                     use_pipeline
                         ? (wall_probe.live() > 0 ? wall_probe.live() : wall_probe.per_frame())
                         : wall_probe.per_frame(),
                     wall_probe.read / double(wall_probe.frames),
                     wall_probe.gpu / double(wall_probe.frames),
                     wall_probe.write / double(wall_probe.frames));
    // Upstream's own timestamp queries: whole recording vs the network's
    // dispatches only. Read from a 4-slot ring, so it takes >4 frames to fill.
    // In batch mode the queries are not submitted until after every record()
    // call, so read_timing() sees VK_NOT_READY and the numbers stay at zero.
    if (batch > 1)
        std::fprintf(stderr, "[timing] note: batch=%u records many frames per submit; "
                             "the per-submit timestamp ring is not sampled in this mode\n", batch);
    std::fprintf(stderr,
                 "[timing] frames=%u last_gpu=%.3fms avg_gpu=%.3fms avg_network=%.3fms "
                 "temporal=%s noise=%s period=%.6fns/tick\n",
                 index, runtime.last_gpu_ms(), runtime.average_gpu_ms(), runtime.average_network_ms(),
                 o.temporal ? "on" : "off", o.noise_every_frame ? "on" : "off",
                 double(ctx.timestamp_period));

    vkDestroyFence(d, fence, nullptr);
    vkFreeCommandBuffers(d, pool, 1, &cmd);
    vkDestroyCommandPool(d, pool, nullptr);
    res.destroy(d);
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    // Upstream prints build diagnostics with printf(). Keep fd 1 for the frame
    // stream only: dup it aside, then point C stdout at stderr so no text can
    // ever be mistaken for pixel data.
    std::fflush(stdout);
    const int out_fd = _dup(_fileno(stdout));
    _dup2(_fileno(stderr), _fileno(stdout));
    setvbuf(stdout, nullptr, _IONBF, 0);
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(out_fd, _O_BINARY);

    try {
        const Options o = parse(argc, argv);
        if (o.io_bench) return io_bench(o, out_fd);
        return run(o, o.root.empty() ? default_root(argv[0]) : o.root, out_fd);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "nr_stream: %s\n", e.what());
        return 1;
    }
}
