# 事前风控守卫生成器

`vnpy.autonomous.risk.RiskGuard` 在安装后包裹 `MainEngine.send_order`：任何模块（策略、算法、脚本）发单前都会先过检查，不合规委托返回空 `vt_orderid` 并记录到状态文件。

## 检查项

- 数量合法性：必须大于 0，且满足合约最小/最大申报单位。
- 单笔数量上限 `max_order_volume` 与名义价值上限 `max_order_value`（价格 x 数量 x 合约乘数，乘数取自合约对象）。
- 全账户活动委托上限 `max_active_orders`。
- 单合约净头寸上限（`max_position_volume`，可按合约用 `symbol_position_limits` 覆盖），按开仓方向预测委托后净头寸。

## 熔断

- 当日权益亏损达到 `daily_loss_limit`，或自时段峰值的回撤达到 `max_drawdown` 时触发。
- 触发后：记录原因、发送通知；`kill_switch_on_breach` 为真时撤销全部活动委托并按市价平掉所有持仓（平仓单走未包裹的原始通道）。
- 熔断状态下默认禁止新开仓（`allow_reduce_when_armed` 控制是否仍允许减仓单），需 `disarm()` 手动解除或重启进程。
- 新交易日开始时可调用 `reset_daily()` 重置日内基准与峰值。

## 对接 vnpy_riskmanager

装配了 `vnpy_riskmanager` 模块时，两者互补：RiskManager 负责监管口径的委托频率/规模等规则，RiskGuard 负责账户级盈亏熔断，可在 `apps` 中同时启用。
