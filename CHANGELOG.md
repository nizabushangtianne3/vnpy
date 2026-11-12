# 4.5.0版本

## 新增

1. 新增自主交易运行器 vnpy.autonomous：交易日历、父级守护与交易子进程编排、事前风控守卫生成器（含单笔数量与名义价值上限、当日亏损与回撤熔断平仓）、策略健康熔断、等权与逆波动率资金分配、状态文件与通知，配套 examples/autonomous 与离线单元测试
2. vnpy.autonomous 支持在配置中声明 app_settings.strategies，由运行器在 init_engine 后通过引擎的 add_strategy 接口自动装配策略（与 vnpy_ctastrategy 界面添加入口一致），单个配置文件管理整个组合；应用模块加载失败、引擎初始化失败不再中断交易子进程；成交归属优先走引擎 orderid_strategy_map，CI 增加 vnpy_ctastrategy 真实链路集成测试
3. vnpy.autonomous 每个交易日开始重置日内风控基准（当日亏损/回撤上限），跨零点夜盘不误触发；父级守护进程重启达到上限后进入冷却并自动恢复重试，不再整日罢工
4. 新增自治参考策略 examples/autonomous/strategies/autonomous_trend_strategy.py：双均线 + ATR 止损 + 仓位自检，通过 variables 上报 realized_pnl/equity 供健康熔断与资金分配消费，附真实引擎全链路测试
5. 新增父级守护进程真进程集成测试（spawn 真实子进程走完整链路）与重启冷却恢复测试
6. 新增原子状态文件写入（带重试）与 Windows 并发读安全，父进程 terminate 后等待子进程真正退出，交易子进程退出时关闭数据库句柄，避免测试与计划任务场景下残留句柄导致文件占用
7. vnpy_ctabacktester的回测配置按策略名分组保存，切换策略时自动恢复；没有历史配置时回到界面初始值
8. 新增 examples/autonomous/install_service.ps1：把自主交易守护进程注册为 Windows 计划任务，登录与开机自动启动、隐藏窗口、进程退出后按一分钟间隔自动重启、无执行时限；注册前校验配置文件存在并做解释器预检，非管理员权限下明确报错而不是假报成功，提供 install / uninstall / status 三个子命令，附只读回归测试
9. 新增 vnpy.autonomous.backtest 离线策略验收模块与 `python -m vnpy.autonomous backtest` 子命令：驱动真实 CtaTemplate 策略跑完一段 K 线并输出成交笔数、胜率、已实现与浮动盈亏、权益、最大回撤、夏普；total_pnl <= 0 时命令返回 1 可直接作为流水线门槛。成交模型复刻 vnpy_ctastrategy 官方回测器：第 N 根 K 线挂出的限价单仅当第 N+1 根 K 线穿过该价位才成交，成交价取该根开盘价，滑点方向始终不利于交易者，手续费按次计收。vnpy_ctastrategy 改为惰性导入，缺失时整个 autonomous 包仍可用
    a. 新增 CSV 与 VeighNa 数据库两条真实数据源路径：`--csv` 支持 datetime,open_price,high_price,low_price,close_price[,volume]，允许表头逗号、数据行空格的混合分隔；`--start/--end` 走 get_database()，与实盘加载同一批数据
    b. 空格分隔数据行的时间戳含空格，普通 DictReader 会把时间与价格折叠成一个字段，专用解析器按行拆分并保持 datetime 完整
