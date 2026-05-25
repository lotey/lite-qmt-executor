#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通知器接口 + 几个开箱即用的实现

框架内所有"对外推送"都走 Notifier.push()，避免硬编码到具体推送 SDK。
用户根据需要继承实现：钉钉、企微、飞书、IM、邮件、短信都行。

自带的实现：
- NullNotifier      —— 空实现（默认）
- ConsoleNotifier   —— 控制台/日志打印
- DingTalkNotifier  —— 钉钉群机器人推送（参考实现，直接可用）
"""

from abc import ABC, abstractmethod
import logging
import threading

logger = logging.getLogger(__name__)


# ==================== 抽象基类 ====================

class Notifier(ABC):
    """通知器抽象类"""

    @abstractmethod
    def push(self, level: str, msg: str) -> None:
        """
        :param level: INFO / WARN / ERROR
        :param msg: 消息内容
        """
        ...


# ==================== 默认实现 ====================

class NullNotifier(Notifier):
    """空实现，什么都不做。默认值。"""

    def push(self, level: str, msg: str) -> None:
        pass


class ConsoleNotifier(Notifier):
    """控制台打印实现。本地调试用。"""

    def push(self, level: str, msg: str) -> None:
        logger.info(f"[NOTIFY {level}] {msg}")


# ==================== 钉钉机器人推送（可直接用 / 也可作为模板）====================

class DingTalkNotifier(Notifier):
    """
    钉钉群机器人 Webhook 推送。
    支持自定义关键字、IP安全设置或加签安全设置。

    使用示例：
        notifier = DingTalkNotifier(
            webhook_url='https://oapi.dingtalk.com/robot/send?access_token=xxxxxx',
            secret='SECxxxxxx',  # 如果钉钉安全设置里启用了"加签"，则配置此项
            broker_name='国金证券'
        )
    """

    def __init__(self, webhook_url: str, secret: str = "", broker_name: str = ""):
        self.webhook_url = webhook_url
        self.secret = secret
        self.broker_name = broker_name

    def push(self, level: str, msg: str) -> None:
        """异步推送，不阻塞调用方。"""
        threading.Thread(
            target=self._send_safe,
            args=(level, msg),
            daemon=True,
            name="dingtalk-pusher-thread",
        ).start()

    def _send_safe(self, level: str, msg: str) -> None:
        try:
            self._send(level, msg)
        except Exception as e:
            # 通知失败永远不能影响主流程，吞异常打 warning
            logger.warning(f"钉钉推送异常: {e}")

    def _send(self, level: str, msg: str) -> None:
        # 延迟 import，避免框架强制依赖 requests
        import requests
        import time
        import hmac
        import hashlib
        import base64
        import urllib.parse

        prefix = f"[{self.broker_name}] " if self.broker_name else ""
        content = f"{prefix}[{level}] {msg}"

        url = self.webhook_url
        if self.secret:
            timestamp = str(round(time.time() * 1000))
            secret_enc = self.secret.encode('utf-8')
            string_to_sign = f'{timestamp}\n{self.secret}'
            string_to_sign_enc = string_to_sign.encode('utf-8')
            hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            url = f"{self.webhook_url}&timestamp={timestamp}&sign={sign}"

        payload = {
            "msgtype": "text",
            "text": {
                "content": content
            }
        }
        headers = {"Content-Type": "application/json"}
        resp = requests.post(url, json=payload, headers=headers, timeout=5)
        if resp.status_code != 200:
            logger.warning(f"钉钉推送失败: status={resp.status_code} body={resp.text}")
