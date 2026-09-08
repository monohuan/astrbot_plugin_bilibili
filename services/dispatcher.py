from __future__ import annotations

from dataclasses import dataclass, field
from inspect import isawaitable
import time
from typing import Any, Awaitable, Callable, Literal, Optional

from astrbot.api import logger
from astrbot.api.event import MessageEventResult
from astrbot.api.message_components import Node

NotificationCategory = Literal["dynamic", "live", "style_test"]
SentHook = Callable[["SubscriptionNotification"], None | Awaitable[None]]


@dataclass(frozen=True)
class SubscriptionNotification:
    sub_user: str
    chain_parts: list[Any]
    send_node: bool = False
    category: NotificationCategory = "dynamic"
    dyn_id: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchResult:
    sent: bool
    dropped: bool = False
    reason: str = ""


class SubscriptionNotificationDispatcher:
    def __init__(
        self,
        context: Any,
        on_sent: Optional[SentHook] = None,
    ):
        self.context = context
        self.on_sent = on_sent
        self.silent_until_ts = 0
        # 平台 -> (bot QQ uin, bot 昵称) 缓存，用于转发消息的发送者名称
        self._bot_identity: dict[str, tuple[str, str]] = {}

    async def _resolve_bot_identity(self, sub_user: str) -> tuple[str, str]:
        """解析当前会话所属平台的 bot 登录信息，返回 (uin, 昵称)。

        解析失败返回 ("0", "")，调用方回退默认名称。
        """
        platform_id = sub_user.split(":", 1)[0] if sub_user else ""
        if not platform_id:
            return "0", ""
        cached = self._bot_identity.get(platform_id)
        if cached is not None:
            return cached
        identity = ("0", "")
        try:
            platform_inst = self.context.get_platform_inst(platform_id)
            client = platform_inst.get_client() if platform_inst else None
            if client and hasattr(client, "call_action"):
                raw = await client.call_action("get_login_info")
                if (
                    isinstance(raw, dict)
                    and isinstance(raw.get("data"), dict)
                ):
                    data = raw["data"]
                elif isinstance(raw, dict):
                    data = raw
                else:
                    data = {}
                uin = str(data.get("user_id") or "0")
                nickname = str(data.get("nickname") or "")
                if uin != "0" or nickname:
                    identity = (uin, nickname)
        except Exception as e:
            logger.warning(f"获取 bot 登录信息失败（转发消息将使用默认名称）: {e}")
        self._bot_identity[platform_id] = identity
        return identity

    async def publish(self, notification: SubscriptionNotification) -> DispatchResult:
        if self._is_silent(notification):
            return DispatchResult(sent=False, dropped=True, reason="silent_mode")

        uin, nickname = await self._resolve_bot_identity(notification.sub_user)
        result = self._build_event_result(notification, uin=uin, name=nickname)
        try:
            await self.context.send_message(notification.sub_user, result)
        except Exception as e:
            logger.error(
                f"发送订阅通知失败: sub_user={notification.sub_user} "
                f"category={notification.category} dyn_id={notification.dyn_id} "
                f"error={e}"
            )
            return DispatchResult(sent=False, reason=str(e))
        await self._on_sent(notification)
        return DispatchResult(sent=True)

    def set_silent_until_ts(self, silent_until_ts: int) -> None:
        self.silent_until_ts = max(int(silent_until_ts), 0)

    async def _on_sent(self, notification: SubscriptionNotification) -> None:
        hook = self.on_sent
        if hook is None:
            return
        result = hook(notification)
        if isawaitable(result):
            await result

    def _is_silent(self, notification: SubscriptionNotification) -> bool:
        if notification.category not in ("dynamic", "live"):
            return False
        now_ts = max(int(time.time()), 0)
        if now_ts >= self.silent_until_ts:
            return False
        logger.info(
            f"订阅通知被静默丢弃: sub_user={notification.sub_user} category={notification.category} dyn_id={notification.dyn_id}"
        )
        return True

    @staticmethod
    def _build_event_result(
        notification: SubscriptionNotification,
        uin: str = "0",
        name: str = "",
    ) -> MessageEventResult:
        if notification.send_node:
            qq_node = Node(
                uin=int(uin) if uin.isdigit() else 0,
                name=name or "AstrBot",
                content=notification.chain_parts,
            )
            return MessageEventResult(chain=[qq_node])
        return MessageEventResult(chain=notification.chain_parts).use_t2i(False)
