import time
from typing import Dict, Optional
from dataclasses import dataclass


@dataclass
class MuteRecord:
    group_id: str
    operator_id: str
    operator_name: str
    mute_time: float
    duration: int
    notified: bool = False


class MuteTracker:
    DEFAULT_CONFIG = {
        'enabled': True,
    }

    def __init__(self):
        self.mute_records: Dict[str, MuteRecord] = {}
        self.config: Dict = self.DEFAULT_CONFIG.copy()
        self._last_active_group: Optional[str] = None

    def initialize(self, config: Dict):
        mute_config = config.get('mute_tracker', {})
        self.config = {**self.DEFAULT_CONFIG, **mute_config}

    def is_enabled(self) -> bool:
        return self.config.get('enabled', True)

    def process_notice_event(self, raw_message: dict, bot_id: str):
        if not self.is_enabled():
            return None

        if not isinstance(raw_message, dict):
            return None

        if raw_message.get('notice_type') != 'group_ban':
            return None

        user_id = str(raw_message.get('user_id', ''))
        if user_id != str(bot_id):
            return None

        group_id = str(raw_message.get('group_id', ''))
        operator_id = str(raw_message.get('operator_id', ''))
        duration = int(raw_message.get('duration', 0))
        sub_type = raw_message.get('sub_type', '')

        if sub_type == 'ban' and duration > 0:
            record = MuteRecord(
                group_id=group_id,
                operator_id=operator_id,
                operator_name=str(operator_id),
                mute_time=time.time(),
                duration=duration,
                notified=False,
            )
            self.mute_records[group_id] = record
            self._last_active_group = group_id
            return None

        if sub_type == 'lift_ban' or duration == 0:
            record = self.mute_records.pop(group_id, None)
            if record:
                mute_minutes = record.duration // 60
                mute_seconds = record.duration % 60
                if mute_minutes > 0:
                    duration_str = f"{mute_minutes}分钟{mute_seconds}秒"
                else:
                    duration_str = f"{mute_seconds}秒"

                return {
                    'group_id': group_id,
                    'operator_id': record.operator_id,
                    'operator_name': record.operator_name,
                    'duration': record.duration,
                    'duration_str': duration_str,
                }

        return None

    def get_last_active_group(self) -> Optional[str]:
        return self._last_active_group