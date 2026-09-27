"""履约服务：执行、五类异常重排、承诺追溯、资源不重复占用、结算归因。"""
import unittest

from src.calendar import CapacityError
from src.model import PLANNED, EXECUTED, CANCELLED, ST_DONE
from src.service import FulfillmentService, FulfillmentError
from src.settlement import Settlement
from src import timeutil as T


def new_service():
    svc = FulfillmentService()
    svc.book_contract("CT-TEA-2610")
    return svc


def run(svc, sid, seqs):
    for seq in seqs:
        st = svc._step(sid, seq)
        if st.docs:
            svc.submit_docs(sid, seq, [d.name for d in st.docs], st.planned_start)
        svc.execute(sid, seq, st.planned_start, actual_end=st.planned_end)


S1 = "CT-TEA-2610-S1"
S2 = "CT-TEA-2610-S2"


class BookingTest(unittest.TestCase):
    def test_book_creates_versions_and_promises(self):
        svc = new_service()
        for sid in (S1, S2):
            s = svc.shipments[sid]
            self.assertEqual(len(s.versions), 1)
            self.assertEqual(s.current.version, 1)
            self.assertEqual(s.current.state, "ACTIVE")

    def test_docs_required_before_customs_execution(self):
        svc = new_service()
        with self.assertRaises(FulfillmentError):
            svc.execute(S1, 2, T.parse("2026-10-08T17:00"))   # 单证未齐

    def test_cannot_execute_twice(self):
        svc = new_service()
        svc.execute(S1, 1, T.parse("2026-10-08T12:00"))
        with self.assertRaises(FulfillmentError):
            svc.execute(S1, 1, T.parse("2026-10-08T12:00"))


class DelayTest(unittest.TestCase):
    def test_in_canal_delay_rebooks_locks_and_cascades(self):
        svc = new_service()
        v1_eta = svc.shipments[S1].original_promise.committed_eta
        run(svc, S1, [1, 2])
        ver = svc.delay_in_canal(
            S1, 3, delay_hours=9, at=T.parse("2026-10-11T12:00"),
            reason="水位管控")
        # 在途航段冻结保留，前缀已执行，尾部重建
        self.assertEqual(ver.version, 2)
        self.assertEqual(ver.steps[2].status, PLANNED)
        self.assertEqual(ver.steps[0].status, EXECUTED)
        # 船闸改订后抵港晚于原计划
        self.assertGreater(ver.steps[2].planned_end,
                           svc.shipments[S1].version(1).steps[2].planned_end)
        # 级联：错过支线班期，交付显著晚于原承诺
        self.assertGreater(ver.committed_eta, v1_eta)
        # v1 完整保留可追溯
        self.assertEqual(svc.shipments[S1].version(1).committed_eta, v1_eta)
        self.assertEqual(svc.shipments[S1].version(1).state, "SUPERSEDED")

    def test_delay_no_double_booking_of_locks(self):
        svc = new_service()
        run(svc, S1, [1, 2])
        step3_before = svc._step(S1, 3)
        n_book = len(step3_before.bookings)
        svc.delay_in_canal(S1, 3, 9, T.parse("2026-10-11T12:00"), "水位管控")
        step3_after = svc._step(S1, 3)
        # 改订后仍是 1 班次 + 剩余未过船闸（已过的被核销移出）
        self.assertLessEqual(len(step3_after.bookings), n_book)
        # 日历中该分段活跃占用编号不重复
        active = [h.booking_id for h in svc.cal.holds_of(S1)]
        self.assertEqual(len(active), len(set(active)))


