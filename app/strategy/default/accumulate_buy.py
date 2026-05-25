#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分档累积买入策略（功能完整默认实现）

核心思路：
- 信号到达时按当日涨幅分流：
  - 涨幅 ≤ ONESHOT_THRESHOLD（默认 2.5%）：一波流，积极重试买到为止
  - 涨幅 ≤ ACCUM_THRESHOLD（默认 4.0%）：首笔保底仓，进入累积模式等回撤加仓
  - 涨幅 > ACCUM_THRESHOLD：等回落，回到 3.5% 以内再进入累积
- 累积模式按"模拟均价档位表"控制单笔金额和总额上限，加仓需当前价 ≤ 上次买价 × 回撤比例
- 下单带撤单重挂：发单 → 等撮合 → 未成交撤单 → 等股份解冻 → 重挂

状态机：INIT → (DONE | WATCHING | ACCUMULATING) → DONE
"""

import logging
import time as _time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from app.config import Config
from app.strategy.default.stg_config import StgConfig
from app.strategy.buy_strategy import BuyStrategy, BuyTask, StrategyContext, OrderTracker
from app.strategy.common import latency_suffix, normalize_status, round_half_up

logger = logging.getLogger(__name__)


# ==================== 数据传输对象 (DTO) ====================

@dataclass
class TradeDirective:
    """发单指令，封装离散的发单参数"""
    amount: float
    price: float
    chg: float
    max_retry: int = 0
    use_premium: bool = True


@dataclass
class FillContext:
    """成交记录上下文，封装离散的成交参数"""
    volume: int
    price: float
    buy_price: float
    chg: float
    attempt: int = 1
    suffix: str = ""
    order_id: Optional[int] = None
    submit_time: float = 0.0


# ==================== 私有状态（存 task.data['_state']）====================

@dataclass
class BuyOrderTracker(OrderTracker):
    """买入策略专用的订单生命周期追踪子类"""
    cancel_time: float = 0.0      # 撤单时间戳
    submit_chg: float = 0.0       # 下单时的涨幅


@dataclass
class _AccState:
    """累积模式状态。存在 task.data['_state']"""
    buy_count: int = 0
    total_bought: float = 0.0          # 已买入总金额
    last_buy_price: float = 0.0
    chg_history: List[float] = field(default_factory=list)
    order_ids: List[int] = field(default_factory=list)
    order_trackers: dict = field(default_factory=dict)  # 委托ID -> BuyOrderTracker 映射 (order_id -> BuyOrderTracker)
    drop_count: int = 0                            # 丢单超时次数统计（用于触发熔断防无限超买）
    allowed_drops: int = 0                         # 当前挂单允许的最大丢单次数（根据入口不同动态赋值）


def _state(task: BuyTask) -> _AccState:
    s = task.data.get('_state')
    if s is None:
        s = _AccState()
        task.data['_state'] = s
    return s


# ==================== 工具 ====================

def _get_tier(avg_chg: float) -> Optional[Tuple[int, int]]:
    """根据模拟均价涨幅查档位，返回 (单笔金额, 总额上限) 或 None（不买）"""
    for upper, step_amount, cap in StgConfig.BUY_ACCUM_TIERS:
        if avg_chg <= upper:
            return step_amount, cap
    return None


def _calc_avg_chg(history: List[float], new_chg: float) -> float:
    """加上新一笔后的算术均价涨幅"""
    return (sum(history) + new_chg) / (len(history) + 1)


# ==================== 策略 ====================

class AccumulateBuyStrategy(BuyStrategy):
    """分档累积买入"""

    def should_accept(self, code) -> bool:
        # 创业板默认不买
        if code.startswith("sz30"):
            return False
        return True

    # ---------- 状态机入口 ----------

    def on_recover(self, task: BuyTask, ctx: StrategyContext) -> None:
        """
        重启状态重构 (State Reconstruction)
        被 BuyEngine 在启动时调用，用于根据柜台真实的成交记录逆推出策略崩溃前的 _AccState。
        """
        logger.info(f"[重构] 开始为 {task.code} 重构累积买入状态")
        
        s = _state(task)
        orders = ctx.broker.get_orders()
        
        # 提取今日所有属于该票的买入订单（有真实成交或当前仍处于活跃状态）
        my_orders = [o for o in orders if o.stock_code == task.qmt_code and o.is_buy and (o.traded_volume > 0 or o.is_active())]
        
        if not my_orders:
            logger.info(f"[重构] {task.code} 尚未有任何成交，以全新状态开始首评估")
            # 转交正常的 on_init 进行首评估
            self.on_init(task, ctx)
            return
            
        # 逆推 _AccState
        total_spent = 0.0
        s.buy_count = len(my_orders)
        s.order_ids = [o.order_id for o in my_orders]
        
        for o in my_orders:
            fill_price = o.traded_price if o.traded_price > 0 else o.price
            total_spent += o.traded_volume * fill_price
            s.last_buy_price = fill_price
            # 近似用 0 填充涨幅历史，只要数量对齐即可
            s.chg_history.append(0.0)
            
            # 为已成订单重建 tracker（避免后续 _handle_accumulating 检测异常）
            # 设置 notified_volume = o.traded_volume 表示这部分历史成交已通知过，不再重复弹窗
            tracker = BuyOrderTracker(submit_time=_time.time(), submit_chg=0.0)
            tracker.notified_volume = o.traded_volume
            s.order_trackers[o.order_id] = tracker
            
        s.total_bought = total_spent
        
        logger.info(f"[重构] {task.code} 恢复记忆: 已买 {s.buy_count} 笔，累计金额 {total_spent:.2f}元")
        
        # 判决恢复后的流转方向
        if s.buy_count >= getattr(StgConfig, 'BUY_MAX_COUNT', 999):
            logger.info(f"[重构结束] {task.code} 累计成交次数已达上限 {getattr(StgConfig, 'BUY_MAX_COUNT', 999)} 次")
            task.phase = "DONE"
            return
            
        if hasattr(StgConfig, 'BUY_ACCUM_TIERS') and StgConfig.BUY_ACCUM_TIERS:
            max_cap = max(t[2] for t in StgConfig.BUY_ACCUM_TIERS)
            if s.total_bought >= max_cap:
                logger.info(f"[重构结束] {task.code} 实际已买金额 {s.total_bought:.2f} >= 档位最大上限 {max_cap}")
                task.phase = "DONE"
                return

        # 额度未满，恢复为累积盯盘模式，继续等待回撤加仓
        task.phase = "ACCUMULATING"

    def on_init(self, task: BuyTask, ctx: StrategyContext) -> None:
        if self._past_deadline(ctx):
            task.phase = "DONE"
            return

        quote = ctx.broker.get_stock_quote(task.qmt_code)
        if not quote:
            logger.warning(f"[行情失败] {task.code} 拿不到行情，等下一轮tick重试")
            return

        chg = quote['change_pct']
        price = quote['last_price']
        logger.info(f"[评估] {task.code} 现价={price} 涨幅={chg:.2f}%")

        # 一波流：涨幅 ≤ 阈值1，积极重试（在子线程中执行）
        if chg <= StgConfig.BUY_ONESHOT_THRESHOLD:
            logger.info(f"[分流→一波流] {task.code} 涨幅{chg:.2f}%≤{StgConfig.BUY_ONESHOT_THRESHOLD}%")
            
            s = _state(task)
            # 风控(一波流大单): 一波流大单入口资金敞口大，采用 0 容错机制，一旦发生丢单直接熔断，不允许重试
            s.allowed_drops = 0  
            
            directive = TradeDirective(
                amount=StgConfig.BUY_ONESHOT_AMOUNT,
                price=price,
                chg=chg,
                max_retry=3,
                use_premium=True
            )
            self._do_buy(task, ctx, directive)
            
            # 计算一波流的实际成交金额
            s = _state(task)
            total_spent = 0.0
            
            # 注意：此处必须实时查询柜台委托列表，以获取包含同步等待期间发生的最新成交数据
            for o in ctx.broker.get_orders():
                if o.order_id in s.order_ids:
                    total_spent += o.traded_volume * o.traded_price
            
            # 若一波流未能吃足额度（例如达到指定比例以上），自动降级为累积模式在后续回撤中继续吸筹
            if total_spent >= StgConfig.BUY_ONESHOT_AMOUNT * StgConfig.BUY_ONESHOT_FILL_RATIO:
                logger.info(f"[一波流足额买入] {task.code} 实际买入 {total_spent:.2f} 元，符合预期")
                task.phase = "DONE"
            else:
                logger.info(f"[一波流未买足降级] {task.code} 实际仅买入 {total_spent:.2f} 元，自动降级为累积模式")
                task.phase = "ACCUMULATING"
            return

        # 首笔保底仓 + 进入累积模式：涨幅 ≤ 阈值2
        if chg <= StgConfig.BUY_ACCUM_THRESHOLD:
            tier = _get_tier(chg)
            if tier:
                step_amount, _ = tier
                logger.info(f"[分流→累积首笔] {task.code} 涨幅{chg:.2f}% 首笔{step_amount}元")
                
                s = _state(task)
                # 风控(累积小单): 资金敞口较小，允许配置一定次数（默认1次）的丢单自愈重试
                s.allowed_drops = getattr(StgConfig, 'BUY_MAX_DROPS', 1)
                
                # 异步佛系化首笔买入：直接发单不重试也不阻塞等待 (max_retry=0, use_premium=True)
                directive = TradeDirective(amount=step_amount, price=price, chg=chg, max_retry=0, use_premium=True)
                self._do_buy(task, ctx, directive)
                task.phase = "ACCUMULATING"
            else:
                task.phase = "WATCHING"
            return

        # 涨幅 > 阈值2 → 等回落
        logger.info(f"[分流→等回落] {task.code} 涨幅{chg:.2f}%>{StgConfig.BUY_ACCUM_THRESHOLD}%")
        task.phase = "WATCHING"

    def on_tick(self, task: BuyTask, ctx: StrategyContext) -> None:
        if self._past_deadline(ctx):
            task.phase = "DONE"
            return
        if task.phase == "WATCHING":
            self._handle_watching(task, ctx)
        elif task.phase == "ACCUMULATING":
            self._handle_accumulating(task, ctx)

    # ---------- 阶段处理 ----------

    def _handle_watching(self, task: BuyTask, ctx: StrategyContext) -> None:
        """等回落到 BUY_FALLBACK_CHG 以内"""
        quote = ctx.broker.get_stock_quote(task.qmt_code)
        if not quote:
            return
        chg = quote['change_pct']
        price = quote['last_price']
        if chg > StgConfig.BUY_FALLBACK_CHG:
            return  # 继续等
        tier = _get_tier(chg)
        if not tier:
            return
        step_amount, _ = tier
        
        s = _state(task)
        # 风控(累积小单): 允许设定次数的丢单自愈重试
        s.allowed_drops = getattr(StgConfig, 'BUY_MAX_DROPS', 1)
        
        # 异步佛系化首单买入 (max_retry=0, use_premium=True)
        directive = TradeDirective(amount=step_amount, price=price, chg=chg, max_retry=0, use_premium=True)
        self._do_buy(task, ctx, directive)
        task.phase = "ACCUMULATING"

    def _handle_accumulating(self, task: BuyTask, ctx: StrategyContext) -> None:
        """累积加仓（真实成交驱动）"""
        s = _state(task)

        # 统计当前该任务真实的成交股份与成交金额，并检测新成交进行推送
        total_shares = 0
        total_spent = 0.0
        
        # 性能优化与并发安全：由于买入任务可能是并发且动态变化的，我们绝不能跨任务或跨时间使用陈旧缓存。
        # 但为了避免同一方法内部多次发起冗余 I/O，在此处拉取一次绝对实时的全量快照，供本次状态评估使用。
        orders_list = ctx.broker.get_orders()
            
        for o in orders_list:
            if o.order_id in s.order_ids:
                total_shares += o.traded_volume
                fill_price = o.traded_price if o.traded_price > 0 else o.price
                total_spent += o.traded_volume * fill_price
                
                # 检测是否有新的成交需要推送通知
                tracker = s.order_trackers.get(o.order_id)
                if tracker:
                    last_notified = tracker.notified_volume
                    if o.traded_volume > last_notified:
                        newly_filled = o.traded_volume - last_notified
                        tracker.notified_volume = o.traded_volume
                        order_chg = tracker.submit_chg
                        msg = (f"✅ [买入成交] {task.code} 买入 {newly_filled}股 @ {fill_price:.2f} "
                               f"({order_chg:+.2f}%) [{o.traded_volume}/{o.order_volume}]")
                        logger.info(msg + f" 委托号={o.order_id}" + latency_suffix(tracker.submit_time, active_check_order=o))
                        ctx.notifier.push("INFO", msg)

        # 同步回状态，确保外部展现和策略状态一致
        s.total_bought = total_spent

        # 获取最新行情以进行价格和档位校验
        quote = ctx.broker.get_stock_quote(task.qmt_code)
        if not quote:
            return

        chg = quote['change_pct']
        price = quote['last_price']

        # 严格加仓前置状态检查与柜台同步检查
        if s.order_ids:
            last_order_id = s.order_ids[-1]
            
            # 从本次实时快照中提取最后一个订单的状态，避免使用 ctx.broker.get_order 导致二次全量查询
            last_order = next((o for o in orders_list if o.order_id == last_order_id), None)
            
            # 风控(等订单同步): 如果最新一笔订单还没出现在 QMT 订单列表中，且未超过同步超时时间，必须等待
            if last_order is None:
                tracker = s.order_trackers.get(last_order_id)
                elapsed = _time.time() - (tracker.submit_time if tracker else 0.0)
                if elapsed < StgConfig.BUY_SYNC_TIMEOUT:
                    logger.warning(f"[等同步] {task.code} 最新委托 {last_order_id} 尚未同步 (已等 {elapsed:.1f}s)，暂不处理")
                    return
                else:
                    # 风控(丢单超买熔断): QMT若发生持续订单同步延迟，无脑剔除重发会导致无限超买打光可用资金，此处严格阻断无限循环重试
                    s.drop_count += 1
                    if s.drop_count > s.allowed_drops:
                        msg = (f"🚨 [等同步超时] {task.code} 委托 {last_order_id} 连续丢单次数({s.drop_count})超过上限({s.allowed_drops})，"
                               f"为防由于系统延迟导致无限重复买入，触发熔断，强行停止该股票任务！")
                        logger.error(msg)
                        ctx.notifier.push("ERROR", msg)
                        task.phase = "DONE"
                        return
                    else:
                        msg = (f"⚠️ [等同步超时] {task.code} 最新委托 {last_order_id} 超过 {StgConfig.BUY_SYNC_TIMEOUT}s 未同步，"
                               f"判定为丢单。剔除记录并允许重试！")
                        logger.warning(msg)
                        ctx.notifier.push("WARNING", msg)
                        s.order_ids.remove(last_order_id)
                        s.order_trackers.pop(last_order_id, None)
                        return

            # 记录废单日志并从追踪列表中剥离（防止后续轮询重复打印），不中断策略让其自然流转到重试逻辑
            if last_order.is_rejected():
                tracker = s.order_trackers.get(last_order_id)
                latency_str = latency_suffix(tracker.submit_time if tracker else 0.0)
                logger.warning(f"[废单提示] {task.code} 委托号 {last_order_id} 被拒({last_order.status_msg or '未知'}), 已剥离重试{latency_str}")
                s.order_ids.remove(last_order_id)
                s.order_trackers.pop(last_order_id, None)
                # 剥离后让代码继续往下执行，实现无感自愈
                
            # 如果订单处于活跃状态（未结案），则拦截等待
            elif last_order.is_active():
                logger.info(f"[等成交] {task.code} 上一笔订单 {last_order_id} 仍在活跃状态，暂不加仓")
                return

        # 统计真实已成交次数与动态校准涨幅历史（过滤未成交订单）
        buy_count = 0
        real_chg_history = []
        for o in orders_list:
            if o.order_id in s.order_ids and o.traded_volume > 0:
                buy_count += 1
                tracker = s.order_trackers.get(o.order_id)
                tracker_chg = tracker.submit_chg if tracker else chg
                real_chg_history.append(tracker_chg)
        
        s.buy_count = buy_count
        s.chg_history = real_chg_history

        # 判断次数与总额是否超限（基于真实的已成交状态）
        if s.buy_count >= StgConfig.BUY_MAX_COUNT:
            logger.info(f"[累积结束] {task.code} 累计成交次数已达上限 {StgConfig.BUY_MAX_COUNT} 次")
            task.phase = "DONE"
            return

        # 计算下一笔预期的模拟均价并查询档位
        sim_avg = _calc_avg_chg(s.chg_history, chg)
        tier = _get_tier(sim_avg)
        if not tier:
            # 均价涨幅超限时，暂停加仓（但保留任务，继续等待价格回落）
            return

        step_amount, cap = tier
        if s.total_bought >= cap:
            logger.info(f"[累积结束] {task.code} 实际已买金额 {s.total_bought:.2f} >= 档位上限 {cap}")
            task.phase = "DONE"
            return

        # 回撤条件校验与下单
        if total_shares == 0:
            # 说明首笔完全没买到（已被撤销），这里重新尝试挂单买入首笔底仓
            logger.info(f"[无持仓补漏] {task.code} 当前无持仓，重新挂单买入首笔底仓")
            
            s.allowed_drops = getattr(StgConfig, 'BUY_MAX_DROPS', 1)
            directive = TradeDirective(amount=step_amount, price=price, chg=chg, max_retry=0, use_premium=True)
            self._do_buy(task, ctx, directive)
            return

        # 有底仓，计算当前实际加权均价
        avg_buy_price = round_half_up(total_spent / total_shares, 2)

        # 风控(防骗炮拦截): 价格跌破 guard_price 时判定为洗盘强砸，停止一切后续加仓，直接结束任务
        open_price = quote.get('open', 0.0)
        if open_price > 0:
            guard_price = round_half_up(open_price * StgConfig.BUY_FAKE_BREAKOUT_RATIO, 2)
            if price < guard_price:
                msg = (f"🚨 [防骗炮拦截] {task.code} 现价 {price} 已砸穿守卫线 {guard_price} (开盘价: {open_price})，"
                       f"停止一切后续加仓，直接结束任务")
                logger.warning(msg)
                ctx.notifier.push("WARNING", msg)
                task.phase = "DONE"
                return

        # 计算回撤买入价格线（加权持仓均价 * 回撤比例）
        pullback_target = round_half_up(avg_buy_price * StgConfig.BUY_PULLBACK_RATIO, 2)
        if price > pullback_target:
            return  # 未跌到加仓线，继续监控

        # 满足回撤条件，触发佛系限价挂单加仓 (max_retry=0, use_premium=False)
        logger.info(f"[触发加仓] {task.code} 均价={avg_buy_price} 目标={pullback_target} 当前价={price}")
        
        s.allowed_drops = getattr(StgConfig, 'BUY_MAX_DROPS', 1)
        directive = TradeDirective(amount=step_amount, price=price, chg=chg, max_retry=0, use_premium=False)
        self._do_buy(task, ctx, directive)

    # ---------- 下单（带撤单重挂）----------

    def _do_buy(self, task, ctx, directive: TradeDirective):
        """
        max_retry=3: 一波流，务必买到（积极重试，运行在独立信号线程中）
        max_retry=0: 佛系挂单不重试（首单/加仓，不阻塞主轮询）
        """
        # 使用本地副本以便在重试循环中安全修改，不污染原传入指令对象
        price = directive.price
        chg = directive.chg
        amount = directive.amount

        if price <= 0:
            logger.warning(f"[价格异常] {task.code} price={price}")
            return

        # 提取单股持仓天花板 (single_max)
        # 本策略配置使用绝对金额，单股上限 (single_max) 取 [一波流单笔金额] 与 [累积加仓各档位最大上限值] 之间的最大者。
        s = _state(task)
        max_cap_in_tiers = max([t[2] for t in StgConfig.BUY_ACCUM_TIERS]) if hasattr(StgConfig, 'BUY_ACCUM_TIERS') and StgConfig.BUY_ACCUM_TIERS else 0.0
        single_max = max(getattr(StgConfig, 'BUY_ONESHOT_AMOUNT', 0.0), max_cap_in_tiers)

        # =====================================================================
        # 🚨🚨🚨 [防呆安全总控 - 核心资金安全锁] 🚨🚨🚨
        # 全局硬安全闸限制，防止由于配置错误或手抖导致单股持仓过大爆仓。
        # 【核心风险提示】如您已完全知晓交易后果、自担实盘风险且确有需要突破限制，
        # 请手动注释或删除下方整段 if 拦截代码（本限制无配置项，仅能在源码中解除）。
        # =====================================================================
        safety_limit = 5000.0
        if single_max > safety_limit:
            logger.error("\n"
                         "========================================================================\n"
                         "🚨🚨🚨 [安全拦截 - 核心资金安全锁触发] 🚨🚨🚨\n"
                         f"当前单股最大持仓预算为 {single_max:.0f}元，已超过默认安全阈值 {safety_limit:.0f}元！\n"
                         "为防止因参数误配置或手抖导致大资金梭哈，已拒绝本次下单交易。\n"
                         "【免责声明】如您已完全知晓交易后果、自担实盘盈亏且确需突破限制：\n"
                         "请直接编辑策略源文件: app/strategy/default/accumulate_buy.py\n"
                         "手动注释或删除此处的 `if single_max > safety_limit:` 整个拦截代码段。\n"
                         "========================================================================")
            return

        # 终极风控：如果本次下单导致单股总额超上限，则进行调整或拦截
        if s.total_bought + amount > single_max:
            allowed_amount = max(single_max - s.total_bought, 0.0)
            if allowed_amount < 100 * price:  # 连一手都买不起
                logger.warning(f"[超限拦截] {task.code} 剩余额度 {allowed_amount:.0f}元 不足一手")
                return
            logger.info(f"[超限调整] {task.code} 计划 {amount:.0f}元 -> 调整为 {allowed_amount:.0f}元 (上限 {single_max:.0f}元)")
            amount = allowed_amount
            directive.amount = allowed_amount

        account = ctx.broker.get_account_info()
        if not account or account.cash < amount:
            logger.warning(f"[资金不足] {task.code} 需要{amount}")
            return

        # 佛系：挂一笔就走（使用指定的 use_premium 控制是否溢价）
        if directive.max_retry == 0:
            self._submit_and_record(task, ctx, directive, label="佛系")
            return

        remaining_amount = amount
        for attempt in range(1, directive.max_retry + 1):
            # 重试时刷新行情
            if attempt > 1:
                quote = ctx.broker.get_stock_quote(task.qmt_code)
                if not quote or quote['last_price'] <= 0:
                    return
                price = quote['last_price']
                chg = quote['change_pct']
                # 涨幅冲过追买限制退回佛系挂一笔
                if chg > StgConfig.BUY_ONESHOT_CHASE_LIMIT:
                    logger.info(f"[佛系挂单] {task.code} 涨幅{chg:.2f}%>{StgConfig.BUY_ONESHOT_CHASE_LIMIT}%，挂单不追")
                    # 构建新的佛系指令
                    fallback_directive = TradeDirective(amount=remaining_amount, price=price, chg=chg, max_retry=0, use_premium=True)
                    self._submit_and_record(task, ctx, fallback_directive, label="冲高")
                    return

            buy_price = ctx.broker.calculate_price(task.qmt_code, price, 'BUY', use_premium=True)
            if remaining_amount < buy_price * 100:
                logger.info(f"[一波流重试] {task.code} 剩余待买额度 {remaining_amount:.0f}元 不足一手，终止")
                return
            volume = max(int(remaining_amount / buy_price / 100) * 100, 100)

            result = ctx.broker.buy(task.qmt_code, volume, buy_price)
            if not result.success:
                logger.warning(f"[发单失败] {task.code} 第{attempt}次 - {result.message}")
                _time.sleep(0.2)
                continue

            # 只要下单成功，立刻记入订单列表 and 发单涨幅记录
            s = _state(task)
            if result.order_id not in s.order_ids:
                s.order_ids.append(result.order_id)
            s.order_trackers[result.order_id] = BuyOrderTracker(submit_time=_time.time(), submit_chg=chg)

            # 等撮合
            _time.sleep(1.0)
            status = self._get_order_status(ctx, result.order_id)
            logger.info(f"[等撮合] {task.code} 委托号={result.order_id} 状态={status}")
            if status == 'FILLED':
                tracker = s.order_trackers.get(result.order_id)
                submit_time = tracker.submit_time if tracker else 0.0
                fill_ctx = FillContext(volume=volume, price=price, buy_price=buy_price, 
                                       chg=chg, attempt=attempt, order_id=result.order_id, submit_time=submit_time)
                self._record_filled(task, ctx, fill_ctx)
                return

            if status == 'PARTIAL_FILLED':
                _time.sleep(1.5)
                status = self._get_order_status(ctx, result.order_id)
                logger.info(f"[部成等全成] {task.code} 委托号={result.order_id} 状态={status}")
                if status == 'FILLED':
                    tracker = s.order_trackers.get(result.order_id)
                    submit_time = tracker.submit_time if tracker else 0.0
                    fill_ctx = FillContext(volume=volume, price=price, buy_price=buy_price, 
                                           chg=chg, attempt=attempt, order_id=result.order_id, submit_time=submit_time)
                    self._record_filled(task, ctx, fill_ctx)
                    return

            # 撤单重挂
            logger.info(f"[未成交撤单] {task.code} 第{attempt}次 委托号={result.order_id}")
            tracker = s.order_trackers.get(result.order_id)
            if tracker:
                tracker.cancel_time = _time.time()
            o = ctx.broker.cancel_and_wait(result.order_id, timeout_seconds=2.0)
            if o and o.traded_volume > 0:
                remaining_amount -= o.traded_volume * (o.traded_price if o.traded_price > 0 else buy_price)
            cancel_status = self._get_order_status(ctx, result.order_id)
            cancel_latency_str = latency_suffix(tracker.cancel_time if tracker else 0.0)
            if cancel_status not in ('CANCELED', 'FILLED'):
                logger.warning(f"[撤单未确认] {task.code}，停止重试{cancel_latency_str}")
                return
            # 撤单期间成交了
            if cancel_status == 'FILLED':
                tracker = s.order_trackers.get(result.order_id)
                submit_time = tracker.submit_time if tracker else 0.0
                fill_ctx = FillContext(volume=volume, price=price, buy_price=buy_price, 
                                       chg=chg, attempt=attempt, suffix="(撤单前成交)", 
                                       order_id=result.order_id, submit_time=submit_time)
                self._record_filled(task, ctx, fill_ctx)
                return

        logger.warning(f"⚠️ 买入重试{directive.max_retry}次未成交: {task.code}")

    def _submit_and_record(self, task, ctx, directive: TradeDirective, label="挂单"):
        """佛系：挂一笔成不成都不追"""
        buy_price = ctx.broker.calculate_price(task.qmt_code, directive.price, 'BUY', use_premium=directive.use_premium)
        volume = max(int(directive.amount / buy_price / 100) * 100, 100)
        result = ctx.broker.buy(task.qmt_code, volume, buy_price)
        if result.success:
            s = _state(task)
            s.buy_count += 1
            s.total_bought += volume * buy_price
            s.last_buy_price = directive.price  # 报单时的市场价作为初始参考
            s.chg_history.append(directive.chg)
            if result.order_id not in s.order_ids:
                s.order_ids.append(result.order_id)
            s.order_trackers[result.order_id] = BuyOrderTracker(submit_time=_time.time(), submit_chg=directive.chg)
            msg = (f"✅ 买入挂单[{label}]: {task.code} {volume}股@{buy_price} "
                   f"chg={directive.chg:.2f}% 第{s.buy_count}次 委托号={result.order_id}")
            logger.info(msg)
        else:
            logger.warning(f"⚠️ 买入失败: {task.code} - {result.message}")

    def _record_filled(self, task, ctx, fill: FillContext):
        s = _state(task)
        s.buy_count += 1
        s.total_bought += fill.volume * fill.buy_price
        s.last_buy_price = fill.price  # 报单时的市场价作为初始参考
        s.chg_history.append(fill.chg)
        if fill.order_id and fill.order_id not in s.order_ids:
            s.order_ids.append(fill.order_id)
        if fill.order_id:
            tracker = s.order_trackers.get(fill.order_id)
            if tracker:
                tracker.notified_volume = fill.volume
        msg = (f"✅ [买入成交] {task.code} 买入 {fill.volume}股 @ {fill.buy_price:.2f} "
               f"({fill.chg:+.2f}%) [{fill.volume}/{fill.volume}]")
        oid_str = f" 委托号={fill.order_id}" if fill.order_id else ""
        logger.info(msg + oid_str + latency_suffix(fill.submit_time))
        ctx.notifier.push("INFO", msg)

    # ---------- 辅助 ----------

    def _get_order_status(self, ctx, order_id) -> str:
        o = ctx.broker.get_order(order_id)
        if o:
            return normalize_status(o, is_buy=True)
        return 'UNKNOWN'

    def _past_deadline(self, ctx) -> bool:
        return ctx.now().time() >= Config.BUY_DEADLINE
