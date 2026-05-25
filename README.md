# lite-qmt-executor

> 基于 miniQMT 的 A 股量化交易执行器，自带默认策略，开箱即用，灵活定制。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 📌 项目定位

1. **核心定位**：本项目是一个纯粹的**“交易执行引擎” (Execution Engine)**，**不提供任何选股功能、不含选股公式、不带回测功能，也不产生交易信号**。
2. **快速实盘**：帮助策略开发者快速接入 miniQMT 实盘交易，解决最后一公里。
3. **适用群体**：量化交易爱好者、策略开发及测试人员。

---

## 它能干什么

- **信号接入**：HTTP API + WebSocket 双通道，断线自动重连 + 心跳保活
- **队列调度**：消费者池并行处理新信号，ticker 周期推进存量任务
- **QMT 适配**：封装 xtquant 的下单/撤单/查询/行情，统一异常处理 + 自动重连
- **灾备自愈**：基于 WAL 日志，重启自动撤单并重建状态，防重买超买
- **通知抽象**：Notifier 接口，钉钉/企微/飞书/邮件自己实现一个 `push()` 就接上
- **策略插件化**：继承 `BuyStrategy` / `SellStrategy`，开发定制策略
- **开箱即用**：自带一套功能完整的默认策略，改 config 就能跑实盘

## 默认策略

| 策略 | 文件 | 说明 |
|------|------|------|
| 分档累积买入 | `default/accumulate_buy.py` | 按涨幅分流：抢买/首笔保底+回撤加仓/等回落，带撤单重挂 |
| 高开止盈 | `default/open_high_sell.py` | 9:30:02 并发池抢卖高开票 |
| 分档盈利止盈 | `default/profit_tier_sell.py` | 涨幅达标分档卖出，实时查持仓防超卖 |

---

## 快速开始

> 💡 没装过 miniQMT？先看 **[doc/miniQMT环境搭建与FAQ.md](doc/miniQMT环境搭建与FAQ.md)**，从开户到登录一步步来。

### 环境要求

- Windows（QMT 只支持 Windows）
- Python 3.8+（推荐3.8-3.11，其他版本请自行测试）
- miniQMT 已登录运行
- xtquant（QMT 自带 SDK，无需 pip 安装）

### 安装

```bash
git clone https://github.com/lotey/lite-qmt-executor.git
cd lite-qmt-executor
pip install -r requirements.txt
```

### 配置

编辑 `app/config.py`：

```python
# 必填
QMT_PATH = r'D:\你的券商QMT\userdata_mini'  # 指向 QMT 安装目录下的 userdata_mini 子目录
ACCOUNT_ID = '你的资金账号'  # 您的真实资金账号

# 可选：WebSocket 信号网关（不需要就保持 None）
GATEWAY_HOST = None
GATEWAY_PORT = None
GATEWAY_TOKEN = None
```

### 启动

**方法一：命令行手动拉起**
首先确保 miniQMT 客户端已正常登录，然后执行：
```bash
python main.py
```

**方法二：一键全自动拉起（推荐）**
双击执行项目根目录下的 `qmt-run.bat`。该脚本已实现如下生产级一键启动功能：
1. **原子性防多开检测**：自动检测 `59999` 端口与当前安装路径下的 `XtMiniQmt.exe` 进程，防止重复运行造成冲突。
2. **垃圾清理**：在完全冷启动前，安全清除无用的 QMT 垃圾日志、崩溃转储及 `*__mutex` 互斥锁残留（自动保留登录状态）。
3. **自动拉起与就绪自旋检测**：自动拉起客户端并以最长 60s 自旋检测就绪状态。就绪则立即拉起 Python 执行器并连接，超时未就绪则报错暂停退出。

> ⚠️ **编码注意事项**：
> `qmt-run.bat` 采用 `GBK` 编码，查看或修改该文件时请务必使用 `GBK` 编码，以防中文乱码导致运行报错。

### 测试信号

```bash
curl -X POST http://localhost:30015/api/buy \
     -H "Content-Type: application/json" \
     -d "{\"code\":\"sz000001\"}"
```

---

## 架构

```
              外部信号
     ws_server ──┐    ┌── http_server
                  │    │
                  ▼    ▼
      ┌─────────────────────────────────┐
      │        TradingEngine            │
      │                                 │
      │  ┌────────┐    ┌──────────┐     │
      │  │ Broker │    │ Notifier │     │
      │  └────────┘    └──────────┘     │
      │                                 │
      │  ┌───────────────────────────┐  │
      │  │ BuyEngine（调度骨架）     │  │
      │  │  signal_queue → 消费者池  │  │
      │  │  ticker 周期扫存量        │  │
      │  └──────────┬────────────────┘  │
      │             ▼                   │
      │     BuyStrategy（你写）         │
      │                                 │
      │  ┌───────────────────────────┐  │
      │  │ SellEngine（调度骨架）    │  │
      │  └──────────┬────────────────┘  │
      │             ▼                   │
      │     SellStrategy（你写）        │
      └─────────────────────────────────┘
```

**并发模型**：
- 新信号首评估：**并行**（8 worker 线程池）
- 存量任务推进：**串行**（3 秒一轮 ticker）
- 卖出策略：**各自独立线程**，互不阻塞

---

## 项目结构

