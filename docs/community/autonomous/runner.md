# 运行器

`vnpy.autonomous.runner.AutonomousRunner` 分两个角色，对应 `python -m vnpy.autonomous run`：

## 父级守护进程

按交易日历工作：交易时段内拉起子进程，时段结束等待子进程自行退出（宽限后强制结束），子进程崩溃按 `restart_backoff_secs` 冷却重启，同一时段超过 `max_restarts_per_session` 则放弃并通知。父进程还监控子进程状态文件的新鲜度。

## 交易子进程

1. 创建 `EventEngine` 与 `MainEngine`（自带 Log/Oms/Email/Wechat 引擎）。
2. 安装 RiskGuard（包裹 `send_order` 并注册委托/成交/账户/持仓事件）。
3. 按配置连接网关、加载应用模块，等待合约推送（`contract_wait_secs`）。
4. 主循环：进入时段时依次执行 `init_engine` → 按 `app_settings.strategies` 自动装配策略 → `init_all_strategies` → 等待 → `start_all_strategies`；持续做心跳、看门狗、健康冷却恢复、策略快照发布；时段结束停止策略并按配置退出。

## 状态文件

`.vntrader/autonomous_state.json` 原子更新（临时文件 + 替换），字段包括角色、模式、当前时段、下次开盘时间、风控快照、策略健康、资金分配、成交台账、各网关最近事件时间。父进程据此判断存活，外部值守系统可直接消费。

## 自定义统计接入

向事件引擎发布以下事件即可把任意策略引擎的统计接入健康与分配：

```python
from vnpy.event import Event, EventEngine
from vnpy.autonomous.event import EVENT_STRATEGY_SNAPSHOT, StrategySnapshot

event_engine.put(Event(
    EVENT_STRATEGY_SNAPSHOT,
    StrategySnapshot(
        strategy_name="my_trend",
        trading=True,
        positions=2,
        trades=17,
        realized_pnl=1234.5,
        equity=101234.5,
    ),
))
```

`python -m vnpy.autonomous sessions` 可查看内置接口/模块清单与后续开盘时间。

## 策略统计适配器

装配条目格式：

```json
"app_settings": {
  "CtaStrategy": {
    "strategies": [
      {"class_name": "MyCtaTemplate", "name": "my_trend", "vt_symbol": "rb2410.SHFE", "setting": {"fixed_size": 1}}
    ]
  }
}
```

vnpy_ctastrategy 的 CtaEngine 通过 `add_strategy(class_name, name, vt_symbol, setting)` 装配（与界面同入口），`name` 可省略（默认 `类名_合约代码`）；已存在的同名策略跳过，重启不重复。成交归属优先使用引擎的 `orderid_strategy_map`，其次解析 `reference` 中的 `应用名_` 前缀。

加载的引擎若暴露 `strategies` 字典（`vnpy_ctastrategy` 的 CtaEngine 即是），运行器会自动挂载 `vnpy.autonomous.adapters.cta.CtaStatsAdapter`，在主循环里把每个策略的 `trading`/`pos`/`trades` 状态以及 `variables` 中的 `realized_pnl`、`equity` 包装成 `StrategySnapshot` 事件发出，健康监控据此做按策略盈亏的熔断，资金分配器据此估计波动率。策略只需在 `variables` 里维护 `realized_pnl` 与 `equity` 两个键即可接入。

## 其他引擎

没有 `strategies` 字典的引擎（如 PortfolioStrategy、自定义组合引擎），可自行向事件引擎发布 `StrategySnapshot`，运行器会同样消费（见上文示例代码）。
