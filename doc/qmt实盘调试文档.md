# QMT实盘调试文档

## 调试目标

逻辑层面看代码就知道对错，**不需要测**。
要测的只有**QMT接口的真实行为**——返回值、字段名、状态字符串、超时表现。

不同券商版本可能有差异，必须实测才知道。

---

## 调试步骤

### 1. 跑测试脚本（一次性）

直接执行 `test/qmt_test.py`：
```bash
python test/qmt_test.py
```

脚本会：
- 登录QMT
- 打印 asset/position/order 各对象的所有属性
- 打印行情字段
- 真实下一单（约1000元成本）
- 撤单测试（看 status_msg 在撤单后的变化）
- 错误下单测试（看异常或返回值）

**总成本几十元手续费**。

### 2. 根据输出对照修正 broker

测试脚本输出的真实字段名/状态字符串如果跟 `broker.py` 不一致，回头改：

**重点对照清单**：
- `asset.cash` 字段名是不是这个
- `position.yesterday_volume` 字段名是不是这个
- `position.can_use_volume` 是否准确反映可卖量
- `order.order_status` 各状态的数字码（见下方状态码表）
- `xtdata.get_full_tick` 字段名：lastPrice / lastClose / open
- `order_stock` 失败时返回什么（负数 / 0 / 异常）
- `cancel_order_stock` 撤单后多久 status 能变成已撤

### 3. 接入实盘

修正 broker 之后，直接接信号跑小金额（1~2万）观察几天。

---

## xtquant 接口参考

### 1. 初始化和登录

```python
import sys, os, time
sys.path.append(os.path.join(os.path.dirname(QMT_PATH), 'bin.x64', 'Lib', 'site-packages'))

from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount
from xtquant import xtconstant
from xtquant import xtdata

session_id = int(time.time())
trader = XtQuantTrader(QMT_PATH, session_id)
trader.start()

connect_result = trader.connect()           # 返回 0=成功，非0=失败
account = StockAccount(ACCOUNT_ID)
sub = trader.subscribe(account)             # 返回 0=成功，非0=失败
```

**关键点**：
- `QMT_PATH` 指向 `userdata_mini` 目录
- xtquant SDK 必须用 QMT 客户端自带的（`bin.x64/Lib/site-packages/xtquant`），pip 安装的版本协议会错配
- `connect()` 失败说明 miniQMT 客户端没启动或没登录

---

### 2. 账户资产 query_stock_asset

```python
asset = trader.query_stock_asset(account)
```

返回 `XtAsset` 对象，关键字段：

| 字段名 | 类型 | 含义 |
|---|---|---|
| `account_id` | str | 资金账号 |
| `total_asset` | float | 总资产 |
| `market_value` | float | 持仓市值 |
| `cash` | float | 可用资金 |
| `frozen_cash` | float | 冻结资金 |

---

### 3. 行情 xtdata.get_full_tick

```python
tick = xtdata.get_full_tick([stock_code])  # 必须传列表
data = tick[stock_code]                    # 取出来是 dict
```

返回 dict，关键字段：

| key | 类型 | 含义 |
|---|---|---|
| `lastPrice` | float | 最新成交价 |
| `lastClose` | float | 昨收 |
| `open` | float | 今日开盘（9:25 集合竞价后才有）|
| `high` / `low` | float | 今日最高/最低 |
| `volume` | int | 成交量（手）|
| `amount` | int | 成交金额（元）|
| `askPrice` | list[float] | 卖一~卖五价 |
| `bidPrice` | list[float] | 买一~买五价 |
| `askVol` | list[int] | 卖一~卖五量（手）|
| `bidVol` | list[int] | 买一~买五量（手）|
| `time` | int | 行情时间（毫秒）|
| `timetag` | str | 行情时间字符串 |
| `stockStatus` | int | 状态码（3=正常交易）|

**注意**：
- 9:15~9:25 集合竞价时段 `open` 字段可能为 0
- 停牌票拿不到 tick，返回 None 或空 dict

---

### 4. 下单 order_stock

```python
order_id = trader.order_stock(
    account=account,
    stock_code='000001.SZ',                # 必须 .SZ/.SH 后缀（大写）
    order_type=xtconstant.STOCK_BUY,       # 23=买入, 24=卖出
    order_volume=100,                      # 必须 100 整数倍（A股最小单位 1 手）
    price_type=xtconstant.FIX_PRICE,       # 50=限价单
    price=10.50,
)
```

**返回值**（int）：
- `> 0` → 委托成功，返回值就是 `order_id`
- `<= 0` → 调用失败

**⚠️ 重要警告**：
- 即使参数错误（volume=0、volume=99、不存在的代码），`order_stock` **依然可能返回正数 order_id**
- 真实状态需要事后查 `order_status` 字段（57=废单）
- 不要光看返回值就以为下单成功

---

### 5. 撤单 cancel_order_stock

```python
result = trader.cancel_order_stock(account, order_id)
```

**返回值**（int）：
- `0` → 撤单调用成功
- `-1` → 撤单失败（order_id 不存在 / 单子已成交无可撤）

**注意**：返回 0 仅表示"接口调用成功"，不代表"单子真撤掉了"。真实状态要查 `order_status`。

---

### 6. 委托查询 query_stock_orders

```python
orders = trader.query_stock_orders(account)  # 返回 list[XtOrder]
```

`XtOrder` 关键字段：

| 字段名 | 类型 | 含义 |
|---|---|---|
| `order_id` | int | 委托号（本地）|
| `order_sysid` | str | 柜台委托号 |
| `stock_code` | str | 股票代码 |
| `order_type` | int | 23=买 / 24=卖 |
| `order_status` | int | 状态码（核心）|
| `status_msg` | str | 状态描述（实测大多为空串）|
| `price` | float | 委托价 |
| `order_volume` | int | 委托量 |
| `traded_volume` | int | 已成交量 |
| `traded_price` | float | 成交均价 |
| `order_time` | int | 委托时间（秒级时间戳）|

