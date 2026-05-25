#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略通用工具与辅助函数
"""

import time as _time

def latency_suffix(submit_time: float, active_check_order=None) -> str:
    """计算并格式化耗时后缀。如果订单尚处于活跃状态（未完全成交或未结案），则不计算耗时。"""
    if active_check_order and active_check_order.is_active():
        return ""
    return f" [{_time.time() - submit_time:.2f}s]" if submit_time > 0 else ""


def normalize_status(order, is_buy: bool = True) -> str:
    """
    根据 QMT 数字状态码判断订单状态。
    部撤(53)和已撤(54)均作为 CANCELED 处理，由上层策略根据实际成交量进行额度/股数扣减。
    """
    s = order.order_status
    if s == 56:
        return 'FILLED'
    if s == 55:
        return 'PARTIAL_FILLED'
    if s == 53:
        return 'CANCELED'
    if s == 54:
        return 'CANCELED'
    if s == 57:
        return 'REJECTED'
    return 'SUBMITTED'


def round_half_up(val: float, decimals: int = 2) -> float:
    """标准数学四舍五入（保留 decimals 位小数）"""
    shift = 10 ** decimals
    return int(val * shift + 0.5 + 1e-9) / shift
