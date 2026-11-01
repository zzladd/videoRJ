"""本机量化站点。只绑定 127.0.0.1，基础功能：总览、运行、策略、发起通路测试。"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import unquote
from urllib.parse import urlparse

from paper_spot.config_loader import ConfigError
from paper_spot.config_loader import load_paper
from paper_spot.config_loader import repo_root
from paper_spot.strategies.loader import load_decide
from paper_spot.strategies.loader import normalize_strategy
from paper_spot.view import build_model
from paper_spot.view import render_run_body

_HOST = "127.0.0.1"
_WEB_MAX_SECONDS = 300
_RUN_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class Site:
    def __init__(
        self,
        root: Path | None = None,
        runs_dir: Path | None = None,
        focus_run: str | None = None,
    ) -> None:
        self.root = (root or repo_root()).resolve()
        self.runs_dir = (runs_dir or (self.root / "runs")).resolve()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.focus_run = focus_run
        self.jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def list_runs(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.runs_dir.is_dir():
            return rows
        for path in self.runs_dir.iterdir():
            if not path.is_dir():
                continue
            if not (path / "ledger.json").is_file():
                continue
            rows.append(self._run_summary(path))
        rows.sort(key=lambda item: item["mtime"], reverse=True)
        return rows

    def resolve_run(self, name: str) -> Path:
        if not _RUN_NAME_RE.match(name):
            raise ConfigError("运行目录名不合法")
        path = (self.runs_dir / name).resolve()
        try:
            path.relative_to(self.runs_dir)
        except ValueError as exc:
            raise ConfigError("运行目录超出 runs/") from exc
        if path == self.runs_dir or not path.is_dir():
            raise ConfigError(f"没有这个运行目录：{name}")
        if not (path / "ledger.json").is_file():
            raise ConfigError(f"{name} 还没有 ledger.json")
        return path

    def user_strategy_path(self) -> Path:
        return self.root / "src" / "paper_spot" / "strategies" / "user_strategy.py"

    def start_job(self, kind: str, command: list[str], run_name: str | None = None) -> str:
        job_id = uuid.uuid4().hex[:12]
        record = {
            "id": job_id,
            "kind": kind,
            "status": "running",
            "command": command,
            "run_name": run_name,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "exit_code": None,
            "log": "",
            "error": "",
        }
        with self._lock:
            self.jobs[job_id] = record
        thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
        thread.start()
        return job_id

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self.jobs.get(job_id)
            return dict(found) if found else None

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            record = self.jobs[job_id]
            command = list(record["command"])
        env = os.environ.copy()
        src = str((self.root / "src").resolve())
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
        env["PYTHONUNBUFFERED"] = "1"
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.root),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            log = (completed.stdout or "") + (completed.stderr or "")
            with self._lock:
                record = self.jobs[job_id]
                record["log"] = log[-20000:]
                record["exit_code"] = completed.returncode
                record["finished_at"] = datetime.now(timezone.utc).isoformat()
                record["status"] = "ok" if completed.returncode == 0 else "failed"
                if completed.returncode != 0 and not record["error"]:
                    record["error"] = f"退出码 {completed.returncode}"
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                record = self.jobs[job_id]
                record["status"] = "failed"
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["finished_at"] = datetime.now(timezone.utc).isoformat()

    def _run_summary(self, path: Path) -> dict[str, Any]:
        ledger = _read_json(path / "ledger.json")
        snapshot = _read_json(path / "config_snapshot.json")
        acceptance = _read_json(path / "report" / "acceptance.json")
        fills = list(ledger.get("fills") or [])
        balances = ledger.get("balances") or {}
        matching = snapshot.get("matching") or {}
        cash = balances.get("USDT")
        if cash is None:
            start = matching.get("starting_balance") or "10000 USDT"
            cash = str(start).split()[0]
        return {
            "name": path.name,
            "instrument": matching.get("instrument_id") or "",
            "strategy": snapshot.get("strategy") or "connectivity",
            "fills": len(fills),
            "cash": str(cash),
            "has_report": (path / "report" / "header.json").is_file(),
            "replay_met": bool((acceptance.get("replay_diff_zero") or {}).get("met")),
            "mtime": path.stat().st_mtime,
        }


def bind_site(site: Site, port: int = 8765) -> ThreadingHTTPServer:
    if port < 0 or port > 65535:
        raise ConfigError("端口要在 0 到 65535 之间")
    try:
        server = ThreadingHTTPServer((_HOST, port), _make_handler(site))
    except OSError as exc:
        raise ConfigError(f"127.0.0.1:{port} 无法监听：{exc}") from exc
    bound_host = server.server_address[0]
    if bound_host not in {_HOST, "localhost"}:
        server.server_close()
        raise ConfigError(f"站点拒绝绑定 {bound_host}")
    return server


def serve_site(
    port: int = 8765,
    root: Path | None = None,
    runs_dir: Path | None = None,
    focus_run: str | None = None,
) -> None:
    site = Site(root=root, runs_dir=runs_dir, focus_run=focus_run)
    server = bind_site(site, port)
    bound_port = server.server_address[1]
    print(f"本机站点 http://{_HOST}:{bound_port}/")
    if focus_run:
        print(f"运行详情 http://{_HOST}:{bound_port}/runs/{focus_run}")
    print(f"目录 {site.root}")
    print("只绑定 127.0.0.1。Ctrl+C 结束。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("本机站点已关闭。")
    finally:
        server.server_close()


def _make_handler(site: Site):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                self._dispatch_get()
            except ConfigError as exc:
                self._html(400, _page(site, "出错", f"<p class='error'>{html.escape(str(exc))}</p>", "home"))
            except Exception as exc:  # noqa: BLE001
                self._html(500, _page(site, "出错", f"<p class='error'>{html.escape(type(exc).__name__ + ': ' + str(exc))}</p>", "home"))

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._dispatch_post()
            except ConfigError as exc:
                self._html(400, _page(site, "出错", f"<p class='error'>{html.escape(str(exc))}</p>", "home"))
            except Exception as exc:  # noqa: BLE001
                self._html(500, _page(site, "出错", f"<p class='error'>{html.escape(type(exc).__name__ + ': ' + str(exc))}</p>", "home"))

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _dispatch_get(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path == "/":
                self._html(200, _home(site))
                return
            if path == "/runs":
                self._html(200, _runs_page(site))
                return
            if path.startswith("/runs/"):
                name = path[len("/runs/") :].strip("/")
                if "/" in name or not name:
                    self.send_error(404)
                    return
                run_dir = site.resolve_run(name)
                self._html(200, _run_page(site, run_dir))
                return
            if path == "/strategy":
                self._html(200, _strategy_page(site))
                return
            if path == "/start":
                self._html(200, _start_page(site))
                return
            if path == "/check":
                self._html(200, _check_page(site))
                return
            if path.startswith("/jobs/"):
                job_id = path[len("/jobs/") :].strip("/")
                job = site.get_job(job_id)
                if job is None:
                    raise ConfigError("没有这个任务")
                self._html(200, _job_page(site, job))
                return
            if path == "/health":
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def _dispatch_post(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            form = self._read_form()
            if path == "/start":
                self._start_run(form)
                return
            if path == "/check":
                self._start_check()
                return
            if path == "/strategy":
                self._save_strategy(form)
                return
            if path.startswith("/runs/") and path.endswith("/report"):
                name = path[len("/runs/") : -len("/report")].strip("/")
                run_dir = site.resolve_run(name)
                job_id = site.start_job(
                    "report",
                    [sys.executable, "-m", "paper_spot", "report", str(run_dir)],
                    run_name=name,
                )
                self._redirect(f"/jobs/{job_id}")
                return
            if path.startswith("/runs/") and path.endswith("/replay"):
                name = path[len("/runs/") : -len("/replay")].strip("/")
                run_dir = site.resolve_run(name)
                job_id = site.start_job(
                    "replay",
                    [sys.executable, "-m", "paper_spot", "replay", str(run_dir)],
                    run_name=name,
                )
                self._redirect(f"/jobs/{job_id}")
                return
            self.send_error(404)

        def _start_run(self, form: dict[str, str]) -> None:
            seconds = _parse_seconds(form.get("seconds", "120"))
            strategy = normalize_strategy(form.get("strategy") or "connectivity")
            imbalance = (form.get("imbalance") or "").strip() or None
            if imbalance is not None:
                Decimal(imbalance)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            run_dir = site.runs_dir / stamp
            command = [
                sys.executable,
                "-m",
                "paper_spot",
                "run",
                "--seconds",
                str(seconds),
                "--run-dir",
                str(run_dir),
                "--strategy",
                strategy,
            ]
            if imbalance is not None:
                command.extend(["--imbalance", imbalance])
            job_id = site.start_job("run", command, run_name=stamp)
            self._redirect(f"/jobs/{job_id}")

        def _start_check(self) -> None:
            job_id = site.start_job("check", [sys.executable, "-m", "paper_spot", "check"])
            self._redirect(f"/jobs/{job_id}")

        def _save_strategy(self, form: dict[str, str]) -> None:
            source = form.get("source")
            if source is None:
                raise ConfigError("缺少策略源码")
            path = site.user_strategy_path()
            if not path.is_file():
                raise ConfigError(f"找不到 {path}")
            backup = path.read_text(encoding="utf-8")
            path.write_text(source, encoding="utf-8")
            try:
                load_decide("user", Decimal("1.2"))
            except Exception as exc:
                path.write_text(backup, encoding="utf-8")
                raise ConfigError(f"策略没有通过检查，已恢复原文：{exc}") from exc
            self._html(
                200,
                _page(
                    site,
                    "策略已保存",
                    "<p class='ok'>已写入 <code>src/paper_spot/strategies/user_strategy.py</code>。"
                    "发起通路测试时选「用户策略」才会用到它。</p>"
                    + _strategy_form(source, saved=True),
                    "strategy",
                ),
            )

        def _read_form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b""
            parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            return {key: values[0] if values else "" for key, values in parsed.items()}

        def _html(self, status: int, document: str) -> None:
            body = document.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def _parse_seconds(text: str) -> int:
    try:
        seconds = int(text)
    except ValueError as exc:
        raise ConfigError("秒数要是整数") from exc
    if seconds < 1:
        raise ConfigError("秒数至少是 1")
    if seconds > _WEB_MAX_SECONDS:
        raise ConfigError(f"本机站点一次最多 {_WEB_MAX_SECONDS} 秒。更长的会话请用命令行。")
    return seconds


def _home(site: Site) -> str:
    runs = site.list_runs()
    _, paper = load_paper(site.root)
    latest = runs[0] if runs else None
    latest_html = "<p class='muted'>还没有运行目录。先发起一次通路测试。</p>"
    if latest:
        latest_html = f"""
