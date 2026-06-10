"""全局配置。所有可调参数均通过环境变量 / .env 注入，便于自用与 SaaS 部署共用同一份代码。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 基础
    data_dir: Path = Path("./data")
    database_url: str = "sqlite:///./data/app.db"

    # 多租户 / 鉴权（SaaS 预留）。格式: "key1:tenant1,key2:tenant2"
    api_keys: str = ""

    # 任务执行
    task_backend: str = "inline"  # inline | celery
    task_workers: int = 2
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # LLM
    llm_provider: str = "mock"  # mock | openai
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    # ASR
    asr_provider: str = "none"  # none | faster_whisper | openai
    asr_whisper_model: str = "small"
    asr_api_base_url: str = "https://api.openai.com/v1"
    asr_api_key: str = ""
    asr_api_model: str = "whisper-1"

    # OCR
    ocr_provider: str = "none"  # none | rapidocr

    # 链接下载：Netscape 格式 cookies.txt 路径（抖音部分内容需登录 Cookie）
    ytdlp_cookies_file: str = ""

    # 视频处理
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    transcode_max_edge: int = 1280
    scene_threshold: float = 0.35
    frame_interval: float = 2.0

    # ---- 派生路径 ----
    @property
    def materials_dir(self) -> Path:
        return self.data_dir / "materials"

    @property
    def frames_dir(self) -> Path:
        return self.data_dir / "frames"

    @property
    def covers_dir(self) -> Path:
        return self.data_dir / "covers"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure_dirs(self) -> None:
        for d in (self.materials_dir, self.frames_dir, self.covers_dir, self.outputs_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)

    def parse_api_keys(self) -> dict[str, str]:
        """返回 {api_key: tenant_id} 映射；为空表示不启用鉴权。"""
        result: dict[str, str] = {}
        for pair in self.api_keys.split(","):
            pair = pair.strip()
            if not pair:
                continue
            key, _, tenant = pair.partition(":")
            result[key.strip()] = (tenant.strip() or "default")
        return result


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