10. 新增 `python -m vnpy.autonomous optimize` 参数扫描子命令：`--grid` 展开 `fast_window=5,10;slow_window=20,40` 形式的笛卡尔积，驱动同一策略在每个参数组合上重复回测，按夏普排序输出 total_pnl / max_drawdown / trade_count / win_rate，全部组合 total_pnl <= 0 时返回 1 可直接作为流水线门槛
11. optimize 增加 walk-forward 样本外验证：`--train-bars/--test-bars` 同时设置后按滑动窗口逐折叠「训练段选参 → 紧随其后的样本外段检验」，输出各折叠样本内外夏普与盈亏；样本外合计 pnl <= 0 或样本外平均夏普不足样本内一半时返回 1，对齐晋升手册「样本外不得显著劣于样本内」的淘汰规则
12. 新增 `python -m vnpy.autonomous status` 运行状态子命令：读取状态文件输出 pid / 模式 / 状态 / 时段 / 运行时长 / 心跳年龄 / 重启次数 / 策略数 / 风控与熔断标记的摘要行，`--follow` 每秒刷新，状态文件缺失或不可读时返回 2 并给出启动提示
13. 通知通道新增 webhook：`notification.webhook_url` 配置后，邮件/微信成功或两者都未配置时统一 POST `{source, mode, message}` JSON，超时 5 秒且失败只记日志不影响交易；父级守护进程在主引擎创建前即可推送重启与看门狗告警，附 5 个 stub 测试
14. 修复 notify_on_session_end 配置存在但从未发送的问题：交易时段结束并停止策略后发送当日成交笔数、风控是否武装、当前状态的摘要，`notify_on_session_end=false` 时不发送，附 3 个 stub 测试
15. 风控快照新增 breached 字段：区分「当前已武装（正在拦截开仓）」与「本时段曾触发熔断（disarm 后拦截解除但历史仍在）」，`reset_daily` 归零，运维看板与 webhook 消费者据此判断是否需要人工复盘
16. 资金分配上限收紧：`策略数 x max_weight < 1` 时改为按上限封顶并将差额保留为现金，不再钳制后归一化放大回超限权重；删除 `_apply_bounds` 中钳制分支后的不可达归一化代码，节假日文件缺失时改为输出警告日志便于运维发现，附边界测试
17. 修复参考策略 realized_pnl / equity 未乘合约乘数的问题：这两个变量是货币金额，未乘数会让健康熔断与资金分配按放大了 size 倍的错误盈亏做决策，实盘下等子系统被误导
18. `status` 子命令输出新增 `guard_breached`：与 `guard_armed` 配合，运维无需打开状态文件即可区分「当前正在拦截」与「本时段曾触发熔断」
19. 修复策略快照把累计 realized_pnl 当作单笔盈亏记入健康熔断的问题：当快照成交数增加时会按轮询频率重复记入累计值，导致日内亏损按倍数虚增并过早触发熔断；现在统一由 pnl 增量通道记录成交，附回归测试
20. 修复风控熔断平仓在 SHFE/INE 上的两个拒单风险：平仓单从市价改为以盘口对手价（无行情时退回持仓价）的限价单，绕开 SHFE/INE 不支持市价单的限制；平仓委托改走 MainEngine 入口，由 OffsetConverter 自动拆分为平今/平昨，避免当日仓用普通平仓被拒单导致熔断平仓失败
21. 修正风控熔断平仓的 OffsetConverter 接入路径：convert_order_request 位于 OmsEngine 而非 MainEngine，平仓流程现在正确调用 OmsEngine.convert_order_request 完成 SHFE/INE 平今/平昨拆分，并在拆分后对每笔请求执行风控校验，确保 allow_reduce_when_armed=False 时平仓单不会被熔断自身拦截
22. 修复 status 子命令 health_halted 永远显示 None 的问题：状态文件里的 health 是 {策略名: {halted: bool}} 结构，原代码用 health.get('halted') 只能取到 None；现在改为聚合任一策略的 halted 状态，测试 payload 同步改为真实结构
23. 修复 optimize walk-forward 在 K 线不足以组成单个折叠时除零崩溃的问题：现在提前校验 `len(bars) >= train_bars + test_bars`，不足时返回明确的错误提示与退出码 2，不再抛出 ZeroDivisionError
24. 修复通知通道把「有 SMTP 服务器地址」误判为「邮件可发送」的问题：默认 SETTINGS 的 `email.server` 非空但 `email.username` / `email.receiver` 为空，每次通知都会排队一次注定失败的 SMTP 连接；现在要求 server、username、receiver 三项齐备才走邮件，否则回退微信 / webhook，附回归测试
25. 自主交易日历内置 2026 年中国期货市场休市日（元旦、春节、清明、劳动、端午、中秋、国庆，含夜盘停牌日），未配置节假日文件不再按空清单运行导致长假误开盘；外部 `holidays_file` 与内置清单合并追加，不覆盖当年数据，附日历与 runner 回归测试
26. 修复交易子进程崩溃重启后日内风控状态丢失的问题：熔断状态、当日起始权益、权益峰值与当前权益现在从当日状态文件恢复（跨日数据不恢复，避免用昨日基准污染今日风控）；恢复后的熔断保持武装，不会再因重启重新开仓，附回归测试
27. 修复策略健康熔断状态与成交计数在子进程重启后丢失的问题：`HealthMonitor` 快照新增 `halted_at` 持久化字段，重启后从当日状态文件恢复熔断标记、冷却起点与当日盈亏计数（跨日数据不恢复）；恢复发生在策略启动前，上一进程已熔断的策略不会因 `start_all_strategies` 重新开仓，附健康与 runner 回归测试
28. 修复交易子进程重启后已熔断策略仍会被批量重新启动的问题：策略启动改为逐策略执行，启动前读取恢复后的健康快照，`halted=True` 的策略保持引擎停用并输出警告；无逐策略接口的引擎保留批量启动兜底，附真实 CTA 引擎回归测试
29. 修正离线回测器与 `vnpy_ctastrategy` 官方回测器在成本与撮合上的四处偏差，避免把不可信的回测曲线当成盈利证据：成交价改为取限价与当根开盘价中更有利的一侧（买入 `min(limit, open)`、卖出 `max(limit, open)`），跳空不再以远优于限价的价格成交；滑点不再混入成交价，而是与手续费一起按合约乘数计为成本（`commission = turnover * size * rate`、`slippage = volume * size * slippage`），对齐官方 DailyResult 口径；胜负回合配对改为按开仓方向匹配，修掉把多头平仓配到空头开仓上导致胜率完全颠倒的问题；委托价入队前按 `pricetick` 取整；`BacktestSettings.size` 改为 float 以支持非整数乘数；附 4 个回归测试，并把 `examples/autonomous/README.md` 的实测结论更新为 7 笔成交、1 笔盈利、总盈亏约 +2763.87，同时写明默认合成序列的 walk-forward 样本外 pnl 约 +11.91、平均夏普 0.013 已触发淘汰门槛
30. `backtest` / `optimize` CLI 的 `--size` 参数支持非整数合约乘数（如 300 股一手或点位折算品种）；`optimize` 新增 `--output` 参数，把普通网格排名与 walk-forward 折叠明细（含样本内外 pnl、夏普与最优参数）写入 JSON 文件，供外部程序、CI 工件与值守脚本直接消费；附 2 个 CLI 导出回归测试与 README 用法说明
31. 修复策略健康熔断只停策略、不处理持仓的风险敞口问题：熔断现在先按策略归属平掉其未平仓头寸（经 `symbol_strategy_map` 匹配合约、按方向核对 `pos`，平仓走 OffsetConverter 的 SHFE/INE 拆分且不被熔断自身拦截），再停用该策略；没有持仓时不再产生多余平仓单；`RiskGuard.flatten_positions` 增加 `strategy_name` 过滤参数，账户整体熔断沿用全平行为；附 2 个回归测试
32. 修正离线回测夏普口径：`sharpe` 改为按 K 线实际时间间隔年化（`annual_sharpe`），此前纯按序列位置计算，同一策略在 1 分钟与 1 日数据上得分不可比，`optimize` 的排序实际在比数据频率而非策略；无时间戳时退化为每样本比值并明确不声明频率；附 2 个回归测试（同收益序列在分钟/日线同比、平曲线为 0）与 README 说明
33. 修复资金分配器在运行时从未被调用的问题：`CapitalAllocator.allocate()` 此前只在测试里执行，运行器只记录权益样本、从不产出目标资金，`portfolio` 配置形同虚设。现在交易子进程按轮询周期调用 `allocate()`，账户权益取自主引擎账户余额累加，策略列表取策略引擎当前持有的策略名，结果实时写入状态文件 `allocation` 字段供策略与值守消费；账户权益未知或无策略时保持为空，不用错误基数算出虚假目标；附 2 个回归测试（正常分配落到状态文件、缺权益/无策略时不崩也不虚报）与 README / portfolio 文档说明
34. 修正回测胜率口径：`win_rate` 此前按每回合的名义价格变动（价格差 x 手数）判定，忽略回合内每笔成交的手续费与滑点，导致「价格上赚钱但净亏损」的回合也被记为盈利，高成本只亏钱的策略会显示高胜率。现在配对开平仓后扣除该回合全部成交成本，`净盈亏 > 0` 才算盈利；`_apply_position_and_cost` 返回每笔成交成本，引擎 stub 以 tradeid 记录并在配对时累加；附回归测试（成本吃掉全部浮盈时判负、低成本判正、无成本表时维持名义口径）
35. 修正 `optimize` 选参规则：此前训练段与普通网格都纯按夏普排序取最优，会把「交易次数很少、权益曲线平滑但 total_pnl 为负」的组合送进样本外检验，等于用一个亏损参数去验证过拟合。现在盈利组合优先，再在其中按夏普取最高；只有全部组合都亏损时才退化为纯夏普排序并照常在结尾给出结论。附 3 个回归测试（跳过亏损高夏普组合、全亏时退化取最高夏普、空结果报错）
36. 修复参考策略无法加仓的结构缺陷：`AutonomousTrendStrategy` 的开仓条件此前要求 `pos <= 0`，而金叉信号只在突破那一根 bar 成立，导致每个趋势只开 `fixed_size`（默认 1）手就再也不动，`max_position=5` 是死参数，趋势策略永远只赚一份合约的利润。现在金叉/死叉负责反向建立头寸，趋势持续期间（快线在上方且未达上限）按 `fixed_size` 逐根金字塔加仓，反向信号或 ATR 止损离场；默认合成序列实测成交数 7 -> 22、最终仓位 1 -> 5、总盈亏 2763.87 -> 13743.02，附 1 个回归测试（持续上涨趋势必须达到 max_position 且多笔成交）
37. 修正参考策略 ATR 止损的判定口径，对齐 `vnpy_ctastrategy` 官方回测器 `cross_stop_order` 的挂单语义：此前多头只在 `bar.close_price <= avg_cost - stop_atr x ATR` 时平仓、空头对称看收盘价，一根 bar 内下穿止损又收回（low 触价、close 收回）会被判为不止损，回测系统性少止损、隐藏真实回撤。现在触发改看 bar 内极值（多头 `bar.low_price <= 止损价`、空头 `bar.high_price >= 止损价`），成交价取触发价与当根开盘价中更不利的一侧（买入 `max`、卖出 `min`），跳空穿过止损不会以优于开盘价的价格成交。`on_bar` 先把 bar 折入 ArrayManager 再给 ATR 定价，因此止损位以当根 bar 自身的 ATR 为准；附 2 个回归测试（bar 内触价且收盘收回必须挂出平仓单、低点未触价保持安静），默认合成序列实测数字不变（22 笔成交、1 笔盈利、总盈亏 13743.02、最大回撤 639.08），说明此前合成序列极少出现 bar 内触价后收回，修正只影响真实数据上的回撤统计
38. 离线回测新增盈利因子 `profit_factor`（已平仓回合总盈利 / 总亏损）：`total_pnl` 相同的两组参数，一组靠一次好运、一组靠几十笔小胜，只按盈亏排序时完全等价，但前者换个窗口就不成立，此前 `optimize` 无法区分。配对逻辑抽成 `_round_trip_pnls`，胜率、盈利因子共用同一份开平仓配对，两个数字不会互相矛盾；没有亏损回合时为 0 而不是无穷大，只赚过一笔的参数组不会排在真正的优势前面；未平仓持仓不计入该比值。`optimize` 选参规则改为「盈利优先 → 夏普次之 → 盈利因子打破平手」，参数表与 `BacktestResult.summary()` 同步输出该字段；附 6 个回归测试（中彩票式与重复小胜式分列、无亏损回合为 0、无成交为 0、汇总字段存在、选参打破平手、缺该字段的行仍可排序）

## walk-forward 样本内指标取错参数（本轮修复）

`optimize --train-bars/--test-bars` 每折先在训练段跑完整参数网格、再用 `_pick_best_params` 选出最优组合送进样本外段，但折叠记录里写的是 `train_rows[0]`，也就是网格按展开顺序的第一组，与被选中的组合毫无关系。表格因此把「A 参数的样本内成绩」和「B 参数的样本外成绩」并排展示，判断样本内外衰减时看的是两个策略。修复为按 `best_params` 反查对应的训练行（逻辑抽成 `_selected_train_row` 供直接测试），未找到时才退回首行。附 2 个回归测试（正确取回选中参数那一行、参数不在网格内时退回首行）。

该 bug 只影响报告数字，不影响选参决策：豆粕 4843 根日线（训练 1400 / 样本外 700，共 4 折）修复前后样本外合计均为 +320510.95、4/4 折盈利；螺纹 4295 根修复前后样本外均为 -53263.91、2/4 折盈利。修复后样本内数字随之变实（豆粕样本内合计 1031626.21 -> 1363587.90、平均夏普 0.698 -> 1.191），不再虚低。

## 豆粕真实日线 81 组参数 walk-forward（2026-11-12）

把网格扩大到 fast 5/10/20 x slow 40/60/120 x stop_atr 1/2/3 x trail_stop 0/2/4 共 81 组，训练 1000 根 / 样本外 500 根步进，4843 根豆粕日线切出 7 折完整检验：

1. 样本外合计 +233969.32、4/7 折盈利、平均夏普 0.447；样本内合计 1106825.69、平均夏普 1.311。样本外仍显著劣于样本内，按现门槛判定「存在过拟合风险，不建议启用」
2. 按选中参数分组统计：`trail_stop=0` 只有 1 折且该折亏损；`trail_stop=2` 选中 5 折、其中 3 折盈利；`trail_stop=4` 选中 1 折且盈利。即 7 折里有 6 折选中了跟踪止损，唯一未选跟踪止损的那折亏损，与「trail 改善盈亏结构」的单点实测方向一致
3. 这轮数量下豆粕样本外为正、螺纹为负（螺纹同口径 4 折样本外 -53263.91），说明当前参考策略的样本外边缘只在部分品种上存在，品种筛选比参数微调更能决定成败

## 实测证伪记录

以下改动经过合成序列 A/B 对照后被放弃，记录在此避免重复尝试：

