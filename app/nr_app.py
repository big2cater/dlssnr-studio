"""nr-app - native desktop front end for the DLSSNR-AMD offline renderer.

One window, no browser and no web server: PySide6 paints the interface and every
job is executed by re-entering this same executable in CLI mode, which runs the
already-verified pipeline (tools/ve_cli.py -> tools/nr_video.py -> the C++
hosts). Packaged with PyInstaller into a single exe.

    nr-app.exe                     open the window
    nr-app.exe --selftest          run one image + one video headlessly
    nr-app.exe <path>ve_cli.py ... internal dispatch used by the window
"""
from __future__ import annotations

import json
import os
import re
import pathlib
import subprocess
import sys
import tempfile
import time

FROZEN = bool(getattr(sys, "frozen", False))
ROOT = pathlib.Path(sys.executable).resolve().parent if FROZEN else pathlib.Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

try:  # one definition shared with the CLI (tools/_media.py)
    from _media import IMAGE_EXT, MEDIA_EXT, VIDEO_EXT
except Exception:  # source checkout where tools/ is not importable yet
    IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tga", ".ppm", ".pgm"}
    VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts"}
    MEDIA_EXT = IMAGE_EXT | VIDEO_EXT
NO_WINDOW = 0x0800_0000 if os.name == "nt" else 0

# The window runs jobs by executing this same binary with a script name as the
# first argument; that keeps one code path for the packaged app and for source.
VE_CLI = TOOLS / "ve_cli.py"
VIDEO_TOOL = TOOLS / "nr_video.py"

try:  # packaged builds keep ffmpeg next to the exe and re-enter by module name
    from _tools import child_script as _child_script, tool as _tool
except Exception:  # source checkout where tools/ is not importable yet
    def _child_script(script: pathlib.Path) -> str:
        return str(script)

    def _tool(name: str) -> str:
        return name

CLI_ARG = _child_script(VE_CLI)
FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")


def default_output_dir() -> pathlib.Path:
    """Where finished renders land.

    Frozen, the program itself lives in %LOCALAPPDATA%\\NRStudio\\<build>, which
    is the last place anyone would look for their own files, so results go to
    Pictures instead. From source they stay in the project's outputs/.
    """
    if not FROZEN:
        return ROOT / "outputs/gui"
    home = pathlib.Path.home()
    for name in ("Pictures", "Documents"):
        candidate = home / name
        if candidate.is_dir():
            return candidate / "NR Studio"
    return home / "NR Studio"


def dispatch(argv: list[str]) -> int | None:
    """If argv looks like an internal CLI call, run it and return its status."""
    if len(argv) < 2:
        return None
    name = pathlib.Path(argv[1]).name
    if name not in ("ve_cli.py", "nr_video.py"):
        return None
    import importlib

    module = importlib.import_module(name[:-3])
    sys.argv = argv[1:]
    return module.main()


def render_cli(argv: list[str]) -> int:
    """nr-app.exe --render <input> -o <output> [...] -> one file, no window.

    Without this the flag fell through to the GUI branch: the exe launched a
    window and sat there, which looked like a hang in every scripted use.
    """
    rest = argv[argv.index("--render") + 1:]
    if not rest:
        print("usage: nr-app.exe --render <input> -o <output> [--preset <file>] "
              "[--max-seconds N] [--crf N] ...", file=sys.stderr)
        return 2
    import ve_cli

    sys.argv = ["ve_cli.py", "render", *rest]
    return ve_cli.main()


