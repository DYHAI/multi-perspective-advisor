/* 多视角人生参谋 · 前端逻辑 */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  meta: { lenses: [], catalog: [], providers: {} },
  config: null,
  models: [],          // [{provider,id,label,lens}]
  aggregator: null,
  drawn: [],
  running: false,
  jobId: null,
  es: null,
  controller: null,
  cards: new Map(),
  order: [],
  finalText: "",
  reviews: [],
  startedAt: 0,
};

/* ---------------- 小工具 ---------------- */
function toast(msg, ms = 2200) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(t._t);
  t._t = setTimeout(() => (t.hidden = true), ms);
}

function lensName(key) {
  const l = state.meta.lenses.find((x) => x.key === key);
  return l ? l.name : "综合视角";
}

const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/* ---------------- Mini Markdown ---------------- */
function inline(s) {
  return s
    .replace(/\*\*\*(.+?)\*\*\*/g, "<strong><em>$1</em></strong>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

function mdRender(src) {
  const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
  let out = "";
  let i = 0;
  let listType = null;
  const closeList = () => { if (listType) { out += `</${listType}>`; listType = null; } };
  const openList = (t) => { if (listType !== t) { closeList(); out += `<${t}>`; listType = t; } };

  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line.trim())) {
      closeList();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) buf.push(lines[i++]);
      i++;
      out += `<pre><code>${esc(buf.join("\n"))}</code></pre>`;
      continue;
    }

    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      closeList();
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(line);
      i += 2;
      let body = "";
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        body += "<tr>" + cells(lines[i]).map((c) => `<td>${inline(esc(c))}</td>`).join("") + "</tr>";
        i++;
      }
      out += `<table><thead><tr>${head.map((c) => `<th>${inline(esc(c))}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table>`;
      continue;
    }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { closeList(); out += `<h${h[1].length}>${inline(esc(h[2]))}</h${h[1].length}>`; i++; continue; }

    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { closeList(); out += "<hr/>"; i++; continue; }

    if (/^\s*>\s?/.test(line)) {
      closeList();
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      out += `<blockquote>${mdRender(buf.join("\n"))}</blockquote>`;
      continue;
    }

    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    if (ul) { openList("ul"); out += `<li>${inline(esc(ul[1]))}</li>`; i++; continue; }

    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ol) { openList("ol"); out += `<li>${inline(esc(ol[1]))}</li>`; i++; continue; }

    if (!line.trim()) { closeList(); i++; continue; }

    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim() &&
           !/^(#{1,6}\s|\s*[-*+]\s|\s*\d+[.)]\s|\s*>|\s*\|)/.test(lines[i]) &&
           !/^```/.test(lines[i].trim())) {
      buf.push(lines[i++]);
    }
    closeList();
    out += `<p>${inline(esc(buf.join("\n"))).replace(/\n/g, "<br/>")}</p>`;
  }
  closeList();
  return out;
}

/* ---------------- 初始化 ---------------- */
async function boot() {
  const [meta, config] = await Promise.all([
    fetch("/api/meta").then((r) => r.json()),
    fetch("/api/config").then((r) => r.json()),
  ]);
  state.meta = meta;
  state.config = config;
  state.models = (config.models || []).map((m) => ({
    ...m,
    lenses: m.lenses || (m.lens ? [m.lens] : [...(config.default_lenses || ["rational", "empathic", "realist"])]),
  }));
  state.aggregator = config.aggregator ? { ...config.aggregator } : null;

  // 先绑定事件，保证界面可交互；再逐步初始化，任何一步失败都不影响其它部分
  bindEvents();
  for (const step of [resumeJob, drawRound, updateKeyStatus]) {
    try {
      await step();
    } catch (err) {
      console.error("[初始化]", step.name, err);
      showInitError(`${step.name}: ${err.message}`);
    }
  }
}

function showInitError(msg) {
  const el = $("#scale-info");
  if (el) {
    el.innerHTML = `<b style="color:#ffb3c4">界面初始化出现问题</b>：${esc(msg)}<br>请刷新页面（⌘R）；若仍不行，把这句话发给 AI 助手。`;
  }
}

