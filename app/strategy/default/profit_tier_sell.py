#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分档盈利止盈策略（功能完整默认实现）

核心思路：
- 9:30 后加载昨日持仓建任务表（排除今日买入和已卖出的）
- 每只票按 SELL_PROFIT_TIERS 配置分档卖出，例如：
    [(5.0, 0.5),    # 涨幅达 5%，卖 50%
     (8.0, 1.0)]    # 涨幅达 8%，卖剩余全部
- 最后一档兜底卖完所有剩余股数
- 每次卖出前实时查持仓，避免人工已卖导致超卖
"""

import logging
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from app.config import Config
from app.strategy.buy_strategy import StrategyContext, OrderTracker
from app.strategy.sell_strategy import SellStrategy
from app.strategy.common import latency_suffix
from .stg_config import StgConfig

logger = logging.getLogger(__name__)


# ==================== 数据传输对象 (DTO) ====================

@dataclass
class SellTickContext:
    """Tick 轮询级别的运行时上下文，打包该轮的内存快照数据"""
    positions: list
    orders: dict

@dataclass
class SellDirective:
    """发单指令，打包离散的发单参数"""
    tier_idx: int
    volume: int
    price: float
    chg: float
    pos: object


@dataclass
class _Task:
    stock_code: str
    total_volume: int
    open_price: float
    tier_volumes: List[int] = field(default_factory=list)
    tier_index: int = 0
    phase: str = "WATCHING"  # 观察中 / 已完成 (WATCHING / DONE)
    order_ids: List[int] = field(default_factory=list)
    tier_sold_volumes: List[int] = field(default_factory=list) # 分档索引 -> 该档位已成交的股数 (index -> sold_volume in this tier)
    tier_order_ids: dict = field(default_factory=dict)       # 分档索引 -> 委托ID (tier_index -> order_id)
    order_trackers: dict = field(default_factory=dict)       # 委托ID -> OrderTracker (order_id -> OrderTracker)






def _calc_tier_volumes(total: int, tiers: List[Tuple[float, float]]) -> List[int]:
    """
    预算每档卖出股数；最后一档兜底卖完。
    各档比例代表卖出当前剩余持仓的比例。
    """
    out = []
    remain = total
    for i, (_chg, ratio) in enumerate(tiers):
        is_last = (i == len(tiers) - 1)
        if is_last:
            vol = remain
        else:
            vol = int(remain * ratio) // 100 * 100
            vol = min(vol, remain)
            if vol < 100:
                vol = 100
        out.append(vol)
        remain -= vol
        if remain <= 0:
            break
    return out


class ProfitTierSellStrategy(SellStrategy):
    """分档盈利止盈"""

    def __init__(self):
        self._running = False
        self._tasks: Dict[str, _Task] = {}
        self._sold: Set[str] = set()
        self._initialized = False
        self._last_date = datetime.now().date()
        self._ctx: Optional[StrategyContext] = None

    # ---------- 生命周期 ----------

    def start(self, ctx: StrategyContext) -> None:
        self._ctx = ctx
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="ProfitTierSellWorker").start()
        logger.info(f"[{self.name()}] 已启动，轮询{StgConfig.SELL_POLL_INTERVAL}s")

    def stop(self) -> None:
        self._running = False

    def _loop(self):
        while self._running:
            _time.sleep(StgConfig.SELL_POLL_INTERVAL)
            try:
                self._tick()
            except Exception as e:
                logger.error(f"[{self.name()}] tick 异常: {e}", exc_info=True)
            
            # 优化(自毁退出): 一旦当轮 tick 推进完毕且发现今日所有持仓已卖完/无可用任务，立刻自毁退出，避免多休眠一个轮询周期
            if self._initialized and not self._tasks:
                logger.info(f"[{self.name()}] 无可用持仓，退出轮询")
                self._running = False
                break

    # ---------- 主流程 ----------

    def _tick(self):
        # 非交易时段早返回
        if not Config.in_trading_window():
            return
        now = datetime.now()
        # 跨日重置
        if now.date() != self._last_date:
            self._tasks.clear()
            self._sold.clear()
            self._initialized = False
            self._last_date = now.date()

        if now.time() < StgConfig.SELL_PROFIT_START_TIME:
            return

        if not self._initialized:
            self._clean_dangling_orders()
            self._load_positions()
            # 风控(初始化连接校验): 只有当 QMT 成功返回数据时才标为初始化完成。
            # 若因闪断或未启动导致加载为空，保持 False 以便在下个周期重试，防止线程误判自毁。
            if self._ctx.broker.is_connected:
                self._initialized = True

        if not self._tasks:
            return

        # 性能(持仓查询优化): 在整轮 tick 开始前只查询一次柜台持仓列表和订单列表，避免在循环中高频重复调用，降低柜台负载
        positions = self._ctx.broker.get_positions()
        
        # 风控(接口闪断保护): 如果在 tick 头部获取到的持仓列表为空，且 broker 状态断开，说明是接口超时等异常，不应误判为已卖光，直接跳过本轮
        if not self._ctx.broker.is_connected:
            logger.warning(f"[{self.name()}] 检测到 QMT 处于断开状态，跳过本轮持仓同步")
            return

        orders_list = self._ctx.broker.get_orders()
        orders = {o.order_id: o for o in orders_list}

        tctx = SellTickContext(positions=positions, orders=orders)

        for task in list(self._tasks.values()):
            if task.phase == "DONE":
                continue
            try:
                self._process(task, tctx)
            except Exception as e:
                logger.error(f"[{self.name()}] 任务异常 {task.stock_code}: {e}", exc_info=True)

        # 清理
        for code in [c for c, t in list(self._tasks.items()) if t.phase == "DONE"]:
            self._tasks.pop(code, None)

    def _clean_dangling_orders(self):
        # 盘中启动时主动清理遗留的活跃卖单（Clean Slate 机制）
        # 确保本策略运行前不会受到前次崩溃遗留的挂单或手工挂单导致的资金冻结影响，实现状态的独占接管
        if not self._ctx.broker.is_connected:
            return
            
        positions = self._ctx.broker.get_positions()
        target_codes = {p.stock_code for p in positions if p.previous_volume > 0}
        if not target_codes:
            return
            
        orders = self._ctx.broker.get_orders()
        cancel_count = 0
        for o in orders:
            if not o.is_buy and o.is_active() and o.stock_code in target_codes:
                logger.info(f"[{self.name()}] 盘中启动清理遗留卖单: {o.stock_code} {o.order_id}")
                self._ctx.broker.cancel(o.order_id)
                cancel_count += 1
                
        if cancel_count > 0:
            logger.info(f"[{self.name()}] 已发出 {cancel_count} 笔撤单请求，短暂休眠等待持仓解冻...")
            _time.sleep(1.0)

    def _load_positions(self):
        positions = self._ctx.broker.get_positions()
        tiers = StgConfig.SELL_PROFIT_TIERS
        for pos in positions:
            # 过滤掉非昨日持仓的股票（即昨日持仓量为 0 的股票）
            if pos.previous_volume <= 0:
                continue
            if pos.stock_code in self._sold:
                continue
            if pos.stock_code in self._tasks:
                continue
                
            # 基于昨日总持仓规划各档位应卖股数（目标配额）
            vols = _calc_tier_volumes(pos.previous_volume, tiers)
            
            # 【无状态断点续传核心】
            # 计算今日真实已成交股数 (Source of Truth: 券商实际持仓差异)
            # 说明：`previous_volume` 是固定不变的昨日收盘持仓，`volume` 是今日实时总持仓
            # 必须用 `pos.volume` 相减，绝不能用 `pos.can_use_volume`，因为挂单未成交的冻结股数不能算作已卖出
            real_sold = pos.previous_volume - pos.volume
            
            # 【倒水算法：记忆恢复】
            # 将真实已卖掉的股数（不论是上次崩溃前算法卖出的，还是人工干预卖出的）
            # 从第 0 档开始，依次向后“倒水”填满，以此推演出程序当前所处的止盈档位和剩余待卖数量
            tier_sold_vols = [0] * len(vols)
            if real_sold > 0:
                remain_sold = real_sold
                for i in range(len(vols)):
                    if remain_sold <= 0:
                        break
                    # 把剩余的已卖股数尽量填入当前档位
                    fill = min(remain_sold, vols[i])
                    tier_sold_vols[i] = fill
                    remain_sold -= fill

            self._tasks[pos.stock_code] = _Task(
                stock_code=pos.stock_code,
                total_volume=pos.previous_volume,
                open_price=pos.open_price,
                tier_volumes=vols,
                tier_sold_volumes=tier_sold_vols,
            )
        if self._tasks:
            logger.info(f"[{self.name()}] 加载持仓完成，共 {len(self._tasks)} 只")

    # ---------- 单 task 推进 ----------

    def _process(self, task: _Task, tctx: SellTickContext):
        # 实时检测持仓是否已被手动/外部卖光，如果是则直接标记为 DONE 退出，防止后续轮询空转
        pos = next(
            (p for p in tctx.positions if p.stock_code == task.stock_code),
            None,
        )
        if not pos or pos.volume <= 0:
            logger.info(f"[{self.name()}] 确认 {task.stock_code} 在柜台已无持仓，自动结束任务")
            task.phase = "DONE"
            return

        # 获取最新行情以进行价格 and 涨幅校验
        quote = self._ctx.broker.get_stock_quote(task.stock_code)
        if not quote or quote['last_price'] <= 0:
            return
        chg = quote['change_pct']
        price = quote['last_price']
        
        # 遍历已发送的活跃订单
        for oid in list(task.order_ids):
            # 找到此订单对应的分档索引
            t_idx = None
            for idx, order_id in list(task.tier_order_ids.items()):
                if order_id == oid:
                    t_idx = idx
                    break
            
            if t_idx is None:
                task.order_ids.remove(oid)
                continue
                
            o = tctx.orders.get(oid)
            
            # 风控(等订单同步): 柜台未同步，需判断是否超时以防丢单
            if o is None:
                tracker = task.order_trackers.get(oid)
                elapsed = _time.time() - (tracker.submit_time if tracker else 0.0)
                if elapsed < StgConfig.SELL_SYNC_TIMEOUT:
                    logger.warning(f"[{self.name()}][等同步] {task.stock_code} 最新委托 {oid} 尚未同步 (已等 {elapsed:.1f}s)，暂不处理")
                    return
                else:
                    msg = (f"🚨 [{self.name()}][等同步超时] {task.stock_code} 委托超过 {StgConfig.SELL_SYNC_TIMEOUT}s 未同步，"
                           f"判定为丢单。自动清除并允许重新挂单！")
                    logger.warning(msg)
                    self._ctx.notifier.push("WARNING", msg)
                    task.order_ids.remove(oid)
                    task.tier_order_ids.pop(t_idx, None)
                    task.order_trackers.pop(oid, None)
                    return

            # 废单自愈处理
            if o.is_rejected():
                tracker = task.order_trackers.get(oid)
                latency_str = latency_suffix(tracker.submit_time if tracker else 0.0)
                logger.warning(f"[废单提示] {task.stock_code} 委托号 {oid} 被拒({o.status_msg or '未知'}), 已剥离重试{latency_str}")
                task.order_ids.remove(oid)
                task.tier_order_ids.pop(t_idx, None)
                task.order_trackers.pop(oid, None)
                continue

            # 增量成交检测与推送
            tracker = task.order_trackers.get(oid)
            if tracker:
                last_notified = tracker.notified_volume
                if o.traded_volume > last_notified:
                    newly_filled = o.traded_volume - last_notified
                    tracker.notified_volume = o.traded_volume
                    task.tier_sold_volumes[t_idx] += newly_filled
                    fill_price = o.traded_price if o.traded_price > 0 else o.price
                    # 消息格式统一规范：✅ [分档止盈] 股票代码 卖出 股数@单价元 涨幅=百分比%
                    # 例如：✅ [分档止盈] sz000001 卖出 500股@10.80元 涨幅=5.20%
                    msg = f"✅ [分档止盈] {task.stock_code} 卖出 {newly_filled}股@{fill_price:.2f}元 涨幅={chg:.2f}%"
                    latency_str = latency_suffix(tracker.submit_time, active_check_order=o)
                    logger.info(msg + latency_str)
                    self._ctx.notifier.push("INFO", msg)

            # 订单已结束（非活跃状态）
            if not o.is_active():
                task.order_ids.remove(oid)
                task.tier_order_ids.pop(t_idx, None)
                task.order_trackers.pop(oid, None)

        tiers = StgConfig.SELL_PROFIT_TIERS
        # 找当前涨幅命中的最高档
        hit_index = -1
        for i in range(len(tiers) - 1, -1, -1):
            if chg >= tiers[i][0]:
                hit_index = i
                break

        if hit_index < 0:
            return

        # 从第 0 档到 hit_index 档，逐档检查并补单
        for i in range(min(hit_index + 1, len(task.tier_volumes))):
            target_vol = task.tier_volumes[i]
            sold_vol = task.tier_sold_volumes[i]
            
            # 检查该档当前是否有活跃委托
            if i in task.tier_order_ids:
                continue  # 正在挂单中，等其结果
                
            if sold_vol >= target_vol:
                continue  # 该档已全部成交，过
                
            # 需要补单卖出该档的剩余数量
            remain_vol = target_vol - sold_vol
            directive = SellDirective(tier_idx=i, volume=remain_vol, price=price, chg=chg, pos=pos)
            self._do_sell_tier(task, directive)

        # 检查是否所有分档都已卖足
        all_done = True
        for i in range(len(task.tier_volumes)):
            if task.tier_sold_volumes[i] < task.tier_volumes[i]:
                all_done = False
                break
        if all_done:
            task.phase = "DONE"

    def _do_sell_tier(self, task: _Task, directive: SellDirective):
        # 性能(降频优化): 复用从 tick 头部传下来的 pos 引用，避免内部高频查 QMT 持仓。
        # 发单后立刻扣减本地可用数量以防同一 tick 触发多档导致超卖
        pos = directive.pos
        # 1. 柜台确实完全没有持仓了，将其标为完成以防死循环
        if not pos or pos.volume <= 0:
            logger.info(f"[{self.name()}] {task.stock_code} 已无可用持仓，跳过分档 {directive.tier_idx + 1}")
            task.tier_sold_volumes[directive.tier_idx] = task.tier_volumes[directive.tier_idx]
            return
            
        # 2. 还有昨日持仓，但当前因为挂单被锁定，暂时跳过等待解冻而不判定为完成
        if pos.can_use_volume <= 0:
            logger.info(f"[{self.name()}] {task.stock_code} 可用持仓不足（可能处于锁定状态），等待解冻...")
            return
            
        actual = min(directive.volume, pos.can_use_volume)
        is_last_tier = (directive.tier_idx == len(task.tier_volumes) - 1)
        
        # 风控(防连带抛售): 只有在总持仓不足1手，或者当前是最后一档且卖出后剩余不足1手时，才允许把碎股全卖
        if pos.can_use_volume < 100:
            actual = pos.can_use_volume
        elif is_last_tier and (pos.can_use_volume - actual) < 100:
            actual = pos.can_use_volume
        else:
            # 否则，严格向下取整到 100 的整数倍。
            # 如果取整后为 0，说明不足 1 手，此时直接 return 不卖，将其自然顺延合并到更高的档位去卖出。
            actual = int(actual / 100) * 100
            
        if actual <= 0:
            return

        sell_price = self._ctx.broker.calculate_price(task.stock_code, directive.price, 'SELL')
        if sell_price <= 0:
            return
            
        result = self._ctx.broker.sell(task.stock_code, actual, sell_price)
        if result.success:
            self._sold.add(task.stock_code)
            task.order_ids.append(result.order_id)
            task.tier_order_ids[directive.tier_idx] = result.order_id
            task.order_trackers[result.order_id] = OrderTracker(submit_time=_time.time())
            
            # 手动扣减可用资金以供同一 tick 里的后续分档判断，完美消灭内部查询
            pos.can_use_volume -= actual
            
            # 只记录订单信息，不发送挂单通知
            pass
        else:
            logger.warning(f"[{self.name()}] 发单失败: {task.stock_code} - {result.message}")
