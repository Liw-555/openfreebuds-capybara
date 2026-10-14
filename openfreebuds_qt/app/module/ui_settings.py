import asyncio

from PyQt6.QtCore import QLocale, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel, QFrame, QVBoxLayout, QSystemTrayIcon
from qasync import asyncSlot

from openfreebuds import OfbEventKind
from openfreebuds.shortcuts import OfbShortcuts
from openfreebuds.utils.logger import create_logger
from openfreebuds_backend import is_run_at_boot, set_run_at_boot, AUTOSTART_AVAILABLE, AUTOUPDATE_AVAILABLE
from openfreebuds_qt.app.widget.low_battery_overlay import POPUP_THEMES
from openfreebuds_qt.app.module import OfbQtCommonModule
from openfreebuds_qt.config import OfbQtConfigParser
from openfreebuds_qt.constants import ASSETS_PATH
from openfreebuds_qt.designer.ui_settings import Ui_OfbQtUiSettingsModule
from openfreebuds_qt.qt_i18n import get_shortcut_names
from openfreebuds_qt.utils import blocked_signals, list_available_locales, OfbCoreEvent

log = create_logger("OfbQtUiSettingsModule")


class PopupThemeCard(QFrame):
    """主题预览卡片：微型弹窗样式 + 名称，点击选中并即时预览。"""

    clicked = pyqtSignal(str)

    def __init__(self, theme_key: str, theme: dict, parent=None):
        super().__init__(parent)
        self._theme_key = theme_key
        self.setFixedSize(112, 78)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 4)
        layout.setSpacing(2)

        if theme.get("sprite"):
            sprite = QLabel(self)
            sprite.setFixedSize(28, 28)
            sprite.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pm = QPixmap(str(ASSETS_PATH / "image" / theme["sprite"]))
            if not pm.isNull():
                sprite.setPixmap(pm.scaled(
                    28, 28,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ))
            layout.addWidget(sprite, alignment=Qt.AlignmentFlag.AlignHCenter)

        title = QLabel(self.tr(theme["label"]))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        self._theme = theme
        self._selected = False
        self._apply_style()

    def set_selected(self, value: bool):
        self._selected = value
        self._apply_style()

    def _apply_style(self):
        border = "2px solid #b0813f" if self._selected else "1px solid rgba(150,108,66,45)"
        # 复用主题卡片样式（缩小内边距版本）
        qss = self._theme["qss"].replace(
            "#lowBatteryOverlayFrame {", "QFrame {").replace(
            "border-radius: 18px;", "border-radius: 10px;").replace(
            "border-radius: 14px;", "border-radius: 10px;")
        self.setStyleSheet(qss + f"\nQFrame {{ {border}; }}\nQLabel {{ font-size: 11px; }}")

    def mousePressEvent(self, event):
        self.clicked.emit(self._theme_key)
        super().mousePressEvent(event)


