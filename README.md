# OKX AI Quant Bot

这是一个面向 OKX Demo Trading 的 AI 辅助加密货币交易机器人。当前版本的核心目标不是省 token，而是在模拟盘里高频扫描、高频复盘、高频训练，让 AI 持续积累交易经验，同时所有真实执行仍被限制在 OKX Demo 现货范围内。

项目当前已经从最早的“规则策略机器人”升级为“行情扫描 + 信息信号 + AI 买卖把关 + AI 执行决策 + 后台训练池 + Telegram 手动报表”的版本。

## 当前做到什么程度了

### 已完成

- OKX Demo Trading 接入：支持行情、余额、现货市价买入、现货市价卖出、现货限价买入、现货限价卖出、撤单、查询挂单、止损条件单。
- 模拟盘保护：默认 `OKX_DEMO=true`，并且 `ALLOW_LIVE_TRADING=false`；真实执行只允许 OKX Demo 现货。
- 动量候选池：从 OKX 行情中扫描候选币，结合 24h 涨幅、振幅、成交额、信息信号、历史经验生成候选排序。
- 信息源接入：支持新闻/RSS、OKX 公告、Binance 公告、CoinGecko trending、Fear & Greed、OKX 技能广场只读信号 URL。
- AI 买入/卖出把关：AI 会对候选币和持仓给出中文 JSON 决策，程序再按本地风控和执行规则落地。
- AI 执行决策：AI 不只回答买不买、卖不卖，还能决定怎么买、怎么卖、下多大、是否换仓、止损止盈怎么处理。
- 后台 AI 训练池：训练任务独立运行，不阻塞交易主循环；支持每周 token 目标、成功 token、估算尝试 token、任务数统计。
- MiMo 接入：支持小米 MiMo OpenAI-compatible Chat Completions，默认模型为 `mimo-v2.5-pro`。
- timeout 重试：AI 请求支持超时、重试、审计记录；交易主循环不会因为单次 AI 慢请求卡死。
- SQLite 稳定性：数据库启用 WAL、busy timeout、写入短重试，降低后台训练和交易线程并发写入锁库风险。
- 经验系统：交易结果、订单失败、止损失败、AI 归因会写入数据库，作为后续 AI prompt 和候选评分上下文。
- 非绝对学习：亏损经验只影响相似场景评分和谨慎程度，不永久封杀币种。
- 探索机制：支持 `AI_EXPLORATION_FRACTION`，保留小仓位试错空间，避免机器人越亏越不敢买。
- 影子全市场：现货以外的 margin、swap、futures、options、grid、trailing 等只做 AI 影子判断和经验记录，不真实下单。
- Telegram 手动菜单：默认不刷屏，用户需要时通过菜单命令查看状态、AI、持仓、训练、执行、经验、行情。
- 中文输出：启动诊断、Telegram 菜单、AI prompt、资金报表、训练状态、经验归因已经改成中文。
- 测试覆盖：当前回归测试 65 个，覆盖 AI 请求、MiMo body、执行决策解析、限价单、分批挂单、部分卖出、换仓相关存储、Telegram 控制、OKX 客户端等。

### 刻意没有做

- 不执行实盘：除非你主动改成 `OKX_DEMO=false` 且 `ALLOW_LIVE_TRADING=true`，否则不会走实盘。
- 不执行合约/杠杆/期权实单：这些目前只做影子训练。
- 不让第三方技能直接下单：OKX 技能广场只作为外部信号源，最终买卖仍走本项目统一风控、仓位、订单记录和 OKX API。
- 不承诺盈利：这是训练和验证系统，不是投资建议。

## 推荐运行模式

当前主推荐入口是：

```bash
python -m okx_quant_bot run-momentum
```

旧入口 `run` 仍保留，用于早期 EMA/RSI 趋势回调策略；现在这套 AI 训练、AI 执行决策、影子全市场和 Telegram 手动报表主要在 `run-momentum` 中运行。

## 快速开始

### 1. 安装

```bash
git clone https://github.com/umplove/ai-quant-okx.git
cd ai-quant-okx
python -m pip install -e .
```

### 2. 创建配置

```bash
cp .env.example .env
```

然后编辑 `.env`，至少填入：

```env
OKX_API_KEY=你的OKX Demo API Key
OKX_SECRET_KEY=你的OKX Demo Secret
OKX_PASSPHRASE=你的OKX Demo Passphrase

OKX_DEMO=true
OKX_SIMULATED_TRADING_HEADER=true
TRADING_ENABLED=true
ALLOW_LIVE_TRADING=false

OPENAI_API_KEY=你的小米MiMo API Key
OPENAI_MODEL=mimo-v2.5-pro
OPENAI_BASE_URL=https://api.xiaomimimo.com/v1
OPENAI_API_MODE=chat

TELEGRAM_BOT_TOKEN=你的Telegram Bot Token
TELEGRAM_CHAT_ID=你的Telegram Chat ID
```