1. ADX 趋势强度过滤（`trend_window` / `trend_threshold`，只在 ADX 达标时开新仓）：在 6 个不同 seed 与漂移的合成序列上，最大回撤几乎全部下降、胜率几乎全部上升，但 4 个阈值档位 x 6 个场景里没有一个能在盈亏上超过不过滤，seed=7 上总盈亏从 2058.11 掉到 82.08。趋势策略的盈利来自持续参与，过滤掉一部分趋势就掉盈利，默认阈值不存在，故未引入
2. ATR 仓化 risk-per-trade（`risk_per_trade`，按止损距离反算手数）：在当前默认资金规模（1,000,000）与合约乘数 10 下，算出的手数稳定为 47 手左右，被 `max_position=5` 全部截断，三个 risk 档位（0.5%/1%/2%）跑出的成交数完全相同，参数完全空转；要生效需要把账户规模压到约百分之一。未在合理的默认参数下产生任何作用，故未引入
3. 上述两者共同踩到一个真实陷阱：`ArrayManager` 只保留最后 `size` 根 bar，而 `talib.ADX` 需要两倍窗口的历史，`size=22` 配 `adx(14)` 会全程返回 NaN；`NaN >= 阈值` 恒为 False，策略会无声地永远无法开仓。后续任何引入 ADX 的改动必须先让 warmup 覆盖 `2 * trend_window`，并对 NaN 显式兜底，不得让它静默冻结交易

## 真实行情验证结论（2026-11-12）

首次用真实行情数据跑通完整链路并得到可复现结论，数据源为东方财富螺纹主力连续日线 `113.rbm`（2009-03-30 至 2026-11-12，4295 根；乘数 10、费率 0.0001、滑点 0.5）：

1. 默认参数（fast=5/slow=40/atr_window=10/stop_atr=2/fixed_size=1/max_position=5）：22 笔级别的合成序列上 `total_pnl` +13743、`profit_factor` 9.22，而真实 17 年日线上 `total_pnl` +216930.84、`profit_factor` 0.895、`win_rate` 0.095、`max_drawdown` 67188.48、`sharpe` 0.446。**盈利因子小于 1 意味着亏损总额大于盈利总额，能赚钱完全依赖少数几笔大单**
2. 分年度盈亏：2009~2019 合计 +140042.31，2020~2026 合计 +42586.03（这 7 年里 6 年亏损）；最好的 5 个回合贡献约 129% 的总盈亏，剔除后整体为亏损，中位回合仅 +286 而平均亏损回合 -3801
3. walk-forward（训练 1500 / 测试 500，`fast_window=5,10;slow_window=40,60`）5 个折叠中 3 个样本外亏损（-21025.87 / -6539.03 / -5929.74），样本外平均夏普 0.098 vs 样本内 0.395，判定过拟合、不建议启用
4. 结论：参考策略在真实螺纹数据上**不可作为可盈利配置启用**，其盈亏结构是「少数大单 + 多数小亏」，2009-2016 的趋势段赚到的钱无法在其他时间窗口复制。仓库默认参数不应直接拿去实盘，必须以真实数据重跑 backtest / optimize 的结论为准
5. 参数扫描（`fast_window=5,10,20;slow_window=40,60`）在真实数据上的最优 `profit_factor` 也仅 1.253，多数组合落在 0.85~1.06，进一步印证上述结构性问题

## 数据健壮性修复

1. `load_bars_from_csv` 新增价格标度突变防护：真实行情源（东方财富期货分钟线）会在同一文件里混入另一套价格标度（实测 2026-11-11 15:00 前后分别为约 11617xxx 与约 30xx，相差约 3830 倍），直接回测会算出 5.7 亿的虚假盈利。现在以首根价格为锚，偏离超过 `MAX_PRICE_JUMP=50` 倍的行被丢弃并 `warnings.warn` 告警；日线数据不受影响（4295 根最大 bar 间跳变仅 1.097 倍），正常 ±10% 波动全部保留。附 2 个回归测试（3830 倍脏标度被丢弃、±10% 正常波动不受影响）

## 多品种真实行情扩展验证（2026-11-12）

数据源从螺纹扩展到 4 个真实品种日线：豆粕 `115.TAM` 4843 根、豆粕（另一连续口径）`115.mam` 3257 根、白银 `113.aum` 4462 根、螺纹 `113.rbm` 4295 根。

1. 参数扫描（fast 5/10/20 x slow 40/60/120 x stop_atr 1/2/3，27 组 x 4 品种）：豆粕 27 组里多数盈利、螺纹盈利组数过半、白银基本不盈利；豆粕 `f=20 s=40 st=3.0` 全样本 profit_factor 最高 1.585
2. 豆粕全样本 walk-forward（训练 1500 / 样本外 500，grid 4 组）样本外合计 -71120.59、平均夏普 -0.302：训练段能赚 66~74 万而样本外翻负，判定过拟合。**全样本盈利不能推出样本外盈利**，与螺纹上的结论一致
3. 加密折叠复验豆粕 walk-forward（训练 1937 / 样本外 968，共 6 折，按「盈利优先、夏普次之」选参）：样本外合计 +269811.96、盈利 4/6 折、平均夏普 0.383、平均回撤 51206

## 跟踪止损与选参规则（本轮改动）

1. 参考策略新增 `trail_stop` 参数（ATR 倍数，默认 0 关闭）：静态止损锚在持仓成本上，趋势跑得越久回吐越多；`trail_stop > 0` 时另跟踪持仓期间最高/最低价（多头取 high、空头取 low，只向有利收紧），止损位取「静态」与「跟踪」中更靠近现价的一侧，持仓归零后极值复位。`variables` 增加 `pos_extreme` 便于观测锚点
2. 真实数据实测 `trail_stop` 效果稳定：豆粕 `f=10 s=60 st=3.0` 下 trail=2 使 profit_factor 1.379 -> 1.759、max_drawdown 186946 -> 45788；螺纹 `f=5 s=60 st=1.0` 下 trail=2 使 profit_factor 1.072 -> 1.040、max_drawdown 41847 -> 22854、sharpe 0.499 -> 0.709。规律是**总盈亏下降、回撤/盈利因子/夏普同时改善**（趋势被提前切掉，但盈亏结构变好）。默认 `trail_stop=0` 保持原行为，合成序列冒烟数字不变（22 笔 / 1 盈利 / total_pnl 13743.02）
3. 选参规则按夏普优先的结论得到真实数据支持：豆粕 walk-forward 对比三种排序（训练 1937 / 样本外 968，共 6 折，81 组含 trail 参数）——盈利优先 +269811.96（4/6 折，平均夏普 0.383，平均回撤 51206）、盈利因子优先 -81910.11（2/6 折，-0.322，36440）、**夏普优先 +178570.16（4/6 折，0.489，22404）**。夏普优先 pnl 低 33% 但夏普高 27%、回撤低 56%，且 6 折全部选中 `trail_stop=2.0`。盈利因子优先反而最差：它会被「一次大单 + 无亏损回合」的高比值骗到，选出交易极少的组合。现有排序键已是 `(sharpe, profit_factor)`，与该结论一致，未改动行为
4. 附 3 个回归测试：跟踪止损随持仓极值收紧、持仓归零后锚点复位、`trail_stop=0` 时静态止损位不变

## 实测证伪记录（补充）

1. 加仓盈利门槛（`add_on_profit`，只在浮盈达到 N 倍 ATR 时加仓）：豆粕/螺纹/白银 3 个真实品种上全部使总盈亏下降（豆粕 710535 -> 679008，螺纹 249757 -> -1899）。趋势策略的盈利来自规模，门槛推迟加仓就是减少盈利敞口，方向已证伪
2. 5 分钟真实数据（东方财富 `klt=5`）：同一文件混有约 11617xxx 与 30xx 两套价格标度（2026-11-11 15:00 为界），以首价为锚时只能保住 1449 根异常标度、丢掉 65 根正常标度，无法自动判定哪段正确。故本轮结论只用日线数据，分钟线未纳入验证

## 调整

1. 交易接口、功能模块、数据库和数据服务补充公开接口文档与类型注解，增加离线测试并纳入持续集成
2. vnpy.alpha的cs_rank改为横截面百分位排名
3. 重构vnpy.alpha的因子和信号绩效分析，移除alphalens-reloaded
4. vnpy.alpha回测增加fill_price参数，新增vwap撮合模式支持
5. 标签周期解析支持ts_delay的任意价格字段
6. 重构vnpy.alpha的ts_mean、ts_std、ts_rank、ts_argmax、ts_argmin、ts_quantile函数，大幅优化性能
7. vnpy_polygon改名为vnpy_massive，通过Massive REST获取美股K线
8. vnpy_mini更新API至1.7.7、交易API至1.7.7
9. vnpy_tap更新API至9.3.1.11、交易API至9.3.9.18
10. vnpy_ctp增加登录时的ProductInfo产品信息传参支持
11. vnpy_ctp的MdApi.subscribeMarketData支持一次传入合约列表
12. vnpy_ctp的纯ASCII字符串的编码转换不再走GBK
13. vnpy_ib更新API至10.45.1

## 修复

