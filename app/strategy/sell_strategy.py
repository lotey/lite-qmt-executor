#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
卖出策略抽象类

每个 SellStrategy 是一个独立的"卖出执行单元"，自己管自己的工作线程、状态、节奏。
框架（SellEngine）只负责注册和启停，不干预内部行为。
"""

from abc import ABC, abstractmethod

from app.strategy.buy_strategy import StrategyContext  # 复用同一个 ctx


class SellStrategy(ABC):
    """卖出策略抽象类"""

    def name(self) -> str:
        """策略名（日志/调试用）"""
        return self.__class__.__name__

    @abstractmethod
    def start(self, ctx: StrategyContext) -> None:
        """启动自己的工作线程。框架在引擎 run() 时调用。"""
        ...

    @abstractmethod
    def stop(self) -> None:
        """停止工作线程，优雅关停。"""
        ...
