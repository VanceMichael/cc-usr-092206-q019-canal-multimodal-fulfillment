"""结算与履约报告。

回答三件事：
1. 成本差异来自哪一段：当前版本相对原承诺(v1)，逐航段拆出基础费差异
   （报价版本、改量、改线段）与异常附加费（甩箱/堆存/退关/改线等）；
2. 各次交接是否完成：逐次货权/责任交接的计划与实际时间、撤销记录，
   以及每段单证是否齐套；
3. 更快还是更省：以旧线(西江—南沙)同期 dry-run 为基准，给时间与费用结论。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import timeutil
from .model import Shipment, Contract, PlanVersion, Step, EXECUTED, CANCELLED
from .service import FulfillmentService


@dataclass
class SegmentRow:
    seq: int
    label: str
    type: str
    route: str
    node: str
    status: str
    planned_start: str
    planned_end: str
    actual_start: str | None
    actual_end: str | None
    schedule_var_hours: float          # 实际相对计划偏差（正=延误）
    base_fee: float
    surcharge_fee: float
    fee_total: float
    surcharge_detail: list[dict]
    handoff: dict | None
    docs: list[dict]

    @property
    def docs_complete(self) -> bool:
        return all(d["submitted"] for d in self.docs)


class Settlement:
    def __init__(self, svc: FulfillmentService):
        self.svc = svc
        self.cat = svc.cat

    # ------------------------------------------------ 单分段逐航段结算
    def segment_rows(self, ship: Shipment) -> list[SegmentRow]:
        rows = []
        for s in ship.current.steps:
            sur = [{"key": f.key, "label": f.label, "amount": f.total,
                    "reason": f.reason} for f in s.fees if f.category == "SURCHARGE"]
            var = 0.0
            if s.actual_end:
                var = timeutil.hours_between(s.planned_end, s.actual_end)
            ho = None
            if s.handoff:
                ho = {"stage": s.handoff.stage, "node": s.handoff.node,
                      "from": self.cat.party(s.handoff.from_party),
                      "to": self.cat.party(s.handoff.to_party),
                      "planned_at": timeutil.fmt(s.handoff.planned_at),
                      "actual_at": timeutil.fmt(s.handoff.actual_at)
                      if s.handoff.actual_at else None,
                      "completed": s.handoff.completed}
            rows.append(SegmentRow(
                seq=s.seq, label=s.label, type=s.type, route=s.route, node=s.node,
                status=s.status,
                planned_start=timeutil.fmt(s.planned_start),
                planned_end=timeutil.fmt(s.planned_end),
                actual_start=timeutil.fmt(s.actual_start) if s.actual_start else None,
                actual_end=timeutil.fmt(s.actual_end) if s.actual_end else None,
                schedule_var_hours=var,
                base_fee=round(sum(f.total for f in s.fees if f.category == "BASE"), 2),
                surcharge_fee=round(sum(f.total for f in s.fees
                                        if f.category == "SURCHARGE"), 2),
                fee_total=s.fee_total, surcharge_detail=sur, handoff=ho,
                docs=[{"name": d.name, "required": d.required,
                       "submitted": d.submitted,
                       "submitted_at": timeutil.fmt(d.submitted_at)
                       if d.submitted_at else None} for d in s.docs]))
        return rows

    # ------------------------------------------------ 成本差异逐段归因
    def cost_attribution(self, ship: Shipment) -> dict:
        """当前承诺费用相对 v1 的差异，拆到航段与异常。"""
        v1 = ship.original_promise
        cur = ship.current

        def base_map(ver: PlanVersion):
            m = {}
            for s in ver.steps:
                for f in s.fees:
                    if f.category == "BASE":
                        m[(s.seq, f.key)] = (s, f)
            return m

        old_b, new_b = base_map(v1), base_map(cur)
        segment_delta: list[dict] = []
        keys = sorted(set(old_b) | set(new_b))
        for key in keys:
            o = old_b.get(key)
            n = new_b.get(key)
            old_amt = o[1].total if o else 0.0
            new_amt = n[1].total if n else 0.0
            delta = round(new_amt - old_amt, 2)
            if delta == 0 and o and n:
                continue
            reasons = []
            if o and not n:
                reasons.append("该航段取消/改线后不再发生")
            elif n and not o:
                reasons.append("改线新增航段" if cur.route != v1.route else "新增航段")
            else:
                if n[1].teu != o[1].teu:
                    reasons.append(f"客户改量 {o[1].teu}→{n[1].teu}TEU")
                if cur.card_version != v1.card_version:
                    reasons.append(f"报价版本 {v1.card_version}→{cur.card_version}")
                if cur.route != v1.route and o[0].ref != n[0].ref:
                    reasons.append(f"改线：{o[0].label}→{n[0].label}")
                if not reasons:
                    reasons.append("费率/箱量调整")
            segment_delta.append({
                "seq": key[0], "fee_key": key[1],
                "label": (n or o)[0].label,
                "old": old_amt, "new": new_amt, "delta": delta,
                "reasons": reasons})

        surcharge_rows = []
        for ex in ship.exceptions:
            for f in ex.surcharge_lines:
                surcharge_rows.append({"exception": ex.id, "kind": ex.kind,
                                       "reason": ex.reason, "key": f.key,
                                       "label": f.label, "amount": f.total,
                                       "detail": f.reason})
        base_delta = round(sum(d["delta"] for d in segment_delta), 2)
        sur_delta = round(sum(r["amount"] for r in surcharge_rows), 2)
        explained = round(base_delta + sur_delta, 2)
        return {
            "v1_cost": v1.committed_cost,
            "current_cost": cur.committed_cost,
            "total_delta": round(cur.committed_cost - v1.committed_cost, 2),
            "base_segment_delta": segment_delta,
            "surcharge_delta": surcharge_rows,
            "base_delta_sum": base_delta,
            "surcharge_delta_sum": sur_delta,
            "reconciled": abs(explained - round(
                cur.committed_cost - v1.committed_cost, 2)) < 0.01,
        }

    # ------------------------------------------------ 交接与撤销
    def handoff_ledger(self, ship: Shipment) -> list[dict]:
        """全过程交接台账。当前版本各交接 + 历史版本中被撤销（如退关）的交接。"""
        ledger = []
        current_keys = set()
        for s in ship.current.steps:
            if s.handoff:
                current_keys.add((s.seq, s.handoff.stage))
                h = s.handoff
                ledger.append({"seq": s.seq, "stage": h.stage,
                               "node": self.cat.node_name(h.node),
                               "from": self.cat.party(h.from_party),
                               "to": self.cat.party(h.to_party),
                               "planned_at": timeutil.fmt(h.planned_at),
                               "actual_at": timeutil.fmt(h.actual_at)
                               if h.actual_at else None,
                               "state": "COMPLETED" if h.completed else "PENDING"})
        # 历史版本里实际发生、但在当前版本消失的交接 => 被撤销
        for ver in ship.versions[:-1]:
            for s in ver.steps:
                if s.handoff and s.handoff.completed and \
                        (s.seq, s.handoff.stage) not in current_keys:
                    h = s.handoff
                    ledger.append({"seq": s.seq, "stage": h.stage,
                                   "node": self.cat.node_name(h.node),
                                   "from": self.cat.party(h.from_party),
                                   "to": self.cat.party(h.to_party),
                                   "planned_at": timeutil.fmt(h.planned_at),
                                   "actual_at": timeutil.fmt(h.actual_at),
                                   "state": "REVOKED"})
        ledger.sort(key=lambda x: (x["seq"], x["state"] != "REVOKED"))
        return ledger

    # ------------------------------------------------ 承诺时间线
    def promise_timeline(self, ship: Shipment) -> list[dict]:
        return [{"version": v.version, "route": v.route_name,
                 "rate_card": f"{v.card_version} {v.card_name}",
                 "reason": v.reason, "exception": v.exception_id,
                 "created_at": timeutil.fmt(v.created_at),
                 "committed_eta": timeutil.fmt(v.committed_eta),
                 "committed_cost": v.committed_cost,
                 "state": v.state,
                 "eta_delta_vs_v1_hours": timeutil.hours_between(
                     ship.original_promise.committed_eta, v.committed_eta)}
                for v in ship.versions]

    # ------------------------------------------------ 旧线基准结论
    def benchmark(self, ship: Shipment, contract_raw: dict) -> dict:
        """以同备货日旧线 dry-run 为基准：本票实际更快/更省多少。"""
        options = {o.route: o for o in self.svc.compare(
            contract_raw["id"], teu=ship.original_teu, ready=ship.ready)}
        old = options["OLD"]
        cur = ship.current
        return {
            "old_route_eta": timeutil.fmt(old.delivery),
            "old_route_cost": old.cost_total,
            "current_route": cur.route_name,
            "current_eta": timeutil.fmt(cur.committed_eta),
            "current_cost": cur.committed_cost,
            "faster_than_old_hours": timeutil.hours_between(
                cur.committed_eta, old.delivery),
            "cheaper_than_old": round(old.cost_total - cur.committed_cost, 2),
            "distance_km_old": old.distance_km,
            "distance_km_current": self.cat.routes[cur.route]["distance_km"],
        }

    # ------------------------------------------------ 分段完整报告
    def shipment_report(self, ship: Shipment, contract_raw: dict) -> dict:
        rows = self.segment_rows(ship)
        return {
            "shipment_id": ship.id,
            "commodity": ship.commodity,
            "teu": ship.teu,
            "original_teu": ship.original_teu,
            "status": ship.status,
            "promise_timeline": self.promise_timeline(ship),
            "segments": [r.__dict__ for r in rows],
            "segments_all_executed": all(r.status == EXECUTED for r in rows),
            "handoffs": self.handoff_ledger(ship),
            "handoffs_pending": [h for h in self.handoff_ledger(ship)
                                 if h["state"] == "PENDING"],
            "docs_missing": [{"seq": r.seq, "label": r.label,
                              "docs": [d["name"] for d in r.docs if not d["submitted"]]}
                             for r in rows if not r.docs_complete],
            "cost_attribution": self.cost_attribution(ship),
            "benchmark_vs_old_route": self.benchmark(ship, contract_raw),
        }

    def contract_report(self, contract: Contract) -> dict:
        return {
            "contract_id": contract.id,
            "customer": contract.raw["customer"],
            "consignee": contract.raw["consignee"],
            "total_teu": contract.raw["total_teu"],
            "currency": contract.raw["currency"],
            "shipments": [self.shipment_report(s, contract.raw)
                          for s in contract.shipments],
            "rollup": self._rollup(contract),
        }

    def _rollup(self, contract: Contract) -> dict:
        cost = sum(s.current.committed_cost for s in contract.shipments)
        v1_cost = sum(s.original_promise.committed_cost for s in contract.shipments)
        delivered = [s for s in contract.shipments if s.delivered_at]
        return {
            "shipment_count": len(contract.shipments),
            "delivered_count": len(delivered),
            "current_total_cost": round(cost, 2),
            "v1_total_cost": round(v1_cost, 2),
            "cost_delta": round(cost - v1_cost, 2),
            "exceptions": [{"shipment": e.shipment_id, "id": e.id, "kind": e.kind,
                            "reason": e.reason,
                            "impact_hours": e.impact_hours}
                           for s in contract.shipments for e in s.exceptions],
        }
