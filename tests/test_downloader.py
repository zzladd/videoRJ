"""链接规范化单元测试（无网络依赖）。

运行: python tests/test_downloader.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import downloader  # noqa: E402
from app.services.downloader import DownloadError, normalize_url  # noqa: E402


def main() -> None:
    # 网页版 modal_id 形式（用户报错的 URL）
    assert normalize_url(
        "https://www.douyin.com/jingxuan?modal_id=7630700301129751545"
    ) == "https://www.douyin.com/video/7630700301129751545"
    assert normalize_url(
        "https://www.douyin.com/discover?modal_id=123&foo=bar"
    ) == "https://www.douyin.com/video/123"

    # 标准 /video/ 链接去掉多余 query
    assert normalize_url(
        "https://www.douyin.com/video/456?previous_page=app_code_link"
    ) == "https://www.douyin.com/video/456"
    assert normalize_url("https://www.douyin.com/note/789") == "https://www.douyin.com/video/789"

    # App 分享文本中提取链接
    assert normalize_url(
        "8.88 复制打开抖音，看看作品 https://www.douyin.com/video/111/ 真不错！"
    ) == "https://www.douyin.com/video/111"

    # 短链：mock 展开
    downloader._expand_short_url = lambda url: "https://www.douyin.com/jingxuan?modal_id=222"
    assert normalize_url("https://v.douyin.com/abcDEF/") == "https://www.douyin.com/video/222"

    # 非抖音链接原样透传
    assert normalize_url("https://www.bilibili.com/video/BV1xx411c7mD") == (
        "https://www.bilibili.com/video/BV1xx411c7mD"
    )

    # 无链接报错
    try:
        normalize_url("这里没有链接")
        raise AssertionError("应当抛出 DownloadError")
    except DownloadError:
        pass

    print("✅ 链接规范化测试全部通过")


if __name__ == "__main__":
    main()
