"""前向排程器。

对一条路线（或路线的尾部若干航段）做链式排程：
- 车船航段绑定具体 *航次箱位*，并满足截关时间；
- 运河航段按三级船闸的通过时刻反推可开行时间；
- 港口/报关/场站/预约绑定具体 *时间窗*；
- 每个资源选择都做容量校验。

两种用法：
- ``commit=False`` 影子排程：只算结果、不占容量，供调度员比较路线；
- ``commit=True`` 正式排程：把选中的资源写入容量台账，失败整体回滚。
重排时可通过 ``ignore_res_ids`` 让本票旧占用先视为已释放：
服务层在调用前会先真正释放这些占用，因此同一份箱量不会新旧两处重复占用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import LegDef, Mode, Route
from .resources import CapacityError, ResourceLedger

WINDOW_KINDS_START = {"port", "customs", "rail", "border"}  # 窗内开始作业即可
WINDOW_KINDS_END = {"appointment"}                          # 必须在窗闭前到达


class SchedulingError(Exception):
    """没有可行排程（容量/窗口/截关均无法满足）。"""


@dataclass
class PlanPick:
    series: str
    bucket: str
    at: datetime
    res_id: str | None = None


@dataclass
class PlanLeg:
    seq: int
    leg_key: str
    label: str
    mode: Mode
    origin: str
    dest: str
    distance_km: int
    depart: datetime
    arrive: datetime
    cost: float
    picks: list[PlanPick] = field(default_factory=list)


@dataclass
class Plan:
    route_code: str
    quote_id: str
    teu: float
    legs: list[PlanLeg]
    ready_at: datetime

    @property
    def eta(self) -> datetime:
        return self.legs[-1].arrive

    @property
    def cost(self) -> float:
        return sum(l.cost for l in self.legs)

    def describe(self) -> list[str]:
        out = []
        for l in self.legs:
            picks = "、".join(f"{p.series}@{p.at:%m-%d %H:%M}" for p in l.picks) or "自有运力"
            out.append(f"{l.seq}. {l.label} {l.depart:%m-%d %H:%M}→{l.arrive:%m-%d %H:%M} [{picks}]")
        return out


class Planner:
    def __init__(self, ledger: ResourceLedger) -> None:
        self.ledger = ledger

    # ------------------------------------------------------------------
    def _avail(self, bucket: str, ignore: frozenset[str]) -> float:
        base = self.ledger.available(bucket)
        if not ignore:
            return base
        for r in self.ledger._buckets[bucket].reservations.values():  # noqa: SLF001
            if r.active and r.res_id in ignore:
                base += r.teu  # 本票旧占用，正式提交前会被真正释放
        return base

    def _earliest_window(self, series: str, ready: datetime, duration: timedelta,
                         teu: float, ignore: frozenset[str], blacklist: frozenset[str]
                         ) -> tuple[object, datetime, PlanPick]:
        wins = sorted(
            (w for w in self.ledger.windows.values()
             if w.series == series and w.closes >= ready),
            key=lambda w: w.opens,
        )
        for w in wins:
            if w.code in blacklist:
                continue
            if self._avail(w.code, ignore) + 1e-9 < teu:
                continue
            start = max(ready, w.opens)
            if start + duration <= w.closes:
                return w, start, PlanPick(series, w.code, start)
        raise SchedulingError(f"资源窗 {series} 在 {ready:%m-%d %H:%M} 后无可用容量/时段")

    def _earliest_book(self, series: str, ready: datetime, teu: float,
                       ignore: frozenset[str], blacklist: frozenset[str],
                       end_series, end_proc_hours
                       ) -> tuple[object, datetime, datetime, list[PlanPick]]:
        books = sorted(
            (b for b in self.ledger.books.values()
             if b.series == series and b.depart_at >= ready),
            key=lambda b: b.depart_at,
        )
        for b in books:
            if b.code in blacklist:
                continue
            if b.depart_at - timedelta(hours=b.cutoff_hours) < ready:
                continue  # 赶不上截关
            if self._avail(b.code, ignore) + 1e-9 < teu:
                continue
            picks = [PlanPick(series, b.code, b.depart_at)]
            arrive = b.arrive_at
            feasible = True
            for es in end_series:
                try:
                    _, start, pick = self._earliest_window(
                        es, arrive, timedelta(hours=end_proc_hours),
                        teu, ignore, blacklist)
                except SchedulingError:
                    feasible = False
                    break
                picks.append(pick)
                arrive = start + timedelta(hours=end_proc_hours)
            if feasible:
                return b, b.depart_at, arrive, picks
        raise SchedulingError(f"船期/班列 {series} 在 {ready:%m-%d %H:%M} 后无可订航次")

    def _canal(self, leg: LegDef, ready: datetime, teu: float,
               ignore: frozenset[str], blacklist: frozenset[str]
               ) -> tuple[datetime, datetime, list[PlanPick]]:
        """三级船闸：按各闸通过偏移反推当天可开行时段，并逐闸核容量。"""
        series_list = [leg.resource_code, *leg.extra_resources]
        offsets = {s: 3 + 4 * i for i, s in enumerate(series_list)}
        day = ready.date()
        for _ in range(14):
            wins_of_day = {}
            lo = datetime.min
            hi = datetime.max
            valid = True
            for s in series_list:
                wins = [w for w in self.ledger.windows.values()
                        if w.series == s and w.opens.date() == day]
                if not wins:
                    valid = False
                    break
                w = wins[0]
                wins_of_day[s] = w
                # 开行时间 d 必须满足 w.opens <= d+offset <= w.closes
                lo = max(lo, w.opens - timedelta(hours=offsets[s]))
                hi = min(hi, w.closes - timedelta(hours=offsets[s]))
            if valid and lo <= hi:
                depart = max(ready, lo)
                if depart <= hi:
                    picks = []
                    for s in series_list:
                        w = wins_of_day[s]
                        passage = depart + timedelta(hours=offsets[s])
                        if w.code in blacklist:
                            valid = False
                            break
                        if not (w.opens <= passage <= w.closes):
                            valid = False
                            break
                        if self._avail(w.code, ignore) + 1e-9 < teu:
                            valid = False
                            break
                        picks.append(PlanPick(s, w.code, passage))
                    if valid:
                        return depart, depart + leg.duration_hint, picks
            day = day + timedelta(days=1)
        raise SchedulingError(f"运河船闸 {series_list} 无法安排通过时刻")

    # ------------------------------------------------------------------
    def schedule(self, leg_defs: list[LegDef], teu: float, ready_at: datetime,
                 quote_per_leg: dict[str, float],
                 ignore_res_ids: frozenset[str] = frozenset(),
                 blacklist: frozenset[str] = frozenset(),
                 start_seq: int = 1, commit: bool = False,
                 split_id: str = "", plan_no: int = 0) -> list[PlanLeg]:
        planned: list[PlanLeg] = []
        tentative: list = []          # 提交模式下已占的资源，失败时回滚
        t = ready_at
        try:
            for ld in leg_defs:
                unit = quote_per_leg.get(ld.leg_key, ld.cost_per_teu)
                cost = round(unit * teu, 2)
                picks: list[PlanPick] = []

                if ld.resource_kind == "none":
                    depart, arrive = t, t + ld.duration_hint
                elif ld.resource_kind == "lock":
                    depart, arrive, picks = self._canal(ld, t, teu, ignore_res_ids, blacklist)
                elif ld.resource_kind in ("slot_ship", "slot_rail"):
                    _, depart, arrive, picks = self._earliest_book(
                        ld.resource_code, t, teu, ignore_res_ids, blacklist,
                        ld.end_resources, ld.end_proc_hours)
                elif ld.resource_kind in WINDOW_KINDS_START or ld.resource_kind in WINDOW_KINDS_END:
                    _, depart0, pick = self._earliest_window(
                        ld.resource_code, t, ld.duration_hint, teu,
                        ignore_res_ids, blacklist)
                    depart, arrive, picks = depart0, depart0 + ld.duration_hint, [pick]
                else:
                    raise SchedulingError(f"未知资源类型 {ld.resource_kind}")

                seq = start_seq + len(planned)
                pl = PlanLeg(
                    seq=seq, leg_key=ld.leg_key, label=ld.line_name,
                    mode=ld.mode, origin=ld.origin, dest=ld.dest,
                    distance_km=ld.distance_km, depart=depart, arrive=arrive,
                    cost=cost, picks=[])
                for p in picks:
                    pl.picks.append(p)
                    if commit:
                        res = self.ledger.reserve(
                            p.bucket, teu,
                            res_id=f"{split_id}#P{plan_no}-L{seq}-{p.series}",
                            split_id=split_id, leg_id=f"L{seq}",
                            reason=f"plan={plan_no}",
                        )
                        tentative.append(res)
                        p.res_id = res.res_id
                planned.append(pl)
                t = arrive
        except (SchedulingError, CapacityError, KeyError) as exc:
            if commit:
                for res in tentative:
                    self.ledger.release(res, event_id="ROLLBACK", reason="排程失败回滚")
            raise SchedulingError(str(exc)) from exc
        return planned

    # ------------------------------------------------------------------
    def shadow_route(self, route: Route, teu: float, ready_at: datetime,
                     quote_per_leg: dict[str, float],
                     ignore_res_ids=frozenset(), blacklist=frozenset()) -> Plan:
        legs = self.schedule(list(route.legs), teu, ready_at, quote_per_leg,
                             ignore_res_ids, blacklist, commit=False)
        return Plan(route.code, "", teu, legs, ready_at)
