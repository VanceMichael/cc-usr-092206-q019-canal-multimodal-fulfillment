"""对货主和调度员的只读视图。

- 货主视图：每批货的当前承诺 ETA / 费用、相对原承诺变化、是否落在收货窗；
  分批交付时逐批给出口径，不互相串扰。
- 调度员视图：每批货的断点、占用资源、新旧路线与替代方案影子对比、台账健康度。
"""
from __future__ import annotations

from .models import Shipment


def shipper_view(shipment: Shipment) -> dict:
    c = shipment.contract
    rows = []
    for s in shipment.splits.values():
        first, latest = s.snapshots[0], s.latest_snapshot()
        rows.append({
            "split_id": s.split_id,
            "teu": s.teu,
            "state": {"planned": "已排程", "in_transit": "在途",
                      "delivered": "已签收", "cancelled": "已退关",
                      "rolled": "甩箱待重排"}[s.state.value],
            "first_eta": first.eta,
            "current_eta": s.eta,
            "eta_change_hours": round((s.eta - first.eta).total_seconds() / 3600, 1)
            if s.eta else None,
            "first_cost": first.cost_total,
            "current_cost": s.planned_cost,
            "cost_change": round(s.planned_cost - first.cost_total, 2),
            "delivered_at": s.delivered_at,
            "in_receive_window": (s.delivered_at is not None and
                                  c.receive_window_start <= s.delivered_at <= c.receive_window_end)
            if s.delivered_at else (c.receive_window_start <= s.eta <= c.receive_window_end)
            if s.eta else None,
            "commitment_versions": len(s.snapshots),
            "latest_reason": latest.reason if latest else None,
        })
    extras = round(sum(x["amount"] for x in shipment.extra_charges), 2)
    return {
        "contract_no": c.contract_no,
        "cargo": c.cargo,
        "consignee": c.consignee,
        "incoterm": c.incoterm,
        "receive_window": (c.receive_window_start, c.receive_window_end),
        "total_teu": c.total_teu,
        "rows": rows,
        "committed_freight": round(sum(r["current_cost"] for r in rows), 2),
        "extra_charges": extras,
        "payable_if_settled_now": round(
            sum(r["current_cost"] for r in rows
                if r["state"] == "已签收") + extras, 2),
    }


def dispatcher_view(shipment: Shipment, service) -> dict:
    c = shipment.contract
    splits = []
    for s in shipment.splits.values():
        active = s.active_legs
        next_leg = next((l for l in active if not l.locked), None)
        splits.append({
            "split_id": s.split_id,
            "teu": s.teu,
            "route": s.route_code,
            "state": s.state.value,
            "executed_seqs": [l.seq for l in active if l.executed],
            "next_open_seq": next_leg.seq if next_leg else None,
            "next_open_node": (f"{next_leg.origin}→{next_leg.dest} · {next_leg.line_name}"
                               if next_leg else None),
            "next_open_at": next_leg.planned_depart if next_leg else None,
            "eta": s.eta,
            "holds": [
                {"resource": r.resource_code, "teu": r.teu}
                for l in active for r in l.reservations if r.active
            ],
        })
    return {
        "contract_no": c.contract_no,
        "ledger_audit": service.ledger.audit(),
        "splits": splits,
        "baselines": shipment.baselines,
        "events": [{"id": e.event_id, "at": e.at, "type": e.type.value,
                    "split": e.split_id, "message": e.message}
                   for e in shipment.events],
    }
