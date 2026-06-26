#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QMT 量化交易执行器 - 主入口

在这个文件里：
1. 实例化 broker / notifier
2. 创建 TradingEngine
3. 注册自己的买入/卖出策略
4. 启动信号入口（HTTP 或 WebSocket）

下面默认使用 app/strategy/default/ 里的默认策略，
真正写自己策略的话，继承 BuyStrategy / SellStrategy 替换即可。
"""

import logging
import socket
import sys
import threading
from logging.handlers import TimedRotatingFileHandler

from app.config import Config
from app.core.broker import QmtBroker, BrokerError
from app.core.notifier import ConsoleNotifier
from app.core.trading_engine import TradingEngine

# 默认策略（功能完整版，开箱即用）
from app.strategy.default.accumulate_buy import AccumulateBuyStrategy
from app.strategy.default.profit_tier_sell import ProfitTierSellStrategy

logger = logging.getLogger(__name__)

# 单实例锁端口（不会真用这个端口通信，只是占位防重复启动）
_SINGLETON_PORT = 59999
_lock_socket = None


def _acquire_singleton():
    """占端口当进程锁，第二个实例绑不上就退出"""
    global _lock_socket
    _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _lock_socket.bind(('127.0.0.1', _SINGLETON_PORT))
    except OSError:
        print("❌ 已有一个 quant-executor 进程在运行，请勿重复启动")
        sys.exit(1)


def setup_logging():
    import os
    if not os.path.exists(Config.LOG_DIR):
        os.makedirs(Config.LOG_DIR)
    log_file = os.path.join(Config.LOG_DIR, Config.LOG_FILE)
    # 按天滚动归档：每天0点切到 qmt_trading.log.YYYY-MM-DD，主文件保留当日，超过保留天数自动删除
    file_handler = TimedRotatingFileHandler(
        log_file, when='midnight', interval=1, backupCount=Config.LOG_BACKUP_DAYS, encoding='utf-8'
    )
    file_handler.suffix = '%Y-%m-%d'
    logging.basicConfig(
        level=getattr(logging, Config.LOG_LEVEL),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            file_handler,
            logging.StreamHandler(sys.stdout),
        ],
    )


def start_http_server_async(engine):
    """启动 HTTP 服务（后台线程，不阻塞主线程）"""
    from app.server.http_server import create_app
    from waitress import serve
    app = create_app(engine)
    logger.info(f"启动HTTP服务: http://{Config.HTTP_HOST}:{Config.HTTP_PORT}")
    t = threading.Thread(
        target=serve, args=(app,),
        kwargs={'host': Config.HTTP_HOST, 'port': Config.HTTP_PORT, '_quiet': True},
        daemon=True, name="HttpServer"
    )
    t.start()


def start_websocket_server(engine):
    """WebSocket 信号入口：连不上时引擎仍继续跑（存量任务推进、卖出仍生效）。"""
    from app.server.ws_server import run_server
    logger.info(f"启动WebSocket服务: {Config.GATEWAY_HOST}:{Config.GATEWAY_PORT}")

    def _ws_thread():
        try:
            run_server(engine)
        except Exception as e:
            logger.error(f"WebSocket线程异常: {e}", exc_info=True)
        finally:
            logger.warning("WebSocket无法连接，请检查配置，当前仅HTTP模式运行")

    threading.Thread(target=_ws_thread, daemon=True, name="WebSocketClient").start()
    threading.Event().wait()  # 主线程挂起，Ctrl+C 退出


def build_engine() -> TradingEngine:
    """组装引擎和策略。要换自己的策略在这里改。"""
    broker = QmtBroker(Config.QMT_PATH, Config.ACCOUNT_ID)

    # 盘前安全获取并缓存初始总资产基准（内部自动进行重试）
    Config.TODAY_TOTAL_ASSETS = broker.get_initial_total_asset(max_retries=5, retry_interval=10.0)

    # 通知器。默认走控制台，需要 IM 推送自行实现 Notifier 接口注入
    notifier = ConsoleNotifier()

    engine = TradingEngine(broker, notifier)

    # 注册买入策略
    engine.register_buy_strategy(AccumulateBuyStrategy())

    # 注册卖出策略
    engine.register_sell_strategy(ProfitTierSellStrategy())

    return engine


def main():
    _acquire_singleton()
    setup_logging()
    print("========================================")
    print("  QMT 量化交易执行器")
    print("========================================")
    print()
    print("[提示] 请确保 miniQMT 已启动并登录")
    print()
    logger.info("=" * 60)
    logger.info("QMT 量化交易执行器 v1.0")
    logger.info("=" * 60)

    engine = None
    try:
        Config.validate()
        engine = build_engine()
        engine.run()

        # HTTP 始终启动（查询/管理接口 + HTTP 模式信号入口）
        start_http_server_async(engine)

        # WebSocket：三个参数都填了才启动，否则提示不启动
        if Config.GATEWAY_HOST and Config.GATEWAY_PORT and Config.GATEWAY_TOKEN:
            start_websocket_server(engine)
        else:
            missing = []
            if not Config.GATEWAY_HOST:
                missing.append('GATEWAY_HOST')
            if not Config.GATEWAY_PORT:
                missing.append('GATEWAY_PORT')
            if not Config.GATEWAY_TOKEN:
                missing.append('GATEWAY_TOKEN')
            logger.warning(f"WebSocket服务未启动，配置不完整（缺少: {', '.join(missing)}）")
            threading.Event().wait()

    except KeyboardInterrupt:
        logger.info("收到退出信号，正在关闭...")
    except BrokerError as e:
        logger.error(f"QMT连接失败: {e}，程序即将退出")
        sys.exit(1)
    except Exception as e:
        logger.error(f"系统启动失败: {e}", exc_info=True)
        sys.exit(1)
    finally:
        try:
            from app.server import ws_server
            ws_server.stop()
        except Exception:
            pass
        if engine is not None:
            try:
                engine.stop()
            except Exception as e:
                logger.warning(f"关闭引擎异常: {e}")
        logger.info("执行器已关闭")


if __name__ == '__main__':
    main()
