"""命令行入口：生成报告，供值班处与区里复核。

用法::

    python3 -m school_reserve.cli forecast --origin 21
    python3 -m school_reserve.cli standing --weather clear
    python3 -m school_reserve.cli desk --day 2026-10-05 --cutoff 560
    python3 -m school_reserve.cli promises --day 2026-10-05
    python3 -m school_reserve.cli backtest
    python3 -m school_reserve.cli strategies
    python3 -m school_reserve.cli all --out reports
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .report import (
    actual_vs_probability,
    backtest_report,
    desk_report,
    forecast_report,
    promise_report,
    standing_forecast_report,
)
from .strategies import compare_strategies, determinism_audit
from .tables import Dataset

DEFAULT_ORIGIN = 21
DEFAULT_DAY = "2026-10-05"


def _sanitize(value):
    """把 NaN / inf 换成 null，保证输出是合法 JSON。"""
    import math

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value


def _dump(payload) -> str:
    return json.dumps(_sanitize(payload), ensure_ascii=False, indent=2, sort_keys=False)


def build_all(dataset: Dataset, origin: int, day: date, weather: str) -> dict:
    return {
        "forecast": forecast_report(dataset, origin),
        "standing_forecast": standing_forecast_report(dataset, origin, weather),
        "backtest": backtest_report(dataset),
        "desk": desk_report(dataset, day, 560),
        "promises": promise_report(dataset, day),
        "actual_vs_probability": actual_vs_probability(dataset, origin, weather),
        "strategies": compare_strategies(dataset),
        "determinism": determinism_audit(dataset),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=[
        "forecast", "standing", "desk", "promises", "backtest",
        "strategies", "determinism", "all",
    ])
    parser.add_argument("--origin", type=int, default=DEFAULT_ORIGIN)
    parser.add_argument("--day", default=DEFAULT_DAY)
    parser.add_argument("--cutoff", type=int, default=560)
    parser.add_argument("--weather", default="clear", choices=["clear", "wet"])
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    dataset = Dataset.load()
    day = date.fromisoformat(args.day)

    if args.command == "forecast":
        payload = forecast_report(dataset, args.origin)
    elif args.command == "standing":
        payload = standing_forecast_report(dataset, args.origin, args.weather)
    elif args.command == "desk":
        payload = desk_report(dataset, day, args.cutoff)
    elif args.command == "promises":
        payload = promise_report(dataset, day)
    elif args.command == "backtest":
        payload = backtest_report(dataset)
    elif args.command == "strategies":
        payload = {
            "strategies": compare_strategies(dataset),
            "determinism": determinism_audit(dataset),
        }
    elif args.command == "determinism":
        payload = determinism_audit(dataset)
    else:
        payload = build_all(dataset, args.origin, day, args.weather)

    text = _dump(payload)
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        target = args.out / f"{args.command}.json"
        target.write_text(text + "\n", encoding="utf-8")
        print(target)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
