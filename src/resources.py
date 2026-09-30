"""资源容量台账：船期箱位、船闸/港口/报关/铁路/查验/收货窗口。

不可违背的两条规则：
1. 任一资源桶内 *活动占用* 之和不得超过容量（超订即拒绝）；
2. 一次占用只能释放一次——重复释放是错误而不是再还一份容量；
   释放后的容量立即可以被新计划占用，但不会被同一票货的新旧两份计划同时持有。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Reservation, SlotBook, Window


class CapacityError(Exception):
    """容量不足，无法占用。"""


class DoubleReleaseError(Exception):
    """同一占用被释放第二次——通常意味着重排把旧计划算了两遍。"""


@dataclass
class _Bucket:
    code: str
    kind: str
    capacity: float
    used: float = 0.0
    reservations: dict[str, Reservation] = field(default_factory=dict)

    def available(self) -> float:
        return self.capacity - self.used


class ResourceLedger:
    def __init__(self) -> None:
        self._books: dict[str, SlotBook] = {}
        self._windows: dict[str, Window] = {}
        self._buckets: dict[str, _Bucket] = {}
        # res_id -> bucket code，防止跨桶重复登记
        self._res_index: dict[str, str] = {}

    # -- 注册 ------------------------------------------------------------
    def register_book(self, book: SlotBook) -> None:
        self._books[book.code] = book
        self._buckets[book.code] = _Bucket(book.code, book.kind, book.capacity_teu)

    def register_window(self, win: Window) -> None:
        self._windows[win.code] = win
        self._buckets[win.code] = _Bucket(win.code, win.kind, win.capacity_per_window)

    # -- 查询 ------------------------------------------------------------
    @property
    def books(self) -> dict[str, SlotBook]:
        return dict(self._books)

    @property
    def windows(self) -> dict[str, Window]:
        return dict(self._windows)

    def book(self, code: str) -> SlotBook:
        return self._books[code]

    def window(self, code: str) -> Window:
        return self._windows[code]

    def capacity(self, code: str) -> float:
        return self._buckets[code].capacity

    def used(self, code: str) -> float:
        return self._buckets[code].used

    def available(self, code: str) -> float:
        return self._buckets[code].available()

    def active_teu_by(self, split_id: str) -> dict[str, float]:
        """某批次当前仍持有的全部占用（供重排前后核对）。"""
        out: dict[str, float] = {}
        for b in self._buckets.values():
            for r in b.reservations.values():
                if r.active and r.reason.startswith(f"split={split_id};"):
                    out[b.code] = out.get(b.code, 0.0) + r.teu
        return out

    def reservation(self, res_id: str) -> Reservation:
        code = self._res_index[res_id]
        return self._buckets[code].reservations[res_id]

    # -- 占用 / 释放 -----------------------------------------------------
    def reserve(self, resource_code: str, teu: float, res_id: str,
                split_id: str, leg_id: str, reason: str = "") -> Reservation:
        if teu <= 0:
            raise ValueError("占用箱量必须为正")
        if res_id in self._res_index:
            raise CapacityError(f"占用编号重复: {res_id}")
        bucket = self._buckets.get(resource_code)
        if bucket is None:
            raise KeyError(f"未知资源: {resource_code}")
        if teu > bucket.available() + 1e-9:
            raise CapacityError(
                f"资源 {resource_code} 容量不足: 需要 {teu} TEU, 剩余 {bucket.available():.2f} TEU")
        tag = f"split={split_id};leg={leg_id}" + (f";{reason}" if reason else "")
        res = Reservation(
            res_id=res_id,
            resource_code=resource_code,
            resource_kind=bucket.kind,
            bucket=bucket.code,
            teu=teu,
            reason=tag,
        )
        bucket.used += teu
        bucket.reservations[res_id] = res
        self._res_index[res_id] = bucket.code
        return res

    def release(self, res: Reservation, event_id: str, reason: str) -> None:
        bucket_code = self._res_index.get(res.res_id)
        if bucket_code is None:
            raise DoubleReleaseError(f"占用 {res.res_id} 未在台账登记")
        bucket = self._buckets[bucket_code]
        stored = bucket.reservations.get(res.res_id)
        if stored is None or not stored.active:
            # 关键防护：同一释放不会把容量还回两次
            raise DoubleReleaseError(f"占用 {res.res_id} 已释放，禁止重复释放")
        stored.active = False
        stored.released_by_event = event_id
        stored.reason += f" | released: {reason}"
        bucket.used -= res.teu
        if bucket.used < -1e-9:
            raise DoubleReleaseError("台账已用容量为负，释放逻辑出错")

    def audit(self) -> list[str]:
        """自检：返回所有违规描述（无违规为空列表）。"""
        problems = []
        for b in self._buckets.values():
            active_sum = sum(r.teu for r in b.reservations.values() if r.active)
            if abs(active_sum - b.used) > 1e-6:
                problems.append(f"{b.code}: 活动占用合计 {active_sum} 与台账已用 {b.used} 不一致")
            if b.used > b.capacity + 1e-9:
                problems.append(f"{b.code}: 超订 {b.used} > {b.capacity}")
        return problems
