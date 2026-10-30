// 动态计算当前前缀，支持公网反代及任意前缀路径
const BASE_PATH = window.location.pathname.replace(/\/+$/, "");

function getApiUrl(endpoint) {
  const cleanEndpoint = endpoint.startsWith("/") ? endpoint : `/${endpoint}`;
  return `${BASE_PATH}${cleanEndpoint}`;
}

let currentCategory = "全部";
let searchQuery = "";
let allTerms = [];

document.addEventListener("DOMContentLoaded", () => {
  fetchContext();
  fetchCategories();
  fetchTerms();

  // 绑定事件
  document.getElementById("btn-refresh-context").addEventListener("click", fetchContext);
  document.getElementById("btn-clear-context").addEventListener("click", clearContext);
  document.getElementById("search-input").addEventListener("input", (e) => {
    searchQuery = e.target.value.trim();
    fetchTerms();
  });

  // 模态弹窗事件
  const modal = document.getElementById("term-modal");
  document.getElementById("btn-open-add-modal").addEventListener("click", () => {
    document.getElementById("term-form").reset();
    document.getElementById("form-term-id").value = "";
    document.getElementById("modal-title").innerText = "添加新术语";
    document.getElementById("form-category").value = "篮球/术语";
    modal.style.display = "flex";
  });
  document.getElementById("btn-close-modal").addEventListener("click", () => modal.style.display = "none");
  document.getElementById("btn-cancel-modal").addEventListener("click", () => modal.style.display = "none");

  // 快捷设为保持英文不翻译按钮
  document.getElementById("btn-set-keep-original").addEventListener("click", () => {
    const src = document.getElementById("form-source").value.trim();
    document.getElementById("form-target").value = src || "[保持原文]";
    document.getElementById("form-note").value = document.getElementById("form-note").value || "保持英文原词，严禁音译或直译";
  });

  // 表单提交（新增或编辑保存）
  document.getElementById("term-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const termId = document.getElementById("form-term-id").value.trim();
    const source = document.getElementById("form-source").value.trim();
    const target = document.getElementById("form-target").value.trim();
    const category = document.getElementById("form-category").value.trim() || "篮球/术语";
    const note = document.getElementById("form-note").value.trim();

    try {
      const url = termId ? getApiUrl(`/api/terms/${termId}`) : getApiUrl("/api/terms");
      const method = termId ? "PUT" : "POST";
      const resp = await fetch(url, {
        method: method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, target, category, note })
      });
      if (resp.ok) {
        modal.style.display = "none";
        fetchCategories();
        fetchTerms();
      } else {
        const errData = await resp.json();
        alert("保存失败: " + (errData.detail || "请检查输入！"));
      }
    } catch (err) {
      alert("请求异常: " + err.message);
    }
  });

  // 导出 JSON
  document.getElementById("btn-export-terms").addEventListener("click", () => {
    window.open(getApiUrl("/api/terms/export"), "_blank");
  });

  // 清空日志按钮
  document.getElementById("btn-clear-live-logs").addEventListener("click", clearLiveLogs);

  // 定时刷新
  fetchLiveLogs();
  setInterval(fetchContext, 5000);
  setInterval(fetchLiveLogs, 1500);
});

async function fetchContext() {
  try {
    const resp = await fetch(getApiUrl("/api/context/current"));
    const data = await resp.json();
    const ctx = data.context;
    const container = document.getElementById("context-content");

    if (!ctx || !ctx.topic) {
      container.innerHTML = `
        <div style="text-align: center; color: #94a3b8; padding: 20px;">
          <p>当前处于<strong>通用模式</strong>或尚未捕获网页。</p>
          <p style="font-size: 13px; margin-top: 6px;">当你在 Firefox 中浏览 Reddit 篮球贴或观看 YouTube 视频时，油猴脚本会自动抓取语境并在此同步呈现。</p>
        </div>
      `;
      return;
    }

    container.innerHTML = `
      <div class="context-grid">
        <div class="ctx-item">
          <div class="ctx-label">当前网页 / 视频标题</div>
          <div class="ctx-value" title="${ctx.title}">${ctx.title}</div>
        </div>
        <div class="ctx-item">
          <div class="ctx-label">领域分类</div>
          <div class="ctx-value">${ctx.domain || "未指定"}</div>
        </div>
        <div class="ctx-item">
          <div class="ctx-label">行文风格</div>
          <div class="ctx-value">${ctx.style || "标准"}</div>
        </div>
        <div class="ctx-summary">
          <div class="ctx-label">全局背景分析（已实时注入翻译 System Prompt）</div>
          <div class="ctx-value">${ctx.summary || ctx.topic}</div>
        </div>
      </div>
    `;
  } catch (err) {
    console.error("获取上下文失败:", err);
  }
}

