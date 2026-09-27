"""ShutdownTool — 基于 PySide6 + QFluentWidgets 的定时关机提示工具。

启动后屏幕中央弹出一个无边框提示窗口，提示计算机将在 XX 后自动关闭。
用户可选择：
    · 已阅           → 窗口向下淡出，倒计时继续；右下角出现圆形悬浮倒计时
    · 延迟 1 分钟    → 倒计时 +60s，窗口保持显示
    · 立即关机       → 立即触发系统关机
    · 取消关机计划   → 撤销关机命令，窗口淡出后退出

圆形悬浮倒计时支持：
    · 单击重新显示弹窗
    · 拖动到屏幕任意位置
"""

from __future__ import annotations

import argparse
import os
import sys
from enum import Enum, auto
from typing import Final

from PySide6.QtCore import (
    QEasingCurve,
    QLockFile,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QProcess,
    QPropertyAnimation,
    QRectF,
    QStandardPaths,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    PrimaryPushButton,
    ProgressBar,
    ProgressRing,
    PushButton,
    SubtitleLabel,
    Theme,
    isDarkTheme,
    setTheme,
    setThemeColor,
)
from qframelesswindow.utils import getSystemAccentColor


# ══════════════════════════════════════════════════════════════════════════════
# 1. 配置 —— 所有可调参数集中于此
# ══════════════════════════════════════════════════════════════════════════════

class AppMeta:
    """应用元信息与单实例通信常量。"""

    ID: Final = "shutdowntool"
    NAME: Final = "shutdowntool"
    DESCRIPTION: Final = "定时关机提示工具"
    SOCKET_NAME: Final = f"{ID}_socket"
    LOCK_FILE: Final = f"{ID}.lock"
    DEFAULT_COUNTDOWN: Final = 60


class Timing:
    """时间相关常量（毫秒或秒）。"""

    TICK_MS: Final = 1000              # 倒计时刷新周期
    CLOSE_DELAY_MS: Final = 500        # 窗口隐藏后到退出程序的延迟
    SHUTDOWN_BUFFER_S: Final = 0       # “立即关机”附加的缓冲秒数
    DELAY_S: Final = 60                # “延迟 1 分钟”的增量
    NOTIFY_TIMEOUT_MS: Final = 500     # 单实例通信超时
    LOCK_TIMEOUT_MS: Final = 100       # 文件锁获取超时


class DialogStyle:
    """弹窗外观与阴影常量。"""

    WIDTH: Final = 600
    RADIUS: Final = 8
    INNER_RADIUS: Final = RADIUS - 1
    SHADOW_MARGIN: Final = 24
    SHADOW_BLUR: Final = 24
    SHADOW_OFFSET_Y: Final = 4
    SHADOW_COLOR: Final = QColor(0, 0, 0, 90)


class CircleStyle:
    """圆形悬浮倒计时外观常量。"""

    SIZE: Final = 84
    RING: Final = 6
    MARGIN_RIGHT: Final = 40
    MARGIN_BOTTOM: Final = 80
    FADE_MS: Final = 220
    FILL_LIGHT: Final = "#FFFFFF"
    FILL_DARK: Final = "#2B2B2B"
    # 中心布局比例
    ICON_RATIO: Final = 0.23
    ICON_TEXT_GAP: Final = 4
    TEXT_FONT_SIZE: Final = 12
    TEXT_HEIGHT: Final = 16
    VISUAL_Y_BIAS: Final = -2


class AnimSpec:
    """窗口隐藏 / 显示动画常量。"""

    SHOW_MS: Final = 260
    HIDE_MS: Final = 260
    SLIDE_PX: Final = 120


class AnimationState(Enum):
    """弹窗动画状态机。"""

    IDLE = auto()
    SHOWING = auto()
    HIDING = auto()


# ══════════════════════════════════════════════════════════════════════════════
# 2. 工具函数
# ══════════════════════════════════════════════════════════════════════════════

def format_duration(seconds: int) -> str:
    """将秒数格式化为可读的长字符串（用于弹窗正文）。"""
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{m} 分钟" if s == 0 else f"{m} 分 {s} 秒"
    return f"{seconds} 秒"


def format_duration_short(seconds: int) -> str:
    """将秒数格式化为 mm:ss（用于悬浮圆中心）。"""
    m, s = divmod(max(0, seconds), 60)
    return f"{m}:{s:02d}"


