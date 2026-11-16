# Third-party components

- **[DLSSNR-AMD](https://github.com/mochizuki0323/DLSSNR-AMD) 0.0.9** — MIT — the neural
  renderer this GUI drives. Source subset tracked under `vendor/DLSSNR-AMD-0.0.9/` with its
  original `LICENSE`, `README.md` and `THIRD_PARTY.md`. The packaged runtime (weights,
  compiled SPIR-V shaders, ACO machine code) ships in the Release archive.
- **ffmpeg / ffprobe** — LGPL/GPL builds from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)
  (`--enable-gpl --enable-version3`, shared, N-126390, 2026-09-02). Bundled inside
  `NRStudio.exe`; the builds' sources are published at the link above.
- **[stb_image](https://github.com/nothings/stb)** / stb_image_write — public domain / MIT —
  `third_party/`.
- **[PySide6](https://doc.qt.io/qtforpython/) (Qt 6)** — LGPL-3.0 — the desktop UI, linked
  dynamically by the packaged app.
- **Vulkan Headers** — Apache-2.0 — build-time only (`C:\VulkanSDK`), not distributed.
- **AMD FSR 1 headers, RenoDX shaders** — MIT — carried inside the vendored upstream tree,
  unchanged.
