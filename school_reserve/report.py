"""值班处查询与报告渲染。

回答四类问题：
1. 本周可执行哪些援班分支，各自卡在哪；
2. 哪些信号还没收到，哪些窗口已被占用，哪些课已有真实预留；
3. 本期实际调用表为何与概率试算不是同一张表；
4. 已承诺但未兑现的课，原承诺是什么、援班为何没能动用。
"""

from __future__ import annotations

from datetime import date

from .dispatch import (
    PromiseRecord,
    availability,
    desk_view,
    exclusive_pairings,
    gap_summary,
    promise_ledger,
    release_reports,
    replay_outcomes,
)
from .forecast import (
    DayForecast,
    backtest,
    forecast_standing,
    forecast_week,
    summary,
)
from .tables import Dataset


def desk_report(dataset: Dataset, day: date, cutoff: int) -> dict:
    """某一决定点的值班处视图。"""
    view = desk_view(dataset, day, cutoff)
    return {
        "day": day.isoformat(),
        "cutoff": cutoff,
        "dispatched": [
            {
                "branch": decision.branch_id,
                "staff": decision.staff,
                "class": decision.target_class,
                "site": decision.target_site,
                "kind": decision.target_kind,
                "call_minute": decision.call_minute,
                "arrival_minute": decision.arrival_minute,
                "status": decision.status,
                "reason": decision.reason,
            }
            for decision in view.decisions
        ],
        "not_dispatched": [
            {
                "branch": decision.branch_id,
                "staff": decision.staff,
                "class": decision.target_class,
                "status": decision.status,
                "reason": decision.reason,
            }
            for decision in view.decisions
            if decision.status != "dispatched"
        ],
        "waiting": [
            {
                "branch": audit.branch_id,
                "missing_signals": list(audit.missing_sites),
                "deadline": audit.deadline,
            }
            for audit in view.waiting
        ],
        "blocked": [
            {
                "branch": audit.branch_id,
                "blockers": list(audit.blockers),
            }
            for audit in view.blocked
        ],
        "missing_signals": list(view.missing_signals),
        "occupied_windows": view.occupied_windows,
        "reservations": [
            {
                "staff": item.staff,
                "class": item.class_id,
                "source": item.source,
                "received": item.received,
                "narrowed_end": item.narrowed_end,
            }
            for item in view.reservations
        ],
        "windows": {
            staff: [
                {
                    "start": window.start,
                    "end": window.end,
                    "sites": sorted(window.sites),
                    "source": window.source,
                }
                for window in items
            ]
            for staff, items in availability(dataset, day).items()
        },
        "release_reports": release_reports(dataset, day),
        "exclusive_pairings": [
            {
                "staff": item.staff,
                "branches": [item.first, item.second],
                "shared_conditions": [list(pair) for pair in item.shared_conditions],
                "distinguishing": [list(pair) for pair in item.distinguishing],
                "observable_before_deadlines": item.observable_before_deadlines,
                "deadlines": item.deadlines,
                "detail": item.detail,
            }
            for item in exclusive_pairings(dataset, day)
        ],
    }


def promise_report(dataset: Dataset, day: date) -> list[dict]:
    return [_promise_row(record) for record in promise_ledger(dataset, day)]


def _promise_row(record: PromiseRecord) -> dict:
    return {
        "class": record.class_id,
        "site": record.site,
        "kind": record.kind,
        "window": f"{record.start}-{record.end}",
        "state": record.state,
        "promised": record.promised,
        "reservations": [
            {
                "staff": item.staff,
                "source": item.source,
                "received": item.received,
            }
            for item in record.reservations
        ],
        "executable_branches": list(record.executable_at_deadline),
        "dispatched_branches": list(record.dispatched_at_deadline),
        "blocked_branches": {
            branch: list(reasons) for branch, reasons in record.blocked_reasons.items()
        },
        "later_outcome": record.later_outcome,
        "honoured": record.honoured,
        "unused_reason": (
            None
            if record.honoured
            else _unused_reason(record)
        ),
    }


def _unused_reason(record: PromiseRecord) -> str:
    if not record.promised:
        return "not_promised"
    if record.honoured is None:
        return "outcome_not_yet_received"
    if record.dispatched_at_deadline:
        return "dispatched_but_not_honoured"
    if record.executable_at_deadline:
        return "executable_but_not_dispatched"
    reasons: set[str] = set()
    for blockers in record.blocked_reasons.values():
        reasons.update(blockers)
    if any(reason.startswith("waiting:") for reason in reasons):
        return "signal_never_received"
    if any("release_after_deadline" in reason for reason in reasons):
        return "release_came_too_late"
    if any("contradicted:" in reason for reason in reasons):
        return "condition_never_held"
    if any(
        reason
        in (
            "leave_after_class_start",
            "window_impossible",
            "arrival_after_start",
            "signal_after_deadline",
            "arrival_outside_availability",
        )
        for reason in reasons
    ):
        return "no_feasible_window"
    if any(
        reason
        in (
            "precommitted_elsewhere",
            "target_outside_availability",
            "class_outside_availability",
            "no_availability",
            "outside_availability",
            "no_route",
        )
        for reason in reasons
    ):
        return "lender_not_available"
    if not record.blocked_reasons:
        return "no_mutual_aid_contract"
    return "no_executable_branch"


def forecast_report(dataset: Dataset, origin: int) -> list[dict]:
    return [_forecast_row(forecast) for forecast in forecast_week(dataset, origin)]


def standing_forecast_report(dataset: Dataset, origin: int, weather: str) -> list[dict]:
    return [
        _forecast_row(forecast)
        for forecast in forecast_standing(dataset, origin, weather)
    ]


