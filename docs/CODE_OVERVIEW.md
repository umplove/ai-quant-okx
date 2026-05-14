# 代码说明文档

这份文档说明 OKX AI Quant Bot 代码在做什么、运行时数据如何从行情走到订单、AI 在哪里参与、风控如何触发，以及经验学习如何沉淀到数据库。

## 1. 项目在做什么

这是一个面向 OKX 的 Python 交易机器人。它可以扫描配置的加密货币交易对，给短线动量机会排序，按配置让 AI 做审核或风险否决，然后执行受保护的订单、管理退出，并把关键决策写入 SQLite。

当前支持三类真实执行路线：

- `SPOT`：现货买卖，OKX 下单使用 `tdMode=cash`。
- `MARGIN`：现货杠杆做多/做空，使用 `tdMode=cross` 或 `tdMode=isolated`。
- `SWAP`：永续合约做多/做空，使用杠杆、`posSide` 和 `reduceOnly` 平仓。

默认配置仍然偏保守：只开现货、优先模拟盘、非模拟实盘需要显式开关。更激进的 AI 试错、杠杆和永续实验通过 `.env` 开启。

## 2. 主运行流程

主命令是：

```bash
python -m okx_quant_bot run-momentum
```

命令先进入 `okx_quant_bot/cli.py`，再启动 `okx_quant_bot/momentum_runner.py` 里的 `MomentumBotRunner`。

每一轮主循环大致做这些事：

1. 校验交易安全配置。
2. 初始化或迁移 SQLite 数据库。
3. 拉取 OKX 行情 ticker。
4. 根据价格变化、振幅、成交量和公开信息构建动量候选。
5. 保存行情快照、情报信息和候选分数。
6. 同步 pending 限价买入单，成交后只按新增成交量更新仓位。
7. 在 AI 卖出前先检查硬止盈、硬止损、移动止盈和最大持仓时间。
8. 复盘当前持仓，记录 mark-to-market 经验。
9. 收集后台 AI 任务结果。
10. 提交市场状态、扫描、买入和卖出 AI 任务。
11. 根据 `MOMENTUM_ENTRY_MODE` 执行符合条件的入场。
12. 将扫描结果送入后台 AI 训练和影子市场学习。
13. 按配置发送 Telegram 资金或状态报告。

每轮结束后休眠 `SCAN_INTERVAL_SECONDS`。

## 3. 行情扫描和候选评分

行情扫描在 `okx_quant_bot/momentum.py`。

`MarketScanner.top_momentum_tickers()` 从 OKX 拉取 spot tickers，并过滤到 `SYMBOLS` 配置内的交易对。只开现货时主要关注上涨币；当启用杠杆或永续时，也会保留下跌币，用于测试 short 机会。

`CandidateScorer.score()` 给每个 ticker 打分，使用：

- 24 小时涨跌幅绝对值；
- 24 小时振幅；
- quote 成交量；
- 新闻和公开情报分；
- 可选的信息面确认；
- 数据库中的历史经验偏置。

输出是排序后的 `CandidateScore` 列表，主 runner 用它决定本轮有哪些入场机会。

## 4. 入场模式

入场模式由 `MOMENTUM_ENTRY_MODE` 控制。

### `ai_required`

这是保守模式。候选币必须同时满足：

- 候选已 confirmed；
- symbol 在配置范围内；
- 同一 symbol 没有 open position；
- 同一 symbol 没有 pending 限价买入；
- 当前持仓数量未超过 `MAX_OPEN_POSITIONS`，或者明确允许换弱仓；
- AI 返回 `action=buy`；
- AI 置信度不低于 `0.65`；
- `AI_EXECUTION_DECISIONS_ENABLED=true`。

### `rules_first`

这是激进学习模式。规则确认的候选可以不等 AI 批准就入场。AI 仍然运行，但角色变成：

- 高置信风险否决；
- 市场状态判断；
- 交易结束后的归因；
- 后台训练样本来源。

如果 `AI_RISK_VETO_ENABLED=true`，高置信 AI `hold` 可以拦截入场。否则系统会继续按规则开仓，以积累更多经验。

## 5. 市场路线选择

`MomentumBotRunner._entry_market()` 决定真实执行路线：

- 如果启用 `SWAP` 且允许 derivatives，则候选走永续合约；
- 否则如果启用 `MARGIN` 且允许 leveraged trading，则候选走现货杠杆；
- 否则走现货。

方向来自候选行情：

- 24 小时上涨通常映射为 `long`；
- 24 小时下跌在启用 `MARGIN` 或 `SWAP` 时可映射为 `short`；
- 现货永远只做 `long`，因为普通现货卖出不等于真实做空。

