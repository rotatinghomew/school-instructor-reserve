# school-instructor-reserve

校外传统体育带班、助教与跨点课表。 其中人物代号、机构、交易及历史回执均为合成资料。

## 数据约定

`data/layout.json` 逐一解释业务表及 CSV 列顺序，`data/rules.json` 记录运营口径。CSV 使用 UTF-8、首行为列名；空白字段表示未提供或不适用，`+` 隔开多值字段。历史记录的业务时刻与送交时刻含义不同，按各表说明解释。

- `staff.csv`：跨校教练与资格，commute 为同出行扰动组。
- `timetable.csv`：排班事实及首次可见时刻，同一班号记录可向后更新。
- `shifts.csv`：历史班次时间为通知时可排窗口，实际到岗状态和确认时间是结局；计划日期不晚于该周通知截面。
- `outcomes.csv`：完课回执的最后状态及到达时间。
- `standing.csv`：新一期各校希望保留或增开的班。
- `reinforcements.csv`：本周增援窗口及学校分批确认的原始回执。
- `rollcalls.csv`：每个地点晨检在某刻得到的原始回报，reported_minute 可晚于实际到岗；空白意味着始终未收。
- `mutual_aid.csv`：可约定的条件调用；condition 形如 校点/信号，多个条件同时成立才触发。
- `call_round.csv`：本周实际到达的证明和接受回执，与历史早读数分开。

## 业务口径

实现见 `school_reserve/`，每个模块顶部都写明它遵守的约定。

### 通知截面与结局

- 每周的通知截面取该周课表最早的 `seen`（`rules.notice_cutoff`，周日前一个工作日
  17:00）。截面之后到达的版本（停课、改点）**不进入当周预测**，只作事后结局。
- 结局分四类：`held`（已证实开课）、`understaffed`（已证实缺师）、
  `receipt_missing`（未收回执，**不是**确认失约）、`closed`（停止开放，
  **不是**零需求）。校准只在没有停课、没有未收回执的日子上做。
- `rollcalls` 的 `reported_minute` 是送交时刻；回放只用截止时刻前已收到的
  回报，未回报的校点按“尚未知道”处理，绝不用当天最终 `actual` 倒推。

### 可兑现带班数预报（`forecast.py`）

- 按 `(天气展望, 三校课型排布)` 分层，训练样本只取此前各周**已证实**的完整
  日子；层内样本不足时依次退到 `weather` / `layout` / `global`，并标注基准。
- 缺口在同一分层里按**整日联合状态**取样，保留多校同时缺师的相关性。
  联合风险 “同一层里至少两节优先课同日缺师”是承诺口径；
  把单校边际独立相乘只作对照基准（`independent_sites`），不作承诺依据。
- 单元格是（校点、课型、起止时刻）。历史周数不足
  `min_history_weeks_per_type_class` 的格**明确弃答**，不拿跨校平均补位。
- 区间取覆盖不少于 `coverage_interval_level` 的最短连续整数区间；
  另给 `median` 与 `expected_lost_minutes`。

### 按周滚动回测（`backtest` / `summary`）

同一组通知截面逐周向前滚，比较：

| 指标 | 说明 |
| --- | --- |
| `coverage` | 预报区间是否盖住当日证实开课数 |
| `mae` | 点预报（均值）误差 |
| `joint_priority_brier` | 优先课联合缺口的校准 |
| `promised_minutes` / `lost_minutes` | 已承诺课时与证实失约课时 |
| `high_demand` | 联合风险高于 `max_joint_uncovered_priority_probability` 的日子，及其中守不住的课时占比 |

对照组：`climatology`（不分层气候平均）、`independent_sites`（单校风险独立
相加）、`fixed_promise`（按满额承诺）。每条记录都带 `origin`，不存在用未来
周训练当周的情况。

### 援班窗口与调用（`dispatch.py`）

- **可用窗口**来自通知时已知的排班与增援回执：当期以 `reinforcements` 为准，
  `shortened` 替换被其引用的原窗口；历史日回落到 `shifts`。两者都不读 `actual`。
- **承诺占用**当周取 `standing.notified_staff`；历史日没有逐人派单记录，
  不能假装知道，只用于校对同一人重复占用。
- 分支截止时刻 = `min(leave_by, 目标课开课 − 路程)`；调用时刻 =
  `max(dispatch_at, 各条件回报时刻)`，必须不晚于截止时刻。
- 分支还须满足：目标校点在该出借人可去范围内；窗口覆盖出发、到场与整节课；
  不与本人已通知的课重叠；成对课型须有 **一名主教加一名合格副教**在场，
  不能同一个人算两次。
- 同一人不可同时占用两个互斥分支；同一目标课只记一个方案。判定的顺序
  （优先课型 → 到达时刻 → 分支编号）固定，因此同一信息状态必然得到同一调用表；
  `determinism_audit` 会逐日核对。
- **无前视回放**：按当天每个晨检回报时刻逐点重放，每个决定点只用当时已收到
  的信号。同一分支派出后不重复计入。`replay_outcomes` 把决定与事后结局并排，
  只用于评分。

### 预留与承诺台账

- 校方接受（`school_accepts` / `school_confirmed`）只形成**预留**，不等于
  教练已到；`reservations` 保留其收到时刻与收窄后的结束分钟。
- `promise_ledger` 对每节已承诺课保留原承诺、真实预留、可执行分支、已派分支、
  全部阻碍，以及事后结局；`unused_reason` 区分 `signal_never_received`、
  `release_came_too_late`、`condition_never_held`、`no_feasible_window` 等。
- 迟到的放人回报只能解释“为何没动用”，不能追认已过时的援班。

### 最差缺口归因与互斥分支

- `attribute_gaps` 把每个失约归到 `common_shortage`（同日多校同时缺师）、
  `late_report`、`missing_signal`、`contradicted`、`no_contract`、
  `no_feasible_window` 之一。
- `exclusive_pairings` 检查同一位助教被写进多个互斥分支时，区分信号是否在
  两条分支的截止时刻之前都能收到；只有能区分时才允许同时保留。

### 固定备班与联保方案（`strategies.py`）

`compare_strategies` 用同一批历史日、同一套结局口径比较：

- `conditional`：只在分支全部条件收到且早于截止时派出；
- `fixed`：预置在东桥的固定备班，只看目标校 `shortage` 且早于开课就派出。

两者都报告 `dispatches / protected / false_alarms / missed_needs / mean_call_minute`，
且都不用当天最终到岗挑日子。

## 本地检查

使用 Python 3.11 及标准库。

```bash
python3 inspect_inputs.py            # 读取原始资料并列出记录数
python3 -m unittest discover -s tests  # 输入资料 + 业务规则测试
```

## 报告

```bash
python3 -m school_reserve.cli all --out reports
python3 -m school_reserve.cli forecast --origin 21
python3 -m school_reserve.cli desk --day 2026-10-05 --cutoff 560
python3 -m school_reserve.cli promises --day 2026-10-05
python3 -m school_reserve.cli backtest
python3 -m school_reserve.cli strategies
python3 -m school_reserve.cli determinism
```

`all` 生成：逐日预报、承诺班表试算、回测与基准对照、值班处调用表、承诺台账、
“实际调用表 vs 概率试算”差异说明、两种备班策略对照、调用约定一致性审计、
以及最差缺口的归因。报告只写结论与可复核的数字，不修改 `data/`。