def trigger_shutdown(delay: int = 0) -> None:
    """异步调用系统关机命令（不阻塞主线程）。"""
    if sys.platform == "win32":
        QProcess.startDetached("shutdown", ["/s", "/f", "/t", str(delay)])
    else:
        QProcess.startDetached("shutdown", ["-h", "-t", str(delay)])


def abort_shutdown() -> None:
    """撤销已经计划好的系统关机（仅 Windows 有效）。"""
    if sys.platform == "win32":
        QProcess.startDetached("shutdown", ["/a"])


def center_on_screen(widget: QWidget) -> None:
    """将顶层窗口居中到当前屏幕的可用区域。"""
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
    """程序启动后播放一次系统提示音（仅 Windows，失败静默）。"""
    if sys.platform != "win32":
        return
    try:
        import winsound
    except ImportError:
        return

    media_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Media")
    wav = os.path.join(media_dir, "Windows Background.wav")
    if os.path.isfile(wav):
        try:
            winsound.PlaySound(
                wav,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
            return
        except RuntimeError:
            pass

    try:
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# 3. SingleInstance —— 单实例控制
# ══════════════════════════════════════════════════════════════════════════════

class SingleInstance:
    """基于 QLockFile + QLocalSocket 的单实例控制器。"""

    def __init__(self) -> None:
        lock_path = os.path.join(
            QStandardPaths.writableLocation(QStandardPaths.TempLocation),
            AppMeta.LOCK_FILE,
        )
        self._lock = QLockFile(lock_path)

    def acquire(self) -> bool:
        """尝试获取锁；返回 False 表示已有实例正在运行。"""
        return self._lock.tryLock(Timing.LOCK_TIMEOUT_MS)

    def release(self) -> None:
        """释放文件锁。"""
        self._lock.unlock()

    def notify_existing(self) -> None:
        """通知已运行的实例显示弹窗。"""
        sock = QLocalSocket()
        sock.connectToServer(AppMeta.SOCKET_NAME)
        if sock.waitForConnected(Timing.NOTIFY_TIMEOUT_MS):
            sock.write(b"SHOW")
            sock.waitForBytesWritten(Timing.NOTIFY_TIMEOUT_MS)
            sock.disconnectFromServer()


# ══════════════════════════════════════════════════════════════════════════════
# 4. ShutdownDialog —— 关机提示对话框
# ══════════════════════════════════════════════════════════════════════════════

class ShutdownDialog(QWidget):
    """无边框圆角关机提示对话框。

    对外暴露四个语义化信号，避免外部直接访问内部按钮。
    """

    accepted = Signal()
    delayed = Signal()
    shutdown_requested = Signal()
    cancelled = Signal()

    def __init__(self, countdown: int) -> None:
        super().__init__()
        self.remaining = countdown
        self.total = countdown

        self._drag_offset: QPoint | None = None
        self._progress_anim: QVariantAnimation | None = None

        self._init_window()
        self._init_content()
        self._init_buttons()
        self._apply_style()
        self.update_content()

    # ---------- 构建 ----------
    def _init_window(self) -> None:
        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            DialogStyle.SHADOW_MARGIN,
            DialogStyle.SHADOW_MARGIN,
            DialogStyle.SHADOW_MARGIN,
            DialogStyle.SHADOW_MARGIN,
        )

        self.container = QFrame(self)
        self.container.setObjectName("shutdownContainer")
        self.container.setAttribute(Qt.WA_StyledBackground, True)
        self.container.setFixedWidth(DialogStyle.WIDTH)
        outer.addWidget(self.container)

        shadow = QGraphicsDropShadowEffect(self.container)
        shadow.setBlurRadius(DialogStyle.SHADOW_BLUR)
        shadow.setOffset(0, DialogStyle.SHADOW_OFFSET_Y)
        shadow.setColor(DialogStyle.SHADOW_COLOR)
        self.container.setGraphicsEffect(shadow)

        card = QVBoxLayout(self.container)
        card.setContentsMargins(0, 0, 0, 0)
        card.setSpacing(0)

        self.content_frame = QFrame(self.container)
        self.content_frame.setObjectName("contentFrame")
        self.content_frame.setAttribute(Qt.WA_StyledBackground, True)
        card.addWidget(self.content_frame)

        self.button_frame = QFrame(self.container)
        self.button_frame.setObjectName("buttonFrame")
        self.button_frame.setAttribute(Qt.WA_StyledBackground, True)
        card.addWidget(self.button_frame)

    def _init_content(self) -> None:
        layout = QVBoxLayout(self.content_frame)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(8)

        self.title_label = SubtitleLabel("要关机吗？", self.content_frame)
        self.content_label = BodyLabel("", self.content_frame)
        self.content_label.setWordWrap(True)

        self.progress_bar = ProgressBar(self.content_frame)
        self.progress_bar.setValue(100)

        layout.addWidget(self.title_label)
        layout.addWidget(self.content_label)
        layout.addSpacing(4)
        layout.addWidget(self.progress_bar)

    def _init_buttons(self) -> None:
        self.accept_btn = PrimaryPushButton(
            FluentIcon.ACCEPT, "已阅", self.button_frame
        )
        self.delay_btn = PushButton(
            FluentIcon.HISTORY, "延迟 1 分钟", self.button_frame
        )
        self.shutdown_btn = PushButton(
            FluentIcon.POWER_BUTTON, "立即关机", self.button_frame
        )
        self.cancel_btn = PushButton(
            FluentIcon.CLOSE, "取消关机计划", self.button_frame
        )

        self.accept_btn.clicked.connect(self.accepted.emit)
        self.delay_btn.clicked.connect(self.delayed.emit)
        self.shutdown_btn.clicked.connect(self.shutdown_requested.emit)
        self.cancel_btn.clicked.connect(self.cancelled.emit)

        row = QHBoxLayout(self.button_frame)
        row.setContentsMargins(24, 16, 24, 16)
        row.setSpacing(8)
        row.addWidget(self.accept_btn)
        row.addWidget(self.delay_btn)
        row.addWidget(self.shutdown_btn)
        row.addStretch(1)
        row.addWidget(self.cancel_btn)

    def _apply_style(self) -> None:
        if isDarkTheme():
            bg, border, btn_bg = "#2B2B2B", "#3A3A3A", "#1F1F1F"
        else:
            bg, border, btn_bg = "#FFFFFF", "#E5E5E5", "#F5F5F5"

        self.container.setStyleSheet(f"""
            #shutdownContainer {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: {DialogStyle.RADIUS}px;
            }}
            #contentFrame {{
                background-color: transparent;
                border: none;
            }}
            #buttonFrame {{
                background-color: {btn_bg};
                border: none;
                border-top: 1px solid {border};
                border-bottom-left-radius: {DialogStyle.INNER_RADIUS}px;
                border-bottom-right-radius: {DialogStyle.INNER_RADIUS}px;
            }}
        """)

    # ---------- 内容更新 ----------
    def update_content(
        self,
        remaining: int | None = None,
        total: int | None = None,
    ) -> None:
        if remaining is not None:
            self.remaining = remaining
        if total is not None:
            self.total = total

        self.content_label.setText(
            f"本节课程为自习；计算机将在 "
            f"{format_duration(self.remaining)}后自动关闭。"
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
                lambda v: self.progress_bar.setValue(int(v))
            )
        self._progress_anim.stop()
        self._progress_anim.setDuration(Timing.TICK_MS)
        self._progress_anim.setStartValue(self.progress_bar.value())
        self._progress_anim.setEndValue(target)
        self._progress_anim.start()

    # ---------- 拖动窗口 ----------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint()
                - self.frameGeometry().topLeft()
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
# 5. CircularIndicator —— 圆形悬浮倒计时
# ══════════════════════════════════════════════════════════════════════════════

