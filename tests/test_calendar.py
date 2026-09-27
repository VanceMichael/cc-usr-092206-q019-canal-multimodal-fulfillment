"""资源日历测试：容量、原子批量、释放防重、核销、重排 swap。"""
import unittest

from src.calendar import (ResourceCalendar, CapacityError, BookingError,
                          VOY, LOCK, TERM)
from src import timeutil as T


def req(kind, key, when, teu, cap, ship="S", seq=1, label="x"):
    return {"kind": kind, "key": key, "bucket": when, "teu": teu,
            "capacity": cap, "shipment_id": ship, "step_seq": seq, "label": label}


class CalendarTest(unittest.TestCase):
    def setUp(self):
        self.cal = ResourceCalendar()
        self.when = T.parse("2026-10-10T06:00")

    def test_hold_within_capacity(self):
        ids = self.cal.hold_many([req(VOY, "V", self.when, 90, 90)])
        self.assertEqual(len(ids), 1)

    def test_hold_over_capacity_rejected(self):
        with self.assertRaises(CapacityError):
            self.cal.hold_many([req(VOY, "V", self.when, 91, 90)])

    def test_cumulative_capacity(self):
        self.cal.hold_many([req(VOY, "V", self.when, 60, 90)])
        self.cal.hold_many([req(VOY, "V", self.when, 30, 90, ship="S2")])
        with self.assertRaises(CapacityError):
            self.cal.hold_many([req(VOY, "V", self.when, 1, 90, ship="S3")])

    def test_batch_atomic_on_partial_failure(self):
        # 第一笔合法、第二笔超容：整批失败，第一笔也不得落库
        with self.assertRaises(CapacityError):
            self.cal.hold_many([
                req(VOY, "V", self.when, 50, 90),
                req(TERM, "T", self.when, 999, 10)])
        self.assertEqual(self.cal.utilization(), [])

    def test_release_then_capacity_returns(self):
        ids = self.cal.hold_many([req(VOY, "V", self.when, 90, 90)])
        self.cal.release(ids)
        # 释放后同桶可被他人整舱占用
        self.cal.hold_many([req(VOY, "V", self.when, 90, 90, ship="OTHER")])

    def test_double_release_rejected(self):
        ids = self.cal.hold_many([req(VOY, "V", self.when, 10, 90)])
        self.cal.release(ids)
        with self.assertRaises(BookingError):
            self.cal.release(ids)

    def test_consume_keeps_capacity_but_blocks_release(self):
        ids = self.cal.hold_many([req(VOY, "V", self.when, 90, 90)])
        self.cal.consume(ids)
        row = self.cal.utilization()[0]
        self.assertEqual(row["used_teu"], 90)          # 容量仍计
        with self.assertRaises(BookingError):
            self.cal.release(ids)                       # 已核销不可释放
        with self.assertRaises(CapacityError):
            self.cal.hold_many([req(VOY, "V", self.when, 1, 90, ship="X")])  # 容量仍计

    def test_swap_rebooks_same_bucket_without_double_count(self):
        ids = self.cal.hold_many([req(VOY, "V", self.when, 80, 90)])
        # 在同一桶把自己的 80 改成 90：不应把原 80 算作占用方
        new = self.cal.swap(ids, [req(VOY, "V", self.when, 90, 90)])
        self.assertEqual(len(new), 1)
        self.assertEqual(self.cal.utilization()[0]["used_teu"], 90)
        # 旧编号已失效
        with self.assertRaises(BookingError):
            self.cal.release(ids)

    def test_swap_atomic_when_new_over_capacity(self):
        ids = self.cal.hold_many([req(VOY, "V", self.when, 80, 90)])
        other_when = T.parse("2026-10-11T06:00")
        with self.assertRaises(CapacityError):
            self.cal.swap(ids, [
                req(VOY, "V", other_when, 80, 90),
                req(LOCK, "L", other_when, 999, 80)])  # 第二笔超容
        # 旧占用必须原样保留
        self.assertEqual(self.cal.utilization()[0]["used_teu"], 80)
        self.assertTrue(self.cal.holds_of("S"))

    def test_different_buckets_independent(self):
        d1 = self.cal.hold_many([req(LOCK, "L", T.parse("2026-10-10T02:10"), 80, 80)])
        d2 = self.cal.hold_many([req(LOCK, "L", T.parse("2026-10-10T10:10"), 80, 80)])
        self.assertEqual(len(self.cal.utilization()), 2)
        self.cal.release(d1)
        self.assertEqual(len(self.cal.utilization()), 1)


if __name__ == "__main__":
    unittest.main()