<div class="cards">
  <div class="card">
    <div class="label">最近一次</div>
    <div class="value"><a href="/runs/{html.escape(latest['name'])}">{html.escape(latest['name'])}</a></div>
    <p>{html.escape(latest['instrument'] or '交易对未写入')} · {html.escape(latest['strategy'])} · {latest['fills']} 笔成交</p>
  </div>
  <div class="card">
    <div class="label">结束现金</div>
    <div class="value mono">{html.escape(latest['cash'])} USDT</div>
  </div>
  <div class="card">
    <div class="label">运行次数</div>
    <div class="value mono">{len(runs)}</div>
  </div>
</div>
"""
    body = f"""
<section class="hero">
  <p class="eyebrow">本机沙箱 · 不配置交易密钥</p>
  <h1>通路测试</h1>
  <p class="lead">加密现货影子盘的本机控制台。行情只读，订单只进沙箱。下面是基础功能：看运行、改策略、发起通路测试、重写报告和回放。</p>
  <div class="actions">
    <a class="btn primary" href="/start">发起通路测试</a>
    <a class="btn" href="/runs">运行列表</a>
    <a class="btn" href="/strategy">策略</a>
    <a class="btn" href="/check">自检</a>
  </div>
</section>
{latest_html}
<section>
  <h2>当前配置</h2>
  <table>
    <tr><th>起始现金</th><td>{html.escape(str(paper.get('starting_balance')))}</td></tr>
    <tr><th>默认名义金额</th><td>{html.escape(str(paper.get('order_quote_notional')))} USDT</td></tr>
    <tr><th>单笔上限</th><td>{html.escape(str(paper.get('max_notional_per_order')))} USDT</td></tr>
    <tr><th>默认会话</th><td>{html.escape(str(paper.get('session_seconds')))} 秒</td></tr>
    <tr><th>参与率警戒</th><td>{html.escape(str(paper.get('participation_warn')))}</td></tr>
  </table>
  <p class="muted">本机站点一次最多 {_WEB_MAX_SECONDS} 秒。更长的会话请用 <code>paper_spot run</code>。</p>