1. 修复界面时间列点击无法排序的问题（TimeCell写入时间戳）
2. 修复vnpy.alpha的ta_rsi、ta_atr在多合约数据上串算的问题，改为按合约分别计算
3. 修复BarGenerator.update_tick，最高价和最低价只反映相对上一笔Tick的变化
4. 修复RPC的主题过滤，并保持心跳，避免发布套接字卡住
5. vnpy_ctp在init、join、exit、release期间释放GIL，避免退出死锁
6. vnpy_ctp行情毫秒不足三位时补零
7. vnpy_ctp确保本地SUBMITTING先于该笔柜台回报入队
8. vnpy_femas修正委托时间字段名
9. vnpy_rohon补上Windows运行库，并修正Linux rpath
10. vnpy_sqlite读取时把带时区的查询起止时间转成朴素时间
11. vnpy_questdb将空的database.host和localhost改为127.0.0.1，避免Windows上连接超时
12. vnpy_tushare日线下载使用trade_date
13. vnpy_tushare分钟和小时数据满页后按最早时间再向前翻页，避免重复请求同一页
14. vnpy_xt将深圳创业板合约识别为股票
15. vnpy_portfoliostrategy的分钟K线按完整分钟切片


# 4.4.0版本

## 新增

1. 新增微信通知功能，支持基于微信iLink协议的扫码绑定、消息推送、推送间隔配置和会话过期提示
2. MainEngine新增send_notification函数，支持统一通过邮件和微信渠道发送通知
3. vnpy.alpha增加自定义表达式函数注册机制register_functions
4. vnpy.alpha增加数据处理函数：process_replace_inf、process_ts_norm、process_drop_feature、process_cs_fill_na
5. DataProxy增加更多运算符支持：反向加减乘除、整除、取模、幂运算、负号和不等于比较
6. 新增用于对接QuestDB高性能时序数据库的vnpy_questdb模块
7. vnpy_esunny重构调整为基于易盛启明星V9 API接口开发，支持期货和黄金TD交易

## 调整

1. vnpy.alpha改进数据集、处理器、模型和回测相关实现，优化类型提示和数据处理流程
2. DataProxy的比较运算结果统一返回pl.Int32，并补充相关单元测试
3. 微信通知增加推送间隔控制，间隔内的新消息会暂存并合并发送
4. 替换send_email为新的send_notification消息发送函数：vnpy_ctastrategy / vnpy_portfoliostrategy / vnpy_spreadtrading / vnpy_scripttrader
5. vnpy_tora更新升级：行情API至v1.0.9_20250825、股票API至v4.1.6_20251121、期权API至v1.3.4_20230908
6. vnpy_ctp中对于因为报单提交被拒绝导致的撤单状态，调整映射为拒单状态
7. vnpy_ctp调整特殊情况撤单（非交易时段、资金不足等）的日志输出
8. vnpy_rohon更新升级至6.7.2版本的底层API
9. vnpy_tts更新升级至6.7.11版本的底层API，并增加Mac OS支持

## 修复

1. 修复loguru日志格式化错误
2. vnpy.alpha修复AlphaLab查询信号文件路径错误的问题
3. vnpy_rqdata修复下载数据时，由于end没有时区信息下载报错的问题
4. vnpy_rqdata修复由于没有成交，导致实时行情中字段缺失的问题
5. vnpy_tora修复Fens服务器登录的问题
6. vnpy_spreadtrading修复算法成交判断中的四舍五入误差问题


# 4.3.0版本

## 新增

1. vnpy.alpha增加WorldQuant的Alpha 101因子特征数据集

## 调整

1. vnpy_sec/vnpy_esunny升级适配4.0版本
2. vnpy_ctabacktester的策略代码编辑功能支持cursor和pycharm编辑器
3. vnpy_ctastrategy的回测引擎，增加RGR绩效统计指标（感谢上弦之月贡献）
4. ArrayManager增加对于指标计算函数重载（Function Overload）的类型提示声明
5. vnpy_ctp增加特殊情况撤单（非交易时段、资金不足等）的日志输出
6. DataProxy的所有比较运算，直接返回pl.Int32（而不是Bool）
7. 重构ts_slope / ts_rsquare / ts_resi算子函数

## 修复

1. vnpy_ib修复查询历史数据问题（query_history函数增加查询锁解决多线程冲突问题）
2. vnpy_optionmaster修复深度虚值期权的隐含波动率计算收敛问题


# 4.2.0版本

## 新增

1. vnpy_riskmanager模块重构
    a. 采用插件式设计，提供标准化风控规则开发模板
    b. 支持中国期货程序化交易系统监管要求中的风控规则
    c. 输出拦截日志后播放提示声音，目前仅支持Windows系统
    d. 使用系统托盘栏图标，弹出交易风控拦截日志气泡框
    e. 提供Cython版本的风控规则开发模板以及具体规则实现
    f. 支持自动扫描加载用户自定义风控规则（放置于Trader目录下的rules文件夹中）
2. vnpy_polygon数据服务接口，支持海外股票、期货、期权等资产品种的历史数据获取

## 调整

1. vnpy_ctp更新底层API到6.7.11（生产和测试统一版本）
2. vnpy_dolphindb升级适配4.0版本
3. vnpy_tqsdk简化时间戳的格式化方法，提高效率
4. vnpy_sqlite支持使用配置文件中声明的数据文件
5. vnpy_taos优化get_bar_overview和get_tick_overview函数性能（直接访问超级表的tags）
6. vnpy_spreadtrading / vnpy_portfoliostrategy / vnpy_scripttrader 注册模块日志输出到日志引擎
7. vnpy_optionmaster优化定价模型中期权最小价值边界判断的逻辑
8. 全局配置中的log.level改为INFO（10），默认启用详细日志记录输出
9. MainEngine增加交易功能函数的调用日志输出
10. vnpy.alpha中的Dataset增加process_data函数，便于测试不同数据处理器的效果
11. vnpy_ib更新支持ibapi至10.40.1版本


## 修复

1. vnpy_gm修复中金所和大商所合约代码转换的问题
2. vnpy_rqdata修复RqdataGateway中的行情订阅函数错误问题
3. vnpy_optionmaster修复关闭窗口时的异常报错
4. vnpy_portfoliostrategy修复遗传算法参数优化中的调用传参问题
5. 修复Linux系统上的sdist安装问题：vnpy_mini / vnpy_sopt / vnpy_rohon / vnpy_tap / vnpy_tts
6. vnpy_xt修复XtGateway断线重连时的传参错误
7. vnpy_optionmaster修复black-76模型中theta计算公式的问题
8. vnpy_postgresql修复写入主键冲突时数据不会更新的问题


# 4.1.0版本

# 新增

1. vnpy_mcdata新增对于Tick数据查询的支持
2. OrderType枚举值增加ETF类型，支持ETF申购和赎回业务
3. 增加遗传算法优化函数run_ga_optimization的入参，允许用户控制优化过程中所使用的全部超参
4. CTA策略回测引擎，增加对于遗传算法优化函数新入参的支持

## 调整

1. 升级扩展模块适配4.0版本：
    * 交易接口：
        * 期货类：vnpy_uft/vnpy_mini/vnpy_femas/vnpy_ctptest
        * 股票类：vnpy_xtp/vnpy_tora
        * 期权类：vnpy_hts/vnpy_sopt/vnpy_sopttest
        * 资管类：vnpy_rohon/vnpy_lstar/vnpy_jees
        * 其他类：vnpy_ksgold/vnpy_tts/vnpy_tap/vnpy_da/vnpy_ib
    * 策略应用：
        * 策略类：vnpy_portfoliostrategy/vnpy_ctabacktester/vnpy_spreadtrading/vnpy_scripttrader
        * 交易类：vnpy_algotrading/vnpy_optionmaster/vnpy_portfoliomanager/vnpy_paperaccount
        * 数据类：vnpy_datarecorder/vnpy_excelrtd/vnpy_datamanager
        * 辅助类：vnpy_chartwizard/vnpy_webtrader/vnpy_rpcservice/vnpy_riskmanager
    * 数据库：vnpy_mysql/vnpy_postgresql/vnpy_mongodb/vnpy_taos
    * 数据服务：vnpy_gm/vnpy_xt/vnpy_tqsdk/vnpy_ifind/vnpy_tushare/vnpy_wind
2. 使用close函数替代unbind，来实现vnpy.rpc模块中zmq.Socket的安全关闭
3. 修改PySide6依赖版本为6.8.2.1，解决部分底层warning输出问题
4. 修改ta-lib依赖版本为0.6.4，解决Linux和Mac系统的安装问题
5. 调整Qt层捕捉到全局异常时的日志输出级别为Critical
6. vnpy_datarecorder移除不必要的行情录制异常抛出，改为记录日志
7. vnpy_rqdata下载股票数据时，除权方式有pre改为pre_volume
8. 数据库模块录制行情数据时，默认跳过extra字段
9. vnpy_ib支持10.30.1版本的ibapi，增加对于新版本撤单函数的传参支持

## 修复

1. 修复新版本ta-lib中，MA_Type类不再是枚举值导致的部分指标计算问题
2. 修复补全MainEngine缺失的get_tick函数
3. 修复邮件发送引擎在使用QQ邮箱时出现的发送后报错问题
4. 修复日志模块由于缺失默认gateway_name参数，在Qt层捕捉到全局异常时输出错误的问题
5. vnpy_rohon新增Linux安装脚本，解决动态库找不到的问题
6. vnpy_rqdata修复品种代码为小写合约的次主力88A2历史数据查询问题

# 4.0.0版本

## 新增

