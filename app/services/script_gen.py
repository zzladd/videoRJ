"""AI 脚本生成：

1. generate_content_script: 主题 -> 内容脚本（口播文案）
2. generate_execution_script: 内容脚本 -> 结构化执行脚本 JSON（经 Pydantic 校验）

镜头时长统一按中文语速（约 CHARS_PER_SECOND 字/秒）根据文案字数校准，
保证字幕节奏与画面切换一致。
"""
import logging
import re

from pydantic import ValidationError

from app.schemas import ExecutionScript
from app.services import llm

logger = logging.getLogger(__name__)

CHARS_PER_SECOND = 4.0  # 中文口播语速
MIN_SHOT, MAX_SHOT = 2.0, 10.0

CONTENT_SYSTEM = """你是资深短视频编导，擅长写抓人的口播脚本。
要求：
- 输出纯文本脚本，按镜头分行，每行一个镜头的口播文案
- 每行以【环节名】开头，例如【开场】【展示】【结尾】
- 文案口语化、节奏快、有钩子，适合竖屏短视频
- 不要输出任何与脚本无关的解释"""

EXECUTION_SYSTEM = """你是视频剪辑执行脚本生成器。把内容脚本转换为结构化 JSON，仅输出 JSON，不要输出其他文字。
JSON Schema:
{
  "version": 1,
  "title": "成片标题",
  "width": 1080, "height": 1920, "fps": 30,
  "subtitle": {"mode": "burn", "font_size": 16},
  "shots": [
    {
      "index": 1,
      "narration": "该镜头的字幕/口播文案",
      "keywords": ["用于匹配素材画面的关键词", "2-4个"],
      "duration": 3.5,
      "transition": "dissolve",
      "transition_duration": 0.4
    }
  ]
}
要求：
- duration ≈ 文案字数 ÷ 4（中文语速约每秒4字），范围 2-10 秒
- transition 表示进入该镜头的转场，取值: cut(硬切) / dissolve(叠化) / fade(黑场) / slide(滑动) / wipe(划像)
  混剪场景推荐以 dissolve 为主让切换流畅，开场可用 fade，节奏强烈处用 cut
- keywords 描述该镜头需要什么画面（物体/动作/场景/产品特写等），不要照抄文案
- narration 保留原文案"""


def generate_content_script(topic: str, style: str, target_duration: float, extra: str = "") -> str:
    user = (
        f"主题: {topic}\n"
        f"风格: {style}\n"
        f"目标时长: {target_duration}秒\n"
        f"补充要求: {extra or '无'}\n"
        f"请输出脚本。"
    )
    return llm.chat(CONTENT_SYSTEM, user).strip()


def generate_execution_script(content: str, topic: str, target_duration: float) -> ExecutionScript:
    user = (
        f"主题: {topic}\n"
        f"目标时长: {target_duration}秒\n"
        f"内容脚本:\n{content}\n"
        f"请输出执行脚本 JSON。"
    )
    raw = llm.chat(EXECUTION_SYSTEM, user, json_mode=True)
    data = llm.extract_json(raw)
    try:
        script = ExecutionScript.model_validate(data)
    except ValidationError as e:
        # 一次自动修复机会：把校验错误回喂给 LLM
        logger.warning("执行脚本校验失败，尝试自动修复: %s", e)
        fix_user = f"以下 JSON 不符合 Schema，错误:\n{e}\n请修复后重新输出完整 JSON:\n{raw}"
        fixed = llm.chat(EXECUTION_SYSTEM, fix_user, json_mode=True)
        script = ExecutionScript.model_validate(llm.extract_json(fixed))
    return fit_durations(script)


def fit_durations(script: ExecutionScript) -> ExecutionScript:
    """按文案字数校准镜头时长，保证字幕节奏与画面统一（LLM 给的时长仅作参考）。"""
    for shot in script.shots:
        chars = len(re.sub(r"[\s，。！？、,.!?]", "", shot.narration))
        if chars:
            shot.duration = round(min(MAX_SHOT, max(MIN_SHOT, chars / CHARS_PER_SECOND + 0.5)), 1)
    return script
