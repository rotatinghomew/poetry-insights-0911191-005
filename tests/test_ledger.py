"""核心计算与守恒测试。"""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ticket_ledger.engine import Ledger
from ticket_ledger.loaders import LoadError, load_dataset

ORDER_HEADER = ["order_id", "channel", "product", "sold_at", "currency", "gross_minor"]
EVENT_HEADER = [
    "event_id",
    "order_id",
    "old_ticket",
    "new_ticket",
    "event",
    "event_date",
    "amount_minor",
    "venue",
]
AGREEMENT_HEADER = [
    "agreement_id",
    "venue",
    "product",
    "channel",
    "start_date",
    "end_date",
    "basis_points",
    "cap_minor",
]
CLOSE_HEADER = ["period", "closed_at", "order_id", "amount_minor", "status"]


def build(
    tmp: Path,
    orders: list[list],
    events: list[list],
    agreements: list[list],
    closes: list[list] | None = None,
    *,
    event_header: list[str] | None = None,
) -> Ledger:
    data = tmp / "data"
    data.mkdir(exist_ok=True)
    header = event_header or EVENT_HEADER
    if any(len(row) > len(header) for row in events):
        header = EVENT_HEADER + ["recorded_at"]
    close_rows = closes or []
    close_header = CLOSE_HEADER
    if any(len(row) > len(close_header) for row in close_rows):
        close_header = CLOSE_HEADER + ["basis_as_of"]
    for name, header, rows in (
        ("orders.csv", ORDER_HEADER, orders),
        ("ticket_events.csv", header, events),
        ("agreements.csv", AGREEMENT_HEADER, agreements),
        ("closes.csv", close_header, close_rows),
    ):
        with (data / name).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            for row in rows:
                writer.writerow(list(row) + [""] * (len(header) - len(row)))
    return Ledger(load_dataset(data))


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class FixtureTests(LedgerTestCase):
    def test_shipped_fixture_is_conserved_and_explained(self):
        ledger = Ledger(load_dataset(Path(__file__).resolve().parents[1] / "data"))
        report = ledger.analyze()
        for row in report["conservation"]:
            self.assertTrue(row["identity_ok"], row)
        self.assertEqual(report["meta"]["current_period"], "2026-11")
        agreements = {row["event_id"]: row for row in report["agreements"]}
        self.assertEqual(agreements["T03"]["chosen"], "A1")
        self.assertEqual(agreements["T04"]["chosen"], "A2")
        # O02 在关账后才收到部分退款，且始终没有核销：不得向任何馆方分摊。
        basis_pending = report["financial"]["basis_pending"]["rows"]
        self.assertEqual([row["order_id"] for row in basis_pending], ["O02"])
        self.assertEqual(basis_pending[0]["amount_minor"], 2800)
        self.assertEqual(report["financial"]["blocked"]["rows"], [])


