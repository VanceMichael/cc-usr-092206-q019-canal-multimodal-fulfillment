"""读取网络、费率、合同资料并提供索引。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def network(path: str | None = None) -> dict:
    return _load(Path(path) if path else FIXTURES / "network.json")


@lru_cache(maxsize=None)
def rates(path: str | None = None) -> dict:
    return _load(Path(path) if path else FIXTURES / "rates.json")


@lru_cache(maxsize=None)
def contracts(path: str | None = None) -> dict:
    return _load(Path(path) if path else FIXTURES / "contracts.json")


def reset_cache() -> None:
    network.cache_clear()
    rates.cache_clear()
    contracts.cache_clear()


class Catalog:
    """资料索引视图。"""

    def __init__(self, net: dict | None = None, rate_data: dict | None = None):
        self.net = net or network()
        self.rates = rate_data or rates()
        self.nodes = {n["id"]: n for n in self.net["nodes"]}
        self.routes = {r["id"]: r for r in self.net["routes"]}
        self.legs = {l["id"]: l for l in self.net["legs"]}
        self.services = {v["id"]: v for v in self.net["vessel_services"]}
        self.locks = {l["id"]: l for l in self.net["locks"]}
        self.terminals = {t["id"]: t for t in self.net["terminals"]}
        self.customs = {c["id"]: c for c in self.net["customs"]}
        self.appointments = {a["id"]: a for a in self.net["appointments"]}
        self.containers = {(c["node"], c["week"]): c["capacity_teu"]
                           for c in self.net.get("container_supply", [])}
        self.party_name = self.net["parties"]
        self.cards: dict[tuple[str, str], dict] = {
            (c["route"], c["version"]): c for c in self.rates["rate_cards"]
        }
        self.surcharges = {s["key"]: s for s in self.rates["surcharges"]}
        self.quotations = {q["id"]: q for q in self.rates["quotations"]}

    # -- 路线/航段 --
    def route_steps(self, route_id: str) -> list[dict]:
        return self.routes[route_id]["steps"]

    def leg_distance(self, leg_id: str | None) -> int:
        return self.legs[leg_id]["distance_km"] if leg_id else 0

    def node_name(self, node_id: str) -> str:
        return self.nodes[node_id]["name"]

    def party(self, key: str) -> str:
        return self.party_name.get(key, key)

    # -- 班期 --
    def service_clocks(self, svc: dict) -> list[str]:
        return svc.get("times") or [svc["time"]]

    # -- 费率卡 --
    def card_for_quotation(self, quotation_id: str) -> tuple[str, str, dict, dict]:
        q = self.quotations[quotation_id]
        card = self.cards[(q["route"], q["card_version"])]
        return q["route"], q["card_version"], q, card

    def card_components(self, route_id: str, version: str) -> dict[str, dict]:
        card = self.cards[(route_id, version)]
        return {c["key"]: c for c in card["components"]}

    def surcharge(self, key: str) -> dict:
        return self.surcharges[key]

    def appointment_for(self, appt_id: str) -> dict:
        return self.appointments[appt_id]
