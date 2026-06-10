/* AI 视频混剪系统 - 前端逻辑 */

const API = "";
let API_KEY = localStorage.getItem("api_key") || "";

// ---------- 基础 ----------
async function req(path, opts = {}) {
  const headers = opts.headers || {};
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  if (opts.json) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const resp = await fetch(API + path, { ...opts, headers });
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try { msg = (await resp.json()).detail || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return resp.json();
}

function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 3500);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtDur(sec) {
  if (!sec) return "-";
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m ? `${m}分${s}秒` : `${s}秒`;
}

const STATUS_TEXT = {
  pending: "等待中", downloading: "下载中", analyzing: "分析中", ready: "就绪", failed: "失败",
  queued: "排队中", matching: "匹配素材", rendering: "渲染中", success: "完成", canceled: "已取消",
  draft: "草稿",
};

// ---------- Tab 切换 ----------
document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
  });
});

// ---------- 素材 ----------
async function loadMaterials() {
  const list = document.getElementById("material-list");
  try {
    const mats = await req("/api/materials");
    if (!mats.length) {
      list.innerHTML = '<div class="empty">暂无素材，请上传视频或粘贴链接</div>';
      return;
    }
    list.innerHTML = mats.map(m => `
      <div class="mat-card">
        ${m.cover_url
          ? `<img class="mat-cover" src="${m.cover_url}" loading="lazy">`
          : `<div class="mat-cover-placeholder">🎞️</div>`}
        <div class="mat-body">
          <div class="mat-title" title="${esc(m.title)}">${esc(m.title) || "(未命名)"}</div>
          <div class="mat-meta">
            <span class="badge ${m.status}">${STATUS_TEXT[m.status] || m.status}</span>
            <span>${fmtDur(m.duration)} · ${m.segment_count}段</span>
          </div>
          ${m.error ? `<div class="hint" style="color:var(--red)">${esc(m.error.slice(0, 120))}</div>` : ""}
          <div class="mat-actions">
            ${m.preview_url ? `<a class="btn small" href="${m.preview_url}" target="_blank">预览</a>` : ""}
            ${m.status === "failed" ? `<button class="btn small" onclick="reanalyze('${m.id}')">重试</button>` : ""}
            <button class="btn small danger" onclick="delMaterial('${m.id}')">删除</button>
          </div>
        </div>
      </div>`).join("");
  } catch (e) { list.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`; }
}

document.getElementById("upload-form").addEventListener("submit", async e => {
  e.preventDefault();
  const form = e.target;
  const fd = new FormData();
  fd.append("file", form.file.files[0]);
  fd.append("title", form.title.value);
  fd.append("license_note", form.license_note.value);
  fd.append("tags", form.tags.value);
  const btn = form.querySelector("button");
  btn.disabled = true; btn.textContent = "上传中...";
  try {
    await req("/api/materials/upload", { method: "POST", body: fd });
    toast("上传成功，正在后台分析");
    form.reset();
    loadMaterials();
  } catch (err) { toast("上传失败: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "上传并分析"; }
});

document.getElementById("url-form").addEventListener("submit", async e => {
  e.preventDefault();
  const form = e.target;
  try {
    await req("/api/materials/from-url", {
      method: "POST",
      json: {
        url: form.url.value,
        title: form.title.value,
        license_note: form.license_note.value,
        tags: form.tags.value.split(",").map(t => t.trim()).filter(Boolean),
      },
    });
    toast("已加入下载队列");
    form.reset();
    loadMaterials();
  } catch (err) { toast("提交失败: " + err.message, true); }
});

window.reanalyze = async id => {
  try { await req(`/api/materials/${id}/reanalyze`, { method: "POST" }); toast("已重新提交分析"); loadMaterials(); }
  catch (e) { toast(e.message, true); }
};
window.delMaterial = async id => {
  if (!confirm("确定删除该素材？")) return;
  try { await req(`/api/materials/${id}`, { method: "DELETE" }); toast("已删除"); loadMaterials(); }
  catch (e) { toast(e.message, true); }
};
document.getElementById("refresh-materials").addEventListener("click", loadMaterials);

// ---------- 脚本 ----------
async function loadScripts() {
  const list = document.getElementById("script-list");
  try {
    const scripts = await req("/api/scripts");
    if (!scripts.length) {
      list.innerHTML = '<div class="empty">暂无脚本，输入主题让 AI 生成</div>';
      return;
    }
    list.innerHTML = scripts.map(s => `
      <div class="row-item">
        <div class="row-head">
          <span class="row-title">${esc(s.title) || "(未命名)"}</span>
          <span class="badge ${s.status}">${STATUS_TEXT[s.status] || s.status}</span>
          <div class="row-actions">
            <button class="btn small" onclick="editScript('${s.id}')">编辑</button>
            <button class="btn small primary" onclick="renderScript('${s.id}')" ${s.execution ? "" : "disabled"}>提交渲染</button>
            <button class="btn small danger" onclick="delScript('${s.id}')">删除</button>
          </div>
        </div>
        <pre class="script-content">${esc(s.content)}</pre>
      </div>`).join("");
  } catch (e) { list.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`; }
}

document.getElementById("script-form").addEventListener("submit", async e => {
  e.preventDefault();
  const form = e.target;
  const btn = form.querySelector("button");
  btn.disabled = true; btn.textContent = "生成中...";
  try {
    await req("/api/scripts/generate", {
      method: "POST",
      json: {
        topic: form.topic.value,
        style: form.style.value,
        target_duration: Number(form.target_duration.value) || 30,
      },
    });
    toast("脚本生成成功");
    loadScripts();
  } catch (err) { toast("生成失败: " + err.message, true); }
  finally { btn.disabled = false; btn.textContent = "生成脚本"; }
});

