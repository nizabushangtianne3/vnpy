# 自主交易运行器

`vnpy.autonomous` 是核心框架内置的无人值守交易运维层，目标是让一个策略组合在交易日历、事前风控、策略健康熔断和心跳监控的保护下持续自主运行。设计上参考了 Freqtrade（交易循环 + dry-run + 持久化）与 Hummingbot（控制器-执行器分层）的工程实践，并与 VeighNa 既有生态（`vnpy_ctastrategy`、`vnpy_riskmanager`、`vnpy_paperaccount` 等 pip 模块）协作而非重复。

## 组成

| 模块 | 职责 |
| --- | --- |
| [calendar](./calendar.md) | 交易日历：日盘/夜盘时段、跨零点归属、节假日与调休 |
| [config](./config.md) | JSON 配置模型（网关、应用、风控、健康、资金、通知、运行时） |
| [risk](./risk.md) | 事前风控守卫生成器：包裹 `MainEngine.send_order`，含熔断平仓 |
| [health](./health.md) | 策略健康熔断：连续亏损/当日亏损/错误率，冷却自愈 |
| [portfolio](./portfolio.md) | 资金分配：等权与逆波动率，水填法应用权重上限 |
| [runner](./runner.md) | 父级守护 + 交易子进程编排，状态文件与心跳 |

从仿真到实盘的推进节奏见 [晋升手册](./promotion.md)。

## 运行方式

```bash
python -m vnpy.autonomous check   --config .vntrader/autonomous.json
python -m vnpy.autonomous run     --config .vntrader/autonomous.json
```

完整字段说明见 [examples/autonomous/README.md](https://github.com/vnpy/vnpy/blob/master/examples/autonomous/README.md)。

## 扩展点

- 新网关/新应用：`gateways`/`apps` 配置项直接写 `"module:Class"` 导入路径。
- 自定义策略统计：向事件引擎发布 `EVENT_STRATEGY_SNAPSHOT`（`vnpy.autonomous.event.StrategySnapshot`），运行器会将其喂给健康监控与资金分配器，支持按策略盈亏触发熔断。
- 外部值守：读取 `.vntrader/autonomous_state.json`（原子更新）获取角色、时段、风控与策略健康快照。
