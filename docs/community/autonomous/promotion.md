# 纸面到实盘晋升手册

自主交易要走向稳定盈利，关键是先把「回测 → 仿真 → 小资金实盘 → 放大」每一关的门槛跑满。本手册给出与本运行器配套的推进节奏。

## 阶段一：历史回测（vnpy_ctabacktester）

1. 对每个候选策略跑参数优化（内置 [vnpy.trader.optimize](https://github.com/vnpy/vnpy/blob/master/vnpy/trader/optimize.py) 支持穷举与遗传算法）。
2. 记录样本外表现：要求样本外夏普、收益回撤比不显著劣于样本内，否则视为过拟合，直接淘汰。
3. 只保留通过门槛的策略进入仿真池。

## 阶段二：仿真交易（SIMNOW / TTS）

1. 配置 `gateways` 使用仿真接口，`mode` 设为 `paper`，按正式风控参数运行（见 [risk](./risk.md)）。
2. 至少覆盖一个完整月，包含夜盘与节假日前后。
3. 观察 `.vntrader/autonomous_state.json`：健康熔断不应频繁触发；若触发，回到阶段一修改策略或风控，而不是放宽阈值。
4. 关注成本：把回测手续费/滑点调到与仿真成交一致，让回测曲线可信。

## 阶段三：小资金实盘

1. `portfolio.max_weight` 收紧（如 0.2），`risk.max_order_volume`、`daily_loss_limit`、`max_drawdown` 按账户规模设置。
2. 单策略以最小手数运行 2-4 周，用成交台账核对信号质量与滑点。
3. 只有连续达到预设月收益/回撤目标后，才按 `portfolio` 分配逐步放大。

## 阶段四：组合运行

1. 多策略并行时用 `portfolio.method = "inverse_volatility"` 平衡波动贡献。
2. 保留 10% 以上现金（`cash_fraction`）应对追保与跳空。
3. 每次调整只改一个变量，用状态文件与通知留痕，方便定位回退。

## 硬性纪律

- 任何阶段触发熔断平仓后，必须先复盘再 `disarm()`，不允许带着未解释的回撤继续跑。
- 修改节假日清单、时段模板后，先用 `python -m vnpy.autonomous check` 与 `sessions` 核对下一次开盘时间。
- 实盘账户密码只放在本机 `.vntrader/`，不要提交到仓库。
