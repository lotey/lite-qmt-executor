#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
默认策略专属个性化配置文件
"""

from datetime import time

# ==================== 信号类型常量 ====================
SIGNAL_TYPE_ACTIVE = "ACTIVE"      # 常规信号（买入）
SIGNAL_TYPE_HOLDGRD = "HOLDGRD"    # 竞价止损核按钮（卖出）

# ==================== 统一信号路由映射表 ====================
SIGNAL_ROUTE_MAP = {
    SIGNAL_TYPE_ACTIVE: "AccumulateBuyStrategy",
    SIGNAL_TYPE_HOLDGRD: "OpeningSellStrategy",
}

class StgConfig:
    # ----- 分档累积买入策略 (AccumulateBuyStrategy) -----

    # 一波流涨幅阈值（≤ 此值积极抢买）
    BUY_ONESHOT_THRESHOLD = 2.5
    # 一波流追买上限（超过此值退回佛系挂单）
    BUY_ONESHOT_CHASE_LIMIT = 3.0
    # 一波流单笔金额
    BUY_ONESHOT_AMOUNT = 3000
    # 一波流最低足额成交比例
    BUY_ONESHOT_FILL_RATIO = 0.9
    # 累积模式涨幅阈值（≤ 此值首笔买入+累积；> 此值等回落）
    BUY_ACCUM_THRESHOLD = 4.0
    # 回落买入涨幅（涨幅 > 4.0% 时，等回落到此值以内才开始买入）
    BUY_FALLBACK_CHG = 3.5
    # 累积模式档位表：(模拟均价上界, 单笔金额, 总额上限)
    BUY_ACCUM_TIERS = [
        (2.0, 1000, 3000),
        (3.0, 1000, 2000),
        (4.0, 1000, 1000),
    ]
    # 最大买入次数
    BUY_MAX_COUNT = 3
    # 加仓回撤比例（当前价 ≤ 上次买价 × 此比例 才加仓）
    BUY_PULLBACK_RATIO = 0.98
    # 扫单溢价比例（目标挂单价 = 现价 × (1 + 此比例(0.2%))）
    BUY_PRICE_PREMIUM_RATIO = 0.002

    # 骗炮拦截比例（当前价 ≤ 开盘价 × 此比例 时，判定为骗炮并停止后续加仓，0.995 代表跌破开盘价 0.5%）
    BUY_FAKE_BREAKOUT_RATIO = 0.995
    # 柜台订单同步超时时间（秒，发单后超过此时间仍未出现在委托列表中则判定为丢单并自愈重试）
    BUY_SYNC_TIMEOUT = 10.0
    # 允许连续丢单的最大次数（仅对非一波流大单生效，超过此次数触发熔断）
    BUY_MAX_DROPS = 1

    # ----- 卖出通用 -----
    # 折价比例（当前价 × (1-此值) 挂单，避开 2% 笼子边界，留 0.1% 安全垫）
    SELL_DISCOUNT = 0.019
    # 卖出委托单柜台同步超时时间（秒）
    SELL_SYNC_TIMEOUT = 10.0

    # ----- 高开止盈 (OpenHighSellStrategy) -----
    OPEN_HIGH_CHG = 3.0                 # 高开阈值（%）
    OPEN_HIGH_SELL_RATIO = 0.5          # 卖出比例
    OPEN_HIGH_PREPARE_TIME = time(9, 29)
    OPEN_HIGH_EXECUTE_TIME = time(9, 30, 2)

    # ----- 分档盈利止盈 (ProfitTierSellStrategy) -----
    SELL_POLL_INTERVAL = 3
    SELL_PROFIT_START_TIME = time(9, 30, 30)
    # 分档止盈：(触发涨幅%, 卖出比例)；最后一档自动卖完剩余
    SELL_PROFIT_TIERS = [
        (3.0, 0.5),
        (5.0, 0.5),
        (8.0, 1.0),
    ]
