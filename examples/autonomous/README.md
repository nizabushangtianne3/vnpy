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
- `holidays_file`：节假日清单（JSON 字符串数组，如 `["2027-01-01"]`），相对于配置文件所在目录解析。2026 年中国期货市场休市日已内置（元旦、春节、清明、劳动、端午、中秋、国庆，含夜盘停牌日），配置外部文件时会在内置清单基础上追加，用于补充后续年份。
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

熔断会在停用策略之前先按策略归属平掉它的未平仓头寸：只有该策略的合约且其持仓方向与 `pos` 一致时才会生成平仓单，走与账户级熔断相同的 `OffsetConverter` 拆分路径；账户级熔断仍会平掉全部头寸。策略已空仓时不会产生多余平仓单。

### `portfolio`

`method` 为 `equal` 或 `inverse_volatility`，按账户权益给出各策略的目标资金（仅供参考，需要策略自行读取使用）；`max_weight` 为单策略权重上限，采用水填算法分配。

### `notification`

复用 `MainEngine.send_notification`，按 `vt_setting.json` 里的邮件/微信配置发送。`heartbeat_secs` 控制心跳通知间隔。`webhook_url` 配置后额外 POST `{source, mode, message}` JSON（超时 5 秒，失败只记日志），父级守护进程在主引擎创建前也能推送重启与看门狗告警，适合接企业微信/钉钉/Telegram bot。

### `runtime`

- `poll_interval_secs`：子进程主循环间隔。
- `state_file`：状态文件路径（相对配置文件目录），父进程据此判断子进程存活。
- `contract_wait_secs`：等待合约推送的超时；`strategy_init_wait_secs`：策略初始化等待。
- `flat_on_session_end`：时段结束是否市价平仓（默认否，仅停止策略）。
- `restart_backoff_secs` / `max_restarts_per_session`：同一交易时段内的重启冷却与次数上限；达到上限后进入冷却（不低于 30 秒），冷却结束自动重置计数并重试，不会整日停止交易。
- `watchdog_stall_secs` / `auto_reconnect`：网关事件停滞阈值与自动重连开关。

## 状态文件

`.vntrader/autonomous_state.json` 原子更新，内容包括：进程角色、模式、当前时段、距下次开盘时间、风控快照、各策略健康度、资金分配、成交台账、各网关最近事件时间。外部值守脚本（如看门狗、大屏）可直接读取。

其中 `allocation` 是运行中实时算出的各策略目标资金（账户权益乘权重、留出现金后的数值），策略可据此决定开仓手数；账户未推送余额或没有任何策略时该字段为空，不会用错误基数算出虚假目标。

不写脚本也能看：`python -m vnpy.autonomous status` 输出一行摘要（pid、模式、状态、时段、运行时长、心跳年龄、重启次数、策略数、风控是否已武装、健康是否熔断），加 `--follow` 每秒刷新。

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

单次回测只能回答「这一组参数行不行」。要看参数稳定性，用 `optimize` 在网格上扫一遍：

```bash
python -m vnpy.autonomous optimize --grid "fast_window=5,10,20;slow_window=20,40" --csv path/to/rb2410_1min.csv
```

输出按夏普排序的参数表，包含每组的 `total_pnl`、`profit_factor`、`max_drawdown`、`trade_count`、`win_rate`。所有组合 `total_pnl <= 0` 时返回 1，可直接当流水线门槛。需要把结果交给外部程序或 CI 工件时，加 `--output path/to/result.json`，普通网格与 walk-forward 两种模式都会落盘：

选参规则：盈利的组合优先，再在其中按夏普取最高，夏普打平时用盈利因子取更可重复的那一组；只有全部组合都亏损时才退化为单纯按夏普排序，并照样在末尾给出结论。纯按夏普选参会把「交易很少、曲线平滑但整体亏损」的组合送到样本外检验，等于用一个亏损参数去验证过拟合。

```bash
python -m vnpy.autonomous optimize --grid "fast_window=5,10,20;slow_window=20,40" \
    --csv path/to/rb2410_1min.csv --output path/to/result.json
```

样本内选参容易过拟合。加 `--train-bars` 与 `--test-bars` 触发 walk-forward 验证：每个折叠用训练段选最优参数，再在紧随其后的样本外段检验，最后给出「样本外表现是否与样本内一致」的结论：