</section>
"""
    return _page(site, "总览", body, "home")


def _runs_page(site: Site) -> str:
    rows = site.list_runs()
    if not rows:
        body = "<p class='empty' id='empty-runs'>还没有运行目录。</p><p><a class='btn primary' href='/start'>发起通路测试</a></p>"
    else:
        lines = []
        for row in rows:
            lines.append(
                "<tr>"
                f"<td><a href='/runs/{html.escape(row['name'])}'>{html.escape(row['name'])}</a></td>"
                f"<td>{html.escape(row['instrument'] or '—')}</td>"
                f"<td>{html.escape(row['strategy'])}</td>"
                f"<td class='mono'>{row['fills']}</td>"
                f"<td class='mono'>{html.escape(row['cash'])}</td>"
                f"<td>{'已回放' if row['replay_met'] else ('有报告' if row['has_report'] else '—')}</td>"
                "</tr>"
            )
        body = (
            "<section><h2>运行列表</h2>"
            "<table><thead><tr><th>目录</th><th>交易对</th><th>策略</th><th>成交</th><th>现金</th><th>报告</th></tr></thead>"
            f"<tbody>{''.join(lines)}</tbody></table></section>"
        )
    return _page(site, "运行", body, "runs")


def _run_page(site: Site, run_dir: Path) -> str:
    model = build_model(run_dir)
    actions = f"""