class RollTest(unittest.TestCase):
    def test_roll_adds_fees_and_later_delivery(self):
        svc = new_service()
        run(svc, S2, [1, 2, 3, 4, 5])
        eta0 = svc.shipments[S2].current.committed_eta
        cost0 = svc.shipments[S2].current.committed_cost
        ver = svc.roll_at_port(S2, T.parse("2026-10-20T18:00"),
                               "支线爆舱甩箱", storage_days=6)
        self.assertGreaterEqual(ver.committed_eta, eta0)
        sur = [f for s in ver.steps for f in s.fees if f.category == "SURCHARGE"]
        keys = {f.key for f in sur}
        self.assertIn("ROLL_FEE", keys)
        self.assertIn("PORT_STORAGE", keys)      # 6天-免4=2天计费
        storage = next(f for f in sur if f.key == "PORT_STORAGE")
        self.assertEqual(storage.qty, 2)
        self.assertGreater(ver.committed_cost, cost0)


class WithdrawTest(unittest.TestCase):
    def test_withdraw_redeclares_from_customs(self):
        svc = new_service()
        run(svc, S1, [1, 2])
        ver = svc.withdraw_and_redeclare(
            S1, T.parse("2026-10-09T09:00"), "植检批号错误退单",
            T.parse("2026-10-09T14:00"))
        # 进场保留，报关起重做
        self.assertEqual(ver.steps[0].status, EXECUTED)
        self.assertEqual(ver.steps[1].type, "CUSTOMS")
        self.assertEqual(ver.steps[1].status, PLANNED)
        sur = {f.key for s in ver.steps for f in s.fees
               if f.category == "SURCHARGE"}
        self.assertEqual(sur, {"CUSTOMS_REDECLARE", "DOC_REISSUE"})

    def test_withdraw_without_declaration_rejected(self):
        svc = new_service()
        with self.assertRaises(FulfillmentError):
            svc.withdraw_and_redeclare(
                S1, T.parse("2026-10-08T10:00"), "x",
                T.parse("2026-10-08T14:00"))


class RerouteTest(unittest.TestCase):
    def test_reroute_freezes_prefix_and_changes_tail(self):
        svc = new_service()
        run(svc, S2, [1, 2, 3, 4, 5])
        ver = svc.reroute(S2, "RAIL_SEA", "v1",
                          T.parse("2026-10-20T22:00"), "海防拥堵改铁路")
        self.assertEqual(ver.route, "RAIL_SEA")
        # 前 5 步运河前缀冻结
        self.assertEqual([s.status for s in ver.steps[:5]],
                         [EXECUTED] * 5)
        # 尾部出现铁路/口岸节点
        nodes = {s.node for s in ver.steps[5:]}
        self.assertIn("PINGXIANG", nodes)
        self.assertIn("HUU_NGHI", nodes)
        self.assertIn("REROUTE_ADMIN",
                      {f.key for s in ver.steps for f in s.fees
                       if f.category == "SURCHARGE"})

    def test_reroute_atomic_when_alternative_full(self):
        import datetime
        from src.calendar import VOY
        svc = new_service()
        run(svc, S2, [1, 2, 3, 4, 5])
        # 压满海铁国内段班列未来班次
        reqs = []
        base = T.parse("2026-10-20T06:00")
        for d in range(28):
            day = base + datetime.timedelta(days=d)
            if day.weekday() in (0, 3):
                reqs.append({"kind": VOY, "key": "VS_RAIL_COAST",
                             "bucket": day.replace(hour=6, minute=0),
                             "teu": 70, "capacity": 70, "shipment_id": "Z",
                             "step_seq": 0, "label": "占满"})
        svc.cal.hold_many(reqs)
        old_eta = svc.shipments[S2].current.committed_eta
        old_bids = {b for s in svc.shipments[S2].current.steps
                    for b in s.bookings}
        with self.assertRaises(CapacityError):
            svc.reroute(S2, "RAIL_SEA", "v1",
                        T.parse("2026-10-20T22:00"), "试改海铁")
        cur = svc.shipments[S2].current
        self.assertEqual(cur.version, 1)                  # 未产生新版本
        self.assertEqual(cur.committed_eta, old_eta)
        self.assertEqual({b for s in cur.steps for b in s.bookings}, old_bids)


