# 通路测试：加密现货影子盘（第一期）

订单只进本机沙箱。行情用交易所官方公共 JSON，不配置交易 API 密钥，不注册交易所执行客户端。没有 A 股、期货、永续，也没有实盘下单。

这一期用来打通链路。报告标题是「通路测试」。默认名义金额 12 USDT，上限 50 USDT，远小于盘口。

## 依赖

- Python 3.12
- `nautilus_trader==1.231.0`
- `pandas`、`pyarrow`
- 可选：`quantstats==0.0.86`。装不上时仍会写日收益摘要，HTML 里的夏普、回撤和月度表就缺着。

```bash
python3 -m pip install -r requirements.txt
```

在仓库根目录运行。程序会从当前目录或源码位置找到 `config/paper.json`。

```bash
PYTHONPATH=src python3 -m paper_spot check
PYTHONPATH=src python3 -m paper_spot run --seconds 120
PYTHONPATH=src python3 -m paper_spot report runs/<目录>
PYTHONPATH=src python3 -m paper_spot replay runs/<目录>
```

连续覆盖加密市场 24 小时时，把秒数设成 86400。进程中断后用同一个目录恢复，现金不会被重置成 10000：

```bash
PYTHONPATH=src python3 -m paper_spot run --seconds 86400 --run-dir runs/<目录> --resume
```

## 这次实际用的行情

程序按这个顺序探测官方接入点，只选一个现货交易对：

1. Binance 现货默认域名，交易对 `BTCUSDT`
2. Binance 官方行情域名 `data-api.binance.vision` 和 `data-stream.binance.vision`，仍是 `BTCUSDT`，仍用 Binance 官方适配器
3. Bybit 官方现货，交易对 `BTCUSDT`（品种号 `BTCUSDT-SPOT.BYBIT`）
4. OKX 官方现货，交易对 `BTC-USDT`（品种号 `BTC-USDT.OKX`）

探测结果写在该次运行的 `config_snapshot.json`。默认域名返回 451 时，第二项是 Binance 公布的行情专用接入点，不是代理，也不是别的行情源。三家官方接入点都不可达时，程序直接停止。

## 配置

`config/paper.json`：

| 项 | 默认 | 作用 |
| --- | --- | --- |
| `starting_balance` | `10000 USDT` | 沙箱现金账户 |
| `latency_ms` | 100 | 下单、改单、撤单延迟 |
| `book_depth` / `book_update_speed_ms` | 20 / 100 | 有限深度 |
| `order_quote_notional` | 12 USDT | 连通性买单的名义金额 |
| `max_notional_per_order` | 50 USDT | 单笔上限 |
| `imbalance_multiple` | 1.2 | 买一量至少是卖一量的倍数才买；反向才卖 |
| `protection_band` | 0.005 | 限价相对决策中间价的 0.5% |
| `session_seconds` | 120 | 到点停止。可用 `--seconds` 覆盖 |
| `gap_threshold_seconds` | 2 | 行情到达比交易所时间晚过这个值，不拿去撮合 |

命令行可以覆盖会话时长和不平衡倍数。`--imbalance` 会写进该次配置哈希。恢复时不能改这个倍数。

费率不写在策略里，而在 `config/fees/`：

- Binance 现货 Regular User，不用 BNB 抵扣：挂单 0.100%，吃单 0.100%。版本 `binance-spot-regular-2026-11-01`，检索日 2026-11-01，来源是币安公开费率页的 Maker/Taker 列。
- 若探测改走 Bybit：VIP 0 现货加密对加密，0.1000% / 0.1000%，版本 `bybit-spot-vip0-2026-11-01`。
- 若改走 OKX：Regular 现货 0.0800% / 0.1000%，版本 `okx-spot-regular-2026-11-01`。

公开品种接口不带账户费率。程序把上面的正费率盖到现货品种上。费率缺失或不是正数时不开新单。