```bash
python -m vnpy.autonomous optimize --grid "fast_window=5,10;slow_window=20,40" \
    --csv path/to/rb2410_1min.csv --train-bars 2000 --test-bars 500
```

样本外 `total_pnl <= 0` 或样本外夏普不到样本内一半时返回 1，判定为过拟合。

`--slippage` / `--rate` / `--size` / `--capital` 分别控制成交成本与资金基数，其中 `--size` 支持非整数合约乘数（如 300 股 / 手 或指数点位折算的品种）。这些参数同样适用于 `backtest` 子命令。

多个品种跑同一套策略、把盈亏合成一条组合曲线，用 `portfolio` 子命令：

```bash
python -m vnpy.autonomous portfolio \
    --markets "m=path/to/m_daily.csv,rb=path/to/rbm_daily.csv" \
    --grid "fast_window=5,10;slow_window=40,60;stop_atr=2.0,3.0;trail_stop=0.0,2.0,4.0" \
    --size 10 --slippage 1 --train-bars 1400 --test-bars 700 \
    --output portfolio_result.json
```

`--markets` 用 `名称=CSV路径` 声明品种，组合层面的 walk-forward 每折在训练段选一组参数，再用同一组参数跑所有品种的样本外段，输出的 JSON 含每折各品种的贡献（`leg_pnls`）——看合计会把「一个品种扛全部」误读成组合优势，所以每折的拆分必须看。

组合曲线的日期按各品种的**并集**对齐，不是按下标配对：两个品种交易日历不同，某个品种没有 bar 的日期对它就是持平日，不是序列结束。各品种独立跑自己的持仓与成本，组合只是账面相加。

真实数据实测（豆粕 4843 根 + 螺纹 4295 根，训练 1400 / 样本外 700、24 组参数）：组合样本外合计 +285972.71、3/4 折盈利、平均夏普 0.536；同期单品种为豆粕 +320510.95（4/4 折）、螺纹 -53263.91（2/4 折）。拆开每折看，螺纹四折合计 -15915.86 几乎全线亏损——**分散化在当前参考策略上只降低了回撤，没有提高盈亏**，组合的正期望来自豆粕单腿。换品种、换策略时请先单独跑过 `backtest` / `optimize`，再用组合验证分散，不要把组合当成让亏损品种起死回生的手段。

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
  profit_factor    9.42