class QuantityTest(unittest.TestCase):
    def test_quantity_change_reprices_only_unexecuted(self):
        svc = new_service()
        ver = svc.change_quantity(S2, 4, T.parse("2026-10-09T10:00"), "客户加量")
        self.assertEqual(svc.shipments[S2].teu, 4)
        # 4 TEU 全程单价 6870
        self.assertEqual(ver.committed_cost, 6870 * 4)

    def test_quantity_change_after_partial_execution(self):
        svc = new_service()
        run(svc, S2, [1, 2])
        # 已执行两步按 3TEU 实际发生，尾部按 5TEU 重排
        ver = svc.change_quantity(S2, 5, T.parse("2026-10-13T10:00"), "客户加量")
        self.assertEqual(ver.steps[0].fees[0].teu, 3)    # 冻结前缀仍是3
        tail = [s for s in ver.steps if s.status == PLANNED]
        self.assertTrue(all(f.teu == 5 for s in tail for f in s.fees))

    def test_invalid_quantity_rejected(self):
        svc = new_service()
        with self.assertRaises(FulfillmentError):
            svc.change_quantity(S2, 0, T.parse("2026-10-09T10:00"), "取消")


class CancelTest(unittest.TestCase):
    def test_cancel_releases_all_pending(self):
        svc = new_service()
        active_before = len(svc.cal.holds_of(S2))
        svc.cancel_shipment(S2, T.parse("2026-10-13T10:00"), "客户取消")
        self.assertEqual(svc.shipments[S2].status, "CANCELLED")
        self.assertEqual(len(svc.cal.holds_of(S2)), 0)
        self.assertGreater(active_before, 0)


class ViewAndSettlementTest(unittest.TestCase):
    def test_shipper_view_consistent_and_traceable(self):
        svc = new_service()
        run(svc, S1, [1, 2])
        svc.delay_in_canal(S1, 3, 9, T.parse("2026-10-11T12:00"), "水位管控")
        view = svc.shipper_view(S1)
        self.assertEqual(view["original_promise"]["eta"],
                         svc.shipments[S1].version(1).committed_eta)
        self.assertEqual(view["current"]["eta"],
                         svc.shipments[S1].current.committed_eta)
        self.assertGreater(view["eta_delta_hours"], 0)
        self.assertEqual(len(view["reasons"]), 1)
        self.assertEqual(view["reasons"][0]["kind"], "DELAY")

    def test_cost_attribution_reconciles(self):
        svc = new_service()
        svc.change_quantity(S2, 4, T.parse("2026-10-09T10:00"), "加量")
        run(svc, S2, [1, 2, 3, 4, 5])
        svc.roll_at_port(S2, T.parse("2026-10-20T18:00"), "甩箱", storage_days=6)
        stl = Settlement(svc)
        attr = stl.cost_attribution(svc.shipments[S2])
        self.assertTrue(attr["reconciled"])
        # 差异 = 改量基础费差 + 甩箱附加费
        self.assertEqual(attr["total_delta"],
                         attr["base_delta_sum"] + attr["surcharge_delta_sum"])

    def test_full_delivery_marks_complete_and_settles(self):
        svc = new_service()
        run(svc, S1, [1, 2])
        svc.delay_in_canal(S1, 3, 9, T.parse("2026-10-11T12:00"), "水位管控")
        run(svc, S1, range(3, 11))
        ship = svc.shipments[S1]
        self.assertEqual(ship.status, ST_DONE)
        self.assertIsNotNone(ship.delivered_at)
        stl = Settlement(svc)
        rep = stl.shipment_report(ship, svc.contracts["CT-TEA-2610"].raw)
        self.assertTrue(rep["segments_all_executed"])
        self.assertEqual(rep["docs_missing"], [])
        # 所有货权交接完成
        self.assertTrue(all(h["state"] == "COMPLETED"
                            for h in rep["handoffs"] if h["stage"] != "DELIVERY"
                            or True))


if __name__ == "__main__":
    unittest.main()
