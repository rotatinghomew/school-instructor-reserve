"""固定备班与条件联保的对照，以及调用约定的一致性审计。

两种策略都只用当天截止时刻之前已收到的信号：

* ``conditional``：按 ``mutual_aid`` 分支，要求该分支的全部条件都已收到且
  一致，才可派出；
* ``fixed``：每天预先在东桥留一名合格助教备班（预置在目标校点，无路程），
  只要目标校当天出现 ``shortage`` 且回报早于开课，就派出，不等出发校的信号。

两者都按同一人的同一窗口只记一次，且都不使用当天最终到岗来挑日子。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .dispatch import (
    audit_branch,
    availability,
    commitments,
    target_for,
    _deadline_cutoff,
    _partner_present,
)
from .tables import Dataset
from .timeline import signal_reports, snapshot


@dataclass
class StrategyDay:
    day: date
    need: bool  # 目标课当天确实缺师（事后）
    dispatched: bool
    call_minute: int | None
    protected: bool  # 目标课当天已证实开课
    target_class: str | None
    reason: str | None


@dataclass
class StrategySummary:
    name: str
    days: int
    dispatches: int
    protected: int
    false_alarms: int
    missed_needs: int
    call_minutes: list[int]
    detail: list[StrategyDay]


def _target_days(dataset: Dataset) -> list[tuple[date, str]]:
    """历史中可作为“东桥成对课”目标的日子及班号。"""
    targets: list[tuple[date, str]] = []
    for week in dataset.weeks:
        for record in snapshot(dataset, week).records:
            if record.site == "东桥" and record.kind == "paired":
                targets.append((record.day, record.class_id))
    return sorted(set(targets))


def _need(dataset: Dataset, class_id: str) -> bool:
    outcome = dataset.outcomes.get(class_id)
    return bool(outcome and outcome.state == "understaffed")


def _protected(dataset: Dataset, class_id: str) -> bool:
    outcome = dataset.outcomes.get(class_id)
    return bool(outcome and outcome.state == "held")


def conditional_strategy(dataset: Dataset) -> StrategySummary:
    detail: list[StrategyDay] = []
    branch = next(
        (
            item
            for item in dataset.branches
            if item.to_site == "东桥"
            and dataset.standing.get(item.class_id) is not None
            and dataset.standing[item.class_id].kind == "paired"
        ),
        None,
    )
    if branch is None:
        return _summarise("conditional", detail)
    for day, class_id in _target_days(dataset):
        target = target_for(dataset, branch, day)
        if target is None or target.class_id != class_id:
            continue
        windows = availability(dataset, day)
        committed = commitments(dataset, day)
        audit = audit_branch(
            dataset,
            branch,
            day,
            _deadline_cutoff(dataset, branch, day),
            windows,
            committed,
        )
        dispatched = audit.executable and _partner_present(dataset, target, branch.staff)
        detail.append(
            StrategyDay(
                day,
                _need(dataset, class_id),
                dispatched,
                audit.call_minute,
                _protected(dataset, class_id),
                class_id,
                None if dispatched else audit.reason,
            )
        )
    return _summarise("conditional", detail)


def fixed_strategy(dataset: Dataset) -> StrategySummary:
    """预置在东桥的固定备班：只看目标校 shortage，须早于开课回报。"""
    detail: list[StrategyDay] = []
    for day, class_id in _target_days(dataset):
        target = _historical_target(dataset, class_id, day)
        if target is None:
            continue
        reports = signal_reports(dataset, day)
        shortage = reports.get("东桥")
        dispatched = bool(
            shortage and shortage[0] == "shortage" and shortage[1] < target.start
        )
        detail.append(
            StrategyDay(
                day,
                _need(dataset, class_id),
                dispatched,
                shortage[1] if dispatched else None,
                _protected(dataset, class_id),
                class_id,
                None if dispatched else "no_shortage_by_start",
            )
        )
    return _summarise("fixed", detail)


def _historical_target(dataset: Dataset, class_id: str, day: date):
    for week in dataset.weeks:
        for record in snapshot(dataset, week).records:
            if record.class_id == class_id and record.day == day:
                return record
    return None


def _summarise(name: str, detail: list[StrategyDay]) -> StrategySummary:
    return StrategySummary(
        name,
        len(detail),
        sum(1 for item in detail if item.dispatched),
        sum(1 for item in detail if item.dispatched and item.protected),
        sum(1 for item in detail if item.dispatched and not item.need),
        sum(1 for item in detail if item.need and not item.dispatched),
        [item.call_minute for item in detail if item.call_minute is not None],
        detail,
    )


def compare_strategies(dataset: Dataset) -> dict:
    conditional = conditional_strategy(dataset)
    fixed = fixed_strategy(dataset)

    def payload(summary: StrategySummary) -> dict:
        return {
            "days": summary.days,
            "dispatches": summary.dispatches,
            "protected": summary.protected,
            "false_alarms": summary.false_alarms,
            "missed_needs": summary.missed_needs,
            "mean_call_minute": (
                sum(summary.call_minutes) / len(summary.call_minutes)
                if summary.call_minutes
                else None
            ),
        }

    return {
        "conditional": payload(conditional),
        "fixed": payload(fixed),
        "reading": [
            "固定备班预置在目标校，不等出发校信号，但每天都要占用一名合格助教。",
            "联保方案只在条件全部收到且早于截止时刻时才派出，误派少，但可能因迟到信号错过。",
            "两者都以当天已证实结局评分，不用最终到岗挑选当天最成功的调法。",
        ],
    }


def _day_records(dataset: Dataset, day: date):
    for week in dataset.weeks:
        records = [
            record
            for record in snapshot(dataset, week).records
            if record.day == day
        ]
        if records:
            return records
    return []


def determinism_audit(dataset: Dataset) -> dict:
    """同一信息状态必须得到同一调用表。

    以（当天、截止时刻、已收信号、可用窗口、承诺占用）作为状态签名，
    对不同日期中签名相同的状态，比较调用表是否一致。
    """
    from .dispatch import desk_view
    from .timeline import signal_minutes

    groups: dict[tuple, list[tuple[date, list[tuple[str, str, str, int]]]]] = {}
    for day in sorted({day for (day, _) in dataset.rollcalls}):
        for cutoff in signal_minutes(dataset, day):
            view = desk_view(dataset, day, cutoff)
            received = {
                site: report
                for site, report in signal_reports(dataset, day).items()
                if report[1] <= cutoff
            }
            signature = (
                tuple(sorted(received.items())),
                tuple(sorted(view.missing_signals)),
                tuple(sorted(
                    (staff, tuple(sorted(window.sites)), window.start, window.end)
                    for staff, items in availability(dataset, day).items()
                    for window in items
                )),
                tuple(sorted(
                    (item.staff, item.class_id, item.start, item.end)
                    for item in commitments(dataset, day)
                )),
                tuple(sorted(
                    (branch.id, branch.conditions, branch.dispatch_at, branch.leave_by)
                    for branch in dataset.branches
                )),
                tuple(
                    sorted(
                        (record.site, record.kind, record.start, record.end)
                        for record in _day_records(dataset, day)
                    )
                ),
            )
            decisions = [
                (
                    item.branch_id,
                    item.status,
                    item.staff,
                    item.call_minute or -1,
                )
                for item in view.decisions
            ]
            groups.setdefault(signature, []).append((day, decisions))

    conflicts = []
    for signature, entries in groups.items():
        distinct = {tuple(entry[1]) for entry in entries}
        if len(distinct) > 1:
            conflicts.append(
                {
                    "days": [entry[0].isoformat() for entry in entries],
                    "decisions": [list(map(list, entry[1])) for entry in entries],
                }
            )
    same_state_days = sum(len(entries) for entries in groups.values())
    return {
        "information_states": len(groups),
        "evaluations": same_state_days,
        "conflicts": conflicts,
        "identical": not conflicts,
    }
