#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立系统心跳管理器
"""

import os
import json
import time
import threading
import logging

logger = logging.getLogger(__name__)

class HeartbeatManager:
    """
    独立系统心跳管理器
    职责：
    1. 提供 get_offline_seconds() 获取停机时间。
    2. 提供 start() 启动独立守护线程定时写入当前时间戳。
    """
    
    def __init__(self, file_path: str = "data/system_heartbeat.json", interval: float = 30.0):
        self.file_path = os.path.abspath(file_path)
        self.interval = interval
        self._running = False

    def get_offline_seconds(self) -> float:
        """
        获取系统上次停机（离线）时长，单位为秒。
        必须在 start() 启动写入之前调用，以防数据被覆盖。
        若返回 -1.0，代表心跳文件不存在或数据损坏（通常为开市首次启动）。
        """
        if not os.path.exists(self.file_path):
            return -1.0
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                last_active = data.get("last_active_time", 0.0)
                if last_active > 0:
                    return time.time() - last_active
        except Exception as e:
            logger.error(f"读取系统心跳文件异常: {e}")
        return -1.0

    def start(self) -> None:
        """
        启动独立的后台守护线程定时写入心跳
        """
        if self._running:
            return
        self._running = True
        thread = threading.Thread(target=self._write_loop, daemon=True, name="HeartbeatThread")
        thread.start()
        logger.info("心跳管理器已启动，刷新频率=%ds", self.interval)

    def _write_loop(self) -> None:
        """后台写入线程主循环"""
        while self._running:
            try:
                os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
                with open(self.file_path, 'w', encoding='utf-8') as f:
                    json.dump({"last_active_time": time.time()}, f)
            except Exception as e:
                logger.error(f"心跳写入失败: {e}")
            
            time.sleep(self.interval)