```
lite-qmt-executor/
├── main.py                     # 装配入口（改这里注册你的策略）
├── qmt-run.bat                 # 一键启动脚本
├── qmt-stop.bat                # 一键关闭脚本
├── requirements.txt
├── app/
│   ├── config.py               # 底层调度与网络连接配置（本端基础参数在此修改）
│   ├── core/
│   │   ├── broker.py           # xtquant 接口封装
│   │   ├── notifier.py         # 通知器接口 + 默认实现
│   │   └── trading_engine.py   # 交易调度总控
│   ├── engine/
│   │   ├── buy_engine.py       # 买入调度骨架
│   │   └── sell_engine.py      # 卖出调度骨架
│   ├── server/
│   │   ├── http_server.py      # Flask HTTP API
│   │   └── ws_server.py        # WebSocket 客户端
│   └── strategy/
│       ├── buy_strategy.py     # BuyStrategy 抽象类
│       ├── sell_strategy.py    # SellStrategy 抽象类
│       ├── common.py           # 策略公共工具
│       └── default/            # 默认策略实现
│           ├── stg_config.py   # 策略专属个性化配置
│           ├── accumulate_buy.py
│           ├── open_high_sell.py
│           └── profit_tier_sell.py
└── doc/
    ├── miniQMT环境搭建与FAQ.md   # 新手指南和常见问题
    ├── 定制开发指南.md           # ⭐ 二开必读
    └── qmt实盘调试文档.md        # 实盘联调与问题排查
```

---

## HTTP API

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/buy` | 买入信号入队 `{code, strategy}` |
| `GET` | `/api/tasks` | 查当前买入任务状态 |
| `POST` | `/api/sell` | 手动卖出 `{code, volume, price}` |
| `GET` | `/api/positions` | 查持仓 |
| `GET` | `/api/orders` | 查委托 |
| `GET` | `/api/account` | 查账户 |
| `POST` | `/api/cancel/<order_id>` | 撤单 |
| `GET` | `/health` | 健康检查 |

---

## 定制开发

按改动量从小到大：

| 档位 | 做什么 | 难度 |
|------|--------|------|
| 不改 | 默认策略 + 改 config 直接跑 | ⭐ |
| 调参数 | 改 `stg_config.py` 里的阈值/金额/档位 | ⭐ |
| 换通知 | 实现 `Notifier.push()` 接钉钉/企微 | ⭐⭐ |
| 写策略 | 继承 `BuyStrategy`/`SellStrategy` | ⭐⭐⭐ |

详见 **[doc/定制开发指南.md](doc/定制开发指南.md)**，含完整代码模板和常见问题。

---

## WebSocket 信号网关

配置 `GATEWAY_HOST` / `GATEWAY_PORT` / `GATEWAY_TOKEN` 三个都填了才启动 WebSocket 客户端。

重连策略：
- 从未连上过：连续失败 3 次放弃（配置错误早发现）
- 曾经连上过：永不放弃，持续重连（网络抖动自动恢复）

不配置 WebSocket 也能用——HTTP API 始终可用，curl 发信号一样跑。

---

## 注意事项

- **纯新手必看**：第一次接触 miniQMT？先看 **[doc/miniQMT环境搭建与FAQ.md](doc/miniQMT环境搭建与FAQ.md)**，从零开始搭环境。老司机跳过。
- **买入默认关闭**：`BUY_ENABLED = False`，调试确认无误后改为 `True` 再接实盘信号
- xtquant 不要 pip 安装。用 QMT 客户端自带的 SDK，broker.py 会自动加载
- 程序启动时若 QMT 未就绪，会自动以 2 秒为间隔进行自旋重试（最多 10 次），直至连接成功或超时
- 非交易时段（9:00 前 / 15:00 后）信号自动丢弃，策略不会空转
- 单实例锁：同一台机器不能重复启动，第二个进程会直接退出

## 容灾与 WAL 持久化机制

为了应对实盘运行中可能出现的进程异常退出、系统重启等突发情况，执行器引入了 WAL（Write-Ahead Log）持久化与灾备恢复机制：

- **增量 WAL 日志**：所有的买入信号和阶段状态变更（如进入 `ACCUMULATING` 或标记为 `DONE`）会在第一时间以 `.jsonl` 格式追加写入 `data/` 目录下的每日 WAL 日志（例如 `buy_signals_YYYYMMDD.jsonl`）。
- **启动恢复与状态重建**：
  - 引擎启动时读取当日 WAL。超时（超过 `BUY_RECOVERY_MAX_AGE_MINS`，默认 10min）的信号直接作废，防止误买。
  - 接管前自动向柜台撤销当日所有未成交买单，避免重复买入。
  - 调用策略 `on_recover` 钩子，从柜台成交历史中对账并重建状态，实现无缝接管。
- **过期日志自动清理**：系统在每次启动时，会自动清理 `data/` 目录下历史超过 7 天的旧 WAL 日志文件，避免占用磁盘空间。

---

## 🛠️ 关于贡献与 PR

本项目核心功能已基本稳定。为保持代码精简，**目前不接受任何 Pull Request (PR)**：
- **定制修改**：如有功能定制或 Bug 修复需求，请自行 Fork 并在您的个人分支中修改使用，上游主仓库不进行代码合并。
- **交流探讨**：欢迎在 Issues 中反馈问题或进行技术交流。

---

## ⚠️ 免责声明

本项目仅供学习和技术研究使用，**不构成任何投资建议**。

- 股票交易存在风险，使用本软件进行实盘交易造成的任何损失，由使用者自行承担
- 作者不对代码的正确性、稳定性、实时性做任何保证
- 使用前请充分了解 A 股交易规则、QMT 接口特性，并在小资金下充分测试
- 本项目与任何券商、交易所无关，不提供任何金融服务
- 请遵守所在地区的法律法规，合规使用

**使用本软件即表示你已理解并接受以上风险。**

---

## 🌟 支持项目

若本项目对您有所帮助，欢迎点个 **Star** 🌟，这是对作者最大的支持！

## License

[MIT](LICENSE)
