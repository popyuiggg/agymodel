/**
 * Antigravity 端点检测工具 - 前端交互逻辑
 * 零外部依赖，支持增量无闪烁轮询、轮次点击展开详情、明暗主题切换。
 */

const state = {
  conversations: [],
  sourceFilter: "all",
  currentId: "",
  currentSource: "",
  currentData: null,
  lastDataSignature: "",
  lastListSignature: "",
  expandedTurns: new Set(),
  sortDesc: true,
  searchQuery: "",
  pollMs: 2000,
  pollTimer: null,
  theme: localStorage.getItem("agymodel_theme") || "light",
};

document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  bindEvents();
  fetchConversationsList(true);
  startPolling();
});

function initTheme() {
  document.documentElement.setAttribute("data-theme", state.theme);
  const btn = document.getElementById("themeBtn");
  if (btn) {
    btn.textContent = state.theme === "dark" ? "☀️ 浅色" : "🌙 深色";
  }
}

function toggleTheme() {
  state.theme = state.theme === "light" ? "dark" : "light";
  localStorage.setItem("agymodel_theme", state.theme);
  initTheme();
}

function bindEvents() {
  // 左侧来源过滤 (全部 / App / CLI)
  document.querySelectorAll("#sourceFilter .seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#sourceFilter .seg-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.sourceFilter = btn.getAttribute("data-source") || "all";
      renderConversationList();
    });
  });

  // 复制会话 ID
  document.getElementById("curId").addEventListener("click", () => {
    if (state.currentId) {
      navigator.clipboard.writeText(state.currentId).then(() => showToast("已复制会话 ID: " + state.currentId));
    }
  });

  // 轮询间隔选择
  document.getElementById("pollSelect").addEventListener("change", (e) => {
    state.pollMs = parseInt(e.target.value, 10) || 0;
    startPolling();
  });

  // 手动刷新
  document.getElementById("refreshBtn").addEventListener("click", () => {
    fetchConversationsList(true);
  });

  // 主题切换
  document.getElementById("themeBtn").addEventListener("click", toggleTheme);

  // 搜索过滤
  document.getElementById("turnSearch").addEventListener("input", (e) => {
    state.searchQuery = e.target.value.trim().toLowerCase();
    renderTurnsTable();
  });

  // 排序切换
  document.getElementById("sortOrderBtn").addEventListener("click", (e) => {
    state.sortDesc = !state.sortDesc;
    e.currentTarget.textContent = state.sortDesc ? "↓ 最新在前" : "↑ 顺序 (#0在前)";
    renderTurnsTable();
  });

  // 展开 / 收起全部
  document.getElementById("expandAllBtn").addEventListener("click", (e) => {
    if (!state.currentData || !state.currentData.turns) return;
    const allTurns = state.currentData.turns;
    if (state.expandedTurns.size >= allTurns.length && allTurns.length > 0) {
      state.expandedTurns.clear();
      e.currentTarget.textContent = "展开全部";
    } else {
      allTurns.forEach((t) => state.expandedTurns.add(t.turn_index));
      e.currentTarget.textContent = "收起全部";
    }
    renderTurnsTable();
  });
}

function startPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  const dot = document.getElementById("pollDot");
  if (state.pollMs > 0) {
    dot.classList.remove("paused");
    state.pollTimer = setInterval(() => {
      fetchConversationsList(false);
    }, state.pollMs);
  } else {
    dot.classList.add("paused");
  }
}

async function fetchConversationsList(forceDetail = false) {
  try {
    const res = await fetch("/api/conversations?limit=40");
    const list = await res.json();
    if (!Array.isArray(list)) return;

    state.conversations = list;
    const sig = list.map((c) => `${c.id}:${c.mtime}:${c.turn_count}`).join("|");
    if (sig !== state.lastListSignature || forceDetail) {
      state.lastListSignature = sig;
      renderConversationList();
    }

    if (!state.currentId && list.length > 0) {
      state.currentId = list[0].id;
      state.currentSource = list[0].source;
      renderConversationList();
    }

    if (state.currentId) {
      await fetchConversationDetail(state.currentId, state.currentSource, forceDetail);
    }
  } catch (err) {
    console.error("获取会话列表失败:", err);
  }
}