class PrincipalTests(LedgerTestCase):
    def test_exchange_keeps_pre_exchange_visit_and_does_not_double_principal(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-10-10", 0, "A"],
                ["E3", "O1", "T1", "T2", "换票", "2026-10-15", 0, ""],
                ["E4", "O1", "T2", "", "核销", "2026-10-20", 0, "B"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 5000, 100000],
                ["G2", "B", "P", "*", "2026-10-01", "2027-01-01", 3000, 100000],
            ],
        )
        model = ledger.order_model("O1")
        self.assertFalse(model.blocked)
        self.assertTrue(model.identity_holds())
        self.assertEqual(model.venue_shares, {"A": 5000, "B": 3000})
        self.assertEqual(model.unallocated_minor, 2000)
        self.assertEqual(model.usage_count(), 2)

    def test_using_old_ticket_after_exchange_blocks_order(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "T2", "换票", "2026-10-15", 0, ""],
                ["E3", "O1", "T1", "", "核销", "2026-10-20", 0, "A"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 5000, 100000],
            ],
        )
        model = ledger.order_model("O1")
        self.assertTrue(model.blocked)
        self.assertEqual(model.venue_shares, {})
        self.assertTrue(any("已换出" in problem for problem in model.problems))

    def test_same_venue_repeat_visit_counts_once(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-10-10", 0, "A"],
                ["E3", "O1", "T1", "", "核销", "2026-10-20", 0, "A"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 5000, 100000],
            ],
        )
        model = ledger.order_model("O1")
        self.assertEqual(model.venue_shares, {"A": 5000})
        self.assertEqual(model.usage_count(), 2)
        self.assertEqual(model.usages[1].repeat_of, "E2")

    def test_refund_reduces_every_venue_proportionally(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-10-10", 0, "A"],
                ["E3", "O1", "T1", "", "核销", "2026-10-11", 0, "B"],
                ["E4", "O1", "T1", "", "部分退款", "2026-11-01", 2000, ""],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 5000, 100000],
                ["G2", "B", "P", "*", "2026-10-01", "2027-01-01", 3000, 100000],
            ],
        )
        model = ledger.order_model("O1")
        # 8,000 × 50% 与 8,000 × 30%，两馆按各自协议同步下降。
        self.assertEqual(model.venue_shares, {"A": 4000, "B": 2400})
        self.assertEqual(model.refunded_minor, 2000)
        self.assertTrue(model.identity_holds())

    def test_rounding_remainder_stays_unallocated_and_is_traceable(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-10-10", 0, "A"],
                ["E3", "O1", "T1", "", "核销", "2026-10-11", 0, "B"],
                ["E4", "O1", "T1", "", "核销", "2026-10-12", 0, "C"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 3333, 100000],
                ["G2", "B", "P", "*", "2026-10-01", "2027-01-01", 3333, 100000],
                ["G3", "C", "P", "*", "2026-10-01", "2027-01-01", 3333, 100000],
            ],
        )
        model = ledger.order_model("O1")
        self.assertEqual(model.venue_shares, {"A": 3333, "B": 3333, "C": 3333})
        self.assertEqual(model.unallocated_minor, 1)
        # 33.33% × 3 = 99.99%，那 1 分来自未签约的 0.01%，不是取整尾差。
        self.assertEqual(model.unallocated_breakdown["uncontracted"], 1)
        self.assertEqual(model.unallocated_breakdown["rounding"], 0)
        self.assertTrue(model.identity_holds())
        claims = [
            step
            for step in model.allocation_trace
            if step["step"] == "claims"
        ][0]
        self.assertEqual(claims["integer_total"], 9999)
        self.assertEqual(claims["exact_total"], "9999")

    def test_cap_does_not_shift_money_to_other_venues(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-10-10", 0, "A"],
                ["E3", "O1", "T1", "", "核销", "2026-10-11", 0, "B"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 4500, 3000],
                ["G2", "B", "P", "*", "2026-10-01", "2027-01-01", 3000, 100000],
            ],
        )
        model = ledger.order_model("O1")
        self.assertEqual(model.venue_shares, {"A": 3000, "B": 3000})
        self.assertEqual(model.unallocated_breakdown["cap"], 1500)
        self.assertEqual(model.unallocated_minor, 4000)
        self.assertTrue(model.identity_holds())

    def test_oversubscribed_rates_are_compressed_evenly(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-10-10", 0, "A"],
                ["E3", "O1", "T1", "", "核销", "2026-10-11", 0, "B"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 7000, 100000],
                ["G2", "B", "P", "*", "2026-10-01", "2027-01-01", 6000, 100000],
            ],
        )
        model = ledger.order_model("O1")
        # 压缩到 100% 后逐笔向下取整，尾差 1 分留在未归属。
        self.assertEqual(sum(model.venue_shares.values()), 9999)
        self.assertEqual(model.unallocated_breakdown["rounding"], 1)
        self.assertAlmostEqual(
            model.venue_shares["A"] / 10000, 7 / 13, places=3
        )
        self.assertTrue(any("超过 100%" in warning for warning in model.warnings))


class AgreementTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.ledger = build(
            self.tmp,
            orders=[
                ["O1", "官方", "P", "2026-10-01", "CNY", 10000],
                ["O2", "合作方甲", "P", "2026-10-01", "CNY", 10000],
            ],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-10-10", 0, "A"],
                ["E3", "O2", "T2", "", "售出", "2026-10-01", 10000, ""],
                ["E4", "O2", "T2", "", "核销", "2026-10-10", 0, "A"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 4000, 100000],
                ["G2", "A", "P", "官方", "2026-10-01", "2027-01-01", 5200, 100000],
            ],
        )

    def test_channel_specific_agreement_explains_same_visit_different_split(self):
        official = self.ledger.order_model("O1")
        partner = self.ledger.order_model("O2")
        self.assertEqual(official.venue_shares["A"], 5200)
        self.assertEqual(partner.venue_shares["A"], 4000)
        match = official.agreement_matches[0]
        self.assertEqual(match.status, "specificity")
        self.assertEqual(match.chosen.agreement_id, "G2")

    def test_overlap_blocks_instead_of_picking_a_version(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-10-10", 0, "A"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 4000, 100000],
                ["G2", "A", "P", "*", "2026-09-01", "2026-12-01", 4500, 100000],
            ],
        )
        model = ledger.order_model("O1")
        self.assertTrue(model.blocked)
        self.assertEqual(model.venue_shares, {})
        self.assertEqual(
            ledger.financial_view()["blocked"]["rows"][0]["order_id"], "O1"
        )

    def test_missing_agreement_blocks_and_is_not_auto_selected(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-12-01", 0, "A"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2026-11-01", 4000, 100000],
            ],
        )
        model = ledger.order_model("O1")
        self.assertTrue(model.blocked)
        self.assertTrue(any("缺少适用协议" in p for p in model.problems))

    def test_agreement_end_date_is_exclusive(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-11-01", 0, "A"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2026-11-01", 4000, 100000],
                ["G2", "A", "P", "*", "2026-11-01", "2027-01-01", 4500, 100000],
            ],
        )
        match = ledger.order_model("O1").agreement_matches[0]
        self.assertEqual(match.status, "ok")
        self.assertEqual(match.chosen.agreement_id, "G2")


