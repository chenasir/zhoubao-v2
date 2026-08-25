// CICC 周报 Agent 前端逻辑
const $ = (id) => document.getElementById(id);
const spinner = $("spinner");
const statusBar = $("statusBar");
const busyOverlay = $("busyOverlay");
const busyTitle = $("busyTitle");
const busyText = $("busyText");
const busyBar = $("busyBar");
const busyStage = $("busyStage");
const busyPercent = $("busyPercent");

const STORAGE_KEYS = {
  runtime: "zhoubao.runtimeConfig.v1",
  candidates: "zhoubao.candidates.v1",
  sortMode: "zhoubao.sortMode.v1",
};

const DEFAULT_RUNTIME_CONFIG = {
  appAccessToken: "",
  openrouterApiKey: "",
  openrouterBaseUrl: "https://openrouter.ai/api/v1",
  llmModelPreset: "deepseek/deepseek-v4-pro",
  llmModelCustom: "",
  llmTimeout: 90,
  llmConcurrency: 3,
};

let COUNTRIES = [];
let CANDIDATES = [];
let COLLAPSED = {};
let VIEW_MODE = {};
let SORT_MODE = localStorage.getItem(STORAGE_KEYS.sortMode) || "source";
let BUSY_STATE = null;
let LAST_DOWNLOAD_URL = "";
let STATUS_FILTER = "all";
let DRAGGING_ID = null;
let SERVER_LLM_CONFIGURED = false;

const BUSY_PRESETS = {
  fetch: {
    title: "正在抓取近 7 天新闻",
    text: "系统正在按配置信源拉取，并执行四层筛选策略。",
    estimatedMs: 90000,
    stages: [
      { pct: 8, text: "正在连接后端服务…" },
      { pct: 28, text: "正在遍历各国家与信源抓取新闻…" },
      { pct: 55, text: "正在做标题去重、硬排除与国家分流…" },
      { pct: 82, text: "正在生成 FINAL / RESERVE / HOLD 清单…" },
    ],
  },
  score: {
    title: "正在进行 LLM 打分排序",
    text: "AI 模型正在逐批评估新闻相关性，请稍候。",
    estimatedMs: 70000,
    stages: [
      { pct: 10, text: "正在准备候选新闻…" },
      { pct: 35, text: "正在调用 AI 模型批量打分…" },
      { pct: 68, text: "正在写回评分结果…" },
      { pct: 88, text: "正在按 AI 评分刷新排序…" },
    ],
  },
  generate: {
    title: "正在生成双语 docx",
    text: "系统正在调用 LLM 重写并套用 Word 模板，请耐心等待。",
    estimatedMs: 0,
    stages: [
      { pct: 8, text: "正在整理你勾选的新闻…" },
      { pct: 30, text: "正在调用 AI 模型生成中英双语稿件…" },
      { pct: 72, text: "正在写入中英模板文档…" },
      { pct: 92, text: "后台任务仍在处理，完成后会自动下载…" },
    ],
  },
  clear: {
    title: "正在清空候选",
    text: "系统正在清空当前浏览器中保存的候选新闻。",
    estimatedMs: 2000,
    stages: [
      { pct: 35, text: "正在清空本地候选…" },
      { pct: 85, text: "正在刷新页面列表…" },
    ],
  },
  manual: {
    title: "正在抓取手动 URL",
    text: "系统正在解析标题与正文，并添加到候选列表。",
    estimatedMs: 18000,
    stages: [
      { pct: 15, text: "正在请求网页…" },
      { pct: 55, text: "正在抽取标题与正文…" },
      { pct: 88, text: "正在刷新候选列表…" },
    ],
  },
};

function setStatus(msg) { statusBar.textContent = msg || ""; }
function showSpinner(v) { spinner.classList.toggle("hidden", !v); }

function apiHeaders(json = false) {
  const headers = {};
  if (json) headers["Content-Type"] = "application/json";
  const token = $("appAccessToken")?.value?.trim();
  if (token) headers["X-App-Token"] = token;
  return headers;
}

async function apiErrorMessage(response) {
  const text = await response.text();
  try {
    const data = JSON.parse(text);
    return data.detail || data.message || text || response.statusText;
  } catch {
    return text || response.statusText;
  }
}

async function apiPost(path, body) {
  showSpinner(true);
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: apiHeaders(true),
      body: body ? JSON.stringify(body) : null,
    });
    if (!r.ok) throw new Error(await apiErrorMessage(r));
    return await r.json();
  } finally {
    showSpinner(false);
  }
}

async function apiPostBlob(path, body) {
  showSpinner(true);
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: apiHeaders(true),
      body: body ? JSON.stringify(body) : null,
    });
    if (!r.ok) throw new Error(await apiErrorMessage(r));
    return {
      blob: await r.blob(),
      contentDisposition: r.headers.get("content-disposition") || "",
      selectedCount: r.headers.get("x-zhoubao-selected-count") || "",
      dedupedCount: r.headers.get("x-zhoubao-deduped-count") || "",
      formattedCount: r.headers.get("x-zhoubao-formatted-count") || "",
      failedCount: r.headers.get("x-zhoubao-failed-count") || "",
      warningCount: r.headers.get("x-zhoubao-warning-count") || "",
    };
  } finally {
    showSpinner(false);
  }
}

async function apiGetBlob(path) {
  showSpinner(true);
  try {
    const r = await fetch(path, { headers: apiHeaders() });
    if (!r.ok) throw new Error(await apiErrorMessage(r));
    return {
      blob: await r.blob(),
      contentDisposition: r.headers.get("content-disposition") || "",
      selectedCount: r.headers.get("x-zhoubao-selected-count") || "",
      dedupedCount: r.headers.get("x-zhoubao-deduped-count") || "",
      formattedCount: r.headers.get("x-zhoubao-formatted-count") || "",
      failedCount: r.headers.get("x-zhoubao-failed-count") || "",
      warningCount: r.headers.get("x-zhoubao-warning-count") || "",
    };
  } finally {
    showSpinner(false);
  }
}