/* ---------------- 随机抽阵容 ---------------- */
async function drawRound() {
  const count = state.config?.random_count || 12;
  const res = await fetch(`/api/draw?count=${count}`).then((r) => r.json());
  state.drawn = res.models || [];
  state.drawnSeconds = res.estimated_seconds || 20;
  renderRoundtable();
}

function activeModels() {
  return state.drawn;
}

function renderRoundtable() {
  const box = $("#model-list");
  $("#round-note").textContent = "每次运行都从模型池里随机抽 12 个模型，各配一个不同的角色 —— 你不需要挑选。";
  box.innerHTML = state.drawn.map((m, i) => {
    const lens = state.meta.lenses.find((l) => l.key === m.lenses[0]) || {};
    return `<div class="draw-row">
      <span class="chip on" title="${esc(lens.desc || "")}">${esc(lens.short || "")}·${esc(lens.name || "")}</span>
      <span class="dr-model">${esc(m.label)}<em>${esc(m.vendor || "")}${m.price ? " · " + esc(m.price) : ""}</em></span>
      <span class="dr-time">${m.speed ? m.speed + "s" : ""}</span>
    </div>`;
  }).join("");
  $("#model-count").textContent = `${state.drawn.length} 份意见 · ${new Set(state.drawn.map((m) => m.vendor)).size} 个厂商`;
  updateScale();
}

function updateKeyStatus() {
  const el = $("#key-status");
  const active = state.config?.active_provider || "siliconflow";
  const p = state.config?.providers?.[active];
  if (p && p.has_key) {
    el.className = "pill ok";
    el.textContent = "硅基流动 · 已就绪";
  } else {
    el.className = "pill warn";
    el.textContent = "未配置 API Key";
  }
}

/* 调用量 & 费用估算 */
function estimateCost() {
  const callsEstimate = { calls: 0, cost: 0, currency: "￥", mix: false };
  activeModels().forEach((m) => {
    const c = state.meta.catalog.find((x) => x.id === m.id && x.provider === m.provider);
    const n = (m.lenses || []).length;
    callsEstimate.calls += n;
    if (c && (c.cin || c.cout)) {
      if (c.provider !== "siliconflow") callsEstimate.mix = true;
      callsEstimate.cost += n * (1300 / 1e6 * (c.cin || 0) + 1100 / 1e6 * (c.cout || 0));
    }
  });
  {
    const a = state.aggregator?.id || "";
    const c = state.meta.catalog.find((x) => x.id === a) || { cin: 2, cout: 3 };
    const lensGroups = new Set();
    activeModels().forEach((m) => (m.lenses || []).forEach((l) => lensGroups.add(l)));
    if (state.config?.layered_summary && callsEstimate.calls > 16) {
      callsEstimate.calls += lensGroups.size;
      callsEstimate.cost += lensGroups.size * (7000 / 1e6 * (c.cin || 0) + 1300 / 1e6 * (c.cout || 0));
    }
    callsEstimate.calls += 1;
    callsEstimate.cost += (callsEstimate.calls * 700 / 1e6) * (c.cin || 0) + (1800 / 1e6) * (c.cout || 0);
  }
  return callsEstimate;
}

function updateScale() {
  const el = $("#scale-info");
  if (!el) return;
  const e = estimateCost();
  if (!e.calls) { el.textContent = ""; return; }
  const money = e.currency + e.cost.toFixed(3);
  const secs = `约 ${Math.round((state.drawnSeconds || 20) + 30)} 秒`;
  el.innerHTML = `将产生 <b>${e.calls}</b> 次模型调用 · 预估 <b>${secs}</b> · 预估花费 <b>${money}</b>${e.mix ? "（含美元模型）" : ""}`;
}

function dotColor(i) {
  const colors = ["#8b7bff", "#4fd1c5", "#ff7ab6", "#ffc46b", "#7fb2ff", "#b6ff7a"];
  return colors[i % colors.length];
}

