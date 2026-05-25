#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTTP API 服务（Flask）

对外接收交易信号、提供持仓/订单查询、撤单等接口。
"""

import logging
from datetime import datetime

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)


def create_app(engine):
    """
    :param engine: TradingEngine 实例
    """
    app = Flask(__name__)
    broker = engine.broker

    @app.before_request
    def log_request_info():
        """
        请求前置拦截器：自动捕获并记录所有 API 请求的方法、路径、客户端来源 IP 以及传入的参数 (GET/POST)
        """
        request.start_time = datetime.now()
        params = ""
        if request.method == 'GET':
            params = f" args={dict(request.args)}" if request.args else ""
        elif request.method == 'POST':
            try:
                json_data = request.get_json(silent=True)
                if json_data:
                    params = f" data={json_data}"
            except Exception:
                pass
        logger.info(f"📥 收到 API 请求: {request.method} {request.path} from {request.remote_addr}{params}")

    @app.after_request
    def log_response_info(response):
        """
        请求后置拦截器：精确计算 API 从接收到响应完成的总耗时（毫秒级），并输出响应状态码
        """
        duration = datetime.now() - getattr(request, 'start_time', datetime.now())
        duration_ms = duration.total_seconds() * 1000
        logger.info(f"📤 API 响应完成: {request.method} {request.path} status={response.status_code} cost={duration_ms:.2f}ms")
        return response

    @app.route('/health', methods=['GET'])
    def health():
        return jsonify({
            "status": "ok",
            "qmt_connected": broker.is_connected,
            "timestamp": datetime.now().isoformat(),
        })

    @app.route('/api/buy', methods=['POST'])
    def buy():
        """
        买入信号入队，立即返回。
        请求体: { "code": "sz000001", "strategy": "default" }
        """
        try:
            data = request.get_json() or {}
            code = data.get('code')
            if not code:
                return jsonify({"success": False, "message": "缺少必填字段: code"}), 400

            strategy_name = data.get('strategy', 'default')

            queued = engine.submit(code, strategy_name=strategy_name)
            return jsonify({
                "success": True,
                "action": "QUEUED" if queued else "DROPPED",
                "code": code,
                "strategy": strategy_name,
                "timestamp": datetime.now().isoformat(),
            })
        except Exception as e:
            logger.error(f"买入接口异常: {e}", exc_info=True)
            return jsonify({"success": False, "message": str(e)}), 500

    @app.route('/api/tasks', methods=['GET'])
    def get_tasks():
        """查询所有策略下的当前买入任务状态"""
        return jsonify({
            "success": True,
            "data": engine.buy_engine.get_status(),
            "timestamp": datetime.now().isoformat(),
        })

    @app.route('/api/sell', methods=['POST'])
    def sell():
        """
        手动卖出（不走策略，直接调 broker）。
        请求体: { "code": "000001.SZ", "volume": 100, "price": 10.50 }
        """
        try:
            data = request.get_json() or {}
            if 'code' not in data or 'volume' not in data:
                return jsonify({"success": False, "message": "缺少必填字段: code, volume"}), 400

            code = data['code']
            volume = data['volume']
            price = data.get('price')

            if not price:
                quote = broker.get_stock_quote(code)
                if not quote:
                    return jsonify({"success": False, "message": f"无法获取行情: {code}"}), 500
                price = quote['last_price']

            result = broker.sell(code, volume, price)
            status = 200 if result.success else 500
            return jsonify({
                "success": result.success,
                "code": code,
                "volume": volume,
                "price": price,
                "order_id": result.order_id,
                "message": result.message,
                "timestamp": datetime.now().isoformat(),
            }), status
        except Exception as e:
            logger.error(f"卖出接口异常: {e}", exc_info=True)
            return jsonify({"success": False, "message": str(e)}), 500

    @app.route('/api/positions', methods=['GET'])
    def get_positions():
        """查询当前持仓列表"""
        try:
            positions = broker.get_positions()
            return jsonify({
                "success": True,
                "data": [
                    {
                        "stock_code": p.stock_code,
                        "volume": p.volume,
                        "can_use_volume": p.can_use_volume,
                        "open_price": p.open_price,
                        "market_value": p.market_value,
                        "is_today_buy": p.is_today_buy,
                    } for p in positions
                ],
                "count": len(positions),
                "timestamp": datetime.now().isoformat(),
            })
        except Exception as e:
            logger.error(f"查询持仓异常: {e}", exc_info=True)
            return jsonify({"success": False, "message": str(e)}), 500

    @app.route('/api/orders', methods=['GET'])
    def get_orders():
        """查询当日委托订单列表"""
        try:
            orders = broker.get_orders()
            return jsonify({
                "success": True,
                "data": [
                    {
                        "order_id": o.order_id,
                        "stock_code": o.stock_code,
                        "order_volume": o.order_volume,
                        "traded_volume": o.traded_volume,
                        "price": o.price,
                        "order_status": o.order_status,
                        "status_msg": o.status_msg,
                        "order_time": o.order_time,
                    } for o in orders
                ],
                "count": len(orders),
                "timestamp": datetime.now().isoformat(),
            })
        except Exception as e:
            logger.error(f"查询订单异常: {e}", exc_info=True)
            return jsonify({"success": False, "message": str(e)}), 500

    @app.route('/api/account', methods=['GET'])
    def get_account():
        """查询账户资产信息（总资产/可用资金/持仓市值）"""
        try:
            account = broker.get_account_info()
            if not account:
                return jsonify({"success": False, "message": "未获取到账户信息"}), 500
            return jsonify({
                "success": True,
                "data": {
                    "account_id": account.account_id,
                    "total_asset": account.total_asset,
                    "market_value": account.market_value,
                    "cash": account.cash,
                    "frozen_cash": account.frozen_cash,
                },
                "timestamp": datetime.now().isoformat(),
            })
        except Exception as e:
            logger.error(f"查询账户异常: {e}", exc_info=True)
            return jsonify({"success": False, "message": str(e)}), 500

    @app.route('/api/cancel/<int:order_id>', methods=['POST'])
    def cancel_order(order_id):
        """撤销指定委托单"""
        try:
            result = broker.cancel(order_id)
            return jsonify({
                "success": result.success,
                "message": result.message,
                "order_id": order_id,
                "timestamp": datetime.now().isoformat(),
            })
        except Exception as e:
            logger.error(f"撤单异常: {e}", exc_info=True)
            return jsonify({"success": False, "message": str(e)}), 500

    return app
