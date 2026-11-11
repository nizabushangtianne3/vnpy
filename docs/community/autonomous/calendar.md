# 交易日历

`vnpy.autonomous.calendar.TradingCalendar` 回答三个问题：今天是否交易日、现在是否在交易时段、下次什么时候开盘。

## 时段模板

| 模板 | 日盘 | 夜盘 |
| --- | --- | --- |
| `cn_futures` | 09:00-11:30、13:30-15:00 | 21:00-night_end（默认 23:00） |
| `cn_futures_midnight` | 同上 | 21:00-02:30（贵金属、原油等） |
| `full_day` | 全天（忽略周末） | - |

也支持自定义 `sessions` 列表：`[{"name": "day", "start": "09:00", "end": "11:30"}]`，`end` 早于 `start` 即跨零点时段。

## 归属规则

- 交易日 = 工作日 且 不在节假日清单（`holidays_file`，JSON 日期数组，相对配置目录解析）。
- 夜盘属于开始当日：周五 21:00-23:00 即使只延续数分钟也在周五名下，周六凌晨不再开盘。
- `next_open` 返回严格晚于当前时刻的下一开盘时刻，已内置跳过周末与节假日。

节假日清单需按交易所公告自行维护（`examples/autonomous/holidays.example.json` 为模板）。
