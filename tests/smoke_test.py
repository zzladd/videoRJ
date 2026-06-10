"""端到端冒烟测试：用 ffmpeg 合成测试素材，跑通 上传 -> 分析 -> 脚本 -> 渲染 全流程。

运行: python tests/smoke_test.py
依赖: ffmpeg、requirements.txt（LLM/ASR/OCR 使用 mock/none，无需外部服务）
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 测试使用独立数据目录，避免污染开发数据
TMP = Path(tempfile.mkdtemp(prefix="vremix_test_"))
os.environ.update({
    "DATA_DIR": str(TMP / "data"),
    "DATABASE_URL": f"sqlite:///{TMP / 'test.db'}",
    "LLM_PROVIDER": "mock",
    "ASR_PROVIDER": "none",
    "OCR_PROVIDER": "none",
    "TASK_BACKEND": "inline",
    "TRANSCODE_MAX_EDGE": "640",
})

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def make_test_video(path: Path, color: str, seconds: int = 6) -> None:
    """生成多场景测试视频：颜色渐变 + 音频。"""
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-vf", f"hue=h={hash(color) % 360}",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        str(path),
    ], check=True, capture_output=True)


def wait_for(fn, timeout: int = 120, interval: float = 1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = fn()
        if result is not None:
            return result
        time.sleep(interval)
    raise TimeoutError("等待超时")


def main() -> None:
    failures: list[str] = []

    with TestClient(app) as client:
        # 1. 健康检查
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/").status_code == 200
        print("[1/6] 健康检查 OK")

        # 2. 上传两个素材
        material_ids = []
        for i, color in enumerate(["red", "blue"]):
            video = TMP / f"src_{i}.mp4"
            make_test_video(video, color)
            with open(video, "rb") as f:
                resp = client.post(
                    "/api/materials/upload",
                    files={"file": (f"测试素材{i}.mp4", f, "video/mp4")},
                    data={"title": f"测试素材{i}", "license_note": "自制测试视频", "tags": f"测试,素材{i}"},
                )
            assert resp.status_code == 200, resp.text
            material_ids.append(resp.json()["id"])
        print("[2/6] 素材上传 OK")

        # 3. 等待分析完成
        def all_ready():
            mats = client.get("/api/materials").json()
            states = {m["id"]: m["status"] for m in mats}
            if any(states[mid] == "failed" for mid in material_ids):
                errs = [m["error"] for m in mats if m["status"] == "failed"]
                raise RuntimeError(f"素材分析失败: {errs}")
            if all(states[mid] == "ready" for mid in material_ids):
                return mats
            return None

        mats = wait_for(all_ready)
        for m in mats:
            assert m["duration"] > 0, "时长未探测"
            assert m["cover_url"], "封面未生成"
            assert m["segment_count"] > 0, "未切分出片段"
        segs = client.get(f"/api/materials/{material_ids[0]}/segments").json()
        assert len(segs) > 0
        print(f"[3/6] 素材分析 OK（片段数: {[m['segment_count'] for m in mats]}）")

        # 4. AI 生成脚本
        resp = client.post("/api/scripts/generate", json={
            "topic": "测试素材展示",
            "style": "口播",
            "target_duration": 12,
        })
        assert resp.status_code == 200, resp.text
        script = resp.json()
        assert script["content"], "内容脚本为空"
        assert script["execution"] and script["execution"]["shots"], "执行脚本为空"
        print(f"[4/6] AI 脚本生成 OK（镜头数: {len(script['execution']['shots'])}）")

        # 5. 编辑执行脚本（降低分辨率加速测试）+ 提交渲染
        execution = script["execution"]
        execution["width"], execution["height"] = 360, 640
        resp = client.put(f"/api/scripts/{script['id']}", json={"execution": execution})
        assert resp.status_code == 200, resp.text

        resp = client.post(f"/api/scripts/{script['id']}/render", json={"subtitle_mode": "burn"})
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["id"]

        def job_done():
            j = client.get(f"/api/jobs/{job_id}").json()
            if j["status"] == "failed":
                raise RuntimeError(f"渲染失败: {j['message']}")
            return j if j["status"] == "success" else None

        job = wait_for(job_done, timeout=300)
        assert job["timeline"], "时间线为空"
        assert job["output_url"], "无成片地址"
        print(f"[5/6] 渲染 OK（时间线片段数: {len(job['timeline'])}）")

        # 6. 成片预览/下载与字幕
        resp = client.get(job["output_url"])
        assert resp.status_code == 200 and len(resp.content) > 10000, "成片下载异常"
        resp = client.get(f"/api/jobs/{job_id}/subtitles")
        assert resp.status_code == 200 and "-->" in resp.text, "字幕文件异常"

        # 校验成片元信息
        out_file = TMP / "out.mp4"
        out_file.write_bytes(client.get(f"{job['output_url']}?download=true").content)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out_file)],
            capture_output=True, text=True, check=True,
        )
        duration = float(probe.stdout.strip())
        assert duration > 5, f"成片时长异常: {duration}"
        print(f"[6/6] 成片下载/预览/字幕 OK（成片时长: {duration:.1f}s）")

    if failures:
        print("失败项:", failures)
        sys.exit(1)
    print(f"\n✅ 全流程冒烟测试通过！测试数据目录: {TMP}")


if __name__ == "__main__":
    main()
