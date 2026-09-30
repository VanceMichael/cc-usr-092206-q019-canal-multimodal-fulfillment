"""履约系统规则测试：资源不可重复占用、只重排未执行部分、承诺可追溯、结算可归因。"""
import unittest
from datetime import datetime

from src.models import EventType, LegState, Mode, SplitState
from src.network import load_network
from src.planning import Planner, SchedulingError
from src.resources import CapacityError, DoubleReleaseError, ResourceLedger
from src.service import FulfillmentError, FulfillmentService
from src.settlement import settle
from src.reporting import dispatcher_view, shipper_view

CONTRACT = "CT-LBTEA-2026-0926"


def dt(s):
    return datetime.fromisoformat(s)


def new_world():
    nw = load_network()
    return nw, FulfillmentService(nw)


class NetworkDataTest(unittest.TestCase):
    def setUp(self):
        self.nw, self.svc = new_world()

    def test_route_distances_match_story(self):
        self.assertEqual(self.nw.route("R-OLD").distance_km, 990)
        self.assertEqual(self.nw.route("R-CANAL").distance_km, 750)

    def test_quotes_history_and_totals(self):
        qs = self.nw.quote_history("R-CANAL")
        self.assertEqual([q.quote_id for q in qs],
                         ["Q-CANAL-2025-11", "Q-CANAL-2026-08", "Q-CANAL-2026-09"])
        for q in qs:
            self.assertAlmostEqual(sum(q.per_leg.values()), q.per_teu_total, places=2)

    def test_background_cargo_consumes_capacity(self):
        self.assertEqual(self.nw.ledger.available("SLOT-SEA-QH-0930"), 5.0)
        self.assertEqual(self.nw.ledger.available("LOCK-QN-0928"), 5.0)

    def test_shadow_routes(self):
        p = Planner(self.nw.ledger)
        ready = dt("2026-09-27T10:00:00")
        eta = {}
        for code in ("R-OLD", "R-CANAL", "R-RAIL", "R-CAILAN"):
            r = self.nw.route(code)
            q = self.nw.quote(r.quote_ids[-1])
            eta[code] = p.shadow_route(r, 2.0, ready, q.per_leg).eta
        self.assertLess(eta["R-CANAL"], eta["R-OLD"])
        self.assertLess(eta["R-RAIL"], eta["R-OLD"])


class LedgerTest(unittest.TestCase):
    def test_overbooking_rejected(self):
        ledger = ResourceLedger()
        from src.models import Mode, SlotBook
        b = SlotBook("B", "S", "slot_ship", Mode.SEA, "x", "A", "B",
                     dt("2026-10-01T00:00"), dt("2026-10-02T00:00"), 10.0)
        ledger.register_book(b)
        ledger.reserve("B", 6, "r1", "X", "L1")
        with self.assertRaises(CapacityError):
            ledger.reserve("B", 5, "r2", "X", "L1")

    def test_release_once_and_reuse(self):
        ledger = ResourceLedger()
        from src.models import Mode, SlotBook
        b = SlotBook("B", "S", "slot_ship", Mode.SEA, "x", "A", "B",
                     dt("2026-10-01T00:00"), dt("2026-10-02T00:00"), 10.0)
        ledger.register_book(b)
        r = ledger.reserve("B", 8, "r1", "X", "L1")
        ledger.release(r, "E1", "重排")
        self.assertEqual(ledger.available("B"), 10.0)
        # 释放出的容量可以被新计划占用
        ledger.reserve("B", 9, "r2", "Y", "L1")
        # 同一份旧占用不能释放第二次（否则容量被重复还回）
        with self.assertRaises(DoubleReleaseError):
            ledger.release(r, "E2", "重复释放")
        self.assertEqual(ledger.used("B"), 9.0)
        self.assertEqual(ledger.audit(), [])


