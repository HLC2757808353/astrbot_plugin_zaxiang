import time
import asyncio
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from astrbot.api import logger


@dataclass
class ColdViolenceRecord:
    user_id: str
    user_name: str
    start_time: float
    duration: int
    reason: str = ""
    
    @property
    def end_time(self) -> float:
        return self.start_time + self.duration * 60
    
    @property
    def remaining_time(self) -> int:
        remaining = int(self.end_time - time.time())
        return max(0, remaining)
    
    @property
    def is_expired(self) -> bool:
        return time.time() >= self.end_time


class ColdViolenceManager:
    def __init__(self):
        self.cold_violence_records: Dict[str, ColdViolenceRecord] = {}
        self.config: Dict = {}
        self._cleanup_task: Optional[asyncio.Task] = None
    
    def initialize(self, config: Dict):
        self.config = config.get('cold_violence', {})
        logger.info(f"冷暴力管理器初始化完成，配置: {self.config}")
    
    async def start_cleanup_task(self):
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            logger.info("冷暴力清理任务已启动")
    
    async def stop_cleanup_task(self):
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("冷暴力清理任务已停止")
    
    async def _periodic_cleanup(self):
        while True:
            try:
                await asyncio.sleep(60)
                self.cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"冷暴力清理任务出错: {e}")
    
    def cleanup_expired(self):
        expired_users = [
            user_id for user_id, record in self.cold_violence_records.items()
            if record.is_expired
        ]
        for user_id in expired_users:
            del self.cold_violence_records[user_id]
            logger.info(f"用户 {user_id} 的冷暴力已自动解除")
    
    def is_enabled(self) -> bool:
        return self.config.get('enabled', True)
    
    def has_authority(self, user_id: str) -> bool:
        authority_ids = self.config.get('authority_ids', [])
        return str(user_id) in [str(uid) for uid in authority_ids]
    
    def is_whitelisted(self, user_id: str) -> bool:
        whitelist_ids = self.config.get('whitelist_ids', [])
        return str(user_id) in [str(uid) for uid in whitelist_ids]
    
    def is_under_cold_violence(self, user_id: str) -> bool:
        user_id_str = str(user_id)
        if user_id_str not in self.cold_violence_records:
            return False
        
        record = self.cold_violence_records[user_id_str]
        if record.is_expired:
            del self.cold_violence_records[user_id_str]
            return False
        
        return True
    
    def get_cold_violence_info(self, user_id: str) -> Optional[ColdViolenceRecord]:
        user_id_str = str(user_id)
        if not self.is_under_cold_violence(user_id_str):
            return None
        return self.cold_violence_records.get(user_id_str)
    
    def add_cold_violence(
        self, 
        user_id: str, 
        user_name: str, 
        duration: Optional[int] = None,
        reason: str = ""
    ) -> bool:
        user_id_str = str(user_id)
        
        if self.is_whitelisted(user_id_str):
            logger.warning(f"用户 {user_id}({user_name}) 在白名单中，无法冷暴力")
            return False
        
        if duration is None:
            duration = self.config.get('default_duration', 30)
        
        record = ColdViolenceRecord(
            user_id=user_id_str,
            user_name=user_name,
            start_time=time.time(),
            duration=duration,
            reason=reason
        )
        
        self.cold_violence_records[user_id_str] = record
        logger.info(f"已对用户 {user_id}({user_name}) 实施冷暴力，时长 {duration} 分钟")
        return True
    
    def remove_cold_violence(self, user_id: str) -> bool:
        user_id_str = str(user_id)
        if user_id_str in self.cold_violence_records:
            del self.cold_violence_records[user_id_str]
            logger.info(f"已解除用户 {user_id} 的冷暴力")
            return True
        return False
    
    def get_all_cold_violence_users(self) -> List[ColdViolenceRecord]:
        self.cleanup_expired()
        return list(self.cold_violence_records.values())
    
    def format_remaining_time(self, seconds: int) -> str:
        if seconds <= 0:
            return "已结束"
        
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        
        if hours > 0:
            return f"{hours}小时{minutes}分钟{secs}秒"
        elif minutes > 0:
            return f"{minutes}分钟{secs}秒"
        else:
            return f"{secs}秒"
