import random
from typing import Dict, Optional
from astrbot.api import logger


class PokeReaction:
    DEFAULT_CONFIG = {
        'enabled': True,
        'probability': 0.5,
    }

    def __init__(self):
        self.config: Dict = self.DEFAULT_CONFIG.copy()
        self._last_poker: Dict[str, Dict] = {}

    def initialize(self, config: Dict):
        poke_config = config.get('poke_reaction', {})
        self.config = {**self.DEFAULT_CONFIG, **poke_config}
        logger.info(f"戳一戳反应模块初始化完成，概率: {self.config['probability']}")

    def is_enabled(self) -> bool:
        return self.config.get('enabled', True)

    def should_poke_back(self) -> bool:
        probability = float(self.config.get('probability', 0.5))
        return random.random() < probability

    def process_poke_event(self, raw_message: dict, bot_id: str) -> Optional[dict]:
        if not self.is_enabled():
            return None

        if not isinstance(raw_message, dict):
            return None

        if raw_message.get('notice_type') != 'notify':
            return None

        if raw_message.get('sub_type') != 'poke':
            return None

        target_id = str(raw_message.get('target_id', ''))
        if target_id != str(bot_id):
            return None

        poker_id = str(raw_message.get('user_id', ''))
        group_id = str(raw_message.get('group_id', ''))
        if not group_id:
            return None

        if not self.should_poke_back():
            logger.info(f"群{group_id}用户{poker_id}戳了AI，未触发反戳（概率: {self.config['probability']}）")
            return None

        logger.info(f"群{group_id}用户{poker_id}戳了AI，触发反戳")
        return {
            'poker_id': poker_id,
            'group_id': group_id,
        }