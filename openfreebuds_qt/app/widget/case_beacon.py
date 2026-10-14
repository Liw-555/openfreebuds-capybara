"""
充电盒 BLE 信标监听（开盖弹窗）。

参考 LibreBuds（GPL-3.0，github.com/librebuds）的开盖检测最佳实践：
华为/荣耀耳机在开盖时会通过 BLE 广播 0xFDEE 服务数据（proximity beacon），
其中 compact 格式（business type 0x02）的字段存在性位图可以推导出盒盖状态：

    flags 0x03FF = 盒盖打开（盒电池 + 尾部均存在）
    flags 0x03DF = 耳机在盒外/佩戴中（有尾部，无盒电池）
    flags 0x00FF/0x007F/0x00BF = 刚合盖过渡态
    flags 0x003F = 盒盖关闭

CaseOpenings 状态机保证"每次开盖只弹一次"：开盖信标在上一条为关闭
（或距上次开盖超过 GAP）时开启一次 opening；关闭/过渡信标结束它。

Windows 侧用 winsdk 的 BluetoothLEAdvertisementWatcher 被动扫描，
与已有的 SPP 控制通道并存，互不影响。
"""

import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

FDEE_UUID_LE = b"\xee\xfd"          # 0xFDEE 服务 UUID 的小端表示
AD_TYPE_SERVICE_DATA_16 = 0x16

# compact 帧字段存在性位图
_HEAD_BITS = 0x1F
_HEAD_LENGTH = 13
_BIT_CASE = 0x20
_BIT_LEFT = 0x40
_BIT_RIGHT = 0x80
_BIT_TAIL = 0x300

RSSI_THRESHOLD = -80                # 距离过滤，参考 LibreBuds 实测
OPENING_GAP_MS = 30_000             # 超过该间隔的开盖信标视为新的一次开盖
POPUP_COOLDOWN_MS = 20_000          # 全局冷却，防止盖子弹跳重复弹窗


class LidState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    BUDS_OUT = "buds_out"
    TRANSITIONAL = "transitional"


@dataclass
class BeaconBattery:
    percent: int
    charging: bool

    @classmethod
    def decode(cls, value: int) -> "BeaconBattery":
        return cls(percent=value & 0x7F, charging=bool(value & 0x80))


@dataclass
class Beacon:
    model_id: Optional[str]
    sub_model: Optional[int]
    flags: Optional[int]
    lid: Optional[LidState]
    state: Optional[int]
    left: Optional[BeaconBattery] = None
    right: Optional[BeaconBattery] = None
    case: Optional[BeaconBattery] = None

    @property
    def case_open(self) -> bool:
        return self.lid == LidState.OPEN

    def battery_dict(self) -> dict:
        out = {}
        if self.left:
            out["left"] = self.left.percent
        if self.right:
            out["right"] = self.right.percent
        if self.case:
            out["case"] = self.case.percent
        return out


def parse_fdee_service_data(data: bytes) -> Optional[Beacon]:
    """解析 0xFDEE 服务数据（不含 UUID 前缀）。仅实现 compact 格式。"""
    if len(data) < 2:
        return None

    if data[0] == 0x01 and data[1] == 0x02:
        return _parse_compact(data)
    # 其他格式（配对模式 TLV / 扩展识别帧）不解析
    return None


def _parse_compact(data: bytes) -> Optional[Beacon]:
    if len(data) < _HEAD_LENGTH:
        return None
    flags = (data[2] << 8) | data[3]
    if flags & _HEAD_BITS != _HEAD_BITS:
        return None
    if data[5] != 0x00 or data[10] != 0x03 or data[12] != 0xAB:
        return None

    optional_bits = [_BIT_CASE, _BIT_LEFT, _BIT_RIGHT, 0x100, 0x200]
    if len(data) < _HEAD_LENGTH + sum(1 for b in optional_bits if flags & b):
        return None

    i = _HEAD_LENGTH

    def optional(bit: int):
        nonlocal i
        if not flags & bit:
            return None
        value = data[i]
        i += 1
        return value

    case_v = optional(_BIT_CASE)
    left_v = optional(_BIT_LEFT)
    right_v = optional(_BIT_RIGHT)
    tail_bits = flags & _BIT_TAIL

    if tail_bits and flags & _BIT_CASE:
        lid = LidState.OPEN
    elif tail_bits:
        lid = LidState.BUDS_OUT
    elif flags & (_BIT_LEFT | _BIT_RIGHT):
        lid = LidState.TRANSITIONAL
    else:
        lid = LidState.CLOSED

    def battery(value):
        if value is None or value == 0:
            return None
        b = BeaconBattery.decode(value)
        return b if b.percent <= 100 else None

    return Beacon(
        model_id=data[6:9].hex(),
        sub_model=data[9],
        flags=flags,
        lid=lid,
        state=data[11],
        left=battery(left_v),
        right=battery(right_v),
        case=battery(case_v),
    )


