# OKX AI Quant Bot

OKX AI Quant Bot 是一个 Python 量化交易机器人，用于 OKX 现货、杠杆和永续合约研究流程。项目默认优先模拟盘，并在实盘、杠杆和衍生品交易前设置显式安全开关。

项目组合了 OKX 行情扫描、动量评分、可选 AI 审核、Telegram 控制、SQLite 持久化、订单同步、止损管理和经验评分，可作为多市场交易实验和自动化研究基础。

详细代码说明见：[docs/CODE_OVERVIEW.md](docs/CODE_OVERVIEW.md)。

## 功能

- OKX 现货行情、余额查询、市价单、限价单、撤单、pending 订单查询和止损算法单。
- 可选 OKX 杠杆和永续合约路由，必须显式开启杠杆和衍生品开关。
- 默认启用模拟盘保护：`OKX_DEMO=true`，`ALLOW_LIVE_TRADING=false`。
- 根据 24 小时涨跌幅、振幅、成交量、公开信息和历史经验进行动量候选排序。
- 短线动量模式默认 5 分钟扫描一次，最多 5 个并发仓位。
- 可选 AI 审核买入、卖出、市场状态、执行模式和交易归因。
- 后台 AI 训练和影子市场评估，不阻塞主交易循环。
- Telegram 通知和命令控制，支持状态、AI、仓位、训练、健康、错误、执行决策、经验和市场状态查询。
- SQLite 存储 candles、orders、positions、stop-loss orders、AI audits、strategy lessons、experience tiers、market intelligence 和 runtime errors。
- 订单安全层支持 pending 限价入场防重复、成交同步、增量仓位更新、active 止损替换和 OKX 精度校验。
- 默认只依赖 Python 标准库；WebSocket 支持可选安装。

## 安全模型

机器人默认保守：

- `TRADING_ENABLED=false` 时，只扫描、调用 AI、训练和记录，不发送订单。
- `OKX_DEMO=true` 时，开启交易后向 OKX 模拟盘发送请求。
- `ALLOW_LIVE_TRADING=false` 会阻止非模拟实盘启动。
- 限价买入单会一直保持 pending，直到 OKX 返回成交；同一 symbol 的重复 AI 买入不会创建重复 pending 入场单。
- 动量仓位默认有硬退出：3% 止盈、2% 止损、1% 移动止盈回撤保护。
- AI 可以建议提前退出或调整止损，但不能取消硬止损边界。
- 止损更新会替换 active 止损记录，不会给同一个仓位堆叠多个 active 止损。
- 实盘提交前会用 OKX instrument 元数据规整价格和数量：`tickSz`、`lotSz`、`minSz`。
- 现货使用 `tdMode=cash`。杠杆和永续需要 `ALLOW_LEVERAGED_TRADING=true` 或 `ALLOW_DERIVATIVES_TRADING=true`，并通过 `ENABLED_MARKET_TYPES` 启用。
- 永续下单前会设置杠杆，合约单使用 `posSide`，平仓路线使用 `reduceOnly`，合约张数按 instrument 元数据换算。
- `rules_first` 模式下，规则确认的机会可以先入场，AI 作为高置信风险否决和归因层。

本仓库不是投资建议。任何自动化交易系统都需要自行审查、测试和谨慎运行。

## 安装

```bash
git clone https://github.com/umplove/ai-quant-okx.git
cd ai-quant-okx
python -m pip install -e .
```

可选 WebSocket 依赖：

```bash
python -m pip install "websockets>=12.0"
```

## 配置

创建本地 `.env`：

```bash
cp .env.example .env
```

最小 OKX 和运行配置：

```env
OKX_API_KEY=
OKX_SECRET_KEY=
OKX_PASSPHRASE=

OKX_DEMO=true
OKX_SIMULATED_TRADING_HEADER=true
TRADING_ENABLED=false
ALLOW_LIVE_TRADING=false

SYMBOLS=BTC-USDT,ETH-USDT
DB_PATH=data/bot.sqlite3
```

