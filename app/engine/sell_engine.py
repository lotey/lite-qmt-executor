#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
卖出调度骨架（框架层）

只负责注册和启停所有 SellStrategy。每个 SellStrategy 自己管自己的工作线程。
框架不规定卖出节奏（高开止盈 9:30 抢一次、盈利止盈每 3 秒扫……都由策略内部决定）。
"""

import logging
from typing import List

from app.strategy.buy_strategy import StrategyContext
from app.strategy.sell_strategy import SellStrategy

logger = logging.getLogger(__name__)


class SellEngine:
    """卖出调度骨架"""

    def __init__(self, ctx: StrategyContext):
        self.ctx = ctx
        self._strategies: List[SellStrategy] = []
        self._running = False

    def register(self, strategy: SellStrategy) -> None:
        self._strategies.append(strategy)
        logger.info(f"已注册卖出策略: {strategy.name()}")

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for s in self._strategies:
            try:
                s.start(self.ctx)
            except Exception as e:
                logger.error(f"启动卖出策略 {s.name()} 失败: {e}", exc_info=True)
        logger.info("卖出引擎已启动，策略=%s", [s.name() for s in self._strategies])

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for s in self._strategies:
            try:
                s.stop()
            except Exception as e:
                logger.warning(f"停止卖出策略 {s.name()} 异常: {e}")
        logger.info("卖出引擎已停止")
