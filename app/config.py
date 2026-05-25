#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
框架配置

只放框架本身需要的配置项。具体策略参数（涨幅档位、卖出比例等）
请在各自的策略类里定义为类常量，便于继承覆盖。
"""

import os
import logging
from datetime import time, datetime

logger = logging.getLogger(__name__)


class Config:
    """系统配置"""

    # ==================== API配置 ====================
    # HTTP 入口（始终启动）
    HTTP_HOST = '0.0.0.0'
    HTTP_PORT = 30015

    # WebSocket 入口（连接 quant-gateway，三个都填了才启动，不需要就填 None）
    GATEWAY_HOST = None
    GATEWAY_PORT = None
    GATEWAY_TOKEN = None

    # ==================== QMT 配置 ====================
    QMT_PATH = r'D:\国金证券QMT交易端\userdata_mini'  # ⚠️ 改为真实的 userdata_mini 目录
    ACCOUNT_ID = '888888'  # ⚠️ 改为真实资金账号
    BROKER_NAME = '国金证券0525'  # 消息前缀（用于多账户区分）

    # ==================== 交易时段（A股） ====================
    # 引擎只在该窗口内 tick，其他时间早返回不工作
    TRADING_OPEN = time(9, 0)
    TRADING_CLOSE = time(15, 0)

    # 买入总开关（设为 False 可临时屏蔽所有买入信号，卖出不受影响）
    BUY_ENABLED = False

    @classmethod
    def in_trading_window(cls) -> bool:
        """当前是否在交易窗口（9:00-15:00），周末暂不排除"""
        now = datetime.now().time()
        return cls.TRADING_OPEN <= now < cls.TRADING_CLOSE

    # ==================== 买入引擎调度参数（框架）====================
    BUY_POLL_INTERVAL = 3           # ticker 周期（秒）
    BUY_QUEUE_MAXSIZE = 32          # 信号队列上限
    BUY_SIGNAL_POOL_SIZE = 8        # 新信号并行池大小
    BUY_DEADLINE = time(14, 50)      # 全局买入截止时间，到点清空所有任务
    NEW_SIGNAL_DEADLINE = time(11, 0)  # 新信号截止时间，此时间后不再接受新股信号
    BUY_RECOVERY_MAX_AGE_MINS = 10      # 灾备恢复最大时效（分钟），超过此时间的未完成信号不进行恢复和补单


    # ==================== 日志配置 ====================
    LOG_LEVEL = 'INFO'
    LOG_DIR = 'logs'
    LOG_FILE = 'qmt_trading.log'
    LOG_BACKUP_DAYS = 7  # 日志按天滚动保留天数
    WAL_BACKUP_DAYS = 7  # WAL 交易状态日志保留天数

    # ==================== 配置校验 ====================

    @classmethod
    def validate(cls):
        if not cls.ACCOUNT_ID:
            raise ValueError("必须配置 ACCOUNT_ID")

        if not os.path.exists(cls.QMT_PATH):
            raise ValueError(f"QMT路径不存在: {cls.QMT_PATH}")

        logger.info("✓ 配置验证通过")
        logger.info(f"  - QMT路径: {cls.QMT_PATH}")
        logger.info(f"  - 账户ID: {cls.ACCOUNT_ID}")
        logger.info(f"  - HTTP服务: {cls.HTTP_HOST}:{cls.HTTP_PORT}")
        if cls.GATEWAY_HOST and cls.GATEWAY_PORT:
            logger.info(f"  - WebSocket网关: {cls.GATEWAY_HOST}:{cls.GATEWAY_PORT}")
        else:
            logger.info(f"  - WebSocket网关: 未配置（仅HTTP）")