`config/instruments/crypto_spot.json` 只保存已核对过的 BTCUSDT 规格，供启动时对照。下单精度以当次官方 instrument 响应为准。`domestic.json` 必须是空的。

## 撮合

NautilusTrader 1.231 的 `SandboxExecutionClient` 把延迟写成 0，并且关闭内部队列，配置里也没有盘口消耗。本仓库不改 Nautilus 源码，只在子类里重建同一个 `SimulatedExchange`：

- 延迟类名是 `LatencyModel`。文档里后来的 `StaticLatencyModel` 在 1.231 里不存在。下单、改单、撤单都是 100 毫秒，`use_message_queue=True`，否则延迟不会生效。
- `MakerTakerFeeModel`，费率来自品种上盖好的公开费率。
- 订单簿 `L2_MBP`，`trade_execution` 打开，盘口消耗打开，不用 K 线成交。
- 市价或可成交的 IOC 限价只吃保护带里的有限档，剩余取消。

执行引擎的对账关闭。沙箱不会向交易所查询订单。

## 怎么确认没有实盘通道

`check` 会探测行情、建好节点，然后停住，不调用 `run`，不发单。通过时会打印：

- 执行客户端只有 `PaperSandboxExecutionClient`
- 执行工厂类名里带 `Sandbox`
- 行情配置的 `api_key` 为空，没有代理

下面任何一条都会拒绝启动：

- 执行配置不是沙箱，或者建成的客户端不是上面这个子类
- 进程环境里有 Binance、Bybit、OKX 的交易密钥变量。报错只给出变量名
- 行情配置写了密钥、口令或代理
- HTTP/WebSocket 主机不在程序里的官方域名表内

代码里没有注册 `BinanceLiveExecClientFactory`、`BybitLiveExecClientFactory`、`OKXLiveExecClientFactory`。

## 输出

每次运行在 `runs/<UTC 时间>/`：

- `catalog/`：逐笔、最优买卖价、有限深度的 Parquet
- `stream/`：引擎自己的 feather 流
- `ledger.json`：影子成交和余额。同一笔 `trade_id` 再入账会拒绝
- `gaps.json`：断线窗口。到达时间比交易所时间晚 2 秒以上的行情也不进撮合
- `config_snapshot.json`：配置哈希、费率版本、延迟、场所
- `report/`：抬头、成交簿（csv 和 parquet）、数据质量、日收益摘要。日收益至少两个点时另有 `quantstats.html`

短会话通常只有不到两个日收益点，这时没有 HTML，`quantstats_note.txt` 会说明缺口。日收益按 365 个加密交易日，不按 252。

过期行情和断线窗口里的事件仍会落盘。重放和沙箱按同一条规则丢掉这些事件：断线前已经发出的旧单，不能用断线期间补出来的价格成交。

## 第 7.4 节里现在能看的和还要跑满的

一次两分钟通路测试不能代替 24 小时验收。

- 配置哈希、费率版本、延迟参数写在报告抬头里。
- 启动自检拒绝交易所执行客户端。
- 同一段行情重放两次，价和量的差异应为 0，并且要和该次会话的影子账本一致。币安 REST 初始快照没有 `F_LAST`，按这个标志粘包会把第一包推迟到下一笔行情的到达时间，第一笔 IOC 落空，最后一笔卖出就对不上。回放按接收时间还原订单簿包。
- 参与率、费用和到达成本要等这次会话真的有成交才是数。没有成交时报告里就是空的。
- 连续 24 小时覆盖，以及中断后从 Parquet 和影子账本恢复且不重复记账，要跑满并实际中断一次才算。这次不启动 24 小时运行。
- 人为断开后再连上，旧单不能用断线期间补出来的价格成交。过期行情不进撮合的测试还在；断线补价不能成交旧单另有回归测试。

离线测试：

```bash
PYTHONPATH=src python3 -m unittest tests.test_phase1
```