<div class="actions">
  <form method="post" action="/runs/{html.escape(run_dir.name)}/report"><button class="btn" type="submit">重写报告</button></form>
  <form method="post" action="/runs/{html.escape(run_dir.name)}/replay"><button class="btn" type="submit">回放两次</button></form>
  <a class="btn" href="/runs">返回列表</a>
</div>
"""
    body = f"<section class='head'><h1>{html.escape(str(model['header_title']))} · {html.escape(run_dir.name)}</h1>{actions}</section>{render_run_body(model)}"
    return _page(site, run_dir.name, body, "runs")


def _strategy_page(site: Site, message: str = "") -> str:
    path = site.user_strategy_path()
    source = path.read_text(encoding="utf-8") if path.is_file() else ""
    body = f"""
<section>
  <h1>策略</h1>
  <p>默认连通性策略在 <code>src/paper_spot/strategies/connectivity.py</code>。自己的买卖改下面这个文件，函数签名是 <code>decide(market) -&gt; OrderIntent | None</code>。</p>
  <p class="muted">站点不会注册交易所执行客户端，也不会保存交易密钥。保存后只写入本机文件。</p>
  {message}
  {_strategy_form(source)}
</section>
"""
    return _page(site, "策略", body, "strategy")


def _strategy_form(source: str, saved: bool = False) -> str:
    note = "<p class='ok'>已保存。</p>" if saved else ""
    return f"""
{note}
<form method="post" action="/strategy" class="stack">
  <label for="source">src/paper_spot/strategies/user_strategy.py</label>
  <textarea id="source" name="source" rows="22" spellcheck="false">{html.escape(source)}</textarea>
  <button class="btn primary" type="submit">保存用户策略</button>
</form>
"""


def _start_page(site: Site) -> str:
    _, paper = load_paper(site.root)
    default_seconds = min(int(paper.get("session_seconds") or 120), _WEB_MAX_SECONDS)
    imbalance = html.escape(str(paper.get("imbalance_multiple") or "1.2"))
    body = f"""
