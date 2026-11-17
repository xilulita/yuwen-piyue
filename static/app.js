/* 智批语文 v9：全程拖拽多图 · 六色批注+文字键入 · 预设控制条 · 默认只显错题 · 自动保存 */
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const S = {
  students: [], aiConfig: null, answerKeys: [],
  images: [], mode: "fast",
  view: null, page: 0, selected: -1,
  tool: "select", color: "#d0342c", showCorrect: false, zoom: "big",   // big | fit | full
  training: null, grading: false, trainingBusy: false,
  saveTimer: null, pending: {},
  marks: {},          // page(str) -> [mark]；view 存在时同步到服务端，否则暂存本地随批改提交
  drawing: null,
};

async function api(path, opts) {
  const res = await fetch(path, opts);
  const j = await res.json().catch(() => ({ error: "响应解析失败" }));
  if (!res.ok || j.error) throw new Error(j.error || ("请求失败 " + res.status));
  return j;
}
const post = (p, b) => api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
const del = p => api(p, { method: "DELETE" });

function toast(msg, ms) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.hidden = true, ms || 2400);
}
function modal(title, bodyHtml, actions) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = bodyHtml;
  const act = $("#modal-actions"); act.innerHTML = "";
  (actions || []).forEach(a => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "btn " + (a.cls || ""); b.textContent = a.label;
    b.onclick = async () => { try { await a.onClick(); } catch (e) { toast(e.message, 5000); } };
    act.appendChild(b);
  });
  $("#modal-root").hidden = false;
}
function closeModal() { $("#modal-root").hidden = true; }
$("#modal-mask").onclick = closeModal;
$("#modal-close").onclick = closeModal;
document.addEventListener("keydown", e => {
  if (e.key === "Escape") closeModal();
  if (e.target.matches("input,textarea,select")) return;
  const map = { v: "select", t: "tick", x: "cross", c: "circle", p: "pen", w: "text", e: "erase" };
  if (map[e.key.toLowerCase()]) setTool(map[e.key.toLowerCase()]);
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); undoMark(); }
});

/* ================= 初始化 ================= */
async function boot() {
  const j = await api("/api/bootstrap");
  S.students = j.students; S.aiConfig = j.ai_config;
  S.answerKeys = j.answer_keys || [];
  $("#custom-prompt").value = j.ai_config.custom_prompt || "";
  syncChips();
  renderAnswerSelect();
  renderAiStatus();
}
function renderAiStatus() {
  const on = !!(S.aiConfig && S.aiConfig.configured);
  const rMap = { auto: "", off: " · 推理已关闭", on: " · 推理保持开启" };
  const rTag = rMap[(S.aiConfig && S.aiConfig.reasoning) || "auto"] || "";
  $("#ai-status").classList.toggle("on", on);
  $("#ai-status").title = on ? "AI 接口已配置：" + (S.aiConfig.model || "") + rTag : "AI 接口未配置";
  $("#upload-hint").textContent = on
    ? "接口就绪（" + (S.aiConfig.model || "默认模型") + rTag + "），配置已记忆。"
    : "批改与 AI 出题需先在右上角「AI 接口」配置一次（之后永久记忆）。";
}
function mergeStudent(stu) {
  const arr = S.students.filter(s => s.id !== stu.id);
  arr.push(stu); return arr;
}
$("#custom-prompt").addEventListener("change", async () => {
  try {
    const j = await post("/api/ai/config", { custom_prompt: $("#custom-prompt").value });
    S.aiConfig = { ...S.aiConfig, ...j };
    toast("自定义批改要求已记忆");
  } catch (e) { toast(e.message); }
});

/* ================= 模式 ================= */
function setMode(m) {
  S.mode = m;
  $("#mode-fast").classList.toggle("active", m === "fast");
  $("#mode-think").classList.toggle("active", m === "think");
  $("#mode-fast").setAttribute("aria-checked", m === "fast");
  $("#mode-think").setAttribute("aria-checked", m === "think");
}
$("#mode-fast").onclick = () => setMode("fast");
$("#mode-think").onclick = () => setMode("think");

/* ================= AI 接口设置 ================= */
$("#btn-settings").onclick = () => {
  const c = S.aiConfig || {};
  modal("AI 接口设置（一次配置，永久记忆）", `
    <p class="muted small" style="margin-top:-4px">OpenAI 兼容视觉接口（qwen-vl / gpt-4o / glm-4v 等）。配置保存在本机，更新后保留。Key 只显脱敏片段。</p>
    <div class="field"><label>接口地址 Base URL</label>
      <input type="text" id="cfg-base" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" value="${esc(c.base_url || "")}"></div>
    <div class="field"><label>模型名称（需支持图片输入）</label>
      <input type="text" id="cfg-model" placeholder="qwen-vl-max / gpt-4o / glm-4v-plus" value="${esc(c.model || "")}"></div>
    <div class="field"><label>API Key ${c.api_key_masked ? `（已保存 ${esc(c.api_key_masked)}，留空沿用）` : ""}</label>
      <input type="password" id="cfg-key" placeholder="${c.api_key_masked ? "留空则沿用已保存的 Key" : "sk-..."}" autocomplete="off"></div>
    <div class="field"><label>模型推理（内置思考）——决定批改速度</label>
      <select id="cfg-reasoning">
        <option value="auto" ${(c.reasoning || "auto") === "auto" ? "selected" : ""}>自动（不干预，跟随模型默认）</option>
        <option value="off" ${c.reasoning === "off" ? "selected" : ""}>关闭推理（更快；丢坐标时软件自动补定位）</option>
        <option value="on" ${c.reasoning === "on" ? "selected" : ""}>保持推理（更细致；速度较慢）</option>
      </select>
      <div class="small muted">这里是<b>速度开关</b>（模型要不要深度推理）；左栏「批改精细度」是<b>报告详略开关</b>（错因写一句话还是详细讲解），两者独立。推理型模型（如 doubao-seed 系列）关闭后批改明显提速；接口不支持该参数时自动忽略。想要极速也可换非推理视觉模型（如 doubao-1.5-vision-pro / doubao-seed-1.6-flash）。</div></div>
    <div id="cfg-test-result" class="small muted"></div>`, [
    { label: "测试连接", onClick: async () => {
      $("#cfg-test-result").textContent = "正在测试连接…";
      try {
        const j = await post("/api/ai/test", {
          base_url: $("#cfg-base").value.trim(), model: $("#cfg-model").value.trim(),
          api_key: $("#cfg-key").value.trim() });
        $("#cfg-test-result").innerHTML = `<span style="color:var(--ok)">✓ 连接成功：${esc(j.reply)}</span>`;
      } catch (e) { $("#cfg-test-result").innerHTML = `<span style="color:var(--bad)">✗ ${esc(e.message)}</span>`; }
    } },
    { label: "保存", cls: "primary", onClick: async () => {
      const j = await post("/api/ai/config", {
        base_url: $("#cfg-base").value.trim(), model: $("#cfg-model").value.trim(),
        api_key: $("#cfg-key").value.trim(), reasoning: $("#cfg-reasoning").value });
      S.aiConfig = { ...S.aiConfig, ...j, base_url: $("#cfg-base").value.trim(), model: $("#cfg-model").value.trim() };
      renderAiStatus(); closeModal(); toast(j.configured ? "已保存并记忆" : "已保存，但配置不完整");
    } }
  ]);
};

