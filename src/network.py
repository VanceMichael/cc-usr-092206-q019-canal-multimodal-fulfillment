"""把 fixtures/network.json 装载成可用的网络对象与资源台账。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .models import Actor, LegDef, Mode, Quote, Route, SlotBook, Window
from .resources import ResourceLedger


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Network:
    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.as_of = _dt(raw["as_of"])
        self.locations: dict[str, object] = {}
        for loc in raw["locations"]:
            self.locations[loc["code"]] = loc
        self.actors = {
            "shipper": Actor("shipper", "梧州六堡茶出口公司", "shipper"),
            "canal": Actor("canal", "平陆运河调度中心", "canal"),
            "carrier": Actor("carrier", "广西北部湾集运", "carrier"),
            "qz_port": Actor("qz_port", "钦州港务集团", "port"),
            "customs": Actor("customs", "钦州海关", "customs"),
            "railway": Actor("railway", "国铁南宁局", "railway"),
            "consignee": Actor("consignee", "河内明宇茶饮连锁", "receiver"),
        }

        self.ledger = ResourceLedger()
        self.books: dict[str, SlotBook] = {}
        self.windows: dict[str, Window] = {}
        for b in raw["resources"]["books"]:
            book = SlotBook(
                code=b["code"], series=b["series"], kind=b["kind"], mode=Mode(b["mode"]),
                service=b["service"], departure_origin=b["origin"], arrival_dest=b["dest"],
                depart_at=_dt(b["depart"]), arrive_at=_dt(b["arrive"]),
                capacity_teu=float(b["capacity_teu"]), cutoff_hours=float(b.get("cutoff_hours", 0)),
            )
            self.books[book.code] = book
            self.ledger.register_book(book)
        for w in raw["resources"]["windows"]:
            win = Window(
                code=w["code"], series=w.get("series", w["code"]), kind=w["kind"], name=w["name"],
                opens=_dt(w["opens"]), closes=_dt(w["closes"]),
                capacity_per_window=float(w["capacity_per_window"]),
                offset_hours=float(w.get("offset_hours", 0)),
            )
            self.windows[win.code] = win
            self.ledger.register_window(win)

        self.routes: dict[str, Route] = {}
        for r in raw["routes"]:
            legs = tuple(
                LegDef(
                    seq=l["seq"], mode=Mode(l["mode"]), origin=l["origin"], dest=l["dest"],
                    distance_km=l["distance_km"], line_name=l["line_name"],
                    cost_per_teu=float(l["cost_per_teu"]),
                    duration_hint=timedelta(hours=float(l["duration_hours"])),
                    resource_kind=l["resource_kind"], resource_code=l.get("resource_code"),
                    leg_key=l.get("key", ""),
                    extra_resources=tuple(l.get("extra_resources", [])),
                    end_resources=tuple(l.get("end_resources", [])),
                    end_proc_hours=float(l.get("end_proc_hours", 0.0)),
                    note=l.get("note", ""),
                )
                for l in r["legs"]
            )
            self.routes[r["code"]] = Route(
                code=r["code"], name=r["name"], distance_km=r["distance_km"],
                legs=legs, quote_ids=tuple(r["quote_ids"]),
            )

        self.quotes: dict[str, Quote] = {}
        for q in raw["quotes"]:
            self.quotes[q["quote_id"]] = Quote(
                quote_id=q["quote_id"], route_code=q["route_code"], valid_from=q["valid_from"],
                per_teu_total=float(q["per_teu_total"]),
                per_leg={k: float(v) for k, v in q["per_leg"].items()},
                note=q.get("note", ""),
            )

        self.contracts_raw = {c["contract_no"]: c for c in raw["contracts"]}

        self._validate()

        # 背景货量（其他客户已占舱位/窗口）
        for bg in raw["resources"].get("background", []):
            self.ledger.reserve(
                bg["resource"], float(bg["teu"]),
                res_id=f"BG:{bg['resource']}", split_id="BACKGROUND", leg_id="-", reason="background",
            )

    def _validate(self) -> None:
        """资料一致性校验：报价与航段对齐、资源引用可解析、背景货量不超容量。"""
        errors: list[str] = []
        series_books = {b.series for b in self.books.values()}
        series_wins = {w.series for w in self.windows.values()}
        book_codes = set(self.books)
        win_codes = set(self.windows)
        for code, rt in self.routes.items():
            for qid in rt.quote_ids:
                if qid not in self.quotes:
                    errors.append(f"路线 {code} 引用了不存在的报价 {qid}")
                    continue
                q = self.quotes[qid]
                leg_keys = {ld.leg_key for ld in rt.legs}
                missing = leg_keys - set(q.per_leg)
                extra = set(q.per_leg) - leg_keys
                if missing:
                    errors.append(f"报价 {qid} 缺少航段单价: {sorted(missing)}")
                if extra:
                    errors.append(f"报价 {qid} 含路线 {code} 不存在的航段: {sorted(extra)}")
                if not missing and abs(sum(q.per_leg.values()) - q.per_teu_total) > 1e-6:
                    errors.append(
                        f"报价 {qid} 分段合计 {sum(q.per_leg.values())} ≠ 总价 {q.per_teu_total}")
                for ld in rt.legs:
                    refs = [ld.resource_code, *ld.extra_resources, *ld.end_resources]
                    for ref in refs:
                        if not ref:
                            continue
                        if ld.resource_kind in ("slot_ship", "slot_rail"):
                            if ref not in series_books:
                                errors.append(f"路线 {code} 航段 {ld.seq} 引用未知船期系列 {ref}")
                        elif ref not in series_wins:
                            errors.append(f"路线 {code} 航段 {ld.seq} 引用未知窗口系列 {ref}")
        for c in self.contracts_raw.values():
            if c["route_code"] not in self.routes:
                errors.append(f"合同 {c['contract_no']} 引用未知路线 {c['route_code']}")
            if c["quote_id"] not in self.quotes:
                errors.append(f"合同 {c['contract_no']} 引用未知报价 {c['quote_id']}")
        for bg in self.raw["resources"].get("background", []):
            if bg["resource"] not in book_codes and bg["resource"] not in win_codes:
                errors.append(f"背景货量引用未知资源 {bg['resource']}")
                continue
            cap = self.books.get(bg["resource"]) or self.windows.get(bg["resource"])
            cap_value = getattr(cap, "capacity_teu", None) or cap.capacity_per_window
            if float(bg["teu"]) > cap_value:
                errors.append(f"背景货量本身已超资源容量: {bg['resource']}")
        if errors:
            raise ValueError("网络资料校验失败：\n- " + "\n- ".join(errors))

    # -- 查询 ------------------------------------------------------------
    def route(self, code: str) -> Route:
        return self.routes[code]

    def quote(self, code: str) -> Quote:
        return self.quotes[code]

    def contract_raw(self, contract_no: str) -> dict:
        return self.contracts_raw[contract_no]

    def books_in_series(self, series: str, ready_by: datetime | None = None) -> list[SlotBook]:
        out = [b for b in self.books.values() if b.series == series]
        if ready_by is not None:
            out = [b for b in out if b.depart_at >= ready_by]
        return sorted(out, key=lambda b: b.depart_at)

    def windows_in_series(self, series: str, ready_by: datetime | None = None) -> list[Window]:
        out = [w for w in self.windows.values() if w.series == series]
        if ready_by is not None:
            out = [w for w in out if w.closes >= ready_by]
        return sorted(out, key=lambda w: w.opens)

    def quote_history(self, route_code: str) -> list[Quote]:
        return [self.quotes[qid] for qid in self.routes[route_code].quote_ids]


def load_network(path: str | Path = "fixtures/network.json") -> Network:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Network(raw)
