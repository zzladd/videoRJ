"""租户解析。

自用模式（API_KEYS 为空）：所有请求归 "default" 租户，无需鉴权。
SaaS 模式：请求需携带 X-API-Key，按 key 映射到租户实现数据隔离。
后续可平滑替换为 JWT / OAuth，所有 API 仅依赖 get_tenant 这一个入口。
"""
from fastapi import Header, HTTPException

from app.config import get_settings


def get_tenant(x_api_key: str | None = Header(default=None)) -> str:
    key_map = get_settings().parse_api_keys()
    if not key_map:
        return "default"
    if not x_api_key or x_api_key not in key_map:
        raise HTTPException(status_code=401, detail="无效的 API Key")
    return key_map[x_api_key]
