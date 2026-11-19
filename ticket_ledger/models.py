"""数据模型。所有金额均为币种最小单位（分）的整数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Optional

REFUND_EVENTS = frozenset({"部分退款", "全额退款"})
USE_EVENTS = frozenset({"核销", "入馆"})


@dataclass(frozen=True)
class Order:
    order_id: str
    channel: str
    product: str
    sold_at: date
    currency: str
    gross_minor: int


@dataclass(frozen=True)
class TicketEvent:
    event_id: str
    order_id: str
    old_ticket: str
    new_ticket: str
    event: str
    event_date: date
    amount_minor: int
    venue: str
    # 源文件行号，用于稳定排序与报错定位。
    sequence: int = 0
    # 可选的记录时间：缺省时历史依据只能按事件日期复核。
    recorded_at: Optional[date] = None

    @property
    def is_use(self) -> bool:
        return self.event in USE_EVENTS

    @property
    def is_refund(self) -> bool:
        return self.event in REFUND_EVENTS


@dataclass(frozen=True)
class Agreement:
    agreement_id: str
    venue: str
    product: str
    channel: str
    start_date: date
    end_date: date
    basis_points: int
    cap_minor: int
    # 可选的协议结算时点：核销后多少天进入应付。缺失时以财务关账为准。
    settlement_lag_days: Optional[int] = None

    def settlement_due(self, on: date) -> Optional[date]:
        if self.settlement_lag_days is None:
            return None
        return on + timedelta(days=self.settlement_lag_days)

    def covers(self, *, venue: str, product: str, channel: str, on: date) -> bool:
        # 协议区间按 [start, end) 解释：后一份协议从起始日起接管。
        return (
            self.venue == venue
            and self.product == product
            and self.channel in {"*", channel}
            and self.start_date <= on < self.end_date
        )

    @property
    def specificity(self) -> int:
        return 0 if self.channel == "*" else 1


@dataclass(frozen=True)
class Close:
    period: str
    closed_at: date
    order_id: str
    amount_minor: int
    status: str
    sequence: int = 0
    # 可选：财务据以确认的依据时点。缺省等于 closed_at，保证对方可按当时
    # 依据复核；即使后来补录了新记录，也不会改变已确认明细的摊分。
    basis_as_of: Optional[date] = None

    @property
    def basis_date(self) -> date:
        return self.basis_as_of or self.closed_at


@dataclass
class Dataset:
    orders: dict[str, Order] = field(default_factory=dict)
    events: list[TicketEvent] = field(default_factory=list)
    agreements: list[Agreement] = field(default_factory=list)
    closes: list[Close] = field(default_factory=list)

    def events_for(self, order_id: str) -> list[TicketEvent]:
        return [event for event in self.events if event.order_id == order_id]

    def agreements_for(self, order: Order, venue: Optional[str] = None) -> list[Agreement]:
        return [
            agreement
            for agreement in self.agreements
            if agreement.product == order.product
            and agreement.channel in {"*", order.channel}
            and (venue is None or agreement.venue == venue)
        ]

    def venues_for(self, order_id: str) -> list[str]:
        found: Iterable[str] = (
            event.venue for event in self.events_for(order_id) if event.venue
        )
        return sorted(set(found))

    def currencies(self) -> list[str]:
        return sorted({order.currency for order in self.orders.values()})
