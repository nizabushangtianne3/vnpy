# 配置模型

配置为单个 JSON 文件（默认 `.vntrader/autonomous.json`），由 `vnpy.autonomous.config.load_config` 读取并做嵌套校验：

| 节点 | 内容 |
| --- | --- |
| `mode` | `paper` / `live`，仅作标识与通知用途 |
| `gateways` | 接口名 -> 设置字典（键名沿用各网关文档） |
| `apps` | 功能模块列表，支持内置名或 `module:Class` 导入路径 |
| `calendar` | 时段模板、夜盘结束时间、节假日文件、自定义时段 |
| `risk` | 事前风控上限与熔断开关（见 [risk](./risk.md)） |
| `health` | 策略健康熔断规则（见 [health](./health.md)） |
| `portfolio` | 资金分配方法与权重约束（见 [portfolio](./portfolio.md)） |
| `notification` | 邮件/微信通知开关与心跳间隔 |
| `runtime` | 轮询间隔、状态文件、重启策略、看门狗等运行时参数 |

校验失败时 `load_config` 抛出带全部错误信息的 `ValueError`；`python -m vnpy.autonomous check` 会用同样规则做上线前检查。

未知键会被忽略，便于配置向前兼容；嵌套段缺失时使用各数据类默认值。