function renderConversationList() {
  const container = document.getElementById("convList");
  const filtered = state.conversations.filter(
    (c) => state.sourceFilter === "all" || c.source === state.sourceFilter
  );

  if (filtered.length === 0) {
    container.innerHTML = `<div class="empty-hint">暂无匹配的会话</div>`;
    return;
  }

  container.innerHTML = filtered
    .map((c) => {
      const isActive = c.id === state.currentId;
      const shortTime = c.updated_at ? c.updated_at.slice(5, 16) : "";
      const tpsText = c.latest_tps ? `${c.latest_tps} TPS` : `${c.turn_count} 轮`;
      return `
        <div class="conv-item ${isActive ? "active" : ""}" data-id="${escapeHtml(c.id)}" data-source="${escapeHtml(c.source)}">
          <div class="conv-item-top">
            <span class="source-tag">${escapeHtml(c.source)}</span>
            <span class="conv-title" title="${escapeHtml(c.title)}">${escapeHtml(c.title)}</span>
          </div>
          <div class="conv-item-bottom">
            <span class="conv-ep-badge" title="真实端点: ${escapeHtml(c.backend_endpoint)}">${escapeHtml(c.backend_endpoint)}</span>
            <span class="mono">${escapeHtml(tpsText)} · ${escapeHtml(shortTime)}</span>
          </div>
        </div>
      `;
    })
    .join("");

  container.querySelectorAll(".conv-item").forEach((el) => {
    el.addEventListener("click", () => {
      const cid = el.getAttribute("data-id") || "";
      const src = el.getAttribute("data-source") || "";
      if (cid && cid !== state.currentId) {
        state.currentId = cid;
        state.currentSource = src;
        state.expandedTurns.clear();
        state.lastDataSignature = "";
        document.getElementById("expandAllBtn").textContent = "展开全部";
        renderConversationList();
        fetchConversationDetail(cid, src, true);
      }
    });
  });
}

async function fetchConversationDetail(cid, source, force = false) {
  try {
    const q = new URLSearchParams({ id: cid });
    if (source) q.set("source", source);
    const res = await fetch(`/api/conversation?${q.toString()}`);
    const data = await res.json();
    if (!data || data.error) return;

    const sig = `${data.id}:${data.mtime}:${(data.turns || []).length}:${data.summary?.latest_tps}`;
    if (!force && sig === state.lastDataSignature) {
      return; // 数据未变化，跳过重绘以保持滚动与交互状态
    }

    state.lastDataSignature = sig;
    state.currentData = data;
    renderHeaderAndMetrics(data);
    renderTurnsTable();
  } catch (err) {
    console.error("获取会话详情失败:", err);
  }
}

function renderHeaderAndMetrics(data) {
  const s = data.summary || {};

  document.getElementById("curSourceTag").textContent = data.source || "App";
  document.getElementById("curTitle").textContent = data.title || data.id;
  document.getElementById("curId").textContent = `ID: ${data.id.slice(0, 12)}...`;
  document.getElementById("curUpdatedAt").textContent = `最后更新 ${data.updated_at || "-"}`;

  // 卡片 1: 后端真实端点
  document.getElementById("statBackendEndpoint").textContent = s.backend_endpoint || "未知";
  document.getElementById("statModelEnum").textContent = s.model_enum || "-";
  const consEl = document.getElementById("statConsistent");
  if ((s.unique_endpoints || []).length <= 1) {
    consEl.textContent = "全程一致";
    consEl.className = "consistency-pill ok";
  } else {
    consEl.textContent = `${s.unique_endpoints.length} 个端点切换`;
    consEl.className = "consistency-pill warn";
    consEl.title = s.unique_endpoints.join(" ➔ ");
  }

  // 卡片 2: 前端配置模型
  document.getElementById("statClientModel").textContent = s.client_model || "默认";
  document.getElementById("statNonGemini").textContent = s.used_non_gemini ? "非 Gemini" : "Gemini 原生";
  document.getElementById("statTotalTurns").textContent = s.total_turns ?? 0;
  document.getElementById("statTotalDuration").textContent = `累计生成 ${s.total_duration || 0}s`;

  // 卡片 3: TPS 与延迟
  document.getElementById("statLatestTps").textContent = s.latest_tps ? `${s.latest_tps}` : "-";
  document.getElementById("statAvgTps").textContent = s.avg_tps ? `${s.avg_tps}` : "-";
  document.getElementById("statAvgStreamTps").textContent = s.avg_streaming_tps ? `${s.avg_streaming_tps}` : "-";
  document.getElementById("statPeakTps").textContent = s.peak_tps ? `峰值 ${s.peak_tps}` : "峰值 -";
  document.getElementById("statLatestTtft").textContent = s.latest_ttft ? `${s.latest_ttft}s` : "-";
  document.getElementById("statAvgTtft").textContent = s.avg_ttft ? `${s.avg_ttft}s` : "-";

  // 卡片 4: Token 消耗
  const totalIn = (s.total_prompt_tokens || 0) + (s.total_cached_tokens || 0);
  const cacheRate = totalIn > 0 ? Math.round(((s.total_cached_tokens || 0) / totalIn) * 100) : 0;
  document.getElementById("statCacheRate").textContent = `缓存率 ${cacheRate}%`;
  document.getElementById("statOutTokens").textContent = formatNumber(s.total_output_tokens || 0);
  document.getElementById("statInTokens").textContent = formatNumber(s.total_prompt_tokens || 0);
  document.getElementById("statThinkContent").textContent = `${formatCompact(s.total_thinking_tokens || 0)} / ${formatCompact(s.total_content_tokens || 0)}`;
  document.getElementById("statCachedTokens").textContent = formatCompact(s.total_cached_tokens || 0);
}