async function clearLiveLogs() {
  try {
    await fetch(getApiUrl("/api/logs/clear"), { method: "POST" });
    fetchLiveLogs();
  } catch (err) {
    console.error("清空日志失败:", err);
  }
}

let lastLogTimestamp = "";

async function fetchLiveLogs() {
  try {
    const resp = await fetch(getApiUrl("/api/logs/live"));
    if (!resp.ok) return;
    const data = await resp.json();
    const logs = data.logs || [];
    const container = document.getElementById("logs-container");
    const countBadge = document.getElementById("logs-count-badge");
    const autoScroll = document.getElementById("check-auto-scroll").checked;

    countBadge.innerText = `${logs.length} 条记录`;

    if (logs.length === 0) {
      container.innerHTML = `<div style="color: #94a3b8; text-align: center; padding: 24px;">等待翻译请求中（看 YouTube 或刷网页时自动实时滚动呈现）...</div>`;
      lastLogTimestamp = "";
      return;
    }

    // 检查是否有新日志
    const newestTs = logs[0].timestamp;
    if (newestTs === lastLogTimestamp) return;
    lastLogTimestamp = newestTs;

    // 按时间正序渲染（旧在上面，新在最下面，符合终端滚动习惯）
    const chronologicalLogs = [...logs].reverse();
    let html = "";
    for (const item of chronologicalLogs) {
      const isCache = item.type === "CACHE_HIT";
      const isStream = item.stream === true;
      const typeClass = isCache ? "cache-hit" : (isStream ? "stream" : "");
      const typeLabel = isCache ? "⚡ 本地缓存命中" : (isStream ? "🌊 流式传输 (Stream)" : "🚀 单次响应");
      
      html += `
        <div class="log-entry ${typeClass}">
          <div class="log-meta">
            <span class="log-tag">${typeLabel}</span>
            <span>⏱️ ${escapeHtml(item.timestamp)}</span>
          </div>
          <div class="log-section">
            <span class="log-section-label">📥 沉浸式翻译请求原文:</span>
            <div class="log-content">${escapeHtml(item.user_input || "-")}</div>
          </div>
          ${item.raw_output ? `
          <div class="log-section">
            <span class="log-section-label" style="color: #f59e0b;">⚠️ 模型原始未清洗输出:</span>
            <div class="log-content" style="color: #fcd34d;">${escapeHtml(item.raw_output)}</div>
          </div>` : ""}
          <div class="log-section">
            <span class="log-section-label" style="color: #4ade80;">📤 网关输出给沉浸式翻译 (Final):</span>
            <div class="log-content out">${escapeHtml(item.output || "-")}</div>
          </div>
        </div>
      `;
    }
    container.innerHTML = html;

    if (autoScroll) {
      container.scrollTop = container.scrollHeight;
    }
  } catch (err) {
    console.error("获取实时日志失败:", err);
  }
}

async function clearContext() {
  if (confirm("确定重置为通用语境模式吗？")) {
    await fetch(getApiUrl("/api/context/clear"), { method: "POST" });
    fetchContext();
  }
}

