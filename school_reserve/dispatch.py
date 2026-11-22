"""可用窗口、承诺占用、条件援班、无前视回放与承诺台账。

约定（见 ``rules.json`` 的 ``handoff``）：

* 分支条件形如 ``校点/信号``，必须在截止时刻之前**已收到**，且与条件一致；
* 截止时刻 = ``min(leave_by, 目标课开课 - 路程)``；
* 调用时刻 = ``max(dispatch_at, 各条件回报时刻)``，不得晚于截止时刻；
* 出借人当天须有可用窗口，窗口须覆盖出发、到场以及整节课；目标校点须在
  其可去范围内；
* 出借人不得与自己的承诺课重叠（含跨校路程）；同一人不得同时占用两个互斥
  分支的调用窗口；同一目标课不得同时计入两个方案；
* **可用窗口与承诺来自通知时已知的排班、增援回执和已发出的通知**；到岗
  ``actual`` 与完课回执只是结局，用于评分，不用于决定。

校方接受只形成预留，不等于教练已到；迟到的放人回报不能追认已经过时的援班。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Literal

from .tables import Branch, Dataset, SITES, StandingClass
from .timeline import signal_minutes, signal_reports, signals_at, snapshot

Status = Literal["executable", "waiting", "blocked"]


# --------------------------------------------------------------------------
# 可用窗口与承诺
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """一名教练某日可被支配的窗口，来自通知时已知的排班或增援回执。"""

    staff: str
    start: int
    end: int
    sites: frozenset[str]
    source: str


@dataclass(frozen=True)
class Commitment:
    """已经发出的通知：某人在某段时间属于某节课。"""

    staff: str
    class_id: str
    day: date
    site: str
    kind: str
    start: int
    end: int
    state: str

    @property
    def promised(self) -> bool:
        return self.state in ("confirmed", "notified")

    def overlaps(self, start: int, end: int) -> bool:
        return start < self.end and self.start < end


def availability(dataset: Dataset, day: date) -> dict[str, list[Window]]:
    """当天各教练的可用窗口。

    当期有增援回执时以回执为准；``shortened`` 替换被其引用的原窗口。
    其余日子回落到通知时已定的班次窗口。两者都不读 ``actual``。
    """
    windows: dict[str, list[Window]] = {}
    same_day = [event for event in dataset.reinforcements if event.day == day]
    if same_day:
        superseded = {
            event.reference for event in same_day if event.action == "shortened"
        }
        for event in same_day:
            if event.action == "available" and event.event not in superseded:
                windows.setdefault(event.staff, []).append(
                    Window(
                        event.staff,
                        event.start,
                        event.end,
                        frozenset(event.sites),
                        "available",
                    )
                )
        for event in same_day:
            if event.action == "shortened":
                windows.setdefault(event.staff, []).append(
                    Window(
                        event.staff,
                        event.start,
                        event.end,
                        frozenset(event.sites),
                        "shortened",
                    )
                )
        return windows

    for (staff, shift_day), shift in dataset.shifts.items():
        if shift_day != day:
            continue
        windows.setdefault(staff, []).append(
            Window(staff, shift.start, shift.end, frozenset(SITES), "shift")
        )
    return windows


def commitments(dataset: Dataset, day: date) -> list[Commitment]:
    """当天已经通知出去的班次占用。

    当周用 ``standing`` 的 ``notified_staff``；历史周没有逐人派单记录，
    由调用方以 ``historical_commitments`` 传入替代口径。
    """
    return [
        Commitment(
            staff,
            record.class_id,
            record.day,
            record.site,
            record.kind,
            record.start,
            record.end,
            record.state,
        )
        for record in dataset.standing.values()
        if record.day == day and record.promised
        for staff in record.notified_staff
    ]


@dataclass(frozen=True)
class Reservation:
    """校方接受后形成的真实预留；不等于教练已到。"""

    staff: str
    class_id: str
    day: date
    branch_id: str | None
    source: str
    received: str
    narrowed_end: int | None = None


def reservations(dataset: Dataset) -> list[Reservation]:
    result: list[Reservation] = []
    by_branch = {branch.id: branch for branch in dataset.branches}
    narrowed = {
        event.staff: event.end
        for event in dataset.reinforcements
        if event.action == "shortened"
    }
    for event in dataset.reinforcements:
        if event.action != "school_accepts":
            continue
        result.append(
            Reservation(
                event.staff,
                event.reference,
                event.day,
                None,
                "school_accepts",
                event.received.isoformat(),
                narrowed.get(event.staff),
            )
        )
    for call in dataset.calls:
        if call.kind != "school_confirmed":
            continue
        if call.value != "yes":
            continue
        branch = by_branch.get(call.target)
        result.append(
            Reservation(
                branch.staff if branch else "",
                branch.class_id if branch else call.target,
                call.day,
                branch.id if branch else None,
                "school_confirmed",
                call.recorded_at.isoformat(),
            )
        )
    return result


# --------------------------------------------------------------------------
# 分支审计
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConditionStatus:
    site: str
    expected: str
    observed: str | None
    reported_at: int | None
    satisfied: bool | None  # None = 到截止时刻仍未收到

    @property
    def missing(self) -> bool:
        return self.reported_at is None


@dataclass(frozen=True)
class BranchAudit:
    branch_id: str
    staff: str
    target_class: str
    target_site: str
    target_kind: str
    status: Status
    reason: str | None
    blockers: tuple[str, ...]
    call_minute: int | None
    arrival_minute: int | None
    deadline: int | None
    latest_departure: int | None
    leave_by: int
    dispatch_at: int
    conditions: tuple[ConditionStatus, ...]
    competing_branches: tuple[str, ...] = ()
    alternative_for_class: tuple[str, ...] = ()
    shared_staff_windows: tuple[str, ...] = ()
    commitment_conflicts: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.status == "executable"

    @property
    def missing_sites(self) -> tuple[str, ...]:
        return tuple(status.site for status in self.conditions if status.missing)


def _conditions_at(
    dataset: Dataset, day: date, branch: Branch, cutoff: int | None
) -> tuple[ConditionStatus, ...]:
    reports = signal_reports(dataset, day)
    statuses = []
    for site, expected in branch.conditions:
        report = reports.get(site)
        if report is None:
            statuses.append(ConditionStatus(site, expected, None, None, None))
            continue
        signal, reported = report
        if cutoff is not None and reported > cutoff:
            statuses.append(ConditionStatus(site, expected, None, None, None))
            continue
        statuses.append(
            ConditionStatus(site, expected, signal, reported, signal == expected)
        )
    return tuple(statuses)


def latest_departure(dataset: Dataset, branch: Branch, target: StandingClass) -> int | None:
    travel = dataset.travel(branch.from_site, target.site)
    return None if travel is None else target.start - travel


def target_for(dataset: Dataset, branch: Branch, day: date) -> StandingClass | None:
    """分支作用于当天目标校点、同课型、同起止的班。

    当周用 ``standing``；历史反事实用当天通知截面里同格的课表，起止沿用
    分支承诺的时段，避免把新合同硬套到旧时段。
    """
    record = dataset.standing.get(branch.class_id)
    if record is not None and record.day == day:
        return record
    if record is None:
        return None
    for week in dataset.weeks:
        for item in snapshot(dataset, week).records:
            if (
                item.day == day
                and item.site == record.site
                and item.kind == record.kind
                and item.start == record.start
                and item.end == record.end
            ):
                return StandingClass(
                    item.class_id,
                    item.site,
                    item.day,
                    item.kind,
                    item.start,
                    item.end,
                    "historical",
                    (),
                )
    return None


def audit_branch(
    dataset: Dataset,
    branch: Branch,
    day: date,
    cutoff: int | None = None,
    windows: dict[str, list[Window]] | None = None,
    committed: list[Commitment] | None = None,
) -> BranchAudit:
    """在 ``cutoff`` 分钟的信息状态下审计一个援班分支，列出全部阻碍。"""
    windows = windows if windows is not None else availability(dataset, day)
    committed = committed if committed is not None else commitments(dataset, day)
    target = target_for(dataset, branch, day)
    conditions = _conditions_at(dataset, day, branch, cutoff)
    shared = tuple(
        other.id
        for other in dataset.branches
        if other.id != branch.id
        and other.staff == branch.staff
        and other.dispatch_at <= branch.leave_by
        and branch.dispatch_at <= other.leave_by
    )
    alternatives = tuple(
        other.id
        for other in dataset.branches
        if other.id != branch.id and other.class_id == branch.class_id
    )

    if target is None:
        return BranchAudit(
            branch.id,
            branch.staff,
            branch.class_id,
            branch.to_site,
            "",
            "blocked",
            "no_target_class",
            ("no_target_class",),
            None,
            None,
            None,
            None,
            branch.leave_by,
            branch.dispatch_at,
            conditions,
            shared,
            alternatives,
            shared,
        )

    travel = dataset.travel(branch.from_site, target.site)
    latest = latest_departure(dataset, branch, target)
    deadline = None if latest is None else min(branch.leave_by, latest)
    window = next(iter(windows.get(branch.staff, [])), None)
    blockers: list[str] = []

    if travel is None or latest is None:
        blockers.append("no_route")
    if branch.leave_by < branch.dispatch_at:
        blockers.append("window_impossible")
    if latest is not None and branch.leave_by > latest:
        blockers.append("leave_after_class_start")
    if window is None:
        blockers.append("no_availability")
    else:
        if branch.leave_by < window.start or branch.dispatch_at > window.end:
            blockers.append("outside_availability")
        if branch.to_site not in window.sites:
            blockers.append("target_outside_availability")
        if branch.dispatch_at + (travel or 0) + (target.end - target.start) > window.end:
            blockers.append("class_outside_availability")

    conflict_ids = tuple(
        item.class_id
        for item in committed
        if item.staff == branch.staff
        and item.class_id != target.class_id
        and item.overlaps(branch.dispatch_at, target.end + (travel or 0))
    )
    if conflict_ids:
        blockers.append("precommitted_elsewhere")

    contradicted = [status for status in conditions if status.satisfied is False]
    for status in contradicted:
        blockers.append(f"contradicted:{status.site}")
    missing = [status for status in conditions if status.missing]
    if missing:
        blockers.append(
            "waiting:" + "+".join(status.site for status in missing)
        )

    if contradicted or missing:
        primary = blockers[-1]
        status: Status = "blocked" if contradicted else "waiting"
        if any(
            item in blockers
            for item in (
                "no_route",
                "window_impossible",
                "no_availability",
                "outside_availability",
                "target_outside_availability",
                "class_outside_availability",
                "precommitted_elsewhere",
            )
        ):
            status = "blocked"
            primary = next(
                item
                for item in blockers
                if item
                in (
                    "no_route",
                    "window_impossible",
                    "no_availability",
                    "outside_availability",
                    "target_outside_availability",
                    "class_outside_availability",
                    "precommitted_elsewhere",
                )
            )
        return BranchAudit(
            branch.id,
            branch.staff,
            target.class_id,
            target.site,
            target.kind,
            status,
            primary,
            tuple(dict.fromkeys(blockers)),
            None,
            None,
            deadline,
            latest,
            branch.leave_by,
            branch.dispatch_at,
            conditions,
            shared,
            alternatives,
            shared,
            conflict_ids,
        )

    call = max(
        [branch.dispatch_at]
        + [status.reported_at for status in conditions]
        + ([window.start] if window is not None else [])
    )
    if deadline is not None and call > deadline:
        late_reports = [
            status.reported_at
            for status in conditions
            if status.reported_at is not None and status.reported_at > deadline
        ]
        blockers.append(
            "release_after_deadline" if late_reports else "signal_after_deadline"
        )
    if travel and call + travel >= target.start:
        blockers.append("arrival_after_start")
    if window is not None and travel and call + travel > window.end:
        blockers.append("arrival_outside_availability")

    if blockers:
        return BranchAudit(
            branch.id,
            branch.staff,
            target.class_id,
            target.site,
            target.kind,
            "blocked",
            blockers[0],
            tuple(dict.fromkeys(blockers)),
            None,
            None,
            deadline,
            latest,
            branch.leave_by,
            branch.dispatch_at,
            conditions,
            shared,
            alternatives,
            shared,
            conflict_ids,
        )

    return BranchAudit(
        branch.id,
        branch.staff,
        target.class_id,
        target.site,
        target.kind,
        "executable",
        None,
        (),
        call,
        call + travel,
        deadline,
        latest,
        branch.leave_by,
        branch.dispatch_at,
        conditions,
        shared,
        alternatives,
        shared,
        conflict_ids,
    )


# --------------------------------------------------------------------------
# 调用约定
# --------------------------------------------------------------------------


@dataclass
class CallDecision:
    day: date
    cutoff: int
    branch_id: str
    staff: str
    target_class: str
    target_site: str
    target_kind: str
    call_minute: int
    arrival_minute: int
    status: Literal["dispatched", "superseded", "credit_conflict", "no_partner"]
    reason: str | None = None


@dataclass
class DeskView:
    day: date
    cutoff: int
    decisions: list[CallDecision]
    waiting: list[BranchAudit]
    blocked: list[BranchAudit]
    executable: list[BranchAudit]
    missing_signals: tuple[str, ...]
    occupied_windows: dict[str, str]
    reservations: list[Reservation]

    @property
    def dispatched(self) -> list[CallDecision]:
        return [item for item in self.decisions if item.status == "dispatched"]


def missing_signals(dataset: Dataset, day: date, cutoff: int) -> tuple[str, ...]:
    received = signals_at(dataset, day, cutoff)
    return tuple(site for site in SITES if site not in received)


def _partner_present(dataset: Dataset, target: StandingClass, arriving: str) -> bool:
    """成对课型须有“一名主教 + 一名合格副教”同时在场。

    ``rules.interpretation`` 要求 paired 由主教与副教共同承担，因此来援者
    是副教时必须另有一位 paired 资格的主教；反之亦然。当周以
    ``standing.notified_staff`` 为准；历史日没有逐人派单记录，改用当天可用
    窗口里覆盖目标校点与课时的人。两者都排除来援者本人。
    """
    if target.kind != "paired":
        return True
    arriving_staff = dataset.staff.get(arriving)
    if arriving_staff is None or not arriving_staff.can_lead("paired"):
        return False
    wanted_role = "lead" if arriving_staff.role == "assistant" else "assistant"
    windows = availability(dataset, target.day)
    notified = set(target.notified_staff)
    for staff, items in windows.items():
        if staff == arriving or staff not in dataset.staff:
            continue
        candidate = dataset.staff[staff]
        if candidate.role != wanted_role or not candidate.can_lead("paired"):
            continue
        if notified and staff not in notified:
            continue
        for window in items:
            if (
                target.site in window.sites
                and window.start <= target.start
                and window.end >= target.end
            ):
                return True
    return False


def desk_view(dataset: Dataset, day: date, cutoff: int) -> DeskView:
    """值班处在 ``cutoff`` 时点的可执行调用表。

    固定顺序：优先课型、到达时刻、分支编号。同一出借人在同一窗口只记一次，
    同一目标课只记一个方案。相同的信息状态必然得到相同的调用表。
    """
    windows = availability(dataset, day)
    committed = commitments(dataset, day)
    audits = [
        audit_branch(dataset, branch, day, cutoff, windows, committed)
        for branch in dataset.branches
    ]
    priority = tuple(dataset.rules.get("priority_types", ()))
    executable = sorted(
        (audit for audit in audits if audit.executable),
        key=lambda audit: (
            0 if audit.target_kind in priority else 1,
            audit.arrival_minute,
            audit.branch_id,
        ),
    )
    used_staff: dict[str, str] = {}
    used_class: dict[str, str] = {}
    decisions: list[CallDecision] = []
    for audit in executable:
        branch = _branch(dataset, audit.branch_id)
        target = target_for(dataset, branch, day)
        if target is not None and not _partner_present(dataset, target, audit.staff):
            decisions.append(
                CallDecision(
                    day,
                    cutoff,
                    audit.branch_id,
                    audit.staff,
                    audit.target_class,
                    audit.target_site,
                    audit.target_kind,
                    audit.call_minute,
                    audit.arrival_minute,
                    "no_partner",
                    "paired_without_partner",
                )
            )
            continue
        if audit.staff in used_staff:
            decisions.append(
                CallDecision(
                    day,
                    cutoff,
                    audit.branch_id,
                    audit.staff,
                    audit.target_class,
                    audit.target_site,
                    audit.target_kind,
                    audit.call_minute,
                    audit.arrival_minute,
                    "credit_conflict",
                    f"same_person_as:{used_staff[audit.staff]}",
                )
            )
            continue
        if audit.target_class in used_class:
            decisions.append(
                CallDecision(
                    day,
                    cutoff,
                    audit.branch_id,
                    audit.staff,
                    audit.target_class,
                    audit.target_site,
                    audit.target_kind,
                    audit.call_minute,
                    audit.arrival_minute,
                    "superseded",
                    f"same_class_as:{used_class[audit.target_class]}",
                )
            )
            continue
        used_staff[audit.staff] = audit.branch_id
        used_class[audit.target_class] = audit.branch_id
        decisions.append(
            CallDecision(
                day,
                cutoff,
                audit.branch_id,
                audit.staff,
                audit.target_class,
                audit.target_site,
                audit.target_kind,
                audit.call_minute,
                audit.arrival_minute,
                "dispatched",
            )
        )

    return DeskView(
        day,
        cutoff,
        decisions,
        [audit for audit in audits if audit.status == "waiting"],
        [audit for audit in audits if audit.status == "blocked"],
        executable,
        missing_signals(dataset, day, cutoff),
        dict(used_staff),
        [reservation for reservation in reservations(dataset) if reservation.day == day],
    )


def _branch(dataset: Dataset, branch_id: str) -> Branch:
    return next(branch for branch in dataset.branches if branch.id == branch_id)



def release_reports(dataset: Dataset, day: date) -> list[dict]:
    """当周放人回执及其到达时刻；用于解释“回报来得太晚”。"""
    result: list[dict] = []
    for call in dataset.calls:
        if call.day != day:
            continue
        if call.kind not in ("release_receipt", "school_confirmed"):
            continue
        branch = _branch(dataset, call.target) if any(
            branch.id == call.target for branch in dataset.branches
        ) else None
        result.append(
            {
                "event": call.event,
                "target": call.target,
                "site": call.site,
                "kind": call.kind,
                "value": call.value,
                "recorded_at": call.recorded_at.isoformat(),
                "minute": call.minute,
                "branch": branch.id if branch else None,
                "class": branch.class_id if branch else None,
            }
        )
    return sorted(result, key=lambda item: item["minute"])



# --------------------------------------------------------------------------
# 无前视回放
# --------------------------------------------------------------------------


@dataclass
class ReplayStep:
    day: date
    cutoff: int
    decisions: list[CallDecision]
    waiting: list[str]
    missing_signals: tuple[str, ...]


@dataclass
class ReplayResult:
    day: date
    steps: list[ReplayStep]
    executed: list[CallDecision] = field(default_factory=list)

    @property
    def dispatched(self) -> list[CallDecision]:
        return [item for item in self.executed if item.status == "dispatched"]

    @property
    def first_call(self) -> CallDecision | None:
        return self.dispatched[0] if self.dispatched else None


def replay_day(dataset: Dataset, day: date) -> ReplayResult:
    """按晨检回报时刻逐点重放；每个决定点只看当时已收到的信号。

    同一分支一旦派出即视为已占用，后续决定点不再重复计入。
    """
    steps: list[ReplayStep] = []
    executed: list[CallDecision] = []
    seen: set[str] = set()
    for cutoff in signal_minutes(dataset, day):
        view = desk_view(dataset, day, cutoff)
        fresh = []
        for decision in view.dispatched:
            if decision.branch_id in seen:
                continue
            seen.add(decision.branch_id)
            fresh.append(decision)
            executed.append(decision)
        steps.append(
            ReplayStep(
                day,
                cutoff,
                fresh,
                [audit.branch_id for audit in view.waiting if audit.branch_id not in seen],
                view.missing_signals,
            )
        )
    return ReplayResult(day, steps, executed)


def replay(dataset: Dataset, days: Iterable[date] | None = None) -> list[ReplayResult]:
    if days is not None:
        target = sorted(days)
    else:
        target = sorted(
            {day for (day, _) in dataset.rollcalls} | {call.day for call in dataset.calls}
        )
    return [replay_day(dataset, day) for day in target]


@dataclass
class ReplayOutcome:
    day: date
    branch_id: str
    target_class: str
    target_site: str
    target_kind: str
    call_minute: int
    arrival_minute: int
    later_outcome: str | None
    promised: bool
    honoured: bool
    reason: str | None


def replay_outcomes(dataset: Dataset) -> list[ReplayOutcome]:
    """回放决定与事后结局并排；只用于评价，不参与当时决定。"""
    results: list[ReplayOutcome] = []
    for result in replay(dataset):
        for decision in result.dispatched:
            record = dataset.standing.get(decision.target_class)
            later = dataset.outcomes.get(decision.target_class)
            state = later.state if later else None
            promised = (
                bool(record.promised)
                if record is not None
                else decision.target_class in dataset.outcomes
            )
            results.append(
                ReplayOutcome(
                    result.day,
                    decision.branch_id,
                    decision.target_class,
                    decision.target_site,
                    decision.target_kind,
                    decision.call_minute,
                    decision.arrival_minute,
                    state,
                    promised,
                    state == "held",
                    None if state == "held" else state,
                )
            )
    return results


# --------------------------------------------------------------------------
# 承诺台账
# --------------------------------------------------------------------------


@dataclass
class PromiseRecord:
    """一节已承诺的课，以及援班是否真正可动用。"""

    day: date
    class_id: str
    site: str
    kind: str
    start: int
    end: int
    state: str
    promised: bool
    reservations: tuple[Reservation, ...]
    executable_at_deadline: tuple[str, ...]
    dispatched_at_deadline: tuple[str, ...]
    blocked_reasons: dict[str, tuple[str, ...]]
    later_outcome: str | None
    honoured: bool | None


def promise_ledger(
    dataset: Dataset, day: date, cutoff: int | None = None
) -> list[PromiseRecord]:
    """一天里每节已承诺课的援班台账。

    每个分支在自己的截止时刻审计（即“当时最晚还能放人”的信息状态），
    这样迟到的放人回报不会被用来追认已过时的援班。整体调用表另给
    ``cutoff``（缺省为当天最后一次回报）。

    分支审计传入 ``cutoff=None``：允许看到所有**确实到达**的回报，但仍按各自
    截止时刻判定；晚于截止的回报只能用来解释“为何没动用”，不能授权调用。
    """
    windows = availability(dataset, day)
    committed = commitments(dataset, day)
    audits = [
        audit_branch(dataset, branch, day, None, windows, committed)
        for branch in dataset.branches
    ]
    minutes = signal_minutes(dataset, day)
    overall = cutoff if cutoff is not None else (minutes[-1] if minutes else 0)
    deadline_view = desk_view(dataset, day, overall)

    targets: dict[str, StandingClass] = {}
    for branch in dataset.branches:
        target = target_for(dataset, branch, day)
        if target is not None:
            targets[target.class_id] = target
    for record in dataset.standing.values():
        if record.day == day:
            targets[record.class_id] = record

    day_reservations = [item for item in reservations(dataset) if item.day == day]
    records: list[PromiseRecord] = []
    for class_id, target in sorted(targets.items()):
        branch_ids = [branch.id for branch in dataset.branches if branch.class_id == class_id]
        related = [audit for audit in audits if audit.branch_id in branch_ids]
        later = dataset.outcomes.get(class_id)
        state = later.state if later else None
        records.append(
            PromiseRecord(
                day,
                class_id,
                target.site,
                target.kind,
                target.start,
                target.end,
                target.state,
                target.promised,
                tuple(item for item in day_reservations if item.class_id == class_id),
                tuple(audit.branch_id for audit in related if audit.executable),
                tuple(
                    decision.branch_id
                    for decision in deadline_view.dispatched
                    if decision.target_class == class_id
                ),
                {
                    audit.branch_id: audit.blockers
                    for audit in related
                    if audit.blockers
                },
                state,
                None if state is None else state == "held",
            )
        )
    return records


def _deadline_cutoff(dataset: Dataset, branch: Branch, day: date) -> int:
    target = target_for(dataset, branch, day)
    if target is None:
        return branch.leave_by
    latest = latest_departure(dataset, branch, target)
    return branch.leave_by if latest is None else min(branch.leave_by, latest)


# --------------------------------------------------------------------------
# 最差缺口的归因
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GapAttribution:
    day: date
    class_id: str | None
    site: str | None
    kind: str | None
    cause: str  # common_shortage / late_report / missing_signal / contradicted / no_contract / none
    detail: str
    clause_hit: bool


def attribute_gaps(dataset: Dataset, day: date) -> list[GapAttribution]:
    """把当天的失约按“共同缺师还是回报太晚”归因。

    共同缺师：同一天有多个校点同时缺师（含停课背景），或者当天所有分支都被
    同一组上游信号拖住；
    回报太晚：分支条件确实成立，但送交时刻晚于截止时刻；
    未收到：直到当天结束也没有回报；
    条件不成立：当天信号与分支约定相反。
    """
    results: list[GapAttribution] = []
    outcomes = {
        class_id: outcome
        for class_id, outcome in dataset.outcomes.items()
        if any(record.day == day for record in dataset.by_class.get(class_id, []))
    }
    failed_sites = {
        record.site
        for class_id, outcome in outcomes.items()
        if outcome.state == "understaffed"
        for record in dataset.by_class[class_id]
        if record.day == day
    }
    common = len(failed_sites) >= 2
    for class_id, outcome in sorted(outcomes.items()):
        if outcome.state != "understaffed":
            continue
        records = [record for record in dataset.by_class[class_id] if record.day == day]
        record = records[-1]
        # 历史班没有 F 编号，按“校点+课型+起止”匹配分支覆盖的格。
        branches = [
            branch
            for branch in dataset.branches
            if (target := target_for(dataset, branch, day)) is not None
            and target.site == record.site
            and target.kind == record.kind
            and target.start == record.start
            and target.end == record.end
        ]
        if not branches and common:
            results.append(
                GapAttribution(
                    day,
                    class_id,
                    record.site,
                    record.kind,
                    "common_shortage",
                    f"当日 {len(failed_sites)} 个校点同时缺师，且该课没有可约定的分支",
                    True,
                )
            )
            continue
        if not branches:
            results.append(
                GapAttribution(
                    day,
                    class_id,
                    record.site,
                    record.kind,
                    "no_contract",
                    "该课没有可约定的分支，且当日并非多校同时缺师",
                    False,
                )
            )
            continue
        audits = [audit_branch(dataset, branch, day, None) for branch in branches]
        late = [
            audit
            for audit in audits
            if any(blocker == "release_after_deadline" for blocker in audit.blockers)
        ]
        missing = [
            audit
            for audit in audits
            if any(blocker.startswith("waiting:") for blocker in audit.blockers)
        ]
        contradicted = [
            audit
            for audit in audits
            if any(blocker.startswith("contradicted:") for blocker in audit.blockers)
        ]
        if late:
            results.append(
                GapAttribution(
                    day,
                    class_id,
                    record.site,
                    record.kind,
                    "late_report",
                    "分支条件成立，但放人/回报晚于最迟出发时刻",
                    False,
                )
            )
        elif common:
            results.append(
                GapAttribution(
                    day,
                    class_id,
                    record.site,
                    record.kind,
                    "common_shortage",
                    f"当日 {len(failed_sites)} 个校点同时缺师",
                    True,
                )
            )
        elif missing:
            results.append(
                GapAttribution(
                    day,
                    class_id,
                    record.site,
                    record.kind,
                    "missing_signal",
                    "直到当天结束也没有收到所需校点的晨检回报",
                    False,
                )
            )
        elif contradicted:
            results.append(
                GapAttribution(
                    day,
                    class_id,
                    record.site,
                    record.kind,
                    "contradicted",
                    "当天信号与分支约定相反，分支本就不应触发",
                    False,
                )
            )
        else:
            results.append(
                GapAttribution(
                    day,
                    class_id,
                    record.site,
                    record.kind,
                    "no_feasible_window",
                    "分支条件成立但路程或出借窗口不允许按时到场",
                    False,
                )
            )
    return results


def gap_summary(dataset: Dataset) -> dict:
    from collections import Counter

    causes = Counter()
    for day in sorted({record.day for record in dataset.classes}):
        for item in attribute_gaps(dataset, day):
            causes[item.cause] += 1
    return {
        "causes": dict(sorted(causes.items())),
        "reading": [
            "共同缺师（同一分层多校同时缺师）只能靠保留联合概率与预留备班缓解。",
            "回报太晚与未收到信号是可观测的出借方履约问题，不是概率问题。",
        ],
    }


# --------------------------------------------------------------------------
# 互斥分支的可区分性
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BranchPairing:
    staff: str
    first: str
    second: str
    shared_conditions: tuple[tuple[str, str], ...]
    distinguishing: tuple[tuple[str, str], ...]
    observable_before_deadlines: bool
    deadlines: dict[str, int]
    detail: str


def exclusive_pairings(dataset: Dataset, day: date) -> list[BranchPairing]:
    """检查同一个助教被写进多个互斥分支时，信号是否足以区分。

    只有“可区分条件”在两条分支的截止时刻之前都能收到，才允许同时保留；
    否则应在通知时只保留一条。
    """
    results: list[BranchPairing] = []
    by_staff: dict[str, list[Branch]] = {}
    for branch in dataset.branches:
        by_staff.setdefault(branch.staff, []).append(branch)
    reports = signal_reports(dataset, day)
    for staff, branches in sorted(by_staff.items()):
        for index, first in enumerate(branches):
            for second in branches[index + 1 :]:
                shared = tuple(
                    condition
                    for condition in first.conditions
                    if condition in second.conditions
                )
                distinguishing = tuple(
                    {
                        condition
                        for condition in first.conditions + second.conditions
                        if condition not in shared
                    }
                )
                deadlines = {
                    first.id: _branch_deadline(dataset, first, day),
                    second.id: _branch_deadline(dataset, second, day),
                }
                observable = all(
                    site in reports
                    and reports[site][1] <= max(
                        deadline for deadline in deadlines.values()
                    )
                    for site, _ in distinguishing
                )
                results.append(
                    BranchPairing(
                        staff,
                        first.id,
                        second.id,
                        shared,
                        distinguishing,
                        observable,
                        deadlines,
                        (
                            "区分信号在最晚截止时刻前均已收到"
                            if observable
                            else "区分信号无法在截止前确认，不应同时保留"
                        ),
                    )
                )
    return results


def _branch_deadline(dataset: Dataset, branch: Branch, day: date) -> int:
    target = target_for(dataset, branch, day)
    if target is None:
        return branch.leave_by
    latest = latest_departure(dataset, branch, target)
    return branch.leave_by if latest is None else min(branch.leave_by, latest)
