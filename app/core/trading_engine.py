#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易调度总控：装配 Broker / Notifier / BuyEngine / SellEngine

外部入口（ws/http）只需调 engine.submit() 入信号；
启动 engine.run()，关停 engine.stop()。
"""

import logging
from typing import Optional

from app.config import Config
from app.core.broker import QmtBroker
from app.core.notifier import Notifier, NullNotifier
from app.engine.buy_engine import BuyEngine
from app.engine.sell_engine import SellEngine
from app.strategy.buy_strategy import BuyStrategy, StrategyContext
from app.strategy.sell_strategy import SellStrategy
from app.strategy.default.stg_config import SIGNAL_TYPE_ACTIVE, SIGNAL_ROUTE_MAP
from app.core.heartbeat import HeartbeatManager

logger = logging.getLogger(__name__)


class TradingEngine:
    """交易调度总控"""

    def __init__(self, broker: QmtBroker, notifier: Optional[Notifier] = None):
        self.broker = broker
        self.notifier = notifier or NullNotifier()
        self.ctx = StrategyContext(broker=self.broker, notifier=self.notifier, config=Config)
        self.buy_engine = BuyEngine(self.ctx)
        self.sell_engine = SellEngine(self.ctx)
        
        # 将 HeartbeatManager 交由引擎统一管控，便于共享状态给所有 Strategy
        self.heartbeat_manager = HeartbeatManager(
            broker=self.broker,
            interval_secs=Config.HEARTBEAT_INTERVAL_SECS
        )
        self.ctx.heartbeat_manager = self.heartbeat_manager
        
        self._running = False

    # ==================== 注册策略 ====================

    def register_buy_strategy(self, strategy: BuyStrategy) -> None:
        """注册买入策略。name 用于路由，submit 时通过 strategy_name 指定。"""
        self.buy_engine.register(strategy)

    def register_sell_strategy(self, strategy: SellStrategy) -> None:
        self.sell_engine.register(strategy)

    # ==================== 对外接口 ====================

    def run(self) -> None:
        if self._running:
            logger.warning("交易引擎已启动，忽略重复 run()")
            return
        self._running = True
        logger.info("交易引擎启动中...")
        
        # 1. 启动并等待首次心跳成功，然后再启动各事业部，确保重连/状态自愈的基础数据(如同总资产)已就位
        self.heartbeat_manager.start()
        
        self.buy_engine.start()
        self.sell_engine.start()
        logger.info("交易引擎启动完成")

    def submit(self, code: str, action: str = "BUY", signal_type: str = SIGNAL_TYPE_ACTIVE, payload: dict = None) -> bool:
        """提交信号接口，支持 BUY/SELL 二元路由"""
        if not self._running:
            logger.warning(f"交易引擎未启动，丢弃信号: {code}")
            return False

        # 从全局路由表中查询策略名称，如果找不到就抛弃
        strategy_name = SIGNAL_ROUTE_MAP.get(signal_type)
        if not strategy_name:
            logger.warning(f"未知的信号类型: {signal_type}，无法路由，丢弃信号: {code}")
            return False

        # 第一层分发：卖出信号交给卖出引擎（SellEngine）进行二次路由
        if action == "SELL":
            return self.sell_engine.submit(code, signal_type=signal_type, strategy_name=strategy_name, payload=payload)
        
        # BUY 信号交给买入引擎
        return self.buy_engine.submit(code, strategy_name=strategy_name, payload=payload)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        logger.info("交易引擎正在停止...")
        self.buy_engine.stop()
        self.sell_engine.stop()
        logger.info("交易引擎已停止")
