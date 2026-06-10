# AI 视频混剪系统

基于 AI 的可产品化视频混剪系统：素材管理 → 智能分析 → AI 脚本 → 自动匹配 → 混剪渲染 → 成片输出。
当前阶段面向自用（单机零依赖部署），架构上为 SaaS 化预留了多租户、鉴权与分布式任务扩展能力。

> ⚠️ 合规说明：链接下载功能仅用于获取**已获得合法授权**的素材。每个素材支持填写 `license_note` 授权说明并留痕存档。

## 功能总览

| 能力 | 说明 |
| --- | --- |
| 素材上传与管理 | 本地文件上传 / 链接下载（yt-dlp，支持抖音等平台），含授权留痕、标签、删除、重试 |
| 素材分析 | 统一转码（H.264/AAC）、封面提取、场景切分（ffmpeg scene score）、抽帧、ASR 转写、OCR 画面文字 |
| AI 内容脚本 | LLM 根据主题/风格/时长生成口播脚本，可人工编辑 |
| 结构化执行脚本 | LLM 将内容脚本转为 JSON（镜头 / 文案 / 关键词 / 时长），Pydantic 严格校验 + 自动修复 |
| 素材自动匹配 | 分词关键词检索（ASR + OCR + 标题/标签）+ 时长契合度 + 多素材交叉混剪（相邻镜头优先不同素材、同片段分窗口复用不重复画面） |
| 自动混剪 | 按时间线裁剪 → 统一分辨率/帧率 → 转场拼接（xfade 叠化/黑场/滑动/划像 + 音频交叉淡化）→ BGM 混音（可选） |
| 字幕 | 镜头时长按文案字数校准语速，长文案自动拆条与画面对齐；支持烧录（burn）/ 软字幕（soft）/ 无（none），SRT 随成片保留 |
| 异步任务 | 渲染与分析全异步，状态/进度落库，支持失败重试 |
| 成片输出 | 在线预览、下载成片与 SRT 字幕 |

## 快速开始（自用模式）

依赖：Python 3.11+、ffmpeg（含 libass）

**安装 ffmpeg**（必须，转码/分析/合成都依赖它）：

- Windows：`winget install Gyan.FFmpeg`（装完**重开终端**生效）；或从 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 下载 full 版解压，把 `bin` 目录加入 PATH，或在 `.env` 中配置完整路径：
  ```
  FFMPEG_BIN=D:\ffmpeg\bin\ffmpeg.exe
  FFPROBE_BIN=D:\ffmpeg\bin\ffprobe.exe
  ```
- macOS：`brew install ffmpeg`
- Ubuntu/Debian：`sudo apt install ffmpeg`

验证：终端执行 `ffmpeg -version` 有输出即可；启动后也可访问 `/api/health` 查看依赖检测结果。

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置（可选，默认即可跑通；LLM 默认 mock 无需 Key）
cp .env.example .env

# 3. 启动（推荐方式，Windows 兼容性最好，Ctrl+C 可正常退出）
python run.py
# 或: uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> Windows 提示：请用 `python run.py` 启动。它会切换到 Selector 事件循环，
> 避免浏览器拖动/关闭视频预览时刷 `ConnectionResetError (WinError 10054)` 日志；
> 后台任务使用守护线程，Ctrl+C 可立即退出，重启后中断的任务会自动标记为失败、可一键重试。

打开 http://localhost:8000 即可使用 Web 界面；API 文档见 http://localhost:8000/docs 。

### Docker 部署

```bash
# 自用模式（单容器，任务进程内执行）
docker compose up api

# SaaS 模式（redis + celery worker，可水平扩展）
TASK_BACKEND=celery docker compose --profile saas up
```

## 典型工作流

1. **素材入库**：上传自拍视频，或粘贴已授权的抖音链接（填写授权说明留痕）
2. **自动分析**：系统后台转码、切场景、抽帧、ASR/OCR（可选），素材变为 `ready`
3. **AI 生成脚本**（可选）：输入主题（如"XX 抽纸种草"），AI 产出内容脚本 + 执行脚本 JSON，可人工微调
4. **手动选择素材渲染**：在素材库勾选多个素材 → 「渲染所选素材」→ 选择一个脚本或不用脚本：
   - **选脚本**：在所选素材范围内按镜头关键词智能匹配片段，带字幕
   - **不选脚本（自动混剪）**：所选素材轮流取片段、叠化转场、保留原声，指定成片时长与单镜头时长即可
5. **预览/下载**：在"渲染任务"页在线预览、下载 mp4 与 SRT

> 对应 API：`POST /api/render`（`material_ids` 多个素材 + 可选 `script_id`）；
> `POST /api/scripts/{id}/render` 仍可用（默认全库素材匹配）。

## 执行脚本 JSON 契约

