"""命令行入口：python -m ticket_ledger [--data DIR] [--period P] [--as-of D] [--json]"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .engine import Ledger, LedgerError
from .loaders import LoadError, load_dataset
from .render import render_json, render_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket_ledger",
        description="诗词联票：核销归属、协议适用与跨期结算分析",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data",
        help="CSV 数据目录（默认仓库 data/）",
    )
    parser.add_argument(
        "--period",
        default=None,
        help="运营对账期间 YYYY-MM（默认等于当前账期）",
    )
    parser.add_argument(
        "--current-period",
        default=None,
        help="财务当前账期 YYYY-MM（默认取最新关账期的下一期）",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="只使用该日期（含）之前的票务事件",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本报告")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="协议冲突/缺失以外，连业务告警（如比例超额、同馆重复核销）也返回非零",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        dataset = load_dataset(args.data)
        ledger = Ledger(dataset)
        report = ledger.analyze(
            current_period=args.current_period,
            as_of=args.as_of,
            ops_period=args.period,
        )
    except (LoadError, LedgerError) as exc:
        print(f"数据错误：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(render_json(report))
    else:
        print(render_text(report))

    fin = report["financial"]
    if args.strict and any(
        order["warnings"] for order in report["orders"]
    ):
        print("业务告警未清零（--strict）。", file=sys.stderr)
        return 1
    if fin["blocked"]["rows"]:
        print(
            "存在协议冲突/缺失的订单，已阻止结算；"
            "详见“协议冲突/缺失”一栏。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