function renderTurnsTable() {
  const tbody = document.getElementById("turnsTableBody");
  const data = state.currentData;
  if (!data || !Array.isArray(data.turns) || data.turns.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-cell">该会话暂无 gen_metadata 推理记录</td></tr>`;
    return;
  }

  let turns = [...data.turns];
  if (state.sortDesc) {
    turns.reverse();
  }

  if (state.searchQuery) {
    const q = state.searchQuery;
    turns = turns.filter((t) => {
      const ctx = t.context || {};
      const toolStr = (ctx.tools || []).map((x) => `${x.name} ${x.target}`).join(" ");
      const hay = `${t.turn_index} ${t.client_model || ""} ${t.backend_model || ""} ${t.model_enum || ""} ${ctx.user_prompt || ""} ${ctx.reply_text || ""} ${ctx.thought_text || ""} ${toolStr}`.toLowerCase();
      return hay.includes(q);
    });
  }

  if (turns.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-cell">没有匹配筛选条件的轮次</td></tr>`;
    return;
  }

  let html = "";
  turns.forEach((t) => {
    const idx = t.turn_index;
    const isExpanded = state.expandedTurns.has(idx);
    const ctx = t.context || {};
    const timeStr = ctx.time_str || "";
    const clientModel = t.client_model || "默认";
    const backendModel = t.backend_model || "未知";
    const enumShort = (t.model_enum || "-").replace("MODEL_PLACEHOLDER_", "M_");

    // 交互摘要一栏预览
    let previewBadge = `<span class="preview-tag">推理</span>`;
    let previewText = ctx.thought_text || ctx.user_prompt || "-";
    if (ctx.reply_text) {
      previewBadge = `<span class="preview-tag reply">回复</span>`;
      previewText = ctx.reply_text;
    } else if (ctx.tools && ctx.tools.length > 0) {
      const toolNames = ctx.tools.map((x) => x.name).join(", ");
      previewBadge = `<span class="preview-tag tool">调用 ${ctx.tools.length} 项</span>`;
      previewText = `${toolNames}${ctx.tools[0].target ? " · " + ctx.tools[0].target : ""}`;
    }
    const cleanPreview = previewText.replace(/\s+/g, " ").trim().slice(0, 58);

    html += `
      <tr class="turn-row ${isExpanded ? "expanded" : ""}" data-turn="${idx}">
        <td>
          <div class="turn-idx-cell">
            <span class="chevron">▸</span>
            <div>
              <span class="turn-badge">#${idx}</span>
              ${timeStr ? `<span class="turn-time">${escapeHtml(timeStr)}</span>` : ""}
            </div>
          </div>
        </td>
        <td>
          <div class="model-pair">
            <span class="client-pill" title="${t.client_model_inherited ? "同会话继承配置" : "当轮显式捕获配置"}">
              ${escapeHtml(clientModel)}${t.client_model_inherited ? '<span class="inherited-tag">(继承)</span>' : ""}
            </span>
            <span class="arrow-sep">➔</span>
            <span class="endpoint-pill">${escapeHtml(backendModel)}</span>
          </div>
        </td>
        <td>
          <span class="enum-text" title="${escapeHtml(t.model_enum || "-")}">${escapeHtml(enumShort)}</span>
        </td>
        <td class="num">
          <span class="tps-main">${t.tps ? t.tps.toFixed(1) : "-"}</span>
          <span class="tps-sub">流式 ${t.streaming_tps ? t.streaming_tps.toFixed(1) : "-"}</span>
        </td>
        <td class="num mono">
          <span>${t.duration_seconds ? t.duration_seconds.toFixed(2) + "s" : "-"}</span>
          <span class="cell-sub">首包 ${t.ttft_seconds ? t.ttft_seconds.toFixed(2) + "s" : "-"} · 流 ${t.stream_seconds ? t.stream_seconds.toFixed(2) + "s" : "-"}</span>
        </td>
        <td class="num mono">
          <span>${formatNumber(t.prompt_tokens || 0)}</span>
          <span class="cell-sub">缓存 ${formatCompact(t.cached_tokens || 0)}</span>
        </td>
        <td class="num mono">
          <span>${formatNumber(t.output_tokens || 0)}</span>
          <span class="cell-sub">思 ${t.thinking_tokens || 0} / 文 ${t.content_tokens || 0}</span>
        </td>
        <td>
          <div class="summary-preview">
            ${previewBadge}
            <span>${escapeHtml(cleanPreview)}</span>
          </div>
        </td>
      </tr>
    `;

    if (isExpanded) {
      html += renderDetailRow(t);
    }
  });

  tbody.innerHTML = html;

  tbody.querySelectorAll(".turn-row").forEach((row) => {
    row.addEventListener("click", () => {
      const idx = parseInt(row.getAttribute("data-turn") || "-1", 10);
      if (idx < 0) return;
      if (state.expandedTurns.has(idx)) {
        state.expandedTurns.delete(idx);
      } else {
        state.expandedTurns.add(idx);
      }
      renderTurnsTable();
    });
  });
}

