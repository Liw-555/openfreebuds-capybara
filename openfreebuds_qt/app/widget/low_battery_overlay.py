import math
import time

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
)
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from openfreebuds_qt.constants import ASSETS_PATH
from openfreebuds_qt.utils import get_img_colored

# ---------------------------------------------------------------------------
# 主题注册表：新增主题 = 在这里加一段配置 + 设置界面下拉自动出现
# ---------------------------------------------------------------------------

POPUP_THEMES = {
    "capybara": {
        "label": "卡皮巴拉",
        "qss": """
            #lowBatteryOverlayFrame {
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #faf0dd, stop:1 #f1dfc0);
                border: 1px solid rgba(150, 108, 66, 90);
                border-radius: 18px;
            }
            QLabel { color: #5a422e; background: transparent; }
            QLabel[role="title"] { font-size: 12px; font-weight: 600; }
            QLabel[role="value"] { font-size: 13px; font-weight: 600; }
        """,
        "value_default": "#5a422e",
        "value_warning": "#d07b1e",
        "value_critical": "#cf3d3d",
        "sprite": "capybara.png",
        "energy": 1.0,
        "dust": True,
    },
    "classic": {
        "label": "经典暗夜",
        "qss": """
            #lowBatteryOverlayFrame {
                background-color: #17191d;
                border: 1px solid rgba(255, 255, 255, 38);
                border-radius: 14px;
            }
            QLabel { color: #f4f6f8; background: transparent; }
            QLabel[role="title"] { font-size: 12px; font-weight: 600; }
            QLabel[role="value"] { font-size: 13px; font-weight: 600; }
        """,
        "value_default": "#f4f6f8",
        "value_warning": "#ffb454",
        "value_critical": "#ff5f57",
        "sprite": None,
        "energy": 0.0,
        "dust": False,
    },
    "sakura": {
        "label": "樱花绯绯",
        "qss": """
            #lowBatteryOverlayFrame {
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #fff3f5, stop:1 #ffdde5);
                border: 1px solid rgba(200, 120, 140, 90);
                border-radius: 18px;
            }
            QLabel { color: #8a4257; background: transparent; }
            QLabel[role="title"] { font-size: 12px; font-weight: 600; }
            QLabel[role="value"] { font-size: 13px; font-weight: 600; }
        """,
        "value_default": "#8a4257",
        "value_warning": "#d0631e",
        "value_critical": "#cf3d3d",
        "sprite": "capybara.png",
        "energy": 0.7,
        "dust": False,
    },
    "mint": {
        "label": "薄荷气泡",
        "qss": """
            #lowBatteryOverlayFrame {
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #f0faf3, stop:1 #d6efdf);
                border: 1px solid rgba(90, 150, 110, 90);
                border-radius: 18px;
            }
            QLabel { color: #33604a; background: transparent; }
            QLabel[role="title"] { font-size: 12px; font-weight: 600; }
            QLabel[role="value"] { font-size: 13px; font-weight: 600; }
        """,
        "value_default": "#33604a",
        "value_warning": "#d07b1e",
        "value_critical": "#cf3d3d",
        "sprite": "capybara.png",
        "energy": 0.7,
        "dust": False,
    },
}


def get_popup_theme(key):
    return POPUP_THEMES.get(key or "capybara", POPUP_THEMES["capybara"])


