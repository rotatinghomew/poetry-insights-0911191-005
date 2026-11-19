"""联票份额计算、协议解析与结算/运营视图。

计算口径（全部金额为整数最小单位，即币种"分"）：

``售价 = 已退款 + Σ馆方使用份额 + 暂未归属``

* 换票只改写票据标识，不改变订单售价；一张订单的本金只分配一次。
* 同一订单的同一馆只在**首次有效核销**时计提一次份额；重复到馆照常计数，
  但不重复分配本金。
* 每笔馆方份额 = ``floor(可分配本金 × 该馆协议比例)``，再受该馆协议
  ``cap_minor`` 封顶。比例用有理数精确计算；取整零头、未签约比例与封顶
  余额分别留在"暂未归属"的细分桶里，逐单可复核。
* 若同一订单各馆协议比例合计超过 100%，全部份额按同一比例压缩后分配，
  并明确告警，不优先照顾任何一馆。
* 协议按"馆 + 票种 + 渠道（含通配）+ 核销日"解析；渠道专属协议优先于
  通配协议；同等具体程度出现多份视为重叠，阻断该订单结算，绝不任选。
* 核销只登记馆方使用份额。财务关账（closes.csv）才把份额确认为应付；
  已确认金额按"关账时已知依据"摊到馆方，供对方复核，原单不可修改。
  关账后补录的核销/退款不回写旧账期，而在当前账期生成逐馆调整单，
  列明调整额、对应原单与原账期；比例份额使退款差额由各馆按比例分担，
  不会全部推给补录的馆。
* "尚未发生有效使用"、"使用记录未交齐"、"协议冲突阻断"、"已确认待付"、
  "调整待确认"、"暂未归属"分别列示，不合并成一个总数。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from typing import Iterable, Optional

from .models import Agreement, Close, Dataset, Order, TicketEvent

BASIS_DENOMINATOR = 10_000


# --------------------------------------------------------------------- 日期
def period_end(period: str) -> date:
    year, month = (int(part) for part in period.split("-", 1))
    return date(year, month, calendar.monthrange(year, month)[1])


def period_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def next_period(period: str) -> str:
    year, month = (int(part) for part in period.split("-", 1))
    if month == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{month + 1:02d}"


def iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year:04d}-W{week:02d}"


def _floor(value: Fraction) -> int:
    return value.numerator // value.denominator


def _fraction_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


# ------------------------------------------------------------------ 整数分配
def distribute_integer(
    amount: int, weights: dict[str, int]
) -> tuple[dict[str, int], dict]:
    """把整数金额按权重分完；余数用最大余数法逐分分配，结果可复核。"""
    keys = sorted(weights)
    if not keys:
        return {}, {"amount": amount, "residual": 0, "extra": {}}
    total = sum(weights.values())
    if amount == 0:
        return {key: 0 for key in keys}, {"amount": 0, "residual": 0, "extra": {}}
    if total <= 0:
        weights = {key: 1 for key in keys}
        total = len(keys)
    shares = {}
    remainders = []
    for key in keys:
        numerator = amount * weights[key]
        shares[key] = numerator // total
        remainders.append((numerator % total, key))
    residual = amount - sum(shares.values())
    remainders.sort(key=lambda item: (-item[0], item[1]))
    extra: dict[str, int] = {}
    for index in range(residual):
        key = remainders[index % len(remainders)][1]
        shares[key] += 1
        extra[key] = extra.get(key, 0) + 1
    return shares, {"amount": amount, "residual": residual, "extra": extra}


# ------------------------------------------------------------------- 结构
@dataclass
class AgreementMatch:
    event_id: str
    order_id: str
    channel: str
    product: str
    venue: str
    on_date: date
    candidates: list[Agreement]
    chosen: Optional[Agreement]
    status: str  # ok / specificity / missing / overlap

    @property
    def blocked(self) -> bool:
        return self.status in {"missing", "overlap"}


@dataclass
class UsageShare:
    event: TicketEvent
    match: AgreementMatch
    basis_points: int
    exact_claim: Fraction = Fraction(0)
    claim_minor: int = 0          # 取整后、封顶前
    share_minor: int = 0          # 最终馆方份额（封顶后）
    rounding_minor: int = 0       # 分到的取整尾差（+1 分次数）
    cap_reduction_minor: int = 0  # 因协议上限被削减的金额
    repeat_of: Optional[str] = None  # 同馆重复核销时指向首次核销事件


@dataclass
class OrderModel:
    order: Order
    cutoff: Optional[date]
    known_by: Optional[date]
    refunded_minor: int
    allocatable_minor: int
    usages: list[UsageShare]
    venue_shares: dict[str, int]
    venue_caps: dict[str, int]
    venue_rates: dict[str, int]
    unallocated_minor: int
    unallocated_reason: Optional[str]
    unallocated_breakdown: dict[str, int]
    blocked: bool
    problems: list[str]
    warnings: list[str]
    agreement_matches: list[AgreementMatch]
    allocation_trace: list[dict] = field(default_factory=list)
    cutoff_label: str = "全量"

    def share_of(self, venue: str) -> int:
        return self.venue_shares.get(venue, 0)

    def usage_count(self, venue: Optional[str] = None) -> int:
        return sum(
            1 for use in self.usages if venue is None or use.event.venue == venue
        )

    def identity_holds(self) -> bool:
        return (
            self.refunded_minor
            + sum(self.venue_shares.values())
            + self.unallocated_minor
            == self.order.gross_minor
        )

    def funds(self) -> dict:
        return {
            "gross_minor": self.order.gross_minor,
            "refunded_minor": self.refunded_minor,
            "venue_total_minor": sum(self.venue_shares.values()),
            "unallocated_minor": self.unallocated_minor,
            "unallocated_breakdown": dict(self.unallocated_breakdown),
            "identity_ok": self.identity_holds(),
        }


class LedgerError(RuntimeError):
    pass


# ------------------------------------------------------------------- 引擎
class Ledger:
    """对一份 Dataset 做全量计算；所有金额为整数最小单位。"""

    def __init__(self, dataset: Dataset):
        self.data = dataset
        self.warnings: list[dict] = []
        self._cache: dict[tuple[str, Optional[date], Optional[date]], OrderModel] = {}
        self._closed = self._effective_closes()
        self._current_period = self._default_current_period()

    # -------------------------------------------------------------- 关账数据
    def _effective_closes(self) -> dict[str, list[Close]]:
        closes: dict[str, list[Close]] = {}
        seen: dict[tuple[str, str], Close] = {}
        for close in self.data.closes:
            if close.status != "已确认":
                continue
            key = (close.order_id, close.period)
            if key in seen:
                self.warnings.append(
                    {
                        "code": "duplicate_close",
                        "order_id": close.order_id,
                        "message": f"订单 {close.order_id} 在 {close.period} 有重复确认"
                        "记录，以首条为准，请财务核对。",
                    }
                )
                continue
            seen[key] = close
            closes.setdefault(close.order_id, []).append(close)
        for rows in closes.values():
            rows.sort(key=lambda close: (close.period, close.sequence))
        return closes

    def _default_current_period(self) -> str:
        months = {period_of(event.event_date) for event in self.data.events}
        months |= {close.period for rows in self._closed.values() for close in rows}
        # 没有事件时以订单售出月为起点；仍为空才退回当天。
        months |= {period_of(order.sold_at) for order in self.data.orders.values()}
        if not months:
            return period_of(date.today())
        current = max(months)
        latest_close = max(
            (close.period for rows in self._closed.values() for close in rows),
            default=None,
        )
        if latest_close is not None and current <= latest_close:
            current = next_period(latest_close)
        return current

    @property
    def current_period(self) -> str:
        return self._current_period

    def latest_close(self, order_id: str) -> Optional[Close]:
        rows = self._closed.get(order_id, [])
        return rows[-1] if rows else None

    def confirmed_minor(self, order_id: str) -> int:
        return sum(close.amount_minor for close in self._closed.get(order_id, []))

    def _events(
        self,
        order_id: str,
        cutoff: Optional[date],
        known_by: Optional[date] = None,
    ) -> list[TicketEvent]:
        """cutoff 按事件日期截断；known_by 按记录时间排除当时尚未收到的记录。"""
        events = self.data.events_for(order_id)
        if cutoff is not None:
            events = [event for event in events if event.event_date <= cutoff]
        if known_by is not None:
            events = [
                event
                for event in events
                if event.recorded_at is None or event.recorded_at <= known_by
            ]
        return sorted(events, key=lambda event: (event.event_date, event.sequence))

    # -------------------------------------------------------------- 协议解析
    def match_agreement(
        self, order: Order, venue: str, on_date: date, event_id: str = ""
    ) -> AgreementMatch:
        candidates = [
            agreement
            for agreement in self.data.agreements_for(order, venue)
            if agreement.start_date <= on_date < agreement.end_date
        ]
        candidates.sort(key=lambda a: (a.specificity, a.start_date, a.agreement_id))
        base = dict(
            event_id=event_id,
            order_id=order.order_id,
            channel=order.channel,
            product=order.product,
            venue=venue,
            on_date=on_date,
            candidates=candidates,
        )
        if not candidates:
            return AgreementMatch(status="missing", chosen=None, **base)
        top_specificity = max(agreement.specificity for agreement in candidates)
        top = [a for a in candidates if a.specificity == top_specificity]
        if len(top) > 1:
            return AgreementMatch(status="overlap", chosen=None, **base)
        chosen = top[0]
        status = "ok" if len(candidates) == 1 else "specificity"
        return AgreementMatch(status=status, chosen=chosen, **base)

    # -------------------------------------------------------------- 票据校验
    def _ticket_problems(self, events: list[TicketEvent]) -> list[str]:
        """校验换票链；换票前发生的合法入馆保留，换票后再用旧票才是问题。"""
        problems: list[str] = []
        has_sale = any(event.event == "售出" for event in events)
        active: set[str] = set()
        seen: set[str] = set()
        for event in events:
            if event.event == "售出":
                if event.old_ticket in seen:
                    problems.append(
                        f"票据 {event.old_ticket} 被重复售出（{event.event_id}）"
                    )
                active.add(event.old_ticket)
                seen.add(event.old_ticket)
            elif event.event == "换票":
                if has_sale and event.old_ticket not in active:
                    problems.append(
                        f"换票 {event.event_id} 的旧票 {event.old_ticket} 在换票日无效"
                    )
                if event.new_ticket in seen:
                    problems.append(
                        f"换票 {event.event_id} 的新票 {event.new_ticket} 已存在"
                    )
                active.discard(event.old_ticket)
                active.add(event.new_ticket)
                seen.add(event.new_ticket)
            elif event.is_use and has_sale:
                if event.old_ticket not in seen:
                    problems.append(
                        f"核销 {event.event_id} 使用了未售出的票据 {event.old_ticket}"
                    )
                elif event.old_ticket not in active:
                    problems.append(
                        f"核销 {event.event_id} 使用了已换出的票据 "
                        f"{event.old_ticket}；换票后应使用新票"
                    )
        return problems

    # -------------------------------------------------------------- 构建模型
    def order_model(
        self,
        order_id: str,
        cutoff: Optional[date] = None,
        known_by: Optional[date] = None,
    ) -> OrderModel:
        key = (order_id, cutoff, known_by)
        if key in self._cache:
            return self._cache[key]
        if order_id not in self.data.orders:
            raise LedgerError(f"未知订单 {order_id}")
        order = self.data.orders[order_id]
        events = self._events(order_id, cutoff, known_by)
        problems: list[str] = []
        warnings: list[str] = []
        trace: list[dict] = []
        for event in events:
            if event.recorded_at is not None and event.recorded_at < event.event_date:
                warnings.append(
                    f"事件 {event.event_id} 的记录日 {event.recorded_at} 早于到馆日 "
                    f"{event.event_date}，请核对补录时间"
                )

        refunded = sum(event.amount_minor for event in events if event.is_refund)
        if refunded > order.gross_minor:
            problems.append(
                f"退款合计 {refunded} 超过售价 {order.gross_minor}，本金无法守恒"
            )
        allocatable = max(0, order.gross_minor - refunded)
        problems.extend(self._ticket_problems(events))

        matches: list[AgreementMatch] = []
        usages: list[UsageShare] = []
        venue_caps: dict[str, int] = {}
        venue_rates: dict[str, int] = {}
        # 每个馆在该订单下只按首次有效核销计提一次，重复入馆只计使用次数。
        first_use: dict[str, UsageShare] = {}
        for event in events:
            if not event.is_use:
                continue
            match = self.match_agreement(
                order, event.venue, event.event_date, event.event_id
            )
            matches.append(match)
            basis_points = match.chosen.basis_points if match.chosen else 0
            usage = UsageShare(event=event, match=match, basis_points=basis_points)
            usages.append(usage)
            if match.blocked:
                continue
            if event.venue not in first_use:
                first_use[event.venue] = usage
                venue_rates[event.venue] = basis_points
                venue_caps[event.venue] = match.chosen.cap_minor
            else:
                usage.repeat_of = first_use[event.venue].event.event_id
                if match.chosen.basis_points != venue_rates[event.venue]:
                    warnings.append(
                        f"{event.venue} 在 {first_use[event.venue].event.event_date} 与 "
                        f"{event.event_date} 适用不同协议比例"
                        f"（{first_use[event.venue].match.chosen.agreement_id} vs "
                        f"{match.chosen.agreement_id}）；本单按首次核销协议计提，"
                        "第二段比例变化需人工裁决"
                    )
        for venue, usage in first_use.items():
            repeats = [
                use.event.event_id
                for use in usages
                if use.event.venue == venue and use.repeat_of is not None
            ]
            trace.append(
                {
                    "step": "venue_entitlement",
                    "venue": venue,
                    "first_event_id": usage.event.event_id,
                    "agreement_id": usage.match.chosen.agreement_id,
                    "basis_points": usage.basis_points,
                    "cap_minor": venue_caps[venue],
                    "repeat_event_ids": repeats,
                    "note": "同馆同单重复入馆不再重复计提"
                    + (f"（{len(repeats)} 次仅计使用次数）" if repeats else ""),
                }
            )

        for match in matches:
            if match.status == "missing":
                problems.append(
                    f"核销 {match.event_id}（{match.venue} {match.on_date}）缺少适用协议"
                )
            elif match.status == "overlap":
                ids = "、".join(agreement.agreement_id for agreement in match.candidates)
                problems.append(
                    f"核销 {match.event_id}（{match.venue} {match.on_date}）"
                    f"命中重叠协议 {ids}"
                )

        blocked = bool(problems)
        shares: dict[str, int] = {}
        breakdown = {"uncontracted": 0, "rounding": 0, "cap": 0}
        if blocked:
            unallocated = allocatable
            reason = "blocked"
            breakdown = {"uncontracted": allocatable, "rounding": 0, "cap": 0}
        elif not usages:
            unallocated = allocatable
            reason = "no_usage"
            breakdown = {"uncontracted": allocatable, "rounding": 0, "cap": 0}
            if allocatable:
                warnings.append(f"订单 {order_id} 有可分配本金但尚无有效核销")
        elif allocatable == 0:
            unallocated = 0
            reason = None
            breakdown = {"uncontracted": 0, "rounding": 0, "cap": 0}
            for use in usages:
                use.exact_claim = Fraction(0)
        else:
            shares, unallocated, breakdown = self._allocate(
                allocatable, usages, venue_caps, trace, warnings
            )
            if unallocated and venue_rates and sum(venue_rates.values()) <= 0:
                reason = "zero_basis"
            elif not unallocated:
                reason = None
            else:
                nonzero = [key for key, value in breakdown.items() if value]
                reason = nonzero[0] if len(nonzero) == 1 else "mixed"

        trace.append(
            {
                "step": "identity",
                "gross_minor": order.gross_minor,
                "refunded_minor": refunded,
                "venue_total_minor": sum(shares.values()),
                "unallocated_minor": unallocated,
                "breakdown": dict(breakdown),
                "ok": refunded + sum(shares.values()) + unallocated == order.gross_minor,
            }
        )

        model = OrderModel(
            order=order,
            cutoff=cutoff,
            known_by=known_by,
            refunded_minor=refunded,
            allocatable_minor=allocatable,
            usages=usages,
            venue_shares=shares,
            venue_caps=venue_caps,
            venue_rates=venue_rates,
            unallocated_minor=unallocated,
            unallocated_reason=reason,
            unallocated_breakdown=breakdown,
            blocked=blocked,
            problems=problems,
            warnings=warnings,
            agreement_matches=matches,
            allocation_trace=trace,
            cutoff_label=self._cutoff_label(cutoff, known_by),
        )
        if not model.identity_holds():
            raise LedgerError(
                f"订单 {order_id} 金额不守恒：{order.gross_minor} != "
                f"{refunded}+{sum(shares.values())}+{unallocated}"
            )
        self._cache[key] = model
        return model

    @staticmethod
    def _cutoff_label(cutoff: Optional[date], known_by: Optional[date]) -> str:
        if cutoff is None and known_by is None:
            return "全量"
        parts = []
        if cutoff is not None:
            parts.append(f"到馆日≤{cutoff.isoformat()}")
        if known_by is not None:
            parts.append(f"记录日≤{known_by.isoformat()}")
        return "，".join(parts)

    def _allocate(
        self,
        allocatable: int,
        usages: list[UsageShare],
        venue_caps: dict[str, int],
        trace: list[dict],
        warnings: list[str],
    ) -> tuple[dict[str, int], int, dict[str, int]]:
        """按协议比例提出馆方份额：精确计算、超额压缩、逐笔取整、逐馆封顶。

        每一笔有效核销的份额 = floor(可分配本金 × 该馆协议比例)，因此每笔
        都可独立复核；取整产生的零头留在未归属的 ``rounding`` 桶。
        同馆重复入馆（``repeat_of``）不再计提，只保留使用次数。
        """
        claim_usages = [use for use in usages if use.repeat_of is None]
        total_rate = sum(use.basis_points for use in claim_usages)
        scale = Fraction(1)
        if total_rate > BASIS_DENOMINATOR:
            scale = Fraction(BASIS_DENOMINATOR, total_rate)
            warnings.append(
                f"该订单协议比例合计 {total_rate} bp 超过 100%，"
                "已按同一比例压缩后分配，请复核协议是否重叠"
            )
            trace.append(
                {
                    "step": "oversubscribed",
                    "total_basis_points": total_rate,
                    "scale": _fraction_text(scale),
                    "note": "所有馆方份额按同一比例压缩，不偏袒任何一方",
                }
            )

        exact_total = Fraction(0)
        for use in claim_usages:
            use.exact_claim = Fraction(
                allocatable * use.basis_points * scale.numerator,
                BASIS_DENOMINATOR * scale.denominator,
            )
            use.claim_minor = _floor(use.exact_claim)
            use.share_minor = use.claim_minor
            exact_total += use.exact_claim
        trace.append(
            {
                "step": "claims",
                "allocatable_minor": allocatable,
                "exact_total": _fraction_text(exact_total),
                "integer_total": sum(use.claim_minor for use in claim_usages),
                "claims": [
                    {
                        "event_id": use.event.event_id,
                        "venue": use.event.venue,
                        "basis_points": use.basis_points,
                        "exact_claim": _fraction_text(use.exact_claim),
                        "claim_minor": use.claim_minor,
                        "note": "逐笔向下取整，零头留在未归属",
                    }
                    for use in claim_usages
                ],
            }
        )

        by_venue: dict[str, int] = {}
        for use in claim_usages:
            by_venue[use.event.venue] = (
                by_venue.get(use.event.venue, 0) + use.share_minor
            )
        venue_shares: dict[str, int] = {}
        cap_total = 0
        for venue, amount in sorted(by_venue.items()):
            cap = venue_caps.get(venue, 0)
            allowed = min(amount, cap)
            if allowed:
                venue_shares[venue] = allowed
            excess = amount - allowed
            if not excess:
                continue
            cap_total += excess
            weights = {
                use.event.event_id: use.share_minor
                for use in claim_usages
                if use.event.venue == venue
            }
            cuts, info = distribute_integer(excess, weights)
            for use in claim_usages:
                if use.event.venue != venue:
                    continue
                use.cap_reduction_minor = cuts.get(use.event.event_id, 0)
                use.share_minor -= use.cap_reduction_minor
            trace.append(
                {
                    "step": "venue_cap",
                    "venue": venue,
                    "cap_minor": cap,
                    "claimed_minor": amount,
                    "cap_reduction_minor": excess,
                    "reduction_by_event": cuts,
                    "residual": info["residual"],
                    "note": "超出协议上限的部分留在未归属，不转给其他馆",
                }
            )

        venue_total = sum(venue_shares.values())
        unallocated = allocatable - venue_total
        uncontracted_exact = max(Fraction(0), Fraction(allocatable) - exact_total)
        uncontracted = min(_floor(uncontracted_exact), unallocated)
        rounding = unallocated - uncontracted - cap_total
        if rounding < 0:
            # 极端情况下（比例合计略超 100% 且已封顶）把差值并入未签约桶。
            uncontracted += rounding
            rounding = 0
        breakdown = {
            "uncontracted": uncontracted,
            "rounding": rounding,
            "cap": cap_total,
        }
        if sum(breakdown.values()) != unallocated:
            raise LedgerError(
                f"未归属拆分不守恒：{breakdown} vs {unallocated}"
                f"（订单 {claim_usages[0].event.order_id}）"
            )
        return venue_shares, unallocated, breakdown

    # -------------------------------------------------------------- 关账依据
    def basis_model(
        self, order_id: str, close: Optional[Close] = None
    ) -> Optional[OrderModel]:
        close = close or self.latest_close(order_id)
        if close is None:
            return None
        # 关账依据 = 账期末已到馆、且关账确认时（或显式 basis_as_of 时）
        # 已收到的记录。basis_as_of 让复核方按当时依据重放，不受后来补录影响。
        return self.order_model(
            order_id, cutoff=period_end(close.period), known_by=close.basis_date
        )

    def close_recognition(self, close: Close) -> tuple[dict[str, int], list[str], str]:
        """按该账期关账时已知依据，把已确认金额摊到馆方，供对方复核。"""
        if close.amount_minor == 0:
            return {}, [], "zero"
        basis = self.basis_model(close.order_id, close)
        notes: list[str] = []
        if basis.blocked:
            return (
                {},
                ["关账时依据存在协议冲突/缺失，已确认金额无法按馆方摊分"],
                "blocked",
            )
        if not basis.venue_shares:
            return (
                {},
                ["关账时没有馆方使用记录，已确认金额保留为订单级；使用记录未交齐"],
                "missing_usage",
            )
        if close.amount_minor > basis.allocatable_minor:
            notes.append(
                f"关账金额 {close.amount_minor} 超过当时可分配本金 "
                f"{basis.allocatable_minor}，请财务复核"
            )
        ratio = Fraction(close.amount_minor, max(1, sum(basis.venue_shares.values())))
        exact = {
            venue: share * ratio for venue, share in basis.venue_shares.items()
        }
        parts = {venue: _floor(value) for venue, value in exact.items()}
        remainder = close.amount_minor - sum(parts.values())
        ordered = sorted(
            exact, key=lambda venue: (-(exact[venue] - parts[venue]), venue)
        )
        extra: dict[str, int] = {}
        if ordered:
            for index in range(remainder):
                venue = ordered[index % len(ordered)]
                parts[venue] += 1
                extra[venue] = extra.get(venue, 0) + 1
        elif remainder:
            parts[ordered[0]] = remainder
            extra[ordered[0]] = remainder
        if close.amount_minor != sum(basis.venue_shares.values()):
            notes.append(
                f"关账金额 {close.amount_minor} 与当时依据份额 "
                f"{sum(basis.venue_shares.values())} 不同，已按依据比例摊分"
            )
        if extra:
            notes.append(
                "摊分舍入零头归入 "
                + "、".join(f"{venue}+{count}" for venue, count in extra.items())
            )
        return parts, notes, "allocated"

    def recognized_by_venue(
        self, order_id: str, as_of: Optional[date] = None
    ) -> dict[str, int]:
        totals: dict[str, int] = {}
        for close in self._closed.get(order_id, []):
            if as_of is not None and close.closed_at > as_of:
                continue
            parts, _, _ = self.close_recognition(close)
            for venue, amount in parts.items():
                totals[venue] = totals.get(venue, 0) + amount
        return totals

    def recognition_detail(self, close: Close) -> dict:
        """供对方复核的明细：依据时点、命中协议与各馆份额。"""
        basis = self.basis_model(close.order_id, close)
        parts, notes, status = self.close_recognition(close)
        return {
            "order_id": close.order_id,
            "period": close.period,
            "closed_at": close.closed_at.isoformat(),
            "basis_as_of": close.basis_date.isoformat(),
            "basis_cutoff": basis.cutoff_label if basis else None,
            "confirmed_minor": close.amount_minor,
            "status": status,
            "by_venue": parts,
            "note": "；".join(notes),
            "usages": [
                {
                    "event_id": use.event.event_id,
                    "visit_date": use.event.event_date.isoformat(),
                    "recorded_at": use.event.recorded_at.isoformat()
                    if use.event.recorded_at
                    else None,
                    "venue": use.event.venue,
                    "agreement_id": use.match.chosen.agreement_id
                    if use.match.chosen
                    else None,
                    "basis_points": use.basis_points,
                    "share_minor": use.share_minor,
                }
                for use in (basis.usages if basis else [])
            ],
        }

    def order_level_confirmed(
        self, order_id: str, as_of: Optional[date] = None
    ) -> int:
        """原关账缺少馆方依据、只能留在订单级的已确认金额。"""
        total = 0
        for close in self._closed.get(order_id, []):
            if as_of is not None and close.closed_at > as_of:
                continue
            parts, _, _ = self.close_recognition(close)
            if not parts and close.amount_minor:
                total += close.amount_minor
        return total

    # -------------------------------------------------------------- 财务视图
    def financial_view(
        self, current_period: Optional[str] = None, as_of: Optional[date] = None
    ) -> dict:
        """已确认 / 待确认 / 调整待确认 / 暂未归属 / 依据待补 / 阻断，分桶列示。"""
        current = current_period or self.current_period
        confirmed: list[dict] = []
        pending: list[dict] = []
        adjustments: list[dict] = []
        unallocated: list[dict] = []
        movements: list[dict] = []
        basis_pending: list[dict] = []
        blocked: list[dict] = []

        for order_id in sorted(self.data.orders):
            order = self.data.orders[order_id]
            full = self.order_model(order_id, cutoff=as_of)
            closes = self._closed.get(order_id, [])
            latest = closes[-1] if closes else None

            for close in closes:
                if as_of is not None and close.closed_at > as_of:
                    continue
                parts, notes, status = self.close_recognition(close)
                if status == "zero":
                    continue
                if parts:
                    for venue in sorted(parts):
                        confirmed.append(
                            {
                                "order_id": order_id,
                                "venue": venue,
                                "currency": order.currency,
                                "period": close.period,
                                "closed_at": close.closed_at.isoformat(),
                                "amount_minor": parts[venue],
                                "status": "已确认待付",
                                "basis": "allocated",
                                "note": "原账期关账依据锁定，原单不可修改",
                            }
                        )
                else:
                    confirmed.append(
                        {
                            "order_id": order_id,
                            "venue": None,
                            "currency": order.currency,
                            "period": close.period,
                            "closed_at": close.closed_at.isoformat(),
                            "amount_minor": close.amount_minor,
                            "status": "已确认待付（订单级，使用记录未交齐）",
                            "basis": status,
                            "note": "；".join(notes),
                        }
                    )

            if latest is not None and as_of is not None and latest.closed_at > as_of:
                # 关账在 as_of 之后才确认：本视图仍按未关账订单列待确认，
                # 不能提前把份额算作应付。
                latest = None

            if latest is not None:
                if full.blocked:
                    blocked.append(self._blocked_row(order, full, current, "调整待定"))
                    continue
                basis = self.basis_model(order_id, latest)
                assert basis is not None
                order_level = self.order_level_confirmed(order_id, as_of=as_of)
                recognized = self.recognized_by_venue(order_id, as_of=as_of)
                basis_had_venues = bool(basis.venue_shares)
                # 原关账留在订单级的金额已经从本金中划出，未归属的起点要扣除它，
                # 否则会与"订单级冲抵"调整行重复计算。
                basis_unreserved = basis.unallocated_minor - order_level
                delta_unallocated = full.unallocated_minor - basis_unreserved

                if not basis_had_venues and not full.venue_shares:
                    # 原关账缺馆方使用依据，且至今仍无使用：不得自动重算或冲销。
                    basis_pending.append(
                        {
                            "order_id": order_id,
                            "venue": None,
                            "currency": order.currency,
                            "period": current,
                            "origin_period": latest.period,
                            "amount_minor": order_level,
                            "recalculated_allocatable_minor": full.allocatable_minor,
                            "reason": "使用记录未交齐：原关账只有订单级金额，"
                            "至今没有馆方核销记录；本金变化不自动向任何馆方分摊",
                            "status": "依据待补",
                            "note": f"原单 {order_id}（原账期 {latest.period}）的 "
                            f"{order_level} 已计入已确认待付，这里不重复计负债；"
                            "请补交使用记录或由财务裁决",
                        }
                    )
                    continue

                # 原关账可能有、也可能没有馆方依据；一律按"原确认为起点"逐馆列调整。
                for venue in sorted(set(full.venue_shares) | set(recognized)):
                    before = recognized.get(venue, 0)
                    after = full.share_of(venue)
                    delta = after - before
                    if delta == 0:
                        continue
                    adjustments.append(
                        {
                            "order_id": order_id,
                            "venue": venue,
                            "currency": order.currency,
                            "period": current,
                            "origin_period": latest.period,
                            "amount_minor": delta,
                            "origin_confirmed_minor": before,
                            "recalculated_minor": after,
                            "unallocated_change_minor": delta_unallocated,
                            "reason": self._change_reason(basis, full, venue),
                            "status": "调整待确认",
                            "note": f"对应原单 {order_id}（原账期 {latest.period}），"
                            "不修改原单；差额按各馆协议比例分别计算，"
                            "不集中推给补录馆",
                        }
                    )
                if order_level:
                    adjustments.append(
                        {
                            "order_id": order_id,
                            "venue": None,
                            "currency": order.currency,
                            "period": current,
                            "origin_period": latest.period,
                            "amount_minor": -order_level,
                            "origin_confirmed_minor": order_level,
                            "recalculated_minor": 0,
                            "reason": "冲抵原订单级已确认金额：原关账未附馆方使用记录，"
                            "现按各馆有效核销重新归属",
                            "status": "调整待确认（订单级冲抵）",
                            "note": f"对应原单 {order_id}（原账期 {latest.period}）；"
                            "与上面各馆调整相加后才是净变动",
                        }
                    )
                if delta_unallocated:
                    movements.append(
                        {
                            "order_id": order_id,
                            "venue": None,
                            "currency": order.currency,
                            "period": current,
                            "origin_period": latest.period,
                            "amount_minor": delta_unallocated,
                            "unallocated_before_minor": basis_unreserved,
                            "unallocated_after_minor": full.unallocated_minor,
                            "reason": "未归属余额随重算变动（属于说明，不是新增负债）："
                            + self._unallocated_delta_reason(basis, full),
                            "status": "未归属变动说明",
                            "note": f"对应原单 {order_id}（原账期 {latest.period}）；"
                            "与上表各馆调整互为镜像，不重复计负债",
                        }
                    )
            else:
                if full.blocked:
                    blocked.append(self._blocked_row(order, full, current, "未结算"))
                    continue
                settlement = self.settlement_status(full, as_of=as_of)
                for venue in sorted(full.venue_shares):
                    pending.append(
                        {
                            "order_id": order_id,
                            "venue": venue,
                            "currency": order.currency,
                            "period": current,
                            "amount_minor": full.venue_shares[venue],
                            "basis_points": full.venue_rates.get(venue, 0),
                            "settlement_status": settlement["status"],
                            "settlement_due": settlement["due"],
                            "status": "待财务确认",
                            "note": "仅记录馆方使用份额；"
                            + settlement["note"],
                        }
                    )
                if full.unallocated_minor:
                    unallocated.append(
                        {
                            "order_id": order_id,
                            "venue": None,
                            "currency": order.currency,
                            "period": current,
                            "amount_minor": full.unallocated_minor,
                            "breakdown": dict(full.unallocated_breakdown),
                            "reason": self._unallocated_label(full.unallocated_reason),
                            "status": "暂未归属",
                            "note": "不并入待确认或已确认金额",
                        }
                    )

        return {
            "current_period": current,
            "confirmed": {
                "label": "已确认待付（历史关账锁定）",
                "rows": confirmed,
                "by_currency_venue": _sum_rows(confirmed),
            },
            "pending": {
                "label": "当前账期待财务确认",
                "rows": pending,
                "by_currency_venue": _sum_rows(pending),
            },
            "adjustments": {
                "label": "调整待确认（关账后补录/退款，逐笔对应原单）",
                "rows": adjustments,
                "by_currency_venue": _sum_rows(adjustments),
            },
            "unallocated": {
                "label": "暂未归属（不含已确认、不含冲突阻断）",
                "rows": unallocated,
                "by_currency_venue": _sum_rows(unallocated),
            },
            "unallocated_movements": {
                "label": "未归属变动说明（与各馆调整互为镜像，不计入负债）",
                "rows": movements,
            },
            "basis_pending": {
                "label": "使用记录未交齐（已确认但无馆方依据，金额不重新分摊）",
                "rows": basis_pending,
            },
            "blocked": {
                "label": "协议冲突/缺失，阻止有关订单结算",
                "rows": blocked,
            },
            "notes": "各桶分开展示；不对不同状态求总额，也不跨币种相加。",
        }

    def settlement_status(
        self, model: OrderModel, as_of: Optional[date] = None
    ) -> dict:
        """协议结算时点：核销日 + 协议宽限天数；无宽限时以财务关账为准。"""
        dues = []
        for use in model.usages:
            if use.repeat_of is not None or use.match.chosen is None:
                continue
            dues.append(
                use.match.chosen.settlement_due(use.event.event_date)
            )
        if not dues or any(due is None for due in dues):
            return {
                "status": "按财务关账结算",
                "due": None,
                "note": "协议未约定核销后结算天数，待财务关账确认",
            }
        latest_due = max(dues)
        # 参考时点必须可复现：优先用 --as-of，其次用该单最晚事件日期，
        # 不依赖运行当天，否则同一份数据明天会得出不同结论。
        reference = as_of or max(
            (use.event.event_date for use in model.usages), default=latest_due
        )
        if reference >= latest_due:
            return {
                "status": "已到协议结算时点，待财务关账",
                "due": latest_due.isoformat(),
                "note": f"协议结算时点 {latest_due.isoformat()} 已到，"
                "仍需财务关账后才转为已确认待付",
            }
        return {
            "status": "未到协议结算时点",
            "due": latest_due.isoformat(),
            "note": f"协议结算时点 {latest_due.isoformat()} 尚未到期",
        }

    @staticmethod
    def _blocked_row(
        order: Order, model: OrderModel, period: str, status: str
    ) -> dict:
        return {
            "order_id": order.order_id,
            "venue": None,
            "currency": order.currency,
            "period": period,
            "amount_minor": None,
            "reason": "；".join(model.problems),
            "status": status,
            "note": "有关订单结算被阻止，本金保留，不得任选协议版本",
        }

    @staticmethod
    def _unallocated_label(reason: Optional[str]) -> str:
        return {
            "no_usage": "尚未发生有效使用",
            "uncontracted": "未签约比例（各馆协议合计不足 100%）",
            "rounding": "逐笔取整零头（含未签约比例的小数部分）",
            "cap": "协议上限封顶后的余额",
            "mixed": "未签约比例、取整零头与封顶余额合计",
            "zero_basis": "协议分成比例为 0，无可分配份额",
            "blocked": "协议冲突/缺失",
            None: "无",
        }.get(reason, str(reason))

    @staticmethod
    def _known_at_close(use: UsageShare, basis: OrderModel) -> bool:
        """该核销是否已包含在原关账依据中（到馆日在账期内且当时已收到）。"""
        if basis.cutoff is None:
            return True
        if use.event.event_date > basis.cutoff:
            return False
        if use.event.recorded_at is not None and basis.known_by is not None:
            return use.event.recorded_at <= basis.known_by
        return True

    def _change_reason(
        self, basis: OrderModel, full: OrderModel, venue: str
    ) -> str:
        new_usages = [
            use
            for use in full.usages
            if use.event.venue == venue and not self._known_at_close(use, basis)
        ]
        if new_usages:
            ids = "、".join(use.event.event_id for use in new_usages)
            return f"关账后补录核销 {ids}（新发生或当时尚未收到）"
        if full.refunded_minor != basis.refunded_minor:
            return "关账后部分退款，按协议比例重算各馆净份额"
        if full.venue_rates.get(venue) != basis.venue_rates.get(venue):
            return "适用协议或比例变化，按核销当日协议重算"
        return "按协议比例重算（含取整尾差）"

    def _unallocated_delta_reason(self, basis: OrderModel, full: OrderModel) -> str:
        reasons = []
        if full.refunded_minor != basis.refunded_minor:
            reasons.append("部分退款减少可分配本金")
        if full.venue_rates != basis.venue_rates:
            reasons.append("新增核销或协议比例变化")
        if (
            full.unallocated_breakdown.get("cap")
            != basis.unallocated_breakdown.get("cap")
        ):
            reasons.append("协议上限封顶变化")
        if (
            full.unallocated_breakdown.get("rounding")
            != basis.unallocated_breakdown.get("rounding")
        ):
            reasons.append("取整尾差")
        return "；".join(reasons) or "重算"

    # -------------------------------------------------------------- 渠道收款
    def sales_view(self) -> dict:
        """渠道资金流水：按收款（售出）日归期，与入馆日分开展示。"""
        rows: list[dict] = []
        for order_id in sorted(self.data.orders):
            order = self.data.orders[order_id]
            sell_events = [
                event
                for event in self.data.events_for(order_id)
                if event.event == "售出"
            ]
            recorded = sum(event.amount_minor for event in sell_events)
            rows.append(
                {
                    "order_id": order_id,
                    "channel": order.channel,
                    "product": order.product,
                    "sold_at": order.sold_at.isoformat(),
                    "period": period_of(order.sold_at),
                    "week": iso_week(order.sold_at),
                    "currency": order.currency,
                    "gross_minor": order.gross_minor,
                    "recorded_sales_minor": recorded,
                    "consistent": recorded in {0, order.gross_minor},
                    "note": "收款周与到馆周可能不同，本表只反映渠道资金流入。",
                }
            )
        by_period: dict[tuple[str, str], int] = {}
        for row in rows:
            key = (row["currency"], row["period"])
            by_period[key] = by_period.get(key, 0) + row["gross_minor"]
        return {
            "label": "渠道收款流水（按售出日归期）",
            "rows": rows,
            "by_period": {
                f"{currency}:{period}": amount
                for (currency, period), amount in sorted(by_period.items())
            },
            "notes": "售出事件金额与订单售价不一致的订单会标为 consistent=false。",
        }

    # -------------------------------------------------------------- 票务流水
    def ticket_events_view(self, as_of: Optional[date] = None) -> list[dict]:
        rows: list[dict] = []
        for order_id in sorted(self.data.orders):
            order = self.data.orders[order_id]
            for event in self._events(order_id, as_of):
                rows.append(
                    {
                        "order_id": order_id,
                        "event_id": event.event_id,
                        "event": event.event,
                        "event_date": event.event_date.isoformat(),
                        "recorded_at": event.recorded_at.isoformat()
                        if event.recorded_at
                        else None,
                        "old_ticket": event.old_ticket,
                        "new_ticket": event.new_ticket,
                        "venue": event.venue,
                        "amount_minor": event.amount_minor,
                        "currency": order.currency,
                    }
                )
        return rows

    # -------------------------------------------------------------- 运营视图
    def ops_view(self, as_of: Optional[date] = None) -> dict:
        daily: list[dict] = []
        weekly: dict[tuple[str, str, str], dict] = {}
        monthly: dict[tuple[str, str, str], dict] = {}
        for order_id in sorted(self.data.orders):
            order = self.data.orders[order_id]
            model = self.order_model(order_id, cutoff=as_of)
            for use in model.usages:
                day = use.event.event_date
                row = {
                    "order_id": order_id,
                    "venue": use.event.venue,
                    "currency": order.currency,
                    "visit_date": day.isoformat(),
                    "period": period_of(day),
                    "week": iso_week(day),
                    "event_id": use.event.event_id,
                    "ticket": use.event.old_ticket,
                    "agreement_id": use.match.chosen.agreement_id
                    if use.match.chosen
                    else None,
                    "agreement_status": use.match.status,
                    "basis_points": use.basis_points,
                    "share_minor": use.share_minor,
                    "rounding_minor": use.rounding_minor,
                    "cap_reduction_minor": use.cap_reduction_minor,
                    "repeat_of": use.repeat_of,
                    "blocked": model.blocked,
                }
                daily.append(row)
                for bucket, granularity in ((monthly, "month"), (weekly, "week")):
                    key = self._ops_key(granularity, order, day, use.event.venue)
                    agg = bucket.setdefault(
                        key,
                        {
                            "currency": order.currency,
                            "venue": use.event.venue,
                            "period": period_of(day),
                            "week": iso_week(day),
                            "usage_count": 0,
                            "share_minor": 0,
                            "blocked_usage_count": 0,
                            "orders": set(),
                        },
                    )
                    agg["usage_count"] += 1
                    agg["orders"].add(order_id)
                    if model.blocked:
                        agg["blocked_usage_count"] += 1
                    else:
                        agg["share_minor"] += use.share_minor

        def finish(bucket: dict) -> list[dict]:
            rows = []
            for key in sorted(bucket):
                row = dict(bucket[key])
                row["orders"] = sorted(row["orders"])
                rows.append(row)
            return rows

        return {
            "label": "运营视图：按真实到馆日期统计更正后份额",
            "scope": "使用事实按核销日归月/归周，供运营复核；"
            "财务账期请看 financial 与 reconciliation",
            "daily": daily,
            "weekly": finish(weekly),
            "monthly": finish(monthly),
            "notes": "被阻止结算的订单只计使用次数，份额为空；同馆同单重复到馆"
            "照常计数，但 repeat_of 指向首次核销、不重复计提份额；"
            "退款不改变已发生的使用事实，但会改变可分配本金与份额。",
        }

    @staticmethod
    def _ops_key(
        granularity: str, order: Order, day: date, venue: str
    ) -> tuple[str, str, str]:
        if granularity == "month":
            return (order.currency, period_of(day), venue)
        return (order.currency, iso_week(day), venue)

    # -------------------------------------------------------------- 对账
    def reconcile(
        self,
        period: str,
        current_period: Optional[str] = None,
        as_of: Optional[date] = None,
    ) -> dict:
        """按馆解释：运营按到馆日的份额 vs 财务在 period 的账务金额。

        恒等式：``差异 = (已关账订单的本期到馆份额 - 本期已确认)
        + (未关账订单的本期到馆份额 - 本期待确认) - 本期调整单``
        """
        current = current_period or self.current_period
        ops = self.ops_view(as_of=as_of)
        financial = self.financial_view(current_period=current, as_of=as_of)

        ops_total: dict[tuple[str, str], int] = {}
        ops_closed: dict[tuple[str, str], int] = {}
        ops_open: dict[tuple[str, str], int] = {}
        usage_count: dict[tuple[str, str], int] = {}
        blocked_count: dict[tuple[str, str], int] = {}
        for row in ops["daily"]:
            if row["period"] != period:
                continue
            key = (row["currency"], row["venue"])
            usage_count[key] = usage_count.get(key, 0) + 1
            if row["blocked"]:
                blocked_count[key] = blocked_count.get(key, 0) + 1
                continue
            ops_total[key] = ops_total.get(key, 0) + row["share_minor"]
            closes = [
                close
                for close in self._closed.get(row["order_id"], [])
                if as_of is None or close.closed_at <= as_of
            ]
            if closes:
                ops_closed[key] = ops_closed.get(key, 0) + row["share_minor"]
            else:
                ops_open[key] = ops_open.get(key, 0) + row["share_minor"]

        confirmed: dict[tuple[str, str], int] = {}
        pending: dict[tuple[str, str], int] = {}
        adjustment_rows: dict[tuple[str, str], list[dict]] = {}
        for row in financial["confirmed"]["rows"]:
            if row["venue"] is None or row["period"] != period:
                continue
            key = (row["currency"], row["venue"])
            confirmed[key] = confirmed.get(key, 0) + row["amount_minor"]
        for row in financial["pending"]["rows"]:
            if row["period"] != period:
                continue
            key = (row["currency"], row["venue"])
            pending[key] = pending.get(key, 0) + row["amount_minor"]
        for row in financial["adjustments"]["rows"]:
            if row["period"] != period or row["venue"] is None:
                continue
            key = (row["currency"], row["venue"])
            adjustment_rows.setdefault(key, []).append(row)

        blocked_orders = {
            row["order_id"] for row in financial["blocked"]["rows"]
        }
        adjusted_orders = {
            row["order_id"]
            for rows in adjustment_rows.values()
            for row in rows
        }
        order_level_rows = [
            dict(row)
            for row in financial["confirmed"]["rows"]
            if row["venue"] is None
            and (row["period"] == period or row["order_id"] in adjusted_orders)
        ]
        keys = sorted(
            set(ops_total)
            | set(usage_count)
            | set(confirmed)
            | set(pending)
            | set(adjustment_rows)
        )
        rows = []
        for currency, venue in keys:
            key = (currency, venue)
            ops_amount = ops_total.get(key, 0)
            confirmed_amount = confirmed.get(key, 0)
            pending_amount = pending.get(key, 0)
            rows_for_venue = adjustment_rows.get(key, [])
            adjustment_amount = sum(row["amount_minor"] for row in rows_for_venue)
            financial_amount = confirmed_amount + pending_amount + adjustment_amount
            difference = ops_amount - financial_amount

            reasons: list[dict] = []
            if blocked_count.get(key, 0):
                reasons.append(
                    {
                        "reason": "协议冲突/缺失：该月部分到馆未分配",
                        "amount_minor": 0,
                        "detail": f"{blocked_count[key]} 次到馆被阻断，运营只计次数；"
                        f"涉及订单 {'、'.join(sorted(blocked_orders))}",
                    }
                )
            closed_gap = ops_closed.get(key, 0) - confirmed_amount
            if closed_gap:
                reasons.append(
                    {
                        "reason": "已关账订单：本期到馆重算份额与当时确认金额之差",
                        "amount_minor": closed_gap,
                        "detail": "原单不改；补录/退款造成的差额列入后续调整单",
                    }
                )
            open_gap = ops_open.get(key, 0) - pending_amount
            if open_gap:
                reasons.append(
                    {
                        "reason": "未关账订单：到馆月与本财务账期的时点差",
                        "amount_minor": open_gap,
                        "detail": "使用份额按到馆日记在运营侧，应付只在当前账期列示",
                    }
                )
            for row in rows_for_venue:
                reasons.append(
                    {
                        "reason": f"本账期调整单（原账期 {row['origin_period']}）："
                        f"{row['reason']}",
                        "amount_minor": -row["amount_minor"],
                        "detail": f"对应原单 {row['order_id']}；调整额 {row['amount_minor']}",
                    }
                )
            explained = sum(item["amount_minor"] for item in reasons)
            if explained != difference:
                reasons.append(
                    {
                        "reason": "其他重算差异（补录/退款/取整）",
                        "amount_minor": difference - explained,
                        "detail": "请按订单明细核对",
                    }
                )
            rows.append(
                {
                    "currency": currency,
                    "venue": venue,
                    "period": period,
                    "ops_usage_count": usage_count.get(key, 0),
                    "ops_blocked_usage_count": blocked_count.get(key, 0),
                    "ops_share_minor": ops_amount,
                    "financial_confirmed_minor": confirmed_amount,
                    "financial_pending_minor": pending_amount,
                    "financial_adjustment_minor": adjustment_amount,
                    "financial_amount_minor": financial_amount,
                    "difference_minor": difference,
                    "difference_reasons": reasons,
                    "explained_ok": sum(item["amount_minor"] for item in reasons)
                    == difference,
                }
            )
        for row in order_level_rows:
            if row["order_id"] in adjusted_orders:
                row["venue_explanation"] = (
                    "本期已按补录使用重新归属，逐馆调整见上；"
                    "原订单级金额由冲抵行结清"
                )
            else:
                row["venue_explanation"] = (
                    "尚无馆方使用记录，保留订单级，不向任何馆方分摊"
                )

        return {
            "period": period,
            "current_period": current,
            "rows": rows,
            "order_level_confirmed": order_level_rows,
            "notes": "差异按馆、按币种分列原因；订单级已确认金额单列，"
            "未归属、阻断与已确认不混入同一合计。",
        }

    # -------------------------------------------------------------- 守恒
    def conservation(self, as_of: Optional[date] = None) -> list[dict]:
        totals: dict[str, dict[str, int]] = {}
        for order_id in sorted(self.data.orders):
            order = self.data.orders[order_id]
            model = self.order_model(order_id, cutoff=as_of)
            bucket = totals.setdefault(
                order.currency,
                {"gross": 0, "refunded": 0, "venue": 0, "unallocated": 0},
            )
            bucket["gross"] += order.gross_minor
            bucket["refunded"] += model.refunded_minor
            bucket["venue"] += sum(model.venue_shares.values())
            bucket["unallocated"] += model.unallocated_minor
        rows = []
        for currency, bucket in sorted(totals.items()):
            rows.append(
                {
                    "currency": currency,
                    "gross_minor": bucket["gross"],
                    "refunded_minor": bucket["refunded"],
                    "venue_total_minor": bucket["venue"],
                    "unallocated_minor": bucket["unallocated"],
                    "identity_ok": bucket["gross"]
                    == bucket["refunded"] + bucket["venue"] + bucket["unallocated"],
                }
            )
        return rows

    # -------------------------------------------------------------- 汇总
    def analyze(
        self,
        current_period: Optional[str] = None,
        as_of: Optional[date] = None,
        ops_period: Optional[str] = None,
    ) -> dict:
        current = current_period or self.current_period
        models = [
            self.order_model(order_id, cutoff=as_of)
            for order_id in sorted(self.data.orders)
        ]
        agreement_rows = []
        for model in models:
            for match in model.agreement_matches:
                agreement_rows.append(
                    {
                        "event_id": match.event_id,
                        "order_id": match.order_id,
                        "channel": match.channel,
                        "product": match.product,
                        "venue": match.venue,
                        "on_date": match.on_date.isoformat(),
                        "candidates": [
                            agreement.agreement_id for agreement in match.candidates
                        ],
                        "chosen": match.chosen.agreement_id if match.chosen else None,
                        "status": match.status,
                        "basis_points": match.chosen.basis_points
                        if match.chosen
                        else None,
                        "explanation": self._agreement_explanation(match),
                    }
                )
        ops = self.ops_view(as_of=as_of)
        target = ops_period or current
        return {
            "meta": {
                "as_of": as_of.isoformat() if as_of else "全量",
                "current_period": current,
                "ops_period": target,
                "unit": "金额均为币种最小单位（分）",
                "orders": len(models),
                "currencies": self.data.currencies(),
                "agreement_boundary": "协议起始日含、结束日不含；同一核销命中多份同等具体协议即阻断",
            "principal_rule": "可分配本金 = 售价 − 退款；各馆份额 = 可分配本金 × 协议比例"
            "（逐笔向下取整，零头留在未归属）；同馆同单重复入馆不重复计提",
            },
            "warnings": self.warnings,
            "orders": [self._order_row(model) for model in models],
            "sales": self.sales_view(),
            "conservation": self.conservation(as_of=as_of),
            "agreements": agreement_rows,
            "ticket_events": self.ticket_events_view(as_of=as_of),
            "financial": self.financial_view(current_period=current, as_of=as_of),
            "recognition_detail": [
                self.recognition_detail(close)
                for order_id in sorted(self.data.orders)
                for close in self._closed.get(order_id, [])
                if as_of is None or close.closed_at <= as_of
            ],
            "operations": ops,
            "reconciliation": self.reconcile(
                target, current_period=current, as_of=as_of
            ),
        }

    @staticmethod
    def _order_row(model: OrderModel) -> dict:
        order = model.order
        return {
            "order_id": order.order_id,
            "channel": order.channel,
            "product": order.product,
            "sold_at": order.sold_at.isoformat(),
            "currency": order.currency,
            "gross_minor": order.gross_minor,
            "refunded_minor": model.refunded_minor,
            "allocatable_minor": model.allocatable_minor,
            "venue_shares": [
                {
                    "venue": venue,
                    "amount_minor": model.venue_shares[venue],
                    "rate_basis_points": model.venue_rates.get(venue, 0),
                    "cap_minor": model.venue_caps.get(venue, 0),
                    "rounding_minor": sum(
                        use.rounding_minor
                        for use in model.usages
                        if use.event.venue == venue
                    ),
                    "cap_reduction_minor": sum(
                        use.cap_reduction_minor
                        for use in model.usages
                        if use.event.venue == venue
                    ),
                }
                for venue in sorted(model.venue_shares)
            ],
            "unallocated_minor": model.unallocated_minor,
            "unallocated_reason": model.unallocated_reason,
            "unallocated_breakdown": dict(model.unallocated_breakdown),
            "funds": model.funds(),
            "identity_ok": model.identity_holds(),
            "blocked": model.blocked,
            "problems": model.problems,
            "warnings": model.warnings,
            "cutoff": model.cutoff_label,
            "allocation_trace": model.allocation_trace,
        }

    @staticmethod
    def _agreement_explanation(match: AgreementMatch) -> str:
        if match.status == "missing":
            return (
                "该订单的渠道/票种/到馆日期没有适用协议；"
                "其他馆或渠道的协议不覆盖本次核销，结算被阻止"
            )
        if match.status == "overlap":
            ids = "、".join(a.agreement_id for a in match.candidates)
            return f"同一次核销命中多份同等具体程度的协议 {ids}，结算被阻止"
        ids = "、".join(agreement.agreement_id for agreement in match.candidates)
        if match.status == "specificity":
            return (
                f"命中 {ids}；渠道专属协议优先于通配协议，"
                f"选中 {match.chosen.agreement_id}"
            )
        return f"唯一适用协议 {match.chosen.agreement_id}"


def _sum_rows(rows: Iterable[dict]) -> dict:
    totals: dict[tuple[str, str], int] = {}
    for row in rows:
        if row.get("amount_minor") is None or row.get("venue") is None:
            continue
        key = (row["currency"], row["venue"])
        totals[key] = totals.get(key, 0) + row["amount_minor"]
    return {
        f"{currency}:{venue}": amount
        for (currency, venue), amount in sorted(totals.items())
    }