/* ---------------- 舞台渲染 ---------------- */
function resetStage() {
  state.cards = new Map();
  state.order = [];
  state.finalText = "";
  state.reviews = [];
  state.jobId = null;
  if (state.es) { state.es.close(); state.es = null; }
  $("#stage").innerHTML = "";
  $("#btn-copy").disabled = true;
  $("#btn-export").disabled = true;
}

function makeCard(key, { title, lens, sub, isFinal = false, isError = false }) {
  const el = document.createElement("div");
  el.className = "mcard" + (isFinal ? " final" : "") + (isError ? " error" : "");
  el.innerHTML = `
    <div class="mc-head">
      <span class="dot" style="color:${dotColor(state.order.length)}"></span>
      <div class="mc-title"><b>${esc(title)}</b>${lens ? `<span class="lens-badge">${esc(lens)}</span>` : ""}</div>
      <span class="status ${isFinal ? "run" : "run"}">${esc(sub || "排队中")}</span>
    </div>
    <div class="mc-body"><div class="md"></div></div>
    <div class="mc-foot"><span class="chars"></span><span class="tm"></span></div>`;
  const card = {
    el,
    body: el.querySelector(".md"),
    status: el.querySelector(".status"),
    charsEl: el.querySelector(".chars"),
    tmEl: el.querySelector(".tm"),
    text: "",
    dirty: false,
    started: performance.now(),
  };
  state.cards.set(key, card);
  state.order.push(key);
  return card;
}

const paint = (key) => {
  const c = state.cards.get(key);
  if (!c || !c.dirty) return;
  c.dirty = false;
  c.body.innerHTML = mdRender(c.text) + (c.running ? '<span class="cursor"></span>' : "");
  c.charsEl.textContent = c.text.length ? `${c.text.length} 字` : "";
  c.tmEl.textContent = c.ended ? `${((c.ended - c.started) / 1000).toFixed(1)}s` : "";
};

let paintQueued = false;
function queuePaint() {
  if (paintQueued) return;
  paintQueued = true;
  requestAnimationFrame(() => {
    paintQueued = false;
    state.cards.forEach((_, k) => paint(k));
  });
}

/* ---------------- 运行 ---------------- */
function collectParts() {
  return {
    encounter: $("#in-encounter").value,
    problem: $("#in-problem").value,
    expectation: $("#in-expectation").value,
  };
}

async function run() {
  const parts = collectParts();
  if (!parts.encounter.trim() && !parts.problem.trim() && !parts.expectation.trim()) {
    toast("至少填一项（遭遇 / 问题 / 期待）");
    return;
  }
  if (activeModels().length < 2) {
    toast("阵容为空，点「换一批」重新抽取");
  }
  const active = state.config?.active_provider || "siliconflow";
  if (!state.config?.providers?.[active]?.has_key) {
    toast("没有读到 API Key，请检查 data/config.json");
    return;
  }

  resetStage();
  state.running = true;
  state.startedAt = performance.now();
  state.controller = new AbortController();
  $("#btn-run").disabled = true;
  $("#btn-stop").hidden = false;
  $("#progress").hidden = false;
  $("#stage-sub").textContent = "已提交，正在分配任务……";

  // 汇总模型固定用配置里的（默认 Qwen3-30B-A3B，实测最快）
  const aggregator = state.config?.aggregator || {
    provider: "siliconflow", id: "Qwen/Qwen3-30B-A3B-Instruct-2507", label: "Qwen3-30B-A3B",
  };

  const payload = {
    parts,
    models: activeModels(),
    aggregator,
    deep: $("#opt-deep").checked,
    layered: state.config?.layered_summary !== false,
    max_concurrency: state.config?.max_concurrency || 6,
    language: state.config?.language || "中文",
    temperature: state.config?.temperature ?? 0.85,
    suggestions: parseInt($("#opt-suggestions").value, 10) || 4,
  };

  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: state.controller.signal,
    }).then((r) => r.json());
    if (!res.job_id) throw new Error(res.error || "提交失败");
    // 记住任务号：手机锁屏、切走、甚至关掉页面，回来都还能接着看
    state.jobId = res.job_id;
    localStorage.setItem("mpa_job", res.job_id);
    $("#stage-sub").textContent = "圆桌进行中……每个模型独立作答，完成后由总分析师收敛。";
    openStream(res.job_id);
  } catch (err) {
    if (err.name !== "AbortError") toast("出错了：" + err.message, 4000);
  } finally {
    if (!state.jobId) finishRunUI();
  }
}

