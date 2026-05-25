#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
买入调度骨架（框架层）

只负责：
- 接收信号入队
- 消费者派发到信号池（新信号立即并行首评估）
- ticker 周期串行扫存量任务
- 任务表去重、daily reset、deadline 收尾

不做任何买入决策，决策全部委托 BuyStrategy 实现。
"""

import logging
import queue
import threading
import time as _time
import os
import json
import glob
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, Optional

from app.config import Config
from app.strategy.buy_strategy import BuyStrategy, BuyTask, StrategyContext

logger = logging.getLogger(__name__)


def _convert_code(code: str) -> str:
    """sz000001 -> 000001.SZ，sh600001 -> 600001.SH"""
    return f"{code[2:]}.{code[:2].upper()}"


class BuyEngine:
    """买入调度骨架"""

    def __init__(self, ctx: StrategyContext):
        """
        :param ctx: 策略上下文（broker / notifier / config）
        """
        self.ctx = ctx
        # 注册的策略：name -> BuyStrategy。第一版只跑 default。
        self._strategies: Dict[str, BuyStrategy] = {}
        # 信号队列：(code, payload, strategy_name)
        self._signal_queue: "queue.Queue[Optional[tuple]]" = queue.Queue(
            maxsize=Config.BUY_QUEUE_MAXSIZE
        )
        # 任务表：每个策略一份，避免不同策略的 code 冲突
        # 结构：{strategy_name: {code: BuyTask}}
        self._tasks: Dict[str, Dict[str, BuyTask]] = {}
        # 今日已完成（去重，避免重复信号）：{strategy_name: set[code]}
        self._today_done: Dict[str, set] = {}
        self._last_date = datetime.now().date()
        self._running = False
        self._new_signal_logged = False

        self._signal_pool = ThreadPoolExecutor(
            max_workers=Config.BUY_SIGNAL_POOL_SIZE,
            thread_name_prefix="BuySignal",
        )

    # ==================== 注册 / 启停 ====================

    def register(self, strategy: BuyStrategy, name: Optional[str] = None) -> None:
        """注册一个买入策略。name 缺省用 strategy.name()。"""
        key = name or strategy.name()
        self._strategies[key] = strategy
        self._tasks.setdefault(key, {})
        self._today_done.setdefault(key, set())
        logger.info(f"已注册买入策略: {key}")

    def start(self) -> None:
        if not self._strategies:
            logger.warning("没有注册任何买入策略，BuyEngine 不启动")
            return
            
        # 引擎启动前置拦截：触发日抛型垃圾回收，清理非今日的过期 WAL 日志，保持磁盘绝对纯净
        self._cleanup_old_wals()
        
        # 引擎核心防断点接管：接管并逆推崩溃前留在内存中的待买信号，同时进行主动撤单防重买保护
        self._recover_state()
        
        self._running = True
        threading.Thread(target=self._consume_loop, daemon=True, name="BuySignalConsumer").start()
        threading.Thread(target=self._tick_loop, daemon=True, name="BuyTaskTicker").start()
        logger.info(
            "买入引擎已启动，策略=%s 轮询=%ds",
            list(self._strategies.keys()), Config.BUY_POLL_INTERVAL,
        )

    def stop(self) -> None:
        self._running = False
        self._signal_pool.shutdown(wait=False)

    # ==================== 信号入口 ====================

    def submit(self, code: str, payload: dict = None, strategy_name: str = "default") -> bool:
        """
        提交信号到指定策略。
        默认 strategy_name='default'，等同于早期单策略行为。
        """
        # 买入开关
        if not Config.BUY_ENABLED:
            logger.info(f"买入已关闭，丢弃信号: {code}")
            return False
        # 非交易时段直接丢弃，避免信号挂在队列里到次日错误执行
        if not Config.in_trading_window():
            logger.warning(f"非交易时段，丢弃信号: {code} (策略={strategy_name})")
            return False
        # 过了信号截止时间，不再接受新信号（新标的）
        if datetime.now().time() >= Config.NEW_SIGNAL_DEADLINE:
            logger.info(f"已过信号截止时间({Config.NEW_SIGNAL_DEADLINE})，丢弃新信号: {code}")
            return False
        if strategy_name not in self._strategies:
            logger.error(f"未知策略 {strategy_name}，丢弃信号: {code}")
            return False
        try:
            self._signal_queue.put_nowait((code, payload, strategy_name))
            return True
        except queue.Full:
            logger.error(f"买入信号队列已满，丢弃: {code}")
            return False

    # ==================== 消费者 ====================

    def _consume_loop(self):
        """消费者：取出信号 → 派发到信号池并行处理（首评估）"""
        while self._running:
            try:
                item = self._signal_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                continue
            code, payload, strategy_name = item
            self._signal_pool.submit(self._safe_handle_signal, code, payload, strategy_name)

    def _safe_handle_signal(self, code, payload, strategy_name):
        try:
            self._handle_signal(code, payload, strategy_name)
        except Exception as e:
            logger.error(f"处理信号异常 {code}@{strategy_name}: {e}", exc_info=True)

    def _handle_signal(self, code, payload, strategy_name):
        self._check_daily_reset()
        strategy = self._strategies[strategy_name]
        # 用户级过滤
        if not strategy.should_accept(code):
            logger.info(f"[{strategy_name}] 信号被策略过滤: {code}")
            return
        tasks = self._tasks[strategy_name]
        done = self._today_done[strategy_name]
        # 框架级去重
        if code in tasks or code in done:
            logger.info(f"[{strategy_name}] 信号忽略(已存在或今日已完成): {code}")
            return
        task = BuyTask(
            code=code,
            qmt_code=_convert_code(code),
            enter_time=datetime.now(),
        )
        if payload:
            task.data.update(payload)
        tasks[code] = task
        logger.info(f"[{strategy_name}] 信号入任务表: {code} payload={payload}")
        
        # 追加 WAL (必须在放入任务表后记录意图)
        self._append_wal("ADD", code, strategy_name, payload, task.enter_time.timestamp())
        
        # 立即首评估
        try:
            strategy.on_init(task, self.ctx)
        except Exception as e:
            logger.error(f"[{strategy_name}] on_init 异常 {code}: {e}", exc_info=True)
        # 评估完如果已 DONE，移到完成表
        if task.is_done():
            tasks.pop(code, None)
            done.add(code)
            self._append_wal("DONE", code, strategy_name)

    # ==================== ticker ====================

    def _tick_loop(self):
        while self._running:
            _time.sleep(Config.BUY_POLL_INTERVAL)
            try:
                self._tick()
            except Exception as e:
                logger.error(f"BuyEngine tick 异常: {e}", exc_info=True)

    def _tick(self):
        # 每日重置优先
        self._check_daily_reset()

        # 非交易时段早返回，避免空转
        if not Config.in_trading_window():
            return

        now_time = datetime.now().time()

        # 超过新信号接收截止时间，只在首次跨越时打印日志提示
        if not getattr(self, '_new_signal_logged', False) and now_time >= Config.NEW_SIGNAL_DEADLINE:
            logger.info(f"⏰ 超过最大买入时间 ({Config.NEW_SIGNAL_DEADLINE.strftime('%H:%M:%S')})，不再接收新信号")
            self._new_signal_logged = True

        # 遍历各买入策略，评估其存量待买任务的状态（驱动捞信号等动作），并回收已完结任务
        for strategy_name, strategy in self._strategies.items():
            tasks = self._tasks[strategy_name]
            done = self._today_done[strategy_name]
            # 快照避免迭代时被信号池修改
            for task in list(tasks.values()):
                if task.is_done():
                    continue
                try:
                    strategy.on_tick(task, self.ctx)
                except Exception as e:
                    logger.error(f"[{strategy_name}] on_tick 异常 {task.code}: {e}", exc_info=True)
            # 清理已 DONE
            for code in [c for c, t in list(tasks.items()) if t.is_done()]:
                tasks.pop(code, None)
                done.add(code)
                self._append_wal("DONE", code, strategy_name)

    # ==================== 辅助 ====================

    def _check_daily_reset(self):
        today = datetime.now().date()
        if today == self._last_date:
            return
        for tasks in self._tasks.values():
            tasks.clear()
        for done in self._today_done.values():
            done.clear()
        self._last_date = today
        self._new_signal_logged = False
        logger.info("买入引擎每日重置")

    def get_status(self) -> dict:
        """供 HTTP 查询用"""
        return {
            name: [
                {"code": t.code, "phase": t.phase, "enter_time": t.enter_time.strftime("%H:%M:%S")}
                for t in list(tasks.values())
            ]
            for name, tasks in self._tasks.items()
        }

    # ==================== WAL & 状态恢复 ====================

    def _get_wal_path(self) -> str:
        """
        计算并获取当日的 WAL 日志存储路径。
        日志按日轮转，确保过夜自动失效。
        """
        data_dir = os.path.join(os.getcwd(), 'data')
        os.makedirs(data_dir, exist_ok=True)
        today_str = datetime.now().strftime("%Y%m%d")
        return os.path.join(data_dir, f"buy_signals_{today_str}.jsonl")

    def _cleanup_old_wals(self):
        """
        自动清理历史过期 WAL 文件。
        根据 Config.WAL_BACKUP_DAYS 保留指定天数的日志，超出则自动删除。
        """
        import re
        from datetime import timedelta
        data_dir = os.path.join(os.getcwd(), 'data')
        if not os.path.exists(data_dir):
            return

        # 计算过期边界时间 (今天 - 保留天数)
        current_wal = self._get_wal_path()
        today = datetime.now().date()
        retention_limit = today - timedelta(days=Config.WAL_BACKUP_DAYS)

        for f in glob.glob(os.path.join(data_dir, "buy_signals_*.jsonl")):
            # 绝对不能删除今天正在使用的 active WAL 文件
            if f == current_wal:
                continue
            basename = os.path.basename(f)
            match = re.search(r"buy_signals_(\d{8})\.jsonl", basename)
            if match:
                try:
                    # 提取文件名中的日期 (YYYYMMDD) 并转换为 date 对象进行对比
                    file_date = datetime.strptime(match.group(1), "%Y%m%d").date()
                    if file_date < retention_limit:
                        os.remove(f)
                        logger.info(f"WAL清理: 删除超过 {Config.WAL_BACKUP_DAYS} 天的历史日志 {basename}")
                except Exception as e:
                    logger.error(f"解析WAL历史日期异常 {basename}: {e}")
            else:
                # 非标准的其他 jsonl 文件安全清理
                try:
                    os.remove(f)
                except Exception:
                    pass

    def _append_wal(self, action: str, code: str, strategy_name: str, payload: dict = None, enter_ts: float = 0.0):
        """
        极速状态机日志追加 (WAL Append-Only)
        负责以极其轻量的方式记录买入意图的生命周期。
        - action="ADD": 记录接管了新信号，并固化当时的 payload 和时间戳，供重启后原样恢复。
        - action="DONE": 记录该任务已彻底终结（买够了/超时了/取消了），重启时作为抵消凭证。
        """
        try:
            record = {"action": action, "code": code, "strategy": strategy_name}
            if action == "ADD":
                record["payload"] = payload or {}
                record["enter_time"] = enter_ts
            with open(self._get_wal_path(), 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.error(f"写入 WAL 异常: {e}")

    def _recover_state(self):
        """
        买入引擎状态防断点接管核心逻辑。
        流程如下：
        - 盘口快照：通过顺向重放今日的 WAL 日志，计算出崩溃瞬间仍处于 Pending 状态的“失联”信号。
        - 战场打扫 (Clean Slate)：遍历这批失联信号对应的真实挂单，将活跃中的买单全部撤销解冻，消除所有暗雷。
        - 状态委派：将重组后的纯净信号重新挂载进任务列表，并召唤策略自身的 on_recover 接口进行记忆重建和对账。
        """
        wal_path = self._get_wal_path()
        if not os.path.exists(wal_path):
            return

        pending = {}
        try:
            with open(wal_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.strip(): continue
                    data = json.loads(line)
                    code = data['code']
                    if data['action'] == 'ADD':
                        pending[code] = data
                    elif data['action'] == 'DONE':
                        pending.pop(code, None)
        except Exception as e:
            logger.error(f"读取 WAL 异常: {e}")
            return

        if not pending:
            return

        # 过滤时效性过期的信号（信号时效性保护）
        now_ts = _time.time()
        max_age_seconds = Config.BUY_RECOVERY_MAX_AGE_MINS * 60
        for code, data in list(pending.items()):
            enter_ts = data.get('enter_time', 0.0)
            if enter_ts > 0 and (now_ts - enter_ts) > max_age_seconds:
                logger.info(f"[买入恢复] 信号 {code} 已超出最大恢复时间({Config.BUY_RECOVERY_MAX_AGE_MINS}min)，系统作废")
                stg_name = data.get('strategy', 'default')
                self._append_wal("DONE", code, stg_name)
                pending.pop(code)

        if not pending:
            return

        logger.info(f"从 WAL 恢复出 {len(pending)} 个有效待处理信号: {list(pending.keys())}")

        # 清理遗留挂单 (Clean Slate): 主动撤单，打扫战场
        if self.ctx.broker.is_connected:
            orders = self.ctx.broker.get_orders()
            qmt_codes = {_convert_code(k): k for k in pending.keys()}
            cancel_count = 0
            for o in orders:
                if o.is_buy and o.is_active() and o.stock_code in qmt_codes:
                    logger.info(f"[买入恢复] 清理遗留活跃买单: {o.stock_code} {o.order_id}")
                    self.ctx.broker.cancel(o.order_id)
                    cancel_count += 1
            if cancel_count > 0:
                logger.info(f"[买入恢复] 已发出 {cancel_count} 笔撤单，等待资金解冻...")
                _time.sleep(1.0)

        # 委托策略进行状态对账与重建
        for code, data in pending.items():
            stg_name = data.get('strategy', 'default')
            if stg_name not in self._strategies:
                continue
            
            task = BuyTask(
                code=code,
                qmt_code=_convert_code(code),
                enter_time=datetime.fromtimestamp(data.get('enter_time', _time.time()))
            )
            if data.get('payload'):
                task.data.update(data['payload'])
            
            self._tasks[stg_name][code] = task
            logger.info(f"[{stg_name}] 信号已重新挂载: {code}，转交策略对账")
            
            # 委派模式核心：引擎不主观臆断是否已经“买够了”，而是将状态推演的权力下放给具体的策略
            # 策略自己去拉取真实成交单并决定接下来是继续买还是 DONE
            try:
                strategy = self._strategies[stg_name]
                if hasattr(strategy, 'on_recover'):
                    # 若策略实现了专属恢复钩子，调用其进行深度状态重构
                    strategy.on_recover(task, self.ctx)
                else:
                    # 兼容未实现重构钩子的极简策略，降级为全新首评估
                    strategy.on_init(task, self.ctx)
            except Exception as e:
                logger.error(f"[{stg_name}] on_recover 异常 {code}: {e}", exc_info=True)
                
            if task.is_done():
                self._tasks[stg_name].pop(code, None)
                self._today_done[stg_name].add(code)
                self._append_wal("DONE", code, stg_name)
