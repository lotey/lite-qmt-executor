#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
卖出调度骨架（框架层）

负责注册、启停以及统一下发信号给所有 SellStrategy。
具体的执行逻辑、执行频率和工作线程由各策略自行实现。
"""

import logging
from typing import List

from app.strategy.buy_strategy import StrategyContext
from app.strategy.sell_strategy import SellStrategy

logger = logging.getLogger(__name__)


class SellEngine:
    """
    卖出调度骨架
    
    采用“松耦合+精准投递”架构：
    1. 引擎本身不干预具体的卖出逻辑（如高开抢跑、止盈止损），只负责生命周期管理。
    2. 基于字典 (Dictionary) 实现 O(1) 的策略实例管理。
    3. 充当统一的信号中转站，将从 TradingEngine 过来的信号精准路由给指定的具体卖出策略。
    """

    def __init__(self, ctx: StrategyContext):
        self.ctx = ctx
        # 策略字典，按策略名称进行注册和 O(1) 路由投递
        self._strategies = {}
        self._running = False

    def register(self, strategy: SellStrategy) -> None:
        """挂载卖出策略插件"""
        self._strategies[strategy.name()] = strategy
        logger.info(f"已注册卖出策略: {strategy.name()}")

    def start(self) -> None:
        """一键拉起所有挂载的卖出策略，让它们各自启动独立的工作线程"""
        if self._running:
            return
        self._running = True
        for s in self._strategies.values():
            try:
                s.start(self.ctx)
            except Exception as e:
                logger.error(f"启动卖出策略 {s.name()} 失败: {e}", exc_info=True)
        logger.info("卖出引擎已启动，已激活策略=%s", list(self._strategies.keys()))

    def stop(self) -> None:
        """优雅关闭所有卖出策略"""
        if not self._running:
            return
        self._running = False
        for name, strategy in self._strategies.items():
            try:
                strategy.stop()
                logger.info(f"已停止卖出策略: {name}")
            except Exception as e:
                logger.warning(f"停止卖出策略 {name} 时异常: {e}")
        logger.info("卖出引擎已完全停止")

    def submit(self, code: str, signal_type: str, strategy_name: str, payload: dict = None) -> bool:
        """
        信号投递核心路口：将卖出信号精准分发给指定的业务执行者
        
        工作流：
        1. TradingEngine 收到外界信号后，通过 SIGNAL_ROUTE_MAP 查到该信号归属的 strategy_name
        2. 投递到此处，此处按名索骥找到挂载的对应策略实例
        3. 调用策略本身的 on_signal() 进行业务内化处理
        """
        strategy = self._strategies.get(strategy_name)
        if not strategy:
            logger.warning(f"未找到对应的卖出策略: {strategy_name}，信号被丢弃")
            return False
            
        # 移交控制权，由具体的卖出策略去评估和执行（如加入等待队列，或直接下柜台）
        return strategy.on_signal(code, signal_type, payload)