async function apiGet(path) {
  showSpinner(true);
  try {
    const r = await fetch(path, { headers: apiHeaders() });
    if (!r.ok) throw new Error(await apiErrorMessage(r));
    return await r.json();
  } finally {
    showSpinner(false);
  }
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function scoreBadgeColor(s) {
  if (s >= 8) return "bg-emerald-100 text-emerald-700";
  if (s >= 5) return "bg-amber-100 text-amber-700";
  if (s >= 3) return "bg-slate-200 text-slate-700";
  return "bg-red-100 text-red-700";
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function setBusyProgress(pct) {
  const safe = Math.max(0, Math.min(100, Math.round(pct)));
  busyBar.style.width = `${safe}%`;
  busyPercent.textContent = `${safe}%`;
}

function setBusyStage(text) {
  busyStage.textContent = text || "";
}

function toggleControlsDisabled(disabled) {
  document.querySelectorAll("button, input, select, textarea").forEach((el) => {
    if (el.closest("#busyOverlay")) return;
    el.disabled = disabled;
  });
  document.body.classList.toggle("busy-mode", disabled);
}

function startBusy(preset) {
  if (BUSY_STATE?.timer) clearInterval(BUSY_STATE.timer);
  BUSY_STATE = {
    preset,
    startedAt: Date.now(),
    progress: 3,
    timer: null,
  };
  busyTitle.textContent = preset.title;
  busyText.textContent = preset.text;
  busyOverlay.classList.remove("hidden");
  busyOverlay.classList.add("flex");
  setBusyStage(preset.stages?.[0]?.text || "处理中…");
  setBusyProgress(3);
  toggleControlsDisabled(true);

  if (preset.estimatedMs > 0) {
    BUSY_STATE.timer = setInterval(() => {
      if (!BUSY_STATE) return;
      const elapsed = Date.now() - BUSY_STATE.startedAt;
      const target = Math.min(93, 3 + (elapsed / Math.max(1000, preset.estimatedMs)) * 90);
      BUSY_STATE.progress = Math.max(BUSY_STATE.progress, target);
      const currentStage =
        [...(preset.stages || [])]
          .reverse()
          .find((stage) => BUSY_STATE.progress >= stage.pct) || preset.stages?.[0];
      if (currentStage) setBusyStage(currentStage.text);
      setBusyProgress(BUSY_STATE.progress);
    }, 300);
  }
}

async function finishBusy(finalText) {
  if (!BUSY_STATE) return;
  if (BUSY_STATE.timer) clearInterval(BUSY_STATE.timer);
  if (finalText) setBusyStage(finalText);
  setBusyProgress(100);
  await sleep(250);
  busyOverlay.classList.add("hidden");
  busyOverlay.classList.remove("flex");
  toggleControlsDisabled(false);
  BUSY_STATE = null;
}

async function withBusy(key, task) {
  const preset = BUSY_PRESETS[key];
  if (!preset) return await task();
  startBusy(preset);
  try {
    const result = await task();
    await finishBusy("已完成");
    return result;
  } catch (err) {
    await finishBusy("操作失败");
    throw err;
  }
}

function countryOrder(code) {
  const idx = COUNTRIES.findIndex((c) => c.code === code);
  return idx >= 0 ? idx : 999;
}

function publishedTs(value) {
  if (!value) return 0;
  const ts = Date.parse(value);
  return Number.isNaN(ts) ? 0 : ts;
}

function stableCandidateId(it) {
  const seed = `${it.country_code || ""}|${it.source || ""}|${it.url || ""}|${it.title || ""}`;
  let hash = 0;
  for (let i = 0; i < seed.length; i += 1) {
    hash = ((hash << 5) - hash + seed.charCodeAt(i)) | 0;
  }
  return `cand_${Math.abs(hash)}`;
}

function normalizeCandidate(it, index = 0) {
  return {
    ...it,
    id: it.id ?? stableCandidateId(it),
    score: Number(it.score || 0),
    score_reason: it.score_reason || "",
    summary: it.summary || "",
    title_zh: it.title_zh || "",
    source_original: it.source_original || "",
    source_carrier: it.source_carrier || "",
    source_homepage: it.source_homepage || "",
    source_article_url: it.source_article_url || "",
    google_news_url: it.google_news_url || "",
    fetched_body: it.fetched_body || "",
    raw_lang: it.raw_lang || "en",
    selected: Boolean(it.selected),
    is_manual: Boolean(it.is_manual),
    manual_order: it.manual_order ?? null,
    original_country_code: it.original_country_code || it.country_code || "",
    route_country: it.route_country || it.country_code || "",
    status: it.status || "",
    gate_result: it.gate_result || "",
    final_bucket: it.final_bucket || "",
    reserve_reason: it.reserve_reason || "",
    topic_cluster: it.topic_cluster || "",
    watchlist_hit: Boolean(it.watchlist_hit),
    watchlist_country: it.watchlist_country || "",
    watchlist_company: it.watchlist_company || "",
    china_hk_linkage: Boolean(it.china_hk_linkage),
    hardness_score: Number(it.hardness_score || 0),
    scale_score: Number(it.scale_score || 0),
    specificity_score: Number(it.specificity_score || 0),
    layer_reasons: Array.isArray(it.layer_reasons) ? it.layer_reasons : [],
    _defaultIndex: index,
  };
}

function serializeCandidates() {
  return CANDIDATES.map(({ _defaultIndex, ...rest }) => ({ ...rest }));
}

function serializeSelectedCandidates() {
  const selected = CANDIDATES.filter((x) => x.selected).map((x) => {
    const { _defaultIndex, ...rest } = x;
    return { ...rest, selected: true };
  });
  selected.sort((a, b) => compareCandidates(a, b));
  return selected;
}

function persistCandidates() {
  localStorage.setItem(STORAGE_KEYS.candidates, JSON.stringify(serializeCandidates()));
  localStorage.setItem(STORAGE_KEYS.sortMode, SORT_MODE);
}

function loadCandidatesFromStorage() {
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.candidates);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map((it, i) => normalizeCandidate(it, i));
  } catch {
    return [];
  }
}