class BookingTest(unittest.TestCase):
    def setUp(self):
        self.nw, self.svc = new_world()
        self.sh = self.svc.book(CONTRACT, dt("2026-09-27T10:00:00"),
                                split_teu=[2.0, 2.0, 1.0])
        self.s1, self.s2, self.s3 = [f"{CONTRACT}#S{i}" for i in (1, 2, 3)]

    def test_split_sum_must_equal_contract(self):
        with self.assertRaises(FulfillmentError):
            self.svc.book(CONTRACT, dt("2026-09-27T10:00:00"), split_teu=[2.0, 2.0])

    def test_first_commitment_and_docs(self):
        for sid in (self.s1, self.s2, self.s3):
            sp = self.sh.splits[sid]
            self.assertEqual(len(sp.snapshots), 1)
            self.assertFalse(sp.snapshots[0].superseded)
            self.assertEqual(sp.eta, dt("2026-10-03T14:00:00"))
        # 每批有出口、进口两张报关单和一个收货预约
        for sid in (self.s1, self.s2, self.s3):
            decls = [d for d in self.sh.declarations.values() if d.split_id == sid]
            appts = [a for a in self.sh.appointments.values() if a.split_id == sid]
            self.assertEqual(len(decls), 2)
            self.assertEqual(len(appts), 1)

    def test_baselines_compare_old_and_alternatives(self):
        b = self.sh.baselines
        self.assertLess(b["R-CANAL"]["eta"], b["R-OLD"]["eta"])
        self.assertLess(b["R-CANAL"]["cost"], b["R-OLD"]["cost"])


class DelayReplanTest(unittest.TestCase):
    def setUp(self):
        self.nw, self.svc = new_world()
        self.sh = self.svc.book(CONTRACT, dt("2026-09-27T10:00:00"),
                                split_teu=[2.0, 2.0, 1.0])
        self.s1, self.s2, self.s3 = [f"{CONTRACT}#S{i}" for i in (1, 2, 3)]

    def test_delay_only_replans_open_legs_of_that_split(self):
        # S2 第一段已发运到达（锁定）
        self.svc.execute(self.sh, self.s2, seqs=[1])
        eta1_before = self.sh.splits[self.s1].eta
        self.svc.register_delay(
            self.sh, self.s2, dt("2026-09-28T02:00:00"), wait_hours=30,
            reason="航道管制",
            blacklist=["LOCK-MD-0928", "LOCK-QN-0928", "LOCK-QS-0928"])
        # S1/S3 承诺不动
        self.assertEqual(self.sh.splits[self.s1].eta, eta1_before)
        self.assertEqual(self.sh.splits[self.s3].eta, dt("2026-10-03T14:00:00"))
        # S2 已执行航段保留，后续出现第二版计划
        sp = self.sh.splits[self.s2]
        self.assertTrue(sp.legs[0].state == LegState.DONE)
        self.assertEqual(sp.snapshots[0].superseded, True)
        self.assertEqual(len(sp.snapshots), 2)
        self.assertGreater(sp.eta, dt("2026-10-03T14:00:00"))

    def test_cannot_replan_departed_leg(self):
        self.svc.execute(self.sh, self.s2, seqs=[1, 2])  # 运河段已发运
        with self.assertRaises(FulfillmentError):
            # 试图从第 2 段重排（已锁定）必须被拒绝
            self.svc.reroute(self.sh, self.s2, dt("2026-09-28T12:00:00"),
                             "R-RAIL", reason="尝试改掉在运航段", start_seq=2)