function renderDetailRow(t) {
  const ctx = t.context || {};
  const userTitle = ctx.is_direct_user_turn
    ? `用户直接提问 (Step #${ctx.user_step_index ?? 0})`
    : `所属任务上下文提问 (Step #${ctx.user_step_index ?? 0} 触发的连续推理)`;

  const toolsHtml =
    ctx.tools && ctx.tools.length > 0
      ? `<div class="tool-pills">
          ${ctx.tools
            .map(
              (tool) => `
              <span class="tool-pill">
                <strong>${escapeHtml(tool.name)}</strong>
                ${tool.target ? `<span>${escapeHtml(tool.target)}</span>` : ""}
              </span>`
            )
            .join("")}
        </div>`
      : "";

  return `
    <tr class="detail-row">
      <td colspan="8">
        <div class="detail-box">
          <div class="detail-grid">
            <div class="detail-col">
              <div class="detail-heading">
                <span>💬 ${escapeHtml(userTitle)}</span>
              </div>
              <div class="detail-text">${escapeHtml(ctx.user_prompt || "未记录用户输入文本")}</div>
            </div>

            <div class="detail-col">
              <div class="detail-heading">
                <span>🤖 本轮模型响应摘要 ${ctx.step_index !== null && ctx.step_index !== undefined ? `(Step #${ctx.step_index})` : ""}</span>
                <span class="mono">${escapeHtml(t.backend_model || "")} · ${escapeHtml(t.model_enum || "")}</span>
              </div>
              ${ctx.thought_text ? `<div class="detail-thought"><strong>思考摘要:</strong> ${escapeHtml(ctx.thought_text)}</div>` : ""}
              ${ctx.reply_text ? `<div class="detail-text">${escapeHtml(ctx.reply_text)}</div>` : ""}
              ${toolsHtml}
              ${!ctx.thought_text && !ctx.reply_text && !toolsHtml ? `<div class="detail-text">该轮次未包含可展示的文本摘要</div>` : ""}
            </div>
          </div>

          <div class="detail-meta-bar mono">
            <span>Request ID: ${escapeHtml(t.request_id || "-")}</span>
            <span>Bot ID: ${escapeHtml(t.bot_id || "-")}</span>
            <span>输入 Step 游标: #${t.last_step_index ?? "-"}</span>
            <span>元数据块大小: ${formatNumber(t.payload_size || 0)} Bytes</span>
          </div>
        </div>
      </td>
    </tr>
  `;
}

function formatNumber(n) {
  return Number(n || 0).toLocaleString();
}

function formatCompact(n) {
  const num = Number(n || 0);
  if (num >= 1000000) return (num / 1000000).toFixed(2) + "M";
  if (num >= 10000) return (num / 1000).toFixed(1) + "k";
  return num.toLocaleString();
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

let toastTimer = null;
function showToast(msg) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.remove("hidden");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.classList.add("hidden");
  }, 2000);
}
