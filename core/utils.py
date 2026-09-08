import base64
import html
import io
import os
import re
import time
from urllib.parse import urlparse

import aiohttp
import qrcode
import qrcode.constants
from astrbot.api import logger
from astrbot.api.all import *
from PIL import Image as PILImage


def image_to_base64(image_source, mime_type: str = "image/png") -> str:
    """
    将图片对象或文件路径转为Base64 Data URI
    :param image_source: PIL Image对象 或 图片文件路径
    :param mime_type: 图片MIME类型，默认image/png
    :return: Base64 Data URI字符串
    """
    buffer = io.BytesIO()

    # 处理PIL Image对象
    if hasattr(image_source, "save"):
        image_source.save(buffer, format=mime_type.split("/")[-1])
    # 处理文件路径
    elif isinstance(image_source, str):
        with open(image_source, "rb") as f:
            buffer.write(f.read())
    else:
        raise ValueError("Unsupported image source type")

    base64_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:{mime_type};base64,{base64_str}"


async def url_to_base64(url: str, timeout: int = 10) -> str:
    """
    下载远程图片并转为 Base64 Data URI，供文转图模板内嵌使用。
    失败时返回空字符串，调用方应回退到原始 URL 或空值。
    """
    if not url or not is_valid_url(url):
        return ""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": "https://bgm.tv/",
    }
    try:
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.read()
                if not data:
                    return ""
                content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                mime_type = content_type if content_type.startswith("image/") else "image/jpeg"
                b64 = base64.b64encode(data).decode("utf-8")
                return f"data:{mime_type};base64,{b64}"
    except Exception as e:
        logger.error(f"下载图片失败 {url}: {e}")
        return ""


def create_qrcode(url):
    if not is_valid_url(url):
        return ""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=1,
    )
    qr.add_data(url)
    qr.make(fit=True)
    qr_image = qr.make_image(fill_color="#fb7299", back_color="white")
    url = image_to_base64(qr_image)
    return url


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return all([parsed.scheme, parsed.netloc])
    except ValueError:
        return False


def format_bili_timestamp(ts) -> str:
    """
    将 unix 时间戳格式化为 "YYYY-MM-DD HH:MM"（本地时区）。
    无效输入（None / 非数字 / <=0）返回空字符串。
    """
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return ""
    if ts_int <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_int))
    except (OverflowError, OSError, ValueError):
        return ""


def is_valid_umo(umo: str) -> bool:
    pattern = r"([^:]+):\s*([^:]+):\s*(.+)"
    return re.match(pattern, umo) is not None


def render_text_to_plain(text: str) -> str:
    """将渲染用的 HTML 片段转换为纯文本。"""
    if not text:
        return ""

    plain = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    plain = re.sub(r"<a [^>]*>(.*?)</a>", r"\1", plain, flags=re.IGNORECASE)
    plain = re.sub(r"<img [^>]*>", "", plain, flags=re.IGNORECASE)
    plain = re.sub(r"</?[^>]+>", "", plain)
    plain = html.unescape(plain)
    lines = [line.strip() for line in plain.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_rich_text(summary, topic):
    text = "<br>".join(filter(None, summary["text"].split("\n")))
    # 真正的话题
    if topic:
        topic_link = f"<a href='{topic['jump_url']}'># {topic['name']}</a>"
        text = f"# {topic_link}<br>" + text
    # 获取富文本节点
    rich_text_nodes = summary["rich_text_nodes"]
    for node in rich_text_nodes:
        # 表情包
        if node["type"] == "RICH_TEXT_NODE_TYPE_EMOJI":
            emoji_info = node["emoji"]
            placeholder = emoji_info["text"]  # 例如 "[脱单doge]"
            img_tag = f"<img src='{emoji_info['icon_url']}'>"
            # 替换文本中的占位符
            text = text.replace(placeholder, img_tag)
        # 话题形如"#一个话题#"，实际是跳转搜索
        elif node["type"] == "RICH_TEXT_NODE_TYPE_TOPIC":
            topic_info = node["text"]
            topic_url = node["jump_url"]
            topic_tag = f"<a href='https:{topic_url}'>{topic_info}</a>"
            # 替换文本中的占位符
            text = text.replace(topic_info, topic_tag)

    return text


def is_height_valid(
    img_path: str, platform_id: str = "", max_height: int = 25000
) -> bool:
    """
    检查图片是否可以作为 Image 发送（而非 File）。
    不同平台对图片尺寸有不同限制。
    """
    try:
        file_size = os.path.getsize(img_path)
        if file_size > 10 * 1024 * 1024:
            return False

        with PILImage.open(img_path) as img:
            width, height = img.size

            if platform_id == "telegram":
                if (width + height) > 10000:
                    return False
                longer = max(width, height)
                shorter = min(width, height)
                if shorter > 0 and (longer / shorter) > 20:
                    return False
                return True

            return height <= max_height
    except Exception as e:
        logger.error(f"无法打开图片 {img_path} 进行尺寸检查: {e}")
        return False