def force_utf8_stdio() -> None:
    """Both the console capture and the JSON report the window parses are UTF-8.

    Without this, a redirected stdout on a Chinese Windows uses cp936 and any
    non-ASCII path comes back as mojibake (the child process inherits the same
    setting, so this covers the re-entrant CLI mode too).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def hide_console() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


# ---------------------------------------------------------------- Qt interface
from PySide6.QtCore import QProcess, QRect, QSize, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QCursor  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)


class CompareView(QWidget):
    """Before/after view: the result, with the source revealed left of a split."""

    def __init__(self) -> None:
        super().__init__()
        self.before: QPixmap | None = None
        self.after: QPixmap | None = None
        self.ratio = 0.5
        self.setMinimumSize(QSize(360, 260))
        self.setMouseTracking(False)

    def set_images(self, before: QPixmap | None, after: QPixmap | None) -> None:
        self.before = before
        self.after = after
        self.update()

    def _target(self) -> QRect:
        """Aspect-correct destination rect inside the widget."""
        src = self.after or self.before
        if src is None:
            return QRect()
        avail = self.rect().adjusted(8, 8, -8, -8)
        scaled = src.size().scaled(avail.size(), Qt.KeepAspectRatio)
        x = avail.left() + (avail.width() - scaled.width()) // 2
        y = avail.top() + (avail.height() - scaled.height()) // 2
        return QRect(x, y, scaled.width(), scaled.height())

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#f5f6f8"))
        if self.after is None and self.before is None:
            p.setPen(QColor("#8a8f98"))
            p.drawText(self.rect(), Qt.AlignCenter, "处理完成后在这里显示 原图 / 结果 对比")
            return
        target = self._target()
        if self.after is not None:
            p.drawPixmap(target, self.after)
        if self.before is not None and self.after is not None:
            split = target.left() + int(target.width() * self.ratio)
            p.save()
            p.setClipRect(QRect(target.left(), target.top(), split - target.left(), target.height()))
            p.drawPixmap(target, self.before)
            p.restore()
            p.setPen(QPen(QColor(255, 255, 255, 220), 2))
            p.drawLine(split, target.top(), split, target.bottom())
        p.setPen(QColor("#5a6270"))
        p.drawText(self.rect().adjusted(10, 0, -10, -6), Qt.AlignLeft | Qt.AlignBottom,
                   "左：原图    右：结果    拖动分割线对比")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._drag(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.LeftButton:
            self._drag(event)

    def _drag(self, event) -> None:
        target = self._target()
        if target.width() <= 0:
            return
        self.ratio = min(1.0, max(0.0, (event.position().x() - target.left()) / target.width()))
        self.update()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DLSSNR-AMD 离线增强器")
        self.resize(1480, 940)
        self.setMinimumWidth(1180)

        self.dir = ROOT / "samples"
        self.out_dir = default_output_dir()
        self.jobs: list[dict] = []
        self.queue: list[dict] = []
        self._closing = False
        self.proc: QProcess | None = None
        self.current: dict | None = None
        self.preview_proc: QProcess | None = None
        self.preset_files: list[str] = []

        self.tick = QTimer(self)
        self.tick.setInterval(400)
        self.tick.timeout.connect(self._on_tick)
        self.setAcceptDrops(True)
        self._build_ui()
        self._refresh_presets()
        self._load_preset(self.preset_combo.currentText())

    # ---------------------------------------------------------------- layout
    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Horizontal)

        # Top toolbar: pickers + drag-drop replace the old directory browser,
        # which duplicated what the queue already shows.
        outer = QVBoxLayout()
        outer.setContentsMargins(8, 8, 8, 8)
        outer.addWidget(self._build_toolbar())

        # centre: params + queue + log
        centre = QWidget()
        cv = QVBoxLayout(centre)
        cv.setContentsMargins(8, 8, 8, 8)
        cv.addWidget(self._build_params())
        cv.addWidget(self._build_queue(), 1)
        cv.addWidget(self._build_log())
        splitter.addWidget(centre)

        # right: preview
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(8, 8, 8, 8)
        self.compare = CompareView()
        rv.addWidget(self.compare, 1)
        self.preview_info = QLabel("尚未处理任何文件")
        self.preview_info.setWordWrap(True)
        rv.addWidget(self.preview_info)
        open_out = QPushButton("打开输出目录")
        open_out.clicked.connect(self._open_out_dir)
        rv.addWidget(open_out)
        splitter.addWidget(right)

        splitter.setSizes([880, 520])
        splitter.setStretchFactor(0, 1)
        outer.addWidget(splitter, 1)
        shell = QWidget()
        shell.setLayout(outer)
        self.setCentralWidget(shell)

        bar = self.statusBar()
        bar.showMessage("就绪")
        self.gpu_label = QLabel("GPU: 检测中…")
        bar.addPermanentWidget(self.gpu_label)
        QTimer.singleShot(150, self._probe_gpu)

    @staticmethod
    def _tip(widget, text: str):
        widget.setToolTip(text)
        return widget

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)

        add_files = QPushButton("添加文件…")
        add_files.clicked.connect(self._pick_files)
        add_folder = QPushButton("添加文件夹…")
        add_folder.clicked.connect(self._pick_folder)
        self._tip(add_files, "选择一个或多个图片/视频加入队列；也可以直接把文件或文件夹拖进窗口")
        self._tip(add_folder, "选择文件夹，递归加入其中的图片与视频")
        h.addWidget(add_files)
        h.addWidget(add_folder)

        # Primary actions sit early on purpose: a long output path must never be
        # able to push them off the right edge.
        h.addSpacing(10)
        self.start_btn = QPushButton("开始处理")
        self.start_btn.setMinimumWidth(96)
        self.start_btn.clicked.connect(self._start)
        self._tip(self.start_btn, "按队列顺序逐个处理：同一时刻只跑一个任务，避免互相争抢 GPU")
        h.addWidget(self.start_btn)
        self.clear_btn = QPushButton("清空队列")
        self.clear_btn.clicked.connect(self._clear_queue)
        self._tip(self.clear_btn, "移除队列里的待处理任务（不影响已完成的输出文件）")
        h.addWidget(self.clear_btn)

        self.cancel_btn = QPushButton("取消当前")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_current)
        self._tip(self.cancel_btn, "终止正在处理的任务，连同它派生的 ffmpeg / 渲染进程一起结束；已排队任务不受影响")
        h.addWidget(self.cancel_btn)

        h.addSpacing(14)
        h.addWidget(self._tip(QLabel("预设"), "presets/ 目录里的参数组合；改完参数可点右侧「保存」写回"))
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(140)
        self.preset_combo.setMaximumWidth(220)
        self.preset_combo.currentTextChanged.connect(self._load_preset)
        h.addWidget(self._tip(self.preset_combo, "切换即载入该预设的全部参数"))
        self.save_preset_btn = QPushButton("保存")
        self.save_preset_btn.clicked.connect(self._save_preset)
        h.addWidget(self._tip(self.save_preset_btn, "把当前面板参数覆盖写回选中的预设文件"))

        h.addSpacing(14)
        h.addWidget(self._tip(QLabel("输出"), "结果目录；与源文件同名时自动加 _2、_3 避免覆盖"))
        self.out_edit = QLineEdit(str(self.out_dir))
        self.out_edit.setMinimumWidth(200)
        self.out_edit.setMaximumWidth(420)
        h.addWidget(self.out_edit, 1)
        browse = QPushButton("…")
        browse.setFixedWidth(34)
        browse.clicked.connect(self._choose_out_dir)
        self._tip(browse, "选择输出目录")
        h.addWidget(browse)
        return bar

    def _build_params(self) -> QGroupBox:
        box = QGroupBox("处理参数")
        outer = QHBoxLayout(box)

        left = QFormLayout()
        self.model_combo = QComboBox()
        self.model_combo.addItems(["small", "original", "auto"])
        self._tip(self.model_combo,
                  "small = CC_SMALL_FP8（108.6 MB，最快）\n"
                  "original = CC_SILVER_AARDWOLD（141 MB，更重）\n"
                  "auto = 读取 runtime/dlssnr-amd.ini 里的设置")
        left.addRow(self._tip(QLabel("模型"), "选择网络权重包"), self.model_combo)

        self.intensity = self._dspin(0.0, 1.0, 1.0, 0.05)
        self._tip(self.intensity, "网络结果与原图的混合强度\n1.0 = 完全采用网络输出，0 = 保持原样")
        left.addRow(self._tip(QLabel("强度"), "整体作用于画面的增强程度"), self.intensity)

        self.style = QSpinBox()
        self.style.setRange(0, 2)
        self._tip(self.style, "上游 pre-conditioning 的 style 通道（0/1/2）\n影响整体观感取向，不同片段可分别试")
        left.addRow(self._tip(QLabel("风格"), "网络风格通道"), self.style)

        self.passes = QSpinBox()
        self.passes.setRange(1, 4)
        self._tip(self.passes, "网络重复迭代次数\n越多细节越强，耗时会成倍增加")
        left.addRow(self._tip(QLabel("passes"), "迭代次数"), self.passes)

        right = QFormLayout()
        self.local_tone = self._dspin(0.0, 2.0, 1.0, 0.05)
        self._tip(self.local_tone, "局部亮度/色调的保留系数\n>1 更强调原有明暗关系，过大可能显得生硬")
        right.addRow(self._tip(QLabel("局部色调"), "局部亮度与色调保留"), self.local_tone)

        self.local_structure = self._dspin(0.0, 2.0, 1.0, 0.05)
        self._tip(self.local_structure, "结构细节（纹理、边缘、发丝）的保留系数\n调高可减少糊化")
        right.addRow(self._tip(QLabel("局部结构"), "细节结构保留"), self.local_structure)

        self.skin_structure = self._dspin(-1.0, 1.0, -1.0, 0.05)
        self._tip(self.skin_structure, "人像皮肤纹理保留程度\n-1 = 交给自动蒙版决定；0~1 = 主动加强皮肤纹理（避免磨皮过度）")
        right.addRow(self._tip(QLabel("皮肤结构"), "皮肤纹理强度"), self.skin_structure)

        checks = QHBoxLayout()
        self.automask = QCheckBox("自动蒙版")
        self.automask.setChecked(True)
        self._tip(self.automask, "自动生成人像/皮肤保护蒙版\n关掉后整幅画面统一处理，人像容易出现过度磨皮")
        self.aco = QCheckBox("ACO 机器码")
        self.aco.setChecked(True)
        self._tip(self.aco, "使用安装包内预编译的 ACO 机器码，替代 LLPC 现场编译\n"
                            "4K 实测网络部分快约 21%；首次运行需导入缓存（约 45 秒，之后命中）")
        self.rgba8 = QCheckBox("RGBA8 直通")
        self.rgba8.setChecked(True)
        self._tip(self.rgba8, "把 8bit 帧直接交给运行时，省 4 倍显存带宽与 CPU 转换\n实测输出与 32F 路径逐字节一致")
        for w in (self.automask, self.aco, self.rgba8):
            checks.addWidget(w)
        right.addRow("", checks)

        self.codec = QComboBox()
        self.codec.addItems(["auto", "libx264", "h264_amf", "hevc_amf", "av1_amf"])
        self._tip(self.codec, "视频编码器\n"
                              "auto：≥4K 用 h264_amf（实测快约 36%），以下用 libx264（同质量体积更小）\n"
                              "AMF 为显卡硬编，libx264 为 CPU 软编；图片任务忽略此项")
        right.addRow(self._tip(QLabel("编码器"), "只影响视频输出"), self.codec)

        self.crf = QSpinBox()
        self.crf.setRange(0, 51)
        self.crf.setValue(18)
        self._tip(self.crf, "质量/体积的取舍：数值越小质量越高、文件越大\n"
                            "x264 的 crf 与 AMF 的 qp 不是同一标尺，但 16~20 都属高质量区间")
        right.addRow(self._tip(QLabel("质量 (crf/qp)"), "编码质量"), self.crf)

        checks2 = QHBoxLayout()
        self.temporal = QCheckBox("时序")
        self.temporal.setChecked(True)
        self._tip(self.temporal, "历史帧混合 + 光流运动估计\n视频务必开启（显著减少闪烁）；单张图片可关闭")
        self.noise = QCheckBox("逐帧噪声")
        self.noise.setChecked(True)
        self._tip(self.noise, "按 DLL 的视频语义逐帧推进噪点种子\n配合时序能压掉约 42% 的帧间抖动")
        self.batch = QSpinBox()
        self.batch.setRange(1, 8)
        self._tip(self.batch, "每次提交录制多少帧\nI/O 优化后已无速度差异，只多占显存，保持 1 即可")
        checks2.addWidget(self.temporal)
        checks2.addWidget(self.noise)
        checks2.addWidget(self._tip(QLabel("batch"), "每批提交帧数"))
        checks2.addWidget(self.batch)
        right.addRow("", checks2)

        self.max_seconds = QDoubleSpinBox()
        self.max_seconds.setRange(0.0, 3600.0)
        self.max_seconds.setDecimals(1)
        self.max_seconds.setSingleStep(5.0)
        self.max_seconds.setSuffix(" 秒")
        self.max_seconds.setSpecialValueText("整段")
        self._tip(self.max_seconds,
                  "只输出前 N 秒（0 = 整段视频）\n"
                  "解码器读到 N 秒就停，不会白解码后面的内容；音轨同步截断\n"
                  "适合先跑 10 秒看效果，确认参数后再整段处理")
        right.addRow(self._tip(QLabel("时长上限"), "视频只处理前 N 秒"), self.max_seconds)

        outer.addLayout(left)
        outer.addSpacing(24)
        outer.addLayout(right)
        outer.addStretch(1)

        # The form columns are narrower than the group box, so put the space to
        # work instead of leaving it blank.
        outer.addSpacing(28)
        hint = QLabel(
            "· 文件或文件夹可以直接拖进窗口\n"
            "· 同名输出自动加 _2、_3，不覆盖已有结果\n"
            "· 视频建议保留「时序 + 逐帧噪声」\n"
            "· 图片任务忽略编码器与质量设置\n"
            "· 鼠标停在任一参数上可看说明"
        )
        hint.setStyleSheet("color:#5b6472; line-height:150%;")
        outer.addWidget(hint, 0, Qt.AlignTop)
        outer.addStretch(1)
        return box

    @staticmethod
    def _dspin(lo: float, hi: float, val: float, step: float) -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setSingleStep(step)
        w.setDecimals(2)
        w.setValue(val)
        return w

    def _build_queue(self) -> QGroupBox:
        box = QGroupBox("任务队列")
        v = QVBoxLayout(box)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["状态", "文件", "进度", "GPU 延迟", "实际/帧", "耗时", "备注"])
        self.table.horizontalHeaderItem(3).setToolTip(
            "每帧的 GPU 计算时间（输入/网络/合成），取自 GPU 时间戳\n"
            "不含等待解码、上传拷贝和等待编码\n"
            "运行中显示最新一帧，完成后显示整段平均")
        self.table.horizontalHeaderItem(4).setToolTip(
            "每帧真实产出间隔：流水线模式下是相邻帧完成的时间差\n"
            "串行模式下 = 读帧+拷贝 + GPU 等待 + 写出等待\n"
            "不含 ffmpeg 解码/编码本身的计算，整段耗时/帧数还会更高")
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        header.setSectionResizeMode(6, QHeaderView.Stretch)
        self.table.setColumnWidth(2, 58)
        self.table.setColumnWidth(3, 82)
        self.table.setColumnWidth(4, 78)
        self.table.setColumnWidth(5, 64)
        v.addWidget(self.table)
        return box

    def _build_log(self) -> QGroupBox:
        box = QGroupBox("日志")
        v = QVBoxLayout(box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        self.log.setFont(QFont("Consolas", 9))
        v.addWidget(self.log)
        return box

    # ------------------------------------------------------------- presets
    def _refresh_presets(self) -> None:
        pdir = ROOT / "presets"
        self.preset_files = sorted(p.name for p in pdir.glob("*.json")) if pdir.is_dir() else []
        current = self.preset_combo.currentText()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItems(self.preset_files or ["(无预设)"])
        if current in self.preset_files:
            self.preset_combo.setCurrentText(current)
        self.preset_combo.blockSignals(False)

    def _load_preset(self, name: str) -> None:
        if not name or name.startswith("("):
            return
        path = ROOT / "presets" / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            self._log(f"读取预设失败 {name}: {e}")
            return
        nr = data.get("neural_rendering", {})
        vid = data.get("video", {})
        rt = data.get("runtime", {})
        out = data.get("output", {})
        if (m := nr.get("model")) in ("small", "original", "auto"):
            self.model_combo.setCurrentText(m)
        for widget, key, default in (
            (self.intensity, "intensity", 1.0),
            (self.local_tone, "local_tone", 1.0),
            (self.local_structure, "local_structure", 1.0),
            (self.skin_structure, "skin_structure", -1.0),
        ):
            widget.setValue(float(nr.get(key, default)))
        self.style.setValue(int(nr.get("style", 0)))
        self.passes.setValue(int(nr.get("passes", 1)))
        self.automask.setChecked(bool(nr.get("automatic_mask", True)))
        self.temporal.setChecked(bool(vid.get("temporal", True)))
        self.noise.setChecked(bool(vid.get("noise_every_frame", True)))
        self.codec.setCurrentText(str(vid.get("codec", "auto")))
        self.crf.setValue(int(vid.get("crf", 18)))
        self.max_seconds.setValue(float(vid.get("max_seconds") or 0))
        self.aco.setChecked(bool(rt.get("aco", True)))
        self.rgba8.setChecked(bool(rt.get("rgba8", True)))
        self.batch.setValue(max(1, int(rt.get("batch", 1))))
        if out.get("suffix"):
            self.suffix = str(out["suffix"])
            self.image_ext = str(out.get("image_ext", "png"))
        self._log(f"已载入预设 {name}")

    def _save_preset(self) -> None:
        name = self.preset_combo.currentText()
        if not name or name.startswith("("):
            QMessageBox.warning(self, "无法保存", "请先在 presets 目录里放一个 .json 预设")
            return
        data = {
            "neural_rendering": {
                "model": self.model_combo.currentText(),
                "intensity": self.intensity.value(),
                "style": self.style.value(),
                "passes": self.passes.value(),
                "local_tone": self.local_tone.value(),
                "local_structure": self.local_structure.value(),
                "skin_structure": self.skin_structure.value(),
                "automatic_mask": self.automask.isChecked(),
            },
            "video": {
                "temporal": self.temporal.isChecked(),
                "noise_every_frame": self.noise.isChecked(),
                "codec": self.codec.currentText(),
                "crf": self.crf.value(),
                "vfr": "cfr",
                "max_seconds": self.max_seconds.value(),
            },
            "output": {"suffix": getattr(self, "suffix", "_visual_enhancer"),
                       "image_ext": getattr(self, "image_ext", "png")},
            "runtime": {
                "aco": self.aco.isChecked(),
                "batch": self.batch.value(),
                "rgba8": self.rgba8.isChecked(),
            },
        }
        (ROOT / "presets" / name).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        self._log(f"预设已保存: {name}")

    # --------------------------------------------------------------- files
    def _pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择图片或视频",
            str(self.dir),
            "媒体文件 (*.png *.jpg *.jpeg *.bmp *.tga *.mp4 *.mkv *.mov *.avi *.webm *.m4v);;所有文件 (*.*)",
        )
        for name in files:
            path = pathlib.Path(name)
            self.dir = path.parent
            self._enqueue(path)

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹（递归加入素材）", str(self.dir))
        if not folder:
            return
        self.dir = pathlib.Path(folder)
        self._collect(self.dir)

    def _collect(self, path: pathlib.Path) -> None:
        """Add a file, or every media file under a directory."""
        if path.is_dir():
            found = sorted(
                (p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXT),
                key=lambda p: str(p).lower(),
            )
            for f in found:
                self._enqueue(f)
            if not found:
                self._log(f"该文件夹里没有可处理的媒体文件: {path}")
        elif path.is_file() and path.suffix.lower() in MEDIA_EXT:
            self._enqueue(path)
        else:
            self._log(f"跳过不支持的文件: {path}")

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if not event.mimeData().hasUrls():
            return
        # Accepting everything shows an accept cursor and then silently ignores
        # .exe/.txt drops; only claim folders and known media.
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if not local:
                continue
            path = pathlib.Path(local)
            if path.is_dir() or path.suffix.lower() in MEDIA_EXT:
                event.acceptProposedAction()
                return

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local:
                self._collect(pathlib.Path(local))
        event.acceptProposedAction()

    def _output_for(self, src: pathlib.Path) -> pathlib.Path:
        is_video = src.suffix.lower() in VIDEO_EXT
        ext = "mp4" if is_video else getattr(self, "image_ext", "png")
        suffix = getattr(self, "suffix", "_visual_enhancer")
        base = self._out_dir() / f"{src.stem}{suffix}.{ext}"
        # Two sources can share a stem (photo.jpg + photo.png): give the second a
        # numbered name instead of letting it overwrite the first.
        def claimed(cand: pathlib.Path) -> bool:
            return any(j["output"] == cand and j["input"] != src for j in self.jobs)

        if not claimed(base):
            return base
        n = 2
        while True:
            cand = base.with_name(f"{base.stem}_{n}{base.suffix}")
            if not cand.exists() and not claimed(cand):
                return cand
            n += 1

    def _out_dir(self) -> pathlib.Path:
        text = self.out_edit.text().strip() if hasattr(self, "out_edit") else str(self.out_dir)
        return pathlib.Path(text) if text else self.out_dir

    def _enqueue(self, src: pathlib.Path) -> None:
        if any(j["input"] == src for j in self.jobs):
            return
        out = self._output_for(src)
        job = {"input": src, "output": out, "status": "待处理", "seconds": 0.0, "note": ""}
        self.jobs.append(job)
        self._sync_table()
        self._log(f"加入队列: {src.name} -> {out.name}")

    def _sync_table(self) -> None:
        colours = {
            "待处理": QColor("#6b7280"),
            "进行中": QColor("#b45309"),
            "完成": QColor("#15803d"),
            "失败": QColor("#b91c1c"),
            "已取消": QColor("#7c3aed"),
        }
        self.table.setRowCount(len(self.jobs))
        for row, job in enumerate(self.jobs):
            status = job["status"]
            progress = job.get("progress")
            if status == "进行中":
                pct = f"{progress:.0f}%" if progress is not None else "…"
            elif status == "完成":
                pct = "100%"
            else:
                pct = ""
            latency = job.get("latency")
            lat = f"{latency:.1f} ms" if latency else ""
            wall = job.get("wall")
            wall_s = f"{wall:.1f} ms" if wall else ""
            cells = (status, job["input"].name, pct, lat, wall_s,
                     f"{job['seconds']:.2f}s" if job.get("seconds") else "", job["note"])
            for col, text in enumerate(cells):
                item = self.table.item(row, col)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, col, item)
                item.setText(text)
                if col in (0, 2):
                    item.setForeground(colours.get(status, QColor("#111827")))

    def _clear_queue(self) -> None:
        if self.proc is not None:
            QMessageBox.information(self, "正在处理", "请先「取消当前」再清空队列")
            return
        self.jobs.clear()
        self.queue.clear()  # nothing queued may start after the table is emptied
        self.current = None
        self.tick.stop()
        self.cancel_btn.setEnabled(False)
        self._sync_table()

    def _choose_out_dir(self) -> None:
        picked = QFileDialog.getExistingDirectory(self, "选择输出目录", str(self._out_dir()))
        if picked:
            self.out_edit.setText(picked)

    def _open_out_dir(self) -> None:
        target = self._out_dir()
        target.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(target))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(target)])

    # ---------------------------------------------------------------- jobs
    def _flags(self) -> list[str]:
        flags = [
            "--model", self.model_combo.currentText(),
            "--intensity", f"{self.intensity.value():.3f}",
            "--style", str(self.style.value()),
            "--passes", str(self.passes.value()),
            "--local-tone", f"{self.local_tone.value():.3f}",
            "--local-structure", f"{self.local_structure.value():.3f}",
            "--skin-structure", f"{self.skin_structure.value():.3f}",
            "--codec", self.codec.currentText(),
            "--crf", str(self.crf.value()),
            "--batch", str(self.batch.value()),
        ]
        if not self.automask.isChecked():
            flags.append("--no-automask")
        if not self.temporal.isChecked():
            flags.append("--no-temporal")
        if self.noise.isChecked():
            flags.append("--noise-every-frame")
        if self.aco.isChecked():
            flags.append("--aco")
        if self.rgba8.isChecked():
            flags.append("--rgba8")
        if self.max_seconds.value() > 0:
            flags += ["--max-seconds", f"{self.max_seconds.value():.3f}"]
        return flags

    def _start(self) -> None:
        if self.proc is not None:
            return
        pending = [j for j in self.jobs if j["status"] == "待处理"]
        if not pending:
            self._log("没有待处理的任务")
            return
        self._out_dir().mkdir(parents=True, exist_ok=True)
        for j in pending:
            j["progress"] = None
            j["seconds"] = 0.0
        self.queue = pending
        self.start_btn.setEnabled(False)
        self._run_next()

    def _run_next(self) -> None:
        # A finish can be queued right as the window goes away; the timer and
        # children are gone by then, so stop quietly instead of raising.
        if self._closing:
            return
        if not self.queue:
            self.start_btn.setEnabled(True)
            self.proc = None
            self._log("队列完成")
            self.statusBar().showMessage("队列完成")
            return
        job = self.queue.pop(0)
        job["status"] = "进行中"
        job["progress"] = None
        job["started"] = time.monotonic()
        job["cmd"] = [sys.executable, CLI_ARG, "render", str(job["input"]),
                      "-o", str(job["output"]), "--overwrite", "--json", "--quiet", *self._flags()]
        # Frames drive the percentage; probing costs ~50 ms once per video.
        cap = self.max_seconds.value()
        job["total"] = (self._video_frames(job["input"], cap)
                        if job["input"].suffix.lower() in VIDEO_EXT else 0)
        job["log_path"] = pathlib.Path(str(job["output"]) + ".nr_stream.log")
        self.current = job
        self._sync_table()
        self.statusBar().showMessage(f"正在处理 {job['input'].name}")
        self._log(f"[开始] {job['input'].name}")
        self.cancel_btn.setEnabled(True)
        self.tick.start()

        cmd = job["cmd"]
        proc = QProcess(self)
        self.proc = proc
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.finished.connect(lambda code, _status, p=proc: self._job_done(job, code, p))
        proc.start(cmd[0], cmd[1:])

    def _job_done(self, job: dict, code: int, proc: QProcess | None = None) -> None:
        if self._closing:
            return
        output = ""
        if proc is not None:
            try:
                output = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
            except RuntimeError:  # wrapper already torn down
                output = ""
        import time

        try:
            self.tick.stop()
            self.cancel_btn.setEnabled(False)
        except RuntimeError:  # window already torn down
            return
        job["seconds"] = time.monotonic() - job.get("started", time.monotonic())
        if job.get("status") == "已取消":
            # The kill surfaces here as a non-zero exit; keep the user's verdict.
            self._log(f"[已取消] {job['input'].name}（输出文件可能不完整）")
            self._sync_table()
            self.current = None
            self.proc = None
            QTimer.singleShot(0, self._run_next)
            return
        if code == 0:
            job["status"] = "完成"
            note = ""
            start = output.find("{")
            if start >= 0:
                try:
                    report = json.loads(output[start:])
                    results = report.get("results") or []
                    if results:
                        note = str(results[0].get("status", ""))
                except (ValueError, TypeError):
                    pass
            job["note"] = note or "ok"
            job["progress"] = 100.0
            if job["input"].suffix.lower() in VIDEO_EXT:
                final = self._read_progress(job)
                if final:
                    # The clip is over, so show its averages rather than the last
                    # frame; a still image has no per-frame timing at all.
                    if final[1]:
                        job["latency"] = final[1]
                    if final[2]:
                        job["wall"] = final[2]
            self._log(f"[完成] {job['input'].name}  {job['seconds']:.2f}s  -> {job['output']}")
            self._load_preview(job)
        else:
            job["status"] = "失败"
            job["note"] = self._failure_text(output, code)
            self._log(f"[失败] {job['input'].name}\n{output[-2000:]}")
        self._sync_table()
        self.current = None
        if proc is None or proc is self.proc:
            self.proc = None
        QTimer.singleShot(0, self._run_next)

    @staticmethod
    def _failure_text(output: str, code: int) -> str:
        """The human-readable reason from a failed run.

        With --json the report is the last thing on stdout, so "last line" was
        literally "}" -- useless in the note column. Prefer the report's error
        field, then the last non-brace line.
        """
        text = (output or "").strip()
        if text:
            start = text.find("{")
            if start >= 0:
                try:
                    results = json.loads(text[start:]).get("results") or []
                    err = str(results[0].get("error") or "").strip() if results else ""
                    if err:
                        return err.splitlines()[-1].strip()[:200]
                except Exception:
                    pass
            lines = [ln.strip() for ln in text.splitlines()
                     if ln.strip() and ln.strip() not in ("{", "}", "[", "]", "],", "},")]
            if lines:
                return lines[-1][:200]
        return f"exit {code}"

    # ------------------------------------------------------------ progress
    def _on_tick(self) -> None:
        job = self.current
        if job is None or self.proc is None:
            self.tick.stop()
            return
        if job.get("started"):
            job["seconds"] = time.monotonic() - job["started"]
        if job["input"].suffix.lower() in VIDEO_EXT:
            seen = self._read_progress(job)
            total = job.get("total") or 0
            if seen is not None:
                frames_seen, gpu_ms, wall_ms = seen
                # The first frames include the graph build, so their wall time is
                # several times the steady state; wait for a few before showing it
                # rather than printing a number that immediately collapses.
                if wall_ms and frames_seen >= 5:
                    job["wall"] = wall_ms
                if gpu_ms:
                    job["latency"] = gpu_ms
                    wall = job.get("wall")
                    extra = f"，实际 {wall:.1f} ms" if wall else ""
                    self.statusBar().showMessage(
                        f"正在处理 {job['input'].name} — GPU {gpu_ms:.1f} ms{extra}"
                        f"（约 {1000.0 / max(gpu_ms, 1e-3):.0f} fps 计算）")
            if seen is not None and total > 0:
                # "frame N:" is the index of the frame just finished, so N+1 are
                # done. Hold back the last percent: the encoder still has to run.
                job["progress"] = min(99.0, (frames_seen + 1) / total * 100.0)
        self._sync_table()

    @staticmethod
    def _read_progress(job: dict) -> tuple[int, float | None, float | None] | None:
        """Frames done, latest GPU time and latest wall time, from the log.

        nr_video writes the log next to the output while it runs, so this needs
        no changes to the pipeline and works for the packaged exe too.
        """
        log = job.get("log_path")
        if not isinstance(log, pathlib.Path) or not log.exists():
            return None
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        done = re.search(r"stream done: (\d+) frames", text)
        if done:
            # The closing line carries the average, which is the number worth
            # showing once the job is over.
            avg = re.search(r"avg_gpu=([\d.]+)ms", text)
            wall = re.search(r"\[wall\] per-frame ([\d.]+)ms", text)
            return (int(done.group(1)),
                    float(avg.group(1)) if avg else None,
                    float(wall.group(1)) if wall else None)
        frames = re.findall(r"frame (\d+):", text)
        if not frames:
            return None
        gpu = re.findall(r"(?<!avg_)gpu=([\d.]+)ms", text)
        wall = re.findall(r"wall=([\d.]+)ms", text)
        return (int(frames[-1]),
                float(gpu[-1]) if gpu else None,
                float(wall[-1]) if wall else None)

    def _video_frames(self, path: pathlib.Path, max_seconds: float = 0.0) -> int:
        """How many frames this job will render, matching nr_video's own maths.

        nb_frames alone is wrong twice over: a variable-frame-rate clip keeps its
        source count (44) while the pipeline resamples it to the nominal rate
        (70), and a duration cap renders only the first N seconds. Either way the
        percentage would stall or jump, so the denominator is derived the same
        way the pipeline derives its frame limit.
        """
        code, out = self._run_hidden(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames,duration,r_frame_rate,avg_frame_rate",
             "-of", "json", str(path)], timeout=15)
        if code != 0:
            return 0
        try:
            stream = json.loads(out)["streams"][0]
        except Exception:
            return 0

        def ratio(text: str) -> float:
            num, _, den = str(text or "").partition("/")
            try:
                return float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                return 0.0

        nominal = ratio(stream.get("r_frame_rate")) or ratio(stream.get("avg_frame_rate")) or 24.0
        try:
            duration = float(stream.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        nb = str(stream.get("nb_frames") or "")
        nb = int(nb) if nb.isdigit() else 0

        expected = nb
        if duration > 0:
            from_duration = int(round(duration * nominal))
            # 2% slack absorbs rounding in the container's duration field; a VFR
            # clip misses by far more than that and must follow the resampling.
            if from_duration > 0 and (nb <= 0 or abs(nb - from_duration) > max(1, from_duration // 50)):
                expected = from_duration
        if max_seconds and max_seconds > 0:
            capped = int(round(max_seconds * nominal))
            expected = min(expected, capped) if expected > 0 else capped
        return max(0, expected)

    # ------------------------------------------------------------ cancel
    def _cancel_current(self) -> None:
        job = self.current
        proc = self.proc
        if job is None or proc is None:
            return
        try:
            alive = proc.state() != QProcess.NotRunning
        except RuntimeError:
            alive = False
        if not alive:
            return
        if QMessageBox.question(
            self, "取消任务",
            f"终止正在处理的任务？\n{job['input'].name}\n\n已生成的输出文件可能不完整。",
        ) != QMessageBox.Yes:
            return
        job["status"] = "已取消"
        job["note"] = "用户取消"
        self._kill_tree(proc)
        self.tick.stop()
        self.cancel_btn.setEnabled(False)
        self._log(f"[取消] {job['input'].name}")
        self._sync_table()

    @staticmethod
    def _kill_tree(proc: QProcess) -> None:
        """Kill the whole process tree: the CLI spawns ffmpeg and the renderer,
        and killing only the parent would leave them holding the GPU."""
        pid = int(proc.processId())
        if pid <= 0:
            proc.kill()
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, creationflags=0x08000000)
        else:
            proc.kill()
        proc.waitForFinished(3000)

    def _remove_job(self, row: int) -> None:
        if not (0 <= row < len(self.jobs)):
            return
        if self.jobs[row] is self.current:
            QMessageBox.information(self, "正在处理", "该任务正在运行，请先取消")
            return
        job = self.jobs.pop(row)
        # The scheduler iterates its own list; dropping the row alone would let
        # the job run invisibly and write its output anyway.
        self.queue = [q for q in self.queue if q is not job]
        self._log(f"已移除任务 {job['input'].name}")
        self._sync_table()

    def _row_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if not (0 <= row < len(self.jobs)):
            return
        job = self.jobs[row]
        menu = QMenu(self)
        if job["status"] == "进行中":
            menu.addAction("取消任务", self._cancel_current)
        else:
            menu.addAction("移除任务", lambda: self._remove_job(row))
        if pathlib.Path(job["output"]).exists():
            menu.addAction("打开输出目录", self._open_out_dir)
        if job.get("cmd"):
            menu.addAction("复制命令行", lambda: QApplication.clipboard().setText(
                " ".join(str(x) for x in job["cmd"])))
        if job.get("note"):
            menu.addAction("复制备注", lambda: QApplication.clipboard().setText(job["note"]))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _probe_gpu(self) -> None:
        exe = ROOT / "build/nr_offline.exe"
        if not exe.exists():
            self.gpu_label.setText("GPU: 缺少 nr_offline.exe")
            return
        code, text = self._run_hidden([str(exe), "devices"], timeout=20)
        if code != 0:
            self.gpu_label.setText("GPU: 检测失败")
            return
        name = ""
        for line in text.splitlines()[1:]:
            parts = line.split(None, 3)
            if len(parts) >= 4:
                name = parts[3].strip()
                break
        self.gpu_label.setText(f"GPU: {name}" if name else "GPU: 未检测到设备")

    # ------------------------------------------------------------- preview
    def _load_preview(self, job: dict) -> None:
        src, out = job["input"], job["output"]
        if not out.exists():
            return
        if src.suffix.lower() in VIDEO_EXT:
            before_png = pathlib.Path(tempfile.gettempdir()) / "nr_app_before.png"
            after_png = pathlib.Path(tempfile.gettempdir()) / "nr_app_after.png"
            ok_before = self._grab_frame(src, before_png)
            ok_after = self._grab_frame(out, after_png)
            if not ok_after:
                return
            self.compare.set_images(
                QPixmap(str(before_png)) if ok_before else None, QPixmap(str(after_png))
            )
        else:
            after = QPixmap(str(out))
            before = QPixmap(str(src)) if src.exists() else None
            self.compare.set_images(before if not before.isNull() else None, after)
        self.preview_info.setText(
            f"{src.name}  →  {out.name}\n{job['seconds']:.2f}s    "
            f"{self.compare.after.width() if self.compare.after else 0} x "
            f"{self.compare.after.height() if self.compare.after else 0}"
        )

    @staticmethod
    def _run_hidden(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
        """Run a short helper without letting it open a console window."""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                               stdin=subprocess.DEVNULL, timeout=timeout,
                               creationflags=NO_WINDOW)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except Exception as e:  # missing binary, timeout, ...
            return -1, str(e)

    def _grab_frame(self, video: pathlib.Path, dest: pathlib.Path) -> bool:
        for at in ("0.5", "0"):
            code, _ = self._run_hidden(
                [FFMPEG, "-v", "error", "-y", "-ss", at, "-i", str(video),
                 "-frames:v", "1", str(dest)])
            if code == 0 and dest.exists():
                return True
        return False

    # ----------------------------------------------------------------- misc
    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True
        # Kill the whole tree, not just the CLI: ffmpeg and the renderer are
        # separate processes and would otherwise keep holding the GPU.
        if self.proc is not None:
            self.tick.stop()
            self._kill_tree(self.proc)
            if self.current is not None:
                self.current["status"] = "已取消"
                self.current["note"] = "窗口关闭"
        super().closeEvent(event)


def selftest() -> int:
    """Run one image and one video through the same dispatch the window uses."""
    samples = ROOT / "samples"
    out_dir = ROOT / "outputs/gui"
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = [(samples / "photo.jpg", out_dir / "selftest_image.png"),
             (samples / "testvfr_audio.mp4", out_dir / "selftest_video.mp4")]
    for src, dst in cases:
        if not src.exists():
            print(f"missing sample: {src}")
            return 1
        cmd = [sys.executable, CLI_ARG, "render", str(src), "-o", str(dst),
               "--overwrite", "--json", "--quiet"]
        rc = subprocess.run(cmd, creationflags=NO_WINDOW).returncode
        print(f"{src.name}: rc={rc} -> {dst}")
        if rc != 0:
            return rc
    return 0


def uitest() -> int:
    """Drive the window's own job path without showing it.

    Covers the three paths that matter: a still image, a video (sampling the
    progress column while it runs), and cancelling a long job mid-flight --
    including the check that no helper process survives the cancel.
    """
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication(sys.argv)
    win = MainWindow()
    failures: list[str] = []
    clock = time

    def run_until_idle(limit: float, sampler=None) -> None:
        end = clock.monotonic() + limit
        while win.proc is not None and clock.monotonic() < end:
            app.processEvents()
            if sampler is not None:
                sampler()
            clock.sleep(0.03)
        app.processEvents()

    # 1) still image
    image = ROOT / "samples/photo.jpg"
    if not image.exists():
        print(f"missing sample {image}")
        return 1
    win._enqueue(image)
    win._start()
    run_until_idle(180)
    job = win.jobs[0]
    ok_image = job["status"] == "完成" and pathlib.Path(job["output"]).exists()
    preview = win.compare.after is not None and not win.compare.after.isNull()
    print(f"image : status={job['status']} {job['seconds']:.2f}s preview={preview}")
    if not (ok_image and preview):
        failures.append("image render/preview")

    # 2) video with progress sampling
    video = ROOT / "samples/minimax_h3.mp4"
    if not video.exists():
        video = ROOT / "samples/testvfr_audio.mp4"
    if not video.exists():
        print(f"missing sample {video}")
        return 1
    win.jobs.clear()
    win._sync_table()
    win._enqueue(video)
    win._start()
    samples: list[float] = []

    def sample() -> None:
        job = win.current
        if job and job.get("progress") is not None:
            samples.append(job["progress"])

    run_until_idle(300, sample)
    job = win.jobs[0]
    mid = [s for s in samples if 0 < s < 100]
    latency = job.get("latency")
    wall = job.get("wall")
    ok_video = (job["status"] == "完成" and bool(mid)
                and latency is not None and 0.05 < latency < 500
                # wall covers read+GPU+write, so it cannot be smaller than the
                # GPU part; if it were, the split would be lying.
                and wall is not None and wall >= latency)
    print(f"video : status={job['status']} {job['seconds']:.2f}s "
          f"progress samples={len(samples)} last={samples[-1] if samples else None} "
          f"gpu={latency}ms wall={wall}ms")
    if not ok_video:
        failures.append("video progress/latency/wall")

    # 3) cancel: start a long job, then kill it and make sure nothing survives
    win.jobs.clear()
    win._sync_table()
    win._enqueue(video)
    win._start()
    clock.sleep(1.5)
    app.processEvents()
    was_running = win.proc is not None
    nr_app_question = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)  # type: ignore[assignment]
    try:
        win._cancel_current()
    finally:
        QMessageBox.question = nr_app_question  # type: ignore[assignment]
    run_until_idle(15)
    job = win.jobs[0]
    leftovers = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq nr_stream.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW,
    ).stdout.strip()
    orphan = "nr_stream.exe" in leftovers.lower()
    ok_cancel = was_running and job["status"] == "已取消" and not orphan
    print(f"cancel: was_running={was_running} status={job['status']} orphan_stream={orphan}")
    if not ok_cancel:
        failures.append("cancel/cleanup")

    # 4) batch with a 2 s cap: both clips must finish, one after the other
    win.jobs.clear()
    win._sync_table()
    win.max_seconds.setValue(2.0)
    for name in ("testvfr_audio.mp4", "input.mp4"):
        candidate = ROOT / "samples" / name
        if candidate.exists():
            win._enqueue(candidate)
    win._start()
    deadline = clock.monotonic() + 120
    while (not all(j["status"] in ("完成", "失败", "已取消") for j in win.jobs)
           and clock.monotonic() < deadline):
        app.processEvents()
        clock.sleep(0.03)
    app.processEvents()
    statuses = [j["status"] for j in win.jobs]
    ok_batch = len(statuses) >= 2 and all(s == "完成" for s in statuses)
    print(f"batch : {len(statuses)} jobs -> {statuses} (cap 2s)")
    if not ok_batch:
        failures.append("batch")
    win.max_seconds.setValue(0.0)

    gpu = win.gpu_label.text()
    print(f"gpu   : {gpu}")

    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("uitest: all checks passed")
    return 0


def uishot(out: str, hover: str | None, job_path: str | None = None) -> int:
    """Grab the real window into a PNG.

    With --hover it forces the tooltip open first (a widget property check
    cannot prove the hint renders), and with --job it starts a render so the
    progress column can be seen live.
    """
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    # Without raising, another application's window can sit on top and the grab
    # captures that instead of ours.
    win.raise_()
    win.activateWindow()
    clock = time
    for _ in range(20):
        app.processEvents()
        clock.sleep(0.03)
    if job_path:
        target = pathlib.Path(job_path)
        if not target.is_absolute():
            target = ROOT / job_path
        win._enqueue(target)
        win._start()
        for _ in range(65):   # ~2 s: enough to be mid-clip, not finished
            app.processEvents()
            clock.sleep(0.03)
    if hover:
        widget = getattr(win, hover, None)
        if widget is not None:
            pos = widget.mapToGlobal(widget.rect().center())
            QCursor.setPos(pos)
            QToolTip.showText(pos, widget.toolTip(), widget)
            for _ in range(30):
                app.processEvents()
                clock.sleep(0.03)
    for _ in range(10):
        app.processEvents()
        clock.sleep(0.03)

    if hover:
        # A tooltip lives in its own top-level window, so only a screen grab can
        # capture it; crop to our frame afterwards.
        pixmap = app.primaryScreen().grabWindow(0)
        frame = win.frameGeometry()
        dpr = pixmap.devicePixelRatio()
        img = pixmap.toImage().copy(
            int(frame.x() * dpr), int(frame.y() * dpr),
            int(frame.width() * dpr), int(frame.height() * dpr),
        )
    else:
        # Rendering the widget ourselves means an unrelated window sitting on top
        # cannot end up in the picture.
        img = win.grab().toImage()
    img.save(out)
    print(f"uishot: {out}  {img.width()}x{img.height()}")
    if win.proc is not None:
        win._kill_tree(win.proc)
    return 0


def main() -> int:
    force_utf8_stdio()
    argv = sys.argv
    dispatched = dispatch(argv)
    if dispatched is not None:
        return dispatched
    if "--selftest" in argv:
        return selftest()
    if "--render" in argv:
        return render_cli(argv)
    if "--uitest" in argv:
        return uitest()
    if "--uishot" in argv:
        idx = argv.index("--uishot")
        out = argv[idx + 1] if len(argv) > idx + 1 else "uishot.png"
        rest = argv[idx + 1:]
        hover = rest[rest.index("--hover") + 1] if "--hover" in rest[:-1] else None
        job = rest[rest.index("--job") + 1] if "--job" in rest[:-1] else None
        return uishot(out, hover, job)

    unknown = [a for a in argv[1:] if a.startswith("-")]
    if unknown:
        print(f"unknown argument(s): {' '.join(unknown)}\n"
              "usage: nr-app.exe [--selftest|--uitest|--uishot <png> [--hover <attr>] [--job <file>]"
              "|--render <input> -o <output> [options]]", file=sys.stderr)
        return 2

    hide_console()
    app = QApplication(argv)
    app.setApplicationName("nr-app")
    app.setStyleSheet(
        "QWidget{font-size:12px;}"
        "QGroupBox{font-weight:600;border:1px solid #d7dae0;border-radius:4px;margin-top:8px;padding-top:8px;}"
        "QGroupBox::title{subcontrol-origin: margin;left:10px;padding:0 4px;}"
        "QPushButton{padding:4px 10px;}"
        "QTableWidget{gridline-color:#e5e7eb;}"
    )
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