class CloseAndAdjustmentTests(LedgerTestCase):
    def _ledger(self, closes, *extra_events):
        events = [
            ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
            ["E2", "O1", "T1", "", "核销", "2026-10-10", 0, "A"],
            ["E3", "O1", "T1", "", "核销", "2026-10-20", 0, "B"],
        ] + list(extra_events)
        return build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=events,
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 5000, 100000],
                ["G2", "B", "P", "*", "2026-10-01", "2027-01-01", 3000, 100000],
                ["G3", "C", "P", "*", "2026-10-01", "2027-01-01", 2000, 100000],
            ],
            closes=closes,
        )

    def test_confirmed_amount_uses_close_time_basis(self):
        ledger = self._ledger(
            [["2026-10", "2026-11-05", "O1", 8000, "已确认"]],
            [
                "E4", "O1", "T1", "", "核销", "2026-10-28", 0, "C",
                "2026-11-10",
            ],
        )
        # C 馆的到馆发生在账期内，但记录 11-10 才收到：关账依据不应包含。
        rows = ledger.financial_view()["confirmed"]["rows"]
        self.assertEqual({row["venue"] for row in rows}, {"A", "B"})
        self.assertEqual(
            sum(row["amount_minor"] for row in rows), 8000
        )

    def test_late_use_creates_adjustment_and_keeps_old_order(self):
        ledger = self._ledger(
            [["2026-10", "2026-11-05", "O1", 8000, "已确认"]],
            ["E4", "O1", "T1", "", "核销", "2026-10-28", 0, "C", "2026-11-10"],
        )
        view = ledger.financial_view()
        self.assertEqual(len(view["confirmed"]["rows"]), 2)
        adjustments = view["adjustments"]["rows"]
        self.assertEqual(len(adjustments), 1)
        row = adjustments[0]
        self.assertEqual(row["order_id"], "O1")
        self.assertEqual(row["origin_period"], "2026-10")
        self.assertEqual(row["venue"], "C")
        self.assertEqual(row["amount_minor"], 2000)
        # 原确认的 A/B 不受影响（份额未变），旧单仍在 confirmed。
        self.assertEqual(
            {row["venue"]: row["amount_minor"] for row in view["confirmed"]["rows"]},
            {"A": 5000, "B": 3000},
        )

    def test_post_close_refund_shared_by_all_venues_without_editing_old_close(self):
        ledger = self._ledger(
            [["2026-10", "2026-11-05", "O1", 8000, "已确认"]],
            ["E4", "O1", "T1", "", "部分退款", "2026-11-12", 1000, ""],
        )
        view = ledger.financial_view()
        adjustments = {
            row["venue"]: row["amount_minor"] for row in view["adjustments"]["rows"]
        }
        # 可分配 9,000：A 4,500、B 2,700；较原确认 A −500、B −300。
        self.assertEqual(adjustments, {"A": -500, "B": -300})
        confirmed = {
            row["venue"]: row["amount_minor"] for row in view["confirmed"]["rows"]
        }
        self.assertEqual(confirmed, {"A": 5000, "B": 3000})
        self.assertTrue(
            all(
                row["origin_period"] == "2026-10" and row["order_id"] == "O1"
                for row in view["adjustments"]["rows"]
            )
        )

    def test_closed_order_without_usage_is_not_reallocated_after_refund(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "部分退款", "2026-11-12", 2000, ""],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 5000, 100000],
            ],
            closes=[["2026-10", "2026-11-05", "O1", 3000, "已确认"]],
        )
        view = ledger.financial_view()
        self.assertEqual(view["adjustments"]["rows"], [])
        pending = view["basis_pending"]["rows"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["amount_minor"], 3000)
        self.assertEqual(pending[0]["status"], "依据待补")

    def test_close_basis_as_of_pins_statement_to_original_evidence(self):
        # 同一份关账数据，补录记录先后不同，只要 basis_as_of 固定，
        # 对方收到的明细都能按当时依据复核出同一结果。
        data = [
            ["2026-10", "2026-11-05", "O1", 8000, "已确认", "2026-11-05"],
        ]
        ledger = self._ledger(
            data,
            ["E4", "O1", "T1", "", "核销", "2026-10-28", 0, "C", "2026-11-10"],
        )
        rows = ledger.financial_view()["confirmed"]["rows"]
        self.assertEqual(
            {row["venue"]: row["amount_minor"] for row in rows},
            {"A": 5000, "B": 3000},
        )

    def test_order_level_close_is_reallocated_with_explicit_offset(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-10-20", 0, "A", "2026-11-15"],
                ["E3", "O1", "T1", "", "部分退款", "2026-11-03", 2000, ""],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 4000, 100000],
            ],
            closes=[["2026-10", "2026-11-05", "O1", 2800, "已确认"]],
        )
        view = ledger.financial_view()
        self.assertEqual(
            view["confirmed"]["rows"][0]["status"],
            "已确认待付（订单级，使用记录未交齐）",
        )
        adjustments = view["adjustments"]["rows"]
        by_key = {(row["venue"], row["status"]): row for row in adjustments}
        venue_row = by_key[("A", "调整待确认")]
        offset_row = by_key[(None, "调整待确认（订单级冲抵）")]
        # 退款后可分配本金 8,000，A 馆 8,000 × 40% = 3,200；
        # 原订单级 2,800 由冲抵行结清，不并入该馆调整额。
        self.assertEqual(venue_row["amount_minor"], 3200)
        self.assertEqual(offset_row["amount_minor"], -2800)
        self.assertEqual(venue_row["origin_period"], "2026-10")

    def test_sales_flow_is_reported_by_sale_date(self):
        report = self._ledger(
            [["2026-10", "2026-11-05", "O1", 8000, "已确认"]],
        ).analyze()
        sales = report["sales"]["rows"][0]
        self.assertEqual(sales["period"], "2026-10")
        self.assertEqual(sales["gross_minor"], 10000)
        self.assertTrue(sales["consistent"])

    def test_statuses_are_separate_buckets(self):
        ledger = self._ledger(
            [["2026-10", "2026-11-05", "O1", 8000, "已确认"]],
            ["E4", "O1", "T1", "", "核销", "2026-10-28", 0, "C"],
        )
        view = ledger.financial_view()
        labels = {
            key: view[key]["label"]
            for key in (
                "confirmed",
                "pending",
                "adjustments",
                "unallocated",
                "basis_pending",
                "blocked",
            )
        }
        self.assertEqual(len(set(labels.values())), len(labels))
        # 调整与未归属说明不混在一起。
        self.assertNotIn(
            "未归属",
            view["adjustments"]["label"],
        )
        self.assertNotEqual(
            view["unallocated"]["label"], view["adjustments"]["label"]
        )


