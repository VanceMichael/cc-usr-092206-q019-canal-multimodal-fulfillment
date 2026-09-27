"""朴素时间工具：全部时间均为业务当地时间，无时区换算。"""
from __future__ import annotations

from datetime import datetime, timedelta


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def fmt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def at(date_value, hhmm: str) -> datetime:
    h, m = hm(hhmm)
    return datetime(date_value.year, date_value.month, date_value.day, h, m)


def next_slot(after: datetime, weekdays, clocks, strict_after: bool = False) -> datetime:
    """最早不早于 after 的班期时刻。

    weekdays: 0=周一 .. 6=周日；clocks: 当日 "HH:MM" 列表（单个时刻也传列表）。
    strict_after=True 时要求严格晚于 after（用于已错过的班次/窗口）。
    """
    clocks = sorted(clocks)
    for delta in range(0, 15):
        day = after + timedelta(days=delta)
        if day.weekday() not in weekdays:
            continue
        for clock in clocks:
            candidate = at(day.date(), clock)
            if strict_after and candidate <= after:
                continue
            if not strict_after and candidate < after:
                continue
            return candidate
    raise RuntimeError(f"15 天内找不到班期: {weekdays} {clocks}")


def whole_days(a: datetime, b: datetime) -> int:
    """a 到 b 之间的完整天数（不足 24 小时不计）。"""
    return int((b - a).total_seconds() // 86400)


def hours_between(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds() / 3600, 1)
