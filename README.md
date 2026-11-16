# DLSSNR-AMD Studio

> 继任 [dlss5-visual-enhancer-zluda](https://github.com/big2cater/dlss5-visual-enhancer-zluda)（已归档）：此后的更新都发布在本仓库。

把 [DLSSNR-AMD](https://github.com/mochizuki0323/DLSSNR-AMD)（NVIDIA DLSS 5 神经渲染网络在 AMD RDNA 显卡上的 Vulkan/ACO 重实现）包成一个**双击即用的 Windows 桌面应用**：原生 PySide6 界面、批量队列、视频全流程，不需要网页、不需要命令行。

## 下载即用

到 [Releases](../../releases) 下载 `NRStudio.exe`（约 330 MB）：

1. 双击运行。第一次会解压到 `%LOCALAPPDATA%\NRStudio\`（约 8–17 秒，只需一次），之后每次启动 < 0.1 秒。
2. 界面直接可用；成品默认输出到 `图片\NR Studio\`。
3. 自带 ffmpeg 与两个模型权重（`CC_SMALL_FP8` / `CC_SILVER_AARDWOLD`），不依赖系统 PATH，也不需要 NVIDIA 显卡。

显卡要求：AMD Radeon RX 9000 系（RDNA4，实测 RX 9070 XT）；RDNA3 为实验性支持（上游 0.0.9 的 ACO 路径）。NVIDIA / Intel 不适用。

## 功能

- **图片 / 视频批量**：拖入文件或文件夹，队列顺序执行，逐任务进度百分比、取消单个任务
- **视频全流程**：ffmpeg 解码 → 神经渲染（RGBA8 管道）→ AMF 硬编（≥4K 自动 `h264_amf`，否则 `libx264`），VFR 输入自动锁定帧数与时长，音轨保留
- **性能**：4K 稳态每帧约 17 ms（含读写重叠的 GPU 流水线）；单张 4K 图端到端约 2.1 s
- **实时对比视图**：左右分割拖动查看处理前后
- **参数预设**：强度 / 风格 / pass / 局部色调 / 皮肤结构等，可保存为 JSON
- **可观测**：每帧 GPU 延迟与实际耗时、日志面板、`--render` 纯命令行模式

## 从源码构建

依赖：Python 3.13（含 `PySide6`、`PyInstaller`）、LLVM/clang-cl、Vulkan SDK（1.4.x）、7-Zip。

```bash
# 1. C++ 离线 / 流式渲染器
python tools/build_core.py
python tools/build_offline.py
python tools/build_stream.py

# 2. 运行时（权重 + 编译好的 shader + ACO 机器码，约 354 MB）
#    从 Releases 下载 dlssnr-amd-runtime-0.0.9.zip，解压出 runtime/ 放到仓库根目录

# 3. 自用快速构建（onefile，需项目目录在原位）
python tools/build_exe.py

# 4. 完整单文件分发版 NRStudio.exe
python tools/package.py
```

`vendor/DLSSNR-AMD-0.0.9/` 只跟踪上游 MIT 源码（`windows/src`、构建定义、GLSL shader 源）；权重、编译产物与 ffmpeg 本地准备，详见 `THIRD_PARTY.md`。

## 命令行

```bash
NRStudio.exe --render photo.jpg -o out/            # 图片
NRStudio.exe --render clip.mp4 -o out/ --max-seconds 10
NRStudio.exe --selftest                            # 内置自检
```

## 目录

| 路径 | 内容 |
| --- | --- |
| `app/nr_app.py` | PySide6 界面 + 自再入 CLI 分发 |
| `src/` | 离线 / 流式渲染宿主（Vulkan compute） |
| `tools/` | 构建、打包、视频管线（`nr_video.py`）、批处理（`ve_cli.py`） |
| `compat/`、`third_party/` | 编译期兼容头、stb_image |
| `vendor/DLSSNR-AMD-0.0.9/` | 上游源码子集（MIT） |
| `presets/` | 渲染参数预设 |

## 致谢

- [mochizuki0323/DLSSNR-AMD](https://github.com/mochizuki0323/DLSSNR-AMD) — 本项目的一切核心能力来自它（MIT）
- [wilsjo2/OptiScaler-DLSSNR-PreSR-Multipass](https://github.com/wilsjo2/OptiScaler-DLSSNR-PreSR-Multipass) — 早期 CLI 的参照
- [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) — 打包内置的 ffmpeg

## 协议

本项目 MIT（见 `LICENSE`）；上游与其第三方组件见 `THIRD_PARTY.md`。打包版内嵌 GPL 构建的 ffmpeg，其源码在上述仓库公开发布。
