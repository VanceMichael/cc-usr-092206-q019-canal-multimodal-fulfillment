"""端到端演示：用真实履约回答"平陆运河新路线是否更快、更省"。

运行：python -m src.demo
情节：
  1) 签约前调度员比选 旧线 / 运河 / 海铁（真实班期、船闸、截关、预约）
  2) 建立六堡茶(分批3+3)与汽车散件(24TEU)合同并做出 v1 承诺
  3) 六堡茶第一批：运河在途水位管控 9h，船闸原子改订后错过支线班期，
     交付顺延 4 天但不增加费用——航程缩短的优势被一个接驳环节抵消
  4) 六堡茶第二批：客户先改量 3→4，到钦州后支线爆舱甩箱，
     产生改配费与超免堆堆存费，逐段归因
  5) 汽车散件：到钦州后遇海防港拥堵，改走海铁联运经友谊关进越
  6) 另以独立小例演示退关重报（原报关交接撤销、重报费可追溯）
  7) 货主一致视图、逐航段结算、交接台账、单证齐套、对照旧线的快/省结论
"""
from __future__ import annotations

import json
from pathlib import Path

from . import timeutil as T
from . import report as R
from .catalog import Catalog
from .calendar import ResourceCalendar
from .model import to_dict
from .service import FulfillmentService
from .settlement import Settlement

