"""Keep the laptop awake during a session; read power source and user idle time
(D-32). Windows APIs through ctypes; every call is injectable for tests.

* `hold()` / `release()`: `SetThreadExecutionState(ES_CONTINUOUS |
  ES_SYSTEM_REQUIRED)` so idle sleep cannot interrupt a session (lid-close and
  power-button actions still apply; the power plan handles those).
* `on_ac_power()`: `GetSystemPowerStatus().ACLineStatus == 1`.
* `idle_seconds()`: time since the last keyboard or mouse input
  (`GetLastInputInfo`), used to postpone sleep while the owner is using the
  laptop.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger("stockify.daemon.keepawake")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class Power:
    """Real Windows implementation."""

    def __init__(self):
        self.held = False

    def hold(self) -> None:
        if ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) == 0:
            log.warning("SetThreadExecutionState failed; the laptop may idle-sleep during the session")
            return
        self.held = True

    def release(self) -> None:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        self.held = False

    def on_ac_power(self) -> bool | None:
        status = _PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None
        return {0: False, 1: True}.get(status.ACLineStatus)        # 255 = unknown

    def idle_seconds(self) -> float:
        info = _LastInput(cbSize=ctypes.sizeof(_LastInput))
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        now = ctypes.windll.kernel32.GetTickCount()
        return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


class FakePower:
    """Test double."""

    def __init__(self, *, ac: bool | None = True, idle_s: float = 3600.0):
        self.ac, self.idle_s, self.held = ac, idle_s, False

    def hold(self) -> None:
        self.held = True

    def release(self) -> None:
        self.held = False

    def on_ac_power(self) -> bool | None:
        return self.ac

    def idle_seconds(self) -> float:
        return self.idle_s