### order_status 状态码对照（核心）

| 码值 | 含义 | 标准化语义 |
|---|---|---|
| 48 | 未报 | SUBMITTED |
| 49 | 待报 | SUBMITTED |
| 50 | 已报 | SUBMITTED |
| 51 | 已报待撤 | SUBMITTED |
| 52 | 部成待撤 | SUBMITTED |
| 53 | 部撤 | CANCELED（traded_volume>0 时实际是部分成交）|
| 54 | 已撤 | CANCELED |
| 55 | 部成 | PARTIAL_FILLED |
| 56 | 已成 | FILLED |
| 57 | 废单 | REJECTED |

**⚠️ 重要**：
- **不要**用 `status_msg` 中文字符串判断状态，实测此字段几乎总是空串
- **必须**用 `order_status` 数字状态码

### order_time 时间戳

实测是**秒级**时间戳：
```python
from datetime import datetime
order_time = 1779759249
print(datetime.fromtimestamp(order_time))   # 正确：2026-xx-xx xx:xx:xx
print(datetime.fromtimestamp(order_time/1000))  # 错误：1970-xxx
```

---

### 7. 持仓查询 query_stock_positions

```python
positions = trader.query_stock_positions(account)  # 返回 list[XtPosition]
```

`XtPosition` 关键字段：

| 字段名 | 类型 | 含义 |
|---|---|---|
| `stock_code` | str | 股票代码 |
| `volume` | int | 持仓总量 |
| `can_use_volume` | int | 可用数量（T+1 解锁后）|
| `frozen_volume` | int | 冻结数量 |
| `yesterday_volume` | int | 昨日持仓（T-1 已有）|
| `on_road_volume` | int | 在途数量（成交未交收）|
| `open_price` | float | 持仓成本价 |
| `market_value` | float | 持仓市值 |

**T+1 规则**：
- `today_volume = volume - yesterday_volume`（今日新买入量）
- 今天买的 → `can_use_volume=0`（不能卖）
- 隔天 → `can_use_volume` 恢复（可卖）
- 全部卖完后该 code 不在持仓列表里（不会保留 0 持仓占位）

---

### 8. 成交查询 query_stock_trades（慎用）

```python
trades = trader.query_stock_trades(account)
```

**⚠️ 实测警告**：某些券商版本调用此接口会**长时间阻塞**，建议改用 `query_stock_orders` 配合 `traded_volume` / `traded_price` 替代。

---

### 9. 关闭连接

```python
trader.stop()    # 释放本地资源
```

---

## 实战 Tips

### 判断订单状态
```python
def is_filled(order):
    return order.order_status == 56
def is_partial(order):
    return order.order_status == 55
def is_canceled(order):
    return order.order_status in (53, 54)
def is_rejected(order):
    return order.order_status == 57
```

### 撤单后等股份解冻
```python
broker.cancel(order_id)
for _ in range(10):
    time.sleep(0.1)
    o = find_order(order_id)
    if o.order_status in (53, 54, 56):
        break
```

### 挂单价计算（A股笼子内）
```python
# 买单想立刻成交：略高于卖一
buy_grab_price = round(ask1 + 0.01, 2)

# 卖单想立刻成交：略低于买一
sell_grab_price = round(bid1 - 0.01, 2)

# 卖单折价挂单（避开笼子边界）
sell_discount_price = round(last_price * 0.981, 2)
```

### 防御性判断成交
```python
# 不要用 status_msg 判断！实测大多为空串
# ❌ if '已成' in order.status_msg:
# ✅
if order.order_status == 56 or (order.order_status in (53, 55) and order.traded_volume > 0):
    print("成交了")
```

---

## 完整下单+确认成交示例

```python
# 1. 下单
order_id = trader.order_stock(
    account=account,
    stock_code='000001.SZ',
    order_type=xtconstant.STOCK_BUY,
    order_volume=100,
    price_type=xtconstant.FIX_PRICE,
    price=10.50,
)
if order_id <= 0:
    print(f"下单调用失败: {order_id}")
    return

# 2. 等撮合（A股一般 < 500ms）
time.sleep(1.0)

# 3. 查状态
def find_order(oid):
    for o in trader.query_stock_orders(account):
        if o.order_id == oid:
            return o
    return None

o = find_order(order_id)
if o is None:
    print("委托查不到")
    return

# 4. 判断结果
if o.order_status == 56:
    print(f"成交：{o.traded_volume}股 @{o.traded_price}")
elif o.order_status == 57:
    print(f"废单（参数错误）")
elif o.order_status == 50:
    print(f"还在挂单中，等等再查或撤单")
    trader.cancel_order_stock(account, order_id)
elif o.order_status == 55:
    print(f"部分成交：{o.traded_volume}/{o.order_volume}")
```

---

## xtconstant 常量速查

| 常量 | 数值 | 含义 |
|---|---|---|
| `STOCK_BUY` | 23 | 买入 |
| `STOCK_SELL` | 24 | 卖出 |
| `FIX_PRICE` | 50 | 限价单 |

---

## 异常应急

**程序卡住**：Ctrl+C，手动登录QMT撤未成交单
**误下大单**：立刻在QMT客户端撤单，程序停掉再修
**资金被锁**：撤单后等几分钟再操作

---

## 测试执行时机

- **盘中**（10:30~11:00 流动性好）：跑买入/撤单/订单状态测试
- **盘前**（9:25之前）：跑账户/持仓/行情字段测试
- **盘后**：跑账户/持仓查询测试

不需要分多天，**一次盘中跑就能覆盖所有接口行为**。
