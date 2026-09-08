import os
from typing import Dict

CURRENT_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets")


def _asset_path(*parts: str) -> str:
    return os.path.join(ASSETS_DIR, *parts)


LOGO_PATH = _asset_path("Astrbot.png")
BANNER_PATH = _asset_path("banner.png")
SUB_LIST_TEMPLATE_PATH = _asset_path("sub_list.html")
SUB_SUCCESS_TEMPLATE_PATH = _asset_path("sub_success.html")
VIDEO_LIST_TEMPLATE_PATH = _asset_path("video_list.html")
SUBJECT_LIST_TEMPLATE_PATH = _asset_path("subject_list.html")
BV = r"(?:\?.*)?(?:https?:\/\/)?(?:www\.)?(?:bilibili\.com\/video\/(BV[a-zA-Z0-9]+)|b23\.tv\/([a-zA-Z0-9]+))\/?(?:\?.*)?|BV[a-zA-Z0-9]+"
VALID_FILTER_TYPES = {
    "forward",
    "lottery",
    "video",
    "article",
    "draw",
    "live",
    "forward_lottery",
}
LIVE_ATALL_OPTION = "live_atall"
AT_ALL_OPTION = "at_all"
AT_SUB_OPTION = "at_sub"
UNAT_SUB_OPTION = "unat_sub"
VALID_SUB_OPTIONS = {LIVE_ATALL_OPTION, AT_ALL_OPTION, AT_SUB_OPTION, UNAT_SUB_OPTION}

# 三态开关的"未指定"哨兵（用于订阅级 img_forward 等可选配置）。
# 注意与 None 的区别：None 表示显式清除订阅级覆盖（恢复跟随会话/全局），UNSET 表示本次未提供该项。
UNSET = object()
DATA_PATH = "data/astrbot_plugin_bilibili.json"
DEFAULT_CFG = {
    "bili_sub_list": {},  # sub_user -> [{"uid": "uid", "last": "last_dynamic_id", ...}]
    "credential": None,
    "last_success_sub_notify_ts": 0,
}

# ==================== 模板注册表 ====================
# 集中管理所有可用的卡片模板
# 添加新模板只需在此处注册即可

CARD_TEMPLATES: Dict[str, dict] = {
    "simple": {
        "name": "简约风格",
        "file": "template_simple.html",
        "path": _asset_path("template_simple.html"),
    },
    "mutsumi_dark": {
        "name": "Mutsumi·暗",
        "file": "mutsumi_dark.html",
        "path": _asset_path("mutsumi_dark.html"),
    },
    "mutsumi_light": {
        "name": "Mutsumi·亮",
        "file": "mutsumi_light.html",
        "path": _asset_path("mutsumi_light.html"),
    },
}

# 默认模板
DEFAULT_TEMPLATE = "mutsumi_dark"


def get_template_path(style: str) -> str:
    """获取指定样式的模板路径"""
    template = CARD_TEMPLATES.get(style, CARD_TEMPLATES[DEFAULT_TEMPLATE])
    return template["path"]


def get_template_names() -> list:
    """获取所有模板的 ID 列表"""
    return list(CARD_TEMPLATES.keys())


MAX_ATTEMPTS = 3
RETRY_DELAY = 2
RECENT_DYNAMIC_CACHE = 4
RECONNECT_SILENT_THRESHOLD_SECS = 21600
RECONNECT_SILENT_PADDING_SECS = 60

# /bili_help 帮助文本（不包含测试指令）
# 过滤类型 -> 中文标签（用于订阅列表/订阅成功卡片的展示）
FILTER_TYPE_LABELS = {
    "video": "视频",
    "draw": "图文",
    "forward": "转发",
    "article": "专栏",
    "live": "直播",
    "lottery": "抽奖",
    "forward_lottery": "转发抽奖",
}

BILI_HELP_TEXT = """/bili 插件帮助
📦 订阅相关
  bili_sub <UID> [过滤参数]：订阅UP主动态
  bili_sub_list：查看当前会话订阅列表
  bili_sub_del <UID>：删除订阅
  bili_card_style [样式名]：查看/切换推送卡片样式
  bili_img_forward on|off：多图动态推送时是否以合并消息附带原图
  bili_help：查看本帮助

🚫 过滤参数（跟在 bili_sub 的 UID 之后）
  类型过滤（命中类型不推送）：
    video=视频  draw=图文  forward=转发
    article=专栏  live=直播
    lottery=抽奖  forward_lottery=转发抽奖
    img_forward=裁切图片是否合并转发
  @ 选项：
    live_atall=开播时@全体  at_all=每条推送@全体
    at_sub=开播时@订阅者  unat_sub=取消@订阅者
  正则过滤：其余参数视为正则，动态文本命中则不推送

⚙️ 局部更新（更新订阅时只改动给出的项，其余保持不变）
  type=video,draw 覆盖类型    type= 清空类型
  +type=video 追加类型        -type=video 移除类型
  lottery=1 加抽奖过滤        lottery=0 去掉抽奖过滤（单类型开关）
  regex=a,b 覆盖关键词        regex= 清空关键词
  +regex=关键词 追加          -regex=关键词 移除
  live_atall=on|off 开关直播@全体"""