@dataclass
class _Opening:
    open: bool = False
    last_open_at: float = 0.0
    shown: bool = False


class CaseOpenings:
    """跨扫描批次跟踪每个 model 的盒盖状态，保证一次开盖只触发一次。"""

    def __init__(self):
        self._openings: dict[str, _Opening] = {}
        self._last_shown_at: Optional[float] = None

    def step(self, beacon: Beacon, now_ms: float) -> bool:
        """喂入一条信标，返回是否应该弹窗。"""
        if not beacon.model_id:
            return False

        prev = self._openings.get(beacon.model_id)
        if beacon.lid == LidState.OPEN:
            continues = (
                prev is not None and prev.open
                and 0 <= now_ms - prev.last_open_at <= OPENING_GAP_MS
            )
            if continues:
                prev.last_open_at = now_ms
                opening = prev
            else:
                opening = _Opening(open=True, last_open_at=now_ms)
                self._openings[beacon.model_id] = opening
        elif beacon.lid in (LidState.CLOSED, LidState.TRANSITIONAL):
            if prev is not None:
                prev.open = False
                prev.shown = False
            return False
        elif beacon.lid == LidState.BUDS_OUT:
            if prev is not None and prev.open:
                prev.last_open_at = now_ms
                opening = prev
            else:
                # 盒外耳机持续广播，不算一次新开盖
                self._openings[beacon.model_id] = _Opening(
                    open=True, last_open_at=now_ms, shown=True)
                return False
        else:
            return False

        opening = self._openings[beacon.model_id]
        if not opening.open or opening.shown:
            return False
        if self._last_shown_at is not None and \
                0 <= now_ms - self._last_shown_at < POPUP_COOLDOWN_MS:
            return False

        opening.shown = True
        self._last_shown_at = now_ms
        return True

    def reset(self):
        self._openings.clear()
        self._last_shown_at = None


class CaseBeaconWatcher:
    """
    蓝牙 BLE 广播监听器。

    事件在 WinRT 线程池线程触发，只做纯 Python 解析，结果放入队列；
    由 Qt 线程定时调用 drain() 取走并触发回调，避免跨线程操作 Qt。
    """

    def __init__(self, on_case_open: Optional[Callable[[Beacon], None]] = None,
                 on_beacon: Optional[Callable[[Beacon], None]] = None):
        self._on_case_open = on_case_open
        self._on_beacon = on_beacon          # 每条信标（用于学习本机 model id）
        self._queue: "queue.Queue" = queue.Queue()
        self._openings = CaseOpenings()
        self._watcher = None
        self._token = None
        self._lock = threading.Lock()
        self._stopped = True

    # ------------------------------------------------------------------

    def start(self) -> bool:
        """启动扫描。失败（非 Windows / 无蓝牙）返回 False。"""
        if not self._stopped:
            return True
        try:
            from winsdk.windows.devices.bluetooth.advertisement import (
                BluetoothLEAdvertisementWatcher,
            )
            watcher = BluetoothLEAdvertisementWatcher()
            self._token = watcher.add_received(self._on_received)
            watcher.start()
            self._watcher = watcher
            self._stopped = False
            return True
        except Exception:
            self._watcher = None
            return False

    def stop(self):
        with self._lock:
            if self._watcher is not None:
                try:
                    if self._token is not None:
                        self._watcher.remove_received(self._token)
                    self._watcher.stop()
                except Exception:
                    pass
            self._watcher = None
            self._stopped = True
            self._openings.reset()

    # ------------------------------------------------------------------

    def _on_received(self, sender, args):
        """WinRT 线程回调：解析 + 状态机，绝不触碰 Qt。"""
        try:
            rssi = args.raw_signal_strength_in_d_bm
            for section in args.advertisement.data_sections:
                if section.data_type != AD_TYPE_SERVICE_DATA_16:
                    continue
                data = _buffer_to_bytes(section.data)
                if data[:2] != FDEE_UUID_LE:
                    continue
                beacon = parse_fdee_service_data(data[2:])
                if beacon is None:
                    return
                self._queue.put(("beacon", beacon, rssi))
                if rssi < RSSI_THRESHOLD:
                    return
                now_ms = time.monotonic() * 1000
                if self._openings.step(beacon, now_ms):
                    self._queue.put(("open", beacon, rssi))
                return
        except Exception:
            pass

    def drain(self):
        """Qt 线程调用：取出待处理事件。"""
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return events


def _buffer_to_bytes(buf) -> bytes:
    """winsdk IBuffer → bytes（Array 构造在该版本 winsdk 上不可用，逐字节读）。"""
    from winsdk.windows.storage.streams import DataReader
    reader = DataReader.from_buffer(buf)
    n = buf.length
    out = bytearray()
    for _ in range(n):
        out.append(reader.read_byte())
    return bytes(out)