class OfbQtUiSettingsModule(Ui_OfbQtUiSettingsModule, OfbQtCommonModule):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.shortcut_names = get_shortcut_names()
        self.available_shortcuts = OfbShortcuts.all()
        self.available_icons = ["auto", "light", "dark"]
        self.available_updater_policies = ["show", "check", "off"]
        self.available_locales = ["auto", *list_available_locales()]
        self.available_popup_themes = list(POPUP_THEMES.keys())
        self.config = OfbQtConfigParser.get_instance()

        self.setupUi(self)

        with blocked_signals(self.updater_policy_picker):
            if AUTOUPDATE_AVAILABLE:
                self.updater_policy_picker.setCurrentIndex(
                    self.available_updater_policies.index(self.config.get("updater", "mode", "show"))
                )
            else:
                self.updater_policy_picker.setCurrentIndex(2)
                self.updater_policy_picker.setEnabled(False)

        with blocked_signals(self.tray_shortcut_picker):
            for shortcut in self.available_shortcuts:
                self.tray_shortcut_picker.addItem(self.shortcut_names.get(shortcut, shortcut))
            self.tray_shortcut_picker.setCurrentIndex(
                self.available_shortcuts.index(self.config.get("ui", "tray_shortcut", "next_mode"))
            )

        with blocked_signals(self.language_picker):
            for locale in self.available_locales:
                if locale == "auto":
                    continue
                name = QLocale(locale).nativeLanguageName()
                self.language_picker.addItem(name)

            self.language_picker.setCurrentIndex(
                self.available_locales.index(self.config.get("ui", "language", "auto"))
            )

        with blocked_signals(self.tray_icon_picker):
            self.tray_icon_picker.setCurrentIndex(
                self.available_icons.index(self.config.get("ui", "tray_icon_theme", "auto"))
            )

        with blocked_signals(self.tray_eq_toggle):
            self.tray_eq_toggle.setChecked(self.config.get("ui", "tray_show_equalizer", False))

        with blocked_signals(self.tray_dc_toggle):
            self.tray_dc_toggle.setChecked(self.config.get("ui", "tray_show_dual_connect", False))

        with blocked_signals(self.low_battery_overlay_toggle):
            self.low_battery_overlay_toggle.setChecked(self.config.get("ui", "low_battery_overlay", True))

        with blocked_signals(self.battery_overlay_on_connect_toggle):
            self.battery_overlay_on_connect_toggle.setChecked(
                self.config.get("ui", "battery_overlay_on_connect", False)
            )

        with blocked_signals(self.popup_theme_picker):
            for key in self.available_popup_themes:
                self.popup_theme_picker.addItem(POPUP_THEMES[key]["label"])
            self.popup_theme_picker.setCurrentIndex(
                self.available_popup_themes.index(
                    self.config.get("ui", "popup_theme", "capybara")
                )
            )

        with blocked_signals(self.popup_on_case_open_toggle):
            self.popup_on_case_open_toggle.setChecked(
                self.config.get("ui", "popup_on_case_open", True)
            )

        # 主题可视化卡片：点击即切换 + 弹出真实预览
        self._theme_cards = {}
        current_theme = self.config.get("ui", "popup_theme", "capybara")
        for key in self.available_popup_themes:
            card = PopupThemeCard(key, POPUP_THEMES[key], self.popup_theme_cards)
            card.set_selected(key == current_theme)
            card.clicked.connect(self._on_theme_card_clicked)
            self.popup_theme_cards_layout.addWidget(card)
            self._theme_cards[key] = card
        self.popup_theme_cards_layout.addStretch(1)

        self.low_battery_overlay_test_button.setVisible(bool(getattr(self.ctx.args, "virtual_device", None)))

    async def update_ui(self, event: OfbCoreEvent):
        if not event.kind_match(OfbEventKind.QT_SETTINGS_CHANGED):
            return

        with blocked_signals(self.autostart_toggle):
            if AUTOSTART_AVAILABLE:
                self.autostart_toggle.setChecked(is_run_at_boot())
            else:
                self.autostart_toggle.setChecked(False)
                self.autostart_toggle.setEnabled(False)

        with blocked_signals(self.background_toggle):
            if QSystemTrayIcon.isSystemTrayAvailable():
                self.background_toggle.setChecked(self.config.get("ui", "background", True))
            else:
                self.background_toggle.setChecked(False)
                self.background_toggle.setEnabled(False)

    @asyncSlot(bool)
    async def on_background_toggle(self, value: bool):
        self.config.set("ui", "background", value)
        self.config.save()

    @asyncSlot(bool)
    async def on_autostart_toggle(self, value: bool):
        await set_run_at_boot(value)

    @asyncSlot(bool)
    async def on_tray_eq_toggle(self, value: bool):
        self.config.set("ui", "tray_show_equalizer", value)
        self.config.save()
        await self.ofb.send_message(OfbEventKind.QT_SETTINGS_CHANGED)

    @asyncSlot(bool)
    async def on_tray_dc_toggle(self, value: bool):
        self.config.set("ui", "tray_show_dual_connect", value)
        self.config.save()
        await self.ofb.send_message(OfbEventKind.QT_SETTINGS_CHANGED)

    @asyncSlot(bool)
    async def on_low_battery_overlay_toggle(self, value: bool):
        self.config.set("ui", "low_battery_overlay", value)
        self.config.save()

    @asyncSlot(bool)
    async def on_battery_overlay_on_connect_toggle(self, value: bool):
        self.config.set("ui", "battery_overlay_on_connect", value)
        self.config.save()

    @asyncSlot()
    async def on_low_battery_overlay_test(self):
        await self.ctx.tray.show_low_battery_overlay_preview()

    @asyncSlot(int)
    async def on_popup_theme_choose(self, index: int):
        self.config.set("ui", "popup_theme", self.available_popup_themes[index])
        self.config.save()
        self._refresh_theme_cards(self.available_popup_themes[index])

    @asyncSlot(bool)
    async def on_popup_on_case_open_toggle(self, value: bool):
        self.config.set("ui", "popup_on_case_open", value)
        self.config.save()

    def _refresh_theme_cards(self, theme_key: str):
        for key, card in self._theme_cards.items():
            card.set_selected(key == theme_key)

    def _on_theme_card_clicked(self, theme_key: str):
        """点击主题卡片：保存选择、同步下拉框、立即弹出真实预览。"""
        self.config.set("ui", "popup_theme", theme_key)
        self.config.save()
        self._refresh_theme_cards(theme_key)
        with blocked_signals(self.popup_theme_picker):
            self.popup_theme_picker.setCurrentIndex(
                self.available_popup_themes.index(theme_key)
            )
        # 需要已连接耳机才能预览；未连接时仅保存选择
        if self.ctx.tray is not None:
            asyncio.ensure_future(
                self.ctx.tray.show_low_battery_overlay_preview()
            )

    @asyncSlot(int)
    async def on_language_choose(self, index: int):
        self.config.set("ui", "language", self.available_locales[index])
        self.config.save()

    @asyncSlot(int)
    async def on_updater_policy_choose(self, index: int):
        self.config.set("updater", "mode", self.available_updater_policies[index])
        self.config.save()

    @asyncSlot(int)
    async def on_tray_color_choose(self, index: int):
        self.config.set("ui", "tray_icon_theme", self.available_icons[index])
        self.config.save()
        await self.ofb.send_message(OfbEventKind.QT_SETTINGS_CHANGED)

    @asyncSlot(int)
    async def on_tray_shortcut_choose(self, index: int):
        self.config.set("ui", "tray_shortcut", self.available_shortcuts[index])
        self.config.save()