/* ================= 多图上传（左栏卡片 + 白板 全程可拖拽追加） ================= */
const dz = $("#dropzone"), fi = $("#file-input"), dzMini = $("#dropzone-mini");
dz.addEventListener("click", () => fi.click());
dz.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fi.click(); } });
dzMini.addEventListener("click", () => fi.click());
dzMini.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fi.click(); } });
fi.addEventListener("change", () => { addFiles(Array.from(fi.files)); fi.value = ""; });
// 拖拽目标：整个左栏 + 整个白板卡片，任何时刻都能追加图片
const dragTargets = [$("#col-left"), $("#board-card")];
dragTargets.forEach(zone => {
  zone.addEventListener("dragenter", e => { e.preventDefault(); zone.classList.add("drag-over"); dz.classList.add("drag"); dzMini.classList.add("drag"); });
  zone.addEventListener("dragover", e => { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
  zone.addEventListener("dragleave", e => {
    if (!zone.contains(e.relatedTarget)) { zone.classList.remove("drag-over"); dz.classList.remove("drag"); dzMini.classList.remove("drag"); }
  });
  zone.addEventListener("drop", e => {
    e.preventDefault();
    zone.classList.remove("drag-over"); dz.classList.remove("drag"); dzMini.classList.remove("drag");
    const files = Array.from(e.dataTransfer.files || []);
    if (files.length) addFiles(files);
  });
});
// 防止拖到页面其他位置时浏览器直接打开图片
["dragover", "drop"].forEach(ev => document.addEventListener(ev, e => e.preventDefault()));
document.addEventListener("paste", e => {
  const items = Array.from(e.clipboardData?.items || []).filter(i => i.type.startsWith("image/"));
  if (items.length) { addFiles(items.map(i => i.getAsFile())); toast("已粘贴截图"); }
});
function addFiles(files) {
  // 已有批改结果时追加图片 => 视为开始新一份（保留学生姓名），避免新旧卷混淆
  if (S.view && !S.view.demo) {
    S.view = null; S.selected = -1; S.training = null; S.pending = {}; S.marks = {}; S.page = 0;
    $("#score-strip").hidden = true; $("#result-banner").hidden = true;
    $("#right-result").hidden = true; $("#right-empty").hidden = false;
    $("#editor-card").hidden = true; $("#btn-next-paper").hidden = true;
    toast("已开始新一份试卷，继续添加图片");
  }
  for (const file of files) {
    if (!file) continue;
    if (S.images.length >= 6) { toast("一份卷子最多 6 张图片"); break; }
    if (!/^image\/(png|jpeg|webp)$/.test(file.type)) { toast("仅支持 PNG / JPG / WEBP：" + file.name); continue; }
    if (file.size > 15 * 1024 * 1024) { toast("单张超过 15MB：" + file.name); continue; }
    const reader = new FileReader();
    reader.onload = () => {
      S.images.push({ dataUrl: reader.result, name: file.name || "粘贴的截图.png", size: file.size });
      renderThumbs();
      S.page = S.images.length - 1;
      renderBoard(S.view);   // 上传即见，切到最新一页
    };
    reader.readAsDataURL(file);
  }
}
function renderThumbs() {
  const list = $("#thumb-list");
  list.innerHTML = S.images.map((im, i) => `
    <div class="thumb ${!S.view && i === S.page ? "active" : ""}" data-thumb="${i}">
      <span class="torder">P${i + 1}</span>
      <img src="${im.dataUrl}" alt="第${i + 1}页">
      <div class="tinfo">
        <span class="tname" title="${esc(im.name)}">${esc(im.name)}</span>
        <span class="tsize">${(im.size / 1024 / 1024).toFixed(2)} MB</span>
      </div>
      <button type="button" data-rm="${i}" title="删除这张">🗑</button>
    </div>`).join("");
  $$("[data-rm]", list).forEach(b => b.onclick = e => {
    e.stopPropagation();
    const i = Number(b.dataset.rm);
    S.images.splice(i, 1);
    delete S.marks[String(i)];
    if (S.page >= S.images.length) S.page = Math.max(0, S.images.length - 1);
    renderThumbs();
    if (!S.view) renderBoard(null);
    toast("已删除该图片");
  });
  $$("[data-thumb]", list).forEach(t => t.onclick = () => {
    if (S.view) return;
    S.page = Number(t.dataset.thumb);
    renderThumbs(); renderBoard(null);
  });
  const has = S.images.length > 0;
  dzMini.hidden = !has;
  $("#img-count").textContent = has ? `${S.images.length} / 6 张` : "";
  dz.style.display = has ? "none" : "";
}

/* 自定义批改要求：预设控制条 */
$$("#custom-chips .cchip").forEach(chip => chip.onclick = () => {
  const ta = $("#custom-prompt");
  const text = chip.dataset.c;
  if (chip.classList.contains("active")) {
    // 再点一次取消该预设
    ta.value = ta.value.replace(text, "").replace(/^[；;\s]+|[；;\s]+$/g, "").replace(/[；;]{2,}/g, "；");
    chip.classList.remove("active");
  } else {
    ta.value = ta.value.trim() ? ta.value.trim().replace(/[；;。\s]+$/,"") + "；" + text : text;
    chip.classList.add("active");
  }
  ta.dispatchEvent(new Event("change"));
  syncChips();
});
function syncChips() {
  const v = $("#custom-prompt").value;
  $$("#custom-chips .cchip").forEach(c => c.classList.toggle("active", v.includes(c.dataset.c)));
}
$("#custom-prompt").addEventListener("input", syncChips);

/* ================= 标准答案库 ================= */
function renderAnswerSelect() {
  const sel = $("#answer-select");
  const cur = sel.value;
  sel.innerHTML = `<option value="">不用答案（AI 自行判断）</option>` +
    S.answerKeys.map(a => `<option value="${a.id}">${esc(a.title)}（${a.question_count || "?"}题 · ${esc(a.date)}）</option>`).join("");
  if (S.answerKeys.some(a => a.id === cur)) sel.value = cur;
  updateAnswerHint();
}
function updateAnswerHint() {
  const id = $("#answer-select").value;
  $("#answer-hint").textContent = id
    ? "✓ 批改将对照这份标准答案判分（答案已提取为文本，不再重复识图，更准更省）"
    : "不选则 AI 自行判断答案";
}
$("#answer-select").addEventListener("change", updateAnswerHint);
$("#btn-answer-manage").onclick = () => {
  const rows = S.answerKeys.map(a => `<tr>
    <td>${esc(a.title)}</td><td class="muted">${esc(a.date)}</td><td>${a.question_count || "?"}题</td>
    <td style="text-align:right;white-space:nowrap">
      <button type="button" class="btn sm" data-akview="${a.id}">✏️ 编辑</button>
      <button type="button" class="btn sm danger" data-akdel="${a.id}">删除</button>
    </td></tr>`).join("");
  modal("标准答案库（一次上传，全班复用）", `
    <p class="muted small" style="margin-top:-4px">上传答案图片由 AI 提取文本，或手动直接写答案。<b>提取有误可点「编辑」直接改，保存后立刻用于批改。</b></p>
    <div class="field"><label>答案名称（如：五上期末卷答案）</label><input type="text" id="ak-title" maxlength="30" placeholder="留空用 AI 识别的试卷名"></div>
    <div class="field"><label>答案图片（可多张，最多4张；不选图片则用下方手动文本）</label><input type="file" id="ak-files" accept="image/png,image/jpeg,image/webp" multiple></div>
    <div class="field"><label>手动答案文本（可选：直接粘贴/输入答案，按题号一行一题）</label>
      <textarea id="ak-manual" rows="4" placeholder="1. 月落乌啼霜满天，江枫渔火对愁眠……&#10;2. 懒惰、叮嘱&#10;3. ……"></textarea></div>
    <div id="ak-status" class="small muted"></div>
    ${rows ? `<table class="list"><thead><tr><th>名称</th><th>日期</th><th>题数</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : `<div class="empty small">还没有标准答案，先上传或手动写一份。</div>`}`,
    [{ label: "保存答案", cls: "primary", onClick: async () => {
      const files = Array.from($("#ak-files").files || []);
      const manual = $("#ak-manual").value.trim();
      const title = $("#ak-title").value.trim();
      // 无图片 + 有文本 => 手动新建（不消耗 AI）
      if (!files.length && manual) {
        if (!title) { toast("手动答案请填写名称"); return; }
        const j = await post("/api/answer/manual", { title, answers_text: manual });
        S.answerKeys.push({ id: j.answer_key.id, title: j.answer_key.title, date: j.answer_key.date, question_count: j.answer_key.question_count });
        renderAnswerSelect();
        $("#answer-select").value = j.answer_key.id; updateAnswerHint();
        closeModal(); toast("手动答案已保存：" + j.answer_key.title);
        return;
      }
      if (!files.length) { toast("请选择答案图片，或填写手动答案文本"); return; }
      if (!S.aiConfig || !S.aiConfig.configured) { toast("请先配置 AI 接口"); return; }
      $("#ak-status").textContent = "AI 正在提取答案文本，约 20~60 秒…";
      const images = await Promise.all(files.map(f => new Promise((res, rej) => {
        const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(f);
      })));
      try {
        const j = await post("/api/answer/create", { images, title });
        // 有手动文本时以手动为准覆盖提取结果
        if (manual) {
          await post("/api/answer/update", { id: j.answer_key.id, answers_text: manual });
          j.answer_key.answers_text = manual;
        }
        S.answerKeys.push({ id: j.answer_key.id, title: j.answer_key.title, date: j.answer_key.date, question_count: j.answer_key.question_count });
        renderAnswerSelect();
        $("#answer-select").value = j.answer_key.id; updateAnswerHint();
        closeModal(); toast("答案已保存：" + j.answer_key.title);
      } catch (e) { $("#ak-status").innerHTML = `<span style="color:var(--bad)">✗ ${esc(e.message)}</span>`; }
    } },
    { label: "关闭", onClick: closeModal }]);
  $$("[data-akview]").forEach(b => b.onclick = async () => {
    const a = await api("/api/answer?id=" + b.dataset.akview);
    modal("编辑答案 · " + a.title, `
      <p class="muted small" style="margin-top:-4px">AI 提取有误可直接修改，<b>保存后立刻用于后续批改</b>（按题号一行一题，多个可接受答案都列上）。</p>
      <div class="field"><label>答案名称</label><input type="text" id="ak-edit-title" maxlength="40" value="${esc(a.title)}"></div>
      <div class="field"><label>答案文本${a.edited ? `（上次手动修改：${esc(a.edited_at || "")}）` : a.manual ? "（手动创建）" : "（AI 提取）"}</label>
        <textarea id="ak-edit-text" rows="14" style="width:100%;font-size:13px;line-height:1.9">${esc(a.answers_text)}</textarea></div>
      <div id="ak-edit-status" class="small muted"></div>`,
      [{ label: "返回", onClick: () => $("#btn-answer-manage").click() },
       { label: "💾 保存修改", cls: "primary", onClick: async () => {
        const text = $("#ak-edit-text").value.trim();
        if (!text) { toast("答案文本不能为空"); return; }
        const j = await post("/api/answer/update", {
          id: a.id, answers_text: text, title: $("#ak-edit-title").value.trim() });
        const idx = S.answerKeys.findIndex(x => x.id === a.id);
        if (idx >= 0) S.answerKeys[idx] = { id: j.answer_key.id, title: j.answer_key.title, date: j.answer_key.date, question_count: j.answer_key.question_count };
        renderAnswerSelect();
        closeModal(); toast("答案已更新，下次批改立即生效");
      } }]);
    setTimeout(() => $("#ak-edit-text").focus(), 60);
  });
  $$("[data-akdel]").forEach(b => b.onclick = async () => {
    await del("/api/answer/" + b.dataset.akdel);
    S.answerKeys = S.answerKeys.filter(a => a.id !== b.dataset.akdel);
    renderAnswerSelect(); $("#btn-answer-manage").click(); toast("已删除");
  });
};

/* ================= 白板渲染（常驻：未批改显示上传原图，批改后叠加标注） ================= */
function boardSources() {
  // 返回当前白板显示的图片来源列表：已批改用服务端文件，否则用本地上传
  if (S.view) {
    const imgs = S.view.images && S.view.images.length ? S.view.images : (S.view.image ? [S.view.image] : []);
    return imgs.map(f => "/uploads/" + f);
  }
  return S.images.map(im => im.dataUrl);
}
function renderBoard(exam) {
  const srcs = boardSources();
  const has = srcs.length > 0;
  $("#board-empty").hidden = has;
  $("#annotated-wrap").hidden = !has;
  $("#no-image-note").hidden = has || !exam;
  const vp = $("#board-viewport");
  vp.classList.toggle("fit", S.zoom === "fit");
  vp.classList.toggle("full", S.zoom === "full");
  $("#btn-zoom").textContent = S.zoom === "big" ? "🔍 放大" : S.zoom === "full" ? "🔍 适配" : "🔍 大图";
  // 页签
  $("#page-tabs").innerHTML = srcs.length > 1 ? srcs.map((_, p) =>
    `<button type="button" class="page-tab ${p === S.page ? "active" : ""}" data-page="${p}">第 ${p + 1} 页</button>`).join("") : "";
  $$("[data-page]").forEach(b => b.onclick = async () => {
    await flushEdits();
    S.page = Number(b.dataset.page); S.selected = -1;
    $("#editor-card").hidden = true;
    renderBoard(S.view); renderThumbs();
  });
  const layer = $("#annotation-layer");
  layer.innerHTML = "";
  if (has) {
    const img = $("#annotated-img");
    const src = srcs[Math.min(S.page, srcs.length - 1)];
    if (img.getAttribute("src") !== src) img.src = src;
    if (exam && !exam.demo) {
      exam.questions.forEach((q, i) => {
        if (!q.bbox || (q.page || 0) !== S.page) return;
        const ok = exam.results[i].correct;
        if (ok && !S.showCorrect && S.selected !== i) return;   // 默认隐藏正确标注
        layer.appendChild(makeAnno(exam, q, i));
      });
    }
  }
  renderMarks();
  renderChips(exam);
}
function makeAnno(exam, q, i) {
  const ok = exam.results[i].correct;
  const el = document.createElement("button");
  el.type = "button";
  el.className = "anno " + (ok ? "ok" : "bad") + (S.selected === i ? " selected" : "");
  el.dataset.anno = i;
  const place = bb => {
    el.style.left = (bb[0] * 100) + "%"; el.style.top = (bb[1] * 100) + "%";
    el.style.width = ((bb[2] - bb[0]) * 100) + "%"; el.style.height = ((bb[3] - bb[1]) * 100) + "%";
  };
  place(q.bbox);
  el.title = `第${i + 1}题 · ${q.kp_name} · ${ok ? "答对" : "答错"}\n单击编辑 · 双击改判 · 拖动移动 · 右下角缩放`;
  el.innerHTML = `<span class="no">${i + 1}</span><span class="mark">${ok ? "✓" : "✗"}</span><span class="resize" data-rs="${i}"></span>`;
  let drag = null;
  el.addEventListener("pointerdown", e => {
    if (S.tool !== "select") return;
    drag = { x: e.clientX, y: e.clientY, bb: [...q.bbox], resize: e.target.dataset.rs !== undefined, moved: false };
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
    e.preventDefault();
  });
  el.addEventListener("pointermove", e => {
    if (!drag) return;
    const wrap = $("#annotated-wrap").getBoundingClientRect();
    const dx = (e.clientX - drag.x) / wrap.width;
    const dy = (e.clientY - drag.y) / wrap.height;
    if (Math.abs(dx) + Math.abs(dy) > 0.004) drag.moved = true;
    if (!drag.moved) return;
    let bb;
    if (drag.resize) {
      bb = [drag.bb[0], drag.bb[1],
            Math.min(1, Math.max(drag.bb[0] + 0.02, drag.bb[2] + dx)),
            Math.min(1, Math.max(drag.bb[1] + 0.02, drag.bb[3] + dy))];
    } else {
      const w = drag.bb[2] - drag.bb[0], h = drag.bb[3] - drag.bb[1];
      const nx = Math.min(1 - w, Math.max(0, drag.bb[0] + dx));
      const ny = Math.min(1 - h, Math.max(0, drag.bb[1] + dy));
      bb = [nx, ny, nx + w, ny + h];
    }
    q.bbox = bb; place(bb);
  });
  el.addEventListener("pointerup", () => {
    if (!drag) return;
    const moved = drag.moved; drag = null;
    if (moved) scheduleSave(i, { bbox: q.bbox });
  });
  el.addEventListener("click", e => { if (S.tool === "select" && e.detail === 1) selectQuestion(i); });
  el.addEventListener("dblclick", e => { if (S.tool === "select") { e.preventDefault(); quickToggle(i); } });
  return el;
}
function renderChips(exam) {
  if (!exam) { $("#question-chips").innerHTML = ""; return; }
  const srcs = boardSources();
  const need = exam.questions.map((q, i) => ({ q, i }))
    .filter(x => !srcs.length || !x.q.bbox || (x.q.page || 0) !== S.page);
  $("#question-chips").innerHTML = need.map(({ q, i }) => {
    const ok = exam.results[i].correct;
    const pg = srcs.length > 1 ? `<span style="font-weight:400;color:var(--muted)">P${(q.page || 0) + 1}</span> ` : "";
    return `<button type="button" class="q-chip ${ok ? "ok" : "bad"}${S.selected === i ? " selected" : ""}" data-chip="${i}" title="${esc(q.stem)}">${pg}${i + 1} ${ok ? "✓" : "✗"}</button>`;
  }).join("");
  $$("[data-chip]").forEach(b => {
    b.onclick = () => selectQuestion(Number(b.dataset.chip));
    b.ondblclick = () => quickToggle(Number(b.dataset.chip));
  });
}

/* ================= 批注工具（勾/叉/圈/书写/文字/橡皮，六色可选） ================= */
function setTool(t) {
  S.tool = t;
  $$("[data-tool]").forEach(b => b.classList.toggle("active", b.dataset.tool === t));
  const vp = $("#board-viewport");
  vp.classList.toggle("drawing", t !== "select");
  vp.dataset.tool = t;
  hideTextInput();
}
$$("[data-tool]").forEach(b => b.onclick = () => setTool(b.dataset.tool));
$$(".color-dot").forEach(b => b.onclick = () => {
  S.color = b.dataset.color;
  $$(".color-dot").forEach(x => x.classList.toggle("active", x === b));
});
$("#show-correct").addEventListener("change", e => {
  S.showCorrect = e.target.checked;
  renderBoard(S.view);
});
$("#btn-zoom").onclick = () => {
  S.zoom = S.zoom === "big" ? "full" : S.zoom === "full" ? "fit" : "big";
  renderBoard(S.view);
};

function pageMarks() { return S.marks[String(S.page)] || (S.marks[String(S.page)] = []); }
function markPath(m) {
  // 归一化坐标 -> SVG path（viewBox 0 0 1000 1000）
  if (m.t === "tick") {
    const [x, y] = m.p, s = m.s || 0.03;
    return `M ${ (x - s) * 1000 } ${ y * 1000 } L ${ (x - s * 0.3) * 1000 } ${ (y + s * 0.7) * 1000 } L ${ (x + s) * 1000 } ${ (y - s * 0.8) * 1000 }`;
  }
  if (m.t === "cross") {
    const [x, y] = m.p, s = m.s || 0.025;
    return `M ${ (x - s) * 1000 } ${ (y - s) * 1000 } L ${ (x + s) * 1000 } ${ (y + s) * 1000 } M ${ (x + s) * 1000 } ${ (y - s) * 1000 } L ${ (x - s) * 1000 } ${ (y + s) * 1000 }`;
  }
  if (m.t === "pen") {
    return m.pts.map((p, i) => `${i ? "L" : "M"} ${p[0] * 1000} ${p[1] * 1000}`).join(" ");
  }
  return "";
}
function renderMarks() {
  const svg = $("#marks-svg");
  svg.setAttribute("viewBox", "0 0 1000 1000");
  svg.setAttribute("preserveAspectRatio", "none");
  const marks = S.marks[String(S.page)] || [];
  svg.innerHTML = marks.map((m, idx) => {
    const color = m.color || "#d0342c";
    const common = `class="mk" data-mk="${idx}" fill="none" stroke="${color}" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"`;
    if (m.t === "text") {
      // 文字批注：SVG text，字号按 viewBox 比例（约图片宽的 2.2%）
      return `<text class="mk" data-mk="${idx}" x="${m.p[0] * 1000}" y="${m.p[1] * 1000}" fill="${color}" font-size="${(m.fs || 0.022) * 1000}" font-family="KaiTi,STKaiti,Microsoft YaHei,serif" font-weight="700">${esc(m.text)}</text>`;
    }
    if (m.t === "circle") {
      const cx = (m.c[0] + m.c[2]) / 2 * 1000, cy = (m.c[1] + m.c[3]) / 2 * 1000;
      const rx = Math.abs(m.c[2] - m.c[0]) / 2 * 1000, ry = Math.abs(m.c[3] - m.c[1]) / 2 * 1000;
      return `<ellipse ${common} cx="${cx}" cy="${cy}" rx="${rx}" ry="${ry}"/>`;
    }
    return `<path ${common} d="${markPath(m)}"/>`;
  }).join("");
  $$(".mk", svg).forEach(el => {
    const idx = Number(el.dataset.mk);
    // 橡皮：点中一笔删除
    el.addEventListener("pointerdown", e => {
      if (S.tool === "erase") {
        e.stopPropagation();
        pageMarks().splice(idx, 1);
        saveMarks(); renderMarks();
        return;
      }
      if (S.tool !== "select") return;
      // 选择模式：拖拽移动批注（文字/勾/叉/圈/笔迹均可）
      const m = pageMarks()[idx];
      if (!m) return;
      e.stopPropagation();
      e.preventDefault();
      const wrap = $("#annotated-wrap").getBoundingClientRect();
      const start = { x: e.clientX, y: e.clientY };
      const orig = JSON.parse(JSON.stringify(m));
      let moved = false;
      el.style.cursor = "grabbing";
      try { el.setPointerCapture(e.pointerId); } catch (_) { /* 合成事件无活动指针时忽略 */ }
      const onMove = ev => {
        const dx = (ev.clientX - start.x) / wrap.width;
        const dy = (ev.clientY - start.y) / wrap.height;
        if (Math.abs(dx) + Math.abs(dy) > 0.003) moved = true;
        if (!moved) return;
        const clamp = v => Math.min(1, Math.max(0, v));
        if (m.t === "circle") {
          const w = orig.c[2] - orig.c[0], h = orig.c[3] - orig.c[1];
          const nx = Math.min(1 - w, Math.max(0, orig.c[0] + dx));
          const ny = Math.min(1 - h, Math.max(0, orig.c[1] + dy));
          m.c = [nx, ny, nx + w, ny + h];
        } else if (m.t === "pen") {
          m.pts = orig.pts.map(p => [clamp(p[0] + dx), clamp(p[1] + dy)]);
        } else {
          m.p = [clamp(orig.p[0] + dx), clamp(orig.p[1] + dy)];
        }
        // 原位更新该元素，不重建 DOM（保持 pointer capture）
        if (m.t === "circle") {
          el.setAttribute("cx", (m.c[0] + m.c[2]) / 2 * 1000);
          el.setAttribute("cy", (m.c[1] + m.c[3]) / 2 * 1000);
        } else if (m.t === "text") {
          el.setAttribute("x", m.p[0] * 1000);
          el.setAttribute("y", m.p[1] * 1000);
        } else {
          el.setAttribute("d", markPath(m));
        }
      };
      const onUp = () => {
        el.removeEventListener("pointermove", onMove);
        el.removeEventListener("pointerup", onUp);
        el.removeEventListener("pointercancel", onUp);
        el.style.cursor = "";
        if (moved) saveMarks();
      };
      el.addEventListener("pointermove", onMove);
      el.addEventListener("pointerup", onUp);
      el.addEventListener("pointercancel", onUp);
    });
  });
}
async function saveMarks() {
  if (S.view && S.view.id && !S.view.demo) {
    try {
      setSaveState("saving", "保存中…");
      await post("/api/exam/marks", { exam_id: S.view.id, page: S.page, set: pageMarks() });
      S.view.marks = S.marks;
      setSaveState("saved", "✓ 已保存");
      setTimeout(() => { if ($("#save-state").textContent === "✓ 已保存") setSaveState("", ""); }, 2000);
    } catch (e) { setSaveState("error", "保存失败"); toast("批注保存失败：" + e.message, 5000); }
  }
  // 未批改时批注暂存内存，批改提交后随 exam 一起保存
}
function undoMark() {
  const arr = S.marks[String(S.page)];
  if (arr && arr.length) { arr.pop(); saveMarks(); renderMarks(); toast("已撤销一笔"); }
}
$("#btn-undo-mark").onclick = undoMark;
$("#btn-clear-marks").onclick = () => {
  if (!(S.marks[String(S.page)] || []).length) return;
  S.marks[String(S.page)] = [];
  saveMarks(); renderMarks(); toast("已清空本页笔迹");
};

