# VeighNa 自主交易示例

本示例演示如何用 `vnpy.autonomous` 包把一个策略组合跑成「无人值守」的自主交易进程：

1. 父级守护进程按交易日历在交易时段内拉起交易子进程，时段结束自动退出，崩溃自动重启（有次数上限）。
2. 交易子进程内部装配 MainEngine、交易接口、策略应用，挂载事前风控守卫生成器、策略健康熔断和资金分配器。
3. 全过程通过 `.vntrader/autonomous_state.json` 状态文件暴露心跳，可通过邮件/微信通知推送关键事件。

## 快速开始

```bash
# 1. 准备配置（复制模板到 .vntrader 后填写账号与风控参数）
mkdir .vntrader
cp examples/autonomous/autonomous.json .vntrader/autonomous.json
cp examples/autonomous/holidays.example.json .vntrader/holidays.json

# 2. 校验配置并查看交易日历
python examples/autonomous/run.py check --config .vntrader/autonomous.json

# 3. 后续交易时段
python examples/autonomous/run.py sessions --count 5 --config .vntrader/autonomous.json

# 4. 启动守护进程（正式运行；先建议用纸面交易模式验证）
python examples/autonomous/run.py run --config .vntrader/autonomous.json
```

等价地也可以直接使用模块入口：`python -m vnpy.autonomous check`。

## 配置说明

### `mode`

`paper` 或 `live`。仅作标识，随通知一起发送，方便人工确认运行模式。

### `gateways`

以「接口名称 -> 设置字典」组织，字典原样传给 `vnpy_xxx` 网关（键名沿用各网关文档，如 CTP 的「用户名」「密码」）。内置名称：`CTP`、`CTP Mini`、`CTP证券`、`易盛`、`XTP`、`TORA`、`TTS`、`FEMAS`、`顶点HTS`、`顶点飞创`、`IB`；未覆盖的接口可直接写 `"module:Class"` 导入路径，例如 `"my_gateway.pack:MyGateway"`。

### `apps` 与 `app_settings`

`apps` 是需要加载的功能模块列表，支持内置名称（`CtaStrategy`、`PortfolioStrategy`、`RiskManager`、`PaperAccount`、`SpreadTrading`、`OptionMaster`、`AlgoTrading`、`ScriptTrader`、`ChartWizard`、`DataManager`、`DataRecorder`）或 `"module:Class"` 导入路径。含有 `init_engine`/`init_all_strategies`/`start_all_strategies` 方法的引擎会被纳入时段启停管理。

`app_settings` 按应用名配置该模块的参数。对暴露 `init_strategy(name, vt_symbol, setting)` 的引擎（vnpy_ctastrategy 的 CtaEngine 即是），时段开始时运行器会自动装配其中列出的策略，整个组合只需这一份配置：

```json
"app_settings": {
  "CtaStrategy": {
    "strategies": [
      {
        "class_name": "AutonomousTrendStrategy",
        "name": "my_trend_strategy",
        "vt_symbol": "rb2410.SHFE",
        "setting": {
          "class_name": "MyCtaTemplate",
          "symbol": "rb2410",
          "exchange": "SHFE",
          "fixed_size": 1
        }
      }
    ]
  }
}
```

装配在 `init_engine()` 之后、`init_all_strategies()` 之前执行（vnpy_ctastrategy 的 CtaEngine 走 `add_strategy(class_name, name, vt_symbol, setting)`，与界面添加策略同一入口）；条目缺 `class_name`/`vt_symbol` 会被跳过并记日志，不影响其余条目。引擎没有 `add_strategy` 方法时该项被忽略并给出警告。

`class_name` 是策略类名（需已加载进引擎的类表）；`name` 可省略，默认取 `类名_合约代码`。引擎里已存在的同名策略会被跳过，重复启动不会重复装配（vnpy_ctastrategy 的 CtaEngine 会把配置持久化到 `.vntrader/cta_strategy_setting.json`，重启后仍在）。

成交归属走引擎的 `orderid_strategy_map`（vnpy_ctastrategy 维护的 `vt_orderid -> 策略` 映射）；没有该映射时退回解析委托 `reference`（CTA 委托的 reference 形如 `CtaStrategy_策略名`，运行器会剥掉应用名前缀）。

### `calendar`

