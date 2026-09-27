"""==============================================================================
ShutdownTool — 基于 PySide6 + QFluentWidgets 的定时关机提示工具
==============================================================================
【这个程序是做什么的？】
    启动后，屏幕中央会弹出一个窗口，提示“计算机将在 XX 后自动关闭”。
    用户可以：
        · 点“已阅”        → 窗口滑出，倒计时继续；右下角出现圆形悬浮倒计时
        · 点“延迟 1 分钟” → 倒计时 +60 秒，窗口不关闭
        · 点“立即关机”    → 立即执行关机
        · 点“取消关机计划” → 撤销关机，退出程序
        · 点圆形悬浮倒计时 → 重新显示窗口
        · 拖动圆形悬浮倒计时 → 可移动到屏幕任意位置
    窗口隐藏时向右滑动 + 淡出；显示时反向滑入。

【代码结构】
    第 1 部分  Config              — 所有可调参数集中在此
    第 2 部分  Utils               — 通用工具函数（含启动音效）
    第 3 部分  SingleInstance      — 保证只运行一个实例
    第 4 部分  ShutdownMessageBox  — 弹窗 UI
    第 5 部分  CircularIndicator   — 圆形悬浮倒计时（基于 ProgressRing）
    第 6 部分  MainWindow          — 主控制器
    第 7 部分  main()              — 程序入口
"""

import os
import sys
import argparse

from PySide6.QtCore import (
    Qt, QTimer, QVariantAnimation, QEasingCurve, QPoint, QPointF, QProcess,
    QLockFile, QStandardPaths, Signal, QParallelAnimationGroup,
    QPropertyAnimation, QRectF,
)
from PySide6.QtGui import QColor, QPainter, QFont
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QWidget, QGraphicsDropShadowEffect,
    QFrame, QVBoxLayout, QHBoxLayout,
)
from qfluentwidgets import (
    BodyLabel, FluentIcon, PrimaryPushButton, ProgressBar, ProgressRing,
    PushButton, SubtitleLabel, Theme,
    setTheme, setThemeColor, isDarkTheme,
)
from qframelesswindow.utils import getSystemAccentColor


# ══════════════════════════════════════════════════════════════════════════════
# 第 1 部分：Config —— 所有可调参数集中在这里
# ══════════════════════════════════════════════════════════════════════════════

# ---------- 应用信息 ----------
APP_ID = "shutdowntool"
APP_NAME = "shutdowntool"
APP_DESCRIPTION = "定时关机提示工具"
SOCKET_NAME = f"{APP_ID}_socket"
LOCK_FILE = f"{APP_ID}.lock"

# ---------- 尺寸与时间 ----------
WIDTH = 600
TICK_MS = 1000
CLOSE_DELAY_MS = 500
SHUTDOWN_BUFFER_S = 0
DELAY_S = 60
NOTIFY_TIMEOUT_MS = 500
LOCK_TIMEOUT_MS = 100

# ---------- 窗口阴影 ----------
SHADOW_MARGIN = 24
SHADOW_BLUR = 24
SHADOW_OFFSET_Y = 4
SHADOW_COLOR = QColor(0, 0, 0, 90)

# ---------- 视觉细节 ----------
RADIUS = 8
INNER_RADIUS = RADIUS - 1

# ---------- 圆形悬浮倒计时 ----------
CIRCLE_SIZE          = 84       # 整个圆控件的直径
CIRCLE_RING          = 6        # 圆环线宽（对应 ProgressRing.strokeWidth）
CIRCLE_MARGIN_RIGHT  = 40       # 默认距屏幕右边的距离
CIRCLE_MARGIN_BOTTOM = 80       # 默认距屏幕底边的距离
CIRCLE_FADE_MS       = 220      # 悬浮圆淡入淡出时长

# 内部填充色（与弹窗背景色一致）
CIRCLE_FILL_LIGHT = "#FFFFFF"
CIRCLE_FILL_DARK  = "#2B2B2B"