function finishRunUI() {
  state.running = false;
  $("#btn-run").disabled = false;
  $("#btn-stop").hidden = true;
  state.cards.forEach((c, k) => {
    c.running = false;
    c.ended = c.ended || performance.now();
    c.dirty = true;
    paint(k);
  });
  updateProgress();
}

/* 用 EventSource 订阅任务：断线它会自己重连，重连时服务端会先发一份现场快照，
   所以手机锁屏几分钟再回来，内容会自动补齐。 */
function openStream(jobId) {
  if (state.es) state.es.close();
  const es = new EventSource(`/api/job/${jobId}/stream`);
  state.es = es;
  es.onmessage = (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch { return; }
    if (ev.event === "snapshot") return applySnapshot(ev);
    handleEvent(ev);
  };
  es.onerror = () => {
    if (state.running) {
      $("#stage-sub").textContent = "连接中断，正在自动重连……（后台仍在继续分析）";
    }
  };
}

/* 用服务端的现场重建整个界面（重连 / 刷新页面 / 换设备都用它） */
function applySnapshot(snap) {
  const stage = $("#stage");
  stage.innerHTML = "";
  state.cards = new Map();
  state.order = [];
  (snap.cards || []).forEach((c) => {
    const isFinal = c.scope === "final";
    const isMerge = c.scope === "lens";
    const card = makeCard(c.key, {
      title: isFinal ? `最终分析 · ${c.label}` : c.label,
      lens: isFinal ? "总分析师" : (isMerge ? "视角综述" : c.lens_name),
      sub: c.status === "done" ? "已作答" : c.status === "error" ? "失败" : "生成中…",
      isFinal,
    });
    card.text = c.text || "";
    card.dirty = true;
    if (c.status === "done") {
      card.running = false; card.ended = performance.now();
      card.status.className = "status done";
      card.status.textContent = isFinal ? "完成" : "已作答";
    } else if (c.status === "error") {
      card.running = false; card.ended = performance.now();
      card.el.classList.add("error");
      card.status.className = "status err";
      card.status.textContent = "失败";
      if (c.error) card.body.innerHTML = `<p>${esc(c.error)}</p>`;
    } else {
      card.running = true;
    }
    if (isFinal) stage.prepend(card.el);
    else if (isMerge) stage.insertBefore(card.el, stage.querySelector(".mcard.final") || null);
    else stage.appendChild(card.el);
    paint(c.key);
  });
  if (snap.state?.total) {
    $("#stage-sub").textContent = snap.state.status === "done"
      ? "分析已完成"
      : `共 ${snap.state.total} 份独立意见（模型 × 视角）正在生成……`;
  }
  updateProgress();
  if (snap.state?.status && snap.state.status !== "running") finishRunUI();
}

/* 页面加载时看看有没有没跑完的任务 */
async function resumeJob() {
  const jobId = localStorage.getItem("mpa_job");
  if (!jobId) return;
  try {
    const snap = await fetch(`/api/job/${jobId}`).then((r) => r.json());
    if (snap.error) { localStorage.removeItem("mpa_job"); return; }
    state.jobId = jobId;
    state.running = snap.state?.status === "running";
    if (snap.parts) {
      $("#in-encounter").value = snap.parts.encounter || "";
      $("#in-problem").value = snap.parts.problem || "";
      $("#in-expectation").value = snap.parts.expectation || "";
      $$("textarea").forEach((t) => t.dispatchEvent(new Event("input")));
    }
    applySnapshot(snap);
    if (state.running) {
      $("#btn-run").disabled = true;
      $("#btn-stop").hidden = false;
      $("#progress").hidden = false;
      toast("已恢复上次未完成的分析");
      openStream(jobId);
    } else {
      $("#btn-copy").disabled = false;
      $("#btn-export").disabled = false;
      localStorage.removeItem("mpa_job");
    }
  } catch { /* 忽略 */ }
}