class RolledContainerTest(unittest.TestCase):
    def setUp(self):
        self.nw, self.svc = new_world()
        self.sh = self.svc.book(CONTRACT, dt("2026-09-27T10:00:00"),
                                split_teu=[2.0, 2.0, 1.0])
        self.s1, self.s2, self.s3 = [f"{CONTRACT}#S{i}" for i in (1, 2, 3)]
        self.svc.execute(self.sh, self.s2, seqs=[1])
        self.svc.register_delay(
            self.sh, self.s2, dt("2026-09-28T02:00:00"), wait_hours=30,
            reason="航道管制",
            blacklist=["LOCK-MD-0928", "LOCK-QN-0928", "LOCK-QS-0928"])
        self.svc.execute(self.sh, self.s2, seqs=[2, 3, 4])

    def test_roll_blocks_voyage_and_picks_next(self):
        sea_legs_old = [l for l in self.sh.splits[self.s2].legs
                        if l.seq == 5 and not l.state == LegState.SKIPPED]
        blocked = sea_legs_old[0].reservations[0].resource_code
        self.assertEqual(blocked, "SLOT-SEA-QH-1006")
        eta_before = self.sh.splits[self.s2].eta
        self.svc.roll_container(self.sh, self.s2, dt("2026-10-02T10:00:00"),
                                reason="航次减舱")
        new_sea = [l for l in self.sh.splits[self.s2].active_legs if l.seq == 5][0]
        self.assertEqual(new_sea.reservations[0].resource_code, "SLOT-SEA-QH-1013")
        self.assertGreater(self.sh.splits[self.s2].eta, eta_before)
        # 甩箱费
        self.assertTrue(any(c["item"].startswith("甩箱") for c in self.sh.extra_charges))
        # 原航次舱位已释放且黑名单持续有效（再排不会回到 1006）
        self.assertIn(blocked, self.sh.splits[self.s2].last_blacklist)


class CustomsWithdrawRerouteTest(unittest.TestCase):
    def setUp(self):
        self.nw, self.svc = new_world()
        self.sh = self.svc.book(CONTRACT, dt("2026-09-27T10:00:00"),
                                split_teu=[2.0, 2.0, 1.0])
        self.s1, self.s2, self.s3 = [f"{CONTRACT}#S{i}" for i in (1, 2, 3)]
        self.svc.execute(self.sh, self.s3, seqs=[1, 2, 3, 4])

    def test_withdraw_then_reroute_to_cailan(self):
        self.svc.withdraw_customs(self.sh, self.s3, dt("2026-09-29T20:00:00"),
                                  reason="台风封港")
        decls = [d for d in self.sh.declarations.values()
                 if d.split_id == self.s3 and d.customs_code.startswith("QZ-CUS")]
        self.assertTrue(any(d.withdrawn for d in decls))
        self.svc.reroute(self.sh, self.s3, dt("2026-09-29T20:00:00"),
                         "R-CAILAN", reason="改港盖邻", start_seq=4,
                         quote_id="Q-CAILAN-2026-10")
        sp = self.sh.splits[self.s3]
        self.assertEqual(sp.route_code, "R-CAILAN")
        # 原海防后续航段全部作废不计费
        skipped = [l for l in sp.legs if l.state == LegState.SKIPPED]
        self.assertTrue(any("海防快线" in l.line_name for l in skipped))
        # 旧报关航段留痕（replaced，不回收窗口容量），新计划在 seq4 重报
        active4 = [l for l in sp.active_legs if l.seq == 4]
        self.assertEqual(len(active4), 1)
        self.assertIn("退关重报", active4[0].line_name)
        # 释放的海防舱位可以再售
        self.svc.sell_released_capacity("SLOT-SEA-QH-0930", 1.0,
                                        dt("2026-09-29T21:00:00"), buyer="外部货主")

    def test_cannot_withdraw_after_goods_sailed(self):
        self.svc.execute(self.sh, self.s3, seqs=[5])  # 海船已发运=出境
        with self.assertRaises(FulfillmentError):
            self.svc.withdraw_customs(self.sh, self.s3, dt("2026-09-30T20:00:00"),
                                      reason="船开了才想退")

    def test_reroute_boundary_must_match(self):
        with self.assertRaises(FulfillmentError):
            self.svc.reroute(self.sh, self.s3, dt("2026-09-29T20:00:00"),
                             "R-RAIL", reason="断点不衔接", start_seq=4)