- `profile`：`cn_futures`（默认，日盘 09:00-11:30、13:30-15:00，夜盘 21:00-night_end）、`cn_futures_midnight`（夜盘至 02:30）、`full_day`（7x24，忽略周末）。
- `night_end`：默认 `23:00`，贵金属/原油等可改 `02:30`。
- `holidays_file`：节假日清单（JSON 字符串数组，如 `["2027-01-01"]`），相对于配置文件所在目录解析。**示例文件为空数组**，请按各交易所休市安排自行维护。
- `sessions`：留空使用模板；也可显式列出 `[{"name": "...", "start": "HH:MM", "end": "HH:MM"}]` 自定义时段（`end` 早于 `start` 表示跨零点）。

夜盘归属「开始当日」：如周五夜盘跨到周六凌晨，仍算周五的交易时段，周六其余时间休市。

### `risk`

事前风控，拦截不合规委托（返回空 `vt_orderid`）。`0` 表示不限制。

| 字段 | 含义 |
| --- | --- |
| `max_order_volume` | 单笔委托数量上限（同时受合约最小/最大申报单位约束） |
| `max_order_value` | 单笔名义价值上限（价格 x 数量 x 合约乘数） |
| `max_active_orders` | 全账户活动委托上限 |
| `max_position_volume` | 单合约净头寸默认上限，可用 `symbol_position_limits` 按合约覆盖 |
| `daily_loss_limit` | 当日权益亏损上限，以当日首笔账户权益为基准 |
| `max_drawdown` | 自时段内峰值权益的回撤上限 |
| `kill_switch_on_breach` | 触发后撤销全部活动委托并市价平掉所有持仓 |
| `allow_reduce_when_armed` | 熔断状态下是否仍允许平仓/减仓单 |

熔断触发后会发送通知，并需在 `.vntrader/autonomous_state.json` 确认后手动调用 `RiskGuard.disarm()`（或重启进程）才会恢复开仓。

### `health`

策略级健康熔断：连续亏损、当日亏损、单位时间错误次数达到阈值即暂停该策略（调用引擎的 `stop_strategy`），冷却结束后自动 `start_strategy` 恢复。`0` 表示不启用该规则。

### `portfolio`

`method` 为 `equal` 或 `inverse_volatility`，按账户权益给出各策略的目标资金（仅供参考，需要策略自行读取使用）；`max_weight` 为单策略权重上限，采用水填算法分配。

### `notification`

复用 `MainEngine.send_notification`，按 `vt_setting.json` 里的邮件/微信配置发送。`heartbeat_secs` 控制心跳通知间隔。

### `runtime`

- `poll_interval_secs`：子进程主循环间隔。
- `state_file`：状态文件路径（相对配置文件目录），父进程据此判断子进程存活。
- `contract_wait_secs`：等待合约推送的超时；`strategy_init_wait_secs`：策略初始化等待。
- `flat_on_session_end`：时段结束是否市价平仓（默认否，仅停止策略）。
- `restart_backoff_secs` / `max_restarts_per_session`：同一交易时段内的重启冷却与次数上限；达到上限后进入冷却（不低于 30 秒），冷却结束自动重置计数并重试，不会整日停止交易。
- `watchdog_stall_secs` / `auto_reconnect`：网关事件停滞阈值与自动重连开关。

## 状态文件

`.vntrader/autonomous_state.json` 原子更新，内容包括：进程角色、模式、当前时段、距下次开盘时间、风控快照、各策略健康度、资金分配、成交台账、各网关最近事件时间。外部值守脚本（如看门狗、大屏）可直接读取。

## 回测验收：先证明策略能赚钱再上实盘

自主交易最容易犯的错误，是拿一个从没被验证过盈亏的策略直接开机自启。`backtest` 子命令就是第一道关卡：

```bash
python -m vnpy.autonomous backtest --bars 300 --seed 7
```

合成序列只验证链路。真实决策请传入历史K线，支持两种数据源：

```bash
# 从 CSV 读取（datetime,open_price,high_price,low_price,close_price[,volume]）
python -m vnpy.autonomous backtest --csv path/to/rb2410_1min.csv --vt-symbol rb2410.SHFE

# 从已配置的 VeighNa 数据库读取
python -m vnpy.autonomous backtest --vt-symbol rb2410.SHFE --start 2024-01-01 --end 2024-06-30
```

CSV 允许表头用逗号、数据行用空格的混合格式（真实行情导出常见），`datetime` 需带日期与时间。