class SettlementTimingTests(LedgerTestCase):
    def test_agreement_lag_marks_pending_as_not_yet_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            for name, header, rows in (
                (
                    "orders.csv",
                    ORDER_HEADER,
                    [["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
                ),
                (
                    "ticket_events.csv",
                    EVENT_HEADER,
                    [
                        ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                        ["E2", "O1", "T1", "", "核销", "2026-10-10", 0, "A"],
                    ],
                ),
                (
                    "agreements.csv",
                    AGREEMENT_HEADER + ["settlement_lag_days"],
                    [["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 5000, 100000, 30]],
                ),
                ("closes.csv", CLOSE_HEADER, []),
            ):
                with (data / name).open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(header)
                    writer.writerows(rows)
            ledger = Ledger(load_dataset(data))
            row = ledger.financial_view(as_of=date(2026, 10, 20))["pending"]["rows"][0]
            self.assertEqual(row["settlement_status"], "未到协议结算时点")
            self.assertEqual(row["settlement_due"], "2026-11-09")
            # 即便协议时点已到，也只到"待财务确认"，不会自动变成已确认待付。
            later = ledger.financial_view(as_of=date(2026, 12, 1))
            self.assertEqual(later["confirmed"]["rows"], [])
            self.assertEqual(
                later["pending"]["rows"][0]["settlement_status"],
                "已到协议结算时点，待财务关账",
            )


class OpsTests(LedgerTestCase):
    def test_ops_uses_real_visit_dates_and_reconciles_to_finance(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-10-10", 0, "A"],
                ["E3", "O1", "T1", "", "核销", "2026-11-10", 0, "A"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 5000, 100000],
            ],
            closes=[["2026-10", "2026-11-05", "O1", 5000, "已确认"]],
        )
        ops = ledger.ops_view()
        months = {(row["period"], row["venue"]): row for row in ops["monthly"]}
        self.assertEqual(months[("2026-10", "A")]["usage_count"], 1)
        self.assertEqual(months[("2026-11", "A")]["usage_count"], 1)
        rec = ledger.reconcile("2026-11", current_period="2026-11")
        row = rec["rows"][0]
        self.assertTrue(row["explained_ok"], row)
        self.assertEqual(row["difference_minor"], 0)

    def test_reconciliation_explains_cross_period_adjustment(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 10000]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-10-20", 0, "A"],
                ["E3", "O1", "T1", "", "核销", "2026-10-28", 0, "B"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 5000, 100000],
                ["G2", "B", "P", "*", "2026-10-01", "2027-01-01", 3000, 100000],
            ],
            closes=[["2026-10", "2026-11-05", "O1", 5000, "已确认"]],
        )
        rec = ledger.reconcile("2026-11", current_period="2026-11")
        rows = {row["venue"]: row for row in rec["rows"]}
        self.assertIn("B", rows)
        self.assertTrue(rows["B"]["explained_ok"], rows["B"])
        self.assertTrue(
            any("调整单" in reason["reason"] for reason in rows["B"]["difference_reasons"])
        )


class CurrencyTests(LedgerTestCase):
    def test_conservation_is_per_currency(self):
        ledger = build(
            self.tmp,
            orders=[
                ["O1", "官方", "P", "2026-10-01", "CNY", 10000],
                ["O2", "官方", "P", "2026-10-01", "USD", 333],
            ],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 10000, ""],
                ["E2", "O1", "T1", "", "核销", "2026-10-10", 0, "A"],
                ["E3", "O2", "T2", "", "售出", "2026-10-01", 333, ""],
                ["E4", "O2", "T2", "", "核销", "2026-10-10", 0, "A"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 3333, 100000],
            ],
        )
        rows = {row["currency"]: row for row in ledger.conservation()}
        self.assertTrue(rows["CNY"]["identity_ok"])
        self.assertTrue(rows["USD"]["identity_ok"])
        self.assertEqual(rows["USD"]["venue_total_minor"], 110)

    def test_zero_rate_leaves_principal_unallocated(self):
        ledger = build(
            self.tmp,
            orders=[["O1", "官方", "P", "2026-10-01", "CNY", 100]],
            events=[
                ["E1", "O1", "T1", "", "售出", "2026-10-01", 100, ""],
                ["E2", "O1", "T1", "", "核销", "2026-10-10", 0, "A"],
            ],
            agreements=[
                ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 0, 100000],
            ],
        )
        model = ledger.order_model("O1")
        self.assertEqual(model.venue_shares, {})
        self.assertEqual(model.unallocated_minor, 100)
        self.assertEqual(model.unallocated_reason, "zero_basis")