<section>
  <h1>发起通路测试</h1>
  <p>订单只进本机沙箱。页面上的会话最长 {_WEB_MAX_SECONDS} 秒。</p>
  <form method="post" action="/start" class="stack">
    <label for="seconds">秒数</label>
    <input id="seconds" name="seconds" type="number" min="1" max="{_WEB_MAX_SECONDS}" value="{default_seconds}">
    <label for="strategy">策略</label>
    <select id="strategy" name="strategy">
      <option value="connectivity" selected>连通性（默认）</option>
      <option value="user">用户策略 user_strategy.py</option>
    </select>
    <label for="imbalance">不平衡倍数（可空，空则用配置）</label>
    <input id="imbalance" name="imbalance" type="text" value="{imbalance}">
    <button class="btn primary" type="submit">开始</button>
  </form>
</section>
"""
    return _page(site, "发起", body, "start")


def _check_page(site: Site) -> str:
    body = """
<section>
  <h1>自检</h1>
  <p>探测官方行情并建节点，不发单。通过时执行客户端只能是 PaperSandboxExecutionClient。</p>
  <form method="post" action="/check"><button class="btn primary" type="submit">运行自检</button></form>
</section>
"""
    return _page(site, "自检", body, "check")


def _job_page(site: Site, job: dict[str, Any]) -> str:
    status = job["status"]
    refresh = ""
    if status == "running":
        refresh = '<meta http-equiv="refresh" content="2">'
    link = ""
    if job.get("run_name") and status == "ok" and job["kind"] in {"run", "report", "replay"}:
        link = f"<p><a class='btn primary' href='/runs/{html.escape(job['run_name'])}'>打开运行详情</a></p>"
    elif job.get("run_name") and status == "running":
        link = f"<p class='muted'>完成后会写到 runs/{html.escape(job['run_name'])}</p>"
    tone = {"running": "muted", "ok": "ok", "failed": "error"}.get(status, "muted")
    body = f"""
<section>
  <h1>任务 {html.escape(job['id'])}</h1>
  <p class="{tone}">状态：{html.escape(status)}　类型：{html.escape(job['kind'])}</p>
  {link}
  <pre class="log">{html.escape(job.get('log') or job.get('error') or '还没有输出。')}</pre>
</section>
"""
    return _page(site, "任务", body, "home", head_extra=refresh)


def _page(site: Site, title: str, body: str, active: str, head_extra: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · 通路测试</title>
{head_extra}
<style>{_SITE_CSS}</style>
</head>
<body>
<header class="top">
  <a class="brand" href="/">通路测试</a>
  <nav>
    {_nav("总览", "/", active == "home")}
    {_nav("运行", "/runs", active == "runs")}
    {_nav("策略", "/strategy", active == "strategy")}
    {_nav("发起", "/start", active == "start")}
    {_nav("自检", "/check", active == "check")}
  </nav>
  <span class="badge">127.0.0.1 · 沙箱</span>
</header>
<main>
{body}
</main>
<footer>
  <p>只绑定 {_HOST}。数来自本机 runs/ 与配置，不是投资建议。</p>
</footer>
</body>
</html>
"""


def _nav(label: str, href: str, on: bool) -> str:
    cls = "on" if on else ""
    return f'<a class="{cls}" href="{html.escape(href)}">{html.escape(label)}</a>'


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


