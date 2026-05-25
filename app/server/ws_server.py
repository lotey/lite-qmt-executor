#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebSocket客户端 - 连接信号网关接收交易信号
"""

import json
import logging
import threading
import websocket

from app.config import Config

logger = logging.getLogger(__name__)

# 模块级控制：用于外部触发停止（例如收到 SIGINT / 跨线程关停）
_stop_event = threading.Event()
_current_ws = None


def stop():
    """请求停止 WebSocket 客户端，可被外部（信号处理、其他线程）调用"""
    _stop_event.set()
    ws = _current_ws
    if ws is not None:
        try:
            ws.close()
        except Exception as e:
            logger.warning(f"关闭WebSocket时异常: {e}")


def run_server(engine, host=None, port=None):
    """
    启动WebSocket客户端，连接网关接收信号

    :param engine: TradingEngine 实例
    :param host: 未使用（保持接口兼容）
    :param port: 未使用（保持接口兼容）
    """
    global _current_ws
    _stop_event.clear()

    gateway_url = f"ws://{Config.GATEWAY_HOST}:{Config.GATEWAY_PORT}/ws?token={Config.GATEWAY_TOKEN}"
    logger.info(f"WebSocket服务启动: ws://{Config.GATEWAY_HOST}:{Config.GATEWAY_PORT}/ws")

    # 重连策略：
    # - 从未成功连上过：最多重试 max_retries 次后放弃
    # - 曾经连上过再断的：永不放弃（网络抖动恢复后自动重连）
    retry_interval = 20
    max_retries = 3
    fail_count = [0]
    ever_connected = [False]

    def on_message(ws, message):
        """收到信号 → 入队，立即返回，不阻塞收包线程"""
        try:
            data = json.loads(message)
            code = data.get('code')
            if not code:
                logger.warning(f"收到无效信号: {message}")
                return
            logger.info(f"收到信号: {code} (原始数据: {message})")
            engine.submit(code)
        except Exception as e:
            logger.error(f"处理信号异常: {e}", exc_info=True)

    def on_error(ws, error):
        logger.error(f"WebSocket错误: {error}")

    def on_close(ws, close_status_code, close_msg):
        logger.warning(f"WebSocket连接断开: {close_status_code} {close_msg}")

    def on_open(ws):
        fail_count[0] = 0
        ever_connected[0] = True
        logger.info("WebSocket连接成功，等待信号...")

    def connect_with_retry():
        """
        带重连的连接：
        - 连续失败 max_retries 次放弃（配置错误/网关没开）
        - 成功过一次后再断，重置计数继续重试（网络抖动）
        - 收到停止信号立即退出
        """
        global _current_ws
        try:
            while not _stop_event.is_set():
                ws = websocket.WebSocketApp(
                    gateway_url,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                    on_open=on_open
                )
                _current_ws = ws
                ws.run_forever(ping_interval=30, ping_timeout=10)
                _current_ws = None

                if _stop_event.is_set():
                    logger.info("收到停止信号，退出WebSocket循环")
                    return

                fail_count[0] += 1

                # 连续失败达到上限且从未成功过 → 放弃
                if fail_count[0] >= max_retries and not ever_connected[0]:
                    logger.error(f"WebSocket连续{fail_count[0]}次连接失败，放弃重连")
                    return

                logger.info(f"{retry_interval}秒后尝试重连... (连续失败{fail_count[0]}次)")
                if _stop_event.wait(retry_interval):
                    logger.info("等待重连期间收到停止信号，退出")
                    return
        finally:
            _current_ws = None

    try:
        connect_with_retry()
    except KeyboardInterrupt:
        logger.info("收到Ctrl+C，正在关闭WebSocket...")
        stop()