class LoaderTests(LedgerTestCase):
    def test_rejects_inverted_agreement_range(self):
        with self.assertRaises(LoadError):
            build(
                self.tmp,
                orders=[["O1", "官方", "P", "2026-10-01", "CNY", 100]],
                events=[["E1", "O1", "T1", "", "售出", "2026-10-01", 100, ""]],
                agreements=[
                    ["G1", "A", "P", "*", "2026-12-01", "2026-10-01", 5000, 1]
                ],
            )

    def test_rejects_unknown_order_reference(self):
        with self.assertRaises(LoadError):
            build(
                self.tmp,
                orders=[["O1", "官方", "P", "2026-10-01", "CNY", 100]],
                events=[["E1", "O2", "T1", "", "售出", "2026-10-01", 100, ""]],
                agreements=[
                    ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 5000, 1]
                ],
            )

    def test_rejects_use_without_venue(self):
        with self.assertRaises(LoadError):
            build(
                self.tmp,
                orders=[["O1", "官方", "P", "2026-10-01", "CNY", 100]],
                events=[
                    ["E1", "O1", "T1", "", "售出", "2026-10-01", 100, ""],
                    ["E2", "O1", "T1", "", "核销", "2026-10-02", 0, ""],
                ],
                agreements=[
                    ["G1", "A", "P", "*", "2026-10-01", "2027-01-01", 5000, 1]
                ],
            )


if __name__ == "__main__":
    unittest.main()
