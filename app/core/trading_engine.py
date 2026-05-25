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

logger = logging.getLogger(__name__)


class TradingEngine:
    """交易调度总控"""

    def __init__(self, broker: QmtBroker, notifier: Optional[Notifier] = None):
        self.broker = broker
        self.notifier = notifier or NullNotifier()
        self.ctx = StrategyContext(broker=self.broker, notifier=self.notifier, config=Config)
        self.buy_engine = BuyEngine(self.ctx)
        self.sell_engine = SellEngine(self.ctx)
        self._running = False

    # ==================== 注册策略 ====================

    def register_buy_strategy(self, name: str, strategy: BuyStrategy) -> None:
        """注册买入策略。name 用于路由，submit 时通过 strategy_name 指定。"""
        self.buy_engine.register(strategy, name=name)

    def register_sell_strategy(self, strategy: SellStrategy) -> None:
        self.sell_engine.register(strategy)

    # ==================== 对外接口 ====================

    def run(self) -> None:
        if self._running:
            logger.warning("交易引擎已启动，忽略重复 run()")
            return
        self._running = True
        logger.info("交易引擎启动中...")
        self.buy_engine.start()
        self.sell_engine.start()
        logger.info("交易引擎启动完成")

    def submit(self, code: str, strategy_name: str = "default") -> bool:
        """提交买入信号到指定策略。"""
        if not self._running:
            logger.warning(f"交易引擎未启动，丢弃信号: {code}")
            return False
        return self.buy_engine.submit(code, strategy_name=strategy_name)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        logger.info("交易引擎正在停止...")
        self.buy_engine.stop()
        self.sell_engine.stop()
        logger.info("交易引擎已停止")
