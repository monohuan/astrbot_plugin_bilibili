import asyncio
import base64
import io
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult
from astrbot.api.event.filter import (
    EventMessageType,
    PermissionType,
    command,
    event_message_type,
    permission_type,
    regex,
)
from astrbot.api.message_components import File, Image, Node
from astrbot.core.star.filter.command import GreedyStr
from bilibili_api import login_v2

from .bili_client import BiliClient
from .core.constant import (
    AT_ALL_OPTION,
    AT_SUB_OPTION,
    BANNER_PATH,
    BILI_HELP_TEXT,
    BV,
    CARD_TEMPLATES,
    DEFAULT_TEMPLATE,
    LIVE_ATALL_OPTION,
    RECENT_DYNAMIC_CACHE,
    RECONNECT_SILENT_PADDING_SECS,
    RECONNECT_SILENT_THRESHOLD_SECS,
    SUB_LIST_TEMPLATE_PATH,
    SUB_SUCCESS_TEMPLATE_PATH,
    UNAT_SUB_OPTION,
    UNSET,
    VALID_FILTER_TYPES,
    VALID_SUB_OPTIONS,
    get_template_names,
)
from .core.data_manager import DataManager
from .core.models import ForwardPayload, RenderPayload, SubscriptionRecord
from .core.utils import create_qrcode, image_to_base64, is_height_valid, is_valid_umo
from .services.dispatcher import SubscriptionNotification, SubscriptionNotificationDispatcher
from .services.listener import DynamicListener
from .services.renderer import Renderer
from .services.subscription_service import SubscriptionService
from .tools.bgm_daily import BgmDailyTool
from .tools.bgm_subject import BgmAdvancedSubjectSearchTool, BgmRecommendHotSubjectsTool
from .tools.bili_hot_video import BiliSearchHotVideosTool
from .tools.bili_user_dynamics import BiliUserDynamicsTool


