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

## 本地检查

使用 Python 3.11 及标准库。`python3 inspect_inputs.py` 读取原始资料并列出记录数；`python3 -m unittest discover -s tests` 检查输入资料。检查不计算或发布业务结论。