1. 新增面向机器学习多因子策略的vnpy.alpha模块
2. MultiCharts数据服务模块vnpy_mcdata

## 调整

1. 核心支持版本升级到Python 3.13
2. 使用pyproject.toml统一项目配置
3. 日志功能使用loguru替代logging
4. 使用mypy优化静态类型声明
5. 使用ruff优化代码细节质量
6. 使用uv作为开发环境管理工具
7. 升级扩展模块适配4.0版本：vnpy_ctp/vnpy_ctastrategy/vnpy_sqlite/vnpy_rqdata

## 修复

1. 修复PySide6中单元格排序可能出现的乱序问题

# 3.9.4版本

## 新增

1. vnpy_tora增加登录时终端动态密钥支持
2. vnpy_taos升级支持TDengine的3.0版本

## 调整

1. vnpy_xt行情接口增加实时行情中的涨跌停价字段
2. vnpy_taos移除不必要的时区转换提高性能
3. vnpy_dolphindb优化写入大量数据时候的内存占用
4. vnpy_portfoliostrategy简化回测引擎的calculate_pnl每日盈亏计算函数
5. vnpy_tap/vnpy_tts升级pybind11封装工具库的版本，支持Python 3.12编译
6. EmailEngine发送邮件失败后，捕捉异常并输出日志

## 修复

1. vnpy_optionmaster移除不必要的价格缓存代码
2. vnpy_dolphindb修复保存overview的时区不正确问题


# 3.9.3版本

## 新增

1. 利星资管交易接口vnpy_lstar
2. 咏春大师数据服务vnpy_voltrader
3. vnpy_rpcservice增加数据服务代理工具RpcDatafeed

## 调整

1. 适配6.3.0版本以上的PySide6模块：vnpy/vnpy_ctastrategy/vnpy_ctabacktester/vnpy_portfoliostrategy/vnpy_spreadtrading/vnpy_datamanager/vnpy_algotrading/vnpy_portfoliomananger/vnpy_optionmaster
2. vnpy_uft升级3.7.4.1004版本API
3. vnpy_ib的execDetails成交回报使用本地缓存的委托记录填充交易所，解决SMART交易所字段可能发生变化的问题
4. vnpy_ib的openOrder委托回报优先使用本地缓存的委托记录，解决交易所字段可能发生变化的问题
5. vnpy_ib的查询历史数据时，使用UTC时间戳传参
6. vnpy_ib的查询历史数据时，异步返回最长等待时间延长为600秒
7. vnpy_ib的增加期权链合约数据更新结束回报
8. vnpy_ib的合约乘数支持浮点数
9. 合约信息ContractData数据类，增加单笔最大委托数量max_volume

## 修复

1. 修复vnpy_spreadtrading回测引擎clear_data时，没有清空价差仓位的问题
2. 修复vnpy_ib查询历史数据失败时的日志输出错误


# 3.9.2版本

## 新增

1. vnpy_xt增加实时行情接口XtGateway
2. vnpy_xt增加基于文件锁实现的xtdc单例运行
3. vnpy_ib增加行情退订功能
4. vnpy_ib的合约乘数支持浮点数
5. vnpy_ib增加期权链合约数据更新结束回报
6. vnpy_ctabacktester、vnpy_ctastrategy、vnpy_portfoliostrategy增加i18n国际化支持

## 调整

1. vnpy_algotrading增加委托/成交推送时，对于算法状态的过滤
2. vnpy_tushare模块的to_ts_asset函数增加ETF基金支持
3. vnpy_xt更新适配xtquant的240613.1.1版本
4. vnpy_xt开启使用期货真实夜盘时间，增加期货历史数据集合竞价K线合成支持
5. vnpy_tts更新API版本到6.7.2
6. vnpy_rohon更新API版本：行情1.4.1.3，交易30.4.1.24
7. vnpy_tap完善API日志输出功能
8. vnpy_rest发送REST请求时，增加对于json参数的支持
9. vnpy_excelrtd优化PyXLL启动时加载模块的方式
10. vnpy_spreadtrading使用线程池实现策略初始化的异步执行
11. vnpy_ib移除期权合约的自动查询功能
12. vnpy_ib缓存查询返回的IB合约数据，简化行情切片查询函数
13. vnpy_ib查询历史数据时，使用UTC时间戳传参，并将最长等待时间延长为600秒
14. vnpy_ctastrategy的绩效统计值增加基于指数移动平均计算的EWM Sharpe比率
15. vnpy_ctastrategy回测引擎的show_chart函数直接返回图表对象

## 修复

1. 修复vnpy_rhon行情登录失败时的判断逻辑问题
2. 修复vnpy_datarecorder记录价差数据时缺失的localtime字段
3. 修复vnpy_spreadtraidng从datafeed加载数据时，时间戳传参缺失时区信息的问题
4. 修复vnpy_paperaccount委托数量为0撮合之后导致的ZeroDivisionError问题
5. 修复vnpy_portoliostrategy停止策略时，没有自动撤销策略委托的功能

# 3.9.1版本

## 新增

1. 增加i18n国际化支持，以及对应的英文翻译
2. 增加CFD和SWAP品种类型枚举值
3. vnpy_ib增加COMEX、Eurex交易所支持
4. vnpy_ib增加CFD品种支持

## 调整

1. vnpy_rqdata完善对于周五夜盘数据查询的支持
2. vnpy_ib订阅行情和委托下单时，检查代码字符串是否包含空格
3. vnpy_ib解析合约对象时，增加对于ConId是否包含非数字字符的检查
4. vnpy_ib查询历史K线数据，支持更长时间段跨度（不再限制半年）
5. vnpy_da更新API版本到1.18.2.0
6. vnpy_da移除历史数据查询功能
7. vnpy_tora调整期权接口的委托号生成规则，支持上限10万数量委托
8. vnpy_xtp调整账户冻结资金的计算逻辑
9. vnpy_optionmaster增加对IB的股票期权品种支持
10. vnpy_optionmaster定价模型改为计算理论希腊值
11. vnpy_optionmaster调整对象希腊值为理论模式
12. vnpy_optionmaster调整中值隐波动的计算方法
13. vnpy_spreadtrading使用线程池实现策略初始化的异步执行
14. vnpy_postgresql支持自动重用已经打开的数据库连接
15. vnpy_ctptest更新API版本至6.7.2
16. 接口封装升级更新pybind11到2.11.1版本：vnpy_ctptest、vnpy_sopttest
17. vnpy_ctp更新API版本到6.7.2
18. 调整extract_vt_symbol函数，兼容代码中带有"."的情况，如HHI.HK-HKD-FUT.HKFE
19. 更新vnpy框架的核心依赖模块到2024年较新的版本

## 修复

1. 修复vnpy_portfoliostrategy调用stop_strategy没有撤销活动委托的问题
2. 修复vnpy_xtp的API封装中queryTickersPriceInfo底层调用错误
3. 修复RpcClient中_last_received_ping变量的类型问题


# 3.9.0版本

## 新增

1. 迅投研数据服务vnpy_xt，支持股票、期货、期权、债券、基金历史数据获取
2. vnpy_ib增加对CBOE和CBOT交易所的支持、对指数期权的支持
3. vnpy_rqdata增加对于88A2连续次主力合约的支持
4. vnpy_wind增加广期所和上期能源交易所的数据支持

## 调整

1. vnpy_sopt升级3.7.0版本API
2. vnpy_portfoliostrategy回测引擎支持交易日参数annual_days
3. K线合成器（BarGenerator）移除对于Tick时间戳的检查过滤逻辑，交由用户层负责控制过滤
4. vnpy_ib收到期权合约数据后，自动查询其切片行情数据
5. vnpy_paperaccount实现对于IB接口合约的特殊路由处理
6. 接口封装升级更新pybind11到2.11.1版本：vnpy_ctp、vnpy_sopt、vnpy_tora
7. vnpy_ctp过滤不支持的委托状态推送
8. vnpy_mysql兼容无数据库写入权限情况下的数据表初始化
9. vnpy_chartwizard支持关闭单个图表标签页
10. vnpy_portfoliostrategy移除策略后同时清除对应的策略状态缓存数据
11. vnpy_portfoliostrategy调整每日盈亏清算对象开盘持仓数据的初始化方式
12. 策略模块遗传优化函数增加ngen_size和max_workers参数


## 修复

1. 修复vnpy_tora接口中的委托部分撤单状态映射缺失
2. 修复vnpy_wind查询日线历史数据时数值存在NaN的问题
3. 修复vnpy_mongodb的Tick汇总数据的条数统计错误
4. 修复vnpy_chartwizard对于升级后的vnpy_spreadtrading价差行情显示问题
5. 修复vnpy_ctastrategy回测成交记录为空时的报错
6. 修复vnpy_ctastrategy策略初始化时，历史数据重复推送调用on_bar的问题


# 3.8.0版本

## 新增

1. K线合成器（BarGenerator）增加对日K线的合成支持
2. 基于华鑫奇点柜台的C++ API重构vnpy_tora，实现VeighNa Station加载支持
3. 新增vnpy_ib对于期权合约查询、波动率和希腊值等扩展行情数据的支持

## 调整