/* 文字批注：点击卷面弹出输入框，回车落字 */
const textOverlay = $("#text-input-overlay");
let textPos = null;
function showTextInput(p, clientX, clientY) {
  textPos = p;
  const wrapR = $("#annotated-wrap").getBoundingClientRect();
  textOverlay.style.left = (clientX - wrapR.left) + "px";
  textOverlay.style.top = (clientY - wrapR.top - 14) + "px";
  textOverlay.style.color = S.color;
  textOverlay.value = "";
  textOverlay.hidden = false;
  setTimeout(() => textOverlay.focus(), 30);
}
function hideTextInput() { textOverlay.hidden = true; textPos = null; }
function commitText() {
  const v = textOverlay.value.trim();
  if (v && textPos) {
    pageMarks().push({ t: "text", p: textPos, text: v, color: S.color, fs: 0.022 });
    saveMarks(); renderMarks();
  }
  hideTextInput();
}
textOverlay.addEventListener("keydown", e => {
  e.stopPropagation();
  if (e.key === "Enter") commitText();
  if (e.key === "Escape") hideTextInput();
});
textOverlay.addEventListener("blur", commitText);

/* 落笔：在白板视口上监听（drawing 模式下标注层不拦截） */
const vp = $("#board-viewport");
function normPos(e) {
  const r = $("#annotated-wrap").getBoundingClientRect();
  return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
          Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
}
vp.addEventListener("pointerdown", e => {
  if (S.tool === "select" || S.tool === "erase") return;
  if ($("#annotated-wrap").hidden) return;
  if (e.target === textOverlay) return;
  const p = normPos(e);
  if (S.tool === "text") {
    if (!textOverlay.hidden) commitText();
    showTextInput(p, e.clientX, e.clientY);
    e.preventDefault();
    return;
  }
  if (S.tool === "tick" || S.tool === "cross") {
    pageMarks().push({ t: S.tool, p, s: S.tool === "tick" ? 0.03 : 0.025, color: S.color });
    saveMarks(); renderMarks();
    return;
  }
  S.drawing = { tool: S.tool, start: p, pts: [p], color: S.color };
  try { vp.setPointerCapture(e.pointerId); } catch (_) {}
  e.preventDefault();
});
vp.addEventListener("pointermove", e => {
  if (!S.drawing) return;
  const p = normPos(e);
  const svg = $("#marks-svg");
  if (S.drawing.tool === "pen") {
    S.drawing.pts.push(p);
    const d = S.drawing.pts.map((q, i) => `${i ? "L" : "M"} ${q[0] * 1000} ${q[1] * 1000}`).join(" ");
    let tmp = svg.querySelector("#tmp-stroke");
    if (!tmp) {
      tmp = document.createElementNS("http://www.w3.org/2000/svg", "path");
      tmp.id = "tmp-stroke";
      tmp.setAttribute("fill", "none"); tmp.setAttribute("stroke", S.drawing.color);
      tmp.setAttribute("stroke-width", "4.5"); tmp.setAttribute("stroke-linecap", "round");
      svg.appendChild(tmp);
    }
    tmp.setAttribute("d", d);
  } else if (S.drawing.tool === "circle") {
    const [x0, y0] = S.drawing.start;
    let tmp = svg.querySelector("#tmp-circle");
    if (!tmp) {
      tmp = document.createElementNS("http://www.w3.org/2000/svg", "ellipse");
      tmp.id = "tmp-circle";
      tmp.setAttribute("fill", "none"); tmp.setAttribute("stroke", S.drawing.color);
      tmp.setAttribute("stroke-width", "4.5");
      svg.appendChild(tmp);
    }
    tmp.setAttribute("cx", (x0 + p[0]) / 2 * 1000); tmp.setAttribute("cy", (y0 + p[1]) / 2 * 1000);
    tmp.setAttribute("rx", Math.abs(p[0] - x0) / 2 * 1000); tmp.setAttribute("ry", Math.abs(p[1] - y0) / 2 * 1000);
  }
});
vp.addEventListener("pointerup", e => {
  if (!S.drawing) return;
  const p = normPos(e);
  const d = S.drawing; S.drawing = null;
  if (d.tool === "pen" && d.pts.length > 1) {
    pageMarks().push({ t: "pen", pts: d.pts, color: d.color });
  } else if (d.tool === "circle") {
    const [x0, y0] = d.start;
    if (Math.abs(p[0] - x0) > 0.01 && Math.abs(p[1] - y0) > 0.01) {
      pageMarks().push({ t: "circle", c: [Math.min(x0, p[0]), Math.min(y0, p[1]), Math.max(x0, p[0]), Math.max(y0, p[1])], color: d.color });
    }
  }
  saveMarks(); renderMarks();
});/* ================= 批改 ================= */
$("#btn-grade").onclick = async () => {
  if (S.grading) return;
  if (!S.images.length) { toast("请先上传试卷照片"); return; }
  if (!S.aiConfig || !S.aiConfig.configured) { toast("请先配置 AI 接口"); $("#btn-settings").click(); return; }
  await flushEdits();
  const preMarks = JSON.parse(JSON.stringify(S.marks));   // 保留批改前的手动笔迹
  S.grading = true;
  $("#btn-grade").disabled = true;
  $("#progress-card").hidden = false;
  $("#pg-title").textContent = (S.mode === "think" ? "🧠 详尽分析" : "⚡ 精简分析") + `批改中…（${S.images.length} 张）`;
  $("#pg-sub").textContent = S.mode === "think" ? "逐字推敲，约 2~5 分钟，请耐心等待" : "识别 → 判分 → 错因，约 1~2 分钟（取决于模型速度），请勿关闭页面";
  try {
    const j = await post("/api/grade/upload", {
      images: S.images.map(im => im.dataUrl),
      student_name: $("#upload-student").value.trim(),
      mode: S.mode, custom: $("#custom-prompt").value.trim(),
      answer_key_id: $("#answer-select").value || undefined,
    });
    S.students = mergeStudent(j.student);
    // 手动笔迹随新 exam 保存
    S.marks = preMarks;
    for (const [pg, arr] of Object.entries(preMarks)) {
      if (arr.length) await post("/api/exam/marks", { exam_id: j.exam.id, page: Number(pg), set: arr });
    }
    showResult({ ...j.exam, demo: false });
    toast("批改完成：" + j.exam.score + " 分");
  } catch (e) {
    toast(e.message, 6000);
  } finally {
    S.grading = false;
    $("#btn-grade").disabled = false;
    $("#progress-card").hidden = true;
  }
};

