import html
import re
from typing import Any, Optional

from astrbot.api import FunctionTool
from astrbot.api.event import MessageEventResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

DEFAULT_LIMIT = 8
MAX_LIMIT = 20
MIN_LIMIT = 1
DEFAULT_PAGE = 1
HOT_SORT = "hot"
VALID_SEARCH_SORT = {"totalrank", "click", "pubdate", "dm", "stow", "scores"}


def _normalize_limit(limit: int) -> int:
    if limit < MIN_LIMIT:
        return MIN_LIMIT
    if limit > MAX_LIMIT:
        return MAX_LIMIT
    return limit


def _normalize_page(page: int) -> int:
    return max(DEFAULT_PAGE, page)


def _normalize_keyword(keyword: str) -> str:
    return keyword.strip()


def _normalize_sort(sort: str, *, has_keyword: bool) -> str:
    value = sort.strip().lower()
    if not value:
        return HOT_SORT if not has_keyword else "totalrank"
    if has_keyword:
        if value == HOT_SORT:
            return "totalrank"
        if value in VALID_SEARCH_SORT:
            return value
        return "totalrank"
    return HOT_SORT


def _clean_title(raw: Any) -> str:
    if not isinstance(raw, str):
        return "未知标题"
    unescaped = html.unescape(raw)
    cleaned = re.sub(r"<[^>]+>", "", unescaped).strip()
    return cleaned or "未知标题"


def _format_count(raw: Any) -> str:
    if isinstance(raw, str):
        text = raw.strip()
        return text or "未知"
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value >= 100000000:
            return f"{value / 100000000:.1f}亿"
        if value >= 10000:
            return f"{value / 10000:.1f}万"
        return str(int(value))
    return "未知"


def _format_duration(raw: Any) -> str:
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, (int, float)) and raw > 0:
        total = int(raw)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"
    return "未知"


def _format_pubdate(raw: Any) -> str:
    """将 B 站返回的发布时间戳格式化为 YYYY-MM-DD，失败返回空字符串。"""
    try:
        import time as _time

        ts = int(raw)
        if ts <= 0:
            return ""
        return _time.strftime("%Y-%m-%d", _time.localtime(ts))
    except Exception:
        return ""