_SITE_CSS = """
:root {
  --ink: #132033;
  --muted: #5a6a7c;
  --line: #d7e0ea;
  --panel: rgba(255,255,255,.92);
  --teal: #0f766e;
  --teal-dark: #0b5f59;
  --warn: #b45309;
  --danger: #9f1239;
  --ok: #0f766e;
  --bg0: #c9d8e8;
  --bg1: #eef2f6;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "IBM Plex Sans", "Source Han Sans SC", "Noto Sans SC", sans-serif;
  color: var(--ink);
  background:
    radial-gradient(1000px 420px at 12% -10%, #b9cfe3 0%, transparent 60%),
    linear-gradient(180deg, var(--bg0) 0%, var(--bg1) 240px, var(--bg1) 100%);
  min-height: 100vh;
}
a { color: var(--teal-dark); text-decoration: none; }
a:hover { text-decoration: underline; }
.top {
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 14px 22px;
  border-bottom: 1px solid var(--line);
  background: rgba(255,255,255,.72);
  backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 5;
}
.brand {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.03em;
  color: var(--ink);
  text-decoration: none;
}
nav { display: flex; gap: 6px; flex-wrap: wrap; }
nav a {
  padding: 8px 12px;
  border-radius: 8px;
  color: var(--muted);
  text-decoration: none;
  font-size: 14px;
}
nav a.on, nav a:hover { background: #e5eef6; color: var(--ink); text-decoration: none; }
.badge {
  margin-left: auto;
  font-size: 12px;
  color: var(--muted);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 4px 10px;
  background: white;
}
main { max-width: 1040px; margin: 0 auto; padding: 28px 20px 72px; }
footer { max-width: 1040px; margin: 0 auto; padding: 0 20px 40px; color: var(--muted); font-size: 13px; }
.hero {
  padding: 28px 0 8px;
}
.eyebrow { color: var(--teal); font-size: 13px; letter-spacing: 0.08em; text-transform: uppercase; margin: 0 0 8px; }
.hero h1 { font-size: clamp(40px, 7vw, 64px); line-height: 1.05; margin: 0 0 12px; letter-spacing: -0.04em; }
.lead { font-size: 18px; max-width: 42rem; color: #304255; }
.actions { display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0; }
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--line);
  background: white;
  color: var(--ink);
  border-radius: 10px;
  padding: 10px 14px;
  font: inherit;
  cursor: pointer;
  text-decoration: none;
}
.btn:hover { text-decoration: none; border-color: #b7c7d8; }
.btn.primary { background: var(--teal); border-color: var(--teal); color: white; }
.btn.primary:hover { background: var(--teal-dark); border-color: var(--teal-dark); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 18px 0; }
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 16px;
}
.label { color: var(--muted); font-size: 12px; margin-bottom: 6px; }
.value { font-size: 22px; font-weight: 650; letter-spacing: -0.02em; }
.mono { font-variant-numeric: tabular-nums; font-family: "IBM Plex Mono", ui-monospace, monospace; }
section, .head {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 18px 18px 20px;
  margin-top: 14px;
}
h1 { font-size: 28px; margin: 0 0 8px; letter-spacing: -0.02em; }
h2 { font-size: 18px; margin: 0 0 8px; }
h3 { font-size: 15px; margin: 18px 0 8px; color: #3b4a5c; }
p { line-height: 1.55; }
.meta, .muted { color: var(--muted); }
.empty { background: #fff4d6; border: 1px solid #e0c36a; padding: 12px 14px; border-radius: 8px; }
.ok { color: var(--ok); }
.error { color: var(--danger); }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 8px 8px; border-bottom: 1px solid #e6edf4; font-size: 14px; vertical-align: top; }
.note { display: grid; grid-template-columns: 140px 1fr; gap: 8px 16px; padding: 10px 0; border-top: 1px solid #e6edf4; }
.note b { font-size: 14px; }
.bar { height: 10px; background: #d9e3ee; border-radius: 99px; margin-top: 6px; }
.bar span { display: block; height: 10px; border-radius: 99px; background: var(--teal); }
.watch span { background: var(--warn); }
svg { width: 100%; height: auto; }
.axis { fill: var(--muted); font-size: 12px; }
.stack { display: grid; gap: 8px; max-width: 820px; }
label { font-size: 13px; color: var(--muted); }
input, select, textarea {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 10px 12px;
  font: inherit;
  background: white;
  color: var(--ink);
}
textarea {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 13px;
  line-height: 1.45;
}
.log {
  background: #0f1b2a;
  color: #d7e4f2;
  border-radius: 12px;
  padding: 14px;
  overflow: auto;
  max-height: 420px;
  font-size: 12px;
  line-height: 1.45;
}
form.inline { display: inline; }
@media (max-width: 720px) {
  .top { flex-wrap: wrap; }
  .badge { margin-left: 0; }
  .hero h1 { font-size: 40px; }
}
"""
