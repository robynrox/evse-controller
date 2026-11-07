"""Backtest shadow DP plans against measured outcomes.

Usage:

    python -m evse_controller.tools.dp_backtest --days 30
    python -m evse_controller.tools.dp_backtest --start 2026-10-01 --end 2026-10-31
    python -m evse_controller.tools.dp_backtest --days 30 --json

The tool is read-only: it replays the first recorded plan of each day against
the measured house load and frozen prices, then compares the planner's cost
with the heuristic's actual cost.  Positive delta means the DP plan would have
been cheaper on grid cost than what actually happened.
"""

from __future__ import annotations

import argparse
from datetime import date
import json

from evse_controller.planner.backtest import (
    backtest_days,
    summarise,
)


def _fmt_money(value) -> str:
    if value is None:
        return "—"
    sign = "−" if value < 0 else ""
    return f"{sign}£{abs(value):.2f}"


def _fmt_pct(value) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _fmt_pp(value) -> str:
    return "—" if value is None else f"{value:+.2f}pp"


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay shadow DP plans against measured outcomes."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="number of days ending at the latest recorded plan (default 30)",
    )
    parser.add_argument("--start", type=_parse_date, default=None)
    parser.add_argument("--end", type=_parse_date, default=None)
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the result as JSON instead of a table",
    )
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    results = backtest_days(
        days=args.days,
        start=args.start,
        end=args.end,
    )
    summary = summarise(results)

    if args.json:
        payload = {
            "summary": summary,
            "days": [
                {
                    "day": row.day.isoformat(),
                    "start_slot": row.start_slot,
                    "run_ts": row.run_ts,
                    "linked_slots": row.linked_slots,
                    "skipped_slots": row.skipped_slots,
                    "dp_cost_gbp": row.dp_cost_gbp,
                    "actual_cost_gbp": row.actual_cost_gbp,
                    "delta_gbp": row.delta_gbp,
                    "soc_mae": row.soc_mae,
                    "soc_bias": row.soc_bias,
                    "action_match_rate": row.action_match_rate,
                }
                for row in results
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0 if results else 1

    if not results:
        print(
            "No shadow plans found. Enable the shadow planner and let it run "
            "for at least one slot."
        )
        return 1

    first = min(row.day for row in results)
    last = max(row.day for row in results)
    print(f"DP backtest: {first} .. {last}")
    print(
        f"days {summary['days']}  "
        f"plan cost {_fmt_money(summary['dp_cost_gbp'])}  "
        f"actual {_fmt_money(summary['actual_cost_gbp'])}  "
        f"delta {_fmt_money(summary['delta_gbp'])}"
    )
    print(
        f"SoC MAE {_fmt_pp(summary['soc_mae'])}  "
        f"bias {_fmt_pp(summary['soc_bias'])}  "
        f"action match {_fmt_pct(summary['action_match_rate'])}"
    )
    print()
    header = (
        f"{'date':10} {'start':>5} {'DP cost':>10} {'actual':>10} "
        f"{'delta':>10} {'SoC MAE':>9} {'match':>6}"
    )
    print(header)
    print("-" * len(header))
    for row in results:
        print(
            f"{row.day.isoformat():10} {row.start_slot:>5} "
            f"{_fmt_money(row.dp_cost_gbp):>10} "
            f"{_fmt_money(row.actual_cost_gbp):>10} "
            f"{_fmt_money(row.delta_gbp):>10} "
            f"{_fmt_pp(row.soc_mae):>9} "
            f"{_fmt_pct(row.action_match_rate):>6}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