function handleEvent(ev) {
  const stage = $("#stage");
  switch (ev.event) {
    case "job_start":
      stage.innerHTML = "";
      if (ev.total) {
        $("#stage-sub").textContent = `共 ${ev.total} 份独立意见（模型 × 视角）正在并行生成……`;
      }
      break;
    case "model_start": {
      const isFinal = ev.scope === "final";
      const isCross = ev.phase === "round2";
      const isMerge = ev.scope === "lens";
      const card = makeCard(ev.key, {
        title: isFinal ? `最终分析 · ${ev.label}` : ev.label,
        lens: isFinal ? "总分析师" : (isMerge ? "视角综述" : ev.lens_name + (isCross ? " · 第二轮" : "")),
        sub: "生成中…",
        isFinal,
      });
      if (isFinal) {
        card.body.innerHTML = '<p class="mini-note">正在汇总所有视角……</p>';
        stage.prepend(card.el);
      } else if (isMerge) {
        card.el.classList.add("merge");
        stage.insertBefore(card.el, stage.querySelector(".mcard.final") || null);
      } else {
        stage.appendChild(card.el);
      }
      card.running = true;
      break;
    }
    case "delta": {
      const card = state.cards.get(ev.key);
      if (!card) break;
      if (card.text === "" && card.body.querySelector(".mini-note")) {
        card.body.innerHTML = "";
        card.status.textContent = "生成中…";
      }
      card.text += ev.text;
      card.dirty = true;
      queuePaint();
      break;
    }
    case "thinking": {
      // 推理模型（DeepSeek V3.2 / R1 等）思考阶段没有可见输出，这里给出进度反馈
      const card = state.cards.get(ev.key);
      if (!card || card.text) break;
      const n = Math.max(1, Math.round((ev.chars || 0) / 2));
      card.status.textContent = `思考中…（约 ${n} 字推理）`;
      if (!card.body.querySelector(".mini-note")) {
        card.body.innerHTML = '<p class="mini-note">模型正在内部推理，稍后一次性输出正文……</p>';
      }
      break;
    }
    case "model_done": {
      const card = state.cards.get(ev.key);
      if (!card) break;
      card.running = false;
      card.ended = performance.now();
      card.status.className = "status done";
      card.status.textContent = ev.scope === "final" ? "完成" : "已作答";
      card.dirty = true;
      paint(ev.key);
      updateProgress();
      break;
    }
    case "model_error": {
      const card = state.cards.get(ev.key);
      if (!card) break;
      card.running = false;
      card.ended = performance.now();
      card.el.classList.add("error");
      card.status.className = "status err";
      card.status.textContent = "失败";
      card.body.innerHTML = `<p>${esc(ev.message || "调用失败")}</p>`;
      card.charsEl.textContent = "";
      paint_skip(ev.key);
      updateProgress();
      break;
    }
    case "fatal":
      toast(ev.message || "任务失败", 6000);
      break;
    case "done":
      {
        let extra = "";
        if (ev.usage && ev.usage.prompt_tokens) {
          const t = (ev.usage.prompt_tokens + ev.usage.completion_tokens).toLocaleString();
          extra = ` · 实际花费约 ￥${Number(ev.cost || 0).toFixed(3)}（${t} tokens）`;
        }
        $("#stage-sub").textContent = `完成 · 共 ${state.cards.size} 张卡片${extra}`;
        localStorage.removeItem("mpa_job");
        if (state.es) { state.es.close(); state.es = null; }
        finishRunUI();
      }
      $("#btn-copy").disabled = false;
      $("#btn-export").disabled = false;
      break;
    default:
      break;
  }
}