```

`total_pnl <= 0` 时命令返回 1，可以直接挂到流水线里当门槛。

**关于成交模型**：它复刻 `vnpy_ctastrategy` 官方回测器的规则——第 N 根 K 线挂出的限价单，只有第 N+1 根 K线真的穿过这个价位才成交；成交价取限价与该根开盘价中更有利的一侧，跳空不会超出限价成交。滑点不计入成交价，而是按官方口径乘以合约乘数后作为成本扣除；手续费按成交金额乘以合约乘数计收。

**当前参考策略的实测结论**：修正金字塔加仓后，`AutonomousTrendStrategy` 在默认合成序列（300 根，seed=7，滑点 0.5，费率 0.0001，乘数 10）上为 22 笔成交、1 笔盈利、总盈亏约 +13743.02，仓位达到 `max_position=5` 上限。这仍只是链路验证：walk-forward 默认合成序列给出的样本外合计 pnl 约 +11.91、平均夏普 0.013，显著劣于样本内并触发淘汰门槛。必须用你自己的真实历史数据重跑 `backtest` 与 `optimize`，再决定是否启用——合成序列不能证明经济含义。

**真实行情上的结论（重要，与合成序列相反）**：用东方财富拉到的螺纹主力连续日线（`113.rbm`，2009-03-30 至 2026-11-12，共 4295 根，乘数 10、费率 0.0001、滑点 0.5）跑默认参数（fast=5/slow=40/atr=10/stop=2x）：

| 指标 | 合成序列 | 真实 17 年日线 |
| --- | --- | --- |
| `total_pnl` | +13743.02 | +216930.84 |
| `profit_factor` | 9.22 | **0.895（亏损大于盈利）** |
| `win_rate` | 0.045 | 0.095 |
| `max_drawdown` | 639.08 | 67188.48 |
| `sharpe` | 386.93 | 0.446 |

分年度盈亏为：2009~2019 合计 +140042、2020~2026 合计 +42586 但其中 7 年有 6 年亏损；最好的 5 个回合贡献约 129% 的总盈亏，去掉这 5 笔整体就是亏损。walk-forward（训练 1500 / 测试 500，参数 5,10 x 40,60）5 个折叠里 3 个样本外亏损，样本外平均夏普 0.098 显著低于样本内 0.395，退出码 1。

结论如实说明：**该参考策略在真实螺纹数据上不可作为可盈利配置启用**。它确实在 2009-2016 那几段趋势里赚到过钱，但盈亏结构是「少数大单 + 多数小亏」，换个时间窗口就不成立。合成序列上那些漂亮的数字来自带正漂移的随机游走，不能证明经济含义——请以你自己的真实数据重跑 `backtest` / `optimize` 得到的结论为准，仓库里的默认参数不应被直接拿去实盘。

**数据脏标度的坑**：东方财富期货 5 分钟线在同一份文件里混了两套价格标度（2026-11-11 15:00 之前约 11617xxx、之后约 30xx），直接喂进回测会算出 5.7 亿这种假盈利。`load_bars_from_csv` 现在会以首根价格为锚，把偏离超过 50 倍的行丢弃并告警，日线数据不受影响（全样本最大 bar 间跳变仅 1.097 倍）。

**关于胜率**：一个回合算盈利，要求扣除该回合全部成交的手续费与滑点后净盈亏为正。只看价格涨跌会把「浮盈变净亏」的回合也记成盈利，让高成本、只亏钱的策略显示高胜率。

**关于夏普**：按 K 线时间间隔年化（`annual_sharpe`），因为不年化时同一策略在 1 分钟与 1 日数据上的得分不可比，`optimize` 会变成在比数据频率而不是比策略。做参数横向比较时，请固定用同一份数据的同一周期。

**关于盈利因子**：`profit_factor` = 已平仓回合的总盈利 / 总亏损，与 `win_rate` 用同一套开平仓配对，所以两个数字不会互相矛盾。它回答的是 `total_pnl` 回答不了的问题：同样赚 2000，一个靠一次好运、一个靠几十笔小胜，在只按盈亏排序时完全等价，但前者换个窗口就不成立。`optimize` 的选参在「盈利优先、夏普次之」之后用盈利因子打破平手。没有亏损回合时为 0（不是无穷大）——只赚过一笔的参数组不应排在真正的优势前面。未平仓的持仓不计入这个比值。

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
- ATR 止损按「bar 内触价」判定，不是看收盘价：多头在 `avg_cost - stop_atr x ATR`，当根 K 线的最低价触到即离场，空头对称看最高价。一根 bar 里下穿止损又收回，收盘价判据会漏掉它，回测因此少记一次真实回撤。成交价取触发价与当根开盘价中更不利的一侧（买入 `max`、卖出 `min`），与 `vnpy_ctastrategy` 官方回测器 `cross_stop_order` 的挂单语义一致，跳空穿过止损不会以优于开盘价的价格成交。
- 已覆盖「预热 → 金叉开仓 → 成交记账 → ATR 止损平仓 → 盈亏变量」全链路的真实引擎测试。

策略参数（均可通过 `app_settings.CtaStrategy.strategies[].setting` 覆盖）：

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `fast_window` / `slow_window` | 5 / 20 | 双均线快慢周期 |
| `atr_window` | 10 | ATR 计算窗口，同时用于止损距离 |
| `stop_atr` | 2.0 | 静态止损距离（ATR 倍数），锚在持仓成本 |
| `trail_stop` | 0.0 | 跟踪止损距离（ATR 倍数），0 关闭 |
| `fixed_size` | 1 | 每次开仓手数 |
| `max_position` | 5 | 净持仓上限 |

**跟踪止损 `trail_stop`**：静态止损锚在持仓成本上，趋势跑得越久，回吐越多才离场。`trail_stop > 0` 时额外跟踪持仓期间见到的最高/最低价（多头取 high、空头取 low，只向有利方向收紧），止损位取静态与跟踪两者中更靠近现价的一侧；持仓归零后极值复位，下一个仓位重新取锚。默认 0 表示保持原有行为。

在真实螺纹/豆粕日线上的实测（`backtest`，乘数 10、费率 0.0001、滑点 1）：

| 品种 | 参数 | 无 trail：pnl / pf / dd / sharpe | trail=2：pnl / pf / dd / sharpe |
| --- | --- | --- | --- |
| 豆粕 m | f=10 s=60 st=3.0 | 710535 / 1.379 / 186946 / 0.611 | 149073 / **1.759** / **45788** / 0.501 |
| 螺纹 rbm | f=5 s=60 st=1.0 | 249757 / 1.072 / 41847 / 0.499 | 165424 / **1.040** / **22854** / **0.709** |
| 豆粕 m | f=5 s=40 st=2.0 | 642646 / 1.142 / 93300 / 0.588 | 245718 / **1.414** / **57762** / 0.577 |

规律：总盈亏普遍下降（趋势被提前切掉），但**回撤、盈利因子、夏普三项同时改善**。walk-forward（豆粕 4843 根日线，训练 40% / 测试 10%、每档 968 根，共 6 折）验证了这一点，见 CHANGELOG「真实行情验证结论」。

使用方式：

```bash
mkdir .vntrader/strategies
cp examples/autonomous/strategies/autonomous_trend_strategy.py .vntrader/strategies/
```

CtaEngine 初始化时扫描 `.vntrader/strategies/` 加载该类，然后在 `app_settings.CtaStrategy.strategies` 里把 `class_name` 写成 `AutonomousTrendStrategy`。`vnpy_ctastrategy` 自带的演示策略同样可被扫描加载和装配。
真实数据实测（豆粕 4843 根 + 螺纹 4295 根，训练 1400 / 样本外 700、24 组参数）：组合样本外合计 +285972.71、3/4 折盈利、平均夏普 0.536；同期单品种为豆粕 +320510.95（4/4 折）、螺纹 -53263.91（2/4 折）。拆开每折看，螺纹四折合计 -15915.86 几乎全线亏损——**分散化在当前参考策略上只降低了回撤，没有提高盈亏**，组合的正期望来自豆粕单腿。换品种、换策略时请先单独跑过 `backtest` / `optimize`，再用组合验证分散，不要把组合当成让亏损品种起死回生的手段。

跑完 `optimize` / `portfolio` 的 walk-forward 后，用 `promote` 做晋升判定，把「要不要进仿真池」这一步也交给规则而不是人工读表：

```bash
python -m vnpy.autonomous promote --run "m9999.DCE=m_wf.json" --run "rb9999.SHFE=rb_wf.json"
```

只输出三种判定：`promote`（样本外盈亏为正、样本外夏普不低于样本内一半、盈利折叠过半、样本外折叠数不少于 `min_folds`）、`watch`（通过门槛但组合只有一个品种盈利，其余品种未提供分散化贡献）、`retire`（任一门槛未过，逐条列出失败原因）。门槛在配置的 `promotion` 段调整（`min_out_pnl` / `min_out_sharpe_ratio` / `min_profitable_fold_ratio` / `min_folds`）。

`min_folds` 默认 4，实测依据：沪锡在训练 1400 / 样本外 700 下只切出 2 折，一折 +470 万、一折 -205 万，盈利折叠比 50% 恰好踩线被放过去；同一份数据换成训练 900 / 样本外 450 切出 4 折后，2 折分别亏损约 69 万与 129 万，样本外夏普 0.774 掉到 0.482，改判 retire。**只跑一种窗口切分不足以下结论**，折叠不够就不评。

`promote` 的候选用命令输出的「建议参数」填 `app_settings.CtaStrategy.strategies[].setting`——那是每折真正被验证过的那一组，不是整个网格；`retire` 的候选要从配置里摘掉，不能让带着过期参数的东西继续跑。

13 个真实品种用同一套网格与同一套 walk-forward 口径筛过之后，只有豆粕在两种窗口切分下都通过全部门槛（训练 1400 / 样本外 700：样本外 +320510.95、4/4 折盈利、夏普 0.685；训练 900 / 样本外 450：样本外 +269143.40、8/8 折盈利、夏普 0.675）。其余品种要么样本外盈亏为负，要么样本外夏普留不到样本内的一半。详细数据见 CHANGELOG「品种扩展与样本外复核」。

实盘前核对行情，用 `quotes` 把配置里装配的标的到交易所盛商那边拉一次快照：

```bash
python -m vnpy.autonomous quotes                                    # 读配置的 strategies
python -m vnpy.autonomous quotes --symbol 600519 --symbol 601318    # 指定代码
python -m vnpy.autonomous quotes --symbol 600519 --json             # 机器可读
```

不带 `--symbol` 时读 `app_settings.CtaStrategy.strategies` 的 `vt_symbol`，正是 `promote --apply` 写回的那批条目，因此能直接用来做上盘前核对。这个命令只拉行情，不下单，也不把快照当成成交回报。
