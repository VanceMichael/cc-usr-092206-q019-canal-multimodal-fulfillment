"""排程器：把路线步骤按真实班期、船闸窗口、截关时间、港口作业能力和收货预约
链式排开，并一次性占用全部资源。

排程结果带：每步计划起止、资源占用编号、报价卡费用行、交接点、单证清单。
dry_run 只做只读推演（不落占用），供调度员比选旧线/运河/海铁方案。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import timeutil
from .calendar import (ResourceCalendar, CapacityError,
                       VOY, LOCK, TERM, CUSTOMS, APPT, CONT)
from .model import Step, FeeLine, DocState, HandOff

ROLE_LABELS = {
    "GATE_IN": "进港还箱进场",
    "TRANSSHIP": "卸船中转装船",
    "DISCHARGE": "卸船待提",
    "RAIL_LOAD": "铁路装车",
    "GAUGE_TRANSSHIP": "口岸换装作业",
    "DELIVERY": "收货方预约交付",
}
CUSTOMS_LABELS = {
    "EXPORT": "出口报关",
    "PORT_RELEASE": "出口转关放行",
    "IMPORT": "越南进口清关",
    "BORDER": "友谊关跨境申报",
}
SEARCH_DAYS = 28


@dataclass
class BuildResult:
    steps: list[Step]
    arrival: datetime          # 到达目的地节点、等待预约的时间
    delivery: datetime         # 预约交付完成时间
    base_cost: float
    requests: list[dict]       # 与 steps 对应关系由调用方按计数切分


class Planner:
    def __init__(self, cat, cal: ResourceCalendar):
        self.cat = cat
        self.cal = cal

    # ============================================================ 对外入口
    def build(self, *, route: str, card_version: str, teu: int, ready: datetime,
              appointment_set: str, shipment_id: str,
              raw_from: int = 0, cur: datetime | None = None,
              seq_from: int | None = None,
              surcharges: list[FeeLine] | None = None,
              dry_run: bool = False,
              carry_fees: bool = True,
              release_booking_ids: list[str] | None = None) -> BuildResult:
        """从路线的 raw_from 步开始排（重排/改线时只重建尾部）。

        cur: 货物在衔接节点的就绪时间；seq_from: 步骤绝对序号起点。
        carry_feus=False 时重建段不挂报价基础费（子分段前缀共用，费用已在母分段计过）。
        release_booking_ids: 旧尾部占用，与新占用做原子 swap。
        """
        route_raw = self.cat.routes[route]
        raw_steps = route_raw["steps"]
        card = self.cat.cards[(route, card_version)]
        comps: dict[str, list[dict]] = {}
        if carry_fees:
            for c in card["components"]:
                comps.setdefault(c["step_ref"], []).append(c)

        cur = ready if cur is None else cur
        seq_base = raw_from + 1 if seq_from is None else seq_from
        steps: list[Step] = []
        all_requests: list[dict] = []
        counts: list[int] = []
        arrival_ready = None

        for i in range(raw_from, len(raw_steps)):
            raw = raw_steps[i]
            seq = seq_base + (i - raw_from)
            kind = raw["t"]
            if kind == "TERMINAL":
                cur, step, reqs = self._terminal(raw, seq, teu, cur, route, shipment_id)
            elif kind == "CUSTOMS":
                cur, step, reqs = self._customs(raw, seq, teu, cur, route, shipment_id)
            elif kind == "VOYAGE":
                cur, step, reqs = self._voyage(raw, seq, teu, cur, route, shipment_id)
            elif kind == "APPOINTMENT":
                arrival_ready = cur
                cur, step, reqs = self._appointment(
                    raw, seq, teu, cur, route, shipment_id, appointment_set)
            else:
                raise ValueError(f"未知步骤类型 {kind}")
            step.raw_index = i

            for comp in comps.get(raw["ref"], []):
                step.fees.append(FeeLine(
                    key=comp["key"], label=comp["label"], amount=comp["amount"],
                    teu=teu, category="BASE"))
            steps.append(step)
            counts.append(len(reqs))
            all_requests.extend(reqs)

        if surcharges:
            steps[0].fees.extend(surcharges)

        if dry_run:
            # 只读推演：给每步挂伪编号，绝不落日历
            pseudo = 0
            for step, n in zip(steps, counts):
                step.bookings.extend(f"DRY-{shipment_id}-{step.seq}-{pseudo + k}"
                                     for k in range(n))
                pseudo += n
        elif release_booking_ids:
            ids = iter(self.cal.swap(release_booking_ids, all_requests))
            for step, n in zip(steps, counts):
                step.bookings.extend(next(ids) for _ in range(n))
        else:
            ids = iter(self.cal.hold_many(all_requests))
            for step, n in zip(steps, counts):
                step.bookings.extend(next(ids) for _ in range(n))

        delivery = steps[-1].planned_end
        arrival = arrival_ready or steps[-1].planned_end
        base_cost = round(sum(f.total for s in steps for f in s.fees), 2)
        return BuildResult(steps, arrival, delivery, base_cost, all_requests)

    # ============================================================ 港口作业
    def _terminal(self, raw, seq, teu, cur, route, shipment_id):
        term = self.cat.terminals[raw["ref"]]
        node = self.cat.node_name(raw["node"])
        reqs: list[dict] = []
        start = None
        for offset in range(SEARCH_DAYS):
            candidate = cur if offset == 0 else timeutil.at(
                (cur + timedelta(days=offset)).date(), "08:00")
            if self.cal.fits(TERM, raw["ref"], candidate, teu, term["capacity_per_day"]):
                start = candidate
                break
        if start is None:
            raise CapacityError([f"{node} {SEARCH_DAYS}天内无作业能力"])
        end = start + timedelta(hours=term["dwell_hours"])
        step = Step(
            seq=seq, type="TERMINAL", ref=raw["ref"],
            label=f"{node}{ROLE_LABELS.get(raw['role'], raw['role'])}",
            stage=raw["role"], node=raw["node"], planned_start=start, planned_end=end,
            teu=teu, route=route)
        reqs.append({"kind": TERM, "key": raw["ref"], "bucket": start, "teu": teu,
                     "capacity": term["capacity_per_day"], "shipment_id": shipment_id,
                     "step_seq": seq, "label": step.label})
        if raw["role"] == "GATE_IN":
            iso = start.isocalendar()
            week = f"{iso.year}-W{iso.week:02d}"
            cap = self.cat.containers.get((raw["node"], week))
            if cap is None:
                raise CapacityError([f"{node} 第{week}周无空箱供给计划"])
            reqs.append({"kind": CONT, "key": f"CONT:{raw['node']}", "bucket": week,
                         "teu": teu, "capacity": cap, "shipment_id": shipment_id,
                         "step_seq": seq, "label": f"{node}当周空箱"})
        if raw.get("party_from") and raw.get("party_to"):
            step.handoff = HandOff(
                seq, step.label, raw["node"], raw["party_from"], raw["party_to"], end)
        return end, step, reqs

    # ============================================================ 报关
    def _customs(self, raw, seq, teu, cur, route, shipment_id):
        cus = self.cat.customs[raw["ref"]]
        reqs: list[dict] = []
        cutoff_today = timeutil.at(cur.date(), cus["cutoff"])
        if cur <= cutoff_today and self.cal.fits(
                CUSTOMS, raw["ref"], cur, teu, cus["capacity_per_day"]):
            filed_at = cur
            release = timeutil.at(cur.date(), cus["same_day_release"])
        else:
            filed_at = release = None
            for offset in range(1, SEARCH_DAYS):
                day = (cur + timedelta(days=offset)).date()
                probe = timeutil.at(day, cus["cutoff"])
                if self.cal.fits(CUSTOMS, raw["ref"], probe, teu,
                                 cus["capacity_per_day"]):
                    filed_at = probe
                    release = timeutil.at(day + timedelta(days=1),
                                          cus["next_day_release"])
                    break
            if filed_at is None:
                raise CapacityError(
                    [f"{self.cat.node_name(raw['node'])}报关 {SEARCH_DAYS}天内无受理能力"])
        step = Step(
            seq=seq, type="CUSTOMS", ref=raw["ref"],
            label=f"{self.cat.node_name(raw['node'])}"
                  f"{CUSTOMS_LABELS.get(raw['role'], raw['role'])}",
            stage=raw["role"], node=raw["node"],
            planned_start=filed_at, planned_end=release, teu=teu, route=route)
        reqs.append({"kind": CUSTOMS, "key": raw["ref"], "bucket": filed_at,
                     "teu": teu, "capacity": cus["capacity_per_day"],
                     "shipment_id": shipment_id, "step_seq": seq, "label": step.label})
        for name in raw.get("docs", []):
            step.docs.append(DocState(name))
        return release, step, reqs

    # ============================================================ 航段（班轮/班车/班列）
    def _voyage(self, raw, seq, teu, cur, route, shipment_id):
        svc = self.cat.services[raw["ref"]]
        reqs: list[dict] = []
        not_before = cur
        cutoff = svc.get("cutoff_hours_before")
        if cutoff:
            not_before = cur + timedelta(hours=cutoff)
        departure = None
        for offset in range(SEARCH_DAYS):
            day = (not_before + timedelta(days=offset)).date()
            if day.weekday() not in svc["weekdays"]:
                continue
            for clock in sorted(self.cat.service_clocks(svc)):
                candidate = timeutil.at(day, clock)
                if candidate < not_before:
                    continue
                if self.cal.fits(VOY, raw["ref"], candidate, teu, svc["capacity_teu"]):
                    departure = candidate
                    break
            if departure:
                break
        if departure is None:
            raise CapacityError([f"{svc['name']} {SEARCH_DAYS}天内无箱位"])

        checkpoints = raw.get("checkpoints", [])
        lock_count = 0
        prev_passage_end: datetime | None = None
        wait_hours = 0.0
        for cp in checkpoints:
            nominal = departure + timedelta(hours=cp["after_hours"])
            earliest = max(nominal, prev_passage_end or nominal)
            if "lock" in cp:
                window = self._earliest_lock(cp["lock"], earliest, teu)
                wait_hours += max(0.0, (window - earliest).total_seconds() / 3600)
                lock = self.cat.locks[cp["lock"]]
                reqs.append({"kind": LOCK, "key": cp["lock"], "bucket": window,
                             "teu": teu, "capacity": lock["capacity_teu"],
                             "shipment_id": shipment_id, "step_seq": seq,
                             "label": f"{self.cat.node_name(cp['node'])}过闸"})
                lock_count += 1
                prev_passage_end = window + timedelta(hours=lock["passage_hours"])

        if "transit_hours" in svc:
            arrival_dt = departure + timedelta(hours=svc["transit_hours"])
        else:
            arrival_dt = departure + timedelta(
                hours=checkpoints[-1]["after_hours"] + wait_hours)
            if prev_passage_end and prev_passage_end > arrival_dt:
                arrival_dt = prev_passage_end

        fn = self.cat.node_name(raw["node_from"])
        tn = self.cat.node_name(raw["node_to"])
        leg = raw.get("leg")
        distance = self.cat.leg_distance(leg)
        if not leg and checkpoints:
            distance = sum(self.cat.leg_distance(cp.get("leg")) for cp in checkpoints)
        step = Step(
            seq=seq, type="VOYAGE", ref=raw["ref"],
            label=f"{svc['name']}（{fn}→{tn}）",
            stage="VOYAGE", node=raw["node_from"],
            planned_start=departure, planned_end=arrival_dt, teu=teu,
            route=route, leg_id=leg, distance_km=distance)
        # 班次占用放最前，船闸占用随后（顺序即 bookings 中的顺序约定）
        voyage_req = {"kind": VOY, "key": raw["ref"], "bucket": departure,
                      "teu": teu, "capacity": svc["capacity_teu"],
                      "shipment_id": shipment_id, "step_seq": seq,
                      "label": svc["name"]}
        reqs.insert(0, voyage_req)
        if raw.get("party_from") and raw.get("party_to"):
            step.handoff = HandOff(
                seq, step.label, raw["node_from"], raw["party_from"],
                raw["party_to"], departure)
        return arrival_dt, step, reqs

    def _earliest_lock(self, lock_id: str, earliest: datetime, teu: int,
                       exclude: set[str] | None = None) -> datetime:
        lock = self.cat.locks[lock_id]
        for offset in range(SEARCH_DAYS):
            day = (earliest + timedelta(days=offset)).date()
            for clock in sorted(lock["windows"]):
                candidate = timeutil.at(day, clock)
                if candidate < earliest:
                    continue
                if self.cal.fits(LOCK, lock_id, candidate, teu,
                                 lock["capacity_teu"], exclude=exclude):
                    return candidate
        raise CapacityError([f"{lock_id} {SEARCH_DAYS}天内无过闸窗口"])

    # ============================================================ 收货预约
    def _appointment(self, raw, seq, teu, cur, route, shipment_id, appt_set_id):
        appt = self.cat.appointment_for(appt_set_id)
        reqs: list[dict] = []
        slot = None
        for offset in range(SEARCH_DAYS):
            day = (cur + timedelta(days=offset)).date()
            if day.weekday() not in appt["weekdays"]:
                continue
            open_at = timeutil.at(day, appt["open"])
            close_at = timeutil.at(day, appt["close"])
            candidate = max(cur, open_at) if offset == 0 else open_at
            if candidate + timedelta(hours=appt["dwell_hours"]) > close_at:
                continue
            if self.cal.fits(APPT, appt_set_id, candidate, teu,
                             appt["capacity_per_day"]):
                slot = candidate
                break
        if slot is None:
            raise CapacityError([f"河内收货 {SEARCH_DAYS}天内无预约名额"])
        end = slot + timedelta(hours=appt["dwell_hours"])
        node = self.cat.node_name(raw["node"])
        step = Step(
            seq=seq, type="APPOINTMENT", ref=raw["ref"],
            label=f"{node}{ROLE_LABELS['DELIVERY']}", stage="DELIVERY",
            node=raw["node"], planned_start=slot, planned_end=end,
            teu=teu, route=route)
        step.note = f"货物{timeutil.fmt(cur)}到达目的地，预约{timeutil.fmt(slot)}交付"
        reqs.append({"kind": APPT, "key": appt_set_id, "bucket": slot, "teu": teu,
                     "capacity": appt["capacity_per_day"], "shipment_id": shipment_id,
                     "step_seq": seq, "label": step.label})
        if raw.get("party_from") and raw.get("party_to"):
            step.handoff = HandOff(
                seq, step.label, raw["node"], raw["party_from"], raw["party_to"], end)
        return end, step, reqs

    # ============================================================ 航行中延误：重订未过船闸
    def rebook_locks(self, *, step: Step, route_id: str, shipment_id: str,
                     first_cp_index: int, delay_hours: float) -> dict:
        """船舶在运河航段中延误 delay_hours，重订 first_cp_index 起尚未通过的
        船闸窗口（原子 swap），顺延抵港时间。返回新增等待小时数与新窗口。"""
        raw = next(s for s in self.cat.routes[route_id]["steps"]
                   if s["ref"] == step.ref and "checkpoints" in s)
        cps = raw["checkpoints"]
        departure = step.planned_start

        # bookings[0]=班次，其后按 checkpoint 顺序是各船闸占用
        lock_bids: dict[int, str] = {}
        bi = 1
        for idx, cp in enumerate(cps):
            if "lock" in cp:
                lock_bids[idx] = step.bookings[bi]
                bi += 1
        own = set(step.bookings)

        shift = timedelta(hours=delay_hours)
        prev_end: datetime | None = None
        new_reqs: list[dict] = []
        windows: dict[int, datetime] = {}
        for idx in range(first_cp_index, len(cps)):
            cp = cps[idx]
            if "lock" not in cp:
                continue
            nominal = departure + timedelta(hours=cp["after_hours"]) + shift
            earliest = max(nominal, prev_end or nominal)
            window = self._earliest_lock(cp["lock"], earliest, step.teu, exclude=own)
            lock = self.cat.locks[cp["lock"]]
            new_reqs.append({"kind": LOCK, "key": cp["lock"], "bucket": window,
                             "teu": step.teu, "capacity": lock["capacity_teu"],
                             "shipment_id": shipment_id, "step_seq": step.seq,
                             "label": f"{self.cat.node_name(cp['node'])}过闸(延误重订)"})
            windows[idx] = window
            prev_end = window + timedelta(hours=lock["passage_hours"])

        to_release = [lock_bids[idx] for idx in windows]
        new_ids = self.cal.swap(to_release, new_reqs) if new_reqs else []
        id_map = {req["key"]: bid for req, bid in zip(new_reqs, new_ids)}

        rebuilt = [step.bookings[0]]
        new_window_by_lock: dict[str, datetime] = {}
        for idx, cp in enumerate(cps):
            if "lock" not in cp:
                continue
            if idx in windows:
                rebuilt.append(id_map[cp["lock"]])
                new_window_by_lock[cp["lock"]] = windows[idx]
            else:
                rebuilt.append(lock_bids[idx])
        step.bookings = rebuilt

        # 新抵港时间：末段名义到达+初始延误，再取不早于末闸通过时刻
        base_arrival = departure + timedelta(
            hours=cps[-1]["after_hours"] + delay_hours)
        new_arrival = max(base_arrival, prev_end or base_arrival)
        added = timeutil.hours_between(step.planned_end, new_arrival)
        step.planned_end = new_arrival
        step.note = (f"航行延误{delay_hours:g}h，重订{len(windows)}个船闸窗口，"
                     f"抵港顺延{added:g}h")
        return {"added_hours": added, "windows": new_window_by_lock,
                "new_arrival": new_arrival}