也可以用：

```env
MIMO_API_KEY=你的小米MiMo API Key
OPENAI_API_KEY=${MIMO_API_KEY}
```

程序已支持简单的 `${MIMO_API_KEY}` 展开。如果你的运行环境不展开变量，建议直接把 key 填到 `OPENAI_API_KEY`。

### 3. 初始化数据库

```bash
python -m okx_quant_bot init-db
```

默认数据库路径：

```env
DB_PATH=data/bot.sqlite3
```

### 4. 本地诊断

不联网诊断：

```bash
python -m okx_quant_bot doctor --no-network
```

联网诊断：

```bash
python -m okx_quant_bot doctor
```

### 5. 启动 AI 动量机器人

```bash
python -m okx_quant_bot run-momentum
```

如果只想扫一轮：

```bash
python -m okx_quant_bot scan-momentum
```

## 关键安全边界

### Demo Trading

推荐始终保持：

```env
OKX_DEMO=true
OKX_SIMULATED_TRADING_HEADER=true
ALLOW_LIVE_TRADING=false
```

这表示走 OKX Demo Trading，并在请求头中带上模拟盘标记。

### 是否真的下单

```env
TRADING_ENABLED=true
```

只有这个开关为 `true` 时才会发送订单。否则程序会跑扫描、AI、训练和记录，但不会真实发单。

### 实盘保护

```env
ALLOW_LIVE_TRADING=false
```

即使误改了部分配置，只要这个仍是 `false`，程序不会允许实盘模式启动。

### 模拟盘疯狂训练模式

当前的目标是前期用模拟盘疯狂积累经验，所以推荐：

```env
RISK_HALT_ENABLED=false
AI_ALWAYS_ON=true
AI_TRAINING_ENABLED=true
AI_WEEKLY_TOKEN_TARGET=1000000000
```

`RISK_HALT_ENABLED=false` 表示不启用每日亏损熔断、连续亏损暂停、币种暂停等机制。模拟盘哪怕亏完也继续跑，用来换经验。

## 交易宇宙配置

```env
SYMBOLS=BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,DOGE-USDT,BNB-USDT,ADA-USDT,AVAX-USDT,LINK-USDT,DOT-USDT
MAX_OPEN_POSITIONS=10
SCAN_INTERVAL_SECONDS=30
CANDIDATE_TOP_N=20
```

说明：

- `SYMBOLS` 是真实下单候选池。
- `MAX_OPEN_POSITIONS=10` 表示最多允许 10 个现货持仓。
- 如果实际一直是 `4/10`，通常不是写死 4 个，而是因为 AI 没批准、候选不足、已有持仓、挂单/止损失败、资金条件不足、或价格条件没触发。
- `CANDIDATE_TOP_N` 控制每轮参与 AI 学习和候选比较的前 N 个机会。

## 仓位与止损止盈

```env
TARGET_POSITION_USDT=1000
RISK_PER_TRADE_USDT=200
STOP_MODE=percent
INITIAL_STOP_LOSS_PCT=0.20
FIXED_STOP_LOSS_USDT=200
TAKE_PROFIT_PCT=0.03
TRAILING_STOP_PCT=0.01
```

当前行为：

- 普通仓位目标为 `TARGET_POSITION_USDT`。
- AI 的 `size_mode` 会调整仓位：
  - `explore`：探索仓，约 30% 正常仓位。
  - `reduced`：降低仓，约 50% 正常仓位。
  - `normal`：正常仓位。
  - `strong`：强信号仓，约 150% 正常仓位。
- 买入后会尝试下止损单。
- 止损单失败不会停止机器人，会记录为执行失败经验。

## AI 配置

### 推荐配置

```env
AI_REVIEW_ENABLED=true
AI_REVIEW_INTERVAL_SCANS=1
AI_REVIEW_MAX_CANDIDATES=5
AI_REVIEW_MAX_TOKENS=8000
AI_REVIEW_TIMEOUT_SECONDS=20
AI_REQUEST_RETRIES=2
AI_RETRY_BACKOFF_SECONDS=1.5

OPENAI_API_KEY=你的 API Key
OPENAI_MODEL=mimo-v2.5-pro
OPENAI_BASE_URL=https://api.xiaomimimo.com/v1
OPENAI_API_MODE=chat
```

MiMo 请求使用 OpenAI-compatible Chat Completions：

- URL：`/chat/completions`
- 字段：`max_completion_tokens`
- `temperature=1.0`
- `top_p=0.95`
- `extra_body={"thinking":{"type":"disabled"}}`

程序会记录：

- prompt 字数
- response 字数
- prompt tokens
- completion tokens
- total tokens
- timeout 次数
- retry 次数
- 最近错误
- 平均耗时

