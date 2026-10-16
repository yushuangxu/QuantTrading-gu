import requests
import time
import html
from requests.exceptions import RequestException

class TelegramPusher:
    def __init__(self, bot_token: str, chat_id: str, send_interval: int = 30) -> None:
        """
        初始化Telegram推送器
        :param bot_token: 机器人API Token
        :param chat_id: 接收消息的Chat ID
        :param send_interval: 最小发送间隔（秒），防止频繁推送
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.send_interval = send_interval
        self.last_send_time = 0
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        # 支持HTML格式，可加粗、换行等
        self.parse_mode = "HTML"

    def push(self, subject: str, content: str) -> None:
        """
        发送消息到Telegram
        :param subject: 消息标题
        :param content: 消息内容
        """
        # 1. 频率限制：避免频繁发送
        now = time.time()
        if now - self.last_send_time < self.send_interval:
            print(f"⚠️ 推送过于频繁，{self.send_interval}秒后再试")
            return

        try:
            # 2. 构造消息内容（支持HTML格式）
            # Telegram 的 parse_mode=HTML 对 <, >, & 等字符敏感，这里做转义避免渲染失败。
            safe_subject = html.escape(str(subject), quote=False)
            safe_content = html.escape(str(content), quote=False)
            message = f"<b>{safe_subject}</b>\n\n{safe_content}"

            # Telegram sendMessage 文本长度通常上限为 4096，这里做保守截断。
            if len(message) > 4000:
                message = message[:3950] + "…"
            
            # 3. 发送请求
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": self.parse_mode
            }
            
            response = requests.post(self.api_url, data=payload, timeout=10)
            response.raise_for_status()  # 抛出HTTP错误
            
            # 4. 处理响应
            result = response.json()
            if result.get("ok"):
                self.last_send_time = now
                print("✅ Telegram消息发送成功")
            else:
                error_msg = result.get("description", "未知错误")
                print(f"❌ 发送失败: {error_msg}")

        except RequestException as e:
            # 捕获所有网络错误，程序不会崩溃
            print(f"❌ 网络错误: {str(e)}")
        except Exception as e:
            print(f"❌ 发送异常: {str(e)}")