window.editScript = async id => {
  try {
    const s = await req(`/api/scripts/${id}`);
    openModal(`编辑脚本：${esc(s.title)}`, `
      <div class="modal-body">
        <label>标题</label>
        <input id="edit-title" value="${esc(s.title)}">
        <label>内容脚本（口播文案）</label>
        <textarea id="edit-content" rows="8">${esc(s.content)}</textarea>
        <label>执行脚本 JSON（结构化，渲染依据）</label>
        <textarea id="edit-execution" rows="14">${esc(JSON.stringify(s.execution, null, 2))}</textarea>
        <div style="display:flex; gap:10px; margin-top:16px">
          <button class="btn primary" onclick="saveScript('${id}')">保存</button>
          <button class="btn" onclick="regenExecution('${id}')">由内容脚本重新生成执行脚本</button>
        </div>
      </div>`);
  } catch (e) { toast(e.message, true); }
};

window.saveScript = async id => {
  try {
    let execution = null;
    const execText = document.getElementById("edit-execution").value.trim();
    if (execText) {
      try { execution = JSON.parse(execText); }
      catch { toast("执行脚本 JSON 格式错误", true); return; }
    }
    await req(`/api/scripts/${id}`, {
      method: "PUT",
      json: {
        title: document.getElementById("edit-title").value,
        content: document.getElementById("edit-content").value,
        execution,
      },
    });
    toast("已保存");
    closeModal();
    loadScripts();
  } catch (e) { toast("保存失败: " + e.message, true); }
};

window.regenExecution = async id => {
  try {
    // 先保存当前内容脚本，再触发转换
    await req(`/api/scripts/${id}`, {
      method: "PUT",
      json: { content: document.getElementById("edit-content").value },
    });
    const s = await req(`/api/scripts/${id}/to-execution`, { method: "POST" });
    document.getElementById("edit-execution").value = JSON.stringify(s.execution, null, 2);
    toast("执行脚本已重新生成");
  } catch (e) { toast(e.message, true); }
};

window.renderScript = async id => {
  try {
    await req(`/api/scripts/${id}/render`, { method: "POST", json: {} });
    toast("渲染任务已提交");
    document.querySelector('[data-tab="jobs"]').click();
    loadJobs();
  } catch (e) { toast("提交失败: " + e.message, true); }
};

window.delScript = async id => {
  if (!confirm("确定删除该脚本？")) return;
  try { await req(`/api/scripts/${id}`, { method: "DELETE" }); toast("已删除"); loadScripts(); }
  catch (e) { toast(e.message, true); }
};
document.getElementById("refresh-scripts").addEventListener("click", loadScripts);

// ---------- 渲染任务 ----------
async function loadJobs() {
  const list = document.getElementById("job-list");
  try {
    const jobs = await req("/api/jobs");
    if (!jobs.length) {
      list.innerHTML = '<div class="empty">暂无渲染任务</div>';
      return;
    }
    list.innerHTML = jobs.map(j => `
      <div class="row-item">
        <div class="row-head">
          <span class="row-title">任务 ${j.id.slice(0, 8)}</span>
          <span class="badge ${j.status}">${STATUS_TEXT[j.status] || j.status}</span>
          <span class="row-meta">${esc(j.message || "")}</span>
          <div class="row-actions">
            ${j.output_url ? `
              <a class="btn small primary" href="${j.output_url}?download=true">下载成片</a>
              <a class="btn small" href="/api/jobs/${j.id}/subtitles" target="_blank">字幕SRT</a>` : ""}
            ${j.status === "failed" ? `<button class="btn small" onclick="retryJob('${j.id}')">重试</button>` : ""}
            <button class="btn small danger" onclick="delJob('${j.id}')">删除</button>
          </div>
        </div>
        ${["queued", "matching", "rendering"].includes(j.status) ? `
          <div class="progress-track"><div class="progress-bar" style="width:${Math.round(j.progress * 100)}%"></div></div>` : ""}
        ${j.output_url ? `<video class="preview" src="${j.output_url}" controls preload="metadata"></video>` : ""}
      </div>`).join("");
  } catch (e) { list.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`; }
}

window.retryJob = async id => {
  try { await req(`/api/jobs/${id}/retry`, { method: "POST" }); toast("已重新提交"); loadJobs(); }
  catch (e) { toast(e.message, true); }
};
window.delJob = async id => {
  if (!confirm("确定删除该任务及成片？")) return;
  try { await req(`/api/jobs/${id}`, { method: "DELETE" }); toast("已删除"); loadJobs(); }
  catch (e) { toast(e.message, true); }
};
document.getElementById("refresh-jobs").addEventListener("click", loadJobs);

// ---------- 弹窗 ----------
function openModal(title, bodyHtml) {
  document.getElementById("modal-title").innerHTML = title;
  document.getElementById("modal-body").innerHTML = bodyHtml;
  document.getElementById("modal").classList.remove("hidden");
}
window.closeModal = () => document.getElementById("modal").classList.add("hidden");
document.getElementById("modal").addEventListener("click", e => {
  if (e.target.id === "modal") closeModal();
});

// ---------- 自动轮询（有进行中任务时） ----------
setInterval(() => {
  const active = document.querySelector(".panel.active")?.id;
  if (active === "tab-jobs") loadJobs();
  if (active === "tab-materials") {
    const hasBusy = document.querySelector(".badge.analyzing, .badge.downloading, .badge.pending");
    if (hasBusy) loadMaterials();
  }
}, 4000);

// 初始加载
loadMaterials();
loadScripts();
loadJobs();
