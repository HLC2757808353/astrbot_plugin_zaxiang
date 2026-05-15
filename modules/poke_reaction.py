import random
from typing import Dict, Optional


class PokeReaction:
    DEFAULT_CONFIG = {
        'enabled': True,
        'back_probability': 0.5,
        'follow_probability': 0.3,
    }

    def __init__(self):
        self.config: Dict = self.DEFAULT_CONFIG.copy()

    def initialize(self, config: Dict):
        poke_config = config.get('poke_reaction', {})
        self.config = {**self.DEFAULT_CONFIG, **poke_config}

    def is_enabled(self) -> bool:
        return self.config.get('enabled', True)

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
        poker_id = str(raw_message.get('user_id', ''))
        group_id = str(raw_message.get('group_id', ''))
        if not group_id or not target_id:
            return None

        if target_id == str(bot_id):
            if random.random() < float(self.config.get('back_probability', 0.5)):
                return {
                    'target_id': poker_id,
                    'group_id': group_id,
                }
        else:
            if poker_id != str(bot_id) and random.random() < float(self.config.get('follow_probability', 0.3)):
                return {
                    'target_id': target_id,
                    'group_id': group_id,
                }

        return None