$("#btn-demo").onclick = async () => {
  const j = await api("/api/demo");
  showResult({
    id: null, demo: true, title: j.paper_title, student_name: j.student_name,
    summary: j.summary, parent_report: j.parent_report,
    questions: j.questions, results: j.results,
    kp_stat: j.kp_stat, score: j.score, date: new Date().toISOString().slice(0, 10),
  });
};

$("#btn-next-paper").onclick = async () => {
  await flushEdits();
  S.view = null; S.images = []; S.selected = -1; S.training = null; S.pending = {}; S.marks = {}; S.page = 0;
  renderThumbs(); renderBoard(null);
  $("#score-strip").hidden = true;
  $("#result-banner").hidden = true;
  $("#right-result").hidden = true;
  $("#right-empty").hidden = false;
  $("#editor-card").hidden = true;
  $("#btn-next-paper").hidden = true;
  $("#upload-student").focus();
  toast("已就绪，上传下一份试卷");
  window.scrollTo({ top: 0, behavior: "smooth" });
};

/* ================= 结果渲染 ================= */
function showResult(exam) {
  S.view = exam; S.training = null; S.selected = -1; S.page = 0; S.pending = {};
  S.marks = JSON.parse(JSON.stringify(exam.marks || {}));
  $("#train-result").hidden = true;
  $("#train-progress").hidden = true;
  $("#tier-plan-box").hidden = true;
  $("#editor-card").hidden = true;
  $("#save-state").textContent = "";
  $("#right-empty").hidden = true;
  $("#right-result").hidden = false;
  $("#btn-next-paper").hidden = exam.demo;

  const banner = $("#result-banner");
  if (exam.demo) {
    banner.hidden = false; banner.className = "demo";
    banner.innerHTML = `🎭 演示数据（虚构试卷）。上传真实照片体验完整批改。
      <button type="button" class="btn sm soft" id="demo-save" style="margin-left:6px">存入档案</button>`;
    $("#demo-save").onclick = async () => {
      const j = await post("/api/demo/save", {});
      S.students = mergeStudent(j.student);
      showResult({ ...j.exam, demo: false });
      toast("已存入演示学生档案");
    };
  } else { banner.hidden = true; banner.innerHTML = ""; }

  renderScoreStrip(exam);
  renderBoard(exam);
  renderThumbs();
  renderReports(exam);
  renderParentReport(exam);
  const wrongKps = wrongKpList(exam);
  $("#train-desc").textContent = wrongKps.length
    ? "AI 将针对「" + wrongKps.map(k => k.name).join("、") + "」的具体错误现场编新题。"
    : "本卷全对！可让 AI 出一套巩固提升题。";
  $("#btn-train").disabled = exam.demo;
}

