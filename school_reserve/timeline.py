"""通知截面、可见版本与结局的时间线。

规则：
* 每周的通知截面取该周课表记录里最早的 ``seen``（即周日前一个工作日 17:00）；
* 截面之后才出现的记录版本（停课、改点）不进入当周预测，只作为事后结局；
* 结局区分 ``held`` / ``understaffed``（已证实）与 ``receipt_missing``（未证实，
  不得当作失约）以及 ``closed``（停止开放，不是零需求）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .tables import ClassRecord, Dataset, Outcome


@dataclass(frozen=True)
class NoticeSnapshot:
    """某一周发出通知时手头掌握的课表。"""

    week: int
    taken_at: datetime
    records: tuple[ClassRecord, ...]

    def by_day_site(self) -> dict[tuple[date, str], ClassRecord]:
        return {(record.day, record.site): record for record in self.records}

    def days(self) -> list[date]:
        return sorted({record.day for record in self.records})


def notice_instant(dataset: Dataset, week: int) -> datetime:
    """该周通知截面：本周课表首次可见的最早时刻。"""
    seen = [record.seen for record in dataset.classes if record.week == week]
    if not seen:
        raise KeyError(f"第 {week} 周没有课表记录")
    return min(seen)


def snapshot(dataset: Dataset, week: int) -> NoticeSnapshot:
    """只保留通知时刻已可见的最新版本。"""
    cutoff = notice_instant(dataset, week)
    visible: dict[str, ClassRecord] = {}
    for record in dataset.classes:
        if record.week != week or record.seen > cutoff:
            continue
        current = visible.get(record.class_id)
        if current is None or record.seen > current.seen:
            visible[record.class_id] = record
    return NoticeSnapshot(week, cutoff, tuple(sorted(visible.values(), key=lambda r: (r.day, r.site))))


def later_versions(dataset: Dataset, week: int) -> dict[str, list[ClassRecord]]:
    """通知截面之后到达的课表版本，按班号分组。"""
    cutoff = notice_instant(dataset, week)
    changes: dict[str, list[ClassRecord]] = {}
    for record in dataset.classes:
        if record.week == week and record.seen > cutoff:
            changes.setdefault(record.class_id, []).append(record)
    return {class_id: sorted(records, key=lambda r: r.seen) for class_id, records in changes.items()}


def final_record(dataset: Dataset, class_id: str) -> ClassRecord:
    return max(dataset.by_class[class_id], key=lambda record: record.seen)


def outcome(dataset: Dataset, class_id: str) -> Outcome:
    return dataset.outcomes[class_id]


def signal_reports(dataset: Dataset, day: date) -> dict[str, tuple[str, int]]:
    """某日各校晨检信号及其被送交的时刻。

    历史日取 ``rollcalls``；当周还没有晨检记录的日子取本周实际回报
    ``call_round`` 中的 ``rollcall`` 事件。两者都不含“后来才补”的终值。
    """
    reports: dict[str, tuple[str, int]] = {}
    for (rc_day, site), rollcall in dataset.rollcalls.items():
        if rc_day != day or rollcall.reported_minute is None:
            continue
        reports[site] = (rollcall.signal, rollcall.reported_minute)
    if reports:
        return reports
    for call in dataset.calls:
        if call.day != day or call.kind != "rollcall":
            continue
        previous = reports.get(call.site)
        if previous is None or call.minute < previous[1]:
            reports[call.site] = (call.value, call.minute)
    return reports


def signals_at(dataset: Dataset, day: date, minute: int) -> dict[str, str]:
    """截至某一分钟已经收到的晨检信号；晚于该刻的回报视为尚未可知。"""
    return {
        site: signal
        for site, (signal, reported) in signal_reports(dataset, day).items()
        if reported <= minute
    }


def signal_minutes(dataset: Dataset, day: date) -> list[int]:
    """当天所有晨检回报时刻，按时间排序。"""
    return sorted({reported for _, reported in signal_reports(dataset, day).values()})