# ---------- 窗口隐藏 / 显示动画 ----------
HIDE_ANIM_MS  = 260
SHOW_ANIM_MS  = 260
HIDE_SLIDE_PX = 140


# ══════════════════════════════════════════════════════════════════════════════
# 第 2 部分：Utils —— 通用工具函数
# ══════════════════════════════════════════════════════════════════════════════

def format_time(seconds: int) -> str:
    """把秒数格式化成易读的长字符串（用于弹窗正文）。"""
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{m} 分钟" if s == 0 else f"{m} 分 {s} 秒"
    return f"{seconds} 秒"


def format_time_short(seconds: int) -> str:
    """把秒数格式化成 mm:ss（用于悬浮圆中心）。"""
    m, s = divmod(max(0, seconds), 60)
    return f"{m}:{s:02d}"


def shutdown_now(delay: int = 0) -> None:
    """
    异步调用系统关机命令。
    使用 QProcess.startDetached 而非 os.system，避免阻塞主线程。
    """
    if sys.platform == "win32":
        QProcess.startDetached("shutdown", ["/s", "/f", "/t", str(delay)])
    else:
        QProcess.startDetached("shutdown", ["-h", "-t", str(delay)])


def cancel_shutdown() -> None:
    """撤销已经计划好的系统关机（仅 Windows 有效）。"""
    if sys.platform == "win32":
        QProcess.startDetached("shutdown", ["/a"])


def center_on_screen(widget: QWidget) -> None:
    """把顶层窗口居中到当前屏幕的可用区域。"""
    screen = widget.screen() or QApplication.primaryScreen()
    if screen is None:
        return
    geo = screen.availableGeometry()
    widget.adjustSize()
    widget.move(
        geo.x() + (geo.width() - widget.width()) // 2,
        geo.y() + (geo.height() - widget.height()) // 2,
    )


def play_startup_sound() -> None:
    """
    程序成功启动后播放 Windows 的提示音。
    非 Windows 平台静默返回，播放失败也不抛异常（不影响主流程）。
    """
    if sys.platform != "win32":
        return
    try:
        import winsound
    except ImportError:
        return

    media_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Media")
    for name in ("Windows Background.wav",):
        wav = os.path.join(media_dir, name)
        if os.path.isfile(wav):
            try:
                winsound.PlaySound(
                    wav,
                    winsound.SND_FILENAME
                    | winsound.SND_ASYNC
                    | winsound.SND_NODEFAULT,
                )
                return
            except RuntimeError:
                pass

    try:
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# 第 3 部分：SingleInstance —— 保证只有一个程序实例在运行
# ══════════════════════════════════════════════════════════════════════════════

class SingleInstance:
    """
    单实例控制器。
    两种机制配合：
        · QLockFile    — 保证同一时刻只有一个程序拿到锁
        · QLocalSocket — 让新启动的程序给已运行的程序发消息
    """

    def __init__(self) -> None:
        lock_path = os.path.join(
            QStandardPaths.writableLocation(QStandardPaths.TempLocation),
            LOCK_FILE,
        )
        self._lock = QLockFile(lock_path)

    def acquire(self) -> bool:
        """尝试获取锁；返回 False 表示已有实例在运行。"""
        return self._lock.tryLock(LOCK_TIMEOUT_MS)

    def release(self) -> None:
        """释放锁。"""
        self._lock.unlock()

    def notify_show(self) -> None:
        """向已运行的实例发送 "SHOW" 消息，让对方把窗口显示出来。"""
        sock = QLocalSocket()
        sock.connectToServer(SOCKET_NAME)
        if sock.waitForConnected(NOTIFY_TIMEOUT_MS):
            sock.write(b"SHOW")
            sock.waitForBytesWritten(NOTIFY_TIMEOUT_MS)
            sock.disconnectFromServer()


