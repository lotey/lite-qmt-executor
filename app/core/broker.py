#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xtquant 接口封装

封装 xtquant 的交易和行情接口，对策略层暴露 broker.buy/sell/cancel/get_*。
不抽象多券商接口（QMT 几乎是唯一选择），直接命名 QmtBroker。
"""

import sys
import os
import time
import logging
from typing import Optional, List
from dataclasses import dataclass
from datetime import datetime
from functools import wraps

from app.config import Config
from app.strategy.default.stg_config import StgConfig

# 用 QMT 自带的 xtquant SDK，避免 pip 版本错配
# 注意：必须 append 到末尾，不能 insert(0)。否则 QMT site-packages 里的老版 werkzeug
# 会覆盖系统的，导致 flask 启动失败（ImportError: cannot import name 'Container'）
sys.path.append(os.path.join(os.path.dirname(Config.QMT_PATH), 'bin.x64', 'Lib', 'site-packages'))

from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount
from xtquant import xtconstant
from xtquant import xtdata

logger = logging.getLogger(__name__)


# ==================== 异常 ====================

class BrokerError(Exception):
    """券商相关错误（连接、登录、下单异常等）"""
    pass


def _safe_call(default):
    """
    装饰器：统一处理 QMT 调用的连接保活 + 异常兜底。
    - 调用前确保已连接，断开会尝试一次重连
    - 重连失败 → 打日志 + 退出进程（让外部进程管理工具重启）
    - 调用过程异常 → 打日志，标记断开，返回 default
    """
    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            if not self.is_connected:
                logger.warning("QMT连接断开，尝试重连...")
                try:
                    self._login()
                except BrokerError as e:
                    logger.error(f"QMT重连失败: {e}，进程退出")
                    sys.exit(1)
            try:
                return func(self, *args, **kwargs)
            except Exception as e:
                logger.error(f"{func.__name__} 失败: {e}")
                self.is_connected = False
                return default() if callable(default) else default
        return wrapper
    return decorator


# ==================== 数据结构 ====================

@dataclass
class OrderResult:
    """订单结果"""
    success: bool
    order_id: Optional[int] = None
    message: str = ""
    error_code: Optional[str] = None

@dataclass
class OrderInfo:
    """委托订单信息"""
    stock_code: str
    order_volume: int
    price: float
    order_id: int
    order_status: int      # QMT 数字状态码（50=已报 53/54=已撤 55=部成 56=已成 57=废单）
    status_msg: str
    order_time: str
    order_type: int = 0    # 23=买入(STOCK_BUY) 24=卖出(STOCK_SELL)
    traded_volume: int = 0
    traded_price: float = 0.0

    def __str__(self):
        return f"订单[{self.order_id}]: {self.stock_code} {self.order_volume}股 @{self.price} 状态码:{self.order_status}"

    @property
    def is_buy(self) -> bool:
        """是否为买单"""
        return self.order_type == xtconstant.STOCK_BUY

    def is_filled(self) -> bool:
        """是否已完全成交"""
        return self.order_status == 56

    def is_canceled(self) -> bool:
        """是否已撤单（包括部撤）"""
        return self.order_status in (53, 54)

    def is_partial_filled(self) -> bool:
        """是否部分成交"""
        return self.order_status == 55

    def is_rejected(self) -> bool:
        """是否废单"""
        return self.order_status == 57

    def is_active(self) -> bool:
        """是否仍在活跃状态（未完结）"""
        return self.order_status in (48, 49, 50, 51, 52, 55)

@dataclass
class PositionInfo:
    """持仓信息"""
    stock_code: str
    volume: int
    can_use_volume: int
    frozen_volume: int
    open_price: float
    market_value: float
    on_road_volume: int
    previous_volume: int
    today_volume: int
    is_today_buy: bool

    def __str__(self):
        buy_flag = "[今买]" if self.is_today_buy else "[老仓]"
        return f"持仓: {self.stock_code} {buy_flag} {self.volume}股 市值:{self.market_value:.2f} 成本:{self.open_price:.2f}"

@dataclass
class AccountInfo:
    """账户信息"""
    account_id: str
    total_asset: float
    market_value: float
    cash: float
    frozen_cash: float

    def __str__(self):
        return (
            f"账户[{self.account_id}]: 总资产:{self.total_asset:.2f} "
            f"可用:{self.cash:.2f} 持仓市值:{self.market_value:.2f}"
        )


# ==================== Broker ====================

class QmtBroker:
    """xtquant 接口封装"""

    def __init__(self, qmt_path: str, account_id: str):
        self.qmt_path = qmt_path
        self.account_id = account_id
        self.xt_trader = None
        self.stock_account = None
        self.is_connected = False
        # 启动时：以 2 秒为间隔进行自旋重试最多 10 次（共 20 秒），直至连接成功或超时
        max_retries = 10
        retry_interval = 2
        for i in range(max_retries):
            try:
                self._login()
                return
            except BrokerError as e:
                if i < max_retries - 1:
                    logger.warning(f"QMT尚未完全启动或登录就绪，第 {i+1}/{max_retries} 次尝试连接失败，{retry_interval}秒后重试...")
                    time.sleep(retry_interval)
                else:
                    logger.error(f"已重试 {max_retries} 次均无法连接 QMT。")
                    raise BrokerError(f"QMT连接最终失败: {e}")

    def _login(self):
        try:
            logger.info("正在连接QMT交易系统...")
            session_id = int(time.time())
            self.xt_trader = XtQuantTrader(self.qmt_path, session_id)
            self.xt_trader.start()
            connect_result = self.xt_trader.connect()
            if connect_result != 0:
                raise BrokerError("请确保miniQMT客户端已运行并登录")
            logger.info("QMT软件终端连接成功")
            self.stock_account = StockAccount(self.account_id)
            sub = self.xt_trader.subscribe(self.stock_account)
            if sub != 0:
                raise BrokerError(f"账户信息订阅失败！请检查账户ID: {self.account_id}")
            logger.info(f"账户[{self.account_id}]信息订阅成功")
            self.is_connected = True
        except BrokerError:
            raise
        except Exception as e:
            logger.error(f"QMT登录失败: {e}")
            raise BrokerError(str(e))

    # ==================== 查询 ====================
    # 所有 QMT 调用走 @_safe_call 装饰器，统一处理重连+异常兜底

    @_safe_call(default=None)
    def get_account_info(self) -> Optional[AccountInfo]:
        asset = self.xt_trader.query_stock_asset(self.stock_account)
        if not asset:
            return None
        return AccountInfo(
            account_id=asset.account_id,
            total_asset=asset.total_asset,
            market_value=asset.market_value,
            cash=asset.cash,
            frozen_cash=asset.frozen_cash,
        )

    @_safe_call(default=list)
    def get_orders(self) -> List[OrderInfo]:
        orders = self.xt_trader.query_stock_orders(self.stock_account)
        return [
            OrderInfo(
                stock_code=o.stock_code,
                order_volume=o.order_volume,
                price=o.price,
                order_id=o.order_id,
                order_status=getattr(o, 'order_status', 0),
                status_msg=getattr(o, 'status_msg', ''),
                order_time=datetime.fromtimestamp(o.order_time).strftime('%Y-%m-%d %H:%M:%S'),
                order_type=getattr(o, 'order_type', 0),
                traded_volume=getattr(o, 'traded_volume', 0),
                traded_price=getattr(o, 'traded_price', 0.0),
            )
            for o in orders
        ]

    @_safe_call(default=None)
    def get_order(self, order_id: int) -> Optional[OrderInfo]:
        """查询指定委托订单"""
        orders = self.get_orders()
        for o in orders:
            if o.order_id == order_id:
                return o
        return None

    @_safe_call(default=None)
    def cancel_and_wait(self, order_id: int, timeout_seconds: float = 3.0) -> Optional[OrderInfo]:
        """
        撤单并同步等待撤单完成或成交确认（部撤/已撤/已成）
        每 0.1 秒轮询一次，直到超时。
        """
        self.cancel(order_id)
        iterations = int(timeout_seconds / 0.1)
        for _ in range(iterations):
            time.sleep(0.1)
            o = self.get_order(order_id)
            if o and o.order_status in (53, 54, 56):
                return o
        return self.get_order(order_id)

    @_safe_call(default=list)
    def get_positions(self) -> List[PositionInfo]:
        positions = self.xt_trader.query_stock_positions(self.stock_account)
        result = []
        for p in positions:
            # 过滤已清仓的无效记录
            if p.volume <= 0:
                continue
                
            today_volume = p.volume - p.yesterday_volume
            result.append(PositionInfo(
                stock_code=p.stock_code,
                volume=p.volume,
                can_use_volume=p.can_use_volume,
                frozen_volume=p.frozen_volume,
                open_price=p.open_price,
                market_value=p.market_value,
                on_road_volume=p.on_road_volume,
                previous_volume=p.yesterday_volume,
                today_volume=today_volume,
                is_today_buy=today_volume > 0,
            ))
        return result

    def get_stock_quote(self, stock_code: str) -> Optional[dict]:
        # 行情走 xtdata 不依赖 trader 连接，单独 try 即可
        try:
            tick = xtdata.get_full_tick([stock_code])
            if not tick or stock_code not in tick:
                return None
            data = tick[stock_code]
            last_price = data.get('lastPrice', 0.0)
            last_close = data.get('lastClose', 0.0)
            change_pct = 0.0
            if last_close > 0:
                change_pct = (last_price - last_close) / last_close * 100
                
            # 提取或计算涨跌停价格（防止报废单）
            high_limit = data.get('highLimitPrice', 0.0) or data.get('highLimit', 0.0)
            low_limit = data.get('lowLimitPrice', 0.0) or data.get('lowLimit', 0.0)
            if high_limit <= 0 or low_limit <= 0:
                if last_close > 0:
                    is_gem_or_star = False
                    clean_code = stock_code.replace('.', '').upper()
                    if clean_code.startswith('SZ30') or clean_code.startswith('30') or clean_code.startswith('SH68') or clean_code.startswith('68'):
                        is_gem_or_star = True
                    ratio = 0.2 if is_gem_or_star else 0.1
                    high_limit = round(last_close * (1 + ratio), 2)
                    low_limit = round(last_close * (1 - ratio), 2)

            return {
                'stock_code': stock_code,
                'last_price': last_price,
                'last_close': last_close,
                'open': data.get('open', 0.0),
                'high': data.get('high', 0.0),
                'low': data.get('low', 0.0),
                'volume': data.get('volume', 0),
                'change_pct': round(change_pct, 2),
                'high_limit': high_limit,
                'low_limit': low_limit,
            }
        except Exception as e:
            logger.error(f"获取行情数据失败: {e}")
            return None

    # ==================== 交易 ====================

    def buy(self, stock_code: str, volume: int, price: float) -> OrderResult:
        return self._order(stock_code, volume, price, xtconstant.STOCK_BUY, "买入")

    def sell(self, stock_code: str, volume: int, price: float) -> OrderResult:
        return self._order(stock_code, volume, price, xtconstant.STOCK_SELL, "卖出")

    @_safe_call(default=lambda: OrderResult(False, message="QMT异常"))
    def _order(self, stock_code, volume, price, order_type, action_name) -> OrderResult:
        order_id = self.xt_trader.order_stock(
            account=self.stock_account,
            stock_code=stock_code,
            order_type=order_type,
            order_volume=volume,
            price_type=xtconstant.FIX_PRICE,
            price=price,
        )
        if order_id > 0:
            logger.info(f"[发单成功] {action_name} {stock_code} {volume}股@{price} 委托号={order_id}")
            return OrderResult(True, order_id=order_id, message=f"{action_name}委托成功")
        logger.warning(f"[发单失败] {action_name} {stock_code} {volume}股@{price} 返回={order_id}")
        return OrderResult(False, message=f"{action_name}委托失败: {stock_code}")

    @_safe_call(default=lambda: OrderResult(False, message="QMT异常"))
    def cancel(self, order_id: int) -> OrderResult:
        r = self.xt_trader.cancel_order_stock(self.stock_account, order_id)
        if r == 0:
            logger.info(f"[撤单成功] 委托号={order_id}")
            return OrderResult(True, order_id=order_id, message="撤单成功")
        logger.warning(f"[撤单失败] 委托号={order_id} 返回={r}")
        return OrderResult(False, message=f"撤单失败: {order_id}")

    def disconnect(self):
        if self.xt_trader and self.is_connected:
            try:
                self.xt_trader.stop()
                self.is_connected = False
                logger.info("QMT连接已断开")
            except Exception as e:
                logger.error(f"断开连接失败: {e}")

    # ==================== 价格计算与保护 ====================

    def calculate_price(self, stock_code: str, price: float, side: str, use_premium: bool = True) -> float:
        """
        统一计算买入/卖出申报价格，支持折溢价及涨跌停限制保护
        side: 'BUY' 或 'SELL'
        """
        quote = self.get_stock_quote(stock_code)
        side_upper = side.upper()
        
        if side_upper == 'BUY':
            high_limit = quote.get('high_limit', 0.0) if quote else 0.0
            # 动态比例溢价，自适应高低价股的滑点扫单价差
            ratio = StgConfig.BUY_PRICE_PREMIUM_RATIO if use_premium else 0.0
            # 统一使用数学四舍五入到分
            buy_price = int((price * (1 + ratio)) * 100 + 0.5 + 1e-9) / 100
            if high_limit > 0:
                buy_price = min(buy_price, high_limit)
            return buy_price
            
        elif side_upper == 'SELL':
            low_limit = quote.get('low_limit', 0.0) if quote else 0.0
            # 统一折价
            sell_price = round(price * (1 - StgConfig.SELL_DISCOUNT), 2)
            # 直接 max 兜底，即便 low_limit 是 0.0 也安全
            return max(sell_price, low_limit)
            
        return price
