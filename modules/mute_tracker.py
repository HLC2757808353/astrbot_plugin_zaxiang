import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Optional

from astrbot.api import logger


@dataclass
class MuteRecord:
    group_id: str
    operator_id: str
    operator_name: str
    mute_time: float
    duration: int
    notified: bool = False


class MuteTracker:
    """追踪机器人自己被禁言的事件。

    记录群禁言通知（notice_type == "group_ban"）中的操作者、时长，
    在禁言解除时把「是谁禁言、禁了多久、又是谁解除的」交给上层去组织语言。
    操作者昵称需要在收到 ban 通知时由上层通过群成员接口补全（见 update_operator_name）。

    记录会持久化到插件数据目录的 mute_state.json，
    这样插件重载 / AstrBot 重启后仍能知道是谁禁言的。
    """

    DEFAULT_CONFIG = {
        'enabled': True,
    }

    # 已到期超过这么久还没收到解禁通知的记录，载入时直接丢弃（避免过期数据堆积）
    STALE_GRACE_SECONDS = 900

    def __init__(self):
        self.mute_records: Dict[str, MuteRecord] = {}
        self.config: Dict = self.DEFAULT_CONFIG.copy()
        self._last_active_group: Optional[str] = None
        self._state_path: Optional[Path] = None

    def initialize(self, config: Dict):
        mute_config = config.get('mute_tracker', {})
        self.config = {**self.DEFAULT_CONFIG, **mute_config}

    def set_state_path(self, path) -> None:
        """指定持久化文件路径，并立即把已有记录载入内存。

        Args:
            path: mute_state.json 的完整路径（通常位于插件数据目录）。
        """
        self._state_path = Path(path)
        self._load_state()

    def _load_state(self) -> None:
        """从磁盘恢复禁言记录，失败的/明显过期的记录会被跳过。"""
        if not self._state_path or not self._state_path.is_file():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding='utf-8'))
        except Exception as e:
            logger.warning(f"读取禁言记录失败，忽略: {e}")
            return
        if not isinstance(data, dict):
            return

        now = time.time()
        for group_id, item in data.items():
            if not isinstance(item, dict):
                continue
            try:
                duration = int(item.get('duration') or 0)
                mute_time = float(item.get('mute_time') or 0)
            except (TypeError, ValueError):
                continue
            # 有明确时长且早就该解除、又一直没收到解禁通知 -> 过期垃圾数据
            if duration > 0 and now > mute_time + duration + self.STALE_GRACE_SECONDS:
                continue
            self.mute_records[str(group_id)] = MuteRecord(
                group_id=str(group_id),
                operator_id=str(item.get('operator_id') or ''),
                operator_name=str(item.get('operator_name') or ''),
                mute_time=mute_time,
                duration=duration,
                notified=bool(item.get('notified', False)),
            )
        if self.mute_records:
            logger.info(f"已从磁盘恢复 {len(self.mute_records)} 条禁言记录")

    def _save_state(self) -> None:
        """把当前禁言记录写回磁盘，失败只告警不影响主流程。"""
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                group_id: asdict(record)
                for group_id, record in self.mute_records.items()
            }
            self._state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
        except Exception as e:
            logger.warning(f"保存禁言记录失败: {e}")

    def is_enabled(self) -> bool:
        return self.config.get('enabled', True)

    def update_operator_name(self, group_id: str, operator_name: str) -> None:
        """补全禁言操作者的昵称（收到 ban 通知后由上层查询群成员信息填充）。"""
        record = self.mute_records.get(str(group_id))
        if record and operator_name:
            record.operator_name = operator_name
            self._save_state()

    def process_notice_event(self, raw_message: dict, bot_id: str) -> Optional[dict]:
        """处理群禁言通知。

        Args:
            raw_message: OneBot v11 通知事件原始数据。
            bot_id: 机器人自身的 QQ。

        Returns:
            命中禁言/解除时返回事件描述字典，否则返回 None。
            ban 事件返回 type="ban"；解禁事件返回 type="lift"（附带时长与操作者）。
        """
        if not self.is_enabled():
            return None

        if raw_message is None:
            return None
        # 兼容 dict 与 aiocqhttp Event（dict 子类 / 带 get 的对象）
        getter = getattr(raw_message, 'get', None)
        if not callable(getter):
            return None

        if getter('notice_type') != 'group_ban':
            return None

        user_id = str(getter('user_id', ''))
        if user_id != str(bot_id):
            return None

        group_id = str(getter('group_id', ''))
        operator_id = str(getter('operator_id', ''))
        duration = int(getter('duration', 0) or 0)
        sub_type = getter('sub_type', '')

        # sub_type 优先；个别实现不带 sub_type，退化为按 duration 判断
        is_ban = sub_type == 'ban' or (sub_type not in ('ban', 'lift_ban') and duration > 0)
        is_lift = sub_type == 'lift_ban' or (
            sub_type not in ('ban', 'lift_ban') and duration == 0
        )

        if is_ban:
            op_id = operator_id
            if op_id == user_id or op_id in ('', '0'):
                op_id = ''
            record = MuteRecord(
                group_id=group_id,
                operator_id=op_id,
                operator_name=op_id or '',
                mute_time=time.time(),
                duration=duration,  # 0 表示永久禁言
                notified=False,
            )
            self.mute_records[group_id] = record
            self._last_active_group = group_id
            self._save_state()
            return {
                'type': 'ban',
                'group_id': group_id,
                'operator_id': op_id,
                'duration': duration,
            }

        if is_lift:
            record = self.mute_records.pop(group_id, None)
            if not record:
                return None
            self._save_state()

            if record.duration <= 0:
                duration_str = "永久"
            else:
                mute_minutes = record.duration // 60
                mute_seconds = record.duration % 60
                if mute_minutes > 0:
                    duration_str = f"{mute_minutes}分钟{mute_seconds}秒"
                else:
                    duration_str = f"{mute_seconds}秒"

            lift_op_id = operator_id
            if lift_op_id in ('0', ''):
                lift_op_id = ''

            return {
                'type': 'lift',
                'group_id': group_id,
                'operator_id': record.operator_id,
                'operator_name': record.operator_name,
                'duration': record.duration,
                'duration_str': duration_str,
                'lift_operator_id': lift_op_id,
                'auto_lifted': not lift_op_id,
            }

        return None

    def get_last_active_group(self) -> Optional[str]:
        return self._last_active_group
