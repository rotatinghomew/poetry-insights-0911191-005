"""CSV 装载与结构性校验。

除必需列外，``ticket_events.csv`` 可选带 ``recorded_at`` 列，用于说明
补录事件是何时进入台账的；缺少该列时，关账依据只能按事件日期复核。
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from .models import Agreement, Close, Dataset, Order, TicketEvent

ORDER_COLUMNS = ("order_id", "channel", "product", "sold_at", "currency", "gross_minor")
EVENT_COLUMNS = (
    "event_id",
    "order_id",
    "old_ticket",
    "new_ticket",
    "event",
    "event_date",
    "amount_minor",
    "venue",
)
EVENT_OPTIONAL = ("recorded_at",)
AGREEMENT_COLUMNS = (
    "agreement_id",
    "venue",
    "product",
    "channel",
    "start_date",
    "end_date",
    "basis_points",
    "cap_minor",
)
CLOSE_COLUMNS = ("period", "closed_at", "order_id", "amount_minor", "status")
CLOSE_OPTIONAL = ("basis_as_of",)
AGREEMENT_OPTIONAL = ("settlement_lag_days",)

EVENT_KINDS = {"售出", "换票", "核销", "入馆", "部分退款", "全额退款"}


class LoadError(ValueError):
    pass


def _read(
    path: Path, required: tuple[str, ...], optional: tuple[str, ...] = ()
) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise LoadError(f"{path.name}: 缺少表头")
        names = [name.strip() for name in reader.fieldnames if name]
        missing = [column for column in required if column not in names]
        extra = [
            name for name in names if name not in required and name not in optional
        ]
        if missing or extra:
            raise LoadError(
                f"{path.name}: 表头不符，缺少 {missing or '无'}，未识别 {extra or '无'}"
            )
        rows = []
        for line_no, raw in enumerate(reader, start=2):
            if None in raw:
                raise LoadError(
                    f"{path.name} 第{line_no}行: 字段数多于表头，多出 "
                    f"{[value for value in raw[None] if value]}；"
                    "含逗号的字段请加引号"
                )
            row = {
                key.strip(): (value or "").strip()
                for key, value in raw.items()
                if key is not None
            }
            row.setdefault("_line", str(line_no))
            if not any(value for key, value in row.items() if key != "_line"):
                continue
            rows.append(row)
        return rows


def _date(value: str, where: str, *, optional: bool = False):
    if not value and optional:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise LoadError(f"{where}: 日期 {value!r} 不是 YYYY-MM-DD") from exc


def _int(value: str, where: str, *, allow_empty: bool = False) -> int:
    if value == "" and allow_empty:
        return 0
    try:
        return int(value, 10)
    except ValueError as exc:
        raise LoadError(f"{where}: 金额/比例 {value!r} 必须是整数（最小单位）") from exc


def _required(row: dict[str, str], key: str, where: str) -> str:
    value = row.get(key, "")
    if not value:
        raise LoadError(f"{where}: {key} 不能为空")
    return value


def _period(value: str, where: str) -> str:
    try:
        year, month = value.split("-", 1)
        if len(year) != 4 or len(month) != 2:
            raise ValueError
        date(int(year), int(month), 1)
    except ValueError as exc:
        raise LoadError(f"{where}: period {value!r} 不是 YYYY-MM") from exc
    return value


def load_dataset(root: Path | str) -> Dataset:
    root = Path(root)
    data = root / "data" if (root / "data").is_dir() else root
    at = lambda name: data / name  # noqa: E731

    dataset = Dataset()

    for row in _read(at("orders.csv"), ORDER_COLUMNS):
        where = f"orders.csv 第{row['_line']}行"
        order = Order(
            order_id=_required(row, "order_id", where),
            channel=_required(row, "channel", where),
            product=_required(row, "product", where),
            sold_at=_date(row["sold_at"], where),
            currency=_required(row, "currency", where),
            gross_minor=_int(row["gross_minor"], where),
        )
        if order.gross_minor < 0:
            raise LoadError(f"{where}: gross_minor 不能为负")
        if order.order_id in dataset.orders:
            raise LoadError(f"{where}: 重复的 order_id {order.order_id}")
        dataset.orders[order.order_id] = order

    for row in _read(at("ticket_events.csv"), EVENT_COLUMNS, EVENT_OPTIONAL):
        where = f"ticket_events.csv 第{row['_line']}行"
        kind = row["event"]
        if kind not in EVENT_KINDS:
            raise LoadError(f"{where}: 未知事件类型 {kind!r}")
        event = TicketEvent(
            event_id=_required(row, "event_id", where),
            order_id=_required(row, "order_id", where),
            old_ticket=row.get("old_ticket", ""),
            new_ticket=row.get("new_ticket", ""),
            event=kind,
            event_date=_date(row["event_date"], where),
            amount_minor=_int(row["amount_minor"], where, allow_empty=True),
            venue=row.get("venue", ""),
            sequence=int(row["_line"]),
            recorded_at=_date(row.get("recorded_at", ""), where, optional=True),
        )
        if event.amount_minor < 0:
            raise LoadError(f"{where}: amount_minor 不能为负")
        if event.is_use and not event.venue:
            raise LoadError(f"{where}: 使用事件必须填写 venue")
        if kind == "换票" and (not event.old_ticket or not event.new_ticket):
            raise LoadError(f"{where}: 换票必须同时给出 old_ticket 与 new_ticket")
        if event.is_refund and event.amount_minor == 0 and kind == "部分退款":
            raise LoadError(f"{where}: 部分退款金额不能为 0")
        if event.order_id not in dataset.orders:
            raise LoadError(f"{where}: 引用了不存在的订单 {event.order_id}")
        dataset.events.append(event)

    if len({event.event_id for event in dataset.events}) != len(dataset.events):
        raise LoadError("ticket_events.csv: event_id 必须唯一")
    dataset.events.sort(key=lambda event: (event.event_date, event.sequence))

    for row in _read(at("agreements.csv"), AGREEMENT_COLUMNS, AGREEMENT_OPTIONAL):
        where = f"agreements.csv 第{row['_line']}行"
        start = _date(row["start_date"], where)
        end = _date(row["end_date"], where)
        if end <= start:
            raise LoadError(f"{where}: end_date 必须晚于 start_date（区间右开）")
        basis_points = _int(row["basis_points"], where)
        if not 0 <= basis_points <= 10_000:
            raise LoadError(f"{where}: basis_points 必须在 0..10000")
        cap = _int(row["cap_minor"], where)
        if cap < 0:
            raise LoadError(f"{where}: cap_minor 不能为负")
        lag_raw = row.get("settlement_lag_days", "")
        lag = _int(lag_raw, where, allow_empty=True) if lag_raw else None
        if lag is not None and lag < 0:
            raise LoadError(f"{where}: settlement_lag_days 不能为负")
        dataset.agreements.append(
            Agreement(
                agreement_id=_required(row, "agreement_id", where),
                venue=_required(row, "venue", where),
                product=_required(row, "product", where),
                channel=row.get("channel") or "*",
                start_date=start,
                end_date=end,
                basis_points=basis_points,
                cap_minor=cap,
                settlement_lag_days=lag,
            )
        )
    if len({item.agreement_id for item in dataset.agreements}) != len(
        dataset.agreements
    ):
        raise LoadError("agreements.csv: agreement_id 必须唯一")

    for row in _read(at("closes.csv"), CLOSE_COLUMNS, CLOSE_OPTIONAL):
        where = f"closes.csv 第{row['_line']}行"
        close = Close(
            period=_period(_required(row, "period", where), where),
            closed_at=_date(row["closed_at"], where),
            order_id=_required(row, "order_id", where),
            amount_minor=_int(row["amount_minor"], where),
            status=_required(row, "status", where),
            sequence=int(row["_line"]),
            basis_as_of=_date(
                row.get("basis_as_of", ""), where, optional=True
            ),
        )
        if close.status not in {"已确认", "已作废"}:
            raise LoadError(f"{where}: status 只能是 已确认/已作废")
        if close.amount_minor < 0:
            raise LoadError(f"{where}: amount_minor 不能为负")
        if close.order_id not in dataset.orders:
            raise LoadError(f"{where}: 引用了不存在的订单 {close.order_id}")
        dataset.closes.append(close)
    dataset.closes.sort(key=lambda close: (close.period, close.sequence))

    return dataset
