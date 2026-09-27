"""资源占用日历。

五类按时间桶管理的稀缺资源，全部以 TEU 计容量：
- VOY    班轮/班车/班列的某一具体班次（出发时刻）
- LOCK   船闸的某一具体过闸窗口
- TERM   码头某自然日作业能力
- CUSTOMS 报关行/口岸某自然日接单能力
- APPT   目的地收货方某自然日预约名额

预留(hold)按批原子进行：任一桶容量不足则整批失败，已检查的预留不落库，
因此不可能出现"占了一半"。重排使用 swap()：先按"剔除待释放占用"后的口径
校验新占用，再原子地释放旧占用、落新占用——同批重订同一桶位也合法，
释放出的资源不会被重复占用。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

VOY = "VOY"
LOCK = "LOCK"
TERM = "TERM"
CUSTOMS = "CUSTOMS"
APPT = "APPT"
CONT = "CONT"


@dataclass(frozen=True)
class Hold:
    booking_id: str
    kind: str
    key: str            # 服务/船闸/码头/报关/预约集合 id
    bucket: str         # 时间桶（班次为出发时刻，其余为日期）
    teu: int
    capacity: int
    shipment_id: str
    step_seq: int
    label: str

    @property
    def bucket_key(self) -> tuple[str, str, str]:
        return (self.kind, self.key, self.bucket)


class CapacityError(Exception):
    def __init__(self, conflicts: list[str]):
        super().__init__("资源容量不足: " + "; ".join(conflicts))
        self.conflicts = conflicts


class BookingError(Exception):
    pass


class ResourceCalendar:
    def __init__(self):
        self._holds: dict[str, Hold] = {}
        self._usage: dict[tuple[str, str, str], dict[str, int]] = {}
        self._cap: dict[tuple[str, str, str], int] = {}
        self._consumed: set[str] = set()   # 已实际核销：仍占容量，但不可再释放/改订
        self._seq = 0

    # ---------------------------------------------------------------- 内部
    def _new_id(self) -> str:
        self._seq += 1
        return f"B{self._seq:05d}"

    def _used(self, kind: str, key: str, bucket: str) -> int:
        return sum(self._usage.get((kind, key, bucket), {}).values())

    def _describe(self, kind: str, key: str, bucket: str) -> str:
        names = {VOY: "班次", LOCK: "船闸窗口", TERM: "码头日班",
                 CUSTOMS: "报关名额", APPT: "收货预约", CONT: "空箱供给(周)"}
        return f"{names[kind]} {key} @ {bucket}"

    # ---------------------------------------------------------------- 预留
    def hold_many(self, requests: list[dict]) -> list[str]:
        """原子批量预留。request: kind/key/bucket(datetime|str)/teu/capacity/
        shipment_id/step_seq/label。全部成功才返回 booking id 列表。"""
        if not requests:
            return []
        normalized: list[tuple[tuple[str, str, str], int, int, dict]] = []
        for req in requests:
            bucket = req["bucket"]
            if isinstance(bucket, datetime):
                bucket = self._bucket_for(req["kind"], bucket)
            bk = (req["kind"], req["key"], bucket)
            if req["teu"] <= 0:
                raise BookingError("预留箱量必须为正")
            normalized.append((bk, int(req["teu"]), int(req["capacity"]), req))

        # 1) 按桶汇总并一次性校验（含本批内部叠加）
        tentative: dict[tuple[str, str, str], int] = {}
        capacities: dict[tuple[str, str, str], int] = {}
        for bk, teu, cap, _ in normalized:
            tentative[bk] = tentative.get(bk, 0) + teu
            capacities[bk] = cap
        conflicts = []
        for bk, add in tentative.items():
            cap = capacities[bk]
            if self._used(*bk) + add > cap:
                conflicts.append(
                    f"{self._describe(*bk)} 需 {self._used(*bk) + add}TEU > 容量 {cap}TEU")
        if conflicts:
            raise CapacityError(conflicts)

        # 2) 全部通过后落库
        ids = []
        for bk, teu, cap, req in normalized:
            booking_id = self._new_id()
            bucket = bk[2]
            hold = Hold(booking_id, bk[0], bk[1], bucket, teu, cap,
                        req["shipment_id"], req["step_seq"], req.get("label", ""))
            self._holds[booking_id] = hold
            self._usage.setdefault(bk, {})[booking_id] = teu
            self._cap[bk] = cap
            ids.append(booking_id)
        return ids

    @staticmethod
    def _bucket_for(kind: str, when: datetime) -> str:
        if kind in (VOY, LOCK):
            return when.strftime("%Y-%m-%d %H:%M")
        if kind == CONT:
            return f"{when.isocalendar().year}-W{when.isocalendar().week:02d}"
        return when.strftime("%Y-%m-%d")

    # ---------------------------------------------------------------- 释放
    def release(self, booking_ids: list[str]) -> None:
        """释放未执行占用。编号不存在、已释放或已核销即报错——杜绝重复释放。"""
        unknown = [b for b in booking_ids
                   if b not in self._holds or b in self._consumed]
        if unknown:
            raise BookingError(f"占用编号不存在、已释放或已核销，拒绝重复释放: {unknown}")
        for bid in booking_ids:
            hold = self._holds.pop(bid)
            bucket = hold.bucket_key
            del self._usage[bucket][bid]
            if not self._usage[bucket]:
                del self._usage[bucket]

    def consume(self, booking_ids: list[str]) -> None:
        """航段实际执行后核销占用：保留容量计数（该名额确实被用掉），
        但此后不可再被释放或改订。"""
        unknown = [b for b in booking_ids if b not in self._holds]
        if unknown:
            raise BookingError(f"占用编号不存在，无法核销: {unknown}")
        for bid in booking_ids:
            self._consumed.add(bid)

    def swap(self, release_ids: list[str], new_requests: list[dict]) -> list[str]:
        """原子重排：校验新预留时先剔除待释放占用，通过后同时释放+预留。

        这样"释放旧箱位、占用新箱位"要么全成要么全不成；即便新箱位与旧箱位
        是同一时间桶（如同班次补舱），也不会把自己算作占用方。"""
        # 待释放编号必须有效且未核销
        missing = [b for b in release_ids
                   if b not in self._holds or b in self._consumed]
        if missing:
            raise BookingError(f"待释放占用不存在或已核销: {missing}")
        released_buckets = {self._holds[b].bucket_key for b in release_ids}

        normalized = []
        for req in new_requests:
            bucket = req["bucket"]
            if isinstance(bucket, datetime):
                bucket = self._bucket_for(req["kind"], bucket)
            normalized.append(((req["kind"], req["key"], bucket),
                               int(req["teu"]), int(req["capacity"]), req))

        def used_excluding_released(bk) -> int:
            entries = self._usage.get(bk, {})
            return sum(teu for bid, teu in entries.items() if bid not in set(release_ids))

        tentative: dict[tuple[str, str, str], int] = {}
        capacities: dict[tuple[str, str, str], int] = {}
        for bk, teu, cap, _ in normalized:
            tentative[bk] = tentative.get(bk, 0) + teu
            capacities[bk] = cap
        conflicts = []
        for bk, add in tentative.items():
            base = used_excluding_released(bk) if bk in released_buckets else self._used(*bk)
            cap = capacities[bk]
            if base + add > cap:
                conflicts.append(
                    f"{self._describe(*bk)} 需 {base + add}TEU > 容量 {cap}TEU")
        if conflicts:
            raise CapacityError(conflicts)

        if release_ids:
            self.release(release_ids)
        return self.hold_many(new_requests)

    # ---------------------------------------------------------------- 查询
    def fits(self, kind: str, key: str, when, teu: int, capacity: int,
             exclude: set[str] | None = None) -> bool:
        """只读探测：在给定时间桶再加 teu 是否仍不超容量。"""
        bucket = when if isinstance(when, str) else self._bucket_for(kind, when)
        entries = self._usage.get((kind, key, bucket), {})
        used = sum(t for b, t in entries.items() if b not in (exclude or set()))
        return used + teu <= capacity

    def holds_of(self, shipment_id: str) -> list[Hold]:
        """该分段当前仍可改/可释放的未核销占用。"""
        return [h for h in self._holds.values()
                if h.shipment_id == shipment_id and h.booking_id not in self._consumed]

    def utilization(self) -> list[dict]:
        """当前所有时间桶的占用情况（供调度员查看瓶颈）。"""
        rows = []
        for bk, entries in sorted(self._usage.items()):
            used = sum(entries.values())
            rows.append({"kind": bk[0], "key": bk[1], "bucket": bk[2],
                         "used_teu": used, "capacity_teu": self._cap[bk],
                         "free_teu": self._cap[bk] - used,
                         "bookings": sorted(entries)})
        return rows

    def snapshot(self) -> dict:
        return {"active_holds": len(self._holds),
                "buckets": self.utilization()}
