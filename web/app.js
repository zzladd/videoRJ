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
    if (btn.dataset.tab === "editor" && !binLoaded) loadEditorBin();
  });
});

// ---------- 素材（支持多选后渲染） ----------
const selectedMats = new Set();

function updateSelectBar() {
  const bar = document.getElementById("select-bar");
  document.getElementById("select-count").textContent = `已选 ${selectedMats.size} 个素材`;
  bar.classList.toggle("hidden", selectedMats.size === 0);
}

window.toggleMatSelect = (id, checked) => {
  if (checked) selectedMats.add(id); else selectedMats.delete(id);
  document.querySelector(`.mat-card[data-id="${id}"]`)?.classList.toggle("selected", checked);
  updateSelectBar();
};

window.clearSelection = () => {
  selectedMats.clear();
  document.querySelectorAll(".mat-card.selected").forEach(el => el.classList.remove("selected"));
  document.querySelectorAll(".mat-check").forEach(el => { el.checked = false; });
  updateSelectBar();
};

async function loadMaterials() {
  const list = document.getElementById("material-list");
  try {
    const mats = await req("/api/materials");
    // 清理已不存在的选中项
    const existing = new Set(mats.map(m => m.id));
    [...selectedMats].forEach(id => { if (!existing.has(id)) selectedMats.delete(id); });
    updateSelectBar();
    if (!mats.length) {
      list.innerHTML = '<div class="empty">暂无素材，请上传视频或粘贴链接</div>';
      return;
    }
    list.innerHTML = mats.map(m => `
      <div class="mat-card ${selectedMats.has(m.id) ? "selected" : ""}" data-id="${m.id}">
        ${m.status === "ready"
          ? `<input type="checkbox" class="mat-check" title="选择该素材参与混剪"
               ${selectedMats.has(m.id) ? "checked" : ""}
               onchange="toggleMatSelect('${m.id}', this.checked)">`
          : ""}
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

window.renderScript = id => openRenderModal(id);

// ---------- 渲染弹窗：手动选择多个素材 + 一个脚本（或不用脚本自动混剪） ----------
const pickState = new Set();

window.openRenderModal = async (scriptId = "") => {
  let mats, scripts;
  try {
    [mats, scripts] = await Promise.all([req("/api/materials?status=ready"), req("/api/scripts")]);
  } catch (e) { toast(e.message, true); return; }
  if (!mats.length) { toast("没有已分析完成的素材，请先上传并等待分析", true); return; }
  const readyScripts = scripts.filter(s => s.execution);

  pickState.clear();
  selectedMats.forEach(id => pickState.add(id));

  openModal("提交渲染", `
    <div class="modal-body">
      <label>1️⃣ 选择素材（可多选，点击卡片切换）</label>
      <div class="pick-grid">
        ${mats.map(m => `
          <div class="pick-item ${pickState.has(m.id) ? "selected" : ""}" data-id="${m.id}" onclick="togglePick('${m.id}')">
            ${m.cover_url ? `<img src="${m.cover_url}" loading="lazy">` : ""}
            <div class="pick-title" title="${esc(m.title)}">${esc(m.title) || "(未命名)"}</div>
          </div>`).join("")}
      </div>
      <label>2️⃣ 选择脚本（不选则自动混剪：素材轮流取片段 + 叠化转场 + 保留原声）</label>
      <select id="rm-script" onchange="onRenderScriptChange()">
        <option value="">不使用脚本（自动混剪）</option>
        ${readyScripts.map(s => `<option value="${s.id}" ${s.id === scriptId ? "selected" : ""}>${esc(s.title) || s.id.slice(0, 8)}</option>`).join("")}
      </select>
      <div class="opt-row" id="rm-duration-row">
        <label>成片时长(秒)</label>
        <input id="rm-duration" type="number" value="30" min="5" max="600" style="width:100px">
        <label>单镜头时长(秒)</label>
        <input id="rm-clip" type="number" value="3.5" min="1.5" max="10" step="0.5" style="width:100px">
      </div>
      <div class="opt-row">
        <label><input id="rm-audio" type="checkbox" style="width:auto"> 保留素材原声</label>
      </div>
      <div style="margin-top:16px">
        <button class="btn primary" onclick="submitRender()">提交渲染</button>
      </div>
    </div>`);
  onRenderScriptChange();
};

window.togglePick = id => {
  if (pickState.has(id)) pickState.delete(id); else pickState.add(id);
  document.querySelector(`.pick-item[data-id="${id}"]`)?.classList.toggle("selected", pickState.has(id));
};

window.onRenderScriptChange = () => {
  const hasScript = !!document.getElementById("rm-script").value;
  document.getElementById("rm-duration-row").style.display = hasScript ? "none" : "flex";
  // 无脚本默认保留原声（否则成片无声）；有脚本默认静音配字幕
  document.getElementById("rm-audio").checked = !hasScript;
};

window.submitRender = async () => {
  if (!pickState.size) { toast("请至少选择一个素材", true); return; }
  const scriptId = document.getElementById("rm-script").value;
  const body = {
    material_ids: [...pickState],
    script_id: scriptId || null,
    keep_source_audio: document.getElementById("rm-audio").checked,
  };
  if (!scriptId) {
    body.target_duration = Number(document.getElementById("rm-duration").value) || 30;
    body.clip_duration = Number(document.getElementById("rm-clip").value) || 3.5;
  }
  try {
    await req("/api/render", { method: "POST", json: body });
    toast("渲染任务已提交");
    closeModal();
    clearSelection();
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
          <span class="row-title">任务 ${j.id.slice(0, 8)}${{ auto: "（自动混剪）", manual: "（手动剪辑）" }[j.kind] || ""}</span>
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

// ---------- 剪辑台：拖拽片段拼接 ----------
let tlClips = [];        // 时间线状态
let binLoaded = false;
let dragData = null;     // { type: 'bin'|'tl', payload|index }

async function loadEditorBin() {
  const bin = document.getElementById("editor-bin");
  try {
    const mats = (await req("/api/materials?status=ready"));
    if (!mats.length) {
      bin.innerHTML = '<div class="empty">暂无已分析完成的素材</div>';
      return;
    }
    const blocks = await Promise.all(mats.map(async m => {
      const segs = await req(`/api/materials/${m.id}/segments`);
      if (!segs.length) return "";
      const chips = segs.map(s => {
        const payload = esc(JSON.stringify({
          material_id: m.id, mat_title: m.title, segment_id: s.id,
          seg_start: s.start, seg_end: s.end,
          thumb: `/api/materials/${m.id}/segments/${s.id}/thumb`,
        }));
        return `
          <div class="seg-chip" draggable="true" data-payload="${payload}" title="拖拽到时间线，或点击追加">
            <img src="/api/materials/${m.id}/segments/${s.id}/thumb" loading="lazy">
            <div class="seg-label">${s.start.toFixed(1)}s - ${s.end.toFixed(1)}s（${(s.end - s.start).toFixed(1)}s）</div>
          </div>`;
      }).join("");
      return `
        <div class="bin-material">
          <div class="bin-material-title">🎞️ ${esc(m.title) || "(未命名)"} <span class="row-meta">${segs.length} 个片段</span></div>
          <div class="bin-segments">${chips}</div>
        </div>`;
    }));
    bin.innerHTML = blocks.join("") || '<div class="empty">素材尚无片段</div>';
    bin.querySelectorAll(".seg-chip").forEach(chip => {
      chip.addEventListener("dragstart", e => {
        dragData = { type: "bin", payload: JSON.parse(chip.dataset.payload) };
        chip.classList.add("dragging");
      });
      chip.addEventListener("dragend", () => chip.classList.remove("dragging"));
      chip.addEventListener("click", () => {
        addClipToTimeline(JSON.parse(chip.dataset.payload), tlClips.length);
      });
    });
    binLoaded = true;
  } catch (e) { bin.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`; }
}

function addClipToTimeline(p, index) {
  tlClips.splice(index, 0, {
    material_id: p.material_id,
    segment_id: p.segment_id,
    mat_title: p.mat_title,
    seg_start: p.seg_start, seg_end: p.seg_end,   // 可裁剪范围
    start: p.seg_start, end: p.seg_end,           // 当前裁剪
    transition: tlClips.length ? "dissolve" : "cut",
    transition_duration: 0.4,
    narration: "",
    thumb: p.thumb,
  });
  renderTimeline();
}

function tlTotalDuration() {
  let total = 0;
  tlClips.forEach((c, i) => {
    const d = c.end - c.start;
    total += d;
    if (i > 0 && c.transition !== "cut") total -= Math.min(c.transition_duration, d / 2);
  });
  return Math.max(0, total);
}

function renderTimeline() {
  const tl = document.getElementById("timeline");
  document.getElementById("tl-total").textContent =
    tlClips.length ? `共 ${tlClips.length} 段 · 预计 ${tlTotalDuration().toFixed(1)} 秒` : "";
  if (!tlClips.length) {
    tl.innerHTML = '<div class="empty" id="tl-empty">从上方拖拽片段到这里开始剪辑</div>';
    return;
  }
  tl.innerHTML = tlClips.map((c, i) => `
    <div class="tl-clip" draggable="true" data-index="${i}">
      <img src="${c.thumb}">
      <div class="tl-body">
        <div class="tl-row">
          <span class="tl-order">#${i + 1}</span>
          <span class="tl-name" title="${esc(c.mat_title)}">${esc(c.mat_title) || "素材"}</span>
          <button class="tl-del" onclick="removeClip(${i})" title="移除">✕</button>
        </div>
        <div class="tl-row">
          <input type="number" step="0.1" min="${c.seg_start}" max="${c.seg_end}" value="${c.start}"
            onchange="updateClip(${i}, 'start', this.value)" title="起点(秒)">
          <span>→</span>
          <input type="number" step="0.1" min="${c.seg_start}" max="${c.seg_end}" value="${c.end}"
            onchange="updateClip(${i}, 'end', this.value)" title="终点(秒)">
          <span>${(c.end - c.start).toFixed(1)}s</span>
        </div>
        <div class="tl-row">
          <select onchange="updateClip(${i}, 'transition', this.value)" title="进入该段的转场" ${i === 0 ? "disabled" : ""}>
            ${["cut|硬切", "dissolve|叠化", "fade|黑场", "slide|滑动", "wipe|划像"].map(o => {
              const [v, t] = o.split("|");
              return `<option value="${v}" ${c.transition === v ? "selected" : ""}>${t}</option>`;
            }).join("")}
          </select>
        </div>
        <input type="text" placeholder="字幕文案（可选）" value="${esc(c.narration)}"
          onchange="updateClip(${i}, 'narration', this.value)">
      </div>
    </div>`).join("");

  tl.querySelectorAll(".tl-clip").forEach(el => {
    el.addEventListener("dragstart", e => {
      // 输入框内不触发整卡拖拽
      if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") { e.preventDefault(); return; }
      dragData = { type: "tl", index: Number(el.dataset.index) };
      el.classList.add("dragging");
    });
    el.addEventListener("dragend", () => el.classList.remove("dragging"));
  });
}

window.updateClip = (i, key, value) => {
  const c = tlClips[i];
  if (!c) return;
  if (key === "start" || key === "end") {
    let v = Math.min(c.seg_end, Math.max(c.seg_start, Number(value) || 0));
    c[key] = v;
    if (c.end - c.start < 0.3) {  // 保证至少 0.3s
      if (key === "start") c.start = Math.max(c.seg_start, c.end - 0.3);
      else c.end = Math.min(c.seg_end, c.start + 0.3);
    }
  } else if (key === "narration") {
    c.narration = value;
    return;  // 不重绘，避免输入框失焦
  } else {
    c[key] = value;
  }
  renderTimeline();
};

window.removeClip = i => { tlClips.splice(i, 1); renderTimeline(); };
window.clearTimeline = () => { tlClips = []; renderTimeline(); };

function dropIndexFromX(tl, clientX) {
  const cards = [...tl.querySelectorAll(".tl-clip")];
  for (let i = 0; i < cards.length; i++) {
    const r = cards[i].getBoundingClientRect();
    if (clientX < r.left + r.width / 2) return i;
  }
  return cards.length;
}

(function initTimelineDnD() {
  const tl = document.getElementById("timeline");
  tl.addEventListener("dragover", e => { e.preventDefault(); tl.classList.add("dragover"); });
  tl.addEventListener("dragleave", () => tl.classList.remove("dragover"));
  tl.addEventListener("drop", e => {
    e.preventDefault();
    tl.classList.remove("dragover");
    if (!dragData) return;
    const idx = dropIndexFromX(tl, e.clientX);
    if (dragData.type === "bin") {
      addClipToTimeline(dragData.payload, idx);
    } else if (dragData.type === "tl") {
      const from = dragData.index;
      let to = idx;
      if (to > from) to -= 1;
      if (to !== from) {
        const [moved] = tlClips.splice(from, 1);
        tlClips.splice(to, 0, moved);
        renderTimeline();
      }
    }
    dragData = null;
  });
})();

window.submitTimeline = async () => {
  if (!tlClips.length) { toast("时间线为空，请先拖入片段", true); return; }
  const [w, h] = document.getElementById("tl-res").value.split("x").map(Number);
  try {
    await req("/api/render/timeline", {
      method: "POST",
      json: {
        clips: tlClips.map(c => ({
          material_id: c.material_id,
          segment_id: c.segment_id,
          start: c.start, end: c.end,
          transition: c.transition,
          transition_duration: c.transition_duration,
          narration: c.narration,
        })),
        width: w, height: h,
        keep_source_audio: document.getElementById("tl-audio").checked,
        subtitle_mode: document.getElementById("tl-subtitle").value,
      },
    });
    toast("剪辑任务已提交");
    document.querySelector('[data-tab="jobs"]').click();
    loadJobs();
  } catch (e) { toast("提交失败: " + e.message, true); }
};

document.getElementById("refresh-editor").addEventListener("click", loadEditorBin);

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

// ---------- 启动依赖检测 ----------
(async () => {
  try {
    const h = await req("/api/health");
    if (h.status !== "ok") {
      const missing = Object.entries(h.tools || {}).filter(([, ok]) => !ok).map(([k]) => k).join(" / ");
      toast(`依赖缺失：${missing} 不可用，请安装 ffmpeg 并配置 PATH 或 .env 中的 FFMPEG_BIN，否则分析与渲染会失败`, true);
    }
  } catch {}
})();

// 初始加载
loadMaterials();
loadScripts();
loadJobs();
