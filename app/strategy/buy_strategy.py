#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
买入策略抽象类 + 上下文

用户继承 BuyStrategy 实现自己的决策逻辑。
框架（BuyEngine）负责调度、并发、任务管理；策略只关心"现在该不该买、买多少"。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# ==================== 数据结构 ====================

@dataclass
class OrderTracker:
    """订单生命周期属性追踪基类"""
    submit_time: float = 0.0       # 下单时间戳
    notified_volume: int = 0       # 已通知的成交股数


@dataclass
class BuyTask:
    """
    买入任务。框架管理生命周期，策略可读写其中字段记录自己状态。

    `phase` 是状态机标记，命名由策略自定义（默认从 INIT 开始，DONE 表示完结）。
    `data` 是策略私有数据兜底，想存什么存什么。
    """
    code: str                                       # 原始code（如 sz000001）
    qmt_code: str                                   # QMT格式（如 000001.SZ）
    enter_time: datetime
    phase: str = "INIT"                             # 任务状态，策略自定义
    data: dict = field(default_factory=dict)        # 策略私有数据

    def is_done(self) -> bool:
        return self.phase == "DONE"


# ==================== 策略上下文 ====================

class StrategyContext:
    """
    传给策略的运行上下文，封装框架能力，避免策略直接依赖 engine 内部。
    """

    def __init__(self, broker, notifier, config):
        self.broker = broker        # QmtBroker，下单/查询用
        self.notifier = notifier    # Notifier，推消息用
        self.config = config        # 配置对象

    def now(self) -> datetime:
        return datetime.now()


# ==================== 策略抽象类 ====================

class BuyStrategy(ABC):
    """买入策略抽象类。用户继承实现具体决策。"""

    def name(self) -> str:
        """策略名（用于多策略路由）。默认用类名。"""
        return self.__class__.__name__

    @abstractmethod
    def on_init(self, task: BuyTask, ctx: StrategyContext) -> None:
        """
        信号首次到达时调用。决定首次买入动作（一波流买、首笔保底、等回落、放弃……）。
        通过修改 task.phase 推进状态，用 task.is_done() 终止。
        """
        ...

    @abstractmethod
    def on_tick(self, task: BuyTask, ctx: StrategyContext) -> None:
        """
        ticker 周期推进（默认 5 秒一次）。
        策略根据 task.phase 自己 dispatch 到对应处理逻辑（等回落、加仓判断等）。
        """
        ...

    def should_accept(self, code: str) -> bool:
        """
        信号过滤。返回 False 直接丢弃这条信号（连任务表都不进）。
        默认全收。常见用法：过滤创业板、特定时段、黑名单。
        """
        return True