### AI 不阻塞交易主循环

交易主循环不会一直等某个 AI 请求返回。它会：

1. 投递买入、卖出、行情状态、扫描复盘任务。
2. 收集已经完成的 AI 决策。
3. 使用最近一次有效 AI 决策执行交易。
4. 超时或失败就记录审计，下一轮继续。

这样可以避免 “AI timeout 导致机器人不扫盘、不下单”。

## AI 执行决策

AI 现在会输出结构化执行字段：

```json
{
  "action": "buy",
  "entry_mode": "market_now",
  "exit_mode": "hold",
  "size_mode": "normal",
  "stop_mode": "fixed",
  "replace_mode": "none",
  "confidence": 0.72,
  "reason": "中文理由"
}
```

### entry_mode

- `market_now`：立即市价买入。
- `limit_pullback`：挂一个回调限价买单。
- `split_limit`：拆成多笔限价买单。
- `breakout_confirm`：等待突破确认，不立即买。
- `wait`：等待，不买。

### exit_mode

- `hold`：继续持有。
- `sell_all`：全部卖出。
- `sell_partial`：部分卖出。
- `trail_profit`：启动或移动追踪止盈。
- `move_to_breakeven`：止损移动到保本价。

### size_mode

- `explore`：探索小仓。
- `normal`：正常仓。
- `strong`：强信号加仓。
- `reduced`：降低仓位。

### stop_mode

- `fixed`：固定止损。
- `wide`：放宽止损。
- `tight`：收紧止损。
- `breakeven`：保本止损。
- `trailing`：追踪止损。

### replace_mode

- `none`：不换仓。
- `replace_weakest`：满仓时允许卖出最弱持仓，换入更强候选。
- `free_cash_only`：只用空闲资金，不卖已有仓位。

## 复杂订单能力

```env
AI_EXECUTION_DECISIONS_ENABLED=true
LIMIT_ORDER_ENABLED=true
SPLIT_ORDER_PARTS=3
PARTIAL_SELL_FRACTIONS=0.3,0.5,1.0
REPLACE_WEAK_POSITION_ENABLED=true
MARKET_REGIME_ENABLED=true
```

已支持：

- 市价买入。
- 市价卖出。
- 限价买入。
- 限价卖出。
- 分批限价买入。
- 部分卖出。
- 满仓时 AI 建议换弱仓。
- 追踪止盈状态更新。
- 保本止损状态更新。
- 撤单接口。
- 查询挂单接口。

注意：复杂订单执行失败时，机器人会记录经验并继续下一轮，不会停机。

## 行情状态判断

每轮会让 AI 生成行情状态：

- 单边上涨
- 震荡
- 急跌反弹
- 高波动插针
- 主流吸血
- 山寨轮动

这个状态会进入买入、卖出、仓位大小、失败归因、经验总结的上下文。

## 失败归因与经验系统

AI 归因分类固定为：

- 追高
- 假突破
- 流动性不足
- 新闻误判
- 止损太紧
- 止盈太早
- 入场太晚
- 执行失败
- 未知

会记录的经验包括：

- 盈利交易
- 亏损交易
- 卖出复盘
- 止损失败
- 挂单失败
- 撤单失败
- AI 误判
- 执行异常

经验的作用是影响相似场景评分和 AI prompt，不会永久禁止某个币。

## 后台训练池

```env
AI_TRAINING_ENABLED=true
AI_TRAINING_WORKERS=4
AI_WEEKLY_TOKEN_TARGET=1000000000
```

训练任务包括：

- 市场复盘
- 候选币买入判断
- 持仓卖出判断
- 未买入反事实
- 已卖出复盘
- 全市场影子策略比较

训练统计包括：

- 本周目标 token
- 成功返回 token
- 估算尝试 token
- 完成率
- 训练任务数
- 成功数
- 失败数
- 活跃线程数
- 队列长度
- 丢弃任务数

timeout 没有 usage 时，程序会按 prompt 字数估算尝试 token，避免训练看起来是 0。

## Telegram 菜单

默认配置：

```env
TELEGRAM_MONEY_ONLY=true
TELEGRAM_CONTROLS_ENABLED=true
TELEGRAM_AUTO_REPORTS=false
MONEY_REPORT_INTERVAL_SCANS=1
```

`TELEGRAM_AUTO_REPORTS=false` 表示不每轮自动刷资金报表。需要时点菜单或发命令查看。

### 命令

