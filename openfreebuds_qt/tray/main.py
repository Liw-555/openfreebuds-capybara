import asyncio
from typing import Optional

from PIL import ImageQt
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QSystemTrayIcon
from qasync import asyncSlot

from openfreebuds import IOpenFreebuds, OfbEventKind
from openfreebuds.exceptions import OfbServerDeadError
from openfreebuds.utils.logger import create_logger
from openfreebuds_qt.config.main import OfbQtConfigParser
from openfreebuds_qt.generic import IOfbQtApplication
from openfreebuds_qt.generic import IOfbTrayIcon
from openfreebuds_qt.tray.menu import OfbQtTrayMenu
from openfreebuds_qt.tray.battery import battery_levels, battery_style, percentage_icon
from openfreebuds_qt.utils import OfbCoreEvent, qt_error_handler, create_tray_icon

try:
    from openfreebuds_qt.app.widget.low_battery_overlay import LowBatteryOverlay
except ImportError:
    LowBatteryOverlay = None

try:
    from openfreebuds_qt.app.widget.case_beacon import CaseBeaconWatcher
except ImportError:
    CaseBeaconWatcher = None

log = create_logger("OfbTrayIcon")


class OfbTrayIcon(IOfbTrayIcon):
    """
    OpenFreebuds Tray icon implementation
    """

    def __init__(self, context: IOfbQtApplication):
        super().__init__(None)

        self._last_tooltip = ""

        # noinspection PyUnresolvedReferences
        self.activated.connect(self._on_click)

        self.ctx = context
        self.ofb = context.ofb
        self.config = OfbQtConfigParser.get_instance()

        self.ui_update_task: Optional[asyncio.Task] = None
        self._low_battery_overlay = None
        self._low_battery_alert_shown = {
            10: False,
            20: False,
        }
        self._low_battery_device_addr = ""
        self._battery_summary_device_addr = ""
        self._battery_summary_overlay_shown = False

        # 开盖弹窗：BLE 信标监听（参考 LibreBuds 的 case-open 检测）
        self._case_beacon_watcher = None
        self._case_beacon_timer = QTimer(self)
        self._case_beacon_timer.setInterval(200)
        self._case_beacon_timer.timeout.connect(self._on_case_beacon_drain)
        self._case_beacon_ids = set(
            self.config.get("ui", "case_beacon_ids", []) or []
        )
        self._last_state = IOpenFreebuds.STATE_STOPPED

        self.menu = OfbQtTrayMenu(self, self.ctx, self.ofb)
        self.setContextMenu(self.menu)
        self._battery_icons = {}
        self._battery_icon_values = {}

    @asyncSlot(QSystemTrayIcon.ActivationReason)
    async def _on_click(self, reason):
        if (
            reason == self.ActivationReason.Trigger
            and await self.ofb.get_state() == self.ofb.STATE_CONNECTED
        ):
            async with qt_error_handler("OfbTrayIcon_OnClick", self.ctx):
                tray_shortcut = self.config.get("ui", "tray_shortcut", "next_mode")
                await self.ofb.run_shortcut(tray_shortcut)

    async def boot(self):
        """
        Will start UI update loop and perform other preparations on boot
        """
        async with qt_error_handler("OfbTrayIcon_Boot", self.ctx):
            if self.ui_update_task is None:
                self.ui_update_task = asyncio.create_task(self._update_loop())

            self._start_case_beacon_watcher()
            await self._update_ui(OfbCoreEvent(None))

    async def close(self):
        """
        Will stop UI update loop
        """
        for icon in self._battery_icons.values():
            icon.hide()
        if self.ui_update_task is not None:
            self.ui_update_task.cancel()
            await self.ui_update_task
            self.ui_update_task = None
        self._stop_case_beacon_watcher()

    async def _update_ui(self, event: OfbCoreEvent):
        """
        UI update callback
        """

        state = await self.ofb.get_state()
        self._last_state = state

        # Update icon
        icon = create_tray_icon(self.config.get_tray_icon_theme(),
                                state,
                                await self.ofb.get_property("battery", "global", 0),
                                await self.ofb.get_property("anc", "mode", "normal"))
        pixmap = QIcon(ImageQt.toqpixmap(icon))
        self.setIcon(pixmap)
        await self._update_battery_icons(state)

        # Update menu and tooltip
        if state == IOpenFreebuds.STATE_CONNECTED:
            self.setToolTip(await self._get_tooltip_text(event))
        elif state == IOpenFreebuds.STATE_WAIT:
            self.setToolTip(self.tr("OpenFreebuds: Connecting to device…"))
        else:
            self.setToolTip("OpenFreebuds")

        if state == IOpenFreebuds.STATE_CONNECTED:
            summary_shown = False
            if event.kind_match(OfbEventKind.STATE_CHANGED) or event.is_changed("battery", ""):
                summary_shown = await self._check_battery_summary_overlay()
            if event.is_changed("battery", "") and not summary_shown:
                await self._check_low_battery_overlay()
        else:
            self._reset_low_battery_alerts()
            self._reset_battery_summary_overlay()

        await self.menu.on_core_event(event, state)

    async def _update_battery_icons(self, state):
        levels = {}
        if state == IOpenFreebuds.STATE_CONNECTED and self.config.get(
            "ui", "tray_battery_percentages", False
        ):
            levels = battery_levels(await self.ofb.get_property("battery"))
        labels = {
            "left": self.tr("Left earbud"),
            "right": self.tr("Right earbud"),
            "case": self.tr("Charging case"),
        }
        theme = self.config.get_tray_icon_theme() if levels else None
        device_name, _ = await self.ofb.get_device_tags() if levels else ("", "")
        for key, icon in self._battery_icons.items():
            if key not in levels:
                icon.hide()
        for key, level in levels.items():
            text_color, background_color, transparent, font_scale, bold = battery_style(self.config, key)
            if transparent:
                background_color = None
            if key not in self._battery_icons:
                icon = QSystemTrayIcon(self)
                icon.setContextMenu(self.menu)
                icon.activated.connect(self._on_battery_click)
                self._battery_icons[key] = icon
            icon = self._battery_icons[key]
            appearance = (level, theme, text_color, background_color, font_scale, bold)
            if self._battery_icon_values.get(key) != appearance:
                icon.setIcon(percentage_icon(*appearance))
                self._battery_icon_values[key] = appearance
            icon.setToolTip(f"{device_name} — {labels[key]}: {level}%")
            icon.show()

    def _on_battery_click(self, reason):
        if reason == self.ActivationReason.Trigger:
            self.ctx.main_window.show()
            self.ctx.main_window.raise_()
            self.ctx.main_window.activateWindow()

    async def _get_tooltip_text(self, event: OfbCoreEvent):
        """
        Create tooltip text for tray icon
        """
        if event.is_changed("battery") or event.kind_match(OfbEventKind.DEVICE_CHANGED) or self._last_tooltip == "":
            device_name, _ = await self.ofb.get_device_tags()
            battery = await self.ofb.get_property("battery", "global", "--")
            self._last_tooltip = f"{device_name}: {battery}%"

        return self._last_tooltip

    async def _check_battery_summary_overlay(self):
        if not self.config.get("ui", "battery_overlay_on_connect", False):
            return False

        battery = await self.ofb.get_property("battery")
        if battery is None:
            return False

        device_name, device_addr = await self.ofb.get_device_tags()
        if device_addr != self._battery_summary_device_addr:
            self._battery_summary_device_addr = device_addr
            self._battery_summary_overlay_shown = False

        if self._battery_summary_overlay_shown:
            return False

        self._battery_summary_overlay_shown = True
        self._show_low_battery_overlay(device_name, battery, 0)
        return True

    async def _check_low_battery_overlay(self):
        if not self.config.get("ui", "low_battery_overlay", True):
            return

        battery = await self.ofb.get_property("battery")
        if battery is None:
            return

        device_name, device_addr = await self.ofb.get_device_tags()
        if device_addr != self._low_battery_device_addr:
            self._low_battery_device_addr = device_addr
            self._reset_low_battery_alerts()

        min_battery = self._get_min_battery_level(battery)
        if min_battery is None:
            return

        if min_battery > 25:
            self._low_battery_alert_shown[20] = False
        if min_battery > 15:
            self._low_battery_alert_shown[10] = False

        threshold = None
        if min_battery <= 10:
            threshold = 10
        elif min_battery <= 20:
            threshold = 20

        if threshold is None or self._low_battery_alert_shown[threshold]:
            return

        self._low_battery_alert_shown[threshold] = True
        self._show_low_battery_overlay(device_name, battery, threshold)

    def _show_low_battery_overlay(self, device_name: str, battery: dict, threshold: int):
        if LowBatteryOverlay is None:
            log.warning("Low battery overlay is not available")
            return

        try:
            if self._low_battery_overlay is not None:
                self._low_battery_overlay.close()

            overlay = LowBatteryOverlay(
                device_name, battery, threshold,
                theme=self.config.get("ui", "popup_theme", "capybara"),
            )
            overlay.destroyed.connect(lambda _=None, current=overlay: self._on_low_battery_overlay_destroyed(current))
            self._low_battery_overlay = overlay
            overlay.show_overlay()
        except Exception:
            log.exception("Failed to show low battery overlay")

    def _on_low_battery_overlay_destroyed(self, overlay):
        if self._low_battery_overlay is overlay:
            self._low_battery_overlay = None

    # ------------------------------------------------------------------
    # 开盖弹窗（BLE 信标）
    # ------------------------------------------------------------------

    def _start_case_beacon_watcher(self):
        if CaseBeaconWatcher is None:
            return
        if self._case_beacon_watcher is not None:
            return
        watcher = CaseBeaconWatcher()
        if not watcher.start():
            log.info("Case beacon watcher is not available")
            return
        self._case_beacon_watcher = watcher
        self._case_beacon_timer.start()
        log.info("Case beacon watcher started")

    def _stop_case_beacon_watcher(self):
        self._case_beacon_timer.stop()
        if self._case_beacon_watcher is not None:
            self._case_beacon_watcher.stop()
            self._case_beacon_watcher = None

    def _on_case_beacon_drain(self):
        """Qt 线程：取出 WinRT 线程解析好的信标事件。"""
        if self._case_beacon_watcher is None:
            return
        for kind, beacon, rssi in self._case_beacon_watcher.drain():
            if not beacon.model_id:
                continue
            if kind == "beacon":
                self._maybe_learn_beacon_id(beacon.model_id)
            elif kind == "open":
                self._maybe_learn_beacon_id(beacon.model_id)
                if not self.config.get("ui", "popup_on_case_open", True):
                    continue
                # 只对本机学习过的 model id 弹窗，避免邻居的耳机误触发
                if self._case_beacon_ids and \
                        beacon.model_id not in self._case_beacon_ids:
                    continue
                log.info(f"Case opened (model {beacon.model_id}, {rssi} dBm)")
                asyncio.ensure_future(self._show_case_beacon_overlay(beacon))

    def _maybe_learn_beacon_id(self, model_id: str):
        """SPP 连接时把见到的信标 model id 记为本机设备。"""
        if self._last_state != IOpenFreebuds.STATE_CONNECTED:
            return
        if model_id in self._case_beacon_ids:
            return
        self._case_beacon_ids.add(model_id)
        self.config.set("ui", "case_beacon_ids", sorted(self._case_beacon_ids))
        self.config.save()
        log.info(f"Learned case beacon model id: {model_id}")

    async def _show_case_beacon_overlay(self, beacon):
        async with qt_error_handler("OfbTrayIcon_CaseBeaconOverlay", self.ctx):
            device_name, _ = await self.ofb.get_device_tags()
            if not device_name:
                device_name = self.config.get("device", "name", "OpenFreebuds")

            # 信标电量优先（开盖瞬间最新，且含充电盒），缺失的用 SPP 电量补
            battery = dict(beacon.battery_dict())
            try:
                spp_battery = await self.ofb.get_property("battery")
            except Exception:
                spp_battery = None
            if isinstance(spp_battery, dict):
                for key, value in spp_battery.items():
                    battery.setdefault(key, value)

            self._show_low_battery_overlay(device_name, battery, 0)

    async def show_low_battery_overlay_preview(self):
        """预览弹窗：已连接用真实电量，未连接用演示电量（10/20/42，顺带展示低电量配色）。"""
        connected = await self.ofb.get_state() == IOpenFreebuds.STATE_CONNECTED

        device_name = ""
        if connected:
            device_name, _ = await self.ofb.get_device_tags()
        if not device_name:
            device_name = self.config.get("device", "name", "OpenFreebuds")

        battery = {
            "left": 10,
            "right": 20,
            "case": 42,
        }
        if connected:
            try:
                real_battery = await self.ofb.get_property("battery")
            except Exception:
                real_battery = None
            if isinstance(real_battery, dict) and real_battery:
                battery = dict(real_battery)

        self._show_low_battery_overlay(device_name, battery, 0)

    def _reset_low_battery_alerts(self):
        self._low_battery_alert_shown[10] = False
        self._low_battery_alert_shown[20] = False

    def _reset_battery_summary_overlay(self):
        self._battery_summary_device_addr = ""
        self._battery_summary_overlay_shown = False

    @staticmethod
    def _get_min_battery_level(battery: dict):
        levels = []
        for key in ("left", "right", "case", "global"):
            value = battery.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                levels.append(value)
            elif isinstance(value, str) and value.isdigit():
                levels.append(int(value))

        if not levels:
            return None
        return min(levels)

    async def _update_loop(self):
        """
        Background task that will subscribe to core event bus and watch
        for changes to perform tray UI update when something changes.
        """

        async with qt_error_handler("OfbTrayIcon_EventLoop", self.ctx):
            member_id = await self.ofb.subscribe()
            log.info(f"Tray update loop started, member_id={member_id}")

            try:
                while True:
                    kind, *args = await self.ofb.wait_for_event(member_id)
                    event = OfbCoreEvent(kind, *args)

                    if event.kind_match(OfbEventKind.QT_BRING_SETTINGS_UP):
                        self.ctx.main_window.show()
                        self.ctx.main_window.activateWindow()

                    if event.kind_match(OfbEventKind.STATE_CHANGED) and args[0] == IOpenFreebuds.STATE_DESTROYED:
                        raise OfbServerDeadError("Server going to exit")

                    if event.kind_in([
                        OfbEventKind.STATE_CHANGED,
                        OfbEventKind.DEVICE_CHANGED,
                        OfbEventKind.QT_SETTINGS_CHANGED,
                        OfbEventKind.PROPERTY_CHANGED,
                    ]):
                        await self._update_ui(event)
            except asyncio.CancelledError:
                await self.ofb.unsubscribe(member_id)
            except OfbServerDeadError:
                log.info("Server is dead, exiting now…")
                self.ui_update_task = None
                await self.ctx.exit(1)