def _normalize_image_url(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    url = raw.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return url


def _extract_hot_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("list", [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _extract_search_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("result", [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _to_video_dict(item: dict[str, Any], *, source: str) -> dict[str, str]:
    title = _clean_title(item.get("title"))
    bvid = item.get("bvid") or "未知BV"
    url = f"https://www.bilibili.com/video/{bvid}"
    duration = _format_duration(item.get("duration"))
    cover = _normalize_image_url(item.get("pic"))

    if source == HOT_SORT:
        owner = item.get("owner") or {}
        author = owner.get("name") if isinstance(owner, dict) else "未知UP"
        stat = item.get("stat") or {}
        if isinstance(stat, dict):
            play = _format_count(stat.get("view"))
            danmaku = _format_count(stat.get("danmaku"))
        else:
            play = "未知"
            danmaku = "未知"
    else:
        author = item.get("author") or "未知UP"
        play = _format_count(item.get("play"))
        danmaku = _format_count(item.get("video_review"))

    return {
        "title": title,
        "author": str(author),
        "duration": duration,
        "play": play,
        "danmaku": danmaku,
        "pubdate": _format_pubdate(item.get("pubdate")),
        "cover": cover,
        "url": url,
        "bvid": str(bvid),
    }


@dataclass
class BiliSearchHotVideosTool(FunctionTool):
    name: str = "bili_search_hot_videos"
    description: str = (
        "搜索/查找哔哩哔哩视频并把结果直接发给用户。调用前必须先判断用户要几个视频，"
        "再据此设置 limit：\n"
        "【只要一个视频 → limit 必须为 1】用户说“给我一个/帮我找一个/找个/推荐一个/"
        "来一个/来个/发一个 XX 视频”，或句中用“一个/部/条”等单数量词指代结果时，"
        "limit 一律设为 1，禁止设为 5 或其他数值。此时返回单个视频的图文卡片。\n"
        "【要多个视频 → limit 设为 5 左右】用户说“帮我找下/有没有/有哪些 XX 相关的视频”"
        "“来几个/多找几个”“找个列表”等复数措辞，或明确要求对比、汇总多个结果时，"
        "limit 设为 5 左右，返回列表图。\n"
        "【例】“给我一个纳西索斯测评视频”→ limit=1；"
        "“帮我找下纳西索斯测评相关的视频”→ limit=5。\n"
        "无关键词时返回全站热门；有关键词时按关键词搜索。"
    )
    bili_client: Any = None
    renderer: Any = None
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "可选。搜索关键词。留空时返回全站热门视频。",
                },
                "sort": {
                    "type": "string",
                    "description": (
                        "排序方式。无关键词时固定为 hot；有关键词时可用 "
                        "totalrank/click/pubdate/dm/stow/scores。默认 hot。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "返回数量，范围 1-20。用户只要一个视频（如“给我一个/找一个 XX 视频”）"
                        "时必须设为 1，不要设为 5；用户要列表/多个视频时设为 5 左右。"
                        "判断不了几个时再看原句数量词。"
                    ),
                    "minimum": MIN_LIMIT,
                    "maximum": MAX_LIMIT,
                },
                "page": {
                    "type": "integer",
                    "description": "页码，默认 1。",
                    "minimum": DEFAULT_PAGE,
                },
                "tid": {
                    "type": "integer",
                    "description": "可选。视频分区 tid，仅在关键词搜索模式下生效。",
                },
            },
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        keyword: str = "",
        sort: str = HOT_SORT,
        limit: int = DEFAULT_LIMIT,
        page: int = DEFAULT_PAGE,
        tid: Optional[int] = None,
    ) -> MessageEventResult | str:
        _ = context
        if self.bili_client is None:
            raise RuntimeError("bili_client 未初始化")

        normalized_keyword = _normalize_keyword(keyword)
        normalized_limit = _normalize_limit(limit)
        normalized_page = _normalize_page(page)
        normalized_sort = _normalize_sort(sort, has_keyword=bool(normalized_keyword))

        if normalized_keyword:
            payload = await self.bili_client.search_videos(
                normalized_keyword,
                order=normalized_sort,
                page=normalized_page,
                page_size=normalized_limit,
                video_zone_type=tid,
            )
            if not payload:
                return "搜索视频失败，请稍后重试。"
            items = _extract_search_items(payload)
            source = "search"
        else:
            payload = await self.bili_client.get_hot_videos(
                pn=normalized_page, ps=normalized_limit
            )
            if not payload:
                return "获取B站热门视频失败，请稍后重试。"
            items = _extract_hot_items(payload)
            source = HOT_SORT

        if not items:
            return "未找到符合条件的视频。可以换个关键词或排序方式。"

        videos = [
            _to_video_dict(item, source=source)
            for item in items[:normalized_limit]
        ]

        if normalized_limit == 1 and videos:
            v = videos[0]
            text = (
                f"《{v['title']}》\n"
                f"UP主：{v['author']} | 时长：{v['duration']}\n"
                f"播放：{v['play']} | 弹幕：{v['danmaku']}\n"
            )
            if v["pubdate"]:
                text += f"发布时间：{v['pubdate']}\n"
            text += f"链接：{v['url']}"
            result = MessageEventResult()
            if v["cover"]:
                result.url_image(v["cover"])
            result.message(text)
            return result

        list_title = (
            "B站热门视频"
            if not normalized_keyword
            else f"「{normalized_keyword}」搜索结果"
        )
        img_path = None
        if self.renderer is not None:
            try:
                img_path = await self.renderer.render_video_list(
                    videos, title=list_title
                )
            except Exception:
                img_path = None

        if img_path:
            return MessageEventResult().file_image(img_path)

        lines = [
            f"{i}. 《{v['title']}》\nUP主：{v['author']}  时长：{v['duration']}"
            + (f"  发布：{v['pubdate']}" if v["pubdate"] else "")
            + f"\n{v['url']}"
            for i, v in enumerate(videos, start=1)
        ]
        return "\n".join(lines)
