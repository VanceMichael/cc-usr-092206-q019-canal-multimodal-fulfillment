"""排程与方案比选测试。"""
import unittest

from src.catalog import Catalog
from src.calendar import ResourceCalendar, CapacityError
from src.planner import Planner
from src.service import FulfillmentService
from src import timeutil as T

READY = T.parse("2026-10-08T08:00")


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.cat = Catalog()
        self.cal = ResourceCalendar()
        self.pl = Planner(self.cat, self.cal)

    def build(self, route="CANAL", card="v2", teu=3, dry=False, rel=None):
        return self.pl.build(
            route=route, card_version=card, teu=teu, ready=READY,
            appointment_set="APP_TEA", shipment_id="T", dry_run=dry,
            release_booking_ids=rel)

    def test_canal_chain_and_locks(self):
        b = self.build()
        # 10 个航段：进场/报关/直达(含3闸)/中转/转关/支线/卸船/清关/卡车/预约
        self.assertEqual(len(b.steps), 10)
        voy = b.steps[2]
        self.assertEqual(len(voy.bookings), 4)      # 1 班次 + 3 船闸
        # 船闸窗口不早于名义到达
        self.assertGreaterEqual(voy.planned_end, voy.planned_start)

    def test_dry_run_does_not_consume_resources(self):
        self.build(dry=True)
        self.assertEqual(self.cal.utilization(), [])

    def test_cost_matches_rate_card(self):
        b = self.build(teu=3)
        # 运河标准卡每TEU 6870，3TEU = 20610
        self.assertEqual(b.base_cost, 6870 * 3)

    def test_cutoff_missed_moves_customs_next_day(self):
        # 备货 16:00 已过梧州 12:00 截单：报关落到次日
        b = self.pl.build(
            route="CANAL", card_version="v2", teu=3,
            ready=T.parse("2026-10-08T16:00"), appointment_set="APP_TEA",
            shipment_id="T")
        cus = b.steps[1]
        self.assertGreater(cus.planned_start.date(),
                           T.parse("2026-10-08T16:00").date())

    def test_capacity_pushes_to_later_voyage(self):
        from src.calendar import VOY
        # 备货当周四，报关当天放行后最早直达班为周六 10-10 06:00；直接占满该班次
        self.cal.hold_many([{
            "kind": VOY, "key": "VS_CANAL_DIRECT",
            "bucket": T.parse("2026-10-10T06:00"), "teu": 90, "capacity": 90,
            "shipment_id": "FILL", "step_seq": 3, "label": "占满周六班"}])
        b = self.build()
        self.assertEqual(b.steps[2].planned_start,
                         T.parse("2026-10-12T06:00"))    # 顺延到下周一班

    def test_no_capacity_within_window_raises(self):
        # 极小容量必然无可用班次
        svc_big = ResourceCalendar()
        pl = Planner(self.cat, svc_big)
        with self.assertRaises(CapacityError):
            pl.build(route="CANAL", card_version="v2", teu=500,
                     ready=READY, appointment_set="APP_TEA",
                     shipment_id="BIG")

    def test_compare_ranks_canal_faster_and_cheaper(self):
        svc = FulfillmentService()
        opts = {o.route: o for o in svc.compare(
            "CT-TEA-2610", teu=3, ready=READY)}
        self.assertLess(opts["CANAL"].delivery, opts["OLD"].delivery)
        self.assertLess(opts["CANAL"].cost_per_teu, opts["OLD"].cost_per_teu)
        # 海铁作为备选存在
        self.assertIn("RAIL_SEA", opts)


if __name__ == "__main__":
    unittest.main()
