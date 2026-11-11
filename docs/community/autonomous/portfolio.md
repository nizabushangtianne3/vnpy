# 资金分配

`vnpy.autonomous.portfolio.CapitalAllocator` 把账户权益映射为各策略的目标资金：

- `equal`：等权分配。
- `inverse_volatility`：按策略权益曲线波动率的倒数加权，波动小的策略分得更多；无波动数据时退化为等权。

约束：`cash_fraction` 预留现金；`min_weight`/`max_weight` 为单策略权重上下限。达到上限时使用水填算法——超限策略被钉在上限，剩余权重按比例重分；当 `策略数 x max_weight < 1` 时上限无法同时满足，按上限封顶后差额保留为现金，不再归一化放大回超限权重。

目标资金只是分配建议，需要策略在开仓时读取使用（例如通过 RPC 或共享状态文件）。