@register("astrbot_plugin_bilibili", "Soulter", "", "", "")
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.cfg = config
        self.context = context

        self.rai = self.cfg.get("rai", True)
        self.enable_parse_miniapp = self.cfg.get("enable_parse_miniapp", True)
        self.enable_parse_BV = self.cfg.get("enable_parse_BV", True)
        self.proxy = (self.cfg.get("proxy", "") or "").strip()
        self.bangumi_token = (self.cfg.get("bangumi_token", "") or "").strip()
        # 读取样式配置
        self.style = self.cfg.get("renderer_template", DEFAULT_TEMPLATE)
        # 自然语言搜索（视频/番剧）列表是否渲染为图片
        self.enable_video_image = self.cfg.get("enable_video_image", True)
        self.enable_bangumi_image = self.cfg.get("enable_bangumi_image", True)

        self.data_manager = DataManager(
            recent_dynamic_cache=self.cfg.get(
                "recent_dynamic_cache", RECENT_DYNAMIC_CACHE
            )
        )
        self.renderer = Renderer(self, self.rai, self.style)
        self._last_notify_write_ts = self.data_manager.get_last_success_sub_notify_ts()
        self.notification_dispatcher = SubscriptionNotificationDispatcher(
            context=self.context,
            on_sent=self._on_subscription_notification_sent,
        )

        # 优先使用 DataManager 中的凭据
        saved_credential = self.data_manager.get_credential()
        if saved_credential:
            self.bili_client = BiliClient(
                credential_dict=saved_credential, proxy=self.proxy
            )
        else:
            self.bili_client = BiliClient(
                sessdata=self.cfg.get("sessdata"), proxy=self.proxy
            )

        self.dynamic_listener = DynamicListener(
            context=self.context,
            data_manager=self.data_manager,
            bili_client=self.bili_client,
            renderer=self.renderer,
            dispatcher=self.notification_dispatcher,
            cfg=self.cfg,
        )
        self.subscription_service = SubscriptionService(
            data_manager=self.data_manager,
            bili_client=self.bili_client,
            parse_dynamics=self.dynamic_listener._parse_and_filter_dynamics,
        )
        video_renderer = self.renderer if self.enable_video_image else None
        bangumi_renderer = self.renderer if self.enable_bangumi_image else None
        llm_tools = (
            BgmAdvancedSubjectSearchTool(
                token=self.bangumi_token,
                renderer=bangumi_renderer,
            ),
            BgmRecommendHotSubjectsTool(
                token=self.bangumi_token,
                renderer=bangumi_renderer,
            ),
            BgmDailyTool(
                token=self.bangumi_token,
            ),
            BiliSearchHotVideosTool(
                bili_client=self.bili_client,
                renderer=video_renderer,
            ),
            BiliUserDynamicsTool(
                bili_client=self.bili_client,
                parse_dynamics=self.dynamic_listener._parse_and_filter_dynamics,
            ),
        )
        self.context.add_llm_tools(*llm_tools)
        self._configure_reconnect_silent()

        self._start_tasks()

    def _start_tasks(self):
        """启动或重启后台任务。"""
        if hasattr(self, "dynamic_listener_task") and self.dynamic_listener_task:
            self.dynamic_listener_task.cancel()

        self.dynamic_listener_task = asyncio.create_task(self.dynamic_listener.start())

    def _compute_reconnect_silent_duration(self) -> int:
        uid_count = len(self.dynamic_listener._build_uid_targets())
        interval_secs = max(float(self.cfg.get("interval_secs")), 0.0)
        task_gap_secs = max(float(self.cfg.get("task_gap_secs")), 0.0)
        duration = (
            interval_secs + task_gap_secs * uid_count + RECONNECT_SILENT_PADDING_SECS
        )
        return max(int(duration), 1)

    def _configure_reconnect_silent(self) -> None:
        if not bool(self.cfg.get("reconnect_silent", False)):
            self.notification_dispatcher.set_silent_until_ts(0)
            return

        last_success_ts = self.data_manager.get_last_success_sub_notify_ts()
        if last_success_ts <= 0:
            logger.info("重连静默未触发：缺少历史推送成功时间。")
            return

        now_ts = int(time.time())
        idle_secs = now_ts - last_success_ts
        if idle_secs <= RECONNECT_SILENT_THRESHOLD_SECS:
            logger.info(
                f"重连静默未触发：距上次成功推送仅 {idle_secs} 秒（阈值 {RECONNECT_SILENT_THRESHOLD_SECS} 秒）。"
            )
            return

        silent_duration = self._compute_reconnect_silent_duration()
        silent_until_ts = now_ts + silent_duration
        self.notification_dispatcher.set_silent_until_ts(silent_until_ts)
        logger.warning(
            f"检测到长时间未成功推送订阅通知（{idle_secs} 秒），进入静默模式 {silent_duration} 秒。"
        )

    async def _on_subscription_notification_sent(self, _notification: object) -> None:
        now_ts = int(time.time())
        if now_ts == self._last_notify_write_ts:
            return
        self._last_notify_write_ts = now_ts
        await self.data_manager.set_last_success_sub_notify_ts(now_ts)

    @dataclass
    class ParsedSubArgs:
        """bili_sub 过滤参数解析结果（patch 语义：未提供的项保持不变）。"""

        type_ops: List[Tuple[str, List[str]]] = field(default_factory=list)
        regex_ops: List[Tuple[str, List[str]]] = field(default_factory=list)
        live_atall: Optional[bool] = None
        at_all: bool = False
        at_sub: bool = False
        unat_sub: bool = False
        # UNSET=未指定 / True / False / None(clear，清除订阅级覆盖)
        img_forward: Any = UNSET

    @staticmethod
    def _parse_sub_args(input_text: GreedyStr) -> "Main.ParsedSubArgs":
        spec = Main.ParsedSubArgs()
        args = input_text.strip().split(" ") if input_text.strip() else []
        bare_types: List[str] = []
        bare_regex: List[str] = []
        has_bare = False

        def _split_values(v: str) -> List[str]:
            return [x.strip() for x in v.split(",") if x.strip()]

        for arg in args:
            # 键值语法：type=/regex=/img_forward=/live_atall=、单类型开关（如 lottery=0）
            if "=" in arg and not arg.startswith(("+", "-")):
                key, _, value = arg.partition("=")
                v = value.strip()
                low = v.lower()
                if key == "img_forward":
                    if low in ("on", "true", "1", "yes", "开", "开启"):
                        spec.img_forward = True
                    elif low in ("off", "false", "0", "no", "关", "关闭"):
                        spec.img_forward = False
                    elif low in ("clear", "none", "default", "follow", "清除", "默认"):
                        spec.img_forward = None
                    continue
                if key == "live_atall":
                    spec.live_atall = low not in ("off", "false", "0", "no", "关", "关闭")
                    continue
                if key == "type":
                    spec.type_ops.append(("set", _split_values(v)))
                    continue
                if key == "regex":
                    spec.regex_ops.append(("set", _split_values(v)))
                    continue
                if key in VALID_FILTER_TYPES:
                    # 单类型开关：lottery=1 追加 / lottery=0 移除
                    if low in ("off", "false", "0", "no", "", "关", "关闭"):
                        spec.type_ops.append(("remove", [key]))
                    else:
                        spec.type_ops.append(("add", [key]))
                    continue
            # 前缀语法：+type= / -type= / +regex= / -regex=
            if arg.startswith(("+", "-")) and "=" in arg:
                key, _, value = arg[1:].partition("=")
                vals = _split_values(value.strip())
                op = "add" if arg.startswith("+") else "remove"
                if key == "type":
                    spec.type_ops.append((op, vals))
                    continue
                if key == "regex":
                    spec.regex_ops.append((op, vals))
                    continue
            # 裸 img_forward（不带=）等同于 img_forward=on，
            # 防止它落入旧裸参数分支被误当作正则并整体覆盖过滤列表
            if arg == "img_forward":
                spec.img_forward = True
                continue
            # 标志位语法
            if arg in VALID_SUB_OPTIONS:
                if arg == LIVE_ATALL_OPTION:
                    spec.live_atall = True
                elif arg == AT_ALL_OPTION:
                    spec.at_all = True
                elif arg == AT_SUB_OPTION:
                    spec.at_sub = True
                elif arg == UNAT_SUB_OPTION:
                    spec.unat_sub = True
                continue
            # 裸参数（旧语法）：类型 or 正则，出现即整体覆盖
            if arg in VALID_FILTER_TYPES:
                bare_types.append(arg)
            else:
                bare_regex.append(arg)
            has_bare = True

        # 旧语法兜底：裸参数整体覆盖类型与正则两个列表（优先于键值语法，按出现顺序最后应用）
        if has_bare:
            spec.type_ops.append(("set", bare_types))
            spec.regex_ops.append(("set", bare_regex))
        return spec

    @staticmethod
    def _apply_list_op(
        base: List[str], op: str, values: List[str]
    ) -> List[str]:
        if op == "set":
            return list(values)
        if op == "add":
            return base + [v for v in values if v not in base]
        if op == "remove":
            return [x for x in base if x not in values]
        return list(base)

    @staticmethod
    def _build_subscription_payload(
        uid: int,
        name: str,
        avatar: str,
        note: str = "",
        title: str = "订阅成功",
        record: Optional[SubscriptionRecord] = None,
        img_forward: bool = False,
    ) -> dict:
        filter_types: List[str] = []
        filter_regex: List[str] = []
        live_atall = False
        at_all = False
        at_sub_users: List[str] = []
        if record:
            filter_types = list(record.filter_types)
            filter_regex = list(record.filter_regex)
            live_atall = bool(record.live_atall)
            at_all = bool(record.at_all)
            at_sub_users = list(record.at_sub_users)
        return {
            "uid": str(uid),
            "name": name,
            "avatar": avatar,
            "note": note.strip(),
            "title": title,
            "filter_types": filter_types,
            "filter_regex": filter_regex,
            "live_atall": live_atall,
            "at_all": at_all,
            "at_sub_users": at_sub_users,
            "img_forward": bool(img_forward),
        }

    async def _render_sub_success(self, context: dict) -> str | None:
        try:
            with open(SUB_SUCCESS_TEMPLATE_PATH, "r", encoding="utf-8") as f:
                tmpl = f.read()
        except Exception as e:
            logger.error(f"加载订阅成功模板失败: {e}")
            return None
        options = {
            "full_page": True,
            "type": "jpeg",
            "quality": 95,
            "scale": "device",
            "device_scale_factor_level": "ultra",
            "viewport_height": 1,
        }
        try:
            img_path = await self.html_render(
                tmpl=tmpl, data=context, return_url=False, options=options
            )
        except Exception as e:
            logger.error(f"渲染订阅成功图片失败: {e}")
            return None
        if (
            img_path
            and os.path.exists(img_path)
            and self.renderer._validate_image(img_path)
        ):
            return img_path
        return None

    @staticmethod
    def _subscription_fallback_chain(payload: dict, text: str) -> MessageChain:
        """订阅卡片无法生成或上传时，回退为文字和 UP 主头像。"""
        chain = MessageChain().message(text)
        avatar = str(payload.get("avatar") or "").strip()
        if avatar:
            chain = chain.url_image(avatar)
        return chain

    async def _send_subscription_result(
        self, event: AstrMessageEvent, payload: dict
    ) -> MessageEventResult | None:
        name = payload.get("name", "")
        uid = payload.get("uid", "")
        note = payload.get("note", "")
        title = payload.get("title", "") or "订阅成功"
        text = f"{title}！UP 主: {name} (UID: {uid})"
        if note:
            text += f"\n{note}"
        text += f"\n图片转发: {'开启' if payload.get('img_forward') else '关闭'}"
        if self.rai:
            img_path = await self._render_sub_success(payload)
            if img_path:
                try:
                    await event.send(MessageChain().file_image(img_path))
                    return None
                except Exception as e:
                    logger.warning(f"订阅卡片上传失败，降级为图文: {e}")
            else:
                logger.warning("订阅卡片生成失败，降级为图文")
            try:
                await event.send(self._subscription_fallback_chain(payload, text))
            except Exception as e:
                logger.warning(f"订阅图文回退仍上传失败，改发纯文本: {e}")
                await event.send(MessageChain().message(text))
            return None
        chain = MessageChain().message(text)
        avatar = payload.get("avatar", "")
        if avatar:
            chain = chain.url_image(avatar)
        return MessageEventResult(chain=chain, use_t2i_=False)

    async def _apply_subscription(
        self,
        sub_user: str,
        uid_int: int,
        spec: "Main.ParsedSubArgs",
        add_sub_users: List[str] | None = None,
        rm_sub_users: List[str] | None = None,
    ) -> Tuple[bool, str]:
        # patch 语义：基于现有记录解析过滤列表，未提供的项保持不变
        existing = self.data_manager.get_subscription(sub_user, uid_int)
        base_types = list(existing.filter_types) if existing else []
        base_regex = list(existing.filter_regex) if existing else []

        filter_types = list(base_types)
        for op, values in spec.type_ops:
            filter_types = self._apply_list_op(filter_types, op, values)
        filter_regex = list(base_regex)
        for op, values in spec.regex_ops:
            filter_regex = self._apply_list_op(filter_regex, op, values)

        result = await self.subscription_service.add_or_update(
            sub_user,
            uid_int,
            filter_types,
            filter_regex,
            spec.live_atall,
            at_all=spec.at_all if spec.at_all else None,
            add_sub_users=add_sub_users,
            rm_sub_users=rm_sub_users,
            img_forward=spec.img_forward,
        )
        if result.updated:
            record = self.data_manager.get_subscription(sub_user, uid_int)
            option_desc = (
                "开启" if record and record.live_atall else "关闭"
            )
            return True, f"该动态已订阅，已更新过滤条件。直播@全体: {option_desc}"
        return False, ""

    @command("bili_login")
    @permission_type(PermissionType.ADMIN)
    async def bili_login(self, event: AstrMessageEvent):
        """扫码登录 Bilibili。"""
        if event.get_group_id():
            return MessageEventResult().message(
                "仅支持管理员在私聊中使用'/bili_login'指令。"
            )

        login_obj = login_v2.QrCodeLogin()
        await login_obj.generate_qrcode()

        # 获取二维码图片路径
        qr_path = os.path.join(tempfile.gettempdir(), "qrcode.png")

        await event.send(
            MessageChain()
            .message("请使用 Bilibili App 扫描下方二维码登录：")
            .file_image(qr_path)
        )

        # 轮询状态
        try:
            while True:
                state = await login_obj.check_state()
                if state == login_v2.QrCodeLoginEvents.DONE:
                    credential = login_obj.get_credential()
                    # 保存凭据
                    self.bili_client.credential = credential
                    cred_dict = self.bili_client.get_credential_dict()
                    if cred_dict is not None:
                        await self.data_manager.set_credential(cred_dict)
                        self._start_tasks()
                        await event.send(MessageChain().message("✅ 登录成功！"))
                    else:
                        await event.send(
                            MessageChain().message("❌ 登录失败：无法获取凭据。")
                        )
                    break
                elif state == login_v2.QrCodeLoginEvents.TIMEOUT:
                    await event.send(
                        MessageChain().message("❌ 登录超时，请重新执行 /bili_login。")
                    )
                    break

                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"登录过程中发生错误: {e}")
            await event.send(MessageChain().message(f"❌ 登录失败: {str(e)}"))

    @command("bili_logout")
    @permission_type(PermissionType.ADMIN)
    async def bili_logout(self, event: AstrMessageEvent):
        """登出 Bilibili，清除凭据。"""
        self.bili_client.credential = None
        await self.data_manager.clear_credential()
        self.bili_client = BiliClient(
            sessdata=self.cfg.get("sessdata"), proxy=self.proxy
        )
        self.dynamic_listener.bili_client = self.bili_client
        self._start_tasks()
        return MessageEventResult().message("✅ 已登出 Bilibili，凭据已清除。")

    @command("bili_card_style", alias={"卡片样式"})
    @permission_type(PermissionType.ADMIN)
    async def switch_style(self, event: AstrMessageEvent, style: str | None = None):
        """切换动态卡片样式。不带参数可以查看可用的卡片样式列表。"""
        available = get_template_names()

        # 不带参数：显示可用样式列表
        if not style:
            lines = ["📋 卡片样式："]
            for tid in available:
                info = CARD_TEMPLATES[tid]
                current = " ← 当前" if tid == self.style else ""
                lines.append(f"  • {tid}: {info['name']}{current}")
            lines.append("\n使用 /bili_card_style <样式名> 切换样式")
            return MessageEventResult().message("\n".join(lines))

        # 带参数：切换样式
        if style not in available:
            return MessageEventResult().message(
                f"样式 '{style}' 不存在。可用样式：{', '.join(available)}"
            )

        self.style = style
        self.renderer.style = style

        info = CARD_TEMPLATES[style]
        self.cfg["renderer_template"] = style
        self.cfg.save_config()
        return MessageEventResult().message(
            f"✅ 已切换样式为：{info['name']} ({style})"
        )

    @regex(BV)
    async def get_video_info(self, event: AstrMessageEvent):
        if self.enable_parse_BV:
            match_ = re.search(BV, event.message_str, re.IGNORECASE)
            if not match_:
                return
            # 匹配到短链接
            if match_.group(2):
                full_link = match_.group(0)
                converted_url = await self.bili_client.b23_to_bv(full_link)
                if not converted_url:
                    return
                match_bv = re.search(r"(BV[a-zA-Z0-9]+)", converted_url, re.IGNORECASE)
                if match_bv:
                    bvid = match_bv.group(1)
                else:
                    return
            # 匹配到长链接
            elif match_.group(1):
                bvid = match_.group(1)
            # 匹配到纯 BV 号
            elif match_.group(0):
                bvid = match_.group(0)

            video_data = await self.bili_client.get_video_info(bvid=bvid)
            if not video_data:
                return await event.send(
                    MessageChain().message("获取视频信息失败了 (´;ω;`)")
                )
            info = video_data["info"]
            online = video_data["online"]

            owner = info.get("owner") or {}
            staff = info.get("staff") or []
            if staff:
                authors = [
                    {
                        "name": str(s.get("name") or ""),
                        "face": str(s.get("face") or ""),
                        "uid": str(s.get("mid") or ""),
                    }
                    for s in staff
                ]
            else:
                authors = [
                    {
                        "name": str(owner.get("name") or ""),
                        "face": str(owner.get("face") or ""),
                        "uid": str(owner.get("mid") or ""),
                    }
                ]

            def _fmt_time(ts) -> str:
                try:
                    return time.strftime("%Y-%m-%d", time.localtime(int(ts)))
                except Exception:
                    return ""

            stat = info.get("stat") or {}
            payload = RenderPayload(
                name=str(owner.get("name") or ""),
                avatar=str(owner.get("face") or ""),
                authors=authors,
                title=str(info.get("title") or ""),
                desc=str(info.get("desc") or ""),
                pub_time=_fmt_time(info.get("pubdate")),
                label="视频动态",
                stat_view=str(stat.get("view", 0)),
                stat_like=str(stat.get("like", 0)),
                stat_coin=str(stat.get("coin", 0)),
                online=str((online or {}).get("total", 0)),
                image_urls=[str(info.get("pic") or "")],
            )

            img_path = await self.renderer.render_dynamic(payload)
            if img_path:
                try:
                    await event.send(MessageChain().file_image(img_path))
                    return
                except Exception as e:
                    logger.warning(f"视频卡片上传失败，降级为图文: {e}")
            else:
                logger.warning("视频卡片生成失败，降级为图文")

            lines = [
                payload.title,
                payload.desc,
                f"播放 {payload.stat_view}  点赞 {payload.stat_like}  投币 {payload.stat_coin}",
                f"总共 {payload.online} 人正在观看",
            ]
            text = "\n".join(filter(None, lines))
            chain = MessageChain().message(text)
            cover = str(info.get("pic") or "").strip()
            if cover:
                chain = chain.url_image(cover)
            try:
                await event.send(chain)
            except Exception as e:
                logger.warning(f"视频图文回退仍上传失败，改发纯文本: {e}")
                await event.send(MessageChain().message(text))

    @command("bili_sub", alias={"订阅动态"})
    async def dynamic_sub(
        self, event: AstrMessageEvent, uid: str = "", input: GreedyStr = ""
    ):
        uid = (uid or "").strip()
        if not uid:
            return MessageEventResult().message(
                "用法：/bili_sub <B站UID> [过滤参数...]\n"
                "更新订阅时只改动给出的项，其余保持不变。\n"
                "示例：/bili_sub 100870070 video 抽奖\n"
                "      /bili_sub 100870070 +regex=新词 img_forward=on"
            )
        spec = self._parse_sub_args(input)

        if (spec.at_all or spec.live_atall) and not event.is_admin():
            if event.role not in ("admin", "owner", "founder"):
                return MessageEventResult().message(
                    "权限不足：只有管理员可以设置 @全体成员 相关选项。"
                )

        sub_user = event.unified_msg_origin
        if not uid.isdigit():
            return MessageEventResult().message(
                "UID 格式错误。用法：/bili_sub <B站UID> [过滤参数...]"
            )
        uid_int = int(uid)

        add_sub_users = [event.get_sender_id()] if spec.at_sub else None
        rm_sub_users = [event.get_sender_id()] if spec.unat_sub else None

        warning = ""
        if (spec.at_all or spec.live_atall) and getattr(
            event, "get_group_id", lambda: None
        )():
            permit_atall = await self.dynamic_listener._check_atall_permission(
                sub_user, True
            )
            if not permit_atall:
                warning = "\n⚠️ 注意：机器人目前在本会话无 @全体成员 的权限，此项设置可能不会生效（请给予机器人管理员权限）。"

        updated, update_msg = await self._apply_subscription(
            sub_user, uid_int, spec, add_sub_users, rm_sub_users
        )
        if updated and warning:
            update_msg += warning

        # 首次订阅与更新过滤条件统一走卡片推送，卡片附 sub_list 同款过滤规则展示。
        title = "订阅更新" if updated else "订阅成功"
        record = self.data_manager.get_subscription(sub_user, uid_int)

        try:
            usr_info, msg = await self.bili_client.get_user_info(uid_int)
        except Exception as e:
            logger.error(f"获取用户信息失败: {e}")
            if updated:
                return MessageEventResult().message(update_msg)
            return MessageEventResult().message("订阅成功，但获取 UP 主信息失败。")
        if not usr_info:
            if updated:
                return MessageEventResult().message(update_msg)
            return MessageEventResult().message(
                f"订阅成功，但获取 UP 主信息失败: {msg}"
            )

        payload = self._build_subscription_payload(
            uid_int,
            str(usr_info.get("name", "Unknown")),
            str(usr_info.get("face", "")),
            note=warning,
            title=title,
            record=record,
            img_forward=bool(record.img_forward) if record else False,
        )
        return await self._send_subscription_result(event, payload)

    @staticmethod
    def _extract_sid(sub_user: str) -> str:
        parts = sub_user.split(":", 2)
        return parts[2] if len(parts) >= 3 else sub_user

    @staticmethod
    def _extract_chat_type(sub_user: str) -> str:
        parts = sub_user.split(":", 2)
        return "Group" if len(parts) >= 2 and "Group" in parts[1] else "Private"

    async def _resolve_session_display_name(self, sub_user: str) -> str:
        """解析会话显示名：群聊返回群名，私聊返回 QQ 昵称。解析失败返回空字符串。"""
        try:
            platform_id, message_type, session_id = sub_user.split(":", 2)
        except ValueError:
            return ""

        platform_inst = self.context.get_platform_inst(platform_id)
        if not platform_inst:
            return ""

        client = platform_inst.get_client()
        if not client or not hasattr(client, "call_action"):
            return ""

        def _unwrap(raw: Any) -> dict:
            if not isinstance(raw, dict):
                return {}
            data = raw.get("data")
            return data if isinstance(data, dict) else raw

        try:
            if message_type == "GroupMessage":
                group_id = int(session_id) if str(session_id).isdigit() else session_id
                raw = await client.call_action("get_group_info", group_id=group_id)
                return str(_unwrap(raw).get("group_name") or "")
            else:
                user_id = int(session_id) if str(session_id).isdigit() else session_id
                raw = await client.call_action(
                    "get_stranger_info", user_id=user_id, no_cache=False
                )
                info = _unwrap(raw)
                return str(info.get("nickname") or info.get("nick") or "")
        except Exception as e:
            logger.debug(f"解析会话显示名失败 ({sub_user}): {e}")
            return ""

    async def _build_sub_list_item(
        self, uid_sub_data: SubscriptionRecord, img_forward: bool = False
    ) -> dict:
        uid = uid_sub_data.uid
        name = str(uid)
        face = ""
        try:
            info, _ = await self.bili_client.get_user_info(int(uid))
            if info:
                if info.get("name"):
                    name = str(info["name"])
                face = str(info.get("face") or "")
        except Exception as e:
            logger.warning(f"获取 UP 主信息失败 (UID: {uid}): {e}")

        return {
            "uid": str(uid),
            "name": name,
            "face": face,
            "filter_types": list(uid_sub_data.filter_types),
            "filter_regex": list(uid_sub_data.filter_regex),
            "live_atall": bool(uid_sub_data.live_atall),
            "at_all": bool(uid_sub_data.at_all),
            "at_sub_users": list(uid_sub_data.at_sub_users),
            "img_forward": bool(img_forward),
        }

    async def _render_sub_list_image(
        self,
        title: str,
        sessions: List[dict],
        total: int,
    ) -> str | None:
        """渲染订阅列表卡片，返回图片路径；失败返回 None。"""
        context = {
            "title": title,
            "total": total,
            "sessions": sessions,
        }

        try:
            with open(SUB_LIST_TEMPLATE_PATH, "r", encoding="utf-8") as f:
                tmpl = f.read()
        except Exception as e:
            logger.error(f"加载订阅列表模板失败: {e}")
            return None

        options = {
            "full_page": True,
            "type": "jpeg",
            "quality": 95,
            "scale": "device",
            "device_scale_factor_level": "ultra",
            "viewport_height": 1,
        }

        try:
            img_path = await self.html_render(
                tmpl=tmpl, data=context, return_url=False, options=options
            )
        except Exception as e:
            logger.error(f"渲染订阅列表失败: {e}")
            return None

        if (
            img_path
            and os.path.exists(img_path)
            and self.renderer._validate_image(img_path)
        ):
            return img_path
        return None

    async def _send_sub_list_image(
        self,
        event: AstrMessageEvent,
        title: str,
        sessions: List[dict],
        total: int,
    ) -> MessageEventResult:
        img_path = await self._render_sub_list_image(title, sessions, total)
        if not img_path:
            return MessageEventResult().message("订阅列表渲染失败")

        platform_name = self.dynamic_listener._resolve_platform_name(
            event.unified_msg_origin
        )
        if is_height_valid(img_path, platform_name):
            chain_parts = [Image.fromFileSystem(img_path)]
        else:
            timestamp = int(time.time())
            filename = f"bilibili_sub_list_{timestamp}.jpg"
            chain_parts = [File(file=img_path, name=filename)]

        return MessageEventResult(chain=chain_parts, use_t2i_=False)

    @command("bili_sub_list", alias={"订阅列表"})
    async def sub_list(self, event: AstrMessageEvent):
        """查看 bilibili 动态监控列表（以图片形式展示）"""
        sub_user = event.unified_msg_origin
        subs = self.data_manager.get_subscriptions_by_user(sub_user)

        if not subs:
            return MessageEventResult().message("无订阅")

        items = [
            await self._build_sub_list_item(
                s, img_forward=bool(s.img_forward)
            )
            for s in subs
        ]
        sessions = [
            {
                "sid": self._extract_sid(sub_user),
                "chat_type": self._extract_chat_type(sub_user),
                "display_name": await self._resolve_session_display_name(sub_user),
                "subs": items,
            }
        ]
        return await self._send_sub_list_image(
            event, "B站订阅列表", sessions, len(items)
        )

    @command("bili_sub_del", alias={"订阅删除"})
    async def sub_del(self, event: AstrMessageEvent, uid: str = ""):
        """删除 bilibili 动态监控"""
        sub_user = event.unified_msg_origin
        uid = (uid or "").strip()
        if not uid:
            return MessageEventResult().message("用法：/bili_sub_del <B站UID>")
        if not uid.isdigit():
            return MessageEventResult().message(
                "UID 格式错误。用法：/bili_sub_del <B站UID>"
            )

        uid2del = int(uid)
        record = self.data_manager.get_subscription(sub_user, uid2del)
        if not await self.data_manager.remove_subscription(sub_user, uid2del):
            return MessageEventResult().message("未找到指定的订阅")

        name = ""
        try:
            usr_info, _ = await self.bili_client.get_user_info(uid2del)
            if usr_info:
                name = str(usr_info.get("name", "") or "")
        except Exception as e:
            logger.warning(f"删除订阅后获取 UP 主信息失败: {e}")
        if name:
            return MessageEventResult().message(f"删除成功：{name} (UID: {uid2del})")
        return MessageEventResult().message(f"删除成功 (UID: {uid2del})")

    @permission_type(PermissionType.ADMIN)
    @command("bili_global_del", alias={"全局删除"})
    async def global_sub_del(self, event: AstrMessageEvent, raw_args: GreedyStr = ""):
        """管理员指令。通过 UMO 删除某一个群聊或者私聊的所有订阅。
        用法: /bili_global_del <UMO>
        UMO 格式: <平台名>:<消息类型>:<会话ID>（平台名可能包含空格，需用「」包裹）
        """
        raw = raw_args.strip()
        if not raw:
            return MessageEventResult().message(
                "用法：/bili_global_del <UMO>。使用 /sid 指令查看当前会话的 UMO。"
            )

        umo = None

        if raw.startswith("「"):
            end_idx = raw.find("」")
            if end_idx == -1:
                return MessageEventResult().message(
                    "UMO 格式错误：请使用「」包裹 UMO，例如: 「QQ 12345:GroupMessage:67890」"
                )
            umo = raw[1:end_idx]
        else:
            parts = raw.split()
            if parts:
                umo = parts[0]

        if not umo or not is_valid_umo(umo):
            return MessageEventResult().message(
                "请提供正确的UMO。使用 /sid 指令查看当前会话的 UMO 或参考 WebUI-自定义规则。"
            )

        msg = await self.data_manager.remove_all_for_user(umo)
        return MessageEventResult().message(msg)

    @permission_type(PermissionType.ADMIN)
    @command("bili_clear", alias={"清空订阅"})
    async def clear_sub(self, event: AstrMessageEvent, raw_args: GreedyStr = ""):
        """管理员指令。清空所有订阅；可指定 SID 仅清空该会话的订阅。
        用法: /bili_clear 或 /bili_clear <SID>
        """
        raw = (raw_args or "").strip()
        if not raw:
            count = await self.data_manager.clear_all_subscriptions()
            if count:
                return MessageEventResult().message(
                    f"已清空所有订阅（共 {count} 个会话）。"
                )
            return MessageEventResult().message("当前没有任何订阅。")

        msg = await self.data_manager.remove_all_for_user(raw)
        return MessageEventResult().message(msg)

    @permission_type(PermissionType.ADMIN)
    @command("bili_global_sub", alias={"全局订阅"})
    async def global_sub_add(self, event: AstrMessageEvent, raw_args: GreedyStr = ""):
        """管理员指令。通过 UMO 和 UID 添加某一个用户的所有订阅。
        用法: /bili_global_sub <UMO> <UID> [过滤参数]
        UMO 格式: <平台名>:<消息类型>:<会话ID>（平台名可能包含空格）
        """
        raw = raw_args.strip()
        if not raw:
            return MessageEventResult().message(
                "用法：/bili_global_sub <UMO> <UID> [过滤参数...]"
            )

        umo = None
        uid = None
        input_str = ""

        if raw.startswith("「"):
            # 主要模式：UMO 被「」包裹，支持平台名含空格的情况
            end_idx = raw.find("」")
            if end_idx == -1:
                return MessageEventResult().message(
                    "UMO 格式错误：请使用「」包裹 UMO，例如: 「QQ 12345:GroupMessage:67890」"
                )
            umo = raw[1:end_idx]
            rest = raw[end_idx + 1 :].strip().split()
            if rest:
                uid = rest[0]
                input_str = " ".join(rest[1:])
        else:
            # 兜底模式：无括号的简单 UMO 格式（兼容旧用法）
            parts = raw.split()
            if len(parts) >= 2:
                umo = parts[0]
                uid = parts[1]
                input_str = " ".join(parts[2:])

        if not umo or not uid or not is_valid_umo(umo) or not uid.isdigit():
            return MessageEventResult().message(
                "请提供正确的UMO与UID。使用 /sid 指令查看当前会话的 UMO 或参考 WebUI-自定义规则。"
            )
        spec = self._parse_sub_args(input_str)
        uid_int = int(uid)

        warning = ""
        if spec.at_all or spec.live_atall:
            permit_atall = await self.dynamic_listener._check_atall_permission(
                umo, True
            )
            if not permit_atall:
                warning = "\n⚠️ 注意：机器人目前在目标会话无 @全体成员 的权限，此项设置可能不会生效（请检查机器人权限）。"

        updated, update_msg = await self._apply_subscription(umo, uid_int, spec)
        if updated:
            if warning:
                update_msg += warning
            return MessageEventResult().message(update_msg)
        return MessageEventResult().message(
            f"订阅完成，已为「{umo}」添加订阅{uid_int}，详情见日志。{warning}"
        )

    @permission_type(PermissionType.ADMIN)
    @command("bili_global_list", alias={"全局列表"})
    async def global_list(self, event: AstrMessageEvent):
        """管理员指令。以图片形式查看所有订阅者"""
        all_subs = self.data_manager.get_all_subscriptions()
        if not all_subs:
            return MessageEventResult().message("没有任何会话订阅过。")

        sessions = []
        total = 0
        for sub_user, sub_list in all_subs.items():
            items = [
                await self._build_sub_list_item(
                    s, img_forward=bool(s.img_forward)
                )
                for s in sub_list
            ]
            total += len(items)
            sessions.append(
                {
                    "sid": self._extract_sid(sub_user),
                    "chat_type": self._extract_chat_type(sub_user),
                    "display_name": await self._resolve_session_display_name(sub_user),
                    "subs": items,
                }
            )

        return await self._send_sub_list_image(
            event, "全局订阅列表", sessions, total
        )

    @event_message_type(EventMessageType.ALL)
    async def parse_miniapp(self, event: AstrMessageEvent):
        if self.enable_parse_miniapp:
            for msg_element in event.message_obj.message:
                if (
                    hasattr(msg_element, "type")
                    and msg_element.type == "Json"
                    and hasattr(msg_element, "data")
                ):
                    json_string = msg_element.data

                    try:
                        if isinstance(json_string, dict):
                            parsed_data = json_string
                        else:
                            parsed_data = json.loads(json_string)
                        meta = parsed_data.get("meta", {})
                        detail_1 = meta.get("detail_1", {})
                        title = detail_1.get("title")
                        qqdocurl = detail_1.get("qqdocurl")
                        desc = detail_1.get("desc")

                        if title == "哔哩哔哩" and qqdocurl:
                            if "https://b23.tv" in qqdocurl:
                                qqdocurl = await self.bili_client.b23_to_bv(qqdocurl)
                            ret = f"标题: {desc}\n链接: {qqdocurl}"
                            await event.send(MessageChain().message(ret))
                        news = meta.get("news", {})
                        tag = news.get("tag", "")
                        jumpurl = news.get("jumpUrl", "")
                        title = news.get("title", "")
                        if tag == "哔哩哔哩" and jumpurl:
                            if "https://b23.tv" in jumpurl:
                                jumpurl = await self.bili_client.b23_to_bv(jumpurl)
                            ret = f"标题: {title}\n链接: {jumpurl}"
                            await event.send(MessageChain().message(ret))
                    except json.JSONDecodeError:
                        logger.error(f"Failed to decode JSON string: {json_string}")
                    except Exception as e:
                        logger.error(f"An error occurred during JSON processing: {e}")

    @permission_type(PermissionType.ADMIN)
    @command("bili_sub_test", alias={"订阅测试"})
    async def sub_test(self, event: AstrMessageEvent, uid: str = ""):
        """测试订阅功能。仅测试获取动态与渲染图片功能，不保存订阅信息。"""
        sub_user = event.unified_msg_origin
        uid = (uid or "").strip()
        if not uid:
            return MessageEventResult().message("用法：/bili_sub_test <B站UID>")
        try:
            uid_int = int(uid)
        except (TypeError, ValueError):
            return MessageEventResult().message(
                "UID 格式错误。用法：/bili_sub_test <B站UID>"
            )

        dyn = await self.bili_client.get_latest_dynamics(uid_int)
        if not dyn:
            return MessageEventResult().message("未获取到动态数据，请稍后重试。")

        sub_data = self.data_manager.get_subscription(sub_user, uid_int)
        if not sub_data:
            # 测试指令：未订阅时构造临时记录，默认开启图片转发以便演示原图合并转发
            sub_data = SubscriptionRecord(uid=uid_int, img_forward=True)

        result_list = self.dynamic_listener._parse_and_filter_dynamics(
            dyn,
            sub_data,
        )

        render_data: RenderPayload | None = None
        # dyn_id = None
        for result in result_list or []:
            if result.has_payload():
                render_data = result.payload
                # dyn_id = result.dyn_id
                break

        if not render_data:
            return MessageEventResult().message(
                "没有可用于测试推送的动态（可能没有新动态、都被过滤掉，或动态类型暂不支持）。"
            )

        # 测试命令需要每次基于当前代码重新构造消息，避免命中同 dyn_id 的历史缓存。
        await self.dynamic_listener._handle_new_dynamic(
            sub_user, render_data, None, sub_data=sub_data
        )
        event.stop_event()

    @command("bili_help", alias={"b站帮助"})
    async def bili_help(self, event: AstrMessageEvent):
        """查看 bilibili 插件命令与过滤规则说明。"""
        return MessageEventResult().message(BILI_HELP_TEXT)

    @staticmethod
    def _make_mock_image(rgb: Tuple[int, int, int], size: Tuple[int, int] = (640, 360)) -> str:
        """生成纯色占位图并转为 Base64 Data URI，供样式测试卡片使用。"""
        try:
            from PIL import Image as PILImage

            img = PILImage.new("RGB", size, rgb)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception as e:
            logger.warning(f"生成样式测试占位图失败: {e}")
            return ""

    def _save_mock_image(
        self, rgb: Tuple[int, int, int], name: str = "bili_style_mock.jpg"
    ) -> str | None:
        """生成纯色占位图并保存为本地文件（合并转发节点需要本地路径）。"""
        try:
            from PIL import Image as PILImage

            img = PILImage.new("RGB", (640, 360), rgb)
            path = os.path.join(tempfile.gettempdir(), name)
            img.save(path, format="JPEG", quality=85)
            return path
        except Exception as e:
            logger.warning(f"保存样式测试占位图失败: {e}")
            return None

    def _build_style_test_payloads(self) -> List[Tuple[str, RenderPayload]]:
        """构建五种动态类型的样式测试卡片数据（含发布时间/类型标注）。"""
        img_pink = self._make_mock_image((251, 114, 153))
        img_green = self._make_mock_image((124, 179, 66))
        img_blue = self._make_mock_image((64, 132, 220))
        avatar = self._make_mock_image((120, 120, 130), size=(200, 200))
        banner = image_to_base64(BANNER_PATH)
        video_url = "https://www.bilibili.com/video/BV1StyleTest"
        live_url = "https://live.bilibili.com/10000"

        return [
            (
                "视频动态",
                RenderPayload(
                    banner=banner,
                    name="测试UP主",
                    avatar=avatar,
                    type="DYNAMIC_TYPE_AV",
                    label="视频动态",
                    pub_time="2026-09-08 20:00",
                    title="【样式测试】视频标题",
                    text="投稿了新视频<br>这里是动态正文示例",
                    image_urls=[img_pink],
                    url=video_url,
                    qrcode=create_qrcode(video_url),
                    stat_view="12345",
                    stat_like="678",
                    stat_coin="90",
                ),
            ),
            (
                "图文动态",
                RenderPayload(
                    banner=banner,
                    name="测试UP主",
                    avatar=avatar,
                    type="DYNAMIC_TYPE_DRAW",
                    label="图文动态",
                    pub_time="2026-09-08 18:30",
                    text="发布了新图文动态<br>这里是图文动态正文示例",
                    image_urls=[img_pink, img_green, img_blue],
                ),
            ),
            (
                "转发动态",
                RenderPayload(
                    banner=banner,
                    name="测试UP主",
                    avatar=avatar,
                    type="DYNAMIC_TYPE_FORWARD",
                    label="转发动态",
                    pub_time="2026-09-08 17:00",
                    text="转发了新动态<br>转发时说的话",
                    forward=ForwardPayload(
                        name="原作者",
                        avatar=avatar,
                        type="DYNAMIC_TYPE_AV",
                        label="视频动态",
                        title="被转发的视频标题",
                        text="被转发原文内容",
                        image_urls=[img_green],
                    ),
                ),
            ),
            (
                "直播开播",
                RenderPayload(
                    banner=banner,
                    name="测试UP主",
                    avatar=avatar,
                    label="直播动态",
                    pub_time="2026-09-08 19:00",
                    title="【样式测试】直播间标题",
                    text="📣 你订阅的UP 「测试UP主」 开播了！",
                    image_urls=[img_blue],
                    url=live_url,
                    qrcode=create_qrcode(live_url),
                ),
            ),
            (
                "直播下播",
                RenderPayload(
                    banner=banner,
                    name="测试UP主",
                    avatar=avatar,
                    label="直播动态",
                    pub_time="2026-09-08 19:00",
                    title="【样式测试】直播间标题",
                    text=(
                        "📣 你订阅的UP 「测试UP主」 下播了！<br>"
                        "本场直播时长：2小时30分钟0秒"
                    ),
                    image_urls=[img_blue],
                    url=live_url,
                    qrcode=create_qrcode(live_url),
                ),
            ),
        ]

    @permission_type(PermissionType.ADMIN)
    @command("bili_style_test", alias={"样式测试"})
    async def style_test(self, event: AstrMessageEvent, style: str | None = None):
        """渲染样式测试卡片，快速预览各动态类型与订阅相关样式。用法: /bili_style_test [样式名]"""
        available = get_template_names()
        if style and style not in available:
            return MessageEventResult().message(
                f"样式 '{style}' 不存在。可用样式：{', '.join(available)}"
            )
        target_style = style or self.style
        payloads = self._build_style_test_payloads()
        sub_card_count = 4  # 多图裁剪图文 / 订阅成功 / 订阅更新 / 订阅列表

        await event.send(
            MessageChain().message(
                f"样式测试（{target_style}）：共 {len(payloads) + sub_card_count} 张卡片"
                "与 2 条文本示例，正在渲染…"
            )
        )
        for name, payload in payloads:
            try:
                img_path = await self.renderer.render_dynamic(payload, style=target_style)
            except Exception as e:
                logger.error(f"样式测试渲染失败 ({name}): {e}")
                img_path = None
            if img_path:
                await event.send(
                    MessageChain().message(f"【{target_style}】{name}").file_image(img_path)
                )
            else:
                await event.send(
                    MessageChain().message(f"【{target_style}】{name} 渲染失败 (´;ω;`)")
                )

        # —— 多图裁剪 + 原图合并转发演示 ——
        banner = image_to_base64(BANNER_PATH)
        avatar = self._make_mock_image((120, 120, 130), size=(200, 200))
        img_pink = self._make_mock_image((251, 114, 153))
        img_green = self._make_mock_image((124, 179, 66))
        img_blue = self._make_mock_image((64, 132, 220))
        multi_payload = RenderPayload(
            banner=banner,
            name="测试UP主",
            avatar=avatar,
            type="DYNAMIC_TYPE_DRAW",
            label="图文动态",
            pub_time="2026-09-09 01:00",
            text="发布了新图文动态<br>图片数大于 1：推送卡片的多图网格会裁剪",
            image_urls=[img_pink, img_green, img_blue],
        )
        try:
            img_path = await self.renderer.render_dynamic(
                multi_payload, style=target_style
            )
        except Exception as e:
            logger.error(f"样式测试渲染失败 (多图图文动态): {e}")
            img_path = None
        if img_path:
            await event.send(
                MessageChain().message(f"【{target_style}】多图图文动态(裁剪)").file_image(img_path)
            )
        else:
            await event.send(
                MessageChain().message(f"【{target_style}】多图图文动态(裁剪) 渲染失败 (´;ω;`)")
            )
        node_images = []
        for idx, rgb in enumerate(
            ((251, 114, 153), (124, 179, 66), (64, 132, 220)), start=1
        ):
            path = self._save_mock_image(rgb, name=f"bili_style_mock_{idx}.jpg")
            if path:
                node_images.append(Image.fromFileSystem(path))
        if node_images:
            try:
                node = Node(uin=0, name="测试UP主", content=node_images)
                await self.dynamic_listener.dispatcher.publish(
                    SubscriptionNotification(
                        sub_user=event.unified_msg_origin,
                        chain_parts=[node],
                        send_node=False,
                        category="style_test",
                        dyn_id=None,
                        meta={"kind": "original_images"},
                    )
                )
            except Exception as e:
                logger.warning(f"样式测试合并转发演示失败: {e}")
                await event.send(
                    MessageChain().message("（原图合并转发演示失败，请查看日志）")
                )

        # —— 订阅成功 / 订阅更新卡片 ——
        sub_card_cases = [
            ("订阅成功", "订阅成功", True),
            ("订阅更新", "订阅更新", False),
        ]
        for case_name, card_title, img_fw in sub_card_cases:
            context = {
                "uid": "100870070",
                "name": "测试UP主",
                "avatar": avatar,
                "note": "",
                "title": card_title,
                "filter_types": ["video", "draw"],
                "filter_regex": ["抽奖|中奖"],
                "live_atall": True,
                "at_all": False,
                "at_sub_users": [],
                "img_forward": img_fw,
            }
            img_path = await self._render_sub_success(context)
            if img_path:
                await event.send(
                    MessageChain().message(f"【订阅卡片】{case_name}").file_image(img_path)
                )
            else:
                await event.send(
                    MessageChain().message(f"【订阅卡片】{case_name} 渲染失败 (´;ω;`)")
                )

        # —— 订阅列表卡片（图片转发状态显示在每条订阅卡片里） ——
        sub_item_a = {
            "uid": "100870070",
            "name": "测试UP主",
            "face": avatar,
            "filter_types": ["video", "draw"],
            "filter_regex": ["抽奖|中奖"],
            "live_atall": True,
            "at_all": False,
            "at_sub_users": [],
            "img_forward": True,
        }
        sub_item_b = {
            "uid": "200200200",
            "name": "另一位UP主",
            "face": avatar,
            "filter_types": [],
            "filter_regex": [],
            "live_atall": False,
            "at_all": True,
            "at_sub_users": ["测试用户"],
            "img_forward": False,
        }
        list_sessions = [
            {
                "sid": "123456",
                "chat_type": "GroupMessage",
                "display_name": "测试群聊",
                "subs": [sub_item_a],
            },
            {
                "sid": "888888",
                "chat_type": "FriendMessage",
                "display_name": "",
                "subs": [sub_item_b],
            },
        ]
        img_path = await self._render_sub_list_image(
            "B站订阅列表", list_sessions, len(list_sessions)
        )
        if img_path:
            await event.send(
                MessageChain().message("【订阅列表】含图片转发状态").file_image(img_path)
            )
        else:
            await event.send(
                MessageChain().message("【订阅列表】渲染失败 (´;ω;`)")
            )

        # —— 文本示例：订阅删除 / 图片转发状态 ——
        await event.send(
            MessageChain().message(
                "【文本示例·订阅删除】\n删除成功：测试UP主 (UID: 100870070)"
            )
        )
        await event.send(
            MessageChain().message(
                "【文本示例·图片转发状态】\n"
                "当前会话多图原图转发：开启\n"
                "全局状态：未强制（由各会话自行设置）"
            )
        )
        event.stop_event()

    @command("bili_img_forward", alias={"图片转发"})
    async def img_forward_toggle(
        self, event: AstrMessageEvent, raw_args: GreedyStr = ""
    ):
        """批量开关当前会话所有订阅：多图动态推送时以合并消息附带原图。
        用法: /bili_img_forward on|off
        """
        sub_user = event.unified_msg_origin
        arg = (raw_args or "").strip().lower()
        if arg not in ("on", "开", "开启", "off", "关", "关闭"):
            return MessageEventResult().message("用法：/bili_img_forward on|off")
        enabled = arg in ("on", "开", "开启")
        changed = await self.data_manager.set_img_forward_for_user(
            sub_user, enabled
        )
        subs = self.data_manager.get_subscriptions_by_user(sub_user) or []
        if not subs:
            return MessageEventResult().message(
                "本会话暂无订阅，图片转发跟随每条订阅设置（可用 /bili_sub <UID> img_forward=on 单独开启）。"
            )
        state = "开启" if enabled else "关闭"
        return MessageEventResult().message(
            f"已{state}本会话 {changed} 条订阅的多图原图转发。"
        )

    @permission_type(PermissionType.ADMIN)
    @command("bili_img_forward_global", alias={"全局图片转发"})
    async def img_forward_global(
        self, event: AstrMessageEvent, raw_args: GreedyStr = ""
    ):
        """管理员指令。批量修改指定会话所有订阅的多图原图转发开关。
        用法: /bili_img_forward_global <UMO> on|off
        UMO 格式: <平台名>:<消息类型>:<会话ID>（平台名可能包含空格，需用「」包裹）
        """
        raw = (raw_args or "").strip()
        if not raw:
            return MessageEventResult().message(
                "用法：/bili_img_forward_global <UMO> on|off。"
                "使用 /sid 指令查看当前会话的 UMO。"
            )

        umo = None
        action = ""
        if raw.startswith("「"):
            end_idx = raw.find("」")
            if end_idx == -1:
                return MessageEventResult().message(
                    "UMO 格式错误：请使用「」包裹 UMO，例如: "
                    "「QQ 12345:GroupMessage:67890」 on"
                )
            umo = raw[1:end_idx]
            rest = raw[end_idx + 1 :].strip().lower()
            action = rest.split()[0] if rest.split() else ""
        else:
            parts = raw.split()
            if len(parts) >= 2:
                umo = parts[0]
                action = parts[1].lower()

        if not umo or not is_valid_umo(umo):
            return MessageEventResult().message(
                "请提供正确的UMO。使用 /sid 指令查看当前会话的 UMO 或参考 WebUI-自定义规则。"
            )
        if action not in ("on", "开", "开启", "off", "关", "关闭"):
            return MessageEventResult().message(
                "用法：/bili_img_forward_global <UMO> on|off"
            )
        enabled = action in ("on", "开", "开启")
        changed = await self.data_manager.set_img_forward_for_user(umo, enabled)
        state = "开启" if enabled else "关闭"
        return MessageEventResult().message(
            f"已{state}会话 {umo} 的 {changed} 条订阅的多图原图转发。"
        )

    async def terminate(self):
        if (
            hasattr(self, "dynamic_listener_task")
            and self.dynamic_listener_task
            and not self.dynamic_listener_task.done()
        ):
            self.dynamic_listener_task.cancel()
            try:
                await self.dynamic_listener_task
            except asyncio.CancelledError:
                logger.info(
                    "bilibili dynamic_listener task was successfully cancelled during terminate."
                )
            except Exception as e:
                logger.error(
                    f"Error awaiting cancellation of dynamic_listener task: {e}"
                )