可选 AI 配置：

```env
AI_REVIEW_ENABLED=false
OPENAI_API_KEY=
OPENAI_MODEL=mimo-v2.5-pro
OPENAI_BASE_URL=https://api.xiaomimimo.com/v1
OPENAI_API_MODE=chat
```

可选 Telegram 配置：

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_CONTROLS_ENABLED=true
TELEGRAM_AUTO_REPORTS=false
```

## 使用

初始化数据库：

```bash
python -m okx_quant_bot init-db
```

无网络配置检查：

```bash
python -m okx_quant_bot doctor --no-network
```

带网络配置检查：

```bash
python -m okx_quant_bot doctor
```

单次动量扫描：

```bash
python -m okx_quant_bot scan-momentum
```

运行动量机器人：

```bash
python -m okx_quant_bot run-momentum
```

旧版 EMA/RSI runner 仍可用：

```bash
python -m okx_quant_bot run
```

## Docker

使用 Docker Compose 构建和运行：

```bash
docker compose up --build
```

Compose 会把 `./data` 挂载到容器内，SQLite 状态可跨重启保留。

## 交易参数

常用配置：

```env
SCAN_INTERVAL_SECONDS=300
CANDIDATE_TOP_N=20
MAX_OPEN_POSITIONS=5
TARGET_POSITION_USDT=1000
RISK_PER_TRADE_USDT=200
STOP_MODE=percent
INITIAL_STOP_LOSS_PCT=0.20
FIXED_STOP_LOSS_USDT=200
ENABLED_MARKET_TYPES=SPOT
ALLOW_LEVERAGED_TRADING=false
ALLOW_DERIVATIVES_TRADING=false
DERIVATIVES_DEMO_FIRST=true
MARGIN_MODE=isolated
POSITION_MODE=long_short
MAX_LEVERAGE=1
MOMENTUM_ENTRY_MODE=ai_required
AI_RISK_VETO_ENABLED=true
MOMENTUM_ROTATION_ENABLED=true
MOMENTUM_ROTATION_MODE=conservative
MOMENTUM_MAX_HOLD_MINUTES=0
MOMENTUM_EXIT_GUARD_ENABLED=true
MOMENTUM_TAKE_PROFIT_PCT=0.03
MOMENTUM_STOP_LOSS_PCT=0.02
MOMENTUM_TRAILING_STOP_PCT=0.01
LIMIT_ORDER_ENABLED=true
SPLIT_ORDER_PARTS=3
PARTIAL_SELL_FRACTIONS=0.3,0.5,1.0
REPLACE_WEAK_POSITION_ENABLED=true
AI_EXECUTION_DECISIONS_ENABLED=true
```

激进多市场模拟实验配置：

```env
ENABLED_MARKET_TYPES=SPOT,MARGIN,SWAP
ALLOW_LEVERAGED_TRADING=true
ALLOW_DERIVATIVES_TRADING=true
DERIVATIVES_DEMO_FIRST=false
MAX_LEVERAGE=5
MAX_OPEN_POSITIONS=10
SCAN_INTERVAL_SECONDS=30
MOMENTUM_ENTRY_MODE=rules_first
MOMENTUM_ROTATION_MODE=aggressive
```

建议先使用模拟盘 key。杠杆和永续会放大亏损，并且 OKX 账户需要支持所选保证金模式和持仓模式。

更严格的风险暂停配置：

```env
RISK_HALT_ENABLED=true
MAX_DAILY_LOSS_PCT=0.03
MAX_CONSECUTIVE_LOSSES=3
```

模拟学习和研究场景下，`RISK_HALT_ENABLED=false` 可以让机器人在亏损后继续积累经验。更保守运行时建议开启。

默认配置下，动量 runner 每 5 分钟扫描新机会。如果当前仓位少于 5 个，且候选没有同币种 pending 入场单，则符合条件的 symbol 可以继续入场，已有仓位独立管理。

硬退出会在 AI 卖出决策前检查：

- `MOMENTUM_TAKE_PROFIT_PCT=0.03`：约 +3% 全仓卖出或平仓。
- `MOMENTUM_STOP_LOSS_PCT=0.02`：约 -2% 全仓卖出或平仓。
- `MOMENTUM_TRAILING_STOP_PCT=0.01`：创新高后从高点回撤 1% 触发移动止盈。

Telegram `/positions` 会在有新行情快照时显示浮盈和距离止盈/止损的大致距离。`/execution` 会显示硬退出和多市场路由配置。

## AI 和训练

AI 审核可以输出结构化 JSON 决策：

- 动量候选的 buy 或 hold；
- 持仓的 sell、partial sell、trail profit、breakeven；
- 入场模式：market、limit pullback、split limit、breakout confirmation、wait；
- 仓位大小模式：explore、reduced、normal、strong；
- 市场状态分类；
- 交易归因。

AI 决策围绕硬风控运行。AI 可以收紧退出或建议提前卖出，但硬止损和硬止盈优先执行。`rules_first` 模式下，AI 是风险否决和归因层，不是买入必需审批。

经验评分会保存试错成本、收益率、PnL 和交易方向，并分成 `elite`、`active`、`cooldown`、`rejected`、`archived`。活跃决策上下文应优先使用 `elite` 和 `active`，同时保留原始交易和审计历史。

训练和审计记录包括 prompt 字符数、response 字符数、prompt tokens、completion tokens、total tokens、attempted tokens、retry count、task count、success count 和 error count。

### 模拟盘真实同步模式

当前动量机器人会优先围绕 OKX 模拟盘真实账户运行：

- 每轮扫描会同步 OKX 账户权益、现货资产、杠杆/永续持仓和 pending 订单。
- Telegram 状态会显示 OKX 总权益、可用、占用、OKX 持仓、本地持仓、同步状态和 AI 调用去向。
- `AI_TRAINING_ENABLED=true` 表示基于真实模拟盘持仓做经验复盘，不再为未执行的影子市场策略批量烧 token。
- 持仓未满时，买入 AI 审核数量会按剩余仓位动态裁剪；持仓已满时，停止普通买入 AI，优先管理持仓、卖出、换仓和移动止损。
- AI 决策 JSON 解析失败会自动尝试一次短 JSON 修复；仍失败则记录为无效决策，不进入交易执行。

## 测试

编译包和测试：

```bash
python -m compileall -q okx_quant_bot tests
```

运行测试套件：

```bash
python -m unittest discover -s tests -p "test*.py" -v
```

测试覆盖 AI 解析、MiMo 兼容请求体、存储持久化、动量评分、订单执行模式、限价单、拆分单、部分卖出、止损替换、Telegram 控制、OKX 现货/杠杆/永续客户端行为、经验评分和训练池统计。

## 项目结构

```text
okx_quant_bot/
  ai_reviewer.py       AI 请求、响应解析和决策辅助
  cli.py               命令行入口
  config.py            环境变量加载和安全校验
  data/storage.py      SQLite schema 和持久化
  exchange/okx.py      OKX REST 客户端和订单精度校验
  momentum.py          行情扫描和候选评分
  momentum_runner.py   主执行循环和交易安全逻辑
  notify.py            Telegram 通知和控制
  risk.py              旧策略风险控制
  training.py          后台 AI 训练池
tests/                 unittest 测试
systemd/               systemd 服务示例
docs/                  项目说明文档
```

## 运维注意

- 不要提交 API key。`.env` 已被 Git 忽略。
- 配置和通知验证前，建议保持 `TRADING_ENABLED=false`。
- 实盘前优先使用 OKX 模拟盘验证。
- SQLite 会在启动时自动迁移；重大升级前建议备份 `data/bot.sqlite3`。
- 部署后关注 `doctor`、Telegram `/health` 和数据库中的 `bot_errors`。
- 如果服务器从 GitHub 拉取部署，建议使用测试通过后的 `main`。
