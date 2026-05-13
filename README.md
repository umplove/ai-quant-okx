# OKX Demo Quant Bot V1

这是一个面向新手的 OKX/欧易模拟交易量化机器人骨架。它默认只运行 Demo Trading，只交易 `BTC-USDT` 和 `ETH-USDT` 现货，不使用杠杆，不承诺盈利。

## 功能

- OKX V5 REST 客户端：K 线、余额、合约/币对信息、下单、查单。
- 本地 SQLite 存储：K 线、信号、订单、持仓、风控状态。
- 策略：EMA200 趋势过滤 + EMA20 回站 + RSI 低位回升。
- 风控：单笔仓位、单币仓位、止损、止盈、移动止损、每日最大亏损、连续亏损暂停。
- 回测：复用实盘策略函数，输出收益、回撤、胜率、盈亏比。
- 常驻 runner：每分钟检查，每小时生成信号，默认不发送订单。
- 部署文件：Docker Compose 和 systemd 示例。

## 快速开始

```powershell
Copy-Item .env.example .env
python -m okx_quant_bot init-db
python -m okx_quant_bot doctor
python -m okx_quant_bot backtest --csv path\to\candles.csv
python -m unittest discover -s tests
```

CSV 回测文件需要包含这些列：

```csv
symbol,ts,open,high,low,close,volume
BTC-USDT,1714521600000,60000,60500,59800,60200,123.4
```

## 连接 OKX Demo Trading

1. 在 OKX App 或网站进入 Demo Trading。
2. 创建 Demo Trading API Key。
3. 只授予 `Read` 和 `Trade` 权限，禁止 `Withdraw`。
4. 绑定云服务器固定 IP。
5. 将 key 写入 `.env`。

默认 `TRADING_ENABLED=false`，机器人只会记录将要执行的动作。确认回测和 Demo 环境正常后，才把它改成 `true`。

```powershell
python -m okx_quant_bot doctor
python -m okx_quant_bot run
```

## 重要安全边界

- V1 默认拒绝实盘：`OKX_DEMO=true`。
- 若要实盘，必须同时设置 `OKX_DEMO=false` 和 `ALLOW_LIVE_TRADING=true`，并自行承担风险。
- 订单失败不会追单，机器人会记录异常并暂停对应交易动作。
- 本项目不是投资建议，只是一个可验证、可复盘、可风控的自动交易流程。
