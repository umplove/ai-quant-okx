# AGENT.md

## 项目概览
- Python 包：`okx_quant_bot`
- 项目用途：OKX 现货、杠杆、永续合约量化交易机器人，包含动量扫描、AI 复盘、Telegram 控制、SQLite 持久化、经验评分和交易安全检查。
- 默认安全姿态：优先模拟盘；非模拟实盘必须显式设置 `ALLOW_LIVE_TRADING=true`。
- 交互和文档要求：默认使用中文回答用户；新增或更新项目文档时默认使用中文。公开文档要写成通用项目说明，不写成只适合某个个人环境的说明。

## 常用命令
- 编译检查：`python -m compileall -q okx_quant_bot tests`
- 测试：`python -m unittest discover -s tests -p "test*.py" -v`
- 本地配置检查：`python -m okx_quant_bot doctor --no-network`
- 主运行命令：`python -m okx_quant_bot run-momentum`
- 单次扫描：`python -m okx_quant_bot scan-momentum`
- 代码说明文档：`docs/CODE_OVERVIEW.md`

## 当前安全架构
- `okx_quant_bot/momentum_runner.py` 负责动量主循环、挂单同步、仓位更新、硬退出、换仓和止损记录维护。
- `okx_quant_bot/exchange/okx.py` 负责 OKX REST 请求、client order id、instrument 精度校验、现货/杠杆/永续下单、撤单和止损算法单。
- `okx_quant_bot/data/storage.py` 负责 SQLite schema 迁移和订单、仓位、止损单、AI 决策、报告、经验评分、异常审计等数据持久化。
- SQLite `ALTER TABLE ... ADD COLUMN` 迁移只能使用常量默认值；时间字段应先 nullable 添加，再回填。
- pending 限价买入使用本地订单状态防止同币种重复提交。
- 限价单 filled/partial filled 后，只按新增成交量增量入账，避免重复加仓。
- active 止损记录要替换而不是叠加；全仓退出后取消剩余 active 止损。
- OKX `tickSz`、`lotSz`、`minSz`、永续 `ctVal` 用于下单前规整或拒绝 payload。
- 动量模式默认保守配置：`SCAN_INTERVAL_SECONDS=300`、`MAX_OPEN_POSITIONS=5`、`ENABLED_MARKET_TYPES=SPOT`。
- 杠杆和永续必须显式开启：`ALLOW_LEVERAGED_TRADING=true`、`ALLOW_DERIVATIVES_TRADING=true`、`ENABLED_MARKET_TYPES=SPOT,MARGIN,SWAP`。
- `MOMENTUM_ENTRY_MODE=rules_first` 下，规则先入场，AI 作为高置信风险否决、归因和训练层，不再是买入必需审批。
- 经验评分记录市场类型、方向、试错成本、分数和分层：`elite`、`active`、`cooldown`、`rejected`、`archived`，原始交易审计记录保留。
- 硬退出优先于 AI 卖出：`MOMENTUM_TAKE_PROFIT_PCT=0.03`、`MOMENTUM_STOP_LOSS_PCT=0.02`、`MOMENTUM_TRAILING_STOP_PCT=0.01`。
- 真实执行支持现货、杠杆和永续。交割合约、期权、网格等仍作为影子学习或后续扩展，除非明确要求实现。

## Git 和文档规则
- 任意文件更新后，如果影响项目行为、流程或交接上下文，需要同步更新本文件。
- 用户可见行为变化后，需要同步更新 `README.md`；公开文档保持通用、可复用，不写私人化部署假设。
- 用户偏好：完成修改并验证后提交并推送到 `origin/main`，方便服务器直接拉取。
- 交易执行代码如果编译或测试失败，不推送。若只改文档且验证失败，最终回复必须明确失败项。

## 后续协作注意
- PowerShell 终端可能把中文显示成乱码，但源码文件按 UTF-8 保存并能正常编译。
- 测试套件基于 `unittest`；本项目验证优先使用上方 unittest 命令，不默认使用 `pytest`。
- 不要移除 demo/live guard、杠杆/永续显式开关、Telegram 安全失败处理，除非用户明确要求。
