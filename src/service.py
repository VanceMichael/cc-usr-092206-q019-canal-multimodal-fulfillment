"""履约编排服务。

把排程器、资源台账和运行态模型串起来，对外提供：
- 订舱（可分批）、承诺快照、替代路线基线；
- 航段执行：发运 / 到达 / 交接（报关放行、预约到货签收）；
- 异常处理：延误、甩箱、退关、改港 / 切换海铁、客户改量；
- 铁律：只重排 *尚未执行* 的航段；已发运航段冻结；
  重排先释放旧占用、再占新资源；旧承诺快照永久保留可追溯。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import (
    CommitmentSnapshot, Contract, CustomsDeclaration, DestinationAppointment,
    Event, EventType, LegState, Mode, ScheduledLeg, Shipment, Split, SplitState,
)
from .planning import PlanLeg, Planner, SchedulingError
from .resources import ResourceLedger
from .network import Network, _dt

EXPORT_CUSTOMS_LOCATIONS = {"QZ", "GZB", "FCG"}


class FulfillmentError(Exception):
    """履约操作不合法（状态不对 / 交接未完成等）。"""


class FulfillmentService:
    def __init__(self, network: Network) -> None:
        self.network = network
        self.ledger: ResourceLedger = network.ledger
        self.planner = Planner(network.ledger)
        self._ev = 0
        self._shipments: dict[str, Shipment] = {}

    def _eid(self) -> str:
        self._ev += 1
        return f"E{self._ev:04d}"

    def _log(self, shipment: Shipment, at: datetime, type_: EventType,
             split_id: str | None, leg_id: str | None, message: str,
             payload: dict | None = None) -> Event:
        ev = Event(self._eid(), at, type_, split_id, leg_id, message, payload or {})
        shipment.events.append(ev)
        return ev

    def _series(self, resource_code: str) -> str:
        if resource_code in self.ledger.books:
            return self.ledger.books[resource_code].series
        return self.ledger.windows[resource_code].series

    # ==================================================================
    # 合同与订舱
    # ==================================================================
    def load_contract(self, contract_no: str) -> Contract:
        c = self.network.contract_raw(contract_no)
        return Contract(
            contract_no=c["contract_no"], shipper=c["shipper"], consignee=c["consignee"],
            cargo=c["cargo"], incoterm=c["incoterm"], total_teu=float(c["total_teu"]),
            route_code=c["route_code"], quote_id=c["quote_id"],
            latest_ship_date=_dt(c["latest_ship_date"]),
            receive_window_start=_dt(c["receive_window_start"]),
            receive_window_end=_dt(c["receive_window_end"]),
            unit_price=float(c.get("unit_price", 0)),
        )

    def book(self, contract_no: str, at: datetime,
             split_teu: list[float] | None = None,
             ready_at: datetime | None = None) -> Shipment:
        contract = self.load_contract(contract_no)
        shipment = Shipment(contract=contract)
        self._shipments[contract_no] = shipment
        teus = split_teu or [contract.total_teu]
        if abs(sum(teus) - contract.total_teu) > 1e-9:
            raise FulfillmentError("分批箱量之和必须等于合同箱量")
        ready = ready_at or at
        # 第一阶段：建批次，并在本票尚未占任何资源的干净台账上做替代路线反事实
        splits: list[tuple[Split, float]] = []
        for i, teu in enumerate(teus, 1):
            split_id = f"{contract_no}#S{i}"
            split = Split(split_id=split_id, contract_no=contract_no, teu=teu,
                          route_code=contract.route_code, quote_id=contract.quote_id)
            shipment.splits[split_id] = split
            split.counterfactuals = self._baselines_for(teu, ready, frozenset())
            splits.append((split, teu))
        # 第二阶段：正式占用合同路线资源
        for split, teu in splits:
            plan_legs = self._commit_suffix(split, 1, teu, ready,
                                            contract.route_code, contract.quote_id,
                                            split.plan_counter, frozenset())
            split.legs = self._instantiate(split, plan_legs, split.plan_counter)
            self._register_docs(shipment, split)
            self._snapshot(split, at, "订舱承诺", None)
            self._log(shipment, at, EventType.BOOKED, split.split_id, None,
                      f"订舱 {teu} TEU，路线 {self.network.route(contract.route_code).name}",
                      {"teu": teu, "route": contract.route_code})

        shipment.baselines = self._aggregate_baselines(shipment)
        return shipment

    def get(self, contract_no: str) -> Shipment:
        return self._shipments[contract_no]

    def _baselines_for(self, teu: float, ready: datetime,
                       blacklist: frozenset[str]) -> dict[str, dict]:
        out = {}
        for code, rt in self.network.routes.items():
            q = self.network.quote(rt.quote_ids[-1])
            try:
                plan = self.planner.shadow_route(rt, teu, ready, q.per_leg,
                                                 blacklist=blacklist)
                out[code] = {"name": rt.name, "distance_km": rt.distance_km,
                             "eta": plan.eta, "cost": round(plan.cost, 2),
                             "cost_per_teu": round(plan.cost / teu, 2),
                             "quote_id": q.quote_id, "lines": plan.describe()}
            except SchedulingError as exc:
                out[code] = {"name": rt.name, "distance_km": rt.distance_km,
                             "eta": None, "cost": None, "infeasible": True,
                             "reason": str(exc)}
        return out

    def _aggregate_baselines(self, shipment: Shipment) -> dict[str, dict]:
        """各批次反事实汇总为一票货口径（批次并行，ETA 取最晚，费用求和）。"""
        codes = list(next(iter(shipment.splits.values())).counterfactuals)
        agg: dict[str, dict] = {}
        for code in codes:
            cfs = [s.counterfactuals[code] for s in shipment.splits.values()]
            if all(c.get("eta") for c in cfs):
                agg[code] = {
                    "name": cfs[0]["name"], "distance_km": cfs[0]["distance_km"],
                    "eta": max(c["eta"] for c in cfs),
                    "cost": round(sum(c["cost"] for c in cfs), 2),
                    "per_split": {s.split_id: s.counterfactuals[code]
                                  for s in shipment.splits.values()},
                }
            else:
                agg[code] = {"name": cfs[0]["name"], "eta": None, "cost": None,
                             "infeasible": True}
        return agg

    # ==================================================================
    # 排程 / 重排
    # ==================================================================
    def _commit_suffix(self, split: Split, seq_from: int, teu: float,
                       ready: datetime, route_code: str, quote_id: str,
                       plan_no: int, blacklist: frozenset[str]) -> list[PlanLeg]:
        rt = self.network.route(route_code)
        q = self.network.quote(quote_id)
        tail = [ld for ld in rt.legs if ld.seq >= seq_from]
        return self.planner.schedule(
            tail, teu, ready, q.per_leg, start_seq=seq_from,
            commit=True, split_id=split.split_id, plan_no=plan_no,
            blacklist=blacklist)

    def _instantiate(self, split: Split, plan_legs: list[PlanLeg],
                     plan_no: int) -> list[ScheduledLeg]:
        rt = self.network.route(split.route_code)
        defs = {ld.seq: ld for ld in rt.legs}
        out: list[ScheduledLeg] = []
        for pl in plan_legs:
            ld = defs[pl.seq]
            reservations = [self.ledger.reservation(pk.res_id)
                            for pk in pl.picks if pk.res_id]
            out.append(ScheduledLeg(
                leg_id=f"{split.split_id}#P{plan_no}-L{pl.seq}",
                split_id=split.split_id, seq=pl.seq, mode=pl.mode,
                origin=pl.origin, dest=pl.dest, distance_km=pl.distance_km,
                line_name=pl.label,
                planned_depart=pl.depart, planned_arrive=pl.arrive,
                planned_cost=pl.cost,
                resource_code=ld.resource_code, resource_kind=ld.resource_kind,
                reservations=reservations, note=ld.note))
        return out

    def _release_suffix(self, shipment: Shipment, split: Split, seq_from: int,
                        event: Event, reason: str) -> None:
        """作废未执行航段并释放其全部资源占用（每个占用只释放一次）。

        断点上唯一允许"覆盖已闭环航段"的情形：报关已 *退关* 的出口海关航段——
        旧航段保留在历史里计费留痕，新计划在同一 seq 重报；其余已发运航段一律冻结。
        """
        for leg in list(split.legs):
            if leg.seq < seq_from or leg.state == LegState.SKIPPED:
                continue
            if leg.locked:
                withdrawn = leg.mode == Mode.CUSTOMS and any(
                    d.leg_id == leg.leg_id and d.withdrawn
                    for d in shipment.declarations.values())
                if not (withdrawn and leg.seq == seq_from):
                    raise FulfillmentError(
                        f"航段 {leg.seq}（{leg.line_name}）已发运，不能重排；"
                        f"异常只能作用于其后尚未执行的部分")
                # 已退关的出口报关航段：留痕（费用沉没）、退出活动计划，
                # 新计划在同一 seq 重新申报；旧报关窗容量已实际消耗，不回收。
                leg.replaced = True
                leg.note = f"{leg.note} | 原放行已退关，由 {event.at:%m-%d %H:%M} 新计划重报".strip(" |")
                continue
            for res in leg.reservations:
                if res.active:
                    self.ledger.release(res, event.event_id, reason)
            leg.state = LegState.SKIPPED
            leg.note = f"{leg.note} | 作废于 {event.at:%m-%d %H:%M}:{reason}".strip(" |")
            # 未放行的出口报关单随航段作废，由新计划重新申报
            for d in shipment.declarations.values():
                if d.leg_id == leg.leg_id and not d.cleared and not d.withdrawn:
                    d.abandoned = True
            for appt in shipment.appointments.values():
                if appt.split_id != split.split_id or appt.cancelled or appt.kept:
                    continue
                if any(r.resource_code == appt.window_code for r in leg.reservations):
                    appt.cancelled = True

    def _register_docs(self, shipment: Shipment, split: Split) -> None:
        existing_decls = {d.decl_id for d in shipment.declarations.values()}
        existing_appts = {a.appt_id for a in shipment.appointments.values()}
        for leg in split.active_legs:
            if leg.mode == Mode.CUSTOMS:
                did = f"DC-{split.split_id}#P{self._plan_of(leg)}-L{leg.seq}"
                if did not in existing_decls:
                    shipment.declarations[did] = CustomsDeclaration(
                        decl_id=did, split_id=split.split_id, leg_id=leg.leg_id,
                        customs_code=leg.resource_code or "", teu=split.teu,
                        declared_at=leg.planned_depart)
            for res in leg.reservations:
                if self._series(res.resource_code) == "APPT-HAN":
                    w = self.ledger.windows[res.resource_code]
                    aid = f"AP-{split.split_id}#P{self._plan_of(leg)}-L{leg.seq}"
                    if aid not in existing_appts:
                        shipment.appointments[aid] = DestinationAppointment(
                            appt_id=aid, split_id=split.split_id,
                            window_code=w.code, teu=split.teu,
                            slot_start=w.opens, slot_end=w.closes)

    @staticmethod
    def _plan_of(leg: ScheduledLeg) -> int:
        return int(leg.leg_id.split("#P")[1].split("-")[0])

    def _snapshot(self, split: Split, at: datetime, reason: str,
                  event: Event | None) -> CommitmentSnapshot:
        for s in split.snapshots:
            s.superseded = True
        per_leg = {
            leg.leg_id: {"seq": leg.seq, "label": leg.line_name,
                         "cost": leg.cost, "state": leg.state.value,
                         "planned_depart": leg.planned_depart,
                         "planned_arrive": leg.planned_arrive}
            for leg in split.active_legs
        }
        if split.active_legs:
            eta = max(l.planned_arrive for l in split.active_legs)
            cost = round(sum(l.cost for l in split.active_legs), 2)
        else:
            # 整段待重排（甩箱暂无可行资源）：沿用上一版承诺时间/费用口径
            eta = split.snapshots[-1].eta if split.snapshots else at
            cost = split.snapshots[-1].cost_total if split.snapshots else 0.0
        snap = CommitmentSnapshot(
            snap_no=len(split.snapshots) + 1, at=at, reason=reason,
            split_id=split.split_id,
            legs=tuple(l.leg_id for l in split.active_legs),
            eta=eta, cost_total=cost,
            per_leg=per_leg, event_ref=event.event_id if event else None)
        split.snapshots.append(snap)
        return snap

    def _boundary_time(self, split: Split, seq_from: int) -> datetime:
        prev = [l for l in split.legs if l.seq < seq_from and l.state != LegState.SKIPPED]
        if not prev:
            return min(l.planned_depart for l in split.legs)
        last = max(prev, key=lambda l: l.seq)
        return last.actual_arrive or last.planned_arrive

    def _replan(self, shipment: Shipment, split: Split, at: datetime,
                seq_from: int, reason: str, event_type: EventType,
                route_code: str | None = None, quote_id: str | None = None,
                blacklist: frozenset[str] | None = None,
                ready: datetime | None = None, teu: float | None = None) -> CommitmentSnapshot:
        route_code = route_code or split.route_code
        blacklist = split.last_blacklist if blacklist is None else tuple(blacklist)
        blacklist = frozenset(blacklist)
        teu = split.teu if teu is None else teu
        ready = max(ready or self._boundary_time(split, seq_from), at)
        plan_no = split.plan_counter + 1

        # 即将被释放的旧占用（退关报关等留痕航段的占用不在其列）
        to_release = {
            r.res_id
            for leg in split.legs
            if leg.seq >= seq_from and leg.state != LegState.SKIPPED and not leg.locked
            for r in leg.reservations if r.active
        }

        # 第一阶段：影子验证新尾部可行（把即将释放的占用视为空出），不可行则什么都不动
        rt = self.network.route(route_code)
        q = self.network.quote(quote_id or split.quote_id)
        tail = [ld for ld in rt.legs if ld.seq >= seq_from]
        try:
            self.planner.schedule(
                tail, teu, ready, q.per_leg, start_seq=seq_from,
                commit=False, blacklist=blacklist,
                ignore_res_ids=frozenset(to_release))
        except SchedulingError as exc:
            split.state = SplitState.ROLLED
            event = self._log(shipment, at, event_type, split.split_id, None,
                              f"{reason}（当前无可行资源，待重新安排）：{exc}",
                              {"seq_from": seq_from, "route": route_code, "teu": teu})
            self._snapshot(split, at, reason + "（暂无可行后续，待重排）", event)
            raise

        split.plan_counter = plan_no
        event = self._log(shipment, at, event_type, split.split_id, None, reason,
                          {"seq_from": seq_from, "route": route_code,
                           "teu": teu, "ready": ready.isoformat(),
                           "blacklist": sorted(blacklist)})
        # 第二阶段：先释放旧占用、再占新资源——同一份箱量绝不会新旧两处重复占用
        self._release_suffix(shipment, split, seq_from, event, reason)
        old_route = split.route_code
        split.route_code = route_code
        if quote_id:
            split.quote_id = quote_id
        plan_legs = self._commit_suffix(split, seq_from, teu, ready,
                                        route_code, split.quote_id,
                                        plan_no, blacklist)
        split.legs.extend(self._instantiate(split, plan_legs, plan_no))
        split.last_blacklist = tuple(blacklist)
        self._register_docs(shipment, split)
        snap = self._snapshot(split, at, reason, event)
        if split.state == SplitState.ROLLED:
            split.state = (SplitState.IN_TRANSIT
                           if any(l.executed for l in split.legs) else SplitState.PLANNED)
        return snap

    # ==================================================================
    # 异常事件
    # ==================================================================
    def _first_open(self, split: Split) -> ScheduledLeg:
        opens = [l for l in split.active_legs if not l.locked]
        if not opens:
            raise FulfillmentError("该批次已无未执行航段")
        return min(opens, key=lambda l: l.seq)

    def register_delay(self, shipment: Shipment, split_id: str, at: datetime,
                       wait_hours: float, reason: str,
                       blacklist: list[str] | None = None) -> CommitmentSnapshot:
        split = shipment.splits[split_id]
        first = self._first_open(split)
        ready = max(at + timedelta(hours=wait_hours), first.planned_depart)
        self._log(shipment, at, EventType.DELAY, split_id, first.leg_id,
                  f"{reason}；后续最早可开始时间 {ready:%m-%d %H:%M}",
                  {"wait_hours": wait_hours, "ready": ready.isoformat()})
        bl = split.last_blacklist if blacklist is None else tuple(split.last_blacklist) + tuple(blacklist)
        return self._replan(shipment, split, at, first.seq,
                            f"延误重排：{reason}", EventType.REPLANNED,
                            blacklist=frozenset(bl), ready=ready)

    def roll_container(self, shipment: Shipment, split_id: str, at: datetime,
                       reason: str, storage_per_teu: float = 500.0) -> CommitmentSnapshot:
        split = shipment.splits[split_id]
        first = self._first_open(split)
        blocked = sorted({r.resource_code for l in split.active_legs
                          for r in l.reservations
                          if l.seq >= first.seq and not l.locked
                          and r.resource_kind in ("slot_ship", "slot_rail")})
        if not blocked:
            raise FulfillmentError("未执行航段中没有可甩的船期/班列箱位")
        charge = round(storage_per_teu * split.teu, 2)
        shipment.extra_charges.append({
            "split_id": split_id, "at": at, "item": "甩箱港务堆存/改配费",
            "amount": charge, "reason": reason})
        self._log(shipment, at, EventType.ROLL, split_id, first.leg_id,
                  f"甩箱：{reason}；拉黑名单 {blocked}", {"blocked": blocked})
        bl = tuple(split.last_blacklist) + tuple(blocked)
        return self._replan(shipment, split, at, first.seq,
                            f"甩箱改配：{reason}", EventType.REPLANNED,
                            blacklist=frozenset(bl),
                            ready=max(at, self._boundary_time(split, first.seq)))

    def withdraw_customs(self, shipment: Shipment, split_id: str, at: datetime,
                         reason: str, penalty_per_teu: float = 400.0) -> Event:
        """退关：已申报（含已放行但未出境）的出口报关单撤回，改船改港前必须办理。

        只撤回单证与许可；后续航段资源由紧接着的 reroute/replan 释放重排，
        旧报关航段保留留痕、申报费沉没，新计划在同一序号重新申报。
        """
        split = shipment.splits[split_id]
        targets = []
        for d in shipment.declarations.values():
            if d.split_id != split_id or d.withdrawn:
                continue
            leg = next((l for l in shipment.all_legs() if l.leg_id == d.leg_id), None)
            if leg is None or leg.dest not in EXPORT_CUSTOMS_LOCATIONS:
                continue
            # 其后的国际航段（海/铁）一旦已发运，货物即已出境，不可退关
            sailed = any(l.locked and l.mode in (Mode.SEA, Mode.RAIL) and l.seq > leg.seq
                         for l in split.legs)
            if not sailed:
                targets.append((d, leg))
        if not targets:
            raise FulfillmentError("没有可退关的出口报关单（货物已出境或无有效申报）")
        for d, _leg in targets:
            d.withdrawn = True
            d.withdrawn_at = at
            bucket = next((r.resource_code for r in _leg.reservations
                           if r.resource_kind == "customs"), None)
            if bucket:
                d.customs_code = bucket
        charge = round(penalty_per_teu * split.teu, 2)
        shipment.extra_charges.append({
            "split_id": split_id, "at": at,
            "item": "退关手续费（撤放行/删单重报）",
            "amount": charge, "reason": reason})
        return self._log(shipment, at, EventType.CUSTOMS_WITHDRAWN, split_id, None,
                         f"退关：{reason}；单据 {[d.decl_id for d, _ in targets]}",
                         {"declarations": [d.decl_id for d, _ in targets],
                          "penalty": charge})

    def reroute(self, shipment: Shipment, split_id: str, at: datetime,
                new_route_code: str, reason: str, start_seq: int | None = None,
                quote_id: str | None = None,
                blacklist: list[str] | None = None) -> CommitmentSnapshot:
        """改港 / 切换海铁：从断点起换路线尾部。seq 必须与新路线航段序号对齐。"""
        split = shipment.splits[split_id]
        seq_from = start_seq or self._first_open(split).seq
        rt_new = self.network.routes[new_route_code]
        boundary = self._boundary_location(split, seq_from)
        new_tail = [ld for ld in rt_new.legs if ld.seq >= seq_from]
        if not new_tail or new_tail[0].origin != boundary:
            raise FulfillmentError(
                f"新路线 {new_route_code} 从航段 {seq_from} 起的起点必须是 {boundary}")
        bl = tuple(split.last_blacklist) if blacklist is None else tuple(blacklist)
        return self._replan(shipment, split, at, seq_from,
                            f"改线/改港：{reason}", EventType.REROUTE,
                            route_code=new_route_code, quote_id=quote_id,
                            blacklist=frozenset(bl))

    def change_quantity(self, shipment: Shipment, split_id: str, at: datetime,
                        new_teu: float, reason: str) -> CommitmentSnapshot:
        """客户改量：未执行部分按新箱量重排，释放的箱位立即回到台账可被再售。"""
        split = shipment.splits[split_id]
        if new_teu <= 0:
            raise FulfillmentError("改量后箱量必须为正；整票取消请走退关")
        if new_teu >= split.teu:
            raise FulfillmentError("本例只处理减量；增量请追加订舱")
        removed = round(split.teu - new_teu, 4)
        first = self._first_open(split)
        self._log(shipment, at, EventType.QUANTITY_CHANGED, split_id, first.leg_id,
                  f"客户改量：{split.teu}→{new_teu} TEU（-{removed}），{reason}",
                  {"old_teu": split.teu, "new_teu": new_teu, "removed": removed})
        split.teu = new_teu
        shipment.cancelled_teu = round(shipment.cancelled_teu + removed, 4)
        return self._replan(shipment, split, at, first.seq,
                            f"客户改量重排（{reason}）", EventType.REPLANNED,
                            ready=self._boundary_time(split, first.seq), teu=new_teu)

    def sell_released_capacity(self, bucket: str, teu: float, at: datetime,
                               buyer: str) -> str:
        """释放回台账的舱位/窗口被其他客户买走（验证容量可复用且不被重复占用）。"""
        res = self.ledger.reserve(
            bucket, teu, res_id=f"EXTERNAL:{buyer}:{bucket}:{at:%Y%m%d%H%M}",
            split_id=f"EXTERNAL-{buyer}", leg_id="-", reason="外部客户购买释放舱位")
        return res.res_id

    def alternatives(self, shipment: Shipment, split_id: str, at: datetime,
                     start_seq: int | None = None,
                     route_codes: list[str] | None = None,
                     blacklist: list[str] | None = None) -> list[dict]:
        """调度员视角：在当前断点上影子比较各路线尾部（不占资源、不改承诺）。"""
        split = shipment.splits[split_id]
        seq_from = start_seq or self._first_open(split).seq
        ready = max(at, self._boundary_time(split, seq_from))
        boundary = self._boundary_location(split, seq_from)
        bl = frozenset(blacklist) if blacklist is not None else frozenset(split.last_blacklist)
        sunk = round(sum(l.cost for l in split.active_legs if l.seq < seq_from), 2)
        out = []
        for code in (route_codes or list(self.network.routes)):
            rt = self.network.routes[code]
            q = self.network.quote(rt.quote_ids[-1])
            tail = [ld for ld in rt.legs if ld.seq >= seq_from]
            if not tail or tail[0].origin != boundary:
                continue
            try:
                legs = self.planner.schedule(
                    tail, split.teu, ready, q.per_leg, start_seq=seq_from,
                    commit=False, blacklist=bl)
                suffix_cost = round(sum(l.cost for l in legs), 2)
                out.append({
                    "route": code, "name": rt.name, "eta": legs[-1].arrive,
                    "suffix_cost": suffix_cost,
                    "sunk_cost": sunk,
                    "total_if_chosen": round(sunk + suffix_cost, 2),
                    "lines": [f"{l.seq}. {l.label} {l.depart:%m-%d %H:%M}→{l.arrive:%m-%d %H:%M}"
                              for l in legs]})
            except SchedulingError as exc:
                out.append({"route": code, "name": rt.name, "eta": None,
                            "infeasible": True, "reason": str(exc)})
        return sorted(out, key=lambda x: (x["eta"] is None, x["eta"] or at))

    def _boundary_location(self, split: Split, seq_from: int) -> str:
        prev = [l for l in split.legs if l.seq < seq_from and l.state != LegState.SKIPPED]
        if prev:
            return max(prev, key=lambda l: l.seq).dest
        return min(split.legs, key=lambda l: l.seq).origin

    # ==================================================================
    # 航段执行与交接
    # ==================================================================
    def depart(self, shipment: Shipment, split_id: str, seq: int, at: datetime) -> ScheduledLeg:
        split = shipment.splits[split_id]
        leg = self._leg(split, seq)
        if leg.state != LegState.PLANNED:
            raise FulfillmentError(f"航段 {seq} 状态 {leg.state.value}，不能发运")
        leg.state = LegState.DEPARTED
        leg.actual_depart = at
        if split.state == SplitState.PLANNED:
            split.state = SplitState.IN_TRANSIT
        self._log(shipment, at, EventType.DEPARTURE, split_id, leg.leg_id,
                  f"{leg.line_name} 发运", {"at": at.isoformat()})
        return leg

    def arrive(self, shipment: Shipment, split_id: str, seq: int, at: datetime) -> ScheduledLeg:
        split = shipment.splits[split_id]
        leg = self._leg(split, seq)
        if leg.state != LegState.DEPARTED:
            raise FulfillmentError(f"航段 {seq} 未发运，不能登记到达")
        leg.state = LegState.ARRIVED
        leg.actual_arrive = at
        self._log(shipment, at, EventType.ARRIVAL, split_id, leg.leg_id,
                  f"{leg.line_name} 到达", {"at": at.isoformat()})
        return leg

    def handover(self, shipment: Shipment, split_id: str, seq: int, at: datetime,
                 docs: list[str], party: str | None = None) -> ScheduledLeg:
        """箱货与单证齐备才算航段闭环：报关航段即放行，尾程即收货预约签收。"""
        split = shipment.splits[split_id]
        leg = self._leg(split, seq)
        if leg.state != LegState.ARRIVED:
            raise FulfillmentError(f"航段 {seq} 未到达，不能交接")
        if not docs:
            raise FulfillmentError("交接必须记录单证（提单/舱单/放行/签收等）")
        leg.state = LegState.DONE
        leg.handover_done = True
        leg.handover_docs = list(docs)
        leg.handover_at = at
        leg.handover_party = party
        self._log(shipment, at, EventType.HANDOVER, split_id, leg.leg_id,
                  f"{leg.line_name} 交接完成：{'、'.join(docs)}",
                  {"docs": docs, "party": party})
        if leg.mode == Mode.CUSTOMS:
            decl = next((d for d in shipment.declarations.values()
                         if d.leg_id == leg.leg_id), None)
            if decl is None:
                did = f"DC-{split.split_id}#P{self._plan_of(leg)}-L{leg.seq}"
                decl = CustomsDeclaration(did, split_id, leg.leg_id,
                                          leg.resource_code or "", split.teu, at)
                shipment.declarations[did] = decl
            if not decl.withdrawn:
                decl.cleared = True
                decl.cleared_at = at
                actual_window = next(
                    (r.resource_code for r in leg.reservations
                     if r.resource_kind == "customs"), None)
                if actual_window:
                    decl.customs_code = actual_window
        for appt in shipment.appointments.values():
            if (appt.split_id == split_id and not appt.cancelled and not appt.kept
                    and any(r.resource_code == appt.window_code for r in leg.reservations)):
                appt.kept = True
                appt.checked_in_at = at
        if seq == max(l.seq for l in split.active_legs):
            split.state = SplitState.DELIVERED
            split.delivered_at = at
            self._log(shipment, at, EventType.DELIVERY, split_id, leg.leg_id,
                      f"批次交付完成，签收 {split.teu} TEU", {"teu": split.teu})
        return leg

    def execute(self, shipment: Shipment, split_id: str, seqs: list[int] | None = None,
                docs: dict[int, list[str]] | None = None) -> None:
        """按计划时刻一键执行若干航段（测试/演示用）。"""
        split = shipment.splits[split_id]
        target = seqs if seqs is not None else [l.seq for l in split.active_legs]
        docs = docs or {}
        for seq in target:
            leg = self._leg(split, seq)
            if leg.state != LegState.PLANNED:
                continue
            self.depart(shipment, split_id, seq, leg.planned_depart)
            self.arrive(shipment, split_id, seq, leg.planned_arrive)
            self.handover(shipment, split_id, seq, leg.planned_arrive,
                          docs.get(seq, self._default_docs(leg)),
                          party=self._default_party(leg))

    def _default_docs(self, leg: ScheduledLeg) -> list[str]:
        if leg.mode == Mode.CUSTOMS:
            return ["报关单", "发票装箱单", "海关电子放行回执"]
        if leg.mode == Mode.SEA:
            return ["海运提单(B/L)", "舱单"]
        if leg.mode == Mode.CANAL:
            return ["内河运单", "过闸排档确认"]
        if leg.mode == Mode.RAIL:
            return ["铁路运单", "舱单"]
        if leg.mode == Mode.PORT:
            return ["装卸作业票", "理货交接单"]
        if leg.seq == 1:
            return ["装箱单", "设备交接单(EIR)"]
        return ["签收单(POD)"]

    def _default_party(self, leg: ScheduledLeg) -> str:
        return {Mode.CUSTOMS: "海关/报关行", Mode.PORT: "港口理货",
                Mode.SEA: "船公司", Mode.CANAL: "运河调度/船东",
                Mode.RAIL: "铁路承运人"}.get(leg.mode, "收货人")

    def _leg(self, split: Split, seq: int) -> ScheduledLeg:
        legs = [l for l in split.active_legs if l.seq == seq]
        if not legs:
            raise FulfillmentError(f"批次 {split.split_id} 找不到活动航段 {seq}")
        return legs[-1]