class CircularIndicator(ProgressRing):
    """圆形悬浮倒计时。

    继承 ProgressRing：圆环由父类全权渲染（主题色 / 轨道色 / 平滑动画）。
    本类只负责：
        · 内部纯色填充
        · 中心图标 + 剩余时间
        · 拖动 / 单击交互
    """

    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()

        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self.setFixedSize(CircleStyle.SIZE, CircleStyle.SIZE)
        self.setStrokeWidth(CircleStyle.RING)
        self.setTextVisible(False)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点击重新显示关机提示，拖动可移动位置")

        dark = isDarkTheme()
        self._fill_color = QColor(
            CircleStyle.FILL_DARK if dark else CircleStyle.FILL_LIGHT
        )
        self._fg_color = QColor("#FFFFFF" if dark else "#000000")
        self._icon = FluentIcon.POWER_BUTTON.icon(
            Theme.DARK if dark else Theme.LIGHT
        )
        self._time_text = "0:00"

        self._press_global: QPoint | None = None
        self._drag_offset: QPoint | None = None
        self._moved = False

    # ---------- 对外接口 ----------
    def set_progress(self, ratio: float) -> None:
        ratio = max(0.0, min(1.0, ratio))
        value = int(round(ratio * 100))
        if self.value() != value:
            self.setValue(value)

    def set_remaining(self, seconds: int) -> None:
        text = format_duration_short(seconds)
        if text != self._time_text:
            self._time_text = text
            self.update()

    # ---------- 交互 ----------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._drag_offset = self._press_global - self.frameGeometry().topLeft()
            self._moved = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (
            self._drag_offset is not None
            and self._press_global is not None
            and event.buttons() & Qt.LeftButton
        ):
            now = event.globalPosition().toPoint()
            if (now - self._press_global).manhattanLength() > 4:
                self._moved = True
            self.move(now - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            if not self._moved:
                self.clicked.emit()
            self._press_global = None
            self._drag_offset = None
            self._moved = False
        super().mouseReleaseEvent(event)

    # ---------- 绘制 ----------
    def paintEvent(self, event) -> None:
        # 1) 内部实心圆（与弹窗背景同色）
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(self._fill_color)
        r = (self.width() - 2 * CircleStyle.RING) / 2.0
        p.drawEllipse(QPointF(self.width() / 2.0, self.height() / 2.0), r, r)
        p.end()

        # 2) 圆环（ProgressRing 全权渲染）
        super().paintEvent(event)

        # 3) 中心图标 + 时间文字
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        w, h = self.width(), self.height()
        icon_size = int(w * CircleStyle.ICON_RATIO)
        block_h = (
            icon_size + CircleStyle.ICON_TEXT_GAP + CircleStyle.TEXT_HEIGHT
        )
        block_top = (h - block_h) // 2 + CircleStyle.VISUAL_Y_BIAS

        self._icon.paint(
            p, (w - icon_size) // 2, block_top, icon_size, icon_size
        )

        p.setPen(self._fg_color)
        f = QFont(self.font())
        f.setPointSize(CircleStyle.TEXT_FONT_SIZE)
        f.setWeight(QFont.Weight.Normal)
        p.setFont(f)
        p.drawText(
            QRectF(
                0,
                block_top + icon_size + CircleStyle.ICON_TEXT_GAP,
                w,
                CircleStyle.TEXT_HEIGHT,
            ),
            Qt.AlignHCenter | Qt.AlignVCenter,
            self._time_text,
        )
        p.end()


# ══════════════════════════════════════════════════════════════════════════════
# 6. ShutdownController —— 主控制器
# ══════════════════════════════════════════════════════════════════════════════

class ShutdownController(QObject):
    """中枢控制器：串联对话框、悬浮圆、定时器与单实例服务。"""

    def __init__(self, countdown: int, single_instance: SingleInstance) -> None:
        super().__init__()
        self._si = single_instance
        self._remaining = countdown
        self._total = countdown
        self._dialog_anchor: QPoint | None = None
        self._anim_state: AnimationState = AnimationState.IDLE

        self._show_anim: QParallelAnimationGroup | None = None
        self._hide_anim: QParallelAnimationGroup | None = None
        self._circle_anim: QPropertyAnimation | None = None
        self._circle_placed = False

        self._apply_theme()
        self._create_dialog(countdown)
        self._create_indicator()
        self._create_server()
        self._create_timer()

        self.show_dialog()

    # ---------- 初始化 ----------
    def _apply_theme(self) -> None:
        setTheme(Theme.AUTO)
        if sys.platform in ("win32", "darwin"):
            setThemeColor(getSystemAccentColor(), save=False)

    def _create_dialog(self, countdown: int) -> None:
        self.dialog = ShutdownDialog(countdown)
        self.dialog.accepted.connect(self._on_accepted)
        self.dialog.delayed.connect(self._on_delayed)
        self.dialog.shutdown_requested.connect(self._on_shutdown_now)
        self.dialog.cancelled.connect(self._on_cancelled)

    def _create_indicator(self) -> None:
        self.indicator = CircularIndicator()
        self.indicator.clicked.connect(self.show_dialog)
        self.indicator.hide()

    def _create_server(self) -> None:
        self.server = QLocalServer(self)
        self.server.removeServer(AppMeta.SOCKET_NAME)
        self.server.listen(AppMeta.SOCKET_NAME)
        self.server.newConnection.connect(self._on_new_connection)

    def _create_timer(self) -> None:
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_tick)
        self.timer.start(Timing.TICK_MS)

    # ---------- 弹窗显示 / 隐藏 ----------
    def show_dialog(self) -> None:
        """显示并置顶对话框（从下方滑入 + 淡入）。"""
        if self._anim_state is AnimationState.SHOWING:
            return
        if (
            self.dialog.isVisible()
            and self._anim_state is AnimationState.IDLE
        ):
            self.dialog.raise_()
            self.dialog.activateWindow()
            return
        self._animate_show()

    def _animate_show(self) -> None:
        self._anim_state = AnimationState.SHOWING
        self._hide_indicator()

        if self._dialog_anchor is None:
            center_on_screen(self.dialog)
            self._dialog_anchor = self.dialog.pos()

        start_pos = self._dialog_anchor + QPoint(0, AnimSpec.SLIDE_PX)

        self.dialog.move(start_pos)
        self.dialog.setWindowOpacity(0.0)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

        self._show_anim = QParallelAnimationGroup(self)
        self._show_anim.addAnimation(self._make_pos_anim(
            start_pos, self._dialog_anchor,
            AnimSpec.SHOW_MS, QEasingCurve.Type.OutCubic,
        ))
        self._show_anim.addAnimation(self._make_opacity_anim(
            0.0, 1.0,
            AnimSpec.SHOW_MS, QEasingCurve.Type.OutCubic,
        ))
        self._show_anim.finished.connect(self._on_show_done)
        self._show_anim.start()

    def _on_show_done(self) -> None:
        self.dialog.setWindowOpacity(1.0)
        self._anim_state = AnimationState.IDLE

    def _animate_hide(self, show_circle: bool = True) -> None:
        """弹窗向下滑出 + 淡出。

        show_circle=True  → 结束后显示悬浮圆（用于“已阅”）
        show_circle=False → 结束后直接退出程序（用于“取消关机计划”）
        """
        if self._anim_state is not AnimationState.IDLE:
            return
        self._anim_state = AnimationState.HIDING
        self._dialog_anchor = self.dialog.pos()

        end_pos = self._dialog_anchor + QPoint(0, AnimSpec.SLIDE_PX)

        self._hide_anim = QParallelAnimationGroup(self)
        self._hide_anim.addAnimation(self._make_pos_anim(
            self._dialog_anchor, end_pos,
            AnimSpec.HIDE_MS, QEasingCurve.Type.InCubic,
        ))
        self._hide_anim.addAnimation(self._make_opacity_anim(
            1.0, 0.0,
            AnimSpec.HIDE_MS, QEasingCurve.Type.InCubic,
        ))
        self._hide_anim.finished.connect(
            lambda: self._on_hide_done(show_circle)
        )
        self._hide_anim.start()

    def _on_hide_done(self, show_circle: bool = True) -> None:
        self.dialog.hide()
        self.dialog.setWindowOpacity(1.0)
        if self._dialog_anchor is not None:
            self.dialog.move(self._dialog_anchor)
        self._anim_state = AnimationState.IDLE

        if show_circle:
            self._show_indicator()
        else:
            QTimer.singleShot(Timing.CLOSE_DELAY_MS, self._quit)

    # ---------- 动画构造辅助 ----------
    def _make_pos_anim(
        self,
        start: QPoint,
        end: QPoint,
        duration: int,
        curve: QEasingCurve.Type,
    ) -> QPropertyAnimation:
        anim = QPropertyAnimation(self.dialog, b"pos", self)
        anim.setDuration(duration)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(curve)
        return anim

    def _make_opacity_anim(
        self,
        start: float,
        end: float,
        duration: int,
        curve: QEasingCurve.Type,
    ) -> QPropertyAnimation:
        anim = QPropertyAnimation(self.dialog, b"windowOpacity", self)
        anim.setDuration(duration)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(curve)
        return anim

    # ---------- 悬浮圆：定位、出现、消失 ----------
    def _default_indicator_pos(self) -> QPoint:
        screen = self.dialog.screen() or QApplication.primaryScreen()
        if screen is None:
            return QPoint(0, 0)
        geo = screen.availableGeometry()
        return QPoint(
            geo.right() - CircleStyle.SIZE - CircleStyle.MARGIN_RIGHT + 1,
            geo.bottom() - CircleStyle.SIZE - CircleStyle.MARGIN_BOTTOM + 1,
        )

    def _show_indicator(self) -> None:
        if not self._circle_placed:
            self.indicator.move(self._default_indicator_pos())
            self._circle_placed = True

        self.indicator.set_progress(self._progress_ratio())
        self.indicator.set_remaining(self._remaining)

        self.indicator.setWindowOpacity(0.0)
        self.indicator.show()
        self.indicator.raise_()

        anim = QPropertyAnimation(self.indicator, b"windowOpacity", self)
        anim.setDuration(CircleStyle.FADE_MS)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.start()
        self._circle_anim = anim

    def _hide_indicator(self) -> None:
        if not self.indicator.isVisible():
            return

        anim = QPropertyAnimation(self.indicator, b"windowOpacity", self)
        anim.setDuration(CircleStyle.FADE_MS)
        anim.setStartValue(self.indicator.windowOpacity())
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim.finished.connect(self._on_indicator_hide_done)
        anim.start()
        self._circle_anim = anim

    def _on_indicator_hide_done(self) -> None:
        self.indicator.hide()
        self.indicator.setWindowOpacity(1.0)

    def _progress_ratio(self) -> float:
        if self._total <= 0:
            return 0.0
        return max(0.0, min(1.0, self._remaining / self._total))

    # ---------- 单实例消息 ----------
    def _on_new_connection(self) -> None:
        sock = self.server.nextPendingConnection()
        if not sock:
            return
        if sock.waitForReadyRead(Timing.NOTIFY_TIMEOUT_MS):
            if sock.readAll().data() == b"SHOW":
                self.show_dialog()
        sock.disconnectFromServer()

    # ---------- 倒计时 ----------
    def _on_tick(self) -> None:
        self._remaining -= 1
        if self._remaining > 0:
            self._refresh_ui()
            return

        self.timer.stop()
        self.dialog.update_content(0, self._total)
        self.indicator.set_progress(0.0)
        self.indicator.set_remaining(0)
        trigger_shutdown()

    def _refresh_ui(self) -> None:
        self.dialog.update_content(self._remaining, self._total)
        self.indicator.set_progress(self._progress_ratio())
        self.indicator.set_remaining(self._remaining)

    # ---------- 用户操作回调 ----------
    def _on_accepted(self) -> None:
        """“已阅”：窗口向下淡出，倒计时继续，右下角出现悬浮圆。"""
        self._animate_hide(show_circle=True)

    def _on_delayed(self) -> None:
        """“延迟 1 分钟”：剩余时间和总时间都 +DELAY_S，窗口保持显示。"""
        self._remaining += Timing.DELAY_S
        self._total += Timing.DELAY_S
        self._refresh_ui()

    def _on_shutdown_now(self) -> None:
        """“立即关机”：停止倒计时，附加缓冲后立即关机。"""
        self.timer.stop()
        trigger_shutdown(Timing.SHUTDOWN_BUFFER_S)

    def _on_cancelled(self) -> None:
        """“取消关机计划”：撤销系统关机命令，窗口向下淡出后退出。"""
        abort_shutdown()
        self.timer.stop()
        self._animate_hide(show_circle=False)

    # ---------- 退出清理 ----------
    def _quit(self) -> None:
        self.timer.stop()
        self.indicator.close()
        self.server.close()
        self._si.release()
        QApplication.quit()


# ══════════════════════════════════════════════════════════════════════════════
# 7. main() —— 程序入口
# ══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=AppMeta.DESCRIPTION)
    parser.add_argument(
        "--countdown",
        type=int,
        default=AppMeta.DEFAULT_COUNTDOWN,
        help="默认倒计时时长（秒）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.countdown <= 0:
        print("错误：--countdown 必须为大于 0 的整数", file=sys.stderr)
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    si = SingleInstance()
    if not si.acquire():
        si.notify_existing()
        sys.exit(0)

    controller = ShutdownController(args.countdown, si)  # noqa: F841
    play_startup_sound()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
