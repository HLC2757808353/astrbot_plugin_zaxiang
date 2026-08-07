import re
from typing import Dict, List, Optional


class WordFilter:
    """输出过滤器：将机器人输出消息中命中的过滤词替换为固定词。

    过滤词库通过配置项 filter_words 配置，多个词之间用逗号（支持中英文逗号）隔开，
    例如：词1,词2,词3 或 词1，词2，词3。
    """

    DEFAULT_CONFIG = {
        'enabled': True,
        'filter_words': '',
        'replacement': 'filtered',
    }

    def __init__(self):
        self.config: Dict = self.DEFAULT_CONFIG.copy()
        self._pattern: Optional[re.Pattern] = None
        self._replacement: str = self.DEFAULT_CONFIG['replacement']

    def initialize(self, config: Dict):
        filter_config = config.get('word_filter', config)
        self.config = {**self.DEFAULT_CONFIG, **filter_config}
        self._build_pattern()

    def _build_pattern(self):
        words = self._parse_words(self.config.get('filter_words', ''))
        self._replacement = str(self.config.get('replacement', 'filtered'))
        if words:
            # 按长度降序排列，避免短词先匹配导致长词漏匹配
            escaped = sorted(
                (re.escape(w) for w in words),
                key=len,
                reverse=True,
            )
            self._pattern = re.compile('|'.join(escaped), re.IGNORECASE)
        else:
            self._pattern = None

    @staticmethod
    def _parse_words(raw) -> List[str]:
        """解析过滤词库，支持字符串（逗号分隔）或列表两种配置形式。"""
        if isinstance(raw, list):
            items = raw
        else:
            items = str(raw).replace('，', ',').split(',')
        return [str(w).strip() for w in items if str(w).strip()]

    def is_enabled(self) -> bool:
        return bool(self.config.get('enabled', True))

    def get_filter_words(self) -> List[str]:
        return self._parse_words(self.config.get('filter_words', ''))

    def filter_text(self, text: str) -> str:
        """将文本中命中的过滤词替换为固定词，未启用或无过滤词时原样返回。"""
        if not text or not self.is_enabled() or not self._pattern:
            return text
        return self._pattern.sub(lambda _: self._replacement, text)
