"""最终结算与对账。

回答三个问题：
1. 相对 *最初承诺*，每一段的费用和时间发生了什么变化、为什么；
2. 相对 *旧路线反事实*（订舱时记录的基线），新路线整体快了多少、省了多少；
3. 每一次交接（船东/港口/海关/铁路/收货人）是否完成、单证是否齐备、是否按点。
"""
from __future__ import annotations

from datetime import datetime

from .models import LegState, Shipment, Split, SplitState
from .service import FulfillmentError


def _hours(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return round((a - b).total_seconds() / 3600, 1)


def settle(shipment: Shipment, at: datetime,
           late_penalty_per_teu_day: float = 0.0) -> dict:
    open_splits = [s for s in shipment.splits.values()
                   if s.state not in (SplitState.DELIVERED, SplitState.CANCELLED)]
    if open_splits:
        raise FulfillmentError(
            f"尚有批次未终结: {[s.split_id for s in open_splits]}，不能结算")

    split_reports = []
    billed_total = 0.0
    for split in shipment.splits.values():
        rep = _settle_split(shipment, split)
        split_reports.append(rep)
        billed_total += rep["billed_legs"]

    extras = round(sum(c["amount"] for c in shipment.extra_charges), 2)
    billed_total = round(billed_total + extras, 2)

    # 迟交违约金（按合同收货窗）
    c = shipment.contract
    late_teu_days = 0.0
    for s in shipment.splits.values():
        if s.state == SplitState.DELIVERED and s.delivered_at \
                and s.delivered_at > c.receive_window_end:
            days = max(1.0, (s.delivered_at.date() - c.receive_window_end.date()).days)
            late_teu_days += s.teu * days
    late_penalty = round(late_teu_days * late_penalty_per_teu_day, 2)
    billed_total = round(billed_total + late_penalty, 2)

    # 最初承诺（全部批次第一版快照之和）
    first_eta = max(s.snapshots[0].eta for s in shipment.splits.values())
    first_cost = round(sum(s.snapshots[0].cost_total for s in shipment.splits.values()), 2)
    final_deliveries = [s.delivered_at for s in shipment.splits.values()
                        if s.state == SplitState.DELIVERED]
    final_eta = max(final_deliveries) if final_deliveries else None

    # 旧路线反事实：按各批次 *最终箱量* 与订舱当时的旧线影子方案对照
    # （班期/船闸时刻由资源决定，箱量差异在同航次内不改航期；费用按比例折算）
    old_rows = []
    for s in shipment.splits.values():
        cf = s.counterfactuals.get("R-OLD")
        if not cf or not cf.get("eta"):
            continue
        # 反事实按订舱时箱量记录；班期时刻由资源决定，费用用单价折算最终箱量
        rate = cf["cost_per_teu"]
        old_rows.append({
            "split_id": s.split_id, "teu": s.teu,
            "old_eta": cf["eta"], "old_cost_at_final_teu": round(rate * s.teu, 2),
            "actual_eta": s.delivered_at,
            "actual_billed_legs": round(
                sum(l.cost for l in s.legs if l.state == LegState.DONE), 2),
        })
    old_eta = max(r["old_eta"] for r in old_rows) if old_rows else None
    old_cost = round(sum(r["old_cost_at_final_teu"] for r in old_rows), 2) if old_rows else None
    legs_billed_all = round(sum(
        l.cost for s in shipment.splits.values()
        for l in s.legs if l.state == LegState.DONE), 2)

    report = {
        "contract_no": c.contract_no,
        "settled_at": at,
        "cargo": c.cargo,
        "total_teu": c.total_teu,
        "delivered_teu": round(sum(s.teu for s in shipment.splits.values()
                                   if s.state == SplitState.DELIVERED), 2),
        "cancelled_teu": shipment.cancelled_teu,
        "first_commitment": {"eta": first_eta, "cost": first_cost},
        "actual": {"eta": final_eta,
                   "lead_time_hours_vs_first": _hours(final_eta, first_eta)},
        "billed_total": billed_total,
        "billed_legs_only": legs_billed_all,
        "vs_first_commitment": round(billed_total - first_cost, 2),
        "vs_old_route": {
            "name": shipment.baselines.get("R-OLD", {}).get("name"),
            "baseline_eta": old_eta,
            "baseline_cost": old_cost,
            "saved_cost": (round(old_cost - billed_total, 2)
                           if old_cost is not None else None),
            "saved_hours": _hours(old_eta, final_eta),
            "per_split": old_rows,
            "note": "旧线为订舱当时无异常的影子排程；实际新线发生了甩箱/封港等事件",
        },
        "extra_charges": shipment.extra_charges,
        "late_penalty": late_penalty,
        "splits": split_reports,
        "handovers": handover_checklist(shipment),
        "documents": document_status(shipment),
    }
    shipment.settled = True
    return report


def _settle_split(shipment: Shipment, split: Split) -> dict:
    # 最初承诺按航段序号留存，后续即使换了计划/路线也能逐段对比
    first_by_seq = {v["seq"]: (v["label"], v["cost"])
                    for v in split.snapshots[0].per_leg.values()}

    leg_rows = []
    billed = 0.0
    for leg in sorted(split.legs, key=lambda l: (l.seq, l.leg_id)):
        executed = leg.state == LegState.DONE
        billed_cost = leg.cost if executed else 0.0
        billed += billed_cost
        base = first_by_seq.get(leg.seq)
        base_cost = base[1] if base else None
        leg_rows.append({
            "seq": leg.seq, "plan": leg.leg_id.split("#P")[1].split("-")[0],
            "label": leg.line_name, "state": leg.state.value,
            "planned_depart": leg.planned_depart,
            "actual_depart": leg.actual_depart,
            "planned_arrive": leg.planned_arrive,
            "actual_arrive": leg.actual_arrive,
            "depart_delay_hours": _hours(leg.actual_depart, leg.planned_depart),
            "arrive_delay_hours": _hours(leg.actual_arrive, leg.planned_arrive),
            "first_quote_cost": base_cost,
            "final_leg_cost": leg.cost,
            "billed_cost": billed_cost,
            "cost_delta_vs_first": (round(leg.cost - base_cost, 2)
                                    if base_cost is not None and executed else None),
            "handover_done": leg.handover_done,
            "handover_at": leg.handover_at,
            "handover_party": leg.handover_party,
            "docs": leg.handover_docs,
            "note": leg.note,
        })

    # 承诺演进（原承诺可追溯）
    trace = [{"snap_no": s.snap_no, "at": s.at, "reason": s.reason,
              "eta": s.eta, "cost_total": s.cost_total,
              "superseded": s.superseded, "event_ref": s.event_ref}
             for s in split.snapshots]

    return {
        "split_id": split.split_id,
        "teu": split.teu,
        "state": split.state.value,
        "route": split.route_code,
        "quote_id": split.quote_id,
        "first_eta": split.snapshots[0].eta,
        "first_cost": split.snapshots[0].cost_total,
        "delivered_at": split.delivered_at,
        "billed_legs": round(billed, 2),
        "leg_rows": leg_rows,
        "commitment_trace": trace,
    }


def handover_checklist(shipment: Shipment) -> list[dict]:
    """逐次交接：谁在什么时候把什么（含哪些单证）交给了谁，是否闭环。"""
    rows = []
    for split in shipment.splits.values():
        for leg in sorted(split.legs, key=lambda l: (l.seq, l.leg_id)):
            if leg.state == LegState.SKIPPED:
                continue
            rows.append({
                "split_id": split.split_id,
                "seq": leg.seq,
                "node": f"{leg.origin}→{leg.dest}",
                "line": leg.line_name,
                "departed": leg.actual_depart is not None,
                "arrived": leg.actual_arrive is not None,
                "handover_completed": leg.handover_done,
                "handover_at": leg.handover_at,
                "counterparty": leg.handover_party,
                "documents": leg.handover_docs,
                "on_time": (leg.actual_arrive is not None
                            and leg.actual_arrive <= leg.planned_arrive),
            })
    return rows


def document_status(shipment: Shipment) -> dict:
    return {
        "customs": [
            {"decl_id": d.decl_id, "split_id": d.split_id, "teu": d.teu,
             "customs_code": d.customs_code,
             "declared_at": d.declared_at, "withdrawn": d.withdrawn,
             "withdrawn_at": d.withdrawn_at, "abandoned": d.abandoned,
             "cleared": d.cleared,
             "cleared_at": d.cleared_at,
             "status": ("已退关" if d.withdrawn else
                        "已作废" if d.abandoned else
                        "已放行" if d.cleared else "申报中")}
            for d in shipment.declarations.values()
        ],
        "appointments": [
            {"appt_id": a.appt_id, "split_id": a.split_id, "window": a.window_code,
             "slot": (a.slot_start, a.slot_end), "teu": a.teu,
             "checked_in_at": a.checked_in_at,
             "status": ("已取消释放" if a.cancelled else
                        "按时到货" if a.kept else "未使用")}
            for a in shipment.appointments.values()
        ],
    }