class QuantityChangeTest(unittest.TestCase):
    def setUp(self):
        self.nw, self.svc = new_world()
        self.sh = self.svc.book(CONTRACT, dt("2026-09-27T10:00:00"),
                                split_teu=[2.0, 2.0, 1.0])
        self.s1 = f"{CONTRACT}#S1"
        self.svc.execute(self.sh, self.s1, seqs=[1, 2, 3])

    def test_reduce_frees_capacity_and_replans(self):
        before = self.nw.ledger.available("SLOT-SEA-QH-0930")
        self.svc.change_quantity(self.sh, self.s1, dt("2026-09-29T16:30:00"),
                                 1.0, reason="门店延后")
        self.assertEqual(self.sh.splits[self.s1].teu, 1.0)
        self.assertEqual(self.sh.cancelled_teu, 1.0)
        self.assertAlmostEqual(
            self.nw.ledger.available("SLOT-SEA-QH-0930"), before + 1.0)
        # 释放舱位可立即被外部占用
        self.svc.sell_released_capacity(
            "SLOT-SEA-QH-0930", 1.0, dt("2026-09-29T17:00:00"), buyer="钛矿货主")

    def test_increase_rejected(self):
        with self.assertRaises(FulfillmentError):
            self.svc.change_quantity(self.sh, self.s1, dt("2026-09-29T16:30:00"),
                                     3.0, reason="想加量")


class ExecutionGuardTest(unittest.TestCase):
    def setUp(self):
        self.nw, self.svc = new_world()
        self.sh = self.svc.book(CONTRACT, dt("2026-09-27T10:00:00"))
        self.sid = f"{CONTRACT}#S1"

    def test_state_transitions_and_docs_required(self):
        with self.assertRaises(FulfillmentError):
            self.svc.arrive(self.sh, self.sid, 1, dt("2026-09-27T12:00:00"))
        self.svc.depart(self.sh, self.sid, 1, dt("2026-09-27T10:00:00"))
        with self.assertRaises(FulfillmentError):
            self.svc.depart(self.sh, self.sid, 1, dt("2026-09-27T10:00:00"))
        self.svc.arrive(self.sh, self.sid, 1, dt("2026-09-28T02:00:00"))
        with self.assertRaises(FulfillmentError):
            self.svc.handover(self.sh, self.sid, 1, dt("2026-09-28T02:00:00"), docs=[])

    def test_settlement_requires_delivery(self):
        with self.assertRaises(FulfillmentError):
            settle(self.sh, dt("2026-10-02T00:00:00"))


