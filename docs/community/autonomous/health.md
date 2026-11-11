# 策略健康熔断

`vnpy.autonomous.health.HealthMonitor` 逐策略跟踪成交、盈亏、连续亏损与错误次数，并按规则熔断：

| 规则 | 字段 |
| --- | --- |
| 连续亏损次数 | `max_consecutive_losses` |
| 当日亏损 | `max_daily_loss`（按自然日重置） |
| 单位窗口错误次数 | `max_error_count` / `error_window_secs` |
| 熔断冷却 | `halt_cooldown_secs`（到期后自动恢复） |

熔断与恢复通过回调交给运行器执行：默认调用引擎的 `stop_strategy(strategy_name)` / `start_strategy(strategy_name)`，引擎未提供时仅记录状态。

## 数据来源

- 成交台账：`EVENT_TRADE` 按委托的 `reference` 归集到策略（vnpy_ctastrategy 的委托 reference 即策略名）。
- 错误：`EVENT_ORDER` 的拒单、以及 ERROR 及以上级别的 `EVENT_LOG` 计入对应策略（按 reference），全局桶为 `__global__`。
- 需要按策略盈亏精细熔断时，策略侧可发布 `EVENT_STRATEGY_SNAPSHOT`（见 [runner](./runner.md)），携带 `realized_pnl`。
