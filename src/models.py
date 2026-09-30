"""平陆运河多式联运履约系统的领域模型。

设计要点：
- 同一票货拆成若干 *批次(Split)*，每个批次沿一条路线的航段独立排程、独立执行；
- 每个航段的资源占用是带容量约束的 *预定(Reservation)*，释放后立即可被再占用，
  且同一次释放不会把容量重复还回；
- *承诺快照(CommitmentSnapshot)* 在每次下达/重排时冻结当时的 ETA 与费用结构，
  旧承诺永久保留、可追溯，最新承诺才代表当前口径。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------- 枚举

class Mode(str, Enum):
    TRUCK = "truck"            # 公路（集卡短驳 / 旧线绕行长距离陆运）
    CANAL = "canal"            # 平陆运河内河航段
    RAIL = "rail"              # 海铁联运铁路班列
    SEA = "sea"                # 沿海/近洋海运
    CUSTOMS = "customs"        # 报关（不产生位移，但是排程节点）
    PORT = "port"              # 港口装卸作业
    BORDER = "border"          # 友谊关跨境查验


class SplitState(str, Enum):
    PLANNED = "planned"        # 已排程未执行
    IN_TRANSIT = "in_transit"  # 首航段已离站
    DELIVERED = "delivered"    # 全部航段完成
    CANCELLED = "cancelled"    # 退关（已申报后取消）
    ROLLED = "rolled"          # 甩箱后待重排（无活动计划）


class LegState(str, Enum):
    PLANNED = "planned"
    DEPARTED = "departed"      # 已发运（该航段不可再改）
    ARRIVED = "arrived"        # 已到达，待交接
    DONE = "done"              # 到达 + 交接完成
    SKIPPED = "skipped"        # 改港/切换路线后被作废的未执行航段


class EventType(str, Enum):
    BOOKED = "booked"                # 首次下达排程
    REPLANNED = "replanned"          # 异常后只重排未执行部分
    QUANTITY_CHANGED = "qty_changed" # 客户改量
    DEPARTURE = "departure"
    ARRIVAL = "arrival"
    HANDOVER = "handover"            # 单证/箱货交接确认
    DELAY = "delay"
    ROLL = "roll"                    # 甩箱
    CUSTOMS_WITHDRAWN = "customs_withdrawn"  # 退关
    REROUTE = "reroute"              # 改港 / 切换海铁
    DELIVERY = "delivery"
    SETTLED = "settled"


# --------------------------------------------------------------------------- 静态资料

@dataclass(frozen=True)
class Actor:
    code: str
    name: str
    role: str            # shipper / canal / carrier / port / customs / receiver / railway


@dataclass(frozen=True)
class Location:
    code: str
    name: str
    kind: str            # depot / port / lock / station / border / icd / consignee


@dataclass(frozen=True)
class LegDef:
    """路线模板中的一个航段（不绑定具体船期/窗口）。"""
    seq: int
    mode: Mode
    origin: str
    dest: str
    distance_km: int
    line_name: str
    cost_per_teu: float
    duration_hint: timedelta          # 资源顺畅时的纯运行时长
    resource_kind: str                # slot_book / slot_ship / lock / port / customs / rail / border / appointment / none
    resource_code: Optional[str]      # 对应资源 *系列* 代码
    leg_key: str = ""                 # 报价 per_leg 的键
    extra_resources: tuple[str, ...] = ()  # 运河航段串联的其余船闸系列
    end_resources: tuple[str, ...] = ()    # 到达后必须排上的目的港泊位系列
    end_proc_hours: float = 0.0            # 靠泊作业时长
    note: str = ""


@dataclass(frozen=True)
class Route:
    code: str
    name: str
    distance_km: int
    legs: tuple[LegDef, ...]
    quote_ids: tuple[str, ...]        # 历次报价（旧→新）

@dataclass(frozen=True)
class Quote:
    quote_id: str
    route_code: str
    valid_from: str
    per_teu_total: float
    per_leg: dict[str, float]         # leg key -> 单价
    note: str = ""


@dataclass(frozen=True)
class SlotBook:
    """班轮 / 班列航次的箱位池。"""
    code: str
    series: str             # 同航线多个航次归一系列，如 SLOT-SEA-QH
    kind: str               # slot_ship / slot_rail
    mode: Mode
    service: str
    departure_origin: str
    arrival_dest: str
    depart_at: datetime
    arrive_at: datetime
    capacity_teu: float
    cutoff_hours: float = 0.0   # 截单/截关提前量


@dataclass(frozen=True)
class Window:
    """船闸 / 港口泊位 / 报关 / 铁路衔接 / 跨境查验 / 收货预约等时间窗容量池。"""
    code: str
    series: str          # 同设施多日窗归一系列，如 LOCK-MD
    kind: str            # lock / port / customs / rail / border / appointment
    name: str
    opens: datetime
    closes: datetime
    capacity_per_window: float
    # 航段经过该窗口时相对航段开始时间的偏移
    offset_hours: float = 0.0


# --------------------------------------------------------------------------- 合同

@dataclass
class Contract:
    contract_no: str
    shipper: str
    consignee: str
    cargo: str
    incoterm: str
    total_teu: float
    route_code: str
    quote_id: str
    latest_ship_date: datetime
    receive_window_start: datetime
    receive_window_end: datetime
    unit_price: float                 # 货值/TEU，供延期违约说明用（可选）


# --------------------------------------------------------------------------- 运行态

@dataclass
class Reservation:
    """一段航段对某个资源池在某时间桶内的箱量占用。"""
    res_id: str
    resource_code: str
    resource_kind: str
    bucket: object                    # SlotBook.code 或时间桶起点
    teu: float
    active: bool = True
    released_by_event: Optional[str] = None
    reason: str = ""


@dataclass
class ScheduledLeg:
    leg_id: str
    split_id: str
    seq: int
    mode: Mode
    origin: str
    dest: str
    distance_km: int
    line_name: str
    planned_depart: datetime
    planned_arrive: datetime
    planned_cost: float
    resource_code: Optional[str]
    resource_kind: str
    reservations: list[Reservation] = field(default_factory=list)
    state: LegState = LegState.PLANNED
    replaced: bool = False          # 退关重报后，旧的已闭环报关航段留痕但退出活动计划
    actual_depart: Optional[datetime] = None
    actual_arrive: Optional[datetime] = None
    actual_cost: Optional[float] = None
    handover_done: bool = False
    handover_docs: list[str] = field(default_factory=list)
    handover_at: Optional[datetime] = None
    handover_party: Optional[str] = None
    note: str = ""

    @property
    def executed(self) -> bool:
        return self.state in (LegState.DEPARTED, LegState.ARRIVED, LegState.DONE)

    @property
    def locked(self) -> bool:
        """已发运航段在任何重排中都不可更改。"""
        return self.state in (LegState.DEPARTED, LegState.ARRIVED, LegState.DONE)

    @property
    def cost(self) -> float:
        return self.actual_cost if self.actual_cost is not None else self.planned_cost


@dataclass
class CommitmentSnapshot:
    """某一时刻给货主的口径冻结：到达时间、费用结构、适用航段集合。"""
    snap_no: int
    at: datetime
    reason: str
    split_id: str
    legs: tuple[str, ...]                       # 当时活动计划的 leg_id
    eta: datetime
    cost_total: float
    per_leg: dict[str, float]
    event_ref: Optional[str] = None
    superseded: bool = False


@dataclass
class Split:
    split_id: str
    contract_no: str
    teu: float
    route_code: str
    quote_id: str
    legs: list[ScheduledLeg] = field(default_factory=list)
    state: SplitState = SplitState.PLANNED
    snapshots: list[CommitmentSnapshot] = field(default_factory=list)
    delivered_at: Optional[datetime] = None
    last_blacklist: tuple[str, ...] = ()
    plan_counter: int = 1
    counterfactuals: dict[str, dict] = field(default_factory=dict)  # 订舱时各路线影子方案

    @property
    def active_legs(self) -> list[ScheduledLeg]:
        return [l for l in self.legs
                if l.state != LegState.SKIPPED and not l.replaced]

    def leg(self, leg_id: str) -> ScheduledLeg:
        for l in self.legs:
            if l.leg_id == leg_id:
                return l
        raise KeyError(leg_id)

    def latest_snapshot(self) -> Optional[CommitmentSnapshot]:
        live = [s for s in self.snapshots if not s.superseded]
        return live[-1] if live else None

    @property
    def eta(self) -> Optional[datetime]:
        snap = self.latest_snapshot()
        return snap.eta if snap else None

    @property
    def planned_cost(self) -> float:
        snap = self.latest_snapshot()
        if snap:
            return snap.cost_total
        return sum(l.planned_cost for l in self.active_legs)


@dataclass
class Event:
    event_id: str
    at: datetime
    type: EventType
    split_id: Optional[str]
    leg_id: Optional[str]
    message: str
    payload: dict = field(default_factory=dict)


@dataclass
class CustomsDeclaration:
    decl_id: str
    split_id: str
    leg_id: str
    customs_code: str
    teu: float
    declared_at: datetime
    withdrawn: bool = False
    withdrawn_at: Optional[datetime] = None
    abandoned: bool = False              # 未放行即随航段重排而作废（新单重建）
    cleared: bool = False
    cleared_at: Optional[datetime] = None


@dataclass
class DestinationAppointment:
    appt_id: str
    split_id: str
    window_code: str
    teu: float
    slot_start: datetime
    slot_end: datetime
    kept: bool = False
    checked_in_at: Optional[datetime] = None
    cancelled: bool = False


@dataclass
class Shipment:
    """一票货（合同）下的全部批次与执行记录。"""
    contract: Contract
    splits: dict[str, Split] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    declarations: dict[str, CustomsDeclaration] = field(default_factory=dict)
    appointments: dict[str, DestinationAppointment] = field(default_factory=dict)
    baselines: dict[str, dict] = field(default_factory=dict)   # 订舱时各替代路线影子方案
    extra_charges: list[dict] = field(default_factory=list)   # 亏舱/改配/退关等费用行
    cancelled_teu: float = 0.0                                # 客户改量减掉的箱量
    settled: bool = False

    # -- 批次级 ----------------------------------------------------------
    def delivered_splits(self) -> list[Split]:
        return [s for s in self.splits.values() if s.state == SplitState.DELIVERED]

    def active_splits(self) -> list[Split]:
        return [s for s in self.splits.values()
                if s.state in (SplitState.PLANNED, SplitState.IN_TRANSIT, SplitState.ROLLED)]

    def all_legs(self) -> list[ScheduledLeg]:
        return [l for s in self.splits.values() for l in s.legs]

    # -- 货主级汇总口径 --------------------------------------------------
    def delivery_view(self) -> dict:
        """货主看到的一致口径：逐箱量的到达承诺与费用变化。"""
        rows = []
        for s in self.splits.values():
            latest = s.latest_snapshot()
            rows.append({
                "split_id": s.split_id,
                "teu": s.teu,
                "state": s.state.value,
                "eta": s.eta,
                "delivered_at": s.delivered_at,
                "committed_cost": latest.cost_total if latest else None,
                "snapshots": len(s.snapshots),
            })
        total_committed = sum(r["committed_cost"] or 0.0 for r in rows)
        delivered_teu = sum(r["teu"] for r in rows if r["state"] == "delivered")
        open_teu = sum(r["teu"] for r in rows if r["state"] in ("planned", "in_transit", "rolled"))
        return {
            "contract_no": self.contract.contract_no,
            "rows": rows,
            "total_teu": self.contract.total_teu,
            "delivered_teu": delivered_teu,
            "open_teu": open_teu,
            "committed_cost_total": total_committed,
        }