- `/status`：资产、持仓、盈亏、最近订单。
- `/ai`：AI 配置、MiMo 自检、调用成功率、timeout、token、最近错误。
- `/positions`：当前持仓、成本、浮盈亏、最近卖出意见。
- `/training`：本周 token 目标、已消耗、完成率、训练任务、经验样本。
- `/health`：线程状态、DB 状态、Telegram 状态、OKX/MiMo 配置状态。
- `/errors`：最近 timeout、HTTP 错误、下单失败、数据库锁等。
- `/shadow`：影子全市场最近建议。
- `/execution`：最近 AI 执行决策、订单方式、挂单/撤单/换仓。
- `/lessons`：最近失败归因和盈利归因。
- `/market`：当前 AI 判断的行情状态。
- `/stop`：暂停机器人。
- `/start`：恢复机器人。
- `/reset`：清除暂停状态。

## 数据库存了什么

主要表包括：

- `candles`：K 线。
- `signals`：规则策略信号。
- `orders`：订单记录。
- `positions`：持仓状态。
- `bot_state`：机器人运行状态。
- `intelligence_items`：新闻、公告、技能广场等信息项。
- `info_signals`：信息动量信号。
- `ai_decisions`：AI 买卖粗决策。
- `execution_decisions`：AI 执行决策。
- `ai_call_audits`：AI 调用审计。
- `ai_training_runs`：训练 token 统计。
- `trade_reviews`：交易复盘。
- `trade_attributions`：交易归因。
- `market_regimes`：行情状态。
- `shadow_decisions`：影子全市场决策。
- `bot_errors`：运行错误。
- `config_snapshots`：启动配置快照。

## OKX 技能广场接入

配置：

```env
OKX_SKILL_SIGNAL_URLS=https://example.com/skill-output-1.json,https://example.com/skill-output-2.json
```

当前接入方式是只读：

- 技能输出会进入信息信号。
- 技能结果可影响候选评分和 AI prompt。
- 技能不能直接下单。
- 所有订单仍走本项目本地风控、仓位、订单记录和 OKX API。

## 影子全市场

真实执行范围：

- OKX Demo 现货。

影子训练范围：

- 现货策略比较。
- 杠杆策略判断。
- 合约策略判断。
- 期权策略判断。
- 网格策略判断。
- 追踪止损策略判断。
- 止盈止损组合判断。

影子决策只写数据库，不调用真实 OKX 非现货下单接口。

## 常用命令

初始化数据库：

```bash
python -m okx_quant_bot init-db
```

检查配置：

```bash
python -m okx_quant_bot doctor --no-network
python -m okx_quant_bot doctor
```

运行 AI 动量机器人：

```bash
python -m okx_quant_bot run-momentum
```

只扫描一轮：

```bash
python -m okx_quant_bot scan-momentum
```

抓取 K 线：

```bash
python -m okx_quant_bot fetch-candles --limit 300
```

CSV 回测：

```bash
python -m okx_quant_bot backtest --csv path/to/candles.csv
```

运行测试：

```bash
python -m unittest discover -s tests
```

## CSV 回测格式

```csv
symbol,ts,open,high,low,close,volume
BTC-USDT,1714521600000,60000,60500,59800,60200,123.4
```

## Docker

构建并启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f
```

当前 Docker 示例建议运行 AI 动量入口：

```yaml
command: ["python", "-m", "okx_quant_bot", "run-momentum"]
```

## systemd

示例服务文件在：

```text
systemd/okx-quant-bot.service
```

推荐 `ExecStart`：

```ini
ExecStart=/usr/bin/python3 -m okx_quant_bot run-momentum
```

安装示例：

```bash
sudo useradd -r -s /usr/sbin/nologin okxbot
sudo mkdir -p /opt/okx-quant-bot
sudo chown -R okxbot:okxbot /opt/okx-quant-bot
sudo cp systemd/okx-quant-bot.service /etc/systemd/system/okx-quant-bot.service
sudo systemctl daemon-reload
sudo systemctl enable okx-quant-bot
sudo systemctl start okx-quant-bot
sudo journalctl -u okx-quant-bot -f
```

## 服务器更新流程

服务器上拉最新代码：

```bash
cd /opt/okx-quant-bot
git pull origin main
python -m pip install -e .
python -m okx_quant_bot doctor --no-network
sudo systemctl restart okx-quant-bot
sudo journalctl -u okx-quant-bot -f
```

如果用 Docker：

```bash
cd /opt/okx-quant-bot
git pull origin main
docker compose up -d --build
docker compose logs -f
```

## 测试状态

当前测试覆盖：

- 配置读取和校验。
- dotenv 变量展开。
- MiMo Chat Completions 请求体。
- AI usage/token 统计。
- AI timeout 重试。
- AI 执行决策解析和非法值回退。
- SQLite WAL、并发写入、训练统计。
- 动量候选和满仓逻辑。
- 市价买入兼容。
- 限价回调买入。
- 分批限价买入。
- 部分卖出。
- 止损失败记录。
- Telegram 手动菜单。
- OKX 限价、撤单、挂单查询接口。
- 中文输出乱码回归。