class NoDoubleHoldingTest(unittest.TestCase):
    """贯穿完整异常剧本：任何时刻同一份箱量不得在新旧两份计划里被重复占用。"""

    def test_full_scenario_ledger_invariant(self):
        nw, svc = new_world()
        sh = svc.book(CONTRACT, dt("2026-09-27T10:00:00"),
                      split_teu=[2.0, 2.0, 1.0])
        s1, s2, s3 = [f"{CONTRACT}#S{i}" for i in (1, 2, 3)]

        svc.execute(sh, s1, seqs=[1, 2, 3])
        svc.execute(sh, s2, seqs=[1])
        svc.register_delay(sh, s2, dt("2026-09-28T02:00:00"), 30, "管制",
                           blacklist=["LOCK-MD-0928", "LOCK-QN-0928", "LOCK-QS-0928"])

        def active_by_split(sid):
            out = {}
            for l in sh.splits[sid].active_legs:
                for r in l.reservations:
                    if r.active:
                        out[r.res_id] = r.resource_code
            return out

        def leg_of_res(sid, rid):
            for l in sh.splits[sid].legs:
                if any(r.res_id == rid for r in l.reservations):
                    return l
            return None

        held_before = {sid: active_by_split(sid) for sid in (s1, s2, s3)}
        svc.change_quantity(sh, s1, dt("2026-09-29T16:30:00"), 1.0, "减量")
        svc.sell_released_capacity("SLOT-SEA-QH-0930", 1.0,
                                   dt("2026-09-29T17:00:00"), "钛矿货主")
        svc.execute(sh, s1)

        svc.execute(sh, s3, seqs=[1, 2, 3, 4])
        svc.withdraw_customs(sh, s3, dt("2026-09-29T20:00:00"), "封港")
        svc.reroute(sh, s3, dt("2026-09-29T20:00:00"), "R-CAILAN",
                    "改港", start_seq=4, quote_id="Q-CAILAN-2026-10")
        svc.sell_released_capacity("SLOT-SEA-QH-0930", 1.0,
                                   dt("2026-09-29T21:00:00"), "电子料货主")
        svc.execute(sh, s3)

        svc.execute(sh, s2, seqs=[2, 3, 4])
        svc.roll_container(sh, s2, dt("2026-10-02T10:00:00"), "减舱甩箱")
        svc.execute(sh, s2)

        # 台账自检：无超订、活动占用合计与已用容量一致
        self.assertEqual(nw.ledger.audit(), [])
        # 铁律：作废航段不得残留活动占用；旧计划占用要么失活、要么属于留痕的已闭环航段
        for sid, held in held_before.items():
            for rid in held:
                res = nw.ledger.reservation(rid)
                leg = leg_of_res(sid, rid)
                if res.active:
                    self.assertNotEqual(leg.state, LegState.SKIPPED)
                    self.assertTrue(leg.replaced or rid in active_by_split(sid))
        # 所有批次已交付
        self.assertTrue(all(s.state == SplitState.DELIVERED for s in sh.splits.values()))


class SettlementTest(unittest.TestCase):
    def _scenario(self):
        nw, svc = new_world()
        sh = svc.book(CONTRACT, dt("2026-09-27T10:00:00"),
                      split_teu=[2.0, 2.0, 1.0])
        s1, s2, s3 = [f"{CONTRACT}#S{i}" for i in (1, 2, 3)]
        svc.execute(sh, s1, seqs=[1, 2, 3])
        svc.execute(sh, s2, seqs=[1])
        svc.register_delay(sh, s2, dt("2026-09-28T02:00:00"), 30, "管制",
                           blacklist=["LOCK-MD-0928", "LOCK-QN-0928", "LOCK-QS-0928"])
        svc.change_quantity(sh, s1, dt("2026-09-29T16:30:00"), 1.0, "减量")
        svc.execute(sh, s1)
        svc.execute(sh, s3, seqs=[1, 2, 3, 4])
        svc.withdraw_customs(sh, s3, dt("2026-09-29T20:00:00"), "封港")
        svc.reroute(sh, s3, dt("2026-09-29T20:00:00"), "R-CAILAN",
                    "改港", start_seq=4, quote_id="Q-CAILAN-2026-10")
        svc.execute(sh, s3)
        svc.execute(sh, s2, seqs=[2, 3, 4])
        svc.roll_container(sh, s2, dt("2026-10-02T10:00:00"), "减舱甩箱")
        svc.execute(sh, s2)
        return nw, svc, sh, (s1, s2, s3)

    def test_settlement_attribution_and_handovers(self):
        nw, svc, sh, (s1, s2, s3) = self._scenario()
        rep = settle(sh, dt("2026-10-16T18:00:00"), late_penalty_per_teu_day=120.0)
        # 账单 = 已执行航段 + 附加费 + 迟交罚金
        self.assertAlmostEqual(
            rep["billed_total"],
            rep["billed_legs_only"]
            + sum(c["amount"] for c in rep["extra_charges"])
            + rep["late_penalty"], places=2)
        # 所有未作废航段交接均完成且有单证
        for h in rep["handovers"]:
            self.assertTrue(h["handover_completed"])
            self.assertTrue(h["documents"])
        # S3 改港：旧线航段作废零计费，盖邻段计费
        s3rep = next(x for x in rep["splits"] if x["split_id"] == s3)
        done_sea = [r for r in s3rep["leg_rows"]
                    if r["state"] == "done" and "盖邻" in r["label"]]
        self.assertTrue(done_sea)
        skipped = [r for r in s3rep["leg_rows"] if r["state"] == "skipped"]
        self.assertTrue(all(r["billed_cost"] == 0 for r in skipped))
        # 承诺演进完整保留（含被取代的版本）
        traces = s3rep["commitment_trace"]
        self.assertEqual([t["snap_no"] for t in traces], [1, 2])
        self.assertTrue(traces[0]["superseded"])
        self.assertFalse(traces[1]["superseded"])
        # 单证：原出口报关已退关、盖邻进口清关已放行
        customs = rep["documents"]["customs"]
        self.assertTrue(any(d["status"] == "已退关" for d in customs))
        self.assertTrue(any(d["status"] == "已放行" and d["customs_code"].startswith("CLP-CUS")
                            for d in customs))
        # 预约：被甩批次旧预约取消释放，最终预约到货
        appts = rep["documents"]["appointments"]
        self.assertTrue(any(a["status"] == "已取消释放" for a in appts))
        self.assertTrue(any(a["status"] == "按时到货" or a["status"] == "未使用"
                            for a in appts))

    def test_shipper_and_dispatcher_views_consistent(self):
        nw, svc, sh, splits = self._scenario()
        view = shipper_view(sh)
        # 三批口径汇总箱量守恒（交付+减量=合同）
        self.assertAlmostEqual(
            sum(r["teu"] for r in view["rows"]) + sh.cancelled_teu,
            sh.contract.total_teu, places=2)
        dv = dispatcher_view(sh, svc)
        self.assertEqual(dv["ledger_audit"], [])
        for s in dv["splits"]:
            self.assertIsNone(s["next_open_seq"])  # 全部执行完


