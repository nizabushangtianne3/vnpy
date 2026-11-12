# 纸面到实盘晋升手册

自主交易要走向稳定盈利，关键是先把「回测 → 仿真 → 小资金实盘 → 放大」每一关的门槛跑满。本手册给出与本运行器配套的推进节奏。

## 阶段一：历史回测（vnpy_ctabacktester）

1. 对每个候选策略跑参数优化（内置 [vnpy.trader.optimize](https://github.com/vnpy/vnpy/blob/master/vnpy/trader/optimize.py) 支持穷举与遗传算法）。
2. 记录样本外表现：要求样本外夏普、收益回撤比不显著劣于样本内，否则视为过拟合，直接淘汰。
3. 只保留通过门槛的策略进入仿真池。

第 1、2 步由 CLI 完成，判定不再依赖人工读表：

```bash
# 先跑出带样本外结论的 JSON（optimize 单品种，portfolio 多品种组合）
python -m vnpy.autonomous optimize --csv m_daily.csv \
    --grid "fast_window=5,10;slow_window=40,60;stop_atr=2.0,3.0;trail_stop=0.0,2.0,4.0" \
    --train-bars 1400 --test-bars 700 --output m_wf.json

# 再按门槛判定：promote / watch / retire
python -m vnpy.autonomous promote --run "m9999.DCE=m_wf.json"
```

`promote` 只输出三种判定，全部由 JSON 里的样本外数字推导：`promote`（通过全部门槛）、`watch`（通过门槛但组合只有一个品种盈利，其余品种未提供分散化贡献）、`retire`（任一门槛未过，并把失败的原因逐条列出）。它不给「有潜力」这一档——能自己给自己发希望的策略已经失去了停手的能力。门槛值在配置的 `promotion` 段调整（`min_out_pnl` / `min_out_sharpe_ratio` / `min_profitable_fold_ratio` / `min_folds`）。

`min_folds` 不是凑数的门槛。两折样本外一折暴利一折巨亏，相抵消后总数为正、盈利折叠比 50%，看上去「半数时间有效」——那其实只是两个结果，不是重复出现的边缘。实测踩过：沪锡在训练 1400/样本外 700 口径下只有 2 折，正好被这道 50% 线放过去判成 promote；同一份数据换成训练 900/样本外 450 切出 4 折后，其中 2 折分别亏损约 69 万与 129 万，样本外夏普从 0.774 掉到 0.482，改判 retire。**窗口切分不能只跑一种**，折叠数低于 `min_folds` 时一律不予晋级。

`retire` 与 `watch` 的候选必须从配置里摘掉，而不是让带着过期参数继续跑。这一步由 `--apply` 完成，不需要人工誊写：

```bash
python -m vnpy.autonomous promote --apply \
    --run "m9999.DCE=m_wf.json" --run "rb9999.SHFE=rb_wf.json"
```

`--apply` 只改 `app_settings.CtaStrategy.strategies` 这一个键。配置文件里还放着交易接口账号与风控上限，晋升判定无权重写它们，因此不走整套配置序列化。写入是原子的（临时文件 + `os.replace`），值守进程正在读同一文件也不会写坏。重复执行是幂等的：第二次会识别为「参数已刷新为最新验证值」，不会堆出重复条目，值守每轮醒来跑一次即可。

新装配的条目用 `--strategy-class` 指定策略类（默认 `AutonomousTrendStrategy`）。`watch` 不携带参数写回——watch 本身就是拒绝装配的判定，把它的参数写回去等于把刚拒掉的东西装回去。

## 阶段二b：让系统自己走完阶段一到二

上述两步串起来即可闭环：`optimize`/`portfolio` 产结论，`promote` 做判定，人只在判定为 `watch` 或对 `retire` 的原因有异议时介入。真正的自主交易要求这条链在无人值守下也能走完，因此判定逻辑写在 `vnpy.autonomous.promotion` 里并由测试锁定，而不是散落在某次人工比对的记忆里。
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