async function fetchCategories() {
  try {
    const resp = await fetch(getApiUrl("/api/terms/categories"));
    const data = await resp.json();
    const categories = data.categories || [];
    const container = document.getElementById("category-filters");
    
    let html = `<button class="chip ${currentCategory === '全部' ? 'active' : ''}" data-cat="全部">全部</button>`;
    for (const cat of categories) {
      html += `<button class="chip ${currentCategory === cat ? 'active' : ''}" data-cat="${cat}">${cat}</button>`;
    }
    container.innerHTML = html;

    container.querySelectorAll(".chip").forEach(btn => {
      btn.addEventListener("click", () => {
        container.querySelectorAll(".chip").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        currentCategory = btn.getAttribute("data-cat");
        fetchTerms();
      });
    });
  } catch (err) {
    console.error("获取分类失败:", err);
  }
}

async function fetchTerms() {
  try {
    const apiUrl = getApiUrl("/api/terms");
    const url = new URL(apiUrl, window.location.origin);
    if (searchQuery) url.searchParams.set("search", searchQuery);
    if (currentCategory && currentCategory !== "全部") url.searchParams.set("category", currentCategory);
    url.searchParams.set("limit", "200");

    const resp = await fetch(url.toString());
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    const data = await resp.json();
    const terms = data.terms || [];
    const total = data.total || 0;
    
    document.getElementById("terms-count-badge").innerText = `${total} 个术语`;

    const tbody = document.getElementById("terms-tbody");
    if (terms.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="text-center" style="padding: 30px; color: #94a3b8;">未找到匹配术语</td></tr>`;
      return;
    }

    let rowsHtml = "";
    for (const t of terms) {
      const isKeep = t.target.trim().toLowerCase() === t.source.trim().toLowerCase() || t.target === "[保持原文]" || t.target === "[不翻译]";
      const targetDisplay = isKeep 
        ? `<span class="badge-keep">🔤 保持英文原文 (${escapeHtml(t.source)})</span>`
        : `<span class="term-target">${escapeHtml(t.target)}</span>`;

      rowsHtml += `
        <tr>
          <td><span class="term-source">${escapeHtml(t.source)}</span></td>
          <td>${targetDisplay}</td>
          <td><span class="term-cat-badge">${escapeHtml(t.category)}</span></td>
          <td><span class="term-note">${escapeHtml(t.note || "-")}</span></td>
          <td><span style="font-weight: 600; color: #f59e0b;">${t.hit_count} 次</span></td>
          <td>
            <button class="btn-icon-action btn-edit" title="编辑修改" onclick="editTerm(${t.id})">✏️</button>
            <button class="btn-icon-action btn-del" title="删除" onclick="deleteTerm(${t.id}, '${escapeHtml(t.source)}')">🗑️</button>
          </td>
        </tr>
      `;
    }
    tbody.innerHTML = rowsHtml;
  } catch (err) {
    console.error("获取术语列表失败:", err);
    document.getElementById("terms-tbody").innerHTML = `
      <tr><td colspan="6" class="text-center" style="padding: 30px; color: #f87171;">
        加载失败: ${escapeHtml(err.message)}（请检查网络或刷新重试）
      </td></tr>
    `;
  }
}

async function editTerm(id) {
  try {
    const resp = await fetch(getApiUrl(`/api/terms/${id}`));
    if (!resp.ok) {
      alert("未找到该术语！");
      return;
    }
    const data = await resp.json();
    const t = data.term;
    document.getElementById("form-term-id").value = t.id;
    document.getElementById("form-source").value = t.source;
    document.getElementById("form-target").value = t.target;
    document.getElementById("form-category").value = t.category || "篮球/术语";
    document.getElementById("form-note").value = t.note || "";
    document.getElementById("modal-title").innerText = `编辑术语: ${t.source}`;
    document.getElementById("term-modal").style.display = "flex";
  } catch (err) {
    alert("读取术语失败: " + err.message);
  }
}

async function deleteTerm(id, name) {
  if (confirm(`确定删除术语 "${name}" 吗？`)) {
    const resp = await fetch(getApiUrl(`/api/terms/${id}`), { method: "DELETE" });
    if (resp.ok) {
      fetchCategories();
      fetchTerms();
    }
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