class LowBatteryOverlay(QWidget):
    DISPLAY_MS = 4400
    SHOW_DURATION_MS = 320
    HIDE_DURATION_MS = 180
    SLIDE_OFFSET = 26
    # 卡片在窗口内的透明边距：上边距就是卡皮巴拉"跳出卡片"的舞台
    PAD_TOP = 88
    PAD_SIDE = 36
    PAD_BOTTOM = 22
    SCREEN_MARGIN = 8
    CARD_HEIGHT = 92
    ICON_COLOR = (90, 66, 46, 255)
    WIDTH_BY_ITEM_COUNT = {
        1: 176,
        2: 210,
        3: 248,
    }
    # ---- 动画编排时间轴（ms，相对 show 开始）----
    T_CROUCH = 700        # 预备下蹲开始
    T_CROUCH_END = 950
    T_JUMP = 950          # 大跳
    T_JUMP_END = 1590
    T_SQUASH_END = 1830   # 落地压扁恢复
    T_WOBBLE_END = 2700   # 阻尼摇晃结束
    T_MINI_FIRST = 2500
    MINI_HOP_PERIOD_MS = 1900
    MINI_HOP_DURATION_MS = 420
    # ---- 动效参数 ----
    TICK_MS = 33          # ~30fps
    SPRITE_CANVAS = 96
    SPRITE_SIZE = 60
    OVERLAP = 8           # 脚陷入卡片顶边深度
    BIG_JUMP_APEX = 44
    BIG_JUMP_DX = 12
    MINI_HOP_APEX = 18
    BOB_AMPLITUDE = 4.0
    BOB_PERIOD_MS = 1600

    def __init__(self, device_name: str, battery: dict, threshold: int,
                 theme=None):
        super().__init__(None)

        del threshold
        if theme is None:
            from openfreebuds_qt.config import OfbQtConfigParser
            theme = OfbQtConfigParser.get_instance().get(
                "ui", "popup_theme", "capybara")
        self._theme = get_popup_theme(theme)

        self._show_animation = None
        self._hide_animation = None
        self._fade_animation = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_overlay)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(self.TICK_MS)
        self._tick_timer.timeout.connect(self._on_tick)
        self._t0 = 0.0

        self._capy_base = QPixmap()
        self._capy_canvas = None
        self._dust_canvas = None
        self._sprite_theme = bool(self._theme["sprite"])

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        # 窗口 = 卡片 + 四周透明边距（精灵活动舞台）
        card_w = self.WIDTH_BY_ITEM_COUNT.get(
            len(self._battery_items(battery)), 248
        )
        self.setFixedSize(
            card_w + self.PAD_SIDE * 2,
            self.PAD_TOP + self.CARD_HEIGHT + self.PAD_BOTTOM,
        )

        frame = QFrame(self)
        frame.setObjectName("lowBatteryOverlayFrame")
        frame.setStyleSheet(self._theme["qss"])
        frame.setGeometry(self.PAD_SIDE, self.PAD_TOP, card_w, self.CARD_HEIGHT)
        self._frame = frame

        # 内容错峰淡入（follow-through：卡片先到，内容跟上）
        self._frame_opacity = QGraphicsOpacityEffect(frame)
        self._frame_opacity.setOpacity(0.0)
        frame.setGraphicsEffect(self._frame_opacity)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)

        title = QLabel(device_name or "OpenFreebuds", frame)
        title.setProperty("role", "title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        battery_layout = QHBoxLayout()
        battery_layout.setSpacing(12)
        battery_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(battery_layout)

        battery_items = self._battery_items(battery)
        for key, value in battery_items:
            item = QHBoxLayout()
            item.setSpacing(4)
            item.setAlignment(Qt.AlignmentFlag.AlignCenter)

            icon = QLabel(frame)
            icon.setFixedSize(18, 18)
            icon_pixmap = self._battery_icon(key)
            if icon_pixmap is not None:
                icon.setPixmap(icon_pixmap)
            item.addWidget(icon)

            value_label = QLabel(f"{value}%", frame)
            value_label.setProperty("role", "value")
            value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value_label.setStyleSheet(
                f"color: {self._battery_value_color(key, value)};"
            )
            item.addWidget(value_label)

            battery_layout.addLayout(item)

        if self._sprite_theme:
            self._capy_base = self._load_capybara(
                self._theme["sprite"], self.SPRITE_SIZE
            )
            self._capy_canvas = QPixmap(self.SPRITE_CANVAS, self.SPRITE_CANVAS)

            # 精灵：窗口级子控件，不受卡片布局约束，可跳出卡片范围
            self._capy_label = QLabel(self)
            self._capy_label.setFixedSize(self.SPRITE_CANVAS, self.SPRITE_CANVAS)
            self._capy_label.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents
            )
            self._capy_base_x = self.width() // 2 - self.SPRITE_CANVAS // 2
            self._capy_base_y = self.PAD_TOP + self.OVERLAP - self.SPRITE_CANVAS
            self._capy_label.move(self._capy_base_x, self._capy_base_y)
            self._capy_label.raise_()

            if self._theme["dust"]:
                # 落地灰尘粒子层
                self._dust_label = QLabel(self)
                self._dust_label.setFixedSize(120, 30)
                self._dust_label.setAttribute(
                    Qt.WidgetAttribute.WA_TransparentForMouseEvents
                )
                self._dust_label.move(self.width() // 2 - 60, self.PAD_TOP - 24)
                self._dust_label.raise_()
                self._dust_canvas = QPixmap(120, 30)
            else:
                self._dust_label = None

            self._render_capy_frame(1.0, 1.0, 0.0, 1.0)

    # ------------------------------------------------------------------
    # 显示 / 隐藏
    # ------------------------------------------------------------------

    def show_overlay(self):
        screen = QGuiApplication.primaryScreen()
        geometry = screen.availableGeometry()
        x = geometry.x() + (geometry.width() - self.width()) // 2
        visible_y = geometry.y() + self.SCREEN_MARGIN
        hidden_y = visible_y - self.height() - self.SLIDE_OFFSET

        self._t0 = time.monotonic()
        self._frame.move(self.PAD_SIDE, self.PAD_TOP)
        self._frame_opacity.setOpacity(0.0)
        self.move(x, hidden_y)
        self.show()
        self.raise_()

        # 弹跳入场（OutBack 带轻微过冲）
        self._show_animation = QPropertyAnimation(self, b"pos", self)
        self._show_animation.setDuration(self.SHOW_DURATION_MS)
        self._show_animation.setStartValue(QPoint(x, hidden_y))
        self._show_animation.setEndValue(QPoint(x, visible_y))
        self._show_animation.setEasingCurve(QEasingCurve.Type.OutBack)
        self._show_animation.finished.connect(self._on_show_finished)
        self._show_animation.start()

        if self._sprite_theme:
            self._tick_timer.start()

    def _on_show_finished(self):
        # 内容淡入（卡片先落地，内容跟随）
        self._fade_animation = QPropertyAnimation(
            self._frame_opacity, b"opacity", self
        )
        self._fade_animation.setDuration(220)
        self._fade_animation.setStartValue(0.0)
        self._fade_animation.setEndValue(1.0)
        self._fade_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_animation.start()
        self._hide_timer.start(self.DISPLAY_MS)

    def hide_overlay(self):
        if not self.isVisible():
            return

        self._tick_timer.stop()
        self._hide_timer.stop()
        end_pos = QPoint(self.x(), self.y() - self.height() - self.SLIDE_OFFSET)
        self._hide_animation = QPropertyAnimation(self, b"pos", self)
        self._hide_animation.setDuration(self.HIDE_DURATION_MS)
        self._hide_animation.setStartValue(self.pos())
        self._hide_animation.setEndValue(end_pos)
        self._hide_animation.setEasingCurve(QEasingCurve.Type.InCubic)
        self._hide_animation.finished.connect(self.close)
        self._hide_animation.start()

    def closeEvent(self, event):
        self._tick_timer.stop()
        self._hide_timer.stop()
        for anim in (self._show_animation, self._hide_animation,
                     self._fade_animation):
            if anim is not None:
                anim.stop()
        event.accept()

    # ------------------------------------------------------------------
    # 动画编排（时间轴纯函数：同一时刻永远得到同一帧）
    # ------------------------------------------------------------------

    def _on_tick(self):
        t = (time.monotonic() - self._t0) * 1000.0
        e = self._theme["energy"]

        dy = self.BOB_AMPLITUDE * e * math.sin(
            2 * math.pi * (t % self.BOB_PERIOD_MS) / self.BOB_PERIOD_MS
        )
        rot = 3.5 * e * math.sin(2 * math.pi * (t % 2600) / 2600)
        sx, sy = 1.0, 1.0
        shadow = 1.0
        dx = 0.0

        jump = self._jump_state(
            t, self.T_JUMP, self.T_JUMP_END - self.T_JUMP,
            self.BIG_JUMP_APEX * e, self.BIG_JUMP_DX * e,
        )
        if jump is None:
            jump = self._mini_hop(t)

        if jump is not None:
            # 腾空：抛物线 + 空中倾身 + 速度映射拉伸
            tp, apex, jdx = jump
            dy -= apex * math.sin(math.pi * tp)
            dx = jdx
            shadow = 1.0 - 0.75 * math.sin(math.pi * tp)
            rot = -9.0 * e * math.sin(math.pi * tp)
            v = abs(math.cos(math.pi * tp))
            sy = 1.0 + 0.20 * v
            sx = 1.0 - 0.14 * v
        elif self.T_CROUCH <= t < self.T_CROUCH_END:
            # 预备动作：起跳前下蹲压缩
            p = (t - self.T_CROUCH) / (self.T_CROUCH_END - self.T_CROUCH)
            sy = 1.0 - 0.20 * p
            sx = 1.0 + 0.18 * p
            dy += 4 * p
        elif self.T_JUMP_END <= t < self.T_SQUASH_END:
            # 落地压扁后恢复
            p = (t - self.T_JUMP_END) / (self.T_SQUASH_END - self.T_JUMP_END)
            sy = 1.0 - 0.28 * (1.0 - p)
            sx = 1.0 + 0.24 * (1.0 - p)
        elif self.T_SQUASH_END <= t < self.T_WOBBLE_END:
            # 跟随动作：阻尼弹簧摇晃
            dt = t - self.T_SQUASH_END
            decay = math.exp(-dt / 380.0)
            rot += 7.0 * e * decay * math.cos(2 * math.pi * dt / 260.0)
            sy -= 0.05 * e * decay * abs(math.cos(2 * math.pi * dt / 260.0))

        self._render_capy_frame(sx, sy, rot, shadow)
        self._render_dust(t)
        self._capy_label.move(
            self._capy_base_x + round(dx), self._capy_base_y + round(dy)
        )

        # 次级动作：卡片被"踩"下沉，阻尼回弹
        if t >= self.T_JUMP_END:
            dt = t - self.T_JUMP_END
            dip = 3.0 * e * math.exp(-dt / 140.0) * math.cos(
                2 * math.pi * dt / 100.0
            )
            self._frame.move(self.PAD_SIDE, round(self.PAD_TOP + dip))

    @staticmethod
    def _jump_state(t, start, duration, apex, dx):
        if start <= t < start + duration:
            tp = (t - start) / duration
            return tp, apex, dx * math.sin(math.pi * tp)
        return None

    def _mini_hop(self, t):
        if t < self.T_MINI_FIRST:
            return None
        phase = (t - self.T_MINI_FIRST) % self.MINI_HOP_PERIOD_MS
        if phase < self.MINI_HOP_DURATION_MS:
            tp = phase / self.MINI_HOP_DURATION_MS
            return tp, self.MINI_HOP_APEX * self._theme["energy"], 0.0
        return None

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def _render_capy_frame(self, sx: float, sy: float, rot: float,
                           shadow: float = 1.0):
        """底部锚点绘制精灵：挤压拉伸 + 旋转 + 随高度缩放的影子。"""
        canvas, size = self.SPRITE_CANVAS, self.SPRITE_SIZE
        self._capy_canvas.fill(Qt.GlobalColor.transparent)

        p = QPainter(self._capy_canvas)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        if shadow > 0.02:
            # 影子：贴卡片顶边，腾空时变小变淡
            sw = 34 * (0.55 + 0.45 * shadow)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(90, 66, 46, int(70 * shadow)))
            p.drawEllipse(QRectF(canvas // 2 - sw / 2, canvas - 7, sw, 6))

        p.translate(canvas // 2, canvas - 2)      # 底部中心锚点
        p.rotate(rot)
        p.scale(sx, sy)
        p.drawPixmap(QRectF(-size / 2, -size, size, size), self._capy_base,
                     QRectF(0, 0, size * 2, size * 2))
        p.end()
        self._capy_label.setPixmap(self._capy_canvas)

    def _render_dust(self, t):
        """落地灰尘粒子：大跳落地瞬间从脚下炸开。"""
        if self._dust_label is None:
            return
        age = t - self.T_JUMP_END
        if not 0 <= age < 320:
            if not self._dust_canvas.isNull():
                self._dust_canvas.fill(Qt.GlobalColor.transparent)
                self._dust_label.setPixmap(self._dust_canvas)
            return

        p = QPainter(self._dust_canvas)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        prog = age / 320.0
        for i in range(5):
            spread = (i - 2) / 2.0                     # -1 .. 1
            px = 60 + spread * 26 * prog
            py = 24 - (10 + 8 * abs(spread)) * math.sin(
                math.pi * min(prog * 1.6, 1.0))
            r = 2.4 * (1.0 - prog)
            alpha = int(110 * (1.0 - prog))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(150, 108, 66, alpha))
            p.drawEllipse(QRectF(px - r, py - r, r * 2, r * 2))
        p.end()
        self._dust_label.setPixmap(self._dust_canvas)

    # ------------------------------------------------------------------
    # 资源与数据
    # ------------------------------------------------------------------

    @staticmethod
    def _load_capybara(sprite_name: str, size: int) -> QPixmap:
        path = ASSETS_PATH / "image" / sprite_name
        pm = QPixmap(str(path))
        if pm.isNull():
            return QPixmap()
        return pm.scaled(
            size * 2, size * 2,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _battery_items(self, battery: dict):
        if all(key in battery for key in ("left", "right", "case")):
            return [
                ("left", battery["left"]),
                ("right", battery["right"]),
                ("case", battery["case"]),
            ]

        rows = []
        for key in ("left", "right", "case", "global"):
            if key in battery:
                rows.append((key, battery[key]))

        return rows or [("global", "--")]

    @classmethod
    def _battery_icon(cls, key: str):
        icon_name = {
            "left": "batt_l",
            "right": "batt_r",
            "case": "batt_c",
            "global": "batt_c",
        }.get(key, "batt_c")

        try:
            return get_img_colored(icon_name, cls.ICON_COLOR, "icon/main_window", (18, 18))
        except Exception:
            return None

    def _battery_value_color(self, key: str, value):
        if key not in ("left", "right") or isinstance(value, bool) \
                or not isinstance(value, int):
            return self._theme["value_default"]
        if value <= 10:
            return self._theme["value_critical"]
        if value <= 20:
            return self._theme["value_warning"]
        return self._theme["value_default"]
