"""文件发送模块。

供 LLM 通过 llm_tool 调用，把本地文件直接发送给用户。
支持代码文件、md、文本、图片等任意文件，受最大体积限制（默认 10MB）。

对 aiocqhttp（QQ/NapCat）平台使用 base64 直传，避免 NapCat 与 AstrBot
文件系统不共享导致读不到本地路径的问题。
"""

import base64
import os
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import File


class FileSenderManager:
    """管理文件发送的配置与校验。"""

    DEFAULT_CONFIG = {
        "enabled": True,
        "max_size_mb": 10,
    }

    def __init__(self):
        self.config: dict = self.DEFAULT_CONFIG.copy()

    def initialize(self, config: dict):
        cfg = config.get("file_sender", config)
        merged = {**self.DEFAULT_CONFIG, **cfg}
        try:
            merged["max_size_mb"] = max(0.1, float(merged.get("max_size_mb", 10)))
        except (TypeError, ValueError):
            merged["max_size_mb"] = 10
        self.config = merged
        logger.info("文件发送模块初始化完成")

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def max_bytes(self) -> int:
        return int(self.config["max_size_mb"] * 1024 * 1024)

    def validate(self, path: str) -> Optional[str]:
        """校验文件路径是否可发送。返回错误信息，None 表示可发送。

        校验项：路径存在、是文件、体积不超过上限。
        """
        if not path or not str(path).strip():
            return "文件路径为空"
        file_path = Path(str(path).strip()).expanduser()
        if not file_path.is_absolute():
            file_path = Path(os.path.abspath(file_path))
        if not file_path.exists():
            return f"文件不存在：{file_path}"
        if not file_path.is_file():
            return f"路径不是文件：{file_path}"
        size = file_path.stat().st_size
        max_bytes = self.max_bytes()
        if size > max_bytes:
            size_mb = size / (1024 * 1024)
            limit_mb = self.config["max_size_mb"]
            return (
                f"文件过大（{size_mb:.1f}MB），超过限制 {limit_mb}MB，"
                f"无法发送。请考虑压缩或截取后重试。"
            )
        return None

    async def _send_via_base64(self, event, file_path: Path) -> None:
        """对 aiocqhttp 平台用 base64 直传文件，避免 NapCat 读不到 AstrBot 容器内路径。"""
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )

        if not isinstance(event, AiocqhttpMessageEvent):
            raise TypeError("base64 发送仅支持 aiocqhttp 平台")

        data = file_path.read_bytes()
        b64 = base64.b64encode(data).decode()
        file_seg = {
            "type": "file",
            "data": {
                "file": f"base64://{b64}",
                "name": file_path.name,
            },
        }
        bot = event.bot
        group_id = getattr(event.message_obj, "group_id", None)
        if group_id:
            await bot.api.call_action(
                "send_group_msg", group_id=int(group_id), message=[file_seg]
            )
        else:
            user_id = event.get_sender_id()
            await bot.api.call_action(
                "send_private_msg", user_id=int(user_id), message=[file_seg]
            )

    async def send(self, event, path: str) -> Optional[str]:
        """校验并发送文件。成功返回 None，失败返回错误信息字符串。"""
        err = self.validate(path)
        if err:
            return err
        file_path = Path(str(path).strip()).expanduser()
        if not file_path.is_absolute():
            file_path = Path(os.path.abspath(file_path))
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            if isinstance(event, AiocqhttpMessageEvent):
                # base64 直传，不依赖文件系统共享
                await self._send_via_base64(event, file_path)
            else:
                # 其他平台回退到 File 组件
                await event.send(MessageChain([File(name=file_path.name, file=str(file_path))]))
            logger.info(f"已发送文件：{file_path}（{file_path.stat().st_size} 字节）")
            return None
        except Exception as e:
            logger.error(f"发送文件失败 {file_path}: {e}")
            return f"发送文件失败：{e}"
