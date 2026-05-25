#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高开止盈策略（功能完整默认实现）

核心思路：
- 9:29 准备阶段：扫昨日持仓 + 行情，挑出开盘涨幅 >= 阈值的票，预备好卖单
- 9:30:00 后高频自旋等待到 9:30:02（避开开盘瞬时拥堵）
- 9:30:02 一次性触发：每只票一个独立线程并发挂卖（互不阻塞）
- 单只票内部：发单 → 等撮合 → 未成交撤单 → 等股份解冻 → 重挂，最多 10 次
- 部分成交：再等一会，全成就走人
"""

import logging
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, time
from typing import List, Optional

from app.config import Config
from app.strategy.default.stg_config import StgConfig
from app.strategy.buy_strategy import StrategyContext
from app.strategy.sell_strategy import SellStrategy
from app.strategy.common import normalize_status

logger = logging.getLogger(__name__)


@dataclass
class _PendingOrder:
    stock_code: str
    volume: int
    sell_price: float
    open_chg: float


class OpenHighSellStrategy(SellStrategy):
    """高开止盈：9:30:02 池子并发抢卖"""

    # 策略重试发单的最大尝试次数
    MAX_RETRY = 10
    # 轮询检查发单及撤单状态的时间间隔（秒）
    CHECK_INTERVAL = 1.0
    # 准备阶段内，网络出现异常时重复扫描重试的时间间隔（秒）
    PREPARE_INTERVAL = 5.0
    # 临近发单时间前执行最后一次行情价格同步的提前量（秒）
    FINAL_SYNC_LEAD = 12.0
    # 临近发单时间前进入高频自旋等待状态的提前量（秒）
    SPIN_WAIT_LEAD = 2.0

    def __init__(self):
        # 策略运行状态标识，用于控制后台主循环的启动和退出
        self._running = False
        # 今日是否成功完成了初始扫描准备（获取持仓及虚拟开盘价行情）
        self._prepared = False
        # 今日临近开盘前是否完成了最后一次行情价格终检同步
        self._final_synced = False
        # 记录今日最终发单执行的日期，用于防止日内重复下单
        self._executed_date = None
        # 筛选出的今日符合高开条件的待发单指令列表
        self._pending: List[_PendingOrder] = []
        # 策略运行上下文环境（提供柜台接口、系统配置及共享状态）
        self._ctx: Optional[StrategyContext] = None
        # 记录上一次执行心跳的日期，用于在跨日时自动重置策略状态
        self._last_date = datetime.now().date()
        # 记录上一次执行扫描准备的时间戳，用于控制重试扫描的间隔时间
        self._last_prepare_time = 0.0

    # ---------- 生命周期 ----------

    def start(self, ctx: StrategyContext) -> None:
        self._ctx = ctx
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="OpenHighSellWorker").start()
        logger.info(f"[{self.name()}] 已启动")

    def stop(self) -> None:
        self._running = False

    def _loop(self):
        while self._running:
            _time.sleep(0.5)
            try:
                self._tick()
            except Exception as e:
                logger.error(f"[{self.name()}] tick 异常: {e}", exc_info=True)

    # ---------- 主流程 ----------

    def _tick(self):
        # 基础时段与交易状态过滤
        if not Config.in_trading_window():
            return
        now = datetime.now()
        
        # 跨日重置
        if now.date() != self._last_date:
            self._prepared = False
            self._final_synced = False
            self._executed_date = None
            self._pending.clear()
            self._last_date = now.date()
            self._last_prepare_time = 0.0

        if self._executed_date == now.date():
            return

        cur = now.time()

        # 计算距离开盘发单的剩余秒数
        execute_datetime = datetime.combine(now.date(), StgConfig.OPEN_HIGH_EXECUTE_TIME)
        time_to_execute = (execute_datetime - now).total_seconds()

        # 准备阶段：在设定的准备区间内进行扫描
        is_prepare_time = StgConfig.OPEN_HIGH_PREPARE_TIME <= cur < StgConfig.OPEN_HIGH_EXECUTE_TIME
        if is_prepare_time and self._ctx.broker.is_connected:
            # 初始准备：尚未成功准备过时，执行首次扫描（若失败则每隔 5 秒重试）
            if not self._prepared:
                if _time.time() - self._last_prepare_time >= self.PREPARE_INTERVAL:
                    self._prepare()
                    self._last_prepare_time = _time.time()
            
            # 终检同步：已完成初始准备，但在进入终检提前量时，执行最后一次开盘行情同步
            elif not self._final_synced and time_to_execute <= self.FINAL_SYNC_LEAD:
                self._prepare()
                self._final_synced = True

        # 触发执行：到达或超过执行时刻，且已完成准备
        if self._prepared and cur >= StgConfig.OPEN_HIGH_EXECUTE_TIME:
            self._execute()
            self._executed_date = now.date()
            return

        # 高频自旋等待：在进入自旋等待提前量内，且存在待发订单时，自旋以保证毫秒级发单精度
        if self._prepared and self._pending and 0 < time_to_execute <= self.SPIN_WAIT_LEAD:
            logger.info(f"[{self.name()}] 接近执行时刻 (剩余 {time_to_execute:.2f} 秒)，进入自旋等待精度发单")
            while datetime.now() < execute_datetime and self._running:
                _time.sleep(0.01)
            self._execute()
            self._executed_date = now.date()

    # ---------- 准备 ----------

    def _prepare(self):
        # 从柜台获取当前所有持仓
        positions = self._ctx.broker.get_positions()
        pending: List[_PendingOrder] = []
        logger.info(f"[{self.name()}] 开始扫描持仓准备高开抢跑，当前持仓品种数: {len(positions)}")
        
        for pos in positions:
            # 过滤无可用持仓的股票（今日买入或已被挂单锁定的昨日持仓）
            if pos.can_use_volume <= 0:
                logger.info(f"[{self.name()}] {pos.stock_code} 可用持仓股数为 0，跳过")
                continue
                
            # 获取实时行情（主要用于提取集合竞价虚拟开盘价与昨日收盘价）
            quote = self._ctx.broker.get_stock_quote(pos.stock_code)
            
            # 风控校验：若行情未获取到，或者开盘价/昨收价数据异常，则跳过防止报错
            if not quote or quote['open'] <= 0 or quote['last_close'] <= 0:
                logger.warning(
                    f"[{self.name()}] {pos.stock_code} 行情数据异常或未获取，跳过: "
                    f"quote={quote}"
                )
                continue
                
            # 计算集合竞价高开涨幅（较昨日收盘价的百分比）
            open_chg = (quote['open'] - quote['last_close']) / quote['last_close'] * 100
            
            # 涨幅条件校验：若未达到高开阈值（如设定的 3%），则忽略不处理
            if open_chg < StgConfig.OPEN_HIGH_CHG:
                logger.info(
                    f"[{self.name()}] {pos.stock_code} 开盘涨幅 {open_chg:.2f}%，"
                    f"未达高开阈值 {StgConfig.OPEN_HIGH_CHG}%，跳过"
                )
                continue
                
            # 计算抢跑卖出股数：按比例计算，并严格向下取整到百股（1手）的整数倍
            sell_volume = int(pos.can_use_volume * StgConfig.OPEN_HIGH_SELL_RATIO) // 100 * 100
            # 兜底风控：如果比例换算后不足 100 股，则强制以最小发单单位 100 股（1手）报单
            if sell_volume < 100:
                sell_volume = 100
                
            # 计算符合柜台规范的合法卖出报价（根据最小变动价位/步长截取）
            sell_price = self._ctx.broker.calculate_price(pos.stock_code, quote['open'], 'SELL')
            
            # 加入今日待发单指令列表
            pending.append(_PendingOrder(pos.stock_code, sell_volume, sell_price, open_chg))
            logger.info(
                f"[{self.name()}] 预备: {pos.stock_code} 高开{open_chg:.2f}% "
                f"卖{sell_volume}@{sell_price}"
            )
            
        # 更新策略的待执行列表和准备状态
        self._pending = pending
        self._prepared = True
        logger.info(f"[{self.name()}] 准备完成，最终待执行 {len(pending)} 笔")

    # ---------- 执行（并发抢卖）----------

    def _execute(self):
        orders = list(self._pending)
        self._pending.clear()
        if not orders:
            return

        with ThreadPoolExecutor(max_workers=len(orders), thread_name_prefix="OpenHighSell") as pool:
            futures = {pool.submit(self._execute_one, o): o for o in orders}
            for f in as_completed(futures):
                o = futures[f]
                try:
                    f.result()
                except Exception as e:
                    logger.error(f"[{self.name()}] 并发执行异常: {o.stock_code} - {e}", exc_info=True)
        logger.info(f"[{self.name()}] 执行完毕")

    def _execute_one(self, order: _PendingOrder):
        for attempt in range(1, self.MAX_RETRY + 1):
            # 重新算合法价（避免行情变化触碰笼子）
            quote = self._ctx.broker.get_stock_quote(order.stock_code)
            current_price = quote.get('last_price', 0) if quote else 0
            if current_price <= 0:
                current_price = order.sell_price
            sell_price = self._ctx.broker.calculate_price(order.stock_code, current_price, 'SELL')

            result = self._ctx.broker.sell(order.stock_code, order.volume, sell_price)
            if not result.success:
                logger.warning(f"[{self.name()}] 第{attempt}次发单失败: {order.stock_code} - {result.message}")
                _time.sleep(0.2)
                continue
            logger.info(f"[{self.name()}] 第{attempt}次发单: {order.stock_code} {order.volume}股@{sell_price} 委托号:{result.order_id}")

            _time.sleep(self.CHECK_INTERVAL)
            status = self._get_order_status(result.order_id)

            # 完全成交直接返回并推送消息
            if status == 'FILLED':
                self._notify_filled(order, sell_price, order.volume)
                return

            # 部分成交处理：多等一会儿看看能不能全成
            if status == 'PARTIAL_FILLED':
                logger.info(f"[{self.name()}] 部分成交中: {order.stock_code}，等待剩余成交")
                _time.sleep(2.0)
                if self._get_order_status(result.order_id) == 'FILLED':
                    self._notify_filled(order, sell_price, order.volume)
                    return

            # 废单自愈处理：如果是废单，无需撤单操作，不进行睡眠等待，直接进行下一次尝试！
            if status == 'REJECTED':
                logger.warning(f"[{self.name()}][废单提示] 第{attempt}次发单被柜台拒绝成为废单，零延迟直接进入下一次挂单重试！")
                continue

            # 走到这里，说明是未成交或部分成交，且等了也没成完。发起撤单。
            logger.info(f"[{self.name()}] 准备撤单: {order.stock_code} 委托号:{result.order_id}")
            self._ctx.broker.cancel_and_wait(result.order_id, timeout_seconds=3.0)
            
            # 风控(防撤单滑点): 撤单后强制获取最终成交量，防止在途期间发生额外成交导致剩余数量算错
            final_order = self._ctx.broker.get_order(result.order_id)
            if not final_order:
                continue
                
            cancel_status = normalize_status(final_order, is_buy=False)
            if cancel_status not in ('CANCELED', 'FILLED'):
                msg = f"⚠️ 高开止盈撤单未确认: {order.stock_code}，请手动处理"
                logger.warning(msg)
                self._ctx.notifier.push("WARNING", msg)
                return
                
            traded = final_order.traded_volume
            if traded > 0:
                self._notify_filled(order, sell_price, traded)
                order.volume -= traded
                
            if order.volume <= 0:
                return

        msg = f"⚠️ 高开止盈重试{self.MAX_RETRY}次后仍有未成交: {order.stock_code} (剩余{order.volume}股未卖出)"
        logger.warning(msg)
        self._ctx.notifier.push("WARNING", msg)

    def _notify_filled(self, order: _PendingOrder, price: float, volume: int):
        # 消息格式统一规范：✅ [高开止盈] 股票代码 卖出 股数@价格元 涨幅=百分比%
        # 例如：✅ [高开止盈] sz000001 卖出 500股@10.80元 涨幅=8.50%
        msg = f"✅ [高开止盈] {order.stock_code} 卖出 {volume}股@{price:.2f}元 涨幅={order.open_chg:.2f}%"
        logger.info(msg)
        self._ctx.notifier.push("INFO", msg)

    def _get_order_status(self, order_id) -> str:
        o = self._ctx.broker.get_order(order_id)
        if o:
            return normalize_status(o, is_buy=False)
        return 'UNKNOWN'
