"""原始资料的读取、类型化与索引。

只负责解释列语义，不做业务推断。通知截面与结局的区分在 :mod:`timeline`。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "data"
SITES = ("棠径", "东桥", "沙溪")
KINDS = ("paired", "ordinary")
WEEKDAY_ORDER = ("一", "二", "三", "四", "五")


def _dt(value: str) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _int(value: str) -> int | None:
    return int(value) if value != "" else None


def _multi(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.split("+") if part)


def read_tables(root: Path = ROOT) -> dict[str, list[dict[str, str]]]:
    """按 layout.json 校验表头后读取全部 CSV。"""
    definitions = json.loads((root / "layout.json").read_text(encoding="utf-8"))
    tables: dict[str, list[dict[str, str]]] = {}
    for name, table in definitions.items():
        with (root / f"{name}.csv").open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames != table["header"]:
                raise ValueError(f"{name}: CSV 表头与约定不符")
            rows = list(reader)
        if any(len(row) != len(table["header"]) or None in row.values() for row in rows):
            raise ValueError(f"{name}: CSV 记录缺列或多列")
        tables[name] = rows
    return tables


@dataclass(frozen=True)
class Staff:
    id: str
    role: str
    credentials: frozenset[str]
    commute: str

    def can_lead(self, kind: str) -> bool:
        return kind in self.credentials


@dataclass(frozen=True)
class ClassRecord:
    class_id: str
    week: int
    seen: datetime
    day: date
    site: str
    kind: str
    start: int
    end: int
    forecast_opening: str
    open: bool

    @property
    def slot(self) -> tuple[str, str, int, int]:
        return (self.site, self.kind, self.start, self.end)

    @property
    def minutes(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Outcome:
    class_id: str
    received: datetime | None
    state: str

    @property
    def known(self) -> bool:
        return self.state in ("held", "understaffed")

    @property
    def delivered(self) -> bool:
        return self.state == "held"

    @property
    def cancelled(self) -> bool:
        return self.state == "closed"

    @property
    def unknown(self) -> bool:
        return self.state == "receipt_missing" or self.received is None


@dataclass(frozen=True)
class Shift:
    staff: str
    day: date
    start: int
    end: int
    actual: str
    received: datetime | None

    @property
    def arrived(self) -> bool:
        return self.actual == "arrived"


@dataclass(frozen=True)
class Rollcall:
    day: date
    site: str
    signal: str
    actual_minute: int
    reported_minute: int | None

    @property
    def received(self) -> bool:
        return self.reported_minute is not None


@dataclass(frozen=True)
class StandingClass:
    class_id: str
    site: str
    day: date
    kind: str
    start: int
    end: int
    state: str
    notified_staff: tuple[str, ...]

    @property
    def promised(self) -> bool:
        return self.state in ("confirmed", "notified")

    @property
    def slot(self) -> tuple[str, str, int, int]:
        return (self.site, self.kind, self.start, self.end)

    @property
    def minutes(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Reinforcement:
    event: str
    staff: str
    day: date
    start: int
    end: int
    sites: tuple[str, ...]
    action: str
    reference: str
    received: datetime


@dataclass(frozen=True)
class Branch:
    id: str
    staff: str
    from_site: str
    to_site: str
    class_id: str
    conditions: tuple[tuple[str, str], ...]
    dispatch_at: int
    leave_by: int
    published_at: datetime
    state: str


@dataclass(frozen=True)
class CallEvent:
    event: str
    day: date
    site: str
    target: str
    recorded_at: datetime
    kind: str
    value: str

    @property
    def minute(self) -> int:
        return self.recorded_at.hour * 60 + self.recorded_at.minute


class Dataset:
    """全部业务表及只读索引。"""

    def __init__(self, tables: dict[str, list[dict[str, str]]]):
        self.raw = tables
        self.staff: dict[str, Staff] = {
            row["staff"]: Staff(
                row["staff"],
                row["role"],
                frozenset(row["credentials"].split("+")),
                row["commute"],
            )
            for row in tables["staff"]
        }
        self.classes: list[ClassRecord] = sorted(
            (
                ClassRecord(
                    row["class"],
                    int(row["week"]),
                    _dt(row["seen"]),
                    date.fromisoformat(row["day"]),
                    row["site"],
                    row["kind"],
                    int(row["start"]),
                    int(row["end"]),
                    row["forecast_opening"],
                    row["open"] == "yes",
                )
                for row in tables["timetable"]
            ),
            key=lambda record: (record.seen, record.class_id, record.day),
        )
        self.outcomes: dict[str, Outcome] = {
            row["class"]: Outcome(row["class"], _dt(row["received"]), row["state"])
            for row in tables["outcomes"]
        }
        self.shifts: dict[tuple[str, date], Shift] = {
            (row["staff"], date.fromisoformat(row["day"])): Shift(
                row["staff"],
                date.fromisoformat(row["day"]),
                int(row["start"]),
                int(row["end"]),
                row["actual"],
                _dt(row["received"]),
            )
            for row in tables["shifts"]
        }
        self.rollcalls: dict[tuple[date, str], Rollcall] = {
            (date.fromisoformat(row["day"]), row["site"]): Rollcall(
                date.fromisoformat(row["day"]),
                row["site"],
                row["signal"],
                int(row["actual_minute"]),
                _int(row["reported_minute"]),
            )
            for row in tables["rollcalls"]
        }
        self.standing: dict[str, StandingClass] = {
            row["class"]: StandingClass(
                row["class"],
                row["site"],
                date.fromisoformat(row["day"]),
                row["kind"],
                int(row["start"]),
                int(row["end"]),
                row["state"],
                _multi(row["notified_staff"]),
            )
            for row in tables["standing"]
        }
        self.reinforcements: list[Reinforcement] = [
            Reinforcement(
                row["event"],
                row["staff"],
                date.fromisoformat(row["day"]),
                int(row["start"]),
                int(row["end"]),
                _multi(row["sites"]),
                row["action"],
                row["reference"],
                _dt(row["received"]),
            )
            for row in tables["reinforcements"]
        ]
        self.branches: list[Branch] = [
            Branch(
                row["branch"],
                row["staff"],
                row["from_site"],
                row["to_site"],
                row["school_class"],
                tuple(tuple(part.split("/")) for part in row["condition"].split("+")),
                int(row["dispatch_at"]),
                int(row["leave_by"]),
                _dt(row["published_at"]),
                row["state"],
            )
            for row in tables["mutual_aid"]
        ]
        self.calls: list[CallEvent] = [
            CallEvent(
                row["event"],
                date.fromisoformat(row["day"]),
                row["site"],
                row["target"],
                _dt(row["recorded_at"]),
                row["kind"],
                row["value"],
            )
            for row in tables["call_round"]
        ]

        self.by_class: dict[str, list[ClassRecord]] = {}
        for record in self.classes:
            self.by_class.setdefault(record.class_id, []).append(record)
        self.weeks: list[int] = sorted({record.week for record in self.classes})
        self.week_start: dict[int, date] = {
            week: min(record.day for record in self.classes if record.week == week)
            for week in self.weeks
        }
        self.rules: dict = json.loads((ROOT / "rules.json").read_text(encoding="utf-8"))

    @classmethod
    def load(cls, root: Path = ROOT) -> "Dataset":
        return cls(read_tables(root))

    def branch(self, branch_id: str) -> Branch:
        return next(branch for branch in self.branches if branch.id == branch_id)

    def travel(self, origin: str, destination: str) -> int | None:
        return self.rules["travel_minutes"].get(f"{origin}/{destination}")

    def current_day(self) -> date:
        return min(standing.day for standing in self.standing.values())

    def next_week(self) -> int:
        return max(self.weeks) + 1