# ══════════════════════════════════════════════════════════════════════════════
# 第 4 部分：ShutdownMessageBox —— 弹窗 UI
# ══════════════════════════════════════════════════════════════════════════════
# 【视觉结构】
#
# ┌─────────────────────────────────────────────┐
# │  要关机吗？                                  │
# │  xxxxx；计算机将在…                          │ ← contentFrame（纯白）
# │  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░    │
# ├─────────────────────────────────────────────┤ ← 1px 分隔线
# │ [已阅][延迟 1 分钟][立即关机]   [取消关机计划] │
# │                                             │ ← buttonFrame（浅灰）
# └─────────────────────────────────────────────┘
#
# ──────────────────────────────────────────────────────────────────────────────

class ShutdownMessageBox(QWidget):
    """关机提示对话框。"""

    def __init__(self, countdown: int) -> None:
        super().__init__()
        # 状态
        self.remaining = countdown
        self.total = countdown
        self._progress_anim = None
        self._drag_offset = None

        # 构建 UI
        self._setup_window()
        self._setup_content()
        self._setup_buttons()
        self._apply_style()
        self.update_content()

    # ──────────────────────────────────────────────────────────────────────
    # 窗口构建
    # ──────────────────────────────────────────────────────────────────────
    def _setup_window(self) -> None:
        """创建最外层的窗口和圆角背景容器。"""
        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        # 外层布局，留出阴影边距
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN
        )

        # 圆角容器
        self.container = QFrame(self)
        self.container.setObjectName("shutdownContainer")
        self.container.setAttribute(Qt.WA_StyledBackground, True)
        self.container.setFixedWidth(WIDTH)
        self._attach_shadow()
        outer.addWidget(self.container)

        # container 内部：上下两块
        main_layout = QVBoxLayout(self.container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 内容区
        self.contentFrame = QFrame(self.container)
        self.contentFrame.setObjectName("contentFrame")
        self.contentFrame.setAttribute(Qt.WA_StyledBackground, True)
        main_layout.addWidget(self.contentFrame)

        # 按钮区
        self.buttonFrame = QFrame(self.container)
        self.buttonFrame.setObjectName("buttonFrame")
        self.buttonFrame.setAttribute(Qt.WA_StyledBackground, True)
        main_layout.addWidget(self.buttonFrame)

    def _attach_shadow(self) -> None:
        """给 container 添加柔和阴影。"""
        shadow = QGraphicsDropShadowEffect(self.container)
        shadow.setBlurRadius(SHADOW_BLUR)
        shadow.setOffset(0, SHADOW_OFFSET_Y)
        shadow.setColor(SHADOW_COLOR)
        self.container.setGraphicsEffect(shadow)

    def _apply_style(self) -> None:
        """根据当前主题应用配色样式。"""
        if isDarkTheme():
            container_bg = "#2B2B2B"
            container_border = "#3A3A3A"
            button_bg = "#1F1F1F"
        else:
            container_bg = "#FFFFFF"
            container_border = "#E5E5E5"
            button_bg = "#F5F5F5"

        self.container.setStyleSheet(f"""
            #shutdownContainer {{
                background-color: {container_bg};
                border: 1px solid {container_border};
                border-radius: {RADIUS}px;
            }}
            #contentFrame {{
                background-color: transparent;
                border: none;
            }}
            #buttonFrame {{
                background-color: {button_bg};
                border: none;
                border-top: 1px solid {container_border};
                border-bottom-left-radius: {INNER_RADIUS}px;
                border-bottom-right-radius: {INNER_RADIUS}px;
            }}
        """)

    # ──────────────────────────────────────────────────────────────────────
    # 内容与按钮
    # ──────────────────────────────────────────────────────────────────────
    def _setup_content(self) -> None:
        """构建上半部分：标题、描述、进度条。"""
        layout = QVBoxLayout(self.contentFrame)
        layout.setSpacing(8)
        layout.setContentsMargins(24, 24, 24, 20)

        self.contentLabel = BodyLabel("", self.contentFrame)
        self.contentLabel.setWordWrap(True)

        self.progressBar = ProgressBar(self.contentFrame)
        self.progressBar.setValue(100)

        layout.addWidget(SubtitleLabel("要关机吗？", self.contentFrame))
        layout.addWidget(self.contentLabel)
        layout.addSpacing(4)
        layout.addWidget(self.progressBar)

    def _setup_buttons(self) -> None:
        """构建下半部分：四个操作按钮。"""
        self.accept_btn = PrimaryPushButton(
            FluentIcon.ACCEPT, "已阅", self.buttonFrame
        )
        self.delay_btn = PushButton(
            FluentIcon.HISTORY, "延迟 1 分钟", self.buttonFrame
        )
        self.shutdown_btn = PushButton(
            FluentIcon.POWER_BUTTON, "立即关机", self.buttonFrame
        )
        self.cancel_btn = PushButton(
            FluentIcon.CLOSE, "取消关机计划", self.buttonFrame
        )

        row = QHBoxLayout(self.buttonFrame)
        row.setContentsMargins(24, 16, 24, 16)
        row.setSpacing(8)
        row.addWidget(self.accept_btn)
        row.addWidget(self.delay_btn)
        row.addWidget(self.shutdown_btn)
        row.addStretch(1)
        row.addWidget(self.cancel_btn)

    # ──────────────────────────────────────────────────────────────────────
    # 内容与进度更新
    # ──────────────────────────────────────────────────────────────────────
    def update_content(
        self, remaining: int | None = None, total: int | None = None
    ) -> None:
        """刷新显示的剩余时间和进度条。"""
        if remaining is not None:
            self.remaining = remaining
        if total is not None:
            self.total = total

        self.contentLabel.setText(
            f"当前为放学时段；计算机将在 {format_time(self.remaining)}后自动关闭。"
        )
        self._animate_progress(self._target_progress())

    def _target_progress(self) -> int:
        """计算进度条目标百分比（0-100 的整数）。"""
        if self.total <= 0:
            return 0
        return max(0, min(100, round(self.remaining * 100 / self.total)))

    def _animate_progress(self, target: int) -> None:
        """让进度条在 TICK_MS 毫秒内平滑过渡到目标值。"""
        if self._progress_anim is None:
            self._progress_anim = QVariantAnimation(self)
            self._progress_anim.setEasingCurve(QEasingCurve.Type.Linear)
            self._progress_anim.valueChanged.connect(
                lambda v: self.progressBar.setValue(int(v))
            )
        self._progress_anim.stop()
        self._progress_anim.setDuration(TICK_MS)
        self._progress_anim.setStartValue(self.progressBar.value())
        self._progress_anim.setEndValue(target)
        self._progress_anim.start()

    # ──────────────────────────────────────────────────────────────────────
    # 拖动窗口
    # ──────────────────────────────────────────────────────────────────────
    def mousePressEvent(self, event) -> None:
        """鼠标按下时记录光标相对窗口左上角的偏移。"""
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """按住鼠标移动时，把窗口移动到新位置。"""
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        """松开鼠标结束拖动。"""
        self._drag_offset = None
        super().mouseReleaseEvent(event)


# ══════════════════════════════════════════════════════════════════════════════
# 第 5 部分：CircularIndicator —— 圆形悬浮倒计时（基于 ProgressRing）
# ══════════════════════════════════════════════════════════════════════════════
# 【设计】
#   直接继承 qfluentwidgets 的 ProgressRing：
#       · 圆环的描边、圆角端帽、主题色、平滑动画，全部由 ProgressRing 负责
#       · 圆环颜色自动跟随 setThemeColor()，即系统强调色
#       · 轨道颜色自动跟随主题（浅色 / 深色）
#   我们只在它基础上：
#       · 加一层内部纯色填充（与弹窗背景一致）
#       · 在中心绘制电源图标 + 剩余时间文字
#       · 覆盖鼠标事件，实现“拖动 / 单击”
#
# 【视觉结构】
#
#        ╭───────╮
#      ╱           ╲
#     │    ⏻        │   ← 中心：FluentIcon.POWER_BUTTON
#     │   3:25      │   ← 中心：剩余时间 mm:ss
#      ╲           ╱
#        ╰───────╯
#       ↑         ↑
#       |         └── 剩余部分：系统强调色，从顶部顺时针
#       └──────────── 轨道：跟随主题的灰
#
# 交互：
#   · 拖动 → 移动位置
#   · 单击（未移动）→ 发出 clicked 信号
#
# ──────────────────────────────────────────────────────────────────────────────

class CircularIndicator(ProgressRing):
    """
    圆形悬浮倒计时。
    继承 qfluentwidgets.ProgressRing，复用其圆环渲染、主题色、平滑动画。
    """

    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()

        # ---- 窗口标志：置顶 / 无边框 / 不抢焦点 ----
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        # ---- 尺寸与外观 ----
        self.setFixedSize(CIRCLE_SIZE, CIRCLE_SIZE)
        self.setStrokeWidth(CIRCLE_RING)
        self.setTextVisible(False)                 # 隐藏默认的百分比文字
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点击重新显示关机提示，拖动可移动位置")

        # ---- 主题相关（启动时快照一次）----
        dark = isDarkTheme()
        self._fill_color = QColor(CIRCLE_FILL_DARK if dark else CIRCLE_FILL_LIGHT)
        self._fg_color = QColor("#FFFFFF" if dark else "#000000")
        self._icon = FluentIcon.POWER_BUTTON.icon(
            Theme.DARK if dark else Theme.LIGHT
        )

        # ---- 状态 ----
        self._remaining_text = "0:00"

        # ---- 拖动状态 ----
        self._press_global = None       # 按下时光标的全局坐标
        self._drag_offset = None        # 光标相对控件左上角的偏移
        self._moved = False             # 本次按下是否产生过拖动

    # ---------- 对外接口 ----------
    def set_progress(self, ratio: float) -> None:
        """ratio = 剩余时间 / 总时间，0.0 ~ 1.0（内部转换为 0~100）。"""
        ratio = max(0.0, min(1.0, ratio))
        v = int(round(ratio * 100))
        if self.value() == v:
            return
        self.setValue(v)                # ProgressRing 会平滑过渡

    def set_remaining(self, seconds: int) -> None:
        """更新中心显示的剩余时间文本。"""
        text = format_time_short(seconds)
        if text == self._remaining_text:
            return
        self._remaining_text = text
        self.update()

    # ---------- 交互 ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._drag_offset = (
                self._press_global - self.frameGeometry().topLeft()
            )
            self._moved = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            now = event.globalPosition().toPoint()
            if (now - self._press_global).manhattanLength() > 4:
                self._moved = True
            self.move(now - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if not self._moved:
                self.clicked.emit()
            self._press_global = None
            self._drag_offset = None
            self._moved = False
        super().mouseReleaseEvent(event)

    # ---------- 绘制 ----------
    def paintEvent(self, event):
        # 1) 内部填充：一个跟弹窗背景同色的实心圆
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(self._fill_color)
        r = (self.width() - 2 * CIRCLE_RING) / 2.0
        p.drawEllipse(
            QPointF(self.width() / 2.0, self.height() / 2.0), r, r
        )
        p.end()

        # 2) 圆环（ProgressRing 全权负责：主题色 + 轨道色 + 平滑动画）
        super().paintEvent(event)

        # 3) 中心图标 + 剩余时间文字
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        w, h = self.width(), self.height()
        icon_size = int(w * 0.30)
        icon_x = (w - icon_size) // 2
        icon_y = int(h * 0.24)
        self._icon.paint(p, icon_x, icon_y, icon_size, icon_size)

        p.setPen(self._fg_color)
        f = QFont(self.font())
        f.setPointSize(9)
        f.setBold(True)
        p.setFont(f)
        text_rect = QRectF(0, icon_y + icon_size - 2, w, 20)
        p.drawText(
            text_rect,
            Qt.AlignHCenter | Qt.AlignTop,
            self._remaining_text,
        )
        p.end()


# ══════════════════════════════════════════════════════════════════════════════
# 第 6 部分：MainWindow —— 主控制器
# ══════════════════════════════════════════════════════════════════════════════

class MainWindow(QWidget):
    """整个程序的中枢：串联对话框、悬浮圆、定时器、单实例。"""

    def __init__(self, countdown: int, single_instance: SingleInstance) -> None:
        super().__init__()
        # ---- 状态 ----
        self._si = single_instance
        self.remaining = countdown
        self.total = countdown
        self._saved_pos = None           # 弹窗“正常显示”时的位置
        self._anim_state = "idle"        # idle / showing / hiding
        self._show_group = None
        self._hide_group = None
        self._circle_anim = None
        self._circle_placed = False      # 悬浮圆是否已经放到默认位置

        # ---- 宿主窗口（不可见）----
        self.setWindowFlags(Qt.Tool)
        self.resize(1, 1)

        # ---- 主题（只在启动时应用一次）----
        self._apply_theme()

        # ---- UI 组件 ----
        self._setup_message_box(countdown)
        self._setup_circular_indicator()
        self._setup_single_instance_server()
        self._setup_timer()

        # ---- 首次显示 ----
        self.show_reminder()

    # ──────────────────────────────────────────────────────────────────────
    # 主题
    # ──────────────────────────────────────────────────────────────────────
    def _apply_theme(self) -> None:
        """应用主题：根据启动时的系统状态选择深浅色。"""
        setTheme(Theme.AUTO)
        if sys.platform in ("win32", "darwin"):
            setThemeColor(getSystemAccentColor(), save=False)

    # ──────────────────────────────────────────────────────────────────────
    # 组件初始化
    # ──────────────────────────────────────────────────────────────────────
    def _setup_message_box(self, countdown: int) -> None:
        """创建对话框并连接按钮信号。"""
        self.message_box = ShutdownMessageBox(countdown)
        self.message_box.accept_btn.clicked.connect(self.on_accept)
        self.message_box.shutdown_btn.clicked.connect(self.on_shutdown_now)
        self.message_box.delay_btn.clicked.connect(self.on_delay_clicked)
        self.message_box.cancel_btn.clicked.connect(self.cancel_shutdown)

    def _setup_circular_indicator(self) -> None:
        """创建圆形悬浮倒计时，点击它重新显示窗口。"""
        self.circular = CircularIndicator()
        self.circular.clicked.connect(self.show_reminder)
        self.circular.hide()

    def _setup_single_instance_server(self) -> None:
        """启动本地 socket 服务，接收其他实例的唤醒请求。"""
        self.server = QLocalServer(self)
        self.server.removeServer(SOCKET_NAME)
        self.server.listen(SOCKET_NAME)
        self.server.newConnection.connect(self._on_new_connection)

    def _setup_timer(self) -> None:
        """启动倒计时定时器。"""
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(TICK_MS)

    # ──────────────────────────────────────────────────────────────────────
    # 显示 / 隐藏窗口
    # ──────────────────────────────────────────────────────────────────────
    def show_reminder(self) -> None:
        """显示并置顶对话框（带从右滑入 + 淡入动画）。"""
        if self._anim_state == "showing":
            return
        if self.message_box.isVisible() and self._anim_state == "idle":
            self.message_box.raise_()
            self.message_box.activateWindow()
            return
        self._animate_show()

    # ──────────────────────────────────────────────────────────────────────
    # 显示 / 隐藏动画
    # ──────────────────────────────────────────────────────────────────────
    def _animate_show(self) -> None:
        """弹窗从右侧滑入 + 淡入；同时把悬浮圆淡出。"""
        self._anim_state = "showing"
        self._hide_circular()

        if self._saved_pos is None:
            center_on_screen(self.message_box)
            self._saved_pos = self.message_box.pos()

        start_pos = self._saved_pos + QPoint(HIDE_SLIDE_PX, 0)

        self.message_box.move(start_pos)
        self.message_box.setWindowOpacity(0.0)
        self.message_box.show()
        self.message_box.raise_()
        self.message_box.activateWindow()

        self._show_group = QParallelAnimationGroup(self)

        a_pos = QPropertyAnimation(self.message_box, b"pos", self)
        a_pos.setDuration(SHOW_ANIM_MS)
        a_pos.setStartValue(start_pos)
        a_pos.setEndValue(self._saved_pos)
        a_pos.setEasingCurve(QEasingCurve.Type.OutCubic)

        a_op = QPropertyAnimation(self.message_box, b"windowOpacity", self)
        a_op.setDuration(SHOW_ANIM_MS)
        a_op.setStartValue(0.0)
        a_op.setEndValue(1.0)
        a_op.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._show_group.addAnimation(a_pos)
        self._show_group.addAnimation(a_op)
        self._show_group.finished.connect(self._on_show_done)
        self._show_group.start()

    def _on_show_done(self) -> None:
        self.message_box.setWindowOpacity(1.0)
        self._anim_state = "idle"

    def _animate_hide(self) -> None:
        """弹窗向右滑出 + 淡出；结束后显示悬浮圆。"""
        if self._anim_state != "idle":
            return
        self._anim_state = "hiding"
        self._saved_pos = self.message_box.pos()

        end_pos = self._saved_pos + QPoint(HIDE_SLIDE_PX, 0)

        self._hide_group = QParallelAnimationGroup(self)

        a_pos = QPropertyAnimation(self.message_box, b"pos", self)
        a_pos.setDuration(HIDE_ANIM_MS)
        a_pos.setStartValue(self._saved_pos)
        a_pos.setEndValue(end_pos)
        a_pos.setEasingCurve(QEasingCurve.Type.InCubic)

        a_op = QPropertyAnimation(self.message_box, b"windowOpacity", self)
        a_op.setDuration(HIDE_ANIM_MS)
        a_op.setStartValue(1.0)
        a_op.setEndValue(0.0)
        a_op.setEasingCurve(QEasingCurve.Type.InCubic)

        self._hide_group.addAnimation(a_pos)
        self._hide_group.addAnimation(a_op)
        self._hide_group.finished.connect(self._on_hide_done)
        self._hide_group.start()

    def _on_hide_done(self) -> None:
        self.message_box.hide()
        self.message_box.setWindowOpacity(1.0)
        self.message_box.move(self._saved_pos)   # 静默复位
        self._anim_state = "idle"
        self._show_circular()

    # ──────────────────────────────────────────────────────────────────────
    # 悬浮圆：定位、出现、消失
    # ──────────────────────────────────────────────────────────────────────
    def _default_circle_pos(self) -> QPoint:
        """计算右下角的默认位置（仅在首次显示时使用）。"""
        screen = self.message_box.screen() or QApplication.primaryScreen()
        if screen is None:
            return QPoint(0, 0)
        geo = screen.availableGeometry()
        return QPoint(
            geo.right() - CIRCLE_SIZE - CIRCLE_MARGIN_RIGHT + 1,
            geo.bottom() - CIRCLE_SIZE - CIRCLE_MARGIN_BOTTOM + 1,
        )

    def _show_circular(self) -> None:
        """让悬浮圆淡入；首次显示时先放到右下角默认位置。"""
        if not self._circle_placed:
            self.circular.move(self._default_circle_pos())
            self._circle_placed = True

        # 同步最新状态
        self.circular.set_progress(self._progress_ratio())
        self.circular.set_remaining(self.remaining)

        self.circular.setWindowOpacity(0.0)
        self.circular.show()
        self.circular.raise_()

        anim = QPropertyAnimation(self.circular, b"windowOpacity", self)
        anim.setDuration(CIRCLE_FADE_MS)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.start()
        self._circle_anim = anim

    def _hide_circular(self) -> None:
        """让悬浮圆淡出后隐藏。"""
        if not self.circular.isVisible():
            return

        anim = QPropertyAnimation(self.circular, b"windowOpacity", self)
        anim.setDuration(CIRCLE_FADE_MS)
        anim.setStartValue(self.circular.windowOpacity())
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim.finished.connect(self._on_circle_hide_done)
        anim.start()
        self._circle_anim = anim

    def _on_circle_hide_done(self) -> None:
        self.circular.hide()
        self.circular.setWindowOpacity(1.0)

    def _progress_ratio(self) -> float:
        if self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.remaining / self.total))

    # ──────────────────────────────────────────────────────────────────────
    # 单实例消息处理
    # ──────────────────────────────────────────────────────────────────────
    def _on_new_connection(self) -> None:
        """有新的程序实例启动并尝试连接时触发。"""
        sock = self.server.nextPendingConnection()
        if not sock:
            return
        if sock.waitForReadyRead(NOTIFY_TIMEOUT_MS):
            if sock.readAll().data() == b"SHOW":
                self.show_reminder()
        sock.disconnectFromServer()

    # ──────────────────────────────────────────────────────────────────────
    # 倒计时逻辑
    # ──────────────────────────────────────────────────────────────────────
    def _tick(self) -> None:
        """每秒触发一次：剩余秒数减 1，刷新 UI 或执行关机。"""
        self.remaining -= 1
        if self.remaining > 0:
            self._refresh_ui()
        else:
            self.timer.stop()
            self.message_box.update_content(0, self.total)
            self.circular.set_progress(0.0)
            self.circular.set_remaining(0)
            shutdown_now()

    def _refresh_ui(self) -> None:
        """把最新的剩余秒数同步到对话框和悬浮圆。"""
        self.message_box.update_content(self.remaining, self.total)
        self.circular.set_progress(self._progress_ratio())
        self.circular.set_remaining(self.remaining)

    # ──────────────────────────────────────────────────────────────────────
    # 按钮回调
    # ──────────────────────────────────────────────────────────────────────
    def on_accept(self) -> None:
        """“已阅”：隐藏窗口，倒计时继续，右下角出现悬浮圆。"""
        self._animate_hide()

    def on_delay_clicked(self) -> None:
        """“延迟 1 分钟”：剩余时间和总时间都 +DELAY_S，窗口保持显示。"""
        self.remaining += DELAY_S
        self.total += DELAY_S
        self._refresh_ui()

    def on_shutdown_now(self) -> None:
        """“立即关机”：停止倒计时，延迟几秒后关机。"""
        self.timer.stop()
        shutdown_now(SHUTDOWN_BUFFER_S)

    def cancel_shutdown(self) -> None:
        """“取消关机计划”：撤销系统关机命令，关闭窗口，稍后退出。"""
        cancel_shutdown()
        self.message_box.close()
        self.circular.close()
        QTimer.singleShot(CLOSE_DELAY_MS, self._quit)

    # ──────────────────────────────────────────────────────────────────────
    # 退出清理
    # ──────────────────────────────────────────────────────────────────────
    def _quit(self) -> None:
        """依次清理资源，最后退出应用。"""
        self.timer.stop()
        self.circular.close()
        self.server.close()
        self._si.release()
        QApplication.quit()


# ══════════════════════════════════════════════════════════════════════════════
# 第 7 部分：main() —— 程序入口
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """程序主入口。"""
    # ---- 1. 解析命令行参数 ----
    parser = argparse.ArgumentParser(description=APP_DESCRIPTION)
    parser.add_argument(
        "--countdown",
        type=int,
        default=15,
        help="默认倒计时时长（秒）",
    )
    args = parser.parse_args()
    if args.countdown <= 0:
        print("错误：--countdown 必须为大于 0 的整数")
        sys.exit(1)

    # ---- 2. 创建 QApplication ----
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # 窗口全隐藏时不退出

    # ---- 3. 单实例检查 ----
    si = SingleInstance()
    if not si.acquire():
        si.notify_show()
        sys.exit(0)

    # ---- 4. 创建主窗口 ----
    window = MainWindow(args.countdown, si)

    # ---- 5. 播放启动音效 ----
    play_startup_sound()

    # ---- 6. 进入事件循环 ----
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