function paint_skip(key) {
  const c = state.cards.get(key);
  if (!c) return;
  c.charsEl.textContent = "";
  c.tmEl.textContent = `${((c.ended - c.started) / 1000).toFixed(1)}s`;
}

function updateProgress() {
  const total = state.cards.size;
  const done = Array.from(state.cards.values()).filter((c) => !c.running).length;
  $("#progress-text").textContent = `${done} / ${total}`;
  const bar = $("#progress .bar i");
  if (bar) bar.style.width = total ? `${(done / total) * 100}%` : "0%";
}

/* ---------------- 导出 ---------------- */
function buildMarkdown() {
  const parts = collectParts();
  const lines = [
    `# 多视角圆桌分析`,
    ``,
    `> 生成时间：${new Date().toLocaleString("zh-CN")}`,
    ``,
    `## 我提供的信息`,
    ``,
    `**遭遇（过往与当下）**`,
    parts.encounter.trim() || "（未填写）",
    ``,
    `**问题（想要解决）**`,
    parts.problem.trim() || "（未填写）",
    ``,
    `**未来的期待**`,
    parts.expectation.trim() || "（未填写）",
    ``,
    `---`,
    ``,
    `## 各模型独立意见`,
    ``,
  ];
  state.order.forEach((k) => {
    const c = state.cards.get(k);
    if (!c || !c.text.trim()) return;
    const head = c.el.querySelector(".mc-title b")?.textContent || k;
    const lens = c.el.querySelector(".lens-badge")?.textContent || "";
    lines.push(`### ${head}${lens ? ` · ${lens}` : ""}`, ``, c.text.trim(), ``);
  });
  const finalCard = state.cards.get("final");
  if (finalCard && finalCard.text.trim()) {
    lines.push(`---`, ``, `## 最终分析与意见`, ``, finalCard.text.trim(), ``);
  }
  return lines.join("\n");
}

function copyAll() {
  const text = buildMarkdown();
  navigator.clipboard.writeText(text).then(
    () => toast("已复制全文到剪贴板"),
    () => toast("复制失败，请手动选择文本")
  );
}

function exportMd() {
  const blob = new Blob([buildMarkdown()], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `多视角分析-${new Date().toISOString().slice(0, 10)}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
  toast("已导出 Markdown");
}

/* ---------------- 事件绑定 ---------------- */
function bindEvents() {
  $$("textarea").forEach((t) => {
    const field = t.closest(".field");
    const counter = field.querySelector(".counter");
    const flag = field.querySelector(".fill-flag");
    const sync = () => {
      counter.textContent = `${t.value.length} 字`;
      flag.textContent = t.value.trim() ? "已填写 ✓" : "（可留空）";
    };
    t.addEventListener("input", sync);
    sync();
  });

  $("#btn-run").addEventListener("click", run);
  $("#btn-stop").addEventListener("click", () => {
    // 通知服务端停下这个任务，并断开本页面的订阅
    if (state.jobId) fetch(`/api/job/${state.jobId}/stop`, { method: "POST" }).catch(() => {});
    if (state.es) { state.es.close(); state.es = null; }
    localStorage.removeItem("mpa_job");
    state.running = false;
    toast("已停止");
    $("#btn-stop").hidden = true;
    $("#btn-run").disabled = false;
  });
  $("#btn-redraw").addEventListener("click", async () => {
    await drawRound();
    toast("已换一批阵容");
  });

  $("#btn-copy").addEventListener("click", copyAll);
  $("#btn-export").addEventListener("click", exportMd);

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") run();
  });
}

boot().catch((e) => toast("初始化失败：" + e.message, 6000));

// 注册 Service Worker：让「添加到主屏幕」后能全屏打开（https 或 localhost 下才生效）
if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1")) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

// 兜底：任何未捕获的错误都显示出来，避免页面看起来"卡住了"
window.addEventListener("error", (e) => {
  const el = $("#scale-info");
  if (el && !el.textContent) {
    el.innerHTML = `<b style="color:#ffb3c4">出错了</b>：${esc(String(e.message))}（刷新页面可恢复）`;
  }
});
