"""履约领域模型：合同、分段、计划版本、航段作业、交接、单证、异常。

设计要点：
- 同一合同拆为若干 *分段(Shipment)* 独立排程，允许分批交货；甩箱/退关/改量再拆子分段。
- 每个分段有不可变的 *计划版本(PlanVersion)*；重排只复制已执行航段并重建未执行航段，
  旧版本完整保留，原承诺（到达时间/费用）随时可追溯。
- 每个航段对应一次 *交接(HandOff)* 与一组资源占用、费用行、单证要求。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

from . import timeutil


# 航段状态
PLANNED = "PLANNED"          # 已排程、资源已占用
EXECUTED = "EXECUTED"        # 已实际完成（含实际时间）
CANCELLED = "CANCELLED"      # 重排后作废（仅存在于被取代的历史版本中）

# 分段状态
ST_PLANNED = "PLANNED"
ST_RUNNING = "IN_EXECUTION"
ST_DONE = "DELIVERED"
ST_CANCELLED = "CANCELLED"


@dataclass
class FeeLine:
    key: str
    label: str
    amount: float                 # 单价（元/TEU 或 元/TEU/天）
    teu: int
    qty: float = 1                # 计费数量（天数等）
    category: str = "BASE"        # BASE 报价基础费用 / SURCHARGE 异常附加费
    reason: str = ""              # 附加费归因（异常编号）

    @property
    def total(self) -> float:
        return round(self.amount * self.teu * self.qty, 2)


@dataclass
class DocState:
    name: str
    required: bool = True
    submitted: bool = False
    submitted_at: datetime | None = None


@dataclass
class HandOff:
    """一次货权/责任交接：from_party -> to_party。"""
    seq: int
    stage: str
    node: str
    from_party: str
    to_party: str
    planned_at: datetime
    actual_at: datetime | None = None

    @property
    def completed(self) -> bool:
        return self.actual_at is not None


@dataclass
class Step:
    """计划中的一个航段/节点作业。"""
    seq: int
    type: str                     # TERMINAL / CUSTOMS / VOYAGE / APPOINTMENT
    ref: str
    label: str
    stage: str
    node: str
    planned_start: datetime
    planned_end: datetime
    teu: int
    route: str
    raw_index: int = 0
    leg_id: str | None = None
    distance_km: int = 0
    status: str = PLANNED
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    bookings: list[str] = field(default_factory=list)   # 未执行资源占用编号
    consumed_bookings: list[str] = field(default_factory=list)  # 已核销占用（留痕）
    fees: list[FeeLine] = field(default_factory=list)
    handoff: HandOff | None = None
    docs: list[DocState] = field(default_factory=list)
    note: str = ""

    @property
    def executed(self) -> bool:
        return self.status == EXECUTED

    @property
    def fee_total(self) -> float:
        return round(sum(f.total for f in self.fees), 2)


@dataclass
class ExceptionEvent:
    id: str
    shipment_id: str
    kind: str                     # DELAY / ROLL / REROUTE / WITHDRAW / QUANTITY
    reason: str
    at: datetime
    from_version: int
    to_version: int
    impact_hours: float = 0
    affected_teu: int = 0
    surcharge_lines: list[FeeLine] = field(default_factory=list)
    detail: str = ""


@dataclass
class PlanVersion:
    version: int
    route: str
    route_name: str
    card_version: str
    card_name: str
    created_at: datetime
    reason: str
    exception_id: str | None
    steps: list[Step]
    committed_eta: datetime
    committed_cost: float
    state: str = "ACTIVE"         # ACTIVE / SUPERSEDED
    parent_version: int | None = None

    @property
    def active(self) -> bool:
        return self.state == "ACTIVE"

    def step_after(self, seq: int) -> Step | None:
        pending = [s for s in self.steps if s.seq > seq and s.status == PLANNED]
        return pending[0] if pending else None


@dataclass
class Shipment:
    id: str
    contract_id: str
    teu: int
    original_teu: int
    ready: datetime
    appointment_set: str
    commodity: str
    parent_id: str | None = None
    versions: list[PlanVersion] = field(default_factory=list)
    exceptions: list[ExceptionEvent] = field(default_factory=list)
    status: str = ST_PLANNED
    delivered_at: datetime | None = None

    @property
    def current(self) -> PlanVersion:
        return self.versions[-1]

    def version(self, n: int) -> PlanVersion:
        return next(v for v in self.versions if v.version == n)

    @property
    def original_promise(self) -> PlanVersion:
        """最初承诺：v1，永不变更。"""
        return self.versions[0]

    @property
    def eta(self) -> datetime:
        return self.current.committed_eta

    @property
    def cost(self) -> float:
        return self.current.committed_cost

    def executed_steps(self) -> list[Step]:
        return [s for s in self.current.steps if s.status == EXECUTED]


@dataclass
class Contract:
    raw: dict[str, Any]
    shipments: list[Shipment] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.raw["id"]

    @property
    def total_teu(self) -> int:
        return self.raw["total_teu"]


# ---------------------------------------------------------------- 序列化

def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"不可序列化: {type(obj)}")


def to_dict(obj: Any) -> Any:
    if isinstance(obj, list):
        return [to_dict(x) for x in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    return obj