class SeaRailAlternativeTest(unittest.TestCase):
    def test_rail_route_bookable_and_reroute_from_start(self):
        nw, svc = new_world()
        sh = svc.book(CONTRACT, dt("2026-09-27T10:00:00"),
                      split_teu=[2.0, 2.0, 1.0])
        sid = f"{CONTRACT}#S1"
        # 未发运前可整条切换海铁备份线（5 TEU 超班列容量，但 2 TEU 的批次可行）
        svc.reroute(sh, sid, dt("2026-09-27T12:00:00"), "R-RAIL",
                    reason="运河船闸军演停航", start_seq=1,
                    quote_id="Q-RAIL-2026-09")
        self.assertEqual(sh.splits[sid].route_code, "R-RAIL")
        modes = [l.mode for l in sh.splits[sid].active_legs]
        self.assertIn(Mode.RAIL, modes)
        self.assertNotIn(Mode.CANAL, modes)
        svc.execute(sh, sid)
        self.assertEqual(sh.splits[sid].state, SplitState.DELIVERED)
        self.assertEqual(nw.ledger.audit(), [])

    def test_replan_failure_changes_nothing(self):
        """影子验证不可行时：不释放任何占用、批次进入甩箱待重排。"""
        nw, svc = new_world()
        sh = svc.book(CONTRACT, dt("2026-09-27T10:00:00"),
                      split_teu=[2.0, 2.0, 1.0])
        sid = f"{CONTRACT}#S1"
        # 把所有运河船闸窗口全部拉黑 → 无可排日期，重排应失败且占用原样保留
        all_locks = [w.code for w in nw.ledger.windows.values() if w.kind == "lock"]
        before = {c: nw.ledger.used(c) for c in nw.ledger.books}
        with self.assertRaises(SchedulingError):
            svc.register_delay(sh, sid, dt("2026-09-27T11:00:00"), 2,
                               reason="停航", blacklist=all_locks)
        self.assertEqual(sh.splits[sid].state, SplitState.ROLLED)
        for code, used in before.items():
            self.assertAlmostEqual(nw.ledger.used(code), used)
        self.assertEqual(nw.ledger.audit(), [])


if __name__ == "__main__":
    unittest.main()
