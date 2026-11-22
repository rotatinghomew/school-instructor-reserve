"""按周滚动的通知截面预报：可兑现带班数、区间、联合缺口风险。

口径：

* 只用通知截面上已知的天气展望与课型排布分层；训练样本限此前各周的
  **已证实**完整日子（``held`` / ``understaffed``）。停课（``closed``）不是
  零需求，未收回执（``receipt_missing``）也不是确认失约，二者都不进训练集。
* 缺口在同一分层内按**整日联合状态**取样，保留多校同时缺师的相关性。
  独立相乘单校边际只作对照基准，不作承诺依据。
* 单元格为（校点、课型、起止时刻）。历史周数不足
  ``min_history_weeks_per_type_class`` 的单元格不进入承诺，也不拿跨校平均补位。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from .tables import ClassRecord, Dataset, SITES
from .timeline import snapshot

MIN_STRATUM_DAYS = 6
LAPLACE = 0.5


# --------------------------------------------------------------------------
# 需求与历史
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DemandCell:
    class_id: str
    day: date
    site: str
    kind: str
    start: int
    end: int
    promised: bool

    @property
    def key(self) -> tuple[str, str, int, int]:
        return (self.site, self.kind, self.start, self.end)

    @property
    def site_index(self) -> int:
        return SITES.index(self.site)

    @property
    def minutes(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class HistoryDay:
    day: date
    weather: str
    layout: tuple[tuple[str, str], ...]  # 按 SITES 顺序的（校点, 课型）
    held: tuple[bool, ...]

    def failed(self, index: int) -> bool:
        return not self.held[index]


def _complete_history(dataset: Dataset, origin: int) -> list[HistoryDay]:
    days: list[HistoryDay] = []
    for week in dataset.weeks:
        if week >= origin:
            continue
        snap = snapshot(dataset, week)
        grouped: dict[date, dict[str, ClassRecord]] = defaultdict(dict)
        for record in snap.records:
            grouped[record.day][record.site] = record
        for day, records in sorted(grouped.items()):
            if set(records) != set(SITES):
                continue
            outcomes = [dataset.outcomes[records[site].class_id] for site in SITES]
            if not all(outcome.known for outcome in outcomes):
                continue
            days.append(
                HistoryDay(
                    day,
                    records[SITES[0]].forecast_opening,
                    tuple((site, records[site].kind) for site in SITES),
                    tuple(outcome.delivered for outcome in outcomes),
                )
            )
    return days


def cell_history_weeks(
    dataset: Dataset, origin: int
) -> dict[tuple[str, str, int, int], int]:
    """每个（校点、课型、起止）在此前已证实结局的周数。"""
    weeks: dict[tuple[str, str, int, int], set[int]] = defaultdict(set)
    for week in dataset.weeks:
        if week >= origin:
            continue
        for record in snapshot(dataset, week).records:
            outcome = dataset.outcomes.get(record.class_id)
            if outcome and outcome.known:
                weeks[record.slot].add(week)
    return {key: len(value) for key, value in weeks.items()}


def committable_cells(
    dataset: Dataset, origin: int, cells: list[DemandCell]
) -> tuple[list[DemandCell], dict[str, str]]:
    """分出证据足够的单元格；不足的明确弃答，不回填跨校平均。"""
    history = cell_history_weeks(dataset, origin)
    minimum = int(dataset.rules["min_history_weeks_per_type_class"])
    allowed: list[DemandCell] = []
    rejected: dict[str, str] = {}
    for cell in cells:
        if history.get(cell.key, 0) >= minimum:
            allowed.append(cell)
        else:
            rejected[cell.class_id] = "insufficient_cell_history"
    return allowed, rejected


# --------------------------------------------------------------------------
# 概率工具
# --------------------------------------------------------------------------


def _counts(days: list[HistoryDay], indices: tuple[int, ...]) -> dict[int, int]:
    return dict(
        Counter(sum(1 for index in indices if day.held[index]) for day in days)
    )


def _smoothed_counts(counts: dict[int, int], size: int) -> dict[int, float]:
    total = sum(counts.values())
    return {
        value: (counts.get(value, 0) + LAPLACE) / (total + LAPLACE * (size + 1))
        for value in range(size + 1)
    }


def shortest_interval(distribution: dict[int, float], level: float) -> tuple[int, int]:
    """质量不少于 level 的最短连续整数区间；并列取靠均值者。"""
    keys = sorted(distribution)
    if not keys:
        return 0, 0
    centre = sum(value * probability for value, probability in distribution.items())
    best: tuple[int, float, int, int] | None = None
    for low in keys:
        mass = 0.0
        for high in keys:
            if high < low:
                continue
            mass += distribution[high]
            if mass >= level - 1e-12:
                candidate = (high - low, abs((low + high) / 2 - centre), low, high)
                if best is None or candidate[:2] < best[:2]:
                    best = candidate
                break
    if best is None:
        return keys[0], keys[-1]
    return best[2], best[3]


def median_value(distribution: dict[int, float]) -> int:
    cumulative = 0.0
    for value in sorted(distribution):
        cumulative += distribution[value]
        if cumulative >= 0.5 - 1e-12:
            return value
    return sorted(distribution)[-1]


def _failure_rate(days: list[HistoryDay], index: int) -> float:
    failures = sum(1 for day in days if day.failed(index))
    return (failures + LAPLACE) / (len(days) + 2 * LAPLACE)


def _cluster_threshold(size: int) -> int:
    """“缺口共同出现”的门槛：一节时就是它自己，多节时至少两节同时缺师。"""
    return 2 if size >= 2 else 1


def _joint_rate(days: list[HistoryDay], indices: tuple[int, ...]) -> float:
    """同一分层里，至少 ``threshold`` 节课同一天缺师的比例。

    整日取样，保留共同通勤阻断、急雨等造成的相关性；这正是不能把单校风险
    独立相加的原因。
    """
    if not indices:
        return 0.0
    threshold = _cluster_threshold(len(indices))
    hits = sum(
        1
        for day in days
        if sum(1 for index in indices if day.failed(index)) >= threshold
    )
    return (hits + LAPLACE) / (len(days) + 2 * LAPLACE)


def _independent_joint_rate(days: list[HistoryDay], indices: tuple[int, ...]) -> float:
    """把每个校点的边际失约率独立相乘，得到同口径的对照。"""
    if not indices:
        return 0.0
    threshold = _cluster_threshold(len(indices))
    distribution: dict[int, float] = {0: 1.0}
    for index in indices:
        failure = _failure_rate(days, index)
        updated: dict[int, float] = defaultdict(float)
        for count, probability in distribution.items():
            updated[count] += probability * (1 - failure)
            updated[count + 1] += probability * failure
        distribution = dict(updated)
    return sum(
        probability for count, probability in distribution.items() if count >= threshold
    )


def _layout(cells: list[DemandCell]) -> tuple[tuple[str, str], ...]:
    """按 SITES 顺序排列的（校点, 课型），与历史日对齐。"""
    return tuple((cell.site, cell.kind) for cell in sorted(cells, key=lambda c: c.site_index))


def _select_stratum(
    history: list[HistoryDay], weather: str, layout: tuple[tuple[str, str], ...]
) -> tuple[str, list[HistoryDay]]:
    """天气优先于排布：共同通勤阻断与急雨才是多校同时缺师的主因。

    排布只用来对齐哪几个校点在上课；湿天里没有同一排布样本时，仍用全部
    湿天，以免把“共同缺师”降级成单校独立风险。
    """
    same_weather = [day for day in history if day.weather == weather]
    same_layout = [day for day in history if day.layout == layout]
    both = [day for day in same_weather if day.layout == layout]
    candidates = (
        ("weather+layout", both),
        ("weather", same_weather),
        ("layout", same_layout),
        ("global", list(history)),
    )
    for name, subset in candidates:
        if len(subset) >= MIN_STRATUM_DAYS:
            return name, subset
    for name, subset in candidates:
        if subset:
            return name + "(thin)", subset
    return "empty", []


# --------------------------------------------------------------------------
# 预报
# --------------------------------------------------------------------------


@dataclass
class CellForecast:
    class_id: str
    site: str
    kind: str
    start: int
    end: int
    promised: bool
    committable: bool
    reason: str | None
    failure_probability: float | None

    @property
    def minutes(self) -> int:
        return self.end - self.start

    @property
    def expected_lost_minutes(self) -> float:
        if self.failure_probability is None or not self.promised:
            return 0.0
        return self.minutes * self.failure_probability


@dataclass
class DayForecast:
    day: date
    weather: str
    layout: tuple[tuple[str, str], ...]
    basis: str
    sample: int
    cells: tuple[CellForecast, ...]
    rejected: dict[str, str]
    distribution: dict[int, float]
    mean: float
    median: int
    low: int
    high: int
    joint_priority_risk: float | None
    independent_priority_risk: float | None
    abstain_reason: str | None
    baselines: dict[str, dict] = field(default_factory=dict)

    @property
    def committable(self) -> tuple[CellForecast, ...]:
        return tuple(cell for cell in self.cells if cell.committable)

    @property
    def committable_minutes(self) -> int:
        return sum(cell.minutes for cell in self.committable)

    @property
    def promised_minutes(self) -> int:
        return sum(cell.minutes for cell in self.committable if cell.promised)

    @property
    def expected_lost_minutes(self) -> float:
        return sum(cell.expected_lost_minutes for cell in self.cells)

    @property
    def expected_delivered(self) -> float:
        return sum(
            1 - cell.failure_probability
            for cell in self.committable
            if cell.failure_probability is not None
        )


def _abstained_day(
    day: date,
    weather: str,
    cells: list[DemandCell],
    rejected: dict[str, str],
    reason: str,
    basis: str = "empty",
    sample: int = 0,
) -> DayForecast:
    return DayForecast(
        day,
        weather,
        _layout(cells),
        basis,
        sample,
        tuple(
            CellForecast(
                cell.class_id,
                cell.site,
                cell.kind,
                cell.start,
                cell.end,
                cell.promised,
                False,
                rejected.get(cell.class_id, reason),
                None,
            )
            for cell in cells
        ),
        dict(rejected),
        {},
        float("nan"),
        -1,
        -1,
        -1,
        None,
        None,
        reason,
    )


def forecast_cells(
    dataset: Dataset,
    origin: int,
    day: date,
    cells: list[DemandCell],
    weather: str,
) -> DayForecast:
    """对一天的若干节课给出可兑现数、区间与联合缺口风险。"""
    history = _complete_history(dataset, origin)
    allowed, rejected = committable_cells(dataset, origin, cells)

    if not allowed:
        return _abstained_day(day, weather, cells, rejected, "no_committable_cell")

    sites = [cell.site for cell in allowed]
    if len(set(sites)) != len(sites):
        return _abstained_day(day, weather, cells, rejected, "unseen_slate")

    # 校点下标必须是历史日里真实存在的，且当天该位置确实在上同一课型。
    layout = _layout(allowed)
    indices = tuple(SITES.index(cell.site) for cell in allowed)
    for index, cell in zip(indices, allowed):
        if not any(
            day_history.layout[index] == (cell.site, cell.kind) for day_history in history
        ):
            return _abstained_day(day, weather, cells, rejected, "unseen_slate")

    basis, stratum = _select_stratum(history, weather, layout)
    if not stratum:
        return _abstained_day(day, weather, cells, rejected, "no_history")

    counts = _counts(stratum, indices)
    smooth = _smoothed_counts(counts, len(indices))
    low, high = shortest_interval(smooth, float(dataset.rules["coverage_interval_level"]))
    mean = sum(value * probability for value, probability in smooth.items())
    median = median_value(smooth)

    failure = {index: _failure_rate(stratum, index) for index in indices}
    joint = _joint_rate(stratum, indices)
    independent = _independent_joint_rate(stratum, indices)

    # 对照基准：不分层的气候平均；把联合状态拆成独立边际；固定满额承诺。
    climate_counts = _counts(history, indices)
    climate_smooth = _smoothed_counts(climate_counts, len(indices))
    independent_counts: dict[int, float] = {0: 1.0}
    for position, index in enumerate(indices):
        updated: dict[int, float] = defaultdict(float)
        for count, probability in independent_counts.items():
            updated[count] += probability * (1 - failure[index])
            updated[count + 1] += probability * failure[index]
        independent_counts = dict(updated)

    baselines = {
        "climatology": {
            "mean": sum(value * probability for value, probability in climate_smooth.items()),
            "low": shortest_interval(
                climate_smooth, float(dataset.rules["coverage_interval_level"])
            )[0],
            "high": shortest_interval(
                climate_smooth, float(dataset.rules["coverage_interval_level"])
            )[1],
            "sample": len(history),
        },
        "independent_sites": {
            "mean": sum(value * probability for value, probability in independent_counts.items()),
            "low": shortest_interval(
                independent_counts, float(dataset.rules["coverage_interval_level"])
            )[0],
            "high": shortest_interval(
                independent_counts, float(dataset.rules["coverage_interval_level"])
            )[1],
            "joint_priority_risk": independent,
        },
        "fixed_promise": {
            "mean": float(len(allowed)),
            "low": len(allowed),
            "high": len(allowed),
        },
    }

    cell_forecasts = []
    for cell in cells:
        if cell.class_id in rejected:
            cell_forecasts.append(
                CellForecast(
                    cell.class_id,
                    cell.site,
                    cell.kind,
                    cell.start,
                    cell.end,
                    cell.promised,
                    False,
                    rejected[cell.class_id],
                    None,
                )
            )
            continue
        index = SITES.index(cell.site)
        cell_forecasts.append(
            CellForecast(
                cell.class_id,
                cell.site,
                cell.kind,
                cell.start,
                cell.end,
                cell.promised,
                True,
                None,
                failure[index],
            )
        )

    return DayForecast(
        day,
        weather,
        layout,
        basis,
        len(stratum),
        tuple(cell_forecasts),
        rejected,
        smooth,
        mean,
        median,
        low,
        high,
        joint,
        independent,
        None,
        baselines,
    )


def forecast_week(dataset: Dataset, origin: int) -> list[DayForecast]:
    snap = snapshot(dataset, origin)
    grouped: dict[date, list[ClassRecord]] = defaultdict(list)
    for record in snap.records:
        grouped[record.day].append(record)
    return [
        forecast_cells(
            dataset,
            origin,
            day,
            [
                DemandCell(
                    record.class_id,
                    record.day,
                    record.site,
                    record.kind,
                    record.start,
                    record.end,
                    promised=True,
                )
                for record in records
            ],
            records[0].forecast_opening,
        )
        for day, records in sorted(grouped.items())
    ]


def forecast_standing(
    dataset: Dataset, origin: int, weather: str
) -> list[DayForecast]:
    """用 standing 的承诺班表预报下一期；天气展望未给定时按指定情景。"""
    grouped: dict[date, list] = defaultdict(list)
    for record in dataset.standing.values():
        grouped[record.day].append(record)
    return [
        forecast_cells(
            dataset,
            origin,
            day,
            [
                DemandCell(
                    record.class_id,
                    record.day,
                    record.site,
                    record.kind,
                    record.start,
                    record.end,
                    record.promised,
                )
                for record in records
            ],
            weather,
        )
        for day, records in sorted(grouped.items())
    ]


# --------------------------------------------------------------------------
# 回测
# --------------------------------------------------------------------------


@dataclass
class BacktestRecord:
    origin: int
    day: date
    weather: str
    layout: tuple[tuple[str, str], ...]
    basis: str
    sample: int
    committable_ids: tuple[str, ...]
    abstain_reason: str | None
    mean: float
    median: int
    low: int
    high: int
    joint: float | None
    independent_joint: float | None
    actual_held: int
    actual_lost_minutes: int
    promised_minutes: int
    cancelled: int
    unknown: int
    baselines: dict[str, dict]


def _day_actuals(
    dataset: Dataset, day: date, forecast: DayForecast
) -> tuple[int, int, int, int, int]:
    held = 0
    lost = 0
    promised = 0
    cancelled = 0
    unknown = 0
    for cell in forecast.committable:
        outcome = dataset.outcomes[cell.class_id]
        promised += cell.minutes
        if outcome.delivered:
            held += 1
        elif outcome.cancelled:
            cancelled += 1
        elif outcome.unknown:
            unknown += 1
        else:
            lost += cell.minutes
    return held, lost, promised, cancelled, unknown


def backtest(dataset: Dataset, first_origin: int | None = None) -> list[BacktestRecord]:
    """按周向前滚动，同一组通知截面逐一评分。"""
    minimum = int(dataset.rules["min_history_weeks_per_type_class"])
    start = first_origin if first_origin is not None else minimum
    records: list[BacktestRecord] = []
    for origin in dataset.weeks:
        if origin < start:
            continue
        for forecast in forecast_week(dataset, origin):
            held, lost, promised, cancelled, unknown = _day_actuals(
                dataset, forecast.day, forecast
            )
            records.append(
                BacktestRecord(
                    origin,
                    forecast.day,
                    forecast.weather,
                    forecast.layout,
                    forecast.basis,
                    forecast.sample,
                    tuple(cell.class_id for cell in forecast.committable),
                    forecast.abstain_reason,
                    forecast.mean,
                    forecast.median,
                    forecast.low,
                    forecast.high,
                    forecast.joint_priority_risk,
                    forecast.independent_priority_risk,
                    held,
                    lost,
                    promised,
                    cancelled,
                    unknown,
                    forecast.baselines,
                )
            )
    return records


def eligible(records: list[BacktestRecord]) -> list[BacktestRecord]:
    """校准集：无停课、无未证实回执，且未弃答。"""
    return [
        record
        for record in records
        if record.cancelled == 0
        and record.unknown == 0
        and record.abstain_reason is None
        and not math.isnan(record.mean)
    ]


def summary(records: list[BacktestRecord], rules: dict) -> dict:
    level = float(rules["coverage_interval_level"])
    budget = float(rules["max_joint_uncovered_priority_probability"])
    sample = eligible(records)

    def interval_hit(row: BacktestRecord, source: str | None = None) -> bool:
        if source is None:
            low, high = row.low, row.high
        else:
            low, high = row.baselines[source]["low"], row.baselines[source]["high"]
        return low <= row.actual_held <= high

    def coverage(rows: list[BacktestRecord], source: str | None = None) -> float:
        return sum(interval_hit(row, source) for row in rows) / len(rows) if rows else float("nan")

    def mae(rows: list[BacktestRecord], source: str | None = None) -> float:
        if not rows:
            return float("nan")
        return sum(
            abs(
                (row.baselines[source]["mean"] if source else row.mean)
                - row.actual_held
            )
            for row in rows
        ) / len(rows)

    def brier(rows: list[BacktestRecord], source: str | None = None) -> float:
        scored = []
        for row in rows:
            size = len(row.committable_ids)
            if size == 0:
                continue
            threshold = _cluster_threshold(size)
            actual = 1.0 if size - row.actual_held >= threshold else 0.0
            predicted = (
                row.joint
                if source is None
                else row.baselines[source]["joint_priority_risk"]
            )
            if predicted is None:
                continue
            scored.append((predicted - actual) ** 2)
        return sum(scored) / len(scored) if scored else float("nan")

    wet = [row for row in sample if row.weather == "wet"]
    clear = [row for row in sample if row.weather == "clear"]
    high_demand = [
        row for row in sample if row.joint is not None and row.joint > budget
    ]
    in_budget = [row for row in sample if row not in high_demand]

    def band(rows: list[BacktestRecord]) -> dict:
        return {
            "days": len(rows),
            "coverage": coverage(rows),
            "mae": mae(rows),
            "joint_priority_brier": brier(rows),
            "promised_minutes": sum(row.promised_minutes for row in rows),
            "lost_minutes": sum(row.actual_lost_minutes for row in rows),
        }

    return {
        "level": level,
        "joint_budget": budget,
        "eligible_days": len(sample),
        "excluded_closed_days": sum(1 for row in records if row.cancelled),
        "excluded_unknown_days": sum(
            1 for row in records if row.unknown and not row.cancelled
        ),
        "abstained_days": sum(1 for row in records if row.abstain_reason),
        "coverage": coverage(sample),
        "coverage_baselines": {
            "climatology": coverage(sample, "climatology"),
            "independent_sites": coverage(sample, "independent_sites"),
            "fixed_promise": coverage(sample, "fixed_promise"),
        },
        "mae": mae(sample),
        "mae_baselines": {
            "climatology": mae(sample, "climatology"),
            "independent_sites": mae(sample, "independent_sites"),
            "fixed_promise": mae(sample, "fixed_promise"),
        },
        "mean_interval_width": (
            sum(row.high - row.low for row in sample) / len(sample) if sample else float("nan")
        ),
        "joint_priority_brier": brier(sample),
        "joint_priority_brier_independent": brier(sample, "independent_sites"),
        "clear": band(clear),
        "wet": band(wet),
        "high_demand": band(high_demand),
        "within_budget": band(in_budget),
        "promised_minutes": sum(row.promised_minutes for row in sample),
        "lost_minutes": sum(row.actual_lost_minutes for row in sample),
        "loss_share_on_high_demand_days": (
            sum(row.actual_lost_minutes for row in high_demand)
            / max(1, sum(row.actual_lost_minutes for row in sample))
        ),
        "strata": dict(sorted(Counter(row.basis for row in sample).items())),
    }