function setCandidates(rows, { preserveSelected = false } = {}) {
  const prevSelected = preserveSelected ? new Set(CANDIDATES.filter((x) => x.selected).map((x) => x.id)) : new Set();
  CANDIDATES = (rows || []).map((it, i) => normalizeCandidate(it, i));
  if (preserveSelected) {
    CANDIDATES.forEach((it) => {
      it.selected = it.selected || prevSelected.has(it.id);
    });
  }
  sortCandidatesInPlace();
  persistCandidates();
  renderCandidates();
}

function loadRuntimeConfig() {
  try {
    return { ...DEFAULT_RUNTIME_CONFIG, ...(JSON.parse(localStorage.getItem(STORAGE_KEYS.runtime) || "{}")) };
  } catch {
    return { ...DEFAULT_RUNTIME_CONFIG };
  }
}

function saveRuntimeConfig() {
  const cfg = {
    appAccessToken: $("appAccessToken").value.trim(),
    openrouterApiKey: $("openrouterApiKey").value.trim(),
    openrouterBaseUrl: $("openrouterBaseUrl").value.trim() || DEFAULT_RUNTIME_CONFIG.openrouterBaseUrl,
    llmModelPreset: $("llmModelPreset").value,
    llmModelCustom: $("llmModelCustom").value.trim(),
    llmTimeout: DEFAULT_RUNTIME_CONFIG.llmTimeout,
    llmConcurrency: Math.max(1, Math.min(8, Number($("llmConcurrency").value || DEFAULT_RUNTIME_CONFIG.llmConcurrency))),
  };
  localStorage.setItem(STORAGE_KEYS.runtime, JSON.stringify(cfg));
}

function getEffectiveModel() {
  const preset = $("llmModelPreset").value;
  if (preset === "custom") return $("llmModelCustom").value.trim();
  return preset;
}

function getRuntimeConfigForApi() {
  saveRuntimeConfig();
  const model = getEffectiveModel();
  const runtime = {
    openrouter_api_key: $("openrouterApiKey").value.trim(),
    openrouter_base_url: $("openrouterBaseUrl").value.trim() || DEFAULT_RUNTIME_CONFIG.openrouterBaseUrl,
    llm_model: model,
    llm_timeout: DEFAULT_RUNTIME_CONFIG.llmTimeout,
    llm_concurrency: Math.max(1, Math.min(8, Number($("llmConcurrency").value || DEFAULT_RUNTIME_CONFIG.llmConcurrency))),
  };
  // 前端不再强制要求填 key，后端会用 .env 里的默认值兜底
  if (!model) {
    throw new Error("请先选择或填写一个模型名");
  }
  return runtime;
}

function ensureLlmConfigured() {
  if ($("openrouterApiKey").value.trim() || SERVER_LLM_CONFIGURED) return true;
  const panel = $("runtimeConfigPanel");
  panel.open = true;
  panel.scrollIntoView({ behavior: "smooth", block: "center" });
  $("openrouterApiKey").focus({ preventScroll: true });
  alert("尚未配置 OpenRouter API Key。请在已展开的“运行配置”中填写后再生成；也可以在 Vercel 环境变量中设置 OPENROUTER_API_KEY 后重新部署。");
  return false;
}

function applyRuntimeConfigToInputs() {
  const cfg = loadRuntimeConfig();
  $("openrouterApiKey").value = cfg.openrouterApiKey || "";
  $("appAccessToken").value = cfg.appAccessToken || "";
  $("openrouterBaseUrl").value = cfg.openrouterBaseUrl || DEFAULT_RUNTIME_CONFIG.openrouterBaseUrl;
  $("llmConcurrency").value = cfg.llmConcurrency || DEFAULT_RUNTIME_CONFIG.llmConcurrency;
  $("llmModelCustom").value = cfg.llmModelCustom || "";

  const presetOptions = new Set(Array.from($("llmModelPreset").options).map((opt) => opt.value));
  const preset = presetOptions.has(cfg.llmModelPreset) ? cfg.llmModelPreset : (presetOptions.has(cfg.llmModelCustom) ? cfg.llmModelCustom : "custom");
  $("llmModelPreset").value = preset;
  if (preset !== "custom" && !cfg.llmModelCustom) {
    $("llmModelCustom").value = preset;
  }
  syncModelInputs();
}

function syncModelInputs() {
  const preset = $("llmModelPreset").value;
  const isCustom = preset === "custom";
  $("llmModelCustom").disabled = !isCustom;
  $("llmModelCustom").classList.toggle("bg-slate-50", !isCustom);
  if (!isCustom) {
    $("llmModelCustom").value = preset;
  }
  saveRuntimeConfig();
}

function compareCandidates(a, b) {
  const countryDiff = countryOrder(a.country_code) - countryOrder(b.country_code);
  if (countryDiff) return countryDiff;

  if (SORT_MODE === "manual") {
    const orderDiff = Number(a.manual_order ?? 9999) - Number(b.manual_order ?? 9999);
    if (orderDiff) return orderDiff;
  }

  const manualDiff = Number(Boolean(b.is_manual)) - Number(Boolean(a.is_manual));
  if (manualDiff) return manualDiff;

  const statusDiff = statusRank(a) - statusRank(b);
  if (statusDiff) return statusDiff;

  if (SORT_MODE === "score") {
    const scoreDiff = Number(b.score || 0) - Number(a.score || 0);
    if (scoreDiff) return scoreDiff;

    const llmDiff = Number(isLlmScored(b)) - Number(isLlmScored(a));
    if (llmDiff) return llmDiff;

    const dateDiff = publishedTs(b.published_at) - publishedTs(a.published_at);
    if (dateDiff) return dateDiff;

    return (a.source || "").localeCompare(b.source || "");
  }

  const scoreDiff = Number(b.score || 0) - Number(a.score || 0);
  if (scoreDiff) return scoreDiff;
  return (a._defaultIndex || 0) - (b._defaultIndex || 0);
}

function sortCandidatesInPlace() {
  CANDIDATES.sort(compareCandidates);
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
}

function isLlmScored(it) {
  return (it.score_reason || "").includes(" | ");
}

function statusRank(it) {
  const status = it.status || "";
  if (it.is_manual) return 0;
  if (status.startsWith("FINAL")) return 1;
  if (status === "RESERVE") return 2;
  if (status.startsWith("HOLD")) return 3;
  return 4;
}

