"""仿真时钟：1 tick = 1 仿真分钟。"""
from __future__ import annotations


class Clock:
    def __init__(self, ticks_per_day: int = 1440, start_day: int = 1, start_tick: int = 360):
        self.ticks_per_day = ticks_per_day
        self.day = start_day
        self.tick = start_tick       # 当天已过 tick 数 (0..1440)
        self.total_ticks = start_tick

    def advance(self, n: int = 1) -> None:
        self.tick += n
        self.total_ticks += n
        while self.tick >= self.ticks_per_day:
            self.tick -= self.ticks_per_day
            self.day += 1

    # --- 派生 ---
    @property
    def hour(self) -> int:
        return self.tick // 60

    @property
    def minute(self) -> int:
        return self.tick % 60

    @property
    def clock_str(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def is_night(self) -> bool:
        return self.hour >= 21 or self.hour < 6

    @property
    def is_work_hours(self) -> bool:
        return 9 <= self.hour < 18

    @property
    def is_school_hours(self) -> bool:
        return 8 <= self.hour < 16

    def snapshot(self) -> dict:
        return {
            "day": self.day,
            "tick": self.tick,
            "clock": self.clock_str,
            "is_night": self.is_night,
        }