OUT = Path(__file__).resolve().parent.parent / "out"


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def execute_to(svc, sid, seqs):
    """按计划时间执行若干航段，报关航段自动随附应交单证。

    实际开始取该航段计划开始（装船/发运时刻），实际完成取计划完成，
    使货权交接时刻（装船在发运、交付在完成）与计划口径一致。"""
    for seq in seqs:
        st = svc._step(sid, seq)
        if st.docs:
            svc.submit_docs(sid, seq, [d.name for d in st.docs], st.planned_start)
        svc.execute(sid, seq, st.planned_start, actual_end=st.planned_end)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    svc = FulfillmentService()
    stl = Settlement(svc)

    # ------------------------------------------------ 1) 签约前比选
    hr("1. 签约前比选：六堡茶 3TEU，2026-10-08 08:00 梧州备货")
    options = svc.compare(
        "CT-TEA-2610", teu=3, ready=T.parse("2026-10-08T08:00"))
    print(R.option_table(options))
    canal, old = next(o for o in options if o.route == "CANAL"), \
        next(o for o in options if o.route == "OLD")
    lead = T.hours_between(canal.delivery, old.delivery)
    print(f"\n结论（仅就本备货日）：运河全程 {canal.transit_hours:.0f}h，"
          f"交付较旧线提前 {lead:.0f}h，每TEU省 {old.cost_per_teu - canal.cost_per_teu:.0f}元；"
          f"但优势取决于能否赶上钦州支线班期，经理不能只按750km理论航程承诺。")

    # ------------------------------------------------ 2) 建合同、出承诺
    hr("2. 建立合同并锁定 v1 承诺（报价版本一并固化）")
    tea = svc.book_contract("CT-TEA-2610")
    auto = svc.book_contract("CT-AUTO-2610")
    for c in (tea, auto):
        for s in c.shipments:
            v = s.current
            win = next(x for x in c.raw["shipments"] if x["id"] == s.id)["latest_delivery"]
            print(f"{s.id} {s.teu}TEU  {v.route_name}  报价{v.card_version} "
                  f"承诺交付 {T.fmt(v.committed_eta)}（窗口≤{win[5:16]}）  "
                  f"{v.committed_cost:.0f}元")

    # ------------------------------------------------ 3) 第一批：在途延误
    s1 = "CT-TEA-2610-S1"
    hr("3. 第一批：梧州出运后，企石枢纽水位管控单向通航（在途延误9h）")
    execute_to(svc, s1, [1, 2])
    before_eta = svc.shipments[s1].original_promise.committed_eta
    v2 = svc.delay_in_canal(
        s1, voyage_seq=3, delay_hours=9,
        at=T.parse("2026-10-11T12:00"),
        reason="企石枢纽上游水位管控,单向通航")
    print(f"船闸窗口原子改订成功；到港顺延，但真正的影响是接驳班期：")
    print(f"  原承诺交付 {T.fmt(before_eta)} → 现承诺 {T.fmt(v2.committed_eta)}")
    print(R.shipper_block(svc.shipper_view(s1)))
    print("\n第一批继续执行至河内交付：")
    execute_to(svc, s1, range(3, 11))
    print(f"  状态={svc.shipments[s1].status}，实际交付 {T.fmt(svc.shipments[s1].delivered_at)}")

    # ------------------------------------------------ 4) 第二批：改量 + 甩箱
    s2 = "CT-TEA-2610-S2"
    hr("4. 第二批：客户改量 3→4TEU；到钦州后支线爆舱甩箱（堆存6天）")
    svc.change_quantity(s2, 4, T.parse("2026-10-09T10:00"),
                        "越南门店调增,第二批改为4TEU")
    execute_to(svc, s2, [1, 2, 3, 4, 5])
    v_roll = svc.roll_at_port(
        s2, at=T.parse("2026-10-20T18:00"),
        reason="钦州—海防近海支线爆舱,本批4TEU被甩箱",
        storage_days=6)
    print(f"甩箱后承诺交付 {T.fmt(v_roll.committed_eta)}，费用 {v_roll.committed_cost:.0f}元")
    execute_to(svc, s2, range(6, 11))
    print(f"  状态={svc.shipments[s2].status}，实际交付 {T.fmt(svc.shipments[s2].delivered_at)}")

    # ------------------------------------------------ 5) 汽车散件：改走海铁
    a1 = "CT-AUTO-2610-S1"
    hr("5. 汽车散件24TEU：到钦州转关后遇海防港拥堵，改海铁经友谊关进越")
    execute_to(svc, a1, [1, 2, 3, 4, 5])
    v_rail = svc.reroute(
        a1, "RAIL_SEA", "v1", at=T.parse("2026-10-09T22:00"),
        reason="海防港临时拥堵,改钦州港东—友谊关—河内班列")
    print(f"已执行的运河前段冻结不动，未执行部分改线；新承诺 {T.fmt(v_rail.committed_eta)}，"
          f"{v_rail.committed_cost:.0f}元，尾部航段：")
    for s in v_rail.steps[5:]:
        print(f"  #{s.seq} {s.type:<11}{s.node:<10}{T.fmt(s.planned_start)}→"
              f"{T.fmt(s.planned_end)} {s.label[:34]}")
    execute_to(svc, a1, range(6, len(v_rail.steps) + 1))
    print(f"  状态={svc.shipments[a1].status}，实际交付 {T.fmt(svc.shipments[a1].delivered_at)}")

    # ------------------------------------------------ 6) 退关重报（独立小例）
    hr("6. 独立小例：出口报关后植检证书批号错误，退关重报")
    svc2 = FulfillmentService(cat=svc.cat, cal=ResourceCalendar())
    svc2.book_contract("CT-TEA-2610")
    sid = "CT-TEA-2610-S1"
    svc2.execute(sid, 1, T.parse("2026-10-08T12:00"))
    svc2.submit_docs(sid, 2, [d.name for d in svc2._step(sid, 2).docs],
                     T.parse("2026-10-08T11:30"))
    svc2.execute(sid, 2, T.parse("2026-10-08T17:00"))
    svc2.withdraw_and_redeclare(
        sid, at=T.parse("2026-10-09T09:00"),
        reason="植物检疫证书批号与货物不符,海关退单",
        redeclare_ready=T.parse("2026-10-09T14:00"))
    cur2 = svc2.shipments[sid]
    redeclared = next(s for s in cur2.current.steps if s.type == "CUSTOMS" and s.seq == 2)
    print(f"原出口报关在 v1 中留痕，新版本从报关起重排：新申报计划 "
          f"{T.fmt(redeclared.planned_start)}→{T.fmt(redeclared.planned_end)}；"
          f"重报产生附加费：")
    for s in cur2.current.steps:
        for f in s.fees:
            if f.category == "SURCHARGE":
                print(f"  +{f.total:.0f}元 {f.label}（{f.reason}）")
    print("历史承诺仍可追溯：",
          [(f"v{v.version}", v.state, T.fmt(v.committed_eta))
           for v in cur2.versions])

    # ------------------------------------------------ 7) 结算报告
    hr("7. 六堡茶合同最终结算：成本差异逐段归因 + 交接台账 + 旧线基准")
    report = stl.contract_report(tea)
    for ship in tea.shipments:
        sr = next(x for x in report["shipments"] if x["shipment_id"] == ship.id)
        print(f"\n----- {ship.id}（{ship.teu}TEU，{ship.status}）-----")
        print(R.promise_timeline(sr["promise_timeline"]))
        print()
        print(R.segment_table(sr["segments"]))
        print()
        print(R.cost_attribution_block(sr["cost_attribution"]))
        print()
        print("交接台账：")
        print(R.handoff_table(sr["handoffs"]))
        if sr["docs_missing"]:
            print("未齐套单证：",
                  [(d["seq"], d["docs"]) for d in sr["docs_missing"]])
        else:
            print("单证：全程齐套 ✓")
        print(R.benchmark_block(sr["benchmark_vs_old_route"]))

    print("\n合同级汇总：", json.dumps(report["rollup"], ensure_ascii=False))

    # ------------------------------------------------ 导出 JSON
    state = {
        "as_of": "2026-09-27",
        "contracts": [
            {"contract": c.raw,
             "shipments": [to_dict(s) for s in c.shipments]}
            for c in (tea, auto)],
        "resource_calendar": svc.cal.snapshot(),
        "settlement_tea": stl.contract_report(tea),
        "settlement_auto": stl.contract_report(auto),
    }
    (OUT / "fulfillment_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n完整状态与结算已导出：{OUT / 'fulfillment_state.json'}")


if __name__ == "__main__":
    main()
