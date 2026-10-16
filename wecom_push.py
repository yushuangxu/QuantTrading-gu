from __future__ import annotations

import time
from typing import Any, Dict, Optional

import requests
from requests.exceptions import RequestException


class WeComPusher:
    """
    企业微信（WeCom）消息推送（webhook）发送器。

    说明：这里使用“消息推送”那种 webhookurl（页面生成的 send webhook），
    调用方式为直接对 webhookurl 发 POST JSON。
    """

    def __init__(self, webhook_url: str, send_interval: int = 30) -> None:
        self.webhook_url = webhook_url
        self.send_interval = send_interval
        self.last_send_time = 0.0

    def push(self, subject: str, content: str) -> None:
        # 简单限流，避免策略频繁触发导致 webhook 短时间内太多请求。
        now = time.time()
        if self.send_interval > 0 and (now - self.last_send_time) < self.send_interval:
            print(f"⚠️ 推送过于频繁，{self.send_interval}秒后再试")
            return

        # 企业微信“消息推送”text content 是 utf8 文本。
        # subject/content 分行拼起来，便于阅读。
        text = f"{subject}\n\n{content}".strip()
        payload: Dict[str, Any] = {"msgtype": "text", "text": {"content": text}}

        try:
            resp = requests.post(
                self.webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()

            # 常见返回：{"errcode":0,"errmsg":"ok"}；也可能直接返回文本。
            try:
                data = resp.json()
            except Exception:
                data = None

            if isinstance(data, dict) and int(data.get("errcode", 0)) != 0:
                print(f"❌ 企业微信发送失败: errcode={data.get('errcode')}, errmsg={data.get('errmsg')}")
                return

            self.last_send_time = now
            print("✅ 企业微信消息发送成功")

        except RequestException as e:
            print(f"❌ 网络错误: {str(e)}")
        except Exception as e:
            print(f"❌ 发送异常: {str(e)}")

