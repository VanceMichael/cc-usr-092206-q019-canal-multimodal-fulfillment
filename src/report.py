"""把履约/结算结果渲染为可读文本（终端或归档）。"""
from __future__ import annotations

from . import timeutil


def option_table(options) -> str:
    head = (f"{'方案':<22}{'里程km':>7}{'交付时刻':>18}{'全程h':>7}"
            f"{'总价(元)':>10}{'每TEU':>8}  关键等待/瓶颈")
    lines = [head, "-" * len(head)]
    for o in options:
        lines.append(
            f"{o.route_name:<22}{o.distance_km:>7}{timeutil.fmt(o.delivery):>18}"
            f"{o.transit_hours:>7.0f}{o.cost_total:>10.0f}{o.cost_per_teu:>8.0f}  "
            f"{o.bottlenecks}")
    return "\n".join(lines)


def promise_timeline(timeline: list[dict]) -> str:
    lines = [f"{'版本':<6}{'路线':<24}{'报价':<12}{'承诺到达':>18}{'承诺费用':>10}  状态/原因"]
    for t in timeline:
        lines.append(
            f"v{t['version']:<5}{t['route']:<24}{t['rate_card']:<12}"
            f"{t['committed_eta']:>18}{t['committed_cost']:>10.0f}  "
            f"[{t['state']}] {t['reason']} (异常{t['exception'] or '-'})")
    return "\n".join(lines)


def segment_table(segments: list[dict]) -> str:
    lines = [f"{'#':>2} {'状态':<9}{'类型':<10}{'计划开始':<17}{'计划完成':<17}"
             f"{'实际完成':<17}{'偏差h':>6}{'基础费':>8}{'附加费':>8}  航段"]
    stat = {"PLANNED": "待执行", "EXECUTED": "已完成", "CANCELLED": "已作废"}
    for s in segments:
        lines.append(
            f"{s['seq']:>2} {stat.get(s['status'], s['status']):<9}{s['type']:<10}"
            f"{s['planned_start']:<17}{s['planned_end']:<17}"
            f"{(s['actual_end'] or '-'):<17}{s['schedule_var_hours']:>6.1f}"
            f"{s['base_fee']:>8.0f}{s['surcharge_fee']:>8.0f}  {s['label']}")
        for d in s["surcharge_detail"]:
            lines.append(f"      └─ {d['label']} +{d['amount']:.0f}元  归因: {d['reason']}")
    return "\n".join(lines)


def handoff_table(handoffs: list[dict]) -> str:
    state = {"COMPLETED": "已完成", "PENDING": "待交接", "REVOKED": "已撤销(退关等)"}
    lines = [f"{'#':>2} {'状态':<14}{'交出方':<12}{'接收方':<14}{'节点':<10}"
             f"{'计划时刻':<17}{'实际时刻':<17}"]
    for h in handoffs:
        lines.append(
            f"{h['seq']:>2} {state.get(h['state'], h['state']):<14}{h['from']:<12}"
            f"{h['to']:<14}{h['node']:<10}{h['planned_at']:<17}"
            f"{(h['actual_at'] or '-'):<17}")
    return "\n".join(lines)


def cost_attribution_block(attr: dict) -> str:
    lines = [
        f"原承诺费用 v1: {attr['v1_cost']:.0f}  当前承诺: {attr['current_cost']:.0f}  "
        f"差异: {attr['total_delta']:+.0f} 元",
        f"  其中 基础费逐段差异合计 {attr['base_delta_sum']:+.0f}；"
        f"异常附加费合计 {attr['surcharge_delta_sum']:+.0f}；"
        f"对账{'平衡 ✓' if attr['reconciled'] else '不平 ✗'}",
    ]
    if attr["base_segment_delta"]:
        lines.append("  逐段基础费差异:")
        for d in attr["base_segment_delta"]:
            lines.append(f"    · #{d['seq']} {d['label']}: "
                         f"{d['old']:.0f}→{d['new']:.0f} ({d['delta']:+.0f}) "
                         f"[{'；'.join(d['reasons'])}]")
    if attr["surcharge_delta"]:
        lines.append("  异常附加费:")
        for r in attr["surcharge_delta"]:
            lines.append(f"    · {r['exception']}/{r['kind']} {r['label']} "
                         f"+{r['amount']:.0f} 元 — {r['detail']}")
    return "\n".join(lines)


def benchmark_block(b: dict) -> str:
    verdict_time = "更快" if b["faster_than_old_hours"] > 0 else "更慢"
    verdict_cost = "更省" if b["cheaper_than_old"] > 0 else "更贵"
    return (
        f"对照同备货日旧线（{b['distance_km_old']}km→{b['distance_km_current']}km）:\n"
        f"  旧线 {b['old_route_eta']} / {b['old_route_cost']:.0f}元   "
        f"本票 {b['current_route']} {b['current_eta']} / {b['current_cost']:.0f}元\n"
        f"  时间{verdict_time} {abs(b['faster_than_old_hours']):.0f} 小时；"
        f"费用{verdict_cost} {abs(b['cheaper_than_old']):.0f} 元（相对旧线全程）")


def shipper_block(view: dict) -> str:
    lines = [
        f"分段 {view['shipment_id']}（{view['teu']}TEU，状态 {view['status']}）",
        f"  原承诺 v{view['original_promise']['version']} "
        f"{view['original_promise']['route']}: 到达 "
        f"{timeutil.fmt(view['original_promise']['eta'])}，"
        f"{view['original_promise']['cost']:.0f}元",
        f"  当前   v{view['current']['version']} "
        f"{view['current']['route']}（{view['current']['rate_card']}）: 到达 "
        f"{timeutil.fmt(view['current']['eta'])}，{view['current']['cost']:.0f}元",
        f"  到达变化 {view['eta_delta_hours']:+.0f}h；费用变化 "
        f"{view['cost_delta']:+.0f}元",
    ]
    if view["reasons"]:
        lines.append("  变化原因（可追溯到异常单与版本）:")
        for r in view["reasons"]:
            lines.append(
                f"    · {r['id']} {r['kind']} {r['v']} {r['at']:%Y-%m-%d %H:%M} "
                f"{r['reason']}（交付影响 {r['impact_hours']:g}h，附加费 "
                f"{r['surcharge']:.0f}元）")
    return "\n".join(lines)