1. vnpy_rest/vnpy_websocket限制在Windows上改为必须使用Selector事件循环
2. vnpy_rest/vnpy_websocket客户端关闭时确保所有会话结束，并等待有异步任务完成后安全退出
3. vnpy_ctp升级6.6.9版本API
4. vnpy_ctp支持大商所的1毫秒级别行情时间戳
5. vnpy_tqsdk过滤不支持的K线频率查询并输出日志
6. vnpy_datamanager增加数据频率下按交易所显示支持，优化数据加载显示速度
7. vnpy_ctabacktester如果加载的历史数据为空，则不执行后续回测
8. vnpy_spreadtrading采用轻量级数据结构，优化图形界面更新机制
9. vnpy_spreadtrading价差子引擎之间的事件推送，不再经过事件引擎，降低延迟水平
10. vnpy_rpcservice增加对下单返回委托号的gateway_name替换处理
11. vnpy_portfoliostrategy策略模板增加引擎类型查询函数get_engine_type
12. vnpy_sec更新行情API至1.6.45.0版本，更新交易API版本至1.6.88.18版本
13. vnpy_ib更新10.19.1版本的API，恢复对于数字格式代码（ConId）的支持
14. 没有配置数据服务或者加载模块失败的情况下，使用BaseDatafeed作为数据服务
15. 遗传优化算法运行时，子进程指定使用spawn方式启动，避免数据库连接对象异常
16. 合约管理控件，增加对于期权合约的特有数据字段显示

## 修复

1. 修复vnpy_datarecorder对于新版本vnpy_spreadtrading价差数据的录制支持
2. 修复vnpy_algotrading条件委托算法StopAlgo全部成交后状态更新可能缺失的问题
3. 修复vnpy_ctastrategy策略初始化时，历史数据重复推送调用on_bar的问题
4. 修复vnpy_wind查询日线历史数据时，数值存在NaN的问题


# 3.7.0版本

## 新增

1. 新增沪股通和深股通交易所枚举值
2. 增加vnpy_tap对于Linux系统的支持
3. 增加vnpy_rqdata对于新型主力合约数据支持（切换前一日收盘价比例复权）

## 调整

1. vnpy_ctastrategy/vnpy_ctabacktester加载策略类时，过滤TargetPosTemplate模板
2. vnpy_ctp连接登录过程中，只有在授权码错误的情况下，才禁止再次发起认证
3. vnpy_uft增加对广期所GFEX的支持
4. vnpy_tqsdk增加对于output日志输出功能的支持
5. vnpy_dolphindb允许指定用户自行配置具体的数据库实例
6. vnpy_rqdata优化对于郑商所期货和期权合约的查询代码转换规则
7. vnpy_rqdata增加对广期所GFEX的支持
8. vnpy_portfoliostrategy增加回测爆仓检查
9. vnpy_portfoliostrategy策略模板增加合约乘数查询函数get_size
10. vnpy_portfoliostrategy回测加载日线和小时线数据时，不使用分段加载

## 修复

1. 修复vnpy_rpcservice中，RPC接口对于推送数据的vt前缀相关字段错误问题
2. 修复vnpy_mini中，对于INE交易所今昨仓位的特殊处理
3. 修复vnpy_datamanager中，批量数据更新时缺失output函数的问题
4. 修复vnpy_spreadtrading中，回测加载数据时优先从数据服务获取历史数据的问题，改为优先从本地数据库加载


# 3.6.0版本

## 新增

1. 新增vnpy_ctp的Mac系统支持（M1/M2）

## 调整

1. BaseDatafeed的相关功能函数增加output入参用于输出日志
2. 修改相关数据服务模块适配output参数：vnpy_rqdata/vnpy_ifind/vnpy_wind/vnpy_tushare
3. 修改相关策略应用模块适配output参数：vnpy_ctastrategy/vnpy_ctabacktester/vnpy_portfoliostrategy/vnpy_spreadtrading/vnpy_datamanager
3. OffsetConverter增加对于SHFE/INE合约的锁仓模式支持
4. 在OmsEngine中添加全局的OffsetConverter功能，不再需要各AppEngine自行维护
5. 添加CTA策略模块在执行参数优化时的最大进程数量限制参数：vnpy_ctastrategy/vnpy_ctabacktester
6. 增加穷举优化算法运行过程中基于tqdm的进度条输出
7. 增加遗传优化算法运行过程中的迭代次数进度输出
8. 增加vnpy_optionmaster模块的期权产品对应标的合约的匹配函数，不再限制产品范围
9.  升级vnpy_tts的dll链接库，解决openctp升级导致的资金不显示的问题
10. 修改vnpy_ctastrategy使用vnpy.trader.database中统一定义的时区来加载数据
11. 增加vnpy_ctastrategy策略模板的合约乘数查询函数get_size
12. 增加vnpy_spreadtrading回测中统计绩效时对于爆仓情况的检查
13. 增加vnpy_scripttrader基于vt_symbol和direction查询持仓数据的函数
14. 修改vt_positionid的字符串内容，增加gateway_name前缀标识

## 修复

1. 修复异常捕捉钩子threading_excepthook的参数错误问题
2. 修复vnpy_ib获取历史数据时的异常失败问题
3. 修复vnpy_rest/vnpy_websocket中aiohttp的代理参数proxy传空时必须为None的问题
4. 修复vnpy_optionmaster模块的Greeks监控表行数设置不足的问题
5. 修复vnpy_rqdata查询股票期权数据报错的问题
6. 修复vnpy_rqdata中RqdataGateway获取期货指数和连续合约信息时错误的问题
7. 修复vnpy_portfoliostrategy中，从缓存文件恢复数据，导致defaultdict变成dict的问题


# 3.5.0版本

## 新增

1. 新增基于米筐RQData的跨市场行情数据接口RqdataGateway
2. 新增东方财富证券EMT柜台交易接口vnpy_emt

## 调整

1. 调整vnpy_algotrading模块设计（模板、引擎），只支持单合约算法执行交易
2. 优化vnpy_algotrading的算法状态控制，增加状态枚举值，算法支持暂停和恢复运行
3. 升级vnpy_hft接口支持HFT国君统一交易网关的2.0版本API
4. 优化vnpy_portfoliostrategy的策略模板，支持持仓目标调仓交易模式

## 修复

1. 修复后台线程异常捕捉钩子函数，对于Python 3.7的语法兼容性问题
2. 修复vnpy_mysql加载历史数据时存在时段重复的问题
3. 修复vnpy_ib由于TWS客户端升级导致的委托失败问题
4. 修复vnpy_rest/vnpy_websocket对Python 3.10后asyncio的支持
5. 修复vnpy_sopt由于流控导致的委托失败时，返回【提交中】状态委托的问题


# 3.4.0版本

## 新增

1. 新增杰宜斯资管系统交易接口vnpy_jees

## 调整

1. 开启vnpy.rpc的pyzmq连接keepalive机制，避免在复杂网络环境下闲置连接的断开
2. 移除vnpy_rpcservice中服务端的EVENT_TIMER定时事件推送
3. 调整vnpy_postgresql采用批量方式写入数据，提高效率
4. 添加VeighNa Trader中的子线程异常捕捉（需要Python>=3.8）
5. 调整vnpy_ib接口查询历史K线数据时，对外汇和贵金属均采用中间价（而非成交价）
6. 增加vnpy_ctastrategy对于回测过程中资金爆仓（小于等于0）情况的检查
7. 优化vnpy_webtrader模块的加密鉴权，支持web进程关闭重启

## 修复

1. 修复vnpy.rpc模块对于23.0以上版本pyzmq的NOBLOCK兼容性问题
2. 修复vnpy_taos由于TDengine版本升级，出现d的一系列兼容问题
3. 修复vnpy_datamanager刷新数据汇总信息显示时，老数据点移除失败的问题



# 3.3.0版本

## 新增
1. 新增数据库组件vnpy.trader.database中的TickOverview对象
2. 新增掘金仿真环境交易接口vnpy_gm
3. BaseData基础数据类型增加extra字段（字典类型），用于传送任意相关数据

## 调整
1. 使用Python内置的zoneinfo库替换三方的pytz库
2. 调整相关交易接口、数据服务接口、数据库适配器、应用模块，使用新的ZoneInfo对象来标识时区信息
3. 数据库适配器接口vnpy.trader.database写入数据时，新增流式写入参数stream，提高行情录制性能


# 3.2.0版本

## 新增
1. 添加广州期货交易所枚举值字段GFEX
2. 新增CTP期权（ETF）穿透式测试接口vnpy_sopttest
3. 新增Currency.CAD（加元）枚举值
4. 新增Exchange.TSE（多伦多交易所）和Exchange.AMEX（美洲交易所）枚举值
5. 新增vnpy_taos，涛思数据TDengine时序数据库适配器
5. 新增vnpy_timescaledb，TimescaleDB时序数据库适配器

## 调整
1. 更新vnpy_ctp/vnpy_ctptest支持广州期货交易所
2. 更新vnpy_tora的现货API接口到最新版本：API_Python3.7_交易_v4.0.3_20220222
3. 更新vnpy_tora的期权API接口到最新版本：API_Python3.7_v1.3.2_20211201
4. 更新vnpy_esunny/vnpy_tap添加关闭接口时对于API退出函数的调用
5. 移除vnpy_ctastrategy/vnpy_ctabacktester/vnpy_optionmaster的反向合约支持
6. 增加vnpy_ib对于沪股通、深股通、多伦多交易所、美洲交易所的支持
7. 增加vnpy_ib对于指数行情数据的支持
8. 添加vnpy_ctastrategy策略交易管理界面的策略实例查找功能