function matchesStatusFilter(it) {
  const status = it.status || "";
  if (STATUS_FILTER === "all") return true;
  if (STATUS_FILTER === "final") return status.startsWith("FINAL");
  if (STATUS_FILTER === "reserve") return status === "RESERVE";
  if (STATUS_FILTER === "hold") return status.startsWith("HOLD");
  if (STATUS_FILTER === "manual") return Boolean(it.is_manual);
  return true;
}

function updateStatusFilterButtons() {
  document.querySelectorAll(".status-filter").forEach((btn) => {
    const active = btn.dataset.statusFilter === STATUS_FILTER;
    btn.classList.toggle("bg-[#17345f]", active);
    btn.classList.toggle("text-white", active);
    btn.classList.toggle("text-slate-600", !active);
    btn.classList.toggle("hover:bg-slate-50", !active);
  });
}

function statusBadge(it) {
  const status = it.status || "";
  if (!status) return "";
  let color = "bg-slate-100 text-slate-600";
  if (status.startsWith("FINAL")) color = "bg-blue-100 text-blue-700";
  else if (status === "RESERVE") color = "bg-purple-100 text-purple-700";
  else if (status.startsWith("HOLD")) color = "bg-orange-100 text-orange-700";
  else if (status.startsWith("DROP")) color = "bg-red-100 text-red-700";
  return `<span class="text-[10px] ${color} rounded-full px-2 py-0.5 font-mono">${escapeHtml(status)}</span>`;
}

function cardHtml(it) {
  const score = Number(it.score || 0).toFixed(1);
  const color = scoreBadgeColor(Number(it.score || 0));
  const date = it.published_at ? new Date(it.published_at).toISOString().slice(0, 10) : "—";
  const host = hostOf(it.url);
  const llm = isLlmScored(it);
  const scoreLabel = llm ? "AI" : "规则";
  const scoreTip = llm
    ? "AI 按新策略辅助评估的排序分（0-10）"
    : "四层筛选策略给出的排序分（0-10）：硬度、规模、具体性。点击「② LLM 打分排序」可让 AI 辅助复核";
  const watch = it.watchlist_hit
    ? `<span class="text-[10px] bg-pink-100 text-pink-700 rounded-full px-2 py-0.5" title="${escapeHtml(it.watchlist_company)}">watchlist: ${escapeHtml(it.watchlist_country)}</span>`
    : "";
  const topic = it.topic_cluster ? `<span class="text-[10px] bg-slate-100 text-slate-600 rounded-full px-2 py-0.5">${escapeHtml(it.topic_cluster)}</span>` : "";
  const route = it.route_country ? `<span class="text-[10px] bg-cyan-100 text-cyan-700 rounded-full px-2 py-0.5">route: ${escapeHtml(it.route_country)}</span>` : "";
  const manual = it.is_manual ? `<span class="text-[10px] bg-indigo-100 text-indigo-700 rounded-full px-2 py-0.5">manual</span>` : "";
  const carrier = it.source_carrier ? `<span class="text-[10px] bg-violet-50 text-violet-700 rounded-full px-2 py-0.5">载体: ${escapeHtml(it.source_carrier)}</span>` : "";
  const layerReason = (it.layer_reasons || []).join(" / ");
  const sourceLabel = it.source || it.source_original || "";
  const discoveredByGoogle = Boolean(it.google_news_url) || host === "news.google.com";
  const sourceLinks = discoveredByGoogle
    ? `<button type="button" data-resolve-original="${escapeHtml(String(it.id))}" class="text-blue-600 hover:underline">解析并打开媒体原文</button>
       ${it.source_homepage ? `<a href="${escapeHtml(it.source_homepage)}" target="_blank" rel="noopener" class="text-slate-500 hover:underline">媒体首页</a>` : ""}
       <a href="${escapeHtml(it.google_news_url || it.url)}" target="_blank" rel="noopener" class="text-slate-400 hover:underline">Google News 发现页</a>`
    : it.source_article_url
      ? `<a href="${escapeHtml(it.source_article_url)}" target="_blank" rel="noopener" class="text-blue-600 hover:underline">发布方原文</a>
         <a href="${escapeHtml(it.url)}" target="_blank" rel="noopener" class="text-slate-400 hover:underline">${escapeHtml(it.source_carrier || "转载")}页面</a>`
      : it.source_carrier
        ? `<a href="${escapeHtml(it.url)}" target="_blank" rel="noopener" class="text-blue-600 hover:underline">${escapeHtml(it.source_carrier)}页面</a>
           ${it.source_homepage ? `<a href="${escapeHtml(it.source_homepage)}" target="_blank" rel="noopener" class="text-slate-500 hover:underline">原始媒体首页</a>` : ""}`
        : `<a href="${escapeHtml(it.url)}" target="_blank" rel="noopener" class="text-blue-600 hover:underline inline-flex items-center gap-1">原文 <span class="text-slate-400">(${escapeHtml(host)})</span></a>`;
  return `
    <div class="news-card flex gap-3 p-4 border border-slate-100 rounded-2xl bg-white/88 ${it.selected ? "selected" : ""}" draggable="true" data-id="${escapeHtml(String(it.id))}">
      <input type="checkbox" data-id="${escapeHtml(String(it.id))}" ${it.selected ? "checked" : ""} class="mt-1 h-4 w-4 flex-none cursor-pointer accent-blue-600" />
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-[11px] ${color} rounded-full px-2 py-0.5 font-mono inline-flex items-center gap-1" title="${scoreTip}">
            ${score}
            <span class="text-[9px] px-1 rounded-full ${llm ? "bg-emerald-600 text-white" : "bg-slate-400 text-white"}">${scoreLabel}</span>
          </span>
          ${statusBadge(it)}
          ${manual}
          ${carrier}
          ${route}
          ${watch}
          ${topic}
          <span class="text-[11px] text-slate-500 bg-slate-50 rounded-full px-2 py-0.5">媒体来源: ${escapeHtml(sourceLabel)}</span>
          ${discoveredByGoogle ? `<span class="text-[10px] bg-amber-50 text-amber-700 rounded-full px-2 py-0.5">发现渠道: Google News</span>` : ""}
          <span class="text-[11px] text-slate-400">${date}</span>
        </div>
        <a href="${escapeHtml(it.url)}" target="_blank" rel="noopener"
           class="block mt-2 text-[15px] leading-snug font-semibold text-slate-850 hover:text-blue-700 hover:underline">
          ${escapeHtml(it.title)}
        </a>
        ${it.title_zh ? `<div class="mt-1 text-sm leading-snug text-slate-600">${escapeHtml(it.title_zh)}</div>` : ""}
        ${it.summary ? `<p class="text-xs leading-relaxed text-slate-500 mt-2 line-clamp-2">${escapeHtml(it.summary.slice(0, 260))}</p>` : ""}
        <div class="mt-3 flex items-center gap-3 text-[11px] flex-wrap">
          ${sourceLinks}
          <button type="button" data-copy="${escapeHtml(it.url)}"
                  class="text-slate-500 hover:text-slate-700">复制链接</button>
          <button type="button" data-related="${escapeHtml(String(it.id))}"
                  class="text-slate-500 hover:text-slate-700">其他 Source</button>
          ${it.score_reason ? `<span class="text-slate-400 italic truncate max-w-[280px]" title="${escapeHtml(it.score_reason)}">reason: ${escapeHtml(it.score_reason)}</span>` : ""}
          ${layerReason ? `<span class="text-slate-400 italic truncate max-w-[360px]" title="${escapeHtml(layerReason)}">layers: ${escapeHtml(layerReason)}</span>` : ""}
        </div>
      </div>
    </div>
  `;
}