它驱动真实的 `CtaTemplate` 策略跑完一段 K 线，输出成交笔数、胜率、已实现/浮动盈亏、权益、最大回撤与夏普：

```
回测结果（合成行情，真实决策前请替换为真实K线）：
  bars             300
  trade_count      11
  winning_trades   1
  open_position    1.0
  closed_pnl       9416.69
  unrealized_pnl   567.48
  total_pnl        9984.18
  equity           1009984.18
  win_rate         0.09
  max_drawdown     144.78
  sharpe           0.85
```

`total_pnl <= 0` 时命令返回 1，可以直接挂到流水线里当门槛。

**关于成交模型**：它复刻 `vnpy_ctastrategy` 官方回测器的规则——第 N 根 K 线挂出的限价单，只有第 N+1 根 K线真的穿过这个价位才成交，成交价取该根的开盘价。没有这个一bar延迟，任何趋势策略在事后看起来都是赚钱的。滑点方向永远不利于交易者，手续费按每次成交计收。

**当前参考策略的实测结论**：`AutonomousTrendStrategy` 在合成行情上会频繁反手，11 笔成交只 1 笔盈利，靠的是持仓浮盈把总数拉正。它在盘整市里靠高换手赚不到钱，这说明必须用你自己的真实历史数据重跑 `backtest`，再决定是否启用——合成序列只能验证链路正确，不能证明经济含义。

## 开机自启（Windows 计划任务）

无人值守需要进程不依赖人工登录。[install_service.ps1](install_service.ps1) 把守护进程注册成 Windows 计划任务：

```powershell
# 以管理员身份打开 PowerShell，然后：

# 注册：登录与开机时自动启动，隐藏窗口，进程退出后自动重启
.\install_service.ps1 install

# 查看任务状态与最近一次运行结果
.\install_service.ps1 status

# 卸载
.\install_service.ps1 uninstall
```

任务动作等价于命令行启动，只是不再需要有人守在电脑前：

```
<python> -m vnpy.autonomous run --config "<仓库>/.vntrader/autonomous.json"
```

几个需要注意的点：

- **需要管理员权限**。任务以最高权限运行（`RunLevel Highest`），非管理员 PowerShell 注册会失败并给出明确提示，不会假报成功。
- **解释器不跟随安装时的 shell**。默认依次查找 `<仓库>/.venv\Scripts\python.exe`、`<仓库>/venv\Scripts\python.exe`，再用 PATH 上的 `python.exe`；也可以用 `-PythonPath` 指定。注册前会跑一次 `python -m vnpy.autonomous run --help` 预检，解释器跑不起来就不注册。
- **配置文件必须先就位**。`install` 会检查 `.vntrader/autonomous.json` 存在，不会带着空配置开机自启。
- **动作里带 `--config` 全路径**，所以计划任务的工作目录变化不影响它读到正确配置。
## 与 Freqtrade 类项目对照

本包对齐了成熟开源交易机器人的运维模式：Freqtrade 的「循环 + dry-run + 持久化」、Hummingbot 的「控制器-执行器分层」；风控守卫生成器类似交易所侧事前风控（vnpy_riskmanager 的脚本化替代），健康熔断类似策略级断路器。

## 附带参考策略

[strategies/autonomous_trend_strategy.py](strategies/autonomous_trend_strategy.py) 是专为自治循环写的双均线趋势策略：

- 通过 `variables` 持续上报 `realized_pnl` 与 `equity`，`CtaStatsAdapter` 据此驱动策略健康熔断与资金分配；没有上报的策略，这两类规则对其保持关闭。
- 内置 `max_position` 仓位自检与 ATR bar 止损，单策略不会超出风控守卫生成器的预期边界。
- 已覆盖「预热 → 金叉开仓 → 成交记账 → ATR 止损平仓 → 盈亏变量」全链路的真实引擎测试。

使用方式：

```bash
mkdir .vntrader/strategies
cp examples/autonomous/strategies/autonomous_trend_strategy.py .vntrader/strategies/
```

CtaEngine 初始化时扫描 `.vntrader/strategies/` 加载该类，然后在 `app_settings.CtaStrategy.strategies` 里把 `class_name` 写成 `AutonomousTrendStrategy`。`vnpy_ctastrategy` 自带的演示策略同样可被扫描加载和装配。
