"""六堡茶一票货走平陆运河的真实履约剧本（可直接运行：python -m examples.demo）。

时间线：
- 09-27 订舱 5 TEU，分三批 2/2/1；调度员同时拿到旧线/海铁/改港线的影子基线；
- 09-28 S2 在平塘江口遇航道临时管制，延误 30 小时（只重排 S2 未执行航段）；
- 09-29 客户减量：S1 由 2 改 1，释放的 0930 快线舱位被外部客户买走；
- 09-29 晚台风将封海防港：S3（1 TEU，已放行未出境）退关，改港盖邻线；
- 10-02 S2 的 1006 航次临时减舱被甩箱，改配 1013；
- 各批依次到货签收，最后结算：费用差异逐段归因、交接逐项核对、对旧线反事实。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.network import load_network
from src.service import FulfillmentService
from src.settlement import settle
from src.reporting import shipper_view, dispatcher_view

CONTRACT = "CT-LBTEA-2026-0926"


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def main() -> None:
    nw = load_network()
    svc = FulfillmentService(nw)

    print("=" * 78)
    print("一、订舱：理论优势必须经船闸/港口/箱位/报关/收货窗口逐关校验")
    print("=" * 78)
    sh = svc.book(CONTRACT, dt("2026-09-27T10:00:00"), split_teu=[2.0, 2.0, 1.0])
    s1, s2, s3 = [f"{CONTRACT}#S{i}" for i in (1, 2, 3)]
    per_teu = {sid: sh.splits[sid].counterfactuals for sid in (s1,)}
    for code, b in sh.baselines.items():
        rate = sh.splits[s1].counterfactuals[code].get("cost_per_teu")
        if b.get("eta"):
            print(f"  {code:10s} {b['distance_km']:4d}km  ETA {b['eta']:%m-%d %H:%M}"
                  f"  约 {rate:7.0f}/TEU  {b['name']}")
        else:
            print(f"  {code:10s} 不可行：{b.get('reason') or '部分批次无资源'}")
    snap = sh.splits[s1].snapshots[0]
    print(f"\n  下达承诺（{snap.at:%m-%d %H:%M}）：{snap.eta:%m-%d %H:%M} 前送达，"
          f"运费 {snap.cost_total:.0f} 元/批")
    print("  注意：运河线 750km 比旧线 990km 短 240km，但 ETA 优势要扣掉船闸排档、")
    print("  班轮班期、泊位与收货预约——本票口径下仍比旧线快 4 天、每 TEU 省 550 元。")

    print("\n" + "=" * 78)
    print("二、S1/S3 先行：西江→三级船闸→09-29 晨抵钦州，泊位作业进行中")
    print("=" * 78)
    # S1、S3 走完前两段；第三段（钦州港作业 06:00→14:00）已开工尚未交接
    for sid in (s1, s3):
        svc.execute(sh, sid, seqs=[1, 2])
        svc.depart(sh, sid, 3, dt("2026-09-29T06:00:00"))
        print(f"  {sid.split('#')[1]}: 在钦州港泊位作业中（06:00 开工，预计 14:00 理货交接）")

    print("\n" + "=" * 78)
    print("三、09-28 S2 遇航道管制：只重排 S2 的未执行航段，S1/S3 不受影响")
    print("=" * 78)
    svc.execute(sh, s2, seqs=[1])  # 西江段已发运并到达平塘江口
    before = sh.splits[s2].eta
    svc.register_delay(
        sh, s2, dt("2026-09-28T02:00:00"), wait_hours=30,
        reason="平塘江口上游航道临时交通管制30小时",
        blacklist=["LOCK-MD-0928", "LOCK-QN-0928", "LOCK-QS-0928"])
    after = sh.splits[s2].eta
    print(f"  S2 承诺 ETA：{before:%m-%d %H:%M} → {after:%m-%d %H:%M}")
    print("  船闸改排 09-30 通过，钦州改 10-01 作业，海船顺延 1006 航次")
    print(f"  S1 承诺不变：{sh.splits[s1].eta:%m-%d %H:%M}；"
          f"S3 承诺不变：{sh.splits[s3].eta:%m-%d %H:%M}")

    print("\n" + "=" * 78)
    print("四、09-29 客户减量 S1 2→1：未执行部分重排，释放舱位立刻可被他人买走")
    print("=" * 78)
    free_before = nw.ledger.available("SLOT-SEA-QH-0930")
    svc.change_quantity(sh, s1, dt("2026-09-29T10:00:00"), 1.0,
                        reason="河内门店延后开业，首批先到1柜")
    free_after = nw.ledger.available("SLOT-SEA-QH-0930")
    print(f"  0930 快线释放后可用舱位：{free_before:.1f} → {free_after:.1f} TEU")
    rid = svc.sell_released_capacity("SLOT-SEA-QH-0930", free_after,
                                     dt("2026-09-29T11:00:00"), buyer="某钛矿货主")
    print(f"  释放舱位即被外部客户买走（占用 {rid}），本票不能再重复使用")
    # 已开工的港口作业仍按原 2 TEU 完成并计费（沉没），之后只按 1 TEU 走后续航段
    svc.arrive(sh, s1, 3, dt("2026-09-29T14:00:00"))
    svc.handover(sh, s1, 3, dt("2026-09-29T14:00:00"),
                 ["装卸作业票", "理货交接单"], party="港口理货")
    svc.execute(sh, s1, seqs=[4, 5, 6, 7, 8])
    print(f"  S1（1 TEU）{sh.splits[s1].delivered_at:%m-%d %H:%M} 河内签收，收货窗内")
    print("  减量柜的港口作业费已实际发生、计入沉没成本（结算逐段可见）")

    print("\n" + "=" * 78)
    print("五、台风封港：S3 已报关放行但未出境 → 退关 → 改港盖邻线")
    print("=" * 78)
    svc.arrive(sh, s3, 3, dt("2026-09-29T14:00:00"))
    svc.handover(sh, s3, 3, dt("2026-09-29T14:00:00"),
                 ["装卸作业票", "理货交接单"], party="港口理货")
    svc.execute(sh, s3, seqs=[4])  # 钦州出口报关已放行，船未开
    print("  S3 钦州海关已电子放行，0930 快线尚未开船——仍在海关监管下，可撤放行")
    print("  调度员在断点（钦州）上比较未执行部分（台风期海防泊位/航线窗口全部拉黑）：")
    typhoon_blocked = [
        "SLOT-SEA-QH-0930", "SLOT-SEA-QH-1006", "SLOT-SEA-QH-1013",
        "HPH-BERTH-1002", "HPH-BERTH-1003", "HPH-BERTH-1006",
        "HPH-BERTH-1008", "HPH-BERTH-1013", "HPH-BERTH-1015",
        "HPH-CUS-1002", "HPH-CUS-1003", "HPH-CUS-1006",
        "HPH-CUS-1008", "HPH-CUS-1013", "HPH-CUS-1015",
    ]
    for alt in svc.alternatives(sh, s3, dt("2026-09-29T20:00:00"), start_seq=4,
                                blacklist=typhoon_blocked):
        if alt.get("eta"):
            print(f"    {alt['route']:10s} ETA {alt['eta']:%m-%d %H:%M}"
                  f"  断点后费用 {alt['suffix_cost']:7.0f}  {alt['name']}")
        else:
            print(f"    {alt['route']:10s} 不可行（海防泊位/航线窗口已关闭）")
    svc.withdraw_customs(sh, s3, dt("2026-09-29T20:00:00"),
                         reason="台风预警海防港10-01起封港3天，改配盖邻")
    svc.reroute(sh, s3, dt("2026-09-29T20:00:00"), "R-CAILAN",
                reason="海防封港改卸盖邻，尾程卡车进河内", start_seq=4,
                quote_id="Q-CAILAN-2026-10")
    rid2 = svc.sell_released_capacity("SLOT-SEA-QH-0930", 1.0,
                                      dt("2026-09-29T21:00:00"), buyer="某电子料货主")
    print(f"  S3 原 0930 海防舱位退回台账后再售（{rid2}）；改走钦州-盖邻 1005 支线")
    svc.execute(sh, s3)
    print(f"  S3（1 TEU）{sh.splits[s3].delivered_at:%m-%d %H:%M} 经盖邻到河内签收"
          f"（晚于收货窗，结算计迟交）")

    print("\n" + "=" * 78)
    print("六、10-02 S2 被甩箱：1006 航次临时减舱 → 自动改配 1013（原航次拉黑）")
    print("=" * 78)
    svc.execute(sh, s2, seqs=[2, 3, 4])
    snap_before = sh.splits[s2].latest_snapshot()
    svc.roll_container(sh, s2, dt("2026-10-02T10:00:00"),
                       reason="1006航次船体检修临时减舱，在港2柜被甩")
    print(f"  ETA：{snap_before.eta:%m-%d %H:%M} → {sh.splits[s2].eta:%m-%d %H:%M}")
    print("  甩箱产生堆存改配费；船闸/港口/报关已执行部分不动，只重排海船及以后")
    svc.execute(sh, s2)
    print(f"  S2（2 TEU）{sh.splits[s2].delivered_at:%m-%d %H:%M} 签收")

    print("\n" + "=" * 78)
    print("七、货主一致口径（到达与费用变化，按批可追溯）")
    print("=" * 78)
    view = shipper_view(sh)
    for r in view["rows"]:
        chg = f"{r['eta_change_hours']:+.0f}h" if r["eta_change_hours"] is not None else "-"
        print(f"  {r['split_id']}  {r['teu']}TEU  {r['state']:4s}"
              f"  原ETA {r['first_eta']:%m-%d %H:%M} → 现ETA {r['current_eta']:%m-%d %H:%M}"
              f" ({chg})  运费 {r['first_cost']:.0f}→{r['current_cost']:.0f}"
              f" ({r['cost_change']:+.0f})  承诺版本 {r['commitment_versions']}")

    print("\n" + "=" * 78)
    print("八、最终结算：成本差异来自哪一段、各次交接是否完成")
    print("=" * 78)
    rep = settle(sh, dt("2026-10-16T18:00:00"), late_penalty_per_teu_day=120.0)
    print(f"  合同 {rep['contract_no']}：{rep['delivered_teu']}/{rep['total_teu']} TEU 签收，"
          f"减量 {rep['cancelled_teu']} TEU")
    print(f"  最初承诺：{rep['first_commitment']['eta']:%m-%d %H:%M} / "
          f"{rep['first_commitment']['cost']:.0f} 元")
    print(f"  最终账单：{rep['billed_total']:.0f} 元"
          f"（含附加费 {sum(c['amount'] for c in rep['extra_charges']):.0f}、"
          f"迟交违约金 {rep['late_penalty']:.0f}），"
          f"较原承诺 {rep['vs_first_commitment']:+.0f} 元")
    v = rep["vs_old_route"]
    print(f"  对旧线（{v['name']}，订舱时无异常影子排程）逐批对照：")
    for r in v["per_split"]:
        dh = round((r["actual_eta"] - r["old_eta"]).total_seconds() / 3600, 0)
        dc = round(r["actual_billed_legs"] - r["old_cost_at_final_teu"], 0)
        verdict = "快于且省于旧线" if dh < 0 and dc < 0 else \
                  ("快于旧线" if dh < 0 else "慢于旧线") + \
                  ("，但费用增加" if dc > 0 else "且更省" if dc < 0 else "，费用持平")
        print(f"    {r['split_id'].split('#')[1]} ({r['teu']}TEU) "
              f"旧线ETA {r['old_eta']:%m-%d %H:%M}/{r['old_cost_at_final_teu']:.0f}元"
              f" → 实际 {r['actual_eta']:%m-%d %H:%M}/{r['actual_billed_legs']:.0f}元"
              f"（{dh:+.0f}h / {dc:+.0f}元，{verdict}）")
    print("  → 结论：无意外时运河线确定更快更省；甩箱+封港两次异常后，")
    print("    S2 已慢于旧线影子排程——系统不粉饰，差异和原因都计入结算。")
    print("\n  附加费归因：")
    for c in rep["extra_charges"]:
        print(f"    {c['at']:%m-%d} {c['split_id'].split('#')[1]}  {c['item']} "
              f"{c['amount']:.0f} 元（{c['reason']}）")

    print("\n  S3（改港盖邻）逐段账单：")
    s3rep = next(x for x in rep["splits"] if x["split_id"] == s3)
    for row in s3rep["leg_rows"]:
        tag = {"done": "已执行", "skipped": "已作废", "planned": "计划",
               "departed": "在途", "arrived": "待交接"}.get(row["state"], row["state"])
        if row["first_quote_cost"] is not None:
            d = row["cost_delta_vs_first"]
            dtxt = f"差异 {d:+.0f}" if d is not None else ""
        else:
            dtxt = "原方案无此段（改港新增）"
        print(f"    L{row['seq']} P{row['plan']} {row['label'][:22]:22s} "
              f"{tag} 计费 {row['billed_cost']:7.0f}  {dtxt}")
    print("  （原钦州报关费因退关沉没，盖邻重新申报；原海防海船/泊位/清关/尾程作废不计费）")

    print("\n  交接完成清单（节选）：")
    for h in rep["handovers"]:
        if h["split_id"] in (s2, s3):
            ok = "✓" if h["handover_completed"] else "✗"
            ot = "准点" if h["on_time"] else "晚点"
            print(f"    {ok} {h['split_id'].split('#')[1]} L{h['seq']} "
                  f"{h['line'][:20]:20s} {h['counterparty'] or '-':12s} "
                  f"{ot}  单证：{'、'.join(h['documents'])}")
    print("\n  单证状态：")
    for d in rep["documents"]["customs"]:
        when = d["withdrawn_at"] or d["cleared_at"] or ""
        print(f"    {d['customs_code'] or '-':14s} {d['split_id'].split('#')[1]} "
              f"{d['status']}  {when}")
    for a in rep["documents"]["appointments"]:
        print(f"    {a['window']:14s} {a['split_id'].split('#')[1]} "
              f"{a['teu']}TEU {a['status']}  {a['checked_in_at'] or ''}")
    print(f"\n  资源台账自检：{nw.ledger.audit() or '无超订、无重复释放'}")


if __name__ == "__main__":
    main()