## 修复

1. 修复vnpy_mongodb中K线数据量统计的问题（使用新的count_documents函数）
2. 修复由于PySide6对象销毁先于__del__调用，导致的BaseMonitor衍生组件无法自动保存界面状态的问题



# 3.1.0版本

## 新增
1. 新增恒生云UF2.0证券仿真环境交易接口vnpy_uf
2. 新增火象投教仿真环境交易接口vnpy_hx

## 调整
1. 升级tzlocal库的版本到4.2，消除get_localzone()函数的warning
2. 完善代码中函数和变量类型提示
3. 使用QtCore.Signal代替老的QtCore.pyqtSignal
4. 优化vnpy_rohon接口的委托成交相关细节功能
5. 更新vnpy_xtp到2.2.32.2.0版本XTP API，支持上交所新债系统
6. 优化vnpy_mongodb的数据写入速度，基于pymongo 4.0版本的批量写入功能
7. 增加vnpy_ctp对于委托函数返回值为非0（请求发送失败）状态的处理
8. 对vnpy_ctastrategy和vnpy_ctabacktester的策略模板下拉框中内容，改为基于首字母排序

## 修复
1. 修复vnpy_optionmaster模块希腊值监控组件的数据刷新问题
2. 修复vnpy_mongodb由于时间戳的时区信息确实，导致的数据加载范围问题
3. 修复vnpy_tts的sdist源代码打包缺失lib文件的问题
4. 修复vnpy_rqdata由于查询返回数据为NaN导致的解析问题


# 3.0.0版本

## 调整
1. 移除api、gateway、app子模块的目录
2. 移除requirements.txt对于插件的默认依赖
3. 简化重构rpc子模块，定位于可靠环境下跨进程通讯（本机、局域网）
4. 移除rpc子模块对于鉴权的支持
5. 调整rpc子模块中的心跳机制的实现方式
6. 移除基于QScintilla开发的代码编辑器，改用VSCode打开代码
7. 优化MainWindow主窗口中，对于QAction按钮图标的加载逻辑
8. MainEngine添加交易接口时，支持自定义接口名称

## 修复
1. 使用非原生窗口菜单栏，修复Linux/Mac下【配置】按钮不显示的问题


# 2.9.0版本

## 新增
1. 新增顶点HTS柜台交易接口vnpy_hts

## 调整
1. 移除恒生期权hsoption接口
2. vnpy_webtrader增加对于自定义监听地址和端口的支持
3. vnpy_mongodb锁定pymongo的依赖版本为3.12.3
4. vnpy_udata安装脚本中添加hs_udata库的依赖
5. vnpy_uft升级使用3.7.2.4版本的恒生API接口

## 剥离
2. 将国泰君安证券统一接入网关交易接口剥离到vnpy_hft项目中
3. 将顶点飞创交易接口剥离到vnpy_sec项目中
4. 将RPC服务和接口剥离到vnpy_rpcservice项目中

## 修复
1. 修复vnpy_tora撤单时，由于撤单编号和委托编号冲突导致的撤单失败问题
2. 修复vnpy_tora股票委托状态中【未成交】状态的错误映射问题
3. 修复vnpy_ctabacktester中，回测开始日期编辑框的数据缓存问题
4. 修复vnpy_udata中，分段下载数据时，可能进入死循环的问题
5. 修复vnpy_udata中，修复下载的数据量为空时，出现的异常报错问题
6. 修复vnpy_dolphindb中，合约名带有符号时数据无法读取问题


# 2.8.0版本

## 新增
1. 新增东证OST柜台交易接口vnpy_ost
2. 增加投资组合策略模块的策略参数优化功能

## 修复
1. 修复部分C++接口模块剥离后，遗留的安装脚本编译代码导致的报错问题
2. 修复vnpy_xtp订阅深交所行情后，可能出现的闪退问题
3. 修复vnpy_tushare部分数据字段为None时，导致的数据错误
4. 修复vnpy_mini，在夜盘换日时上期所行情时间戳的日期字段误差问题
5. 修复vnpy_uft的ETF期权合约信息解析缺失的问题
6. 修复vnpy_wind下载数据存在缺失时的N/A解析问题
7. 修复vnpy_webtrader的html静态文件缺失的问题
8. 修复vnpy_dolphindb存储Tick数据时的数据类型问题
9. 修复vnpy_dolphindb读取数据为空时的BUG
10. 修复vnpy_esunny查询黄金TD合约的合约乘数为0的问题
11. 修复vnpy_ctastrategy策略初始化读取布尔值false失败的问题
12. 修复vnpy_rohon的期权合约字段赋值错误的问题
13. 修复vnpy_leveldb的Linux安装依赖库问题

## 调整
1. 移除老版本基于requests库的RestClient客户端
2. 移除老版本基于websocket-client库的WebsocketClient客户端
3. vnpy_tts增加对上交所和深交所股票模拟交易的支持
4. 移除vnpy_ctp的期权询价指令支持
5. 增加vnpy_ctp的授权码验证失败后，避免重复操作的功能
6. 优化vnpy_uft的断线重连行情订阅逻辑
7. 增加vnpy_arctic对于用户名和密码的鉴权功能
8. 增加vnpy_mini对于股指期权的支持

## 剥离
1. 将华鑫奇点交易接口剥离到vnpy_tora项目中，并升级到4.0版本
2. 将飞马交易接口剥离到vnpy_femas项目中
3. 将金仕达黄金接口剥离到vnpy_ksgold项目中
4. 将投资组合策略模块剥离到vnpy_portfoliostrategy项目中
5. 将Excel RTD模块剥离到vnpy_excelrtd项目中
6. 将本地仿真模拟交易模块剥离到vnpy_paperaccount项目中

# 2.7.0版本

## 新增
1. 新增天软数据服务项目vnpy_tinysoft
2. 新增同花顺iFinD数据服务项目vnpy_ifind
3. 新增dYdx交易接口vnpy_dydx
4. 新增万得Wind数据服务项目vnpy_wind
5. 新增PortfolioStrategy专用的PortfolioBarGenerator

## 调整
1. 移除KasiaGateway
4. 移除MarketRadarApp
5. 算法交易模块中移除套利和网格两个非执行类算法
6. vnpy_tushare数据服务，增加持仓量和成交额字段
8. vnpy_datamanager数据管理器，查询的K线信息按合约代码排序显示
13. vnpy_dolphindb优化数据的加载解析速度
14. vnpy_influxdb采用pandas解析CSV数据，提高整体速度

## 修复
1. 修复vnpy_ctp的CtpGateway，在夜盘换日时上期所行情时间戳的日期字段误差问题
2. 修复vnpy_arctic的数据重复写入时出现的错误覆盖问题

## 剥离
1. 将InteractiveBrokers交易接口剥离到vnpy_ib项目中
2. 将飞鼠交易接口剥离到vnpy_sgit项目中
3. 将易盛外盘交易接口剥离到vnpy_tap项目中
4. 将直达期货交易接口剥离到vnpy_da项目中
5. 将算法交易模块剥离到vnpy_algotrading项目中
6. 将脚本交易模块剥离到vnpy_scripttrader项目中
7. 将交易组合管理模块剥离到vnpy_portfoliomanager项目中


# 2.6.0版本

## 新增
1. 增加双边报价业务的发送和撤销函数功能
2. 增加双边报价监控UI组件
3. 增加用于对接数据库的抽象接口vnpy.trader.database
4. 新增基于Arctic的MongoDB数据库接口项目vnpy_arctic
5. 新增LevelDB数据库接口项目vnpy_leveldb
6. 新增DolphinDB数据库接口项目vnpy_dolphindb
7. 增加用于对接数据服务的抽象接口vnpy.trader.datafeed
8. 新增TuShare数据服务项目vnpy_tushare
8. 新增恒生UData数据服务项目vnpy_udata
8. 新增天勤TQSDK数据服务项目vnpy_tqsdk
8. 新增CoinAPI数据服务项目vnpy_coinapi

## 调整
1. 移除批量委托和批量撤单相关的函数功能
2. 移除老虎证券交易接口TigerGateway
3. 移除鑫管家交易接口XgjGateway
4. 移除AlgoTrading算法交易模块对于金纳算法服务的支持
5. RestClient增加对操作系统代理配置的支持
6. RestClient和WebsocketClient的默认异常处理逻辑由抛出异常修改为打印输出
7. 价差交易模块移除对反向合约、线性价差、开平字段的支持
8. 价差交易模块优化对灵活价差的支持，优化价差行情推送过滤判断
9. 价差交易算法停止时，等待全部委托结束、各条腿平衡后，再结束算法

## 修复
1. 修复在Linux/Mac系统上，运行多进程优化时的进程启动错误
2. 修复WebsocketClient由于心跳机制不完善，导致的频繁断线问题

## 剥离
1. 将米筐数据接口剥离到vnpy_rqdata项目中，并升级到2.9.38版本
2. 将行情录制模块剥离到vnpy_datarecorder项目中
3. 将K线图表模块剥离到vnpy_chartwizard项目中
4. 将SQLite数据库接口剥离到vnpy_sqlite项目中
5. 将MySQL数据库接口剥离到vnpy_mysql项目中
6. 将PostgreSQL数据库接口剥离到vnpy_postgresql项目中
7. 将MongoDB数据库接口剥离到vnpy_mongodb项目中
8. 将InfluxDB数据库接口剥离到vnpy_influxdb项目中
13. 将期权波动率交易模块剥离到vnpy_optionmaster项目中


