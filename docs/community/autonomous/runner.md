# 运行器

`vnpy.autonomous.runner.AutonomousRunner` 分两个角色，对应 `python -m vnpy.autonomous run`：

## 父级守护进程

按交易日历工作：交易时段内拉起子进程，时段结束等待子进程自行退出（宽限后强制结束），子进程崩溃按 `restart_backoff_secs` 冷却重启，同一时段超过 `max_restarts_per_session` 则放弃并通知。父进程还监控子进程状态文件的新鲜度。

## 交易子进程

1. 创建 `EventEngine` 与 `MainEngine`（自带 Log/Oms/Email/Wechat 引擎）。
2. 安装 RiskGuard（包裹 `send_order` 并注册委托/成交/账户/持仓事件）。
3. 按配置连接网关、加载应用模块，等待合约推送（`contract_wait_secs`）。
4. 主循环：进入时段时执行 `init_engine` / `init_all_strategies` / `start_all_strategies`；持续做心跳、看门狗、健康冷却恢复检查；时段结束停止策略并按配置退出。

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
