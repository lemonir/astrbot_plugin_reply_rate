"""分群回复频率：给每个群单独设定主动接话的概率。"""

from __future__ import annotations

import random
import time
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


def _parse_table(raw: Any) -> dict[str, float]:
    """把 ["群号:概率", ...] 解析成 {群号: 概率}。"""
    table: dict[str, float] = {}
    if not isinstance(raw, list):
        return table
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        sep = ":" if ":" in text else ("：" if "：" in text else "")
        if not sep:
            continue
        key, _, value = text.partition(sep)
        key = key.strip()
        if not key:
            continue
        try:
            prob = float(value.strip())
        except (TypeError, ValueError):
            logger.warning(f"[reply_rate] 无法解析概率：{text}")
            continue
        table[key] = max(0.0, min(1.0, prob))
    return table


@register(
    "reply_rate",
    "大肥鱼",
    "按群设置群聊主动接话概率",
    "1.0.0",
)
class ReplyRatePlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._last_reply_at: dict[str, float] = {}
        self._last_at: dict[str, tuple[float, str]] = {}
        self._combo: dict[str, int] = {}
        self._muted_until: dict[str, float] = {}

    def _probability(self, group_id: str) -> float:
        table = _parse_table(self.config.get("group_probabilities"))
        if group_id in table:
            return table[group_id]
        try:
            default = float(self.config.get("default_probability", 0.03))
        except (TypeError, ValueError):
            default = 0.03
        return max(0.0, min(1.0, default))

    def _min_interval(self) -> float:
        try:
            return max(0.0, float(self.config.get("min_interval_seconds", 30)))
        except (TypeError, ValueError):
            return 30.0

    def _at_window(self) -> float:
        try:
            return max(0.0, float(self.config.get("at_window_seconds", 15)))
        except (TypeError, ValueError):
            return 15.0

    @staticmethod
    def _is_bare_mention(event: AstrMessageEvent) -> bool:
        """消息里除了 @ 机器人，没有任何实际内容。"""
        messages = event.get_messages() or []
        if not messages:
            return True
        for comp in messages:
            if isinstance(comp, Comp.At):
                continue
            if isinstance(comp, Comp.Plain) and not str(comp.text or "").strip():
                continue
            return False
        return True

    def _window_min_interval(self) -> float:
        try:
            return max(0.0, float(self.config.get("window_min_interval_seconds", 5)))
        except (TypeError, ValueError):
            return 5.0

    def _combo_limit(self) -> int:
        try:
            return max(0, int(self.config.get("combo_limit", 7)))
        except (TypeError, ValueError):
            return 7

    def _combo_reset(self) -> float:
        try:
            return max(0.0, float(self.config.get("combo_reset_seconds", 120)))
        except (TypeError, ValueError):
            return 120.0

    def _muted(self, group_id: str, now: float | None = None) -> bool:
        """连击打满后的静音期内不再接话。"""
        now = time.time() if now is None else now
        until = self._muted_until.get(group_id, 0.0)
        if not until:
            return False
        if now >= until:
            self._muted_until.pop(group_id, None)
            return False
        return True

    def _bump_combo(self, group_id: str, now: float) -> bool:
        """记一次接话；返回 True 表示已达连击上限并已进入静音。"""
        limit = self._combo_limit()
        last = self._last_reply_at.get(group_id, 0.0)
        if now - last > self._combo_reset():
            self._combo[group_id] = 0
        self._combo[group_id] = self._combo.get(group_id, 0) + 1
        if limit and self._combo[group_id] >= limit:
            self._combo[group_id] = 0
            self._last_at.pop(group_id, None)
            self._muted_until[group_id] = now + self._combo_reset()
            logger.info(
                f"[reply_rate] 群 {group_id} 连击已达 {limit} 句，闭嘴 {self._combo_reset():.0f}s"
            )
            return True
        return False

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-1)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self.config.get("enable", True):
            return

        sender_id = str(event.get_sender_id() or "")
        if sender_id and sender_id == str(event.get_self_id() or ""):
            return

        group_id = str(event.get_group_id() or "")
        if not group_id:
            return

        # 被 @ 或被唤醒：记下窗口起点，正常回复交给框架
        if event.is_at_or_wake_command:
            if not self._muted(group_id):
                self._last_at[group_id] = (time.time(), sender_id)
            # 纯 @ 一个字都没带时，框架那边内容为空、不会开口，
            # 这里补一句，免得像是没收到。
            if self.config.get("reply_bare_mention", True) and self._is_bare_mention(event):
                logger.info(f"[reply_rate] 群 {group_id} 收到纯 @，主动应一声")
                yield event.request_llm(
                    prompt="（对方只 @ 了你，没写别的内容，用你自己的语气随口应一声，很短）"
                )
            return

        text = (event.message_str or "").strip()
        if not text:
            return

        now = time.time()
        if self._muted(group_id, now):
            return

        # 唤醒窗口只由 @ 或 / 开启：这里接话绝不刷新 _last_at，
        # 否则每次接话都会把窗口续上，永远停不下来。
        window = self._at_window()
        at_time, at_user = self._last_at.get(group_id, (0.0, ""))
        if window > 0 and now - at_time < window and sender_id and sender_id == at_user:
            if now - self._last_reply_at.get(group_id, 0.0) < self._window_min_interval():
                return
            logger.info(
                f"[reply_rate] 群 {group_id} 处于 {window:.0f}s 唤醒窗口内，直接接话"
            )
            self._last_reply_at[group_id] = now
            self._bump_combo(group_id, now)
            yield event.request_llm(prompt=text)
            return

        if self.config.get("only_when_chat", False):
            chain = event.get_messages() or []
            if any(not isinstance(comp, Comp.Plain) for comp in chain):
                return

        probability = self._probability(group_id)
        if probability <= 0:
            return
        if now - self._last_reply_at.get(group_id, 0.0) < self._min_interval():
            return
        if random.random() >= probability:
            return

        self._last_reply_at[group_id] = now
        self._bump_combo(group_id, now)
        logger.info(
            f"[reply_rate] 群 {group_id} 命中接话概率 {probability:.2%}，交给 AI 处理"
        )
        yield event.request_llm(prompt=text)

    @filter.after_message_sent()
    async def on_bot_message_sent(self, event: AstrMessageEvent):
        """发完消息不再重置唤醒窗口：窗口只从被 @ / 唤醒那一刻开始计时，
        主动回复不会延长唤醒期。"""
        return