永续路由会把 `BTC-USDT` 这类 symbol 转成 `BTC-USDT-SWAP` 再下单。

## 6. OKX 下单路由

OKX REST 客户端在 `okx_quant_bot/exchange/okx.py`。

核心数据结构：

- `OrderRequest`：实际订单请求；
- `TradeIntent`：多市场交易意图。

重要方法：

- `place_market_buy_quote()`：现货按 quote 金额市价买入；
- `place_market_sell_base()`：现货按 base 数量市价卖出；
- `place_margin_market()`：杠杆做多/做空开平仓；
- `place_swap_market()`：永续合约做多/做空开平仓；
- `set_leverage()`：杠杆或永续下单前设置杠杆；
- `get_positions()`：读取 OKX 账户仓位；
- `list_open_orders()` 和 `get_order_details()`：同步 pending 订单；
- `place_stop_loss_order()` 和 `cancel_stop_loss_order()`：现货止损算法单。

真实提交前，客户端会读取 OKX public instruments，并按以下字段规整：

- `tickSz`：价格步长；
- `lotSz`：数量步长；
- `minSz`：最小下单量；
- `ctVal` 和 `ctValCcy`：永续合约张数换算。

如果规整后的订单低于 `minSz`，机器人会在本地拒绝并记录执行失败，不把明显错误的 payload 发到 OKX。

## 7. 仓位模型

仓位结构是 `okx_quant_bot/models.py` 里的 `Position`，持久化在 `positions` 表。

每个仓位记录：

- `symbol`；
- `base_qty`；
- `avg_entry_price`；
- `highest_price`；
- `market_type`；
- `direction`；
- `leverage`；
- `margin_mode`；
- `opened_at`；
- `updated_at`。

多头和空头的盈亏方向不同：

- long：价格上涨盈利；
- short：价格下跌盈利。

runner 在卖出、换仓和归因时会按方向计算 PnL 和 return。

## 8. 退出逻辑

硬退出在 AI 卖出前运行，避免已经触发边界却还等 AI。

相关配置：

```env
MOMENTUM_EXIT_GUARD_ENABLED=true
MOMENTUM_TAKE_PROFIT_PCT=0.03
MOMENTUM_STOP_LOSS_PCT=0.02
MOMENTUM_TRAILING_STOP_PCT=0.01
MOMENTUM_MAX_HOLD_MINUTES=0
```

runner 会检查：

- 硬止盈；
- 硬止损；
- 移动止盈回撤；
- 可选最大持仓时间。

现货仓位会维护 active 止损记录；更新止损时替换旧记录，全仓退出后取消剩余 active 止损。

杠杆和永续仓位平仓时使用反向 side，永续/杠杆路线会按需要带 `reduceOnly`。

## 9. 换仓逻辑

当 `MOMENTUM_ROTATION_ENABLED=true` 时，如果持仓已满且出现更强的新候选，runner 可以卖掉弱仓腾出位置。

`MOMENTUM_ROTATION_MODE=aggressive` 会更积极地给新机会腾仓。弱仓主要按当前收益率判断，并考虑 long/short 方向。

这个机制的目标是避免老仓一直占位，导致更强机会无法入场。

## 10. AI 审核和训练

AI 相关代码主要在：

- `okx_quant_bot/ai_reviewer.py`
- `okx_quant_bot/training.py`

AI 可以输出：

- buy/hold 审核；
- sell/hold 审核；
- 入场模式建议；
- 出场模式建议；
- 市场状态分类；
- 交易归因；
- 后台训练决策；
- 影子市场决策。

运行时使用后台 `ThreadPoolExecutor`，AI 调用不会完全阻塞主循环。后台任务完成后，结果会被保存，仍在有效期内时用于执行。

AI Provider 配置：

```env
AI_REVIEW_ENABLED=true
OPENAI_API_KEY=
OPENAI_MODEL=mimo-v2.5-pro
OPENAI_BASE_URL=https://api.xiaomimimo.com/v1
OPENAI_API_MODE=chat
AI_REVIEW_TIMEOUT_SECONDS=12
AI_REQUEST_RETRIES=2
```

## 11. 经验评分

交易归因和经验评分在 `okx_quant_bot/data/storage.py`。

当交易关闭或执行失败时，机器人会记录：

- PnL；
- return percentage；
- market type；
- direction；
- experiment cost；
- market regime；
- AI 归因分类和原因；
- provider 或 exchange 原始输出。

经验分层：

- `elite`：最强、最值得保留的经验；
- `active`：当前可用于决策上下文的经验；
- `cooldown`：暂时较弱或不适合当前市场；
- `rejected`：表现差，不进入活跃决策上下文；
- `archived`：长期归档层，预留给后续清理策略。