function renderScoreStrip(exam) {
  const total = exam.results.length;
  const right = exam.results.filter(r => r.correct).length;
  const stuName = exam.student_name ||
    (S.students.find(s => s.id === exam.student_id) || {}).name || "学生";
  const modeTag = exam.mode === "think" ? '<span class="pill info">🧠 思考</span>'
    : exam.mode === "fast" ? '<span class="pill info">⚡ 快速</span>' : "";
  const customTag = exam.custom ? `<span class="pill info" title="${esc(exam.custom)}">🎛 自定义</span>` : "";
  $("#score-strip").hidden = false;
  $("#score-strip").innerHTML = `
    <div class="score-top">
      <div class="score-big">${exam.score}<small> /100</small></div>
      <div class="score-meta"><b>${esc(exam.title || "试卷")}</b>
        <span class="muted small">${esc(stuName)} · ${esc(exam.date || "")}</span></div>
    </div>
    <div class="score-pills">
      <span class="pill ok">✓ ${right}</span>
      <span class="pill bad">✗ ${total - right}</span>
      <span class="pill info">共 ${total} 题</span>${modeTag}${customTag}
    </div>
    ${exam.summary ? `<div id="summary-text">📝 ${esc(exam.summary)}</div>` : ""}`;
}

/* ---------- 自动保存编辑 ---------- */
function setSaveState(state, text) {
  const el = $("#save-state");
  el.className = "small " + state;
  el.textContent = text;
}
function scheduleSave(i, fields) {
  if (!S.view || S.view.demo || !S.view.id) return;
  S.pending[i] = { ...(S.pending[i] || {}), ...fields };
  setSaveState("saving", "保存中…");
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(flushEdits, 500);
}
async function flushEdits() {
  clearTimeout(S.saveTimer);
  if (!S.view || S.view.demo || !S.view.id || !Object.keys(S.pending).length) return;
  const payload = Object.entries(S.pending).map(([i, f]) => ({ index: Number(i), ...f }));
  S.pending = {};
  try {
    const j = await post("/api/exam/edit", { exam_id: S.view.id, questions: payload });
    const keep = S.selected;
    S.view = { ...j.exam, demo: false, marks: S.marks };
    S.selected = keep;
    renderScoreStrip(S.view);
    renderBoard(S.view);
    renderReports(S.view);
    renderParentReport(S.view);
    setSaveState("saved", "✓ 已保存");
    setTimeout(() => { if ($("#save-state").textContent === "✓ 已保存") setSaveState("", ""); }, 2000);
  } catch (e) {
    setSaveState("error", "保存失败");
    toast("保存失败：" + e.message, 6000);
  }
}
window.addEventListener("beforeunload", () => {
  if (Object.keys(S.pending).length && S.view && S.view.id && !S.view.demo) {
    const payload = Object.entries(S.pending).map(([i, f]) => ({ index: Number(i), ...f }));
    navigator.sendBeacon("/api/exam/edit", new Blob(
      [JSON.stringify({ exam_id: S.view.id, questions: payload })], { type: "application/json" }));
  }
});
function quickToggle(i) {
  const ok = !S.view.results[i].correct;
  S.view.results[i].correct = ok;
  scheduleSave(i, { correct: ok });
  renderBoard(S.view);
  renderScoreStripPreview();
  toast(`第${i + 1}题已改判为${ok ? "✓ 正确" : "✗ 错误"}（自动保存）`);
}
function renderScoreStripPreview() {
  const rs = S.view.results;
  const score = Math.round(rs.filter(r => r.correct).length / rs.length * 100);
  const el = $(".score-big");
  if (el) el.innerHTML = `${score}<small> /100</small>`;
}
function selectQuestion(i) {
  S.selected = i;
  const exam = S.view, q = exam.questions[i];
  const srcs = boardSources();
  if (srcs.length && q.bbox && (q.page || 0) !== S.page) S.page = q.page || 0;
  setTool("select");
  renderBoard(exam);
  const ok = exam.results[i].correct;
  $("#editor-card").hidden = false;
  $("#editor-title").textContent = `✏️ 第 ${i + 1} 题 · ${q.kp_name}`;
  const box = $("#question-editor");
  box.innerHTML = `<div class="qe-box">
    <div class="judge" style="margin-bottom:10px">
      <button type="button" class="judge-btn ${ok ? "on-ok" : ""}" data-j="ok">✓ 正确</button>
      <button type="button" class="judge-btn ${!ok ? "on-bad" : ""}" data-j="bad">✗ 错误</button>
      <div class="qe-nav">
        <button type="button" class="btn sm" id="qe-prev" ${i === 0 ? "disabled" : ""}>← 上一题</button>
        <button type="button" class="btn sm" id="qe-next" ${i === exam.questions.length - 1 ? "disabled" : ""}>下一题 →</button>
      </div>
    </div>
    <div class="field"><label>题干</label><textarea rows="2" data-f="stem">${esc(q.stem)}</textarea></div>
    <div class="row">
      <div class="field"><label>学生作答</label><input type="text" data-f="student_answer" value="${esc(q.student_answer)}"></div>
      <div class="field"><label>参考答案</label><input type="text" data-f="answer" value="${esc(q.answer)}"></div>
      <div class="field"><label>知识点</label><input type="text" data-f="kp_name" value="${esc(q.kp_name)}"></div>
    </div>
    <div class="field"><label>错因分析</label><textarea rows="2" data-f="analysis">${esc(q.analysis || "")}</textarea></div>
  </div>`;
  $$("[data-j]", box).forEach(b => b.onclick = () => {
    const val = b.dataset.j === "ok";
    exam.results[i].correct = val;
    $$("[data-j]", box).forEach(x => x.classList.remove("on-ok", "on-bad"));
    b.classList.add(val ? "on-ok" : "on-bad");
    scheduleSave(i, { correct: val });
    renderBoard(exam);
    renderScoreStripPreview();
  });
  $$("[data-f]", box).forEach(inp => inp.addEventListener("change", () => {
    const f = inp.dataset.f;
    q[f] = inp.value;
    if (f === "kp_name") { q.kp = "ai:" + inp.value; $("#editor-title").textContent = `✏️ 第 ${i + 1} 题 · ${inp.value}`; }
    scheduleSave(i, { [f]: inp.value });
  }));
  $("#qe-prev").onclick = async () => { await flushEdits(); selectQuestion(i - 1); };
  $("#qe-next").onclick = async () => { await flushEdits(); selectQuestion(i + 1); };
  $("#editor-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
$("#qe-close").onclick = async () => {
  await flushEdits();
  S.selected = -1;
  $("#editor-card").hidden = true;
  if (S.view) renderBoard(S.view);
};/* ================= 报告与家长小结 ================= */
function wrongKpList(exam) {
  const map = {};
  exam.questions.forEach((q, i) => {
    if (!exam.results[i].correct) {
      const e = map[q.kp_name] || (map[q.kp_name] = { name: q.kp_name, count: 0 });
      e.count++;
    }
  });
  return Object.values(map).sort((a, b) => b.count - a.count);
}
function renderReports(exam) {
  const wrongs = exam.questions.map((q, i) => ({ q, i })).filter(x => !exam.results[x.i].correct);
  if (!wrongs.length) {
    $("#wrong-report").innerHTML = `<div class="empty">🎉 全部答对，没有错题！</div>`;
  } else {
    const groups = wrongKpList(exam);
    $("#wrong-report").innerHTML = `
      <div class="small muted" style="margin-bottom:6px">共 ${wrongs.length} 道错题，集中在 ${groups.length} 类知识点：</div>
      ${groups.map(g => `<span class="pill bad">${esc(g.name)} ×${g.count}</span>`).join("")}
      ${wrongs.map(({ q, i }) => `<div class="wr-item">
        <div class="wr-title"><button type="button" class="btn ghost sm" data-goto="${i}" style="padding:2px 8px">第 ${i + 1} 题</button> ${esc(q.kp_name)}</div>
        <div class="wr-detail">${esc(q.stem)}<br>
          学生作答 <b style="color:var(--bad)">${esc(q.student_answer || "未作答")}</b>
          → 参考答案 <b style="color:var(--ok)">${esc(q.answer)}</b></div>
        ${q.analysis ? `<div class="wr-detail" style="color:#8f1d1d">错因：${esc(q.analysis)}</div>` : ""}
      </div>`).join("")}`;
    $$("[data-goto]").forEach(b => b.onclick = () => selectQuestion(Number(b.dataset.goto)));
  }
  const stats = Object.values(exam.kp_stat || {}).map(st => ({
    ...st, rate: Math.round((st.total - st.wrong) / st.total * 100)
  })).sort((a, b) => a.rate - b.rate);
  $("#kp-report").innerHTML = stats.map(st => {
    const cls = st.rate >= 80 ? "" : st.rate >= 60 ? "mid" : "low";
    return `<div class="kp-row">
      <div>${esc(st.name)} ${st.rate < 80 ? '<span class="pill bad" style="padding:1px 7px;font-size:11px">薄弱</span>' : ""}</div>
      <div class="bar-wrap"><div class="bar ${cls}" style="width:${st.rate}%"></div></div>
      <div class="kp-rate">${st.rate}%（${st.total - st.wrong}/${st.total}）</div>
    </div>`;
  }).join("") || `<div class="empty">暂无数据</div>`;
}
function renderParentReport(exam) {
  const pr = exam.parent_report;
  const card = $("#parent-card");
  if (!pr || (!pr.problems && !(pr.steps || []).length)) {
    const wrongs = exam.questions.map((q, i) => ({ q, i })).filter(x => !exam.results[x.i].correct);
    if (!wrongs.length) { card.hidden = true; return; }
    const groups = wrongKpList(exam);
    fillParent({
      problems: `本次共错 ${wrongs.length} 题，集中在：${groups.map(g => g.name + "（" + g.count + "题）").join("、")}。`,
      cause: "详见上方错题分析报告中每题的错因说明。",
      steps: [
        "把本次错题抄入错字/错题本，写明正确写法和自己错在哪里",
        "每天晚饭后针对薄弱知识点练习 10 分钟（可用「AI 出题」生成练习卷打印）",
        "错字每个抄 3 遍并组 2 个词，第二天先重做昨天的错题再练新题",
        "一周后用同类题再测一次，对比错题数量是否下降"
      ]
    }, true);
    return;
  }
  fillParent(pr, false);
}
function fillParent(pr, isLocal) {
  $("#parent-card").hidden = false;
  $("#parent-report").innerHTML = `
    ${isLocal ? `<div class="small muted" style="margin-bottom:6px">（系统根据错题生成的通用建议）</div>` : ""}
    <div class="pr-block"><div class="pr-label">📌 孩子的问题</div><div class="pr-text">${esc(pr.problems || "")}</div></div>
    ${pr.cause ? `<div class="pr-block"><div class="pr-label">🔎 原因</div><div class="pr-text">${esc(pr.cause)}</div></div>` : ""}
    <div class="pr-block"><div class="pr-label">✅ 家长可以这样做</div>
      <ol class="pr-steps">${(pr.steps || []).map(s => `<li>${esc(s)}</li>`).join("")}</ol></div>`;
  $("#btn-copy-parent").onclick = async () => {
    const text = `【给家长的小结】\n孩子的问题：${pr.problems || ""}\n${pr.cause ? "原因：" + pr.cause + "\n" : ""}建议做法：\n${(pr.steps || []).map((s, i) => (i + 1) + ". " + s).join("\n")}`;
    try { await navigator.clipboard.writeText(text); toast("已复制，可直接发给家长"); }
    catch { toast("复制失败，请手动选择文字复制"); }
  };
}

/* ================= AI 出题 + PDF ================= */
$("#btn-train").onclick = async () => {
  const exam = S.view;
  if (!exam || exam.demo || S.trainingBusy) { if (exam && exam.demo) toast("演示数据请先存入档案"); return; }
  if (!S.aiConfig || !S.aiConfig.configured) { toast("AI 出题需要配置接口"); $("#btn-settings").click(); return; }
  await flushEdits();
  S.trainingBusy = true;
  $("#btn-train").disabled = true;
  $("#train-progress").hidden = false;
  $("#train-result").hidden = true;
  try {
    const j = await post("/api/training/create", {
      student_id: exam.student_id, exam_id: exam.id,
      count_per_kp: Number($("#train-per").value) || 4, engine: "ai",
    });
    S.training = j.training;
    $("#train-result").hidden = false;
    $("#tier-plan-box").hidden = true;
    $("#train-source").innerHTML = `<span class="pill info">🤖 AI 出题</span><span class="muted">共 ${j.training.items.length} 题 · 基于本卷错题设计</span>`;
    $("#train-list").innerHTML = j.training.items.map((it, i) =>
      `<div class="train-item"><b>${i + 1}．</b>${esc(it.stem)}<span class="kp">［${esc(it.kp_name)}］</span></div>`).join("");
    toast("AI 已生成 " + j.training.items.length + " 道训练题");
  } catch (e) {
    toast("AI 出题失败：" + e.message, 7000);
  } finally {
    S.trainingBusy = false;
    $("#btn-train").disabled = false;
    $("#train-progress").hidden = true;
  }
};

/* ================= 分层出题 ================= */
const TIER_CLS = { "基础层": "bad", "巩固层": "warn", "提升层": "ok" };
function renderTierPlan(plan) {
  const box = $("#tier-plan-box");
  if (!plan || !plan.tiers || !plan.tiers.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<div class="small" style="margin:10px 0 4px"><b>📶 分层计划</b>（依据 ${plan.exam_count} 份已批试卷的累计学情）</div>` +
    plan.tiers.map(t => `
      <div style="border:1px solid var(--border);border-radius:9px;padding:8px 10px;margin-bottom:6px">
        <span class="pill ${TIER_CLS[t.tier] || "info"}">${esc(t.tier)} · ${esc(t.focus)}</span>
        <div class="small muted" style="margin-top:3px">${esc(t.reason)}</div>
        <div class="small" style="margin-top:3px">${t.kps.map(k => `${esc(k.name)}（${k.rate}%）`).join("、") || "综合"} · 每类 ${t.count_per_kp} 题</div>
      </div>`).join("");
}
$("#btn-tiered").onclick = async () => {
  const exam = S.view;
  if (!exam || exam.demo || S.trainingBusy) { if (exam && exam.demo) toast("演示数据请先存入档案"); return; }
  if (!S.aiConfig || !S.aiConfig.configured) { toast("分层出题需要配置接口"); $("#btn-settings").click(); return; }
  await flushEdits();
  // 先取计划预览展示，再出题
  try {
    const p = await api("/api/tierplan?student_id=" + exam.student_id);
    renderTierPlan(p.plan);
    if (!p.plan.exam_count) { toast("该生还没有批改记录，无法分层"); return; }
  } catch (e) { toast(e.message); return; }
  S.trainingBusy = true;
  $("#btn-tiered").disabled = true;
  $("#train-progress").hidden = false;
  $("#train-result").hidden = true;
  try {
    const j = await post("/api/training/tiered", { student_id: exam.student_id });
    S.training = j.training;
    renderTierPlan(j.plan);
    $("#train-result").hidden = false;
    $("#train-source").innerHTML = `<span class="pill info">📶 分层专项</span><span class="muted">共 ${j.training.items.length} 题 · 按基础→巩固→提升排序</span>`;
    $("#train-list").innerHTML = j.training.items.map((it, i) =>
      `<div class="train-item"><b>${i + 1}．</b>${esc(it.stem)}<span class="kp">［${esc(it.tier || "")} · ${esc(it.kp_name)}］</span></div>`).join("");
    toast("分层训练已生成：" + j.training.items.length + " 题");
  } catch (e) {
    toast("分层出题失败：" + e.message, 7000);
  } finally {
    S.trainingBusy = false;
    $("#btn-tiered").disabled = false;
    $("#train-progress").hidden = true;
  }
};
$("#btn-pdf-plain").onclick = () => { if (S.training) downloadPdf(S.training.id, false); };
$("#btn-pdf-answers").onclick = () => { if (S.training) downloadPdf(S.training.id, true); };
async function downloadPdf(tid, withAnswers) {
  toast("正在生成 PDF…");
  try {
    const res = await fetch(`/api/training/${tid}/pdf?answers=${withAnswers ? 1 : 0}`);
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.error || "PDF 生成失败");
    }
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = decodeURIComponent(res.headers.get("X-Filename") || "training.pdf");
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    toast(withAnswers ? "答案版 PDF 已导出" : "无答案版 PDF 已导出");
  } catch (e) { toast(e.message, 5000); }
}/* ================= 学生档案 ================= */
$("#btn-archive").onclick = async () => {
  if (!S.students.length) {
    modal("学生档案", `<div class="empty">还没有学生档案。<br>上传试卷批改后会自动建档，也可手动添加。</div>`, [
      { label: "手动添加学生", cls: "primary", onClick: () => addStudentModal() },
      { label: "关闭", onClick: closeModal }]);
    return;
  }
  modal("学生档案管理", `
    <div class="arch-grid" id="arch-grid"></div>
    <div class="arch-detail" id="arch-detail"><div class="empty small">点击上方学生卡片，查看往期批改数据与学情分析</div></div>`,
    [{ label: "＋ 添加学生", onClick: () => addStudentModal() },
     { label: "关闭", onClick: closeModal }]);
  renderArchGrid();
};
function addStudentModal() {
  modal("添加学生", `
    <div class="field"><label>姓名（必填）</label><input type="text" id="m-name" maxlength="20"></div>
    <div class="field"><label>班级 / 年级</label><input type="text" id="m-grade" maxlength="20" placeholder="如：五年级2班"></div>
    <div class="field"><label>备注</label><input type="text" id="m-note" maxlength="60"></div>`, [
    { label: "取消", onClick: () => $("#btn-archive").click() },
    { label: "保存", cls: "primary", onClick: async () => {
      const name = $("#m-name").value.trim();
      if (!name) { toast("请填写姓名"); return; }
      const j = await post("/api/student", { name, grade: $("#m-grade").value, note: $("#m-note").value });
      S.students = mergeStudent(j.student);
      $("#btn-archive").click(); toast("已添加 " + name);
    } }]);
}
async function renderArchGrid() {
  const grid = $("#arch-grid");
  const cards = [];
  for (const s of S.students) {
    const d = await api("/api/student?id=" + s.id);
    cards.push({ s, d });
  }
  grid.innerHTML = cards.map(({ s, d }) => `
    <button type="button" class="arch-card" data-arch="${s.id}">
      <b>${esc(s.name)}</b>
      <div class="small muted">${esc(s.grade || "未填年级")}</div>
      <div class="small">批改 ${d.analysis.exam_count} 份${d.analysis.weak.length ? ` · <span style="color:var(--bad)">薄弱 ${d.analysis.weak.length} 类</span>` : ""}</div>
    </button>`).join("");
  $$("[data-arch]", grid).forEach(b => b.onclick = () => {
    $$(".arch-card", grid).forEach(x => x.classList.toggle("active", x === b));
    const { s, d } = cards.find(c => c.s.id === b.dataset.arch);
    renderArchDetail(s, d);
  });
}
function renderArchDetail(s, d) {
  const a = d.analysis;
  const hist = a.history.slice(-8);
  const box = $("#arch-detail");
  box.innerHTML = `
    <div class="flex" style="justify-content:space-between">
      <b style="font-size:15px">${esc(s.name)} 的学情档案</b>
      <div class="flex">
        <button type="button" class="btn sm" id="arch-edit">✏️ 编辑资料</button>
        <button type="button" class="btn sm danger" id="arch-del">删除学生</button>
      </div>
    </div>
    <div class="small muted" style="margin:4px 0 10px">${esc(s.grade || "")} ${esc(s.note || "")} · 建档 ${esc(s.created)}</div>
    ${a.exam_count ? `
      <div class="small" style="font-weight:700;margin-bottom:4px">成绩轨迹（最近 ${hist.length} 次）</div>
      <div class="trend">${hist.map(h => `<div class="trend-bar" style="height:${Math.max(h.score, 4)}%"><i>${h.score}</i></div>`).join("")}</div>
      <div class="trend-labels">${hist.map(h => `<span title="${esc(h.title)}">${esc(h.date.slice(5))}</span>`).join("")}</div>
      <div class="small" style="font-weight:700;margin:12px 0 4px">知识点掌握（累计全部试卷）</div>
      ${a.kp_stat.map(k => {
        const cls = k.rate >= 80 ? "" : k.rate >= 60 ? "mid" : "low";
        return `<div class="kp-row">
          <div>${esc(k.name)} ${k.weak ? '<span class="pill bad" style="padding:1px 7px;font-size:11px">薄弱</span>' : ""}</div>
          <div class="bar-wrap"><div class="bar ${cls}" style="width:${k.rate}%"></div></div>
          <div class="kp-rate">${k.rate}%（错${k.wrong}/${k.total}）</div>
        </div>${k.weak ? `<div class="small" style="color:var(--warn);padding:2px 0 6px">💡 ${esc(k.hint)}</div>` : ""}`;
      }).join("")}
      <div class="small" style="font-weight:700;margin:12px 0 4px">往期批改（点击查看卷面）</div>
      <table class="list"><tbody>
      ${d.exams.slice().reverse().map(e => `<tr>
        <td>${esc(e.title)}</td><td class="muted">${esc(e.date)}</td>
        <td><b style="color:${e.score >= 80 ? "var(--ok)" : e.score >= 60 ? "var(--warn)" : "var(--bad)"}">${e.score}</b></td>
        <td style="text-align:right"><button type="button" class="btn sm" data-aview="${e.id}">查看</button></td>
      </tr>`).join("")}
      </tbody></table>`
    : `<div class="empty small">该生还没有批改记录。</div>`}`;
  $$("[data-aview]", box).forEach(b => b.onclick = async () => {
    await flushEdits();
    const e = await api("/api/exam?id=" + b.dataset.aview);
    closeModal(); showResult({ ...e, demo: false });
  });
  $("#arch-edit").onclick = () => {
    modal("编辑学生资料", `
      <div class="field"><label>姓名</label><input type="text" id="e-name" value="${esc(s.name)}" maxlength="20"></div>
      <div class="field"><label>班级 / 年级</label><input type="text" id="e-grade" value="${esc(s.grade || "")}" maxlength="20"></div>
      <div class="field"><label>备注</label><input type="text" id="e-note" value="${esc(s.note || "")}" maxlength="60"></div>`, [
      { label: "取消", onClick: () => $("#btn-archive").click() },
      { label: "保存", cls: "primary", onClick: async () => {
        const j = await post("/api/student/update", { id: s.id, name: $("#e-name").value, grade: $("#e-grade").value, note: $("#e-note").value });
        S.students = mergeStudent(j.student);
        $("#btn-archive").click(); toast("已保存");
      } }]);
  };
  $("#arch-del").onclick = () => {
    modal("删除学生", `<p>确定删除「<b>${esc(s.name)}</b>」？其全部批改与训练记录会一并删除，不可恢复。</p>`, [
      { label: "取消", onClick: () => $("#btn-archive").click() },
      { label: "确认删除", cls: "danger", onClick: async () => {
        await del("/api/student/" + s.id);
        S.students = S.students.filter(x => x.id !== s.id);
        $("#btn-archive").click(); toast("已删除");
      } }]);
  };
}

/* ================= 批改记录 ================= */
$("#btn-history").onclick = async () => {
  await flushEdits();
  const rows = [];
  for (const s of S.students) {
    const j = await api("/api/student?id=" + s.id);
    j.exams.forEach(e => rows.push({ ...e, student: s.name }));
  }
  rows.sort((a, b) => (b.date + b.id).localeCompare(a.date + a.id));
  modal("批改记录（全部学生）", rows.length ? `
    <table class="list"><thead><tr><th>学生</th><th>试卷</th><th>日期</th><th>得分</th><th></th></tr></thead><tbody>
    ${rows.map(e => `<tr>
      <td>${esc(e.student)}</td><td>${esc(e.title)}</td><td class="muted">${esc(e.date)}</td>
      <td><b style="color:${e.score >= 80 ? "var(--ok)" : e.score >= 60 ? "var(--warn)" : "var(--bad)"}">${e.score}</b></td>
      <td style="text-align:right;white-space:nowrap">
        <button type="button" class="btn sm" data-hview="${e.id}">查看</button>
        <button type="button" class="btn sm danger" data-hdel="${e.id}">删除</button>
      </td></tr>`).join("")}
    </tbody></table>` : `<div class="empty">还没有批改记录。</div>`,
    [{ label: "关闭", onClick: closeModal }]);
  $$("[data-hview]").forEach(b => b.onclick = async () => {
    const e = await api("/api/exam?id=" + b.dataset.hview);
    closeModal(); showResult({ ...e, demo: false });
  });
  $$("[data-hdel]").forEach(b => b.onclick = async () => {
    await del("/api/exam/" + b.dataset.hdel);
    closeModal(); toast("已删除"); $("#btn-history").click();
  });
};

boot().catch(e => {
  $("#upload-hint").textContent = "初始化失败：" + e.message + "（请确认通过 python app.py 启动服务）";
});