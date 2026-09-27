"""履约编排服务。

职责：
- 按合同建立分段计划（v1 承诺），原子占用箱位/船闸/港口/班列/报关/空箱/预约；
- 执行航段：登记实际时间、单证提交与交接，已执行航段的资源占用即时核销；
- 异常处理（延误/甩箱/改港改线/退关/改量）：冻结已执行前缀，只重建未执行尾部，
  旧计划版本整版保留（原承诺可追溯），旧尾部资源经原子 swap 释放后改占新资源；
- 对货主输出唯一一致的当前到达与费用，以及相对原承诺的差异和原因。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import timeutil
from .calendar import ResourceCalendar, CapacityError
from .catalog import Catalog
from .model import (Shipment, Contract, PlanVersion, Step, FeeLine, DocState,
                    HandOff, ExceptionEvent,
                    PLANNED, EXECUTED, CANCELLED,
                    ST_PLANNED, ST_RUNNING, ST_DONE, ST_CANCELLED)
from .planner import Planner


class FulfillmentError(Exception):
    pass


@dataclass
class Option:
    route: str
    route_name: str
    card_version: str
    card_name: str
    ready: datetime
    arrival: datetime
    delivery: datetime
    cost_per_teu: float
    cost_total: float
    distance_km: int
    transit_hours: float
    bottlenecks: str


class FulfillmentService:
    def __init__(self, cat: Catalog | None = None, cal: ResourceCalendar | None = None):
        self.cat = cat or Catalog()
        self.cal = cal or ResourceCalendar()
        self.planner = Planner(self.cat, self.cal)
        self.contracts: dict[str, Contract] = {}
        self.shipments: dict[str, Shipment] = {}
        self._ex_seq = 0

    # ============================================================ 建单
    def book_contract(self, contract_id: str) -> Contract:
        raw = next(c for c in self._contracts_raw() if c["id"] == contract_id)
        contract = Contract(raw=raw)
        route_id, card_ver, quote, card = self.cat.card_for_quotation(
            raw["quotation_ref"])
        if raw["route_locked"] != route_id:
            raise FulfillmentError("报价路线与合同锁定路线不一致")
        for s in raw["shipments"]:
            ready = timeutil.parse(s["cargo_ready"])
            shipment = Shipment(
                id=s["id"], contract_id=contract_id, teu=s["teu"],
                original_teu=s["teu"], ready=ready,
                appointment_set=raw["appointment_set"], commodity=raw["commodity"])
            built = self.planner.build(
                route=route_id, card_version=card_ver, teu=s["teu"], ready=ready,
                appointment_set=raw["appointment_set"], shipment_id=s["id"])
            v1 = PlanVersion(
                version=1, route=route_id,
                route_name=self.cat.routes[route_id]["name"],
                card_version=card_ver, card_name=card["name"],
                created_at=ready, reason="初次排程（合同承诺）", exception_id=None,
                steps=built.steps, committed_eta=built.delivery,
                committed_cost=built.base_cost)
            shipment.versions.append(v1)
            self.shipments[s["id"]] = shipment
            contract.shipments.append(shipment)
        self.contracts[contract_id] = contract
        return contract

    def _contracts_raw(self) -> list[dict]:
        from .catalog import contracts as load_contracts
        return load_contracts()["contracts"]

    # ============================================================ 方案比选（不落占用）
    def compare(self, contract_id: str, teu: int | None = None,
                ready: datetime | None = None) -> list[Option]:
        raw = self.contracts[contract_id].raw if contract_id in self.contracts \
            else next(c for c in self._contracts_raw() if c["id"] == contract_id)
        teu = teu or raw["shipments"][0]["teu"]
        ready = ready or timeutil.parse(raw["shipments"][0]["cargo_ready"])
        options = []
        for route_id, card_ver in [("OLD", "v3"), ("CANAL", "v2"), ("RAIL_SEA", "v1")]:
            try:
                built = self.planner.build(
                    route=route_id, card_version=card_ver, teu=teu, ready=ready,
                    appointment_set=raw["appointment_set"],
                    shipment_id=f"DRY-{route_id}", dry_run=True)
            except CapacityError as exc:
                options.append(Option(route_id, self.cat.routes[route_id]["name"],
                                      card_ver, "", ready, ready, ready,
                                      0, 0, self.cat.routes[route_id]["distance_km"],
                                      0, " / ".join(exc.conflicts)))
                continue
            card = self.cat.cards[(route_id, card_ver)]
            waits = []
            for s in built.steps:
                slack = timeutil.hours_between(
                    built.steps[s.seq - 2].planned_end if s.seq > 1 else ready,
                    s.planned_start) if s.seq > 1 else 0
                if slack >= 1 and s.type in ("VOYAGE", "APPOINTMENT", "CUSTOMS"):
                    waits.append(f"{s.label}等{slack:g}h")
            options.append(Option(
                route=route_id, route_name=self.cat.routes[route_id]["name"],
                card_version=card_ver, card_name=card["name"], ready=ready,
                arrival=built.arrival, delivery=built.delivery,
                cost_per_teu=round(built.base_cost / teu, 2),
                cost_total=built.base_cost,
                distance_km=self.cat.routes[route_id]["distance_km"],
                transit_hours=timeutil.hours_between(ready, built.delivery),
                bottlenecks="；".join(waits) or "无明显等待"))
        return options

    # ============================================================ 执行
    def submit_docs(self, shipment_id: str, seq: int, names: list[str],
                    when: datetime) -> None:
        step = self._step(shipment_id, seq)
        have = {d.name for d in step.docs}
        for name in names:
            if name not in have:
                raise FulfillmentError(f"{step.label} 无需单证 {name}")
        for d in step.docs:
            if d.name in names and not d.submitted:
                d.submitted = True
                d.submitted_at = when

    def execute(self, shipment_id: str, seq: int, when: datetime,
                actual_end: datetime | None = None) -> Step:
        ship = self.shipments[shipment_id]
        step = self._step(shipment_id, seq)
        if step.status != PLANNED:
            raise FulfillmentError(f"{ship.id}-{seq} 状态 {step.status}，不能执行")
        pending_missing = [d.name for d in step.docs
                           if d.required and not d.submitted]
        if pending_missing:
            raise FulfillmentError(
                f"{step.label} 单证未齐: {pending_missing}")
        end = actual_end or when
        step.status = EXECUTED
        step.actual_start = when
        step.actual_end = end
        if step.handoff:
            # 装船类交接发生在步骤开始，进场/交付类发生在完成时刻，与 planned_at 口径一致
            step.handoff.actual_at = (when if step.handoff.planned_at == step.planned_start
                                      else end)
        # 资源已实际消耗：核销占用（容量仍计，名额确实被用掉），编号留痕不可再释放
        if step.bookings:
            self.cal.consume(step.bookings)
            step.consumed_bookings = list(step.bookings)
            step.bookings = []
        if ship.status == ST_PLANNED:
            ship.status = ST_RUNNING
        if seq == ship.current.steps[-1].seq:
            ship.status = ST_DONE
            ship.delivered_at = end
        return step

    # ============================================================ 异常
    def delay_before_voyage(self, shipment_id: str, at: datetime,
                            delay_hours: float, reason: str) -> PlanVersion:
        """航段开启前延误（驳船晚开、港口压港、报关顺延）：重排未执行尾部。"""
        return self._replan(
            self.shipments[shipment_id], kind="DELAY", reason=reason, at=at,
            cur=at + timedelta(hours=delay_hours), surcharges=[])

    def delay_in_canal(self, shipment_id: str, voyage_seq: int,
                       delay_hours: float, at: datetime, reason: str) -> PlanVersion:
        """运河航行中延误：原子改订未过船闸窗口，抵港顺延后重排港后尾部。

        在途航段本身冻结（班次箱位与已过船闸不动），只重建到港之后的航段。"""
        ship = self.shipments[shipment_id]
        ver = ship.current
        step = next(s for s in ver.steps if s.seq == voyage_seq)
        if step.status != PLANNED:
            raise FulfillmentError("航段已结束，应按到港实际时间登记延误")
        raw_voy = next(s for s in self.cat.routes[ver.route]["steps"]
                       if s.get("ref") == step.ref and "checkpoints" in s)
        cps = raw_voy["checkpoints"]
        first_pending = self._first_pending_lock(cps, step.planned_start, at)

        # 改订会原地变更在途航段。先留存改订前快照，稍后放入被取代的 v1，
        # 使原承诺在步骤级也不可变；新计划则冻结改订后的实时航段。
        snap = copy.deepcopy(step)

        outcome = self.planner.rebook_locks(
            step=step, route_id=ver.route, shipment_id=ship.id,
            first_cp_index=first_pending, delay_hours=delay_hours)
        # 已通过船闸的窗口实际消耗，核销（箱位与新窗口保留为未执行占用）
        passed_bids = []
        bi = 1
        for idx, cp in enumerate(cps):
            if "lock" in cp:
                if idx < first_pending:
                    passed_bids.append(step.bookings[bi])
                bi += 1
        if passed_bids:
            self.cal.consume(passed_bids)
            step.consumed_bookings.extend(passed_bids)
            step.bookings = [b for b in step.bookings if b not in set(passed_bids)]
        new_ver = self._replan(
            ship, kind="DELAY", reason=reason, at=at,
            cur=outcome["new_arrival"], freeze_through_seq=voyage_seq,
            surcharges=[])
        # 被取代的 v1 保留改订前的航段快照（占用编号已不属于现行日历，清空以免误用）
        snap.bookings = []
        old_idx = next(i for i, st in enumerate(ver.steps) if st.seq == voyage_seq)
        ver.steps[old_idx] = snap
        return new_ver

    @staticmethod
    def _first_pending_lock(cps, departure: datetime, at: datetime) -> int:
        for idx, cp in enumerate(cps):
            if "lock" in cp:
                nominal = departure + timedelta(hours=cp["after_hours"])
                if nominal >= at:
                    return idx
        return len(cps)

    def roll_at_port(self, shipment_id: str, at: datetime, reason: str,
                     hours_late: float = 0, storage_days: int = 0) -> PlanVersion:
        """甩箱：原定班轮未装上，改配后续班次，收改配费；超免堆期另收堆存费。"""
        ship = self.shipments[shipment_id]
        surcharges = [self._surcharge_line("ROLL_FEE", ship.teu, 1, "甩箱改配")]
        if storage_days:
            surcharges.append(self._surcharge_line(
                "PORT_STORAGE", ship.teu, storage_days,
                f"甩箱堆存{storage_days}天(免堆4天)"))
        return self._replan(
            ship, kind="ROLL", reason=reason, at=at,
            cur=at + timedelta(hours=hours_late), surcharges=surcharges)

    def withdraw_and_redeclare(self, shipment_id: str, at: datetime,
                               reason: str, redeclare_ready: datetime) -> PlanVersion:
        """退关：已报关后撤单，从最近一次报关起重走，收重报费与单证重制费。"""
        ship = self.shipments[shipment_id]
        executed_customs = [s for s in ship.current.steps
                            if s.type == "CUSTOMS" and s.status == EXECUTED]
        if not executed_customs:
            raise FulfillmentError("尚未报关，不存在退关")
        last_customs = executed_customs[-1]
        surcharges = [
            self._surcharge_line("CUSTOMS_REDECLARE", ship.teu, 1, "退关重报"),
            self._surcharge_line("DOC_REISSUE", ship.teu, 1, "单证重制")]
        return self._replan(
            ship, kind="WITHDRAW", reason=reason, at=at,
            cur=redeclare_ready, redo_from_seq=last_customs.seq,
            surcharges=surcharges)

    def reroute(self, shipment_id: str, new_route: str, new_card: str,
                at: datetime, reason: str) -> PlanVersion:
        """改港/改线：已执行前缀冻结，未执行部分改走替代路线与报价。"""
        ship = self.shipments[shipment_id]
        surcharges = [self._surcharge_line("REROUTE_ADMIN", ship.teu, 1, "改港改线")]
        return self._replan(
            ship, kind="REROUTE", reason=reason, at=at, cur=at,
            route_override=(new_route, new_card), surcharges=surcharges)

    def change_quantity(self, shipment_id: str, new_teu: int, at: datetime,
                        reason: str) -> PlanVersion:
        """客户改量：只影响未执行部分，已执行航段费用按实际箱量保留。"""
        if new_teu <= 0:
            raise FulfillmentError("改量后箱量必须为正；整票取消请用 cancel_shipment")
        return self._replan(
            self.shipments[shipment_id], kind="QUANTITY", reason=reason, at=at,
            cur=at, teu_override=new_teu, surcharges=[])

    def cancel_shipment(self, shipment_id: str, at: datetime, reason: str) -> None:
        """整票未执行部分退关取消：释放全部未执行占用（已核销部分不动）。"""
        ship = self.shipments[shipment_id]
        pending = [s for s in ship.current.steps if s.status == PLANNED]
        bids = [b for s in pending for b in s.bookings]
        if bids:
            self.cal.release(bids)
        for s in pending:
            s.status = CANCELLED
            s.bookings = []
        ship.status = ST_CANCELLED
        self._new_exception(ship, "WITHDRAW", reason, at,
                            ship.current.version, ship.current.version,
                            affected_teu=ship.teu)

    # ============================================================ 重排内核
    def _replan(self, ship: Shipment, *, kind: str, reason: str, at: datetime,
                cur: datetime, surcharges: list[FeeLine],
                redo_from_seq: int | None = None,
                freeze_through_seq: int | None = None,
                route_override: tuple[str, str] | None = None,
                teu_override: int | None = None,
                impact: float = 0) -> PlanVersion:
        """冻结已执行（及在途冻结）前缀，只重建未执行尾部。

        - redo_from_seq: 退关重报——该序号的已执行报关步也要作废重建；
        - freeze_through_seq: 在途延误——该序号航段尚未执行完但冻结保留
          （其船闸窗口已由 rebook_locks 原子改订），重建从其后一步开始。
        旧版本整版保留；旧尾部未执行占用经原子 swap 换成新占用。
        """
        old = ship.current
        new_route, new_card = route_override or (old.route, old.card_version)
        new_teu = teu_override or ship.teu

        executed = [s for s in old.steps if s.status == EXECUTED]
        executed_seqs = {s.seq for s in executed}
        redo_step = next((s for s in old.steps
                          if redo_from_seq is not None and s.seq == redo_from_seq), None)
        frozen_seqs = set(executed_seqs)
        if freeze_through_seq is not None:
            frozen_seqs.add(freeze_through_seq)

        def is_pending(s: Step) -> bool:
            if s.status != PLANNED:
                return False
            if redo_step is not None and s.seq == redo_step.seq:
                return True       # 退关：旧报关步重建
            return s.seq not in frozen_seqs

        pending = [s for s in old.steps if is_pending(s)]
        if not pending:
            raise FulfillmentError("没有需要重排的未执行航段")

        # 重建起点（新路线 raw 步下标与绝对序号）
        anchor = pending[0]
        if redo_step is not None:
            raw_from = redo_step.raw_index
            seq_from = redo_step.seq
        elif new_route == old.route:
            raw_from = anchor.raw_index
            seq_from = anchor.seq
        else:
            raw_from = self._map_raw_index(old, anchor, new_route)
            seq_from = anchor.seq

        # 旧尾部未执行占用（退关时旧报关占用已核销，不可释放；其余未执行占用释放）
        release_bids: list[str] = []
        for s in pending:
            release_bids.extend(s.bookings)
        # 在途冻结航段的占用不能释放（箱位+改订后船闸窗口仍有效）
        if freeze_through_seq is not None:
            frozen_step = next(s for s in old.steps if s.seq == freeze_through_seq)
            release_bids = [b for b in release_bids
                            if b not in set(frozen_step.bookings)]

        built = self.planner.build(
            route=new_route, card_version=new_card, teu=new_teu, ready=cur,
            cur=cur, raw_from=raw_from, seq_from=seq_from,
            appointment_set=ship.appointment_set, shipment_id=ship.id,
            surcharges=surcharges, release_booking_ids=release_bids)

        # 冻结前缀：深拷贝（已核销占用不再出现在 bookings 中），保持原序号
        rebuilt_seqs = {s.seq for s in built.steps}
        frozen: list[Step] = []
        for s in old.steps:
            if s.seq in rebuilt_seqs or s.seq not in (
                    frozen_seqs | executed_seqs):
                continue
            cp = copy.deepcopy(s)
            frozen.append(cp)
        # 退关时被重做的旧报关步不进新版本
        new_steps = sorted(frozen + built.steps, key=lambda s: s.seq)

        total_cost = round(sum(f.total for s in new_steps for f in s.fees), 2)
        new_version_no = old.version + 1
        ver = PlanVersion(
            version=new_version_no, route=new_route,
            route_name=self.cat.routes[new_route]["name"],
            card_version=new_card,
            card_name=self.cat.cards[(new_route, new_card)]["name"],
            created_at=at, reason=reason, exception_id=None,
            steps=new_steps, committed_eta=built.delivery,
            committed_cost=total_cost, parent_version=old.version)

        ex = self._new_exception(
            ship, kind, reason, at, old.version, new_version_no,
            impact_hours=impact or timeutil.hours_between(
                old.committed_eta, built.delivery),
            affected_teu=new_teu, surcharge_lines=surcharges)
        ver.exception_id = ex.id
        old.state = "SUPERSEDED"
        ship.versions.append(ver)
        ship.teu = new_teu
        return ver


    def _map_raw_index(self, old_ver: PlanVersion, first_pending: Step,
                       new_route: str) -> int:
        """跨路线改线：按节点+作业类型把尾部起点映射到新路线步骤。"""
        target_node = first_pending.node
        target_type = first_pending.type
        if first_pending.type == "VOYAGE":
            target_node = self._voyage_from_node(old_ver.route, first_pending.ref)
        raw = self.cat.routes[new_route]["steps"]
        for i, r in enumerate(raw):
            if r["t"] != target_type:
                continue
            node = r.get("node") or r.get("node_from")
            if node == target_node:
                return i
        raise FulfillmentError(
            f"无法在{new_route}上衔接 {target_type}@{target_node}，请选可衔接路线")

    def _voyage_from_node(self, route: str, ref: str) -> str:
        step = next(s for s in self.cat.routes[route]["steps"] if s.get("ref") == ref)
        return step["node_from"]

    # ============================================================ 附加费
    def _surcharge_line(self, key: str, teu: int,
                        qty: float, reason: str) -> FeeLine:
        rule = self.cat.surcharge(key)
        note = reason
        if rule.get("unit") == "TEU_DAY":
            billable_days = max(0, qty - rule.get("free_days", 0))
            note = f"{reason}；免{rule['free_days']}天, 计费{billable_days:g}天"
            qty = billable_days
        return FeeLine(key=key, label=rule["label"], amount=rule["amount"], teu=teu,
                       qty=qty, category="SURCHARGE", reason=note)

    def _new_exception(self, ship: Shipment, kind: str, reason: str, at: datetime,
                       v_from: int, v_to: int, *, impact_hours: float = 0,
                       affected_teu: int = 0,
                       surcharge_lines: list[FeeLine] | None = None) -> ExceptionEvent:
        self._ex_seq += 1
        ex = ExceptionEvent(
            id=f"EX-{self._ex_seq:03d}", shipment_id=ship.id, kind=kind,
            reason=reason, at=at, from_version=v_from, to_version=v_to,
            impact_hours=round(max(0, impact_hours), 1),
            affected_teu=affected_teu,
            surcharge_lines=surcharge_lines or [])
        ship.exceptions.append(ex)
        return ex

    # ============================================================ 查询
    def _step(self, shipment_id: str, seq: int) -> Step:
        ship = self.shipments[shipment_id]
        return next(s for s in ship.current.steps if s.seq == seq)

    def shipper_view(self, shipment_id: str) -> dict:
        """货主一致视图：原承诺 vs 当前承诺，差异全部归因到异常。"""
        ship = self.shipments[shipment_id]
        v1, cur = ship.original_promise, ship.current
        return {
            "shipment_id": ship.id,
            "contract_id": ship.contract_id,
            "teu": ship.teu,
            "original_teu": ship.original_teu,
            "status": ship.status,
            "original_promise": {
                "version": 1, "route": v1.route_name,
                "eta": v1.committed_eta, "cost": v1.committed_cost,
                "cost_per_teu": round(v1.committed_cost / ship.original_teu, 2)},
            "current": {
                "version": cur.version, "route": cur.route_name,
                "rate_card": f"{cur.card_version} {cur.card_name}",
                "eta": cur.committed_eta, "cost": cur.committed_cost,
                "cost_per_teu": round(cur.committed_cost / ship.teu, 2)},
            "eta_delta_hours": timeutil.hours_between(
                v1.committed_eta, cur.committed_eta),
            "cost_delta": round(cur.committed_cost - v1.committed_cost, 2),
            "reasons": [{"id": e.id, "kind": e.kind, "reason": e.reason,
                         "at": e.at, "v": f"v{e.from_version}→v{e.to_version}",
                         "impact_hours": e.impact_hours,
                         "surcharge": round(sum(l.total for l in e.surcharge_lines), 2)}
                        for e in ship.exceptions],
        }