def _forecast_row(forecast: DayForecast) -> dict:
    return {
        "day": forecast.day.isoformat(),
        "weather": forecast.weather,
        "basis": forecast.basis,
        "sample_days": forecast.sample,
        "committable": [
            {
                "class": cell.class_id,
                "site": cell.site,
                "kind": cell.kind,
                "window": f"{cell.start}-{cell.end}",
                "promised": cell.promised,
                "failure_probability": cell.failure_probability,
            }
            for cell in forecast.committable
        ],
        "abstained": dict(forecast.rejected)
        if forecast.rejected
        else {},
        "mean": _finite(forecast.mean),
        "median": forecast.median,
        "interval": [forecast.low, forecast.high],
        "joint_priority_risk": _finite(forecast.joint_priority_risk),
        "independent_priority_risk": _finite(forecast.independent_priority_risk),
        "expected_lost_minutes": forecast.expected_lost_minutes,
        "baselines": {
            name: {
                key: _finite(value) if isinstance(value, float) else value
                for key, value in payload.items()
            }
            for name, payload in forecast.baselines.items()
        },
        "abstain_reason": forecast.abstain_reason,
    }


def _finite(value: float | None) -> float | None:
    import math

    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return round(value, 4)


def backtest_report(dataset: Dataset) -> dict:
    records = backtest(dataset)
    result = summary(records, dataset.rules)
    result["oracle_selection_check"] = _oracle_check(dataset)
    result["gap_attribution"] = gap_summary(dataset)
    return result


def _oracle_check(dataset: Dataset) -> dict:
    """检查是否有人按当天最终到岗挑选“最成功的调法”。

    对每天，比较“只用当时已收到信号”的回放与“已知全场结局”的口径：
    若两者调用表不同，说明存在前视差异；这些差异不得写进当时决定。
    """
    outcomes = replay_outcomes(dataset)
    by_day: dict[str, dict] = {}
    for item in outcomes:
        bucket = by_day.setdefault(
            item.day.isoformat(),
            {"dispatched": 0, "held": 0, "not_held": 0, "branches": set()},
        )
        bucket["dispatched"] += 1
        bucket["held"] += int(item.honoured)
        bucket["not_held"] += int(not item.honoured)
        bucket["branches"].add(item.branch_id)
    return {
        "days_with_dispatch": len(by_day),
        "dispatches": sum(bucket["dispatched"] for bucket in by_day.values()),
        "held_afterwards": sum(bucket["held"] for bucket in by_day.values()),
        "not_held_afterwards": sum(bucket["not_held"] for bucket in by_day.values()),
        "detail": {
            day: {
                "dispatched": bucket["dispatched"],
                "branches": sorted(bucket["branches"]),
                "held": bucket["held"],
                "not_held": bucket["not_held"],
            }
            for day, bucket in sorted(by_day.items())
        },
    }


def actual_vs_probability(dataset: Dataset, origin: int, weather: str) -> dict:
    """解释“本期实际调用表”与“当初概率试算”为何不同。

    概率试算给的是**承诺口径**（哪些格可以承诺、期望能兑现几节）；实际调用
    表受当天的可执行分支、已占窗口、未收到信号与截止时刻约束。两者不是
    同一张表：前者不含人，后者不含概率。
    """
    forecasts = forecast_standing(dataset, origin, weather)
    day = dataset.current_day()
    cutoff = max(
        (call.minute for call in dataset.calls if call.day == day),
        default=0,
    )
    view = desk_view(dataset, day, cutoff)
    ledger = promise_ledger(dataset, day)
    promised = [cell for forecast in forecasts for cell in forecast.committable if cell.promised]
    return {
        "probability_table": {
            "committable_classes": [cell.class_id for cell in promised],
            "abstained_classes": {
                cell.class_id: cell.reason
                for forecast in forecasts
                for cell in forecast.cells
                if not cell.committable
            },
            "expected_delivered": sum(
                forecast.expected_delivered for forecast in forecasts
            ),
            "joint_priority_risk": {
                forecast.day.isoformat(): _finite(forecast.joint_priority_risk)
                for forecast in forecasts
            },
        },
        "actual_call_table": {
            "cutoff": cutoff,
            "dispatched": [
                {
                    "branch": decision.branch_id,
                    "class": decision.target_class,
                    "staff": decision.staff,
                }
                for decision in view.dispatched
            ],
            "missing_signals": list(view.missing_signals),
            "occupied_windows": view.occupied_windows,
            "reservations": [
                {"staff": item.staff, "class": item.class_id, "source": item.source}
                for item in view.reservations
            ],
            "not_dispatched": [
                {
                    "branch": decision.branch_id,
                    "status": decision.status,
                    "reason": decision.reason,
                }
                for decision in view.decisions
                if decision.status != "dispatched"
            ],
        },
        "promise_ledger": [_promise_row(record) for record in ledger],
        "release_reports": release_reports(dataset, day),
        "exclusive_pairings": [
            {
                "staff": item.staff,
                "branches": [item.first, item.second],
                "shared_conditions": [list(pair) for pair in item.shared_conditions],
                "distinguishing": [list(pair) for pair in item.distinguishing],
                "observable_before_deadlines": item.observable_before_deadlines,
                "deadlines": item.deadlines,
                "detail": item.detail,
            }
            for item in exclusive_pairings(dataset, day)
        ],
        "not_same_table_reasons": [
            "概率试算按可承诺格计期望，不含具体出借人；实际调用表按分支窗口与已收信号逐个判定。",
            "概率表把停课与未收回执单独计数，不折算成零需求或确认失约。",
            "实际调用表只使用当时已收到的信号；迟到的放人回报不能追认已过时的援班。",
            "校方接受只形成预留，不进入概率表的‘已兑现’。",
            "证据不足的格在两张表里都不承诺，也不以跨校平均补位。",
        ],
    }