function renderCandidates() {
  const panels = $("countryPanels");
  panels.innerHTML = "";
  const empty = CANDIDATES.length === 0;
  $("emptyHint").classList.toggle("hidden", !empty);
  if (empty) return;

  const byCountry = {};
  for (const c of COUNTRIES) byCountry[c.code] = [];
  for (const it of CANDIDATES) (byCountry[it.country_code] ||= []).push(it);

  for (const c of COUNTRIES) {
    const list = byCountry[c.code] || [];
    if (COLLAPSED[c.code] === undefined) {
      COLLAPSED[c.code] = list.length >= 20;
    }
    if (VIEW_MODE[c.code] === undefined) VIEW_MODE[c.code] = "all";
    const collapsed = COLLAPSED[c.code];
    const mode = VIEW_MODE[c.code];
    const filteredList = list.filter(matchesStatusFilter);
    const visibleList = mode === "selected" ? filteredList.filter((x) => x.selected) : filteredList;

    const selCount = list.filter((x) => x.selected).length;
    const section = document.createElement("section");
    section.className = "bg-white/78 border border-white/80 rounded-3xl overflow-hidden shadow-sm";
    section.innerHTML = `
      <header class="px-5 py-4 bg-gradient-to-r from-[#17345f] to-[#23508c] text-white flex items-center justify-between select-none">
        <button data-action="toggle" data-code="${c.code}" class="flex items-center gap-2 text-left hover:opacity-80">
          <span class="text-base font-mono w-5">${collapsed ? "▶" : "▼"}</span>
          <span>
            <span class="font-semibold">${escapeHtml(c.name_en)}</span>
            <span class="text-xs opacity-80 ml-2">${escapeHtml(c.name_zh)}</span>
            <span class="text-xs ml-3 opacity-80">共 ${list.length} 条 · 已选 <span data-sel="${c.code}">${selCount}</span></span>
          </span>
        </button>
        <div class="space-x-2 text-xs flex items-center">
          <div class="bg-white/12 rounded-xl overflow-hidden flex p-0.5">
            <button data-action="mode" data-code="${c.code}" data-mode="all" class="px-2.5 py-1 rounded-lg ${mode === "all" ? "bg-white text-[#1f3a6e]" : "hover:bg-white/20"}">全部</button>
            <button data-action="mode" data-code="${c.code}" data-mode="selected" class="px-2.5 py-1 rounded-lg ${mode === "selected" ? "bg-white text-[#1f3a6e]" : "hover:bg-white/20"}">只看已选</button>
          </div>
          <button data-action="selectTop" data-code="${c.code}" class="px-2.5 py-1 bg-white/15 hover:bg-white/25 rounded-lg">选前 6 条</button>
          <button data-action="clearSel" data-code="${c.code}" class="px-2.5 py-1 bg-white/15 hover:bg-white/25 rounded-lg">全不选</button>
        </div>
      </header>
      ${
        collapsed
          ? ""
          : `<div class="max-h-[70vh] overflow-y-auto p-4 space-y-3">
               ${visibleList.map(cardHtml).join("") || `<div class="p-6 text-sm text-slate-400 text-center">${mode === "selected" ? "暂未选中任何条目" : "该国暂无候选"}</div>`}
             </div>`
      }
    `;
    panels.appendChild(section);
  }

  panels.querySelectorAll(".news-card input[type='checkbox']").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      const id = e.target.dataset.id;
      const it = CANDIDATES.find((x) => String(x.id) === String(id));
      if (it) it.selected = e.target.checked;
      const card = e.target.closest(".news-card");
      card.classList.toggle("selected", e.target.checked);
      persistCandidates();
      updateCounts();
    });
  });

  panels.querySelectorAll(".news-card").forEach((card) => {
    card.addEventListener("click", (e) => {
      if (e.target.closest("a,button,input")) return;
      const cb = card.querySelector("input[type='checkbox']");
      if (!cb) return;
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event("change"));
    });
    card.addEventListener("dragstart", (e) => {
      DRAGGING_ID = card.dataset.id;
      card.classList.add("opacity-50");
      e.dataTransfer.effectAllowed = "move";
    });
    card.addEventListener("dragend", () => {
      DRAGGING_ID = null;
      card.classList.remove("opacity-50");
    });
    card.addEventListener("dragover", (e) => {
      if (!DRAGGING_ID || DRAGGING_ID === card.dataset.id) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
    });
    card.addEventListener("drop", (e) => {
      e.preventDefault();
      if (!DRAGGING_ID || DRAGGING_ID === card.dataset.id) return;
      moveCandidateBefore(DRAGGING_ID, card.dataset.id);
    });
  });

  panels.querySelectorAll("button[data-copy]").forEach((b) => {
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      const url = b.getAttribute("data-copy");
      navigator.clipboard.writeText(url).then(() => {
        const old = b.textContent;
        b.textContent = "✓ 已复制";
        setTimeout(() => (b.textContent = old), 1200);
      });
    });
  });

  panels.querySelectorAll("button[data-related]").forEach((b) => {
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = b.getAttribute("data-related");
      const item = CANDIDATES.find((x) => String(x.id) === String(id));
      if (!item) return;
      await showRelatedSources(item);
    });
  });

  panels.querySelectorAll("button[data-resolve-original]").forEach((b) => {
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = b.getAttribute("data-resolve-original");
      const item = CANDIDATES.find((x) => String(x.id) === String(id));
      if (!item) return;
      const pendingTab = window.open("about:blank", "_blank");
      const oldText = b.textContent;
      b.disabled = true;
      b.textContent = "正在解析媒体原文…";
      try {
        const data = await apiPost("/api/resolve-url", { url: item.google_news_url || item.url });
        if (!data.resolved) throw new Error("该条链接暂时无法从 Google News 还原，请稍后重试");
        item.google_news_url = item.google_news_url || item.url;
        item.url = data.url;
        persistCandidates();
        if (pendingTab) pendingTab.location.href = data.url;
        renderCandidates();
      } catch (error) {
        pendingTab?.close();
        b.disabled = false;
        b.textContent = oldText;
        alert("解析原文失败：" + error.message);
      }
    });
  });

  panels.querySelectorAll("button[data-action]").forEach((b) => {
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      const code = b.dataset.code;
      const action = b.dataset.action;
      if (action === "toggle") {
        COLLAPSED[code] = !COLLAPSED[code];
      } else if (action === "mode") {
        VIEW_MODE[code] = b.dataset.mode;
        COLLAPSED[code] = false;
      } else if (action === "selectTop") {
        const list = CANDIDATES.filter((x) => x.country_code === code);
        const picked = new Set(pickDiversified(list, 6).map((x) => x.id));
        list.forEach((x) => (x.selected = picked.has(x.id)));
      } else if (action === "clearSel") {
        const list = CANDIDATES.filter((x) => x.country_code === code);
        list.forEach((x) => (x.selected = false));
      }
      persistCandidates();
      renderCandidates();
    });
  });
}

