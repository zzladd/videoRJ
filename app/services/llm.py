"""LLM 客户端（可插拔）。

- mock:   内置规则生成，无需 API Key，用于调试与离线演示
- openai: 任意 OpenAI 兼容接口（OpenAI / DeepSeek / 通义 / Moonshot / Ollama 等）
"""
import json
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def chat(system: str, user: str, json_mode: bool = False) -> str:
    settings = get_settings()
    if settings.llm_provider == "mock":
        return _mock_chat(system, user, json_mode)
    if settings.llm_provider == "openai":
        return _openai_chat(system, user, json_mode)
    raise ValueError(f"未知 LLM provider: {settings.llm_provider}")


def _openai_chat(system: str, user: str, json_mode: bool) -> str:
    settings = get_settings()
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload: dict = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json=payload,
            timeout=300,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except httpx.HTTPError as e:
        raise LLMError(f"LLM 调用失败: {e}") from e


def extract_json(text: str) -> dict:
    """从 LLM 输出中宽容地提取 JSON 对象（兼容 ```json 代码块、前后缀文本）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError(f"LLM 输出中未找到 JSON: {text[:200]}")
    return json.loads(text[start : end + 1])


# ---------------------------------------------------------------------------
# Mock：根据 prompt 中的结构化标记生成可用结果，保证无 Key 也能跑通全流程
# ---------------------------------------------------------------------------


def _mock_chat(system: str, user: str, json_mode: bool) -> str:
    if json_mode or "JSON" in system.upper():
        return _mock_execution_json(user)
    return _mock_content_script(user)


def _mock_content_script(user: str) -> str:
    topic = _extract_field(user, "主题") or "产品介绍"
    return (
        f"【开场】今天给大家分享{topic}，看完这条视频你一定有收获。\n"
        f"【展示】先看整体效果，{topic}的核心亮点一目了然。\n"
        f"【细节】重点来了，这里的细节处理非常到位，实用性拉满。\n"
        f"【体验】实际上手体验，流畅自然，完全不踩坑。\n"
        f"【结尾】如果你也感兴趣，点赞收藏，下期继续分享。"
    )


def _mock_execution_json(user: str) -> str:
    # 从用户 prompt 中提取内容脚本行，按行生成镜头
    lines: list[str] = []
    capture = False
    for raw in user.splitlines():
        line = raw.strip()
        if line.startswith("内容脚本"):
            capture = True
            continue
        if capture:
            if line.startswith("请输出"):  # 指令行，不是文案
                break
            if line:
                lines.append(line.lstrip("【").replace("】", " "))
    if not lines:
        lines = ["开场 引入主题", "展示 核心亮点", "细节 重点说明", "结尾 引导关注"]

    shots = []
    for i, line in enumerate(lines):
        words = [w for w in line.replace("，", " ").replace("。", " ").split() if len(w) >= 2][:4]
        chars = len(line.replace(" ", ""))
        shots.append({
            "index": i + 1,
            "narration": line,
            "keywords": words or [line[:4]],
            "duration": round(max(2.0, min(10.0, chars / 4.0 + 0.5)), 1),
            # 开场淡入，之后以叠化为主保证切换流畅
            "transition": "fade" if i == 0 else "dissolve",
            "transition_duration": 0.4,
        })
    return json.dumps({
        "version": 1,
        "title": _extract_field(user, "主题") or "AI 混剪成片",
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "subtitle": {"mode": "burn", "font_size": 16},
        "shots": shots,
    }, ensure_ascii=False)


def _extract_field(text: str, label: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(label):
            return line.split(":", 1)[-1].split("：", 1)[-1].strip()
    return ""