```json
{
  "version": 1,
  "title": "成片标题",
  "width": 1080, "height": 1920, "fps": 30,
  "keep_source_audio": false,
  "bgm_path": null,
  "subtitle": { "mode": "burn", "font_size": 16 },
  "shots": [
    {
      "index": 1,
      "narration": "该镜头的字幕/口播文案",
      "keywords": ["纸巾", "抽纸特写"],
      "duration": 3.5,
      "material_id": null,
      "transition": "dissolve",
      "transition_duration": 0.4
    }
  ]
}
```

- `keywords`：用于匹配素材画面，描述需要的物体/动作/场景
- `material_id`：可强制指定素材，留空则自动匹配
- `duration`：系统会按文案字数自动校准（中文语速约 4 字/秒），保证字幕节奏与画面统一
- `transition`：进入该镜头的转场，`cut` 硬切 / `dissolve` 叠化 / `fade` 黑场 / `slide` 滑动 / `wipe` 划像；含转场时使用 xfade + 音频交叉淡化合成，切换平滑
- `subtitle.mode`：`burn` 烧录 / `soft` 软字幕 / `none` 不加；长文案自动拆分为多条短字幕并按比例对齐画面

## 配置说明（节选，完整见 `.env.example`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_PROVIDER` | `mock` | `mock`（离线调试）/ `openai`（任意 OpenAI 兼容接口：OpenAI、DeepSeek、通义、Ollama 等） |
| `ASR_PROVIDER` | `none` | `none` / `faster_whisper`（本地）/ `openai`（API） |
| `OCR_PROVIDER` | `none` | `none` / `rapidocr`（本地） |
| `TASK_BACKEND` | `inline` | `inline`（进程内线程池）/ `celery`（分布式） |
| `API_KEYS` | 空 | 空=不鉴权；`key:tenant,...` 启用多租户鉴权 |
| `DATABASE_URL` | SQLite | SaaS 化换 PostgreSQL 仅需改此项 |

本地 ASR/OCR 为可选依赖：

```bash
pip install faster-whisper            # 本地语音转写
pip install rapidocr-onnxruntime      # 本地 OCR
```

## 架构

```
web/            纯静态管理界面（FastAPI 托管）
app/
├── main.py     FastAPI 入口
├── config.py   环境变量配置（12-factor）
├── models.py   SQLAlchemy 模型（全表带 tenant_id）
├── auth.py     租户解析（API Key → tenant，可平滑换 JWT）
├── api/        materials / scripts / jobs 三组 REST API
├── services/   可插拔能力层
│   ├── downloader.py   yt-dlp 链接下载
│   ├── media.py        ffmpeg 转码/抽帧/场景切分/封面
│   ├── asr.py          ASR（none / faster_whisper / openai）
│   ├── ocr.py          OCR（none / rapidocr）
│   ├── llm.py          LLM（mock / openai 兼容）
│   ├── script_gen.py   内容脚本 ↔ 执行脚本 JSON
│   ├── matcher.py      素材片段匹配引擎
│   ├── subtitles.py    SRT 生成
│   └── composer.py     ffmpeg 混剪合成
├── pipeline/   analyze（素材分析）/ render（渲染）流水线
└── tasks/      任务调度抽象（inline 线程池 / celery 队列）
```

### 数据流

```
上传/链接 ──> Material(pending) ──> [分析流水线] ──> Segment + TranscriptLine (ready)
主题输入 ──> LLM 内容脚本 ──> LLM 执行脚本 JSON（校验）──> Script(ready)
提交渲染 ──> RenderJob(queued) ──> 匹配 timeline ──> ffmpeg 合成 ──> 成片 + SRT (success)
```

## SaaS 化扩展路线

代码已为以下扩展预留接口，升级时无需重构：

1. **多租户**：所有业务表已带 `tenant_id`，API 层统一经 `get_tenant` 解析；配置 `API_KEYS` 即启用隔离，后续可替换为 JWT/OAuth + 用户表
2. **水平扩展**：`TASK_BACKEND=celery` 切换到 Redis 队列，worker 独立扩容；API 与 worker 共享对象存储即可多机部署
3. **数据库**：`DATABASE_URL` 切换 PostgreSQL；建议引入 Alembic 管理迁移
4. **存储**：当前为本地磁盘，路径全部经 `config.py` 收敛，可替换为 S3/OSS 并用预签名 URL 下发
5. **计费与配额**：在 `get_tenant` 后挂配额中间件（素材数量/渲染时长/并发数），渲染任务天然是计量点
6. **检索升级**：`matcher.py` 接口稳定，可替换为 CLIP 图文向量 + 语义检索提升匹配质量
7. **能力升级**：ASR/OCR/LLM 均为 provider 模式，可按租户套餐路由到不同档位的模型

## 测试

```bash
python tests/smoke_test.py
```

冒烟测试会用 ffmpeg 合成测试素材，完整跑通 上传 → 分析 → AI 脚本 → 匹配 → 渲染 → 下载 全流程（LLM 使用内置 mock，无需外部服务）。
