"""==============================================================================
ShutdownTool — 基于 PySide6 + QFluentWidgets 的定时关机提示工具
==============================================================================
【这个程序是做什么的？】
    启动后，屏幕中央会弹出一个窗口，提示“计算机将在 XX 后自动关闭”。
    用户可以：
        · 点“已阅”        → 窗口向下淡出，倒计时继续；右下角出现圆形悬浮倒计时
        · 点“延迟 1 分钟” → 倒计时 +60 秒，窗口不关闭
        · 点“立即关机”    → 立即执行关机
        · 点“取消关机计划” → 撤销关机，退出程序
        · 点圆形悬浮倒计时 → 重新显示窗口
        · 拖动圆形悬浮倒计时 → 可移动到屏幕任意位置

【代码结构】
    第 1 部分  Config              — 所有可调参数集中在此
    第 2 部分  Utils               — 通用工具函数（含启动音效）
    第 3 部分  SingleInstance      — 保证只运行一个实例
    第 4 部分  ShutdownMessageBox  — 弹窗 UI
    第 5 部分  CircularIndicator   — 圆形悬浮倒计时
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
CIRCLE_RING          = 6        # 圆环线宽
CIRCLE_MARGIN_RIGHT  = 40       # 默认距屏幕右边的距离
CIRCLE_MARGIN_BOTTOM = 80       # 默认距屏幕底边的距离
CIRCLE_FADE_MS       = 220      # 悬浮圆淡入淡出时长

# 内部填充色（与弹窗背景色一致）
CIRCLE_FILL_LIGHT = "#FFFFFF"
CIRCLE_FILL_DARK  = "#2B2B2B"

# ---------- 窗口隐藏 / 显示动画 ----------
# 竖直方向滑动：隐藏时向下、显示时从下方滑入
HIDE_ANIM_MS   = 260
SHOW_ANIM_MS   = 260
HIDE_SLIDE_PX  = 120    # 竖直滑动距离


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

    def _setup_window(self) -> None:
        """创建最外层的窗口和圆角背景容器。"""
        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN
        )

        self.container = QFrame(self)
        self.container.setObjectName("shutdownContainer")
        self.container.setAttribute(Qt.WA_StyledBackground, True)
        self.container.setFixedWidth(WIDTH)
        self._attach_shadow()
        outer.addWidget(self.container)

        main_layout = QVBoxLayout(self.container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.contentFrame = QFrame(self.container)
        self.contentFrame.setObjectName("contentFrame")
        self.contentFrame.setAttribute(Qt.WA_StyledBackground, True)
        main_layout.addWidget(self.contentFrame)

        self.buttonFrame = QFrame(self.container)
        self.buttonFrame.setObjectName("buttonFrame")
        self.buttonFrame.setAttribute(Qt.WA_StyledBackground, True)
        main_layout.addWidget(self.buttonFrame)

    def _attach_shadow(self) -> None:
        shadow = QGraphicsDropShadowEffect(self.container)
        shadow.setBlurRadius(SHADOW_BLUR)
        shadow.setOffset(0, SHADOW_OFFSET_Y)
        shadow.setColor(SHADOW_COLOR)
        self.container.setGraphicsEffect(shadow)

    def _apply_style(self) -> None:
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

    def _setup_content(self) -> None:
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

    def update_content(
        self, remaining: int | None = None, total: int | None = None
    ) -> None:
        if remaining is not None:
            self.remaining = remaining
        if total is not None:
            self.total = total

        self.contentLabel.setText(
            f"当前为放学时段；计算机将在 {format_time(self.remaining)}后自动关闭。"
        )
        self._animate_progress(self._target_progress())

    def _target_progress(self) -> int:
        if self.total <= 0:
            return 0
        return max(0, min(100, round(self.remaining * 100 / self.total)))

    def _animate_progress(self, target: int) -> None:
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

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)


# ══════════════════════════════════════════════════════════════════════════════
# 第 5 部分：CircularIndicator —— 圆形悬浮倒计时
# ══════════════════════════════════════════════════════════════════════════════
# 【布局设计】
#
#   ┌─────────────────────┐
#   │       ╭────╮         │  ← 圆环外缘
#   │     ╱        ╲       │
#   │    │   ⏻     │       │  ← 图标：居中偏上，尺寸约 26% 直径
#   │    │         │       │     图标与下方文字间距很小，视觉上是一组
#   │    │  3:25   │       │  ← 时间：9pt 加粗，紧贴图标下方
#   │     ╲        ╱       │
#   │       ╰────╯         │
#   └─────────────────────┘
#
#   整体（图标 + 文字）在圆内垂直居中，视觉重心略偏上。
#   图标与文字之间的间距由 ICON_TEXT_GAP 控制。
#
# ──────────────────────────────────────────────────────────────────────────────

class CircularIndicator(ProgressRing):
    """
    圆形悬浮倒计时，继承 qfluentwidgets.ProgressRing。
    圆环由 ProgressRing 全权渲染（主题色 / 轨道色 / 平滑动画），
    本类只负责：
        · 内部纯色填充
        · 中心图标 + 剩余时间
        · 拖动 / 单击交互
    """

    clicked = Signal()

    # ---- 中心布局参数（相对于控件直径的比例）----
    ICON_RATIO      = 0.30      # 图标大小 ÷ 直径
    ICON_TEXT_GAP   = 4         # 图标与文字之间的间距（像素）
    TEXT_FONT_SIZE  = 10        # 时间文字字号
    TEXT_HEIGHT     = 16        # 时间文字占用的高度（像素）
    VISUAL_Y_BIAS   = -2        # 整体上移一点，视觉更居中

    def __init__(self) -> None:
        super().__init__()

        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self.setFixedSize(CIRCLE_SIZE, CIRCLE_SIZE)
        self.setStrokeWidth(CIRCLE_RING)
        self.setTextVisible(False)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点击重新显示关机提示，拖动可移动位置")

        dark = isDarkTheme()
        self._fill_color = QColor(CIRCLE_FILL_DARK if dark else CIRCLE_FILL_LIGHT)
        self._fg_color = QColor("#FFFFFF" if dark else "#000000")
        self._icon = FluentIcon.POWER_BUTTON.icon(
            Theme.DARK if dark else Theme.LIGHT
        )

        self._remaining_text = "0:00"

        self._press_global = None
        self._drag_offset = None
        self._moved = False

    # ---------- 对外接口 ----------
    def set_progress(self, ratio: float) -> None:
        ratio = max(0.0, min(1.0, ratio))
        v = int(round(ratio * 100))
        if self.value() == v:
            return
        self.setValue(v)

    def set_remaining(self, seconds: int) -> None:
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
        # 1) 内部填充：跟弹窗背景同色的实心圆
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(self._fill_color)
        r = (self.width() - 2 * CIRCLE_RING) / 2.0
        p.drawEllipse(
            QPointF(self.width() / 2.0, self.height() / 2.0), r, r
        )
        p.end()

        # 2) 圆环（ProgressRing 全权负责）
        super().paintEvent(event)

        # 3) 中心图标 + 时间文字（整体在圆内垂直居中，略偏上）
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        w, h = self.width(), self.height()

        # 图标尺寸
        icon_size = int(w * self.ICON_RATIO)
        # 整组（图标 + 间距 + 文字）的总高
        block_h = icon_size + self.ICON_TEXT_GAP + self.TEXT_HEIGHT
        # 整组在圆内垂直居中（加上视觉偏移）
        block_top = (h - block_h) // 2 + self.VISUAL_Y_BIAS

        icon_x = (w - icon_size) // 2
        icon_y = block_top
        self._icon.paint(p, icon_x, icon_y, icon_size, icon_size)

        # 时间文字
        p.setPen(self._fg_color)
        f = QFont(self.font())
        f.setPointSize(self.TEXT_FONT_SIZE)
        f.setBold(True)
        p.setFont(f)
        text_rect = QRectF(
            0,
            block_top + icon_size + self.ICON_TEXT_GAP,
            w,
            self.TEXT_HEIGHT,
        )
        p.drawText(
            text_rect,
            Qt.AlignHCenter | Qt.AlignVCenter,
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
        self._si = single_instance
        self.remaining = countdown
        self.total = countdown
        self._saved_pos = None
        self._anim_state = "idle"
        self._show_group = None
        self._hide_group = None
        self._circle_anim = None
        self._circle_placed = False

        self.setWindowFlags(Qt.Tool)
        self.resize(1, 1)

        self._apply_theme()
        self._setup_message_box(countdown)
        self._setup_circular_indicator()
        self._setup_single_instance_server()
        self._setup_timer()

        self.show_reminder()

    # ──────────────────────────────────────────────────────────────────────
    # 主题
    # ──────────────────────────────────────────────────────────────────────
    def _apply_theme(self) -> None:
        setTheme(Theme.AUTO)
        if sys.platform in ("win32", "darwin"):
            setThemeColor(getSystemAccentColor(), save=False)

    # ──────────────────────────────────────────────────────────────────────
    # 组件初始化
    # ──────────────────────────────────────────────────────────────────────
    def _setup_message_box(self, countdown: int) -> None:
        self.message_box = ShutdownMessageBox(countdown)
        self.message_box.accept_btn.clicked.connect(self.on_accept)
        self.message_box.shutdown_btn.clicked.connect(self.on_shutdown_now)
        self.message_box.delay_btn.clicked.connect(self.on_delay_clicked)
        self.message_box.cancel_btn.clicked.connect(self.cancel_shutdown)

    def _setup_circular_indicator(self) -> None:
        self.circular = CircularIndicator()
        self.circular.clicked.connect(self.show_reminder)
        self.circular.hide()

    def _setup_single_instance_server(self) -> None:
        self.server = QLocalServer(self)
        self.server.removeServer(SOCKET_NAME)
        self.server.listen(SOCKET_NAME)
        self.server.newConnection.connect(self._on_new_connection)

    def _setup_timer(self) -> None:
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(TICK_MS)

    # ──────────────────────────────────────────────────────────────────────
    # 显示 / 隐藏窗口
    # ──────────────────────────────────────────────────────────────────────
    def show_reminder(self) -> None:
        """显示并置顶对话框（从下方滑入 + 淡入）。"""
        if self._anim_state == "showing":
            return
        if self.message_box.isVisible() and self._anim_state == "idle":
            self.message_box.raise_()
            self.message_box.activateWindow()
            return
        self._animate_show()

    # ──────────────────────────────────────────────────────────────────────
    # 显示 / 隐藏动画（竖直方向）
    # ──────────────────────────────────────────────────────────────────────
    def _animate_show(self) -> None:
        """弹窗从下方滑入 + 淡入；同时把悬浮圆淡出。"""
        self._anim_state = "showing"
        self._hide_circular()

        if self._saved_pos is None:
            center_on_screen(self.message_box)
            self._saved_pos = self.message_box.pos()

        # 起点：比目标位置低 HIDE_SLIDE_PX 像素
        start_pos = self._saved_pos + QPoint(0, HIDE_SLIDE_PX)

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
        """弹窗向下滑出 + 淡出；结束后显示悬浮圆。"""
        if self._anim_state != "idle":
            return
        self._anim_state = "hiding"
        self._saved_pos = self.message_box.pos()

        # 终点：比原位置低 HIDE_SLIDE_PX 像素
        end_pos = self._saved_pos + QPoint(0, HIDE_SLIDE_PX)

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
        screen = self.message_box.screen() or QApplication.primaryScreen()
        if screen is None:
            return QPoint(0, 0)
        geo = screen.availableGeometry()
        return QPoint(
            geo.right() - CIRCLE_SIZE - CIRCLE_MARGIN_RIGHT + 1,
            geo.bottom() - CIRCLE_SIZE - CIRCLE_MARGIN_BOTTOM + 1,
        )

    def _show_circular(self) -> None:
        if not self._circle_placed:
            self.circular.move(self._default_circle_pos())
            self._circle_placed = True

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
        self.message_box.update_content(self.remaining, self.total)
        self.circular.set_progress(self._progress_ratio())
        self.circular.set_remaining(self.remaining)

    # ──────────────────────────────────────────────────────────────────────
    # 按钮回调
    # ──────────────────────────────────────────────────────────────────────
    def on_accept(self) -> None:
        """“已阅”：向下隐藏窗口，倒计时继续，右下角出现悬浮圆。"""
        self._animate_hide()

    def on_delay_clicked(self) -> None:
        self.remaining += DELAY_S
        self.total += DELAY_S
        self._refresh_ui()

    def on_shutdown_now(self) -> None:
        self.timer.stop()
        shutdown_now(SHUTDOWN_BUFFER_S)

    def cancel_shutdown(self) -> None:
        cancel_shutdown()
        self.message_box.close()
        self.circular.close()
        QTimer.singleShot(CLOSE_DELAY_MS, self._quit)

    # ──────────────────────────────────────────────────────────────────────
    # 退出清理
    # ──────────────────────────────────────────────────────────────────────
    def _quit(self) -> None:
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

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    si = SingleInstance()
    if not si.acquire():
        si.notify_show()
        sys.exit(0)

    window = MainWindow(args.countdown, si)
    play_startup_sound()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
