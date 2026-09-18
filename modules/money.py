"""挣钱（收款码）模块。

AI 通过 llm_tool 调用，把上传好的收款二维码发送给用户。
金额只作为 AI 组织回话的素材，不改变二维码本身。
"""

from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Image


class MoneyManager:
    """管理收款二维码的查找与发送。"""

    DEFAULT_CONFIG = {
        "enabled": True,
        "qr_filename": "qrcode.png",
        "allowed_ids": [],
    }

    # 二维码文件扩展名探测顺序
    _EXTS = (".png", ".jpg", ".jpeg", ".webp")

    def __init__(self):
        self.config: dict = self.DEFAULT_CONFIG.copy()
        self.data_dir: Optional[Path] = None

    def initialize(self, config: dict):
        cfg = config.get("money", config)
        merged = {**self.DEFAULT_CONFIG, **cfg}
        merged["qr_filename"] = str(merged.get("qr_filename") or "qrcode.png").strip()
        self.config = merged
        logger.info("挣钱模块初始化完成")

    def set_data_dir(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def has_permission(self, user_id) -> bool:
        """仅 allowed_ids 中的用户可触发；留空则谁都不能触发。"""
        allowed = self.config.get("allowed_ids") or []
        return str(user_id) in [str(uid) for uid in allowed]

    def find_qrcode(self) -> Optional[Path]:
        """查找已上传的二维码文件，找不到返回 None。"""
        if not self.data_dir:
            return None
        configured = self.config.get("qr_filename") or "qrcode.png"
        candidate = self.data_dir / configured
        if candidate.is_file():
            return candidate
        stem = Path(configured).stem or "qrcode"
        for ext in self._EXTS:
            alt = self.data_dir / f"{stem}{ext}"
            if alt.is_file():
                return alt
        return None

    async def send_qrcode(self, event) -> Optional[str]:
        """发送二维码。成功返回 None，失败返回错误信息。"""
        path = self.find_qrcode()
        if not path:
            return "收款二维码尚未配置，请先在插件页面「收款码设置」里上传二维码图片。"
        try:
            await event.send(MessageChain([Image.fromFileSystem(str(path))]))
            logger.info(f"已发送收款二维码：{path}")
            return None
        except Exception as e:
            logger.error(f"发送收款二维码失败: {e}")
            return f"发送收款二维码失败：{e}"