# 2.5.0版本
## 新增
1. 新增TTS交易系统（兼容CTP的仿真交易环境）的接口vnpy_tts（6.5.1）
2. 新增易盛启明星/北斗星兼容交易API的接口vnpy_esunny（1.0.2.2）
3. 新增BarData和TickData的成交额turnover字段

## 调整
1. 将SpreadTrading模块策略初始化时的K线价差数据加载，改为优先通过RQData查询数据
2. 在MainWindow的AboutDialog中，基于importlib_metadata模块来获取版本信息
3. 隐藏所有对话框右上角的【？】按钮
4. 将易盛外盘TapGateway的合约信息，从行情接口获取改为交易接口获取（避免外盘合约size为0的问题）
5. 改进VeighNa Trader的异常捕捉对话框弹出方式，避免多次重复报错情况下的程序卡死崩溃

## 修复
1. 修复Linux下安装时，对于已经剥离的XTP API的自动编译操作
2. 修复PortfolioManager的UI组件，对于成交事件监听类型错误的BUG
3. 修复vnpy_rest下的Response对象缺乏text字段导致的BUG
4. 修复RestClient，代理端口信息传空时，导致底层连接出错的BUG
6. 修复ArrayManager的Aroon指标计算输出结果顺序错误的BUG
7. 修复数据库管理器读写TickData时，由于缺少对localtime字段处理导致的BUG

## 剥离
1. 将融航接口剥离到vnpy_rohon项目中，并升级到6.5.1版本
2. 将CTP MINI接口剥离到vnpy_mini项目中，并升级到1.5.6版本
3. 将CTP期权接口剥离到vnpy_sopt项目中
4. 将恒生UFT柜台极速API接口剥离到vnpy_uft项目中


# 2.4.0版本

## 新增
1. 新增TickData的本地时间戳字段local_time（不带时区信息）
2. 新增基于asyncio和aiohttp实现的协程异步REST API客户端vnpy_rest项目
3. 新增基于asyncio和aiohttp实现的协程异步Websocket API客户端vnpy_websocket项目
4. 新增基于多进程模式的遗传算法优化功能
5. 新增XTP的API封装中，行情登录函数对于本地网卡地址的参数支持

## 调整
2. 剥离CTA策略模块下的穷举和遗传优化算法到vnpy.trader.optimize模块下
3. 遗传算法优化完成后，输出所有回测过的参数对应结果（而不只是最优结果）
4. CTA策略引擎加载策略文件时，增加模块重载的操作，使得任何策略文件修改可以立即生效
5. CTA策略引擎扫描特定目录下的策略文件时，使用glob函数（替换原有的os.walk），避免对子目录中文件的错误加载
6. 将CTA策略模块剥离到vnpy_ctastrategy项目中
7. 将CTA回测模块剥离到vnpy_ctabacktester项目中
8. 将XTP接口剥离到vnpy_xtp项目中，并升级到2.2.27.4版本
9. 将事前风控模块剥离到vnpy_riskmanager项目中
10. 将数据管理模块剥离到vnpy_datamanager项目中

## 修复
2. 修复MySQL和PostgreSQL数据库管理器删除K线数据时出错的问题
3. 修复基于aiohttp的RestClient和WebsocketClient，事件循环停止后重新启动失败的问题
7. 修复CtaBacktester基于Tick级别数据进行参数优化时，启动优化失败的问题
8. 修复ToraStockGateway和ToraOptionGateway，调用下单函数时没有返回委托号的问题
9. 修复InfluxDB数据管理器，导入数据时时间字段解析错误的问题

# 2.3.0版本

## 修复
1. 修复IbGateway断线重连后，没有自动订阅之前已订阅的合约行情问题
2. 修复CTA模块的净仓交易模式中，部分平仓部分开仓时，开仓部分下单错误的问题
6. 修复CtpGateway对于FAK和FOK委托指令的处理错误问题
10. 修复IbGateway，查询历史数据由于传参错误导致的查询失败问题
11. 修复IbGateway，当要查询的合约历史数据不存在时卡死的问题
12. 修复IbGateway，查询返回的合约乘数（字符串）未作转换导致的上层应用问题
14. 修复BarGenerator，在合成小时K线时部分情况下遗漏分钟K线收盘价更新的问题
15. 修复UftGateway，在连接ETF期权服务器时无法订阅行情的问题
16. 修复UftGateway，在连接ETF期权服务器时，对于包含毫秒的委托时间戳处理错误的问题

## 调整
1. 修改CTA模块的净仓交易模式，支持上期所和能交所的今昨仓拆分下单
2. 调整组合策略模块的回测引擎K线回放逻辑，当某个时间点K线数据缺失时，推送给策略的K线字典中不对其进行向前补齐
3. 将CTP接口和API封装，剥离到vnpy_ctp项目中
4. 将CTP穿透式测试接口和API封装，剥离到vnpy_ctptest项目中

## 新增
1. 新增DataManager在导入CSV文件时，对于时间戳时区的选择功能
2. 新增CtaStrategy模块的策略移仓助手功能，实现一键式期货换月移仓支持


# 2.2.0版本

## 修复
1. 修复DataManager查询数据库中K线数据范围时，开始和结束日期相反的问题
6. 修复PostgreSQL数据库对接层中，save_tick_data函数由于访问interval导致保存出错的问题
7. 修复DataRecorder模块中add_bar_recording下保存录制用合约配置错误的问题
8. 修复PostgreSQL数据库对接层中，由于事务执行失败导致的后续报错问题，创建数据库对象时设置自动回滚模式（autorollback=True）
9. 修复DataManager自动更新数据时，查询数据范围由于调用老版本函数导致的错误
10. 修复RQData下载获取的历史数据浮点数精度问题
11. 修复BarGenerator在合成N小时K线时，收盘价、成交量、持仓量字段缺失的问题
12. 修复K线图表底层组件ChartWidget当绘制数据较少时，坐标轴时间点显示重复的问题
13. 修复SpreadTrading模块生成的价差盘口数据的时区信息缺失问题
14. 修复IbGateway的现货贵金属行情数据缺失最新价和时间戳的问题
15. 修复BarGenerator在合成小时级别K线时，成交量字段部分缺失的问题
16. 修复vnpy.rpc模块启用非对称加密后无法正常退出的问题

## 调整
1. 修改vnpy.chart下ChartItem为按需绘制，大幅缩短图表第一次显示出来的耗时
2. 修改IbGateway的历史数据查询功能，包括所有可用时间（即欧美晚上的电子交易时段）
3. 修改DataRecorder的数据入库为定时批量写入，提高录制大量合约数据时的写入性能

## 新增
1. 新增IbGateway连接断开后的自动重连功能（每10秒检查）
2. 新增双边报价业务相关的底层数据结构和功能函数
3. 新增开平转换器OffsetConverter的净仓交易模式
4. 新增CtaStrategy模块策略模板的委托时的净仓交易可选参数
5. 新增CtaStrategy模块回测引擎中的全年交易日可选参数
6. 新增ChartWizard模块对于价差行情图表的显示支持
7. 新增MarketRadar模块的雷达信号条件提醒功能

# 2.1.9.1版本

## 修复
1. 修复RestClient中，因为pyopenssl.extract_from_urllib3引起的兼容性问题

## 调整
1. 调整OptionMaster模块中，期权链数据结构搜索平值行权价的算法，不再依赖标的物合约

## 新增
1. 新增OptionMaster模块使用合成期货作为定价标的合约的功能


# 2.1.9版本

## 修复
1. 修复BarGenerator的小时线合成时，出现同一个小时的K线重复推送两次的问题
2. 修复遗传算法优化时，因为lru_cache缓存导致的新一轮优化结果不变的问题
3. 修复RestClient发起请求时，由于requests库底层使用OpenSSL导致的WinError 10054 WSAECONNRESET的问题
5. 修复程序中频繁捕捉到异常时，异常捕捉对话框反复执行导致卡死的问题
7. 修复活动委托监控组件ActiveOrderMonitor，保存CSV时会将所有委托数据一起保存的问题
8. 修复XtpGateway重复发起登录操作时，出现的系统崩溃问题
9. 修复XtpGateway的股票市价委托类型映射错误问题

## 调整
1. 对XTP接口的行情价格数据基于合约最小价格跳动进行取整，资金保留2位小数
2. BaseMonitor保存CSV文件时，表头改为图形界面显示的中文（之前是数据的字段名英文）
3. 初始化TWAP算法时，对每轮委托数量取整到合约最小交易数量
4. 将原vnpy.trader.database中的数据库客户端拆分到独立的vnpy.database模块下
5. 对SQLite/MySQL/PostgreSQL/MongoDB/InfluxDB客户端进行代码重构优化，增加K线数据整体情况BarOverview查询功能

## 新增
1. 新增BaseMonitor数据监控UI组件（以及其子类），自动保存列宽的功能
2. 增加华鑫奇点ToraGateway对于FENS服务器连接和资金账户登录的支持，之前只支持前置机连接和用户代码登录 
4. 增加InfluxDB数据库客户端vnpy.database.influx对于Tick数据储存和加载的支持