function moveCandidateBefore(dragId, targetId) {
  const from = CANDIDATES.findIndex((x) => String(x.id) === String(dragId));
  const to = CANDIDATES.findIndex((x) => String(x.id) === String(targetId));
  if (from < 0 || to < 0 || from === to) return;
  const [item] = CANDIDATES.splice(from, 1);
  const nextTo = CANDIDATES.findIndex((x) => String(x.id) === String(targetId));
  CANDIDATES.splice(nextTo, 0, item);
  CANDIDATES.forEach((x, idx) => (x.manual_order = idx));
  SORT_MODE = "manual";
  persistCandidates();
  renderCandidates();
  setStatus("已更新手动排序，生成时会按当前顺序输出");
}

async function showRelatedSources(item) {
  const modal = $("relatedModal");
  const body = $("relatedBody");
  body.innerHTML = `<div class="text-sm text-slate-400 py-6 text-center">正在检索其他来源…</div>`;
  modal.classList.remove("hidden");
  modal.classList.add("flex");
  try {
    const data = await apiPost("/api/related-sources", {
      title: item.title,
      country_code: item.country_code,
      current_url: item.url,
    });
    const rows = data.items || [];
    body.innerHTML = rows.length
      ? rows.map((row) => `
          <div class="border border-slate-100 rounded-xl p-3">
            <div class="flex items-center justify-between gap-3">
              <span class="text-xs font-semibold text-[#17345f]">Source: ${escapeHtml(row.source || "")}</span>
              <button type="button" data-copy-related="${escapeHtml(row.url || "")}" class="text-xs text-slate-500 hover:text-slate-700">复制链接</button>
            </div>
            <a href="${escapeHtml(row.url || "")}" target="_blank" rel="noopener" class="block mt-1 text-sm font-medium text-slate-800 hover:text-blue-700 hover:underline">${escapeHtml(row.title || "")}</a>
          </div>
        `).join("")
      : `<div class="text-sm text-slate-400 py-6 text-center">暂未找到其他来源</div>`;
    body.querySelectorAll("button[data-copy-related]").forEach((btn) => {
      btn.addEventListener("click", () => navigator.clipboard.writeText(btn.getAttribute("data-copy-related") || ""));
    });
  } catch (e) {
    body.innerHTML = `<div class="text-sm text-red-500 py-6 text-center">检索失败：${escapeHtml(e.message)}</div>`;
  }
}

function updateCounts() {
  for (const c of COUNTRIES) {
    const cnt = CANDIDATES.filter((x) => x.country_code === c.code && x.selected).length;
    const el = document.querySelector(`[data-sel='${c.code}']`);
    if (el) el.textContent = cnt;
  }
}

function pickDiversified(list, limit) {
  const picked = [];
  const seenIds = new Set();
  const seenSources = new Set();

  for (const item of list) {
    const source = item.source || "";
    if (seenSources.has(source)) continue;
    picked.push(item);
    seenIds.add(item.id);
    seenSources.add(source);
    if (picked.length >= limit) return picked;
  }

  for (const item of list) {
    if (seenIds.has(item.id)) continue;
    picked.push(item);
    seenIds.add(item.id);
    if (picked.length >= limit) break;
  }

  return picked;
}

function fmtDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

function defaultWeekDates() {
  const end = new Date();
  const start = new Date();
  start.setDate(end.getDate() - 6);
  return { start: fmtDate(start), end: fmtDate(end) };
}

function extractFilename(contentDisposition, fallback) {
  const match = /filename="?(.*?)"?$/i.exec(contentDisposition || "");
  return match?.[1] || fallback;
}