原始交易流水和审计记录保留；所谓淘汰坏经验，是把它移出活跃决策上下文，而不是删除原始审计。

## 12. SQLite 持久化

SQLite 是本地唯一状态库，默认路径：

```env
DB_PATH=data/bot.sqlite3
```

主要表：

- `orders`：本地和交易所订单记录；
- `positions`：当前仓位状态；
- `stop_loss_orders`：active/inactive 止损算法单；
- `market_snapshots`：行情快照；
- `candidate_scores`：候选机会评分；
- `ai_decisions`：AI 扫描层决策；
- `execution_decisions`：AI 买卖执行 JSON 决策；
- `ai_call_audits`：prompt、response、token、重试、耗时审计；
- `shadow_decisions`：不真实执行的影子市场学习；
- `trade_attributions`：交易盈亏和 AI 解释；
- `experience_scores`：按 symbol、market type、direction 分组的经验分层；
- `bot_errors`：运行错误。

迁移规则：SQLite `ALTER TABLE ... ADD COLUMN` 只能使用常量默认值；时间字段要先 nullable 添加，再回填。

## 13. Telegram 控制

Telegram 集成在 `okx_quant_bot/notify.py`，命令处理在 `MomentumBotRunner._handle_controls()`。

常用命令：

- `/health`：数据库、Telegram、AI、训练健康状态；
- `/positions`：仓位、路线、方向、杠杆、浮盈和退出距离；
- `/execution`：硬退出、市场路线、杠杆、入场模式、风险否决和最近执行决策；
- `/lessons`：最近交易归因和经验分层；
- `/shadow`：最近影子市场建议；
- `/errors`：最近运行错误；
- `/training`：AI 训练用量和队列状态。

Telegram 发送失败会记录，但不会让机器人崩溃。

## 14. 安全开关

重要安全变量：

```env
TRADING_ENABLED=false
OKX_DEMO=true
ALLOW_LIVE_TRADING=false
ALLOW_LEVERAGED_TRADING=false
ALLOW_DERIVATIVES_TRADING=false
DERIVATIVES_DEMO_FIRST=true
RISK_HALT_ENABLED=false
```

非模拟实盘需要 `ALLOW_LIVE_TRADING=true`。

杠杆交易需要 `ALLOW_LEVERAGED_TRADING=true`。

永续交易需要 `ALLOW_DERIVATIVES_TRADING=true`。

如果 `DERIVATIVES_DEMO_FIRST=true`，即使普通实盘允许，live swap 也会被阻止，必须显式确认后才能开。

## 15. 配置示例

保守现货模拟：

```env
TRADING_ENABLED=true
OKX_DEMO=true
ALLOW_LIVE_TRADING=false
ENABLED_MARKET_TYPES=SPOT
MAX_OPEN_POSITIONS=5
SCAN_INTERVAL_SECONDS=300
MOMENTUM_ENTRY_MODE=ai_required
MAX_LEVERAGE=1
```

激进模拟学习：

```env
TRADING_ENABLED=true
OKX_DEMO=true
ALLOW_LIVE_TRADING=false
ENABLED_MARKET_TYPES=SPOT,MARGIN,SWAP
ALLOW_LEVERAGED_TRADING=true
ALLOW_DERIVATIVES_TRADING=true
DERIVATIVES_DEMO_FIRST=false
MAX_LEVERAGE=5
MAX_OPEN_POSITIONS=10
SCAN_INTERVAL_SECONDS=30
MOMENTUM_ENTRY_MODE=rules_first
AI_RISK_VETO_ENABLED=true
MOMENTUM_ROTATION_MODE=aggressive
MOMENTUM_MAX_HOLD_MINUTES=180
```

## 16. 部署更新流程

服务器更新通常这样做：

```bash
cd ~/ai-quant-okx
sudo systemctl stop okx-quant-bot
git pull origin main
source .venv/bin/activate
python -m pip install -e .
python -m okx_quant_bot doctor --no-network
sudo systemctl restart okx-quant-bot
sudo journalctl -u okx-quant-bot -f
```

重大升级前建议备份 SQLite：

```bash
[ -f data/bot.sqlite3 ] && cp data/bot.sqlite3 data/bot.sqlite3.bak.$(date +%Y%m%d-%H%M%S)
```

## 17. 测试

编译：

```bash
python -m compileall -q okx_quant_bot tests
```

运行测试：

```bash
python -m unittest discover -s tests -p "test*.py" -v
```

测试覆盖配置校验、AI 解析、存储迁移、OKX payload 构建、现货/杠杆/永续路由、动量入场退出、pending 订单安全、Telegram 控制和训练池统计。
