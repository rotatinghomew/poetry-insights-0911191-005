"""输出：稳定的 JSON 与可读文本报告（供运营与结算共同复核）。"""

from __future__ import annotations

import json


def render_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)


def _money(value) -> str:
    if value is None:
        return "-"
    return f"{value:,}"


def _venue_rows(rows, period=None) -> list[str]:
    lines = []
    for row in rows:
        if period is not None and row.get("period") != period:
            continue
        venue = row.get("venue") or "（订单级/未归属）"
        amount = _money(row.get("amount_minor"))
        note = row.get("note") or row.get("reason") or ""
        origin = row.get("origin_period")
        origin_text = f"（原账期 {origin}）" if origin else ""
        basis_points = row.get("basis_points")
        rate_text = f" {basis_points} bp" if basis_points else ""
        due = row.get("settlement_due")
        if due:
            rate_text += f" 结算时点 {due}"
        lines.append(
            f"  - {venue} {row.get('currency', '')} {amount}{rate_text} "
            f"[{row.get('status', '')}]{origin_text} {note}".rstrip()
        )
    return lines


def render_text(report: dict) -> str:
    meta = report["meta"]
    lines = [
        "诗词联票 · 核销归属与跨期结算分析",
        f"口径：{meta['unit']}；数据截至 {meta['as_of']}；"
        f"当前账期 {meta['current_period']}；运营对账期 {meta['ops_period']}",
        "",
    ]

    if report["warnings"]:
        lines.append("数据告警")
        for warning in report["warnings"]:
            lines.append(f"  - {warning['message']}")
        lines.append("")

    lines.append("一、订单本金与馆方份额（全量重算）")
    for order in report["orders"]:
        flag = "  【禁止结算】" if order["blocked"] else ""
        lines.append(
            f"  {order['order_id']} {order['product']} / {order['channel']} / "
            f"售价 {_money(order['gross_minor'])} / 退款 {_money(order['refunded_minor'])} / "
            f"可分配 {_money(order['allocatable_minor'])}{flag}"
        )
        for share in order["venue_shares"]:
            lines.append(
                f"    · {share['venue']} 份额 {_money(share['amount_minor'])}"
                f"（协议比例 {share['rate_basis_points']} bp，"
                f"协议上限 {_money(share['cap_minor'])}，"
                f"分配舍入 {share['rounding_minor']} 分，封顶 {share['cap_reduction_minor']} 分）"
            )
        if order["unallocated_minor"]:
            reason = order["unallocated_reason"]
            detail = ""
            if reason not in {"no_usage", "blocked"}:
                breakdown = order["unallocated_breakdown"]
                detail = "：" + "，".join(
                    f"{label}{_money(breakdown[key])}"
                    for key, label in (
                        ("uncontracted", "未签约比例 "),
                        ("rounding", "取整零头 "),
                        ("cap", "封顶余额 "),
                    )
                    if breakdown.get(key)
                )
            lines.append(
                f"    · 暂未归属 {_money(order['unallocated_minor'])}"
                f"（{reason}{detail}）"
            )
        for problem in order["problems"]:
            lines.append(f"    ! {problem}")
        for warning in order["warnings"]:
            lines.append(f"    ? {warning}")
        trace = [
            step
            for step in order["allocation_trace"]
            if step["step"] in {"oversubscribed", "identity"}
        ]
        for step in trace:
            if step["step"] == "oversubscribed":
                lines.append(
                    f"    ! 协议比例合计 {step['total_basis_points']} bp，"
                    f"按 {step['scale']} 统一压缩"
                )
    lines.append("")

    lines.append("二、协议适用解释（同一次核销为何在不同订单上分成不同）")
    if not report["agreements"]:
        lines.append("  （无核销记录）")
    for row in report["agreements"]:
        chosen = row["chosen"] or "无（阻断）"
        lines.append(
            f"  {row['event_id']} {row['order_id']} {row['product']}/{row['channel']} "
            f"{row['venue']} {row['on_date']} → {chosen} "
            f"({row['basis_points']} bp) [{row['status']}]"
        )
        lines.append(f"    {row['explanation']}")
    lines.append("")

    sales = report["sales"]
    lines.append(f"三、{sales['label']}")
    for row in sales["rows"]:
        flag = "" if row["consistent"] else "  ! 售出记录与订单售价不一致"
        lines.append(
            f"  {row['period']} {row['order_id']} {row['channel']} "
            f"{_money(row['gross_minor'])} {row['currency']}{flag}"
        )
    if sales["by_period"]:
        lines.append(
            "    分币种/期间合计："
            + "；".join(f"{k}={_money(v)}" for k, v in sales["by_period"].items())
        )
    lines.append("")

    lines.append("四、票务事件链（换票不重复分配本金；换票前入馆保留）")
    lines.append("  收款按售出日、入馆按真实到馆日、调整按本账期分别归期，互不改写。")
    for row in report["ticket_events"]:
        ticket = row["old_ticket"] or "-"
        if row["new_ticket"]:
            ticket = f"{ticket} → {row['new_ticket']}"
        venue = f" {row['venue']}" if row["venue"] else ""
        recorded = f" 记录于 {row['recorded_at']}" if row["recorded_at"] else ""
        lines.append(
            f"  {row['event_date']} {row['order_id']} {row['event_id']} {row['event']} "
            f"{ticket}{venue} {_money(row['amount_minor'])} {row['currency']}{recorded}"
        )
    lines.append("")

    fin = report["financial"]
    lines.append(f"五、财务结算视图（当前账期 {fin['current_period']}）")
    lines.append(
        "  已确认=原账期锁定；待确认=仅登记份额；调整=关账后补录/退款；"
        "未归属与依据待补单列，互不合并。"
    )
    for key in (
        "confirmed",
        "pending",
        "adjustments",
        "unallocated",
        "unallocated_movements",
        "basis_pending",
    ):
        bucket = fin[key]
        lines.append(f"  {bucket['label']}")
        rows = _venue_rows(bucket["rows"])
        lines.extend(rows or ["  - （无）"])
        totals = bucket.get("by_currency_venue")
        if totals:
            lines.append(
                "    分币种合计：" + "；".join(f"{k}={_money(v)}" for k, v in totals.items())
            )
    lines.append(f"  {fin['blocked']['label']}")
    blocked = [
        f"  - {row['order_id']} {row['currency']} [{row['status']}] {row['reason']}"
        for row in fin["blocked"]["rows"]
    ]
    lines.extend(blocked or ["  - （无）"])
    lines.append(f"  {fin['notes']}")
    lines.append("")

    ops = report["operations"]
    lines.append("六、运营视图（按真实到馆日期，含周报口径）")
    lines.append(f"  {ops['label']}")
    for row in ops["weekly"]:
        blocked_note = (
            f"，其中 {row['blocked_usage_count']} 次因协议问题未分配"
            if row["blocked_usage_count"]
            else ""
        )
        lines.append(
            f"  - {row['week']} {row['venue']} 到馆 {row['usage_count']} 次，"
            f"份额 {_money(row['share_minor'])} {row['currency']}{blocked_note}"
        )
        for day in ops["daily"]:
            if (
                day["week"] == row["week"]
                and day["venue"] == row["venue"]
                and day["repeat_of"]
            ):
                lines.append(
                    f"      · {day['visit_date']} {day['event_id']} 为重复到馆，"
                    f"沿用 {day['repeat_of']} 的份额，不重复分配"
                )
    lines.append(f"  {ops['notes']}")
    lines.append("")

    details = report.get("recognition_detail", [])
    if details:
        lines.append("七、已确认明细复核（按关账当时依据，对方可重放）")
        for detail in details:
            basis = "、".join(
                f"{venue} {_money(amount)}"
                for venue, amount in sorted(detail["by_venue"].items())
            ) or "（订单级，无馆方依据）"
            lines.append(
                f"  - {detail['order_id']} 原账期 {detail['period']} "
                f"确认 {_money(detail['confirmed_minor'])} {detail['basis_cutoff']}："
                f"{basis}"
            )
            for use in detail["usages"]:
                recorded = (
                    f" 记录于 {use['recorded_at']}" if use["recorded_at"] else ""
                )
                lines.append(
                    f"      · {use['visit_date']} {use['event_id']} {use['venue']} "
                    f"{use['agreement_id']} {use['basis_points']} bp "
                    f"份额 {_money(use['share_minor'])}{recorded}"
                )
        lines.append("")

    rec = report["reconciliation"]
    lines.append(
        f"八、运营 vs 财务对账（期间 {rec['period']}，财务当前账期 {rec['current_period']}）"
    )
    if not rec["rows"]:
        lines.append("  - （该期间无记录）")
    for row in rec["rows"]:
        lines.append(
            f"  - {row['venue']} {row['currency']} 运营份额 "
            f"{_money(row['ops_share_minor'])}（到馆 {row['ops_usage_count']} 次）"
            f" / 财务 = 确认 {_money(row['financial_confirmed_minor'])}"
            f" + 待确认 {_money(row['financial_pending_minor'])}"
            f" + 调整 {_money(row['financial_adjustment_minor'])}"
            f" = {_money(row['financial_amount_minor'])}"
            f"；差异 {_money(row['difference_minor'])}"
        )
        for reason in row["difference_reasons"]:
            lines.append(
                f"      · {reason['reason']} {_money(reason['amount_minor'])}"
                f" — {reason['detail']}"
            )
    for row in rec.get("order_level_confirmed", []):
        lines.append(
            f"  - 订单级已确认 {row['order_id']} {row['currency']} "
            f"{_money(row['amount_minor'])}（原账期 {row['period']}）："
            f"{row.get('venue_explanation', '')}"
        )
    lines.append(f"  {rec['notes']}")
    return "\n".join(lines)