function showDownloadResult(blob, filename) {
  if (LAST_DOWNLOAD_URL) {
    URL.revokeObjectURL(LAST_DOWNLOAD_URL);
  }
  LAST_DOWNLOAD_URL = URL.createObjectURL(blob);
  const safeFilename = escapeHtml(filename);
  $("downloadLinks").innerHTML = `
    <a id="downloadZipLink" href="${LAST_DOWNLOAD_URL}" download="${safeFilename}"
       class="block px-3 py-2 bg-slate-100 hover:bg-slate-200 rounded text-sm text-slate-800">
       ⬇ 下载 ${safeFilename}
    </a>
  `;
  $("resultModal").classList.remove("hidden");
  $("resultModal").classList.add("flex");
  setTimeout(() => {
    $("downloadZipLink")?.click();
  }, 100);
}

async function mapWithConcurrency(items, concurrency, worker) {
  const results = new Array(items.length);
  let cursor = 0;
  async function run() {
    while (cursor < items.length) {
      const index = cursor;
      cursor += 1;
      results[index] = await worker(items[index], index);
    }
  }
  const workers = Array.from({ length: Math.min(Math.max(1, concurrency), items.length) }, () => run());
  await Promise.all(workers);
  return results;
}

async function init() {
  applyRuntimeConfigToInputs();

  const health = await fetch("/api/health").then((r) => r.json());
  SERVER_LLM_CONFIGURED = Boolean(health.llm_configured);
  if (health.auth_required && !$("appAccessToken").value.trim()) {
    const token = window.prompt("该部署已启用内部访问口令，请输入：") || "";
    $("appAccessToken").value = token.trim();
    saveRuntimeConfig();
  }

  COUNTRIES = await apiGet("/api/countries");
  const sel = $("manualCountry");
  sel.innerHTML = COUNTRIES.map((c) => `<option value="${c.code}">${c.name_en} (${c.name_zh})</option>`).join("");

  const { start, end } = defaultWeekDates();
  if (!$("startDate").value) $("startDate").value = start;
  if (!$("endDate").value) $("endDate").value = end;

  updateStatusFilterButtons();
  setCandidates(loadCandidatesFromStorage());
}

$("openrouterApiKey").addEventListener("change", saveRuntimeConfig);
$("appAccessToken").addEventListener("change", saveRuntimeConfig);
$("openrouterBaseUrl").addEventListener("change", saveRuntimeConfig);
$("llmConcurrency").addEventListener("change", saveRuntimeConfig);
$("llmModelPreset").addEventListener("change", syncModelInputs);
$("llmModelCustom").addEventListener("change", saveRuntimeConfig);

$("btnFetch").addEventListener("click", async () => {
  setStatus("抓取中……可能需要 30 秒～2 分钟");
  try {
    await withBusy("fetch", async () => {
      const r = await apiPost("/api/fetch");
      SORT_MODE = "source";
      setBusyStage("正在刷新候选列表…");
      setCandidates(r.candidates || []);
      setStatus(`抓取完成：原始 ${r.fetched}，去重 ${r.deduped}，Layer1后 ${r.after_rules}，PASS ${r.passed_gates || 0}，FINAL ${r.final || 0}，RESERVE ${r.reserve || 0}，HOLD ${r.hold || 0}，DROP ${r.dropped || 0}`);
    });
  } catch (e) {
    setStatus("抓取失败：" + e.message);
  }
});

$("btnScore").addEventListener("click", async () => {
  if (!ensureLlmConfigured()) return;
  setStatus("LLM 打分中……");
  try {
    const runtime = getRuntimeConfigForApi();
    await withBusy("score", async () => {
      const r = await apiPost("/api/score", {
        candidates: serializeCandidates(),
        runtime,
      });
      SORT_MODE = "score";
      setBusyStage("正在按 AI 评分刷新排序…");
      setCandidates(r.candidates || [], { preserveSelected: true });
      setStatus(`已评分 ${r.scored} 条，当前已按 AI 评分排序`);
    });
  } catch (e) {
    setStatus("打分失败：" + e.message);
  }
});

$("btnClear").addEventListener("click", async () => {
  if (!confirm("确认清空当前浏览器中的候选？")) return;
  try {
    await withBusy("clear", async () => {
      const deleted = CANDIDATES.length;
      SORT_MODE = "source";
      COLLAPSED = {};
      VIEW_MODE = {};
      setCandidates([]);
      setStatus(`已清空 ${deleted} 条`);
    });
  } catch (e) {
    setStatus("清空失败：" + e.message);
  }
});

$("btnManualAdd").addEventListener("click", async () => {
  const url = $("manualUrl").value.trim();
  if (!url) return alert("请输入 URL");
  setStatus("抓取手动 URL……");
  try {
    await withBusy("manual", async () => {
      const r = await apiPost("/api/manual", {
        country_code: $("manualCountry").value,
        url,
        source: $("manualSource").value.trim(),
      });
      $("manualUrl").value = "";
      const next = [...CANDIDATES, r.item];
      setCandidates(next, { preserveSelected: true });
      setStatus(`已添加；识别到原始来源：${r.item.source || "待人工确认"}`);
    });
  } catch (e) {
    setStatus("添加失败：" + e.message);
  }
});

$("btnExpandAll").addEventListener("click", () => {
  for (const c of COUNTRIES) COLLAPSED[c.code] = false;
  renderCandidates();
});

$("btnCollapseAll").addEventListener("click", () => {
  for (const c of COUNTRIES) COLLAPSED[c.code] = true;
  renderCandidates();
});

document.querySelectorAll(".status-filter").forEach((btn) => {
  btn.addEventListener("click", () => {
    STATUS_FILTER = btn.dataset.statusFilter || "all";
    updateStatusFilterButtons();
    renderCandidates();
  });
});

$("btnSources").addEventListener("click", async () => {
  try {
    const data = await apiGet("/api/sources");
    const body = $("sourcesBody");
    const allSources = data.flatMap((country) => country.sources || []);
    const directCount = allSources.filter((source) => source.type === "rss").length;
    const discoveryCount = allSources.length - directCount;
    const summary = `
      <div class="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-relaxed text-amber-900">
        当前共 ${allSources.length} 个检索策略：${directCount} 个可用的直接 RSS，${discoveryCount} 个通过 Google News 定向检索指定媒体、机构或重点公司。
        Google News 是发现渠道，不是新闻发布方；候选卡片中的“媒体来源”才是原始发布机构。
      </div>`;
    body.innerHTML = summary + data
      .map((c) => {
        const rows = (c.sources || [])
          .map((s) => {
            const typeLabel = s.type === "google_news" ? "Google News 搜索" : (s.type === "rss" ? "RSS 订阅" : "Watch List 搜索");
            const tierLabel = `T${s.tier || 3} · ${s.category || "media"}`;
            const target = s.type === "google_news"
              ? `<code class="text-[11px] text-slate-700">${escapeHtml(s.query || "")}</code>`
              : (s.url
                ? `<a href="${escapeHtml(s.url)}" target="_blank" class="text-[11px] text-blue-600 hover:underline truncate inline-block max-w-[400px] align-bottom">${escapeHtml(s.url)}</a>`
                : "");
            return `
              <tr class="border-b border-slate-100 last:border-0">
                <td class="py-1.5 pr-2 text-xs font-medium text-slate-700">${escapeHtml(s.name)}</td>
                <td class="py-1.5 pr-2 text-[11px] text-slate-500 whitespace-nowrap">${typeLabel}<br>${escapeHtml(tierLabel)}</td>
                <td class="py-1.5 pr-2">${target}</td>
              </tr>`;
          })
          .join("");
        return `
          <div class="border border-slate-200 rounded-md overflow-hidden">
            <div class="bg-slate-50 px-3 py-1.5 font-semibold text-sm text-[#1f3a6e]">
              ${escapeHtml(c.name_en)} · ${escapeHtml(c.name_zh)}
              <span class="text-xs text-slate-500 font-normal">（${(c.sources || []).length} 个检索策略）</span>
            </div>
            <table class="w-full"><tbody>${rows}</tbody></table>
          </div>`;
      })
      .join("");
    $("sourcesModal").classList.remove("hidden");
    $("sourcesModal").classList.add("flex");
  } catch (e) {
    alert("加载信源失败：" + e.message);
  }
});

$("btnGenerate").addEventListener("click", async () => {
  const selected = CANDIDATES.filter((x) => x.selected);
  if (!selected.length) return alert("请至少勾选一条新闻");
  if (!ensureLlmConfigured()) return;
  const byCountry = {};
  selected.forEach((item) => (byCountry[item.country_code] = (byCountry[item.country_code] || 0) + 1));
  const quotaWarnings = Object.entries(byCountry)
    .filter(([, count]) => count < 3 || count > 5)
    .map(([country, count]) => `${country}: ${count} 条（建议 3-5 条）`);
  const warningText = quotaWarnings.length ? `\n\n选择数量提示：\n${quotaWarnings.join("\n")}` : "";
  if (!confirm(`将对 ${selected.length} 条新闻调用 LLM 生成中英双语 docx，可能需要几分钟，继续？${warningText}`)) return;
  const generationSnapshot = serializeSelectedCandidates();
  if (generationSnapshot.length !== selected.length) {
    return alert("勾选状态已变化，请重新确认后再生成");
  }
  setStatus(`生成中（${generationSnapshot.length} 条）……请耐心等待`);
  try {
    const runtime = getRuntimeConfigForApi();
    const result = await withBusy("generate", async () => {
      let completed = 0;
      const concurrency = Math.max(1, Math.min(4, runtime.llm_concurrency || 2));
      const outcomes = await mapWithConcurrency(generationSnapshot, concurrency, async (candidate) => {
        try {
          const response = await apiPost("/api/format-item", { candidate, runtime });
          if (response.item?.url && response.item.url !== candidate.url) {
            const liveItem = CANDIDATES.find((item) => String(item.id) === String(candidate.id));
            if (liveItem) {
              liveItem.google_news_url = liveItem.google_news_url || liveItem.url;
              liveItem.url = response.item.url;
            }
          }
          return { item: response.item, error: "" };
        } catch (error) {
          return { item: null, error: `${candidate.title}: ${error.message}` };
        } finally {
          completed += 1;
          const actualProgress = 5 + (completed / generationSnapshot.length) * 82;
          if (BUSY_STATE) BUSY_STATE.progress = actualProgress;
          setBusyProgress(actualProgress);
          setBusyStage(`正在生成双语稿件… ${completed}/${generationSnapshot.length}`);
        }
      });
      const formattedItems = outcomes.filter((outcome) => outcome.item).map((outcome) => outcome.item);
      const clientFailures = outcomes.filter((outcome) => outcome.error);
      if (!formattedItems.length) {
        throw new Error(`所有新闻均生成失败。${clientFailures[0]?.error || "请检查模型和 API 配置"}`);
      }
      persistCandidates();
      setBusyStage("双语稿件完成，正在套用 Word 模板…");
      setBusyProgress(92);
      const { blob, contentDisposition, selectedCount, dedupedCount, formattedCount, failedCount, warningCount } = await apiPostBlob("/api/render", {
        formatted_items: formattedItems,
        issue_number: Number($("issueNumber").value || 137),
        start_date: $("startDate").value,
        end_date: $("endDate").value,
      });
      const filename = extractFilename(contentDisposition, `issue_${$("issueNumber").value || 137}_bilingual_docx.zip`);
      return {
        blob,
        filename,
        selectedCount,
        dedupedCount,
        formattedCount,
        failedCount: Number(failedCount || 0) + clientFailures.length,
        warningCount,
      };
    });
    showDownloadResult(result.blob, result.filename);
    const countTip = result.selectedCount
      ? `勾选 ${result.selectedCount} 条，成功写入 ${result.formattedCount || "?"} 条`
      : `生成完成：${generationSnapshot.length} 条`;
    const failedTip = Number(result.failedCount || 0) > 0
      ? `，${result.failedCount} 条格式化失败`
      : "";
    const warningTip = Number(result.warningCount || 0) > 0 ? `，${result.warningCount} 个事实一致性提示需人工核对` : "";
    setStatus(`${countTip}${failedTip}${warningTip}，已自动触发下载，也可点击弹窗按钮重新下载`);
  } catch (e) {
    setStatus("生成失败：" + e.message);
  }
});

init().catch((error) => {
  setStatus(`初始化失败：${error.message}`);
  console.error(error);
});
