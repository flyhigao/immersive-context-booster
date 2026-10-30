// ==UserScript==
// @name         沉浸式翻译 · 智能语境与术语伴侣 (Reddit, Quora & YouTube)
// @namespace    https://github.com/immersive-context-booster
// @version      1.1.2
// @description  为沉浸式翻译自动注入当前 Reddit 帖子/Quora 问答/YouTube 视频的全局语境、专业领域与黑话术语，彻底解决多领域乱翻误翻问题！
// @author       gao
// @updateURL    http://127.0.0.1:8787/immersive-context-booster.user.js
// @downloadURL  http://127.0.0.1:8787/immersive-context-booster.user.js
// @match        *://*.reddit.com/*
// @match        *://*.quora.com/*
// @match        *://*.youtube.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @connect      *
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const GATEWAY_URL = "http://127.0.0.1:8787";
  let lastAnalyzedUrl = "";
  let currentContext = null;

  // 注入样式
  GM_addStyle(`
    #imt-booster-root {
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 9999999;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      user-select: none;
    }
    .imt-capsule {
      display: flex;
      align-items: center;
      gap: 8px;
      background: rgba(15, 23, 42, 0.92);
      color: #f8fafc;
      border: 1px solid #334155;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
      padding: 8px 14px;
      border-radius: 9999px;
      cursor: pointer;
      font-size: 13px;
      font-weight: 500;
      backdrop-filter: blur(8px);
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .imt-capsule:hover {
      background: #1e293b;
      border-color: #f97316;
      transform: translateY(-2px);
    }
    .imt-capsule .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #10b981;
      box-shadow: 0 0 8px #10b981;
    }
    .imt-capsule .dot.syncing {
      background: #f59e0b;
      box-shadow: 0 0 8px #f59e0b;
      animation: imtPulse 1s infinite alternate;
    }
    @keyframes imtPulse {
      0% { opacity: 0.3; }
      100% { opacity: 1; }
    }
    .imt-card {
      position: absolute;
      bottom: 48px;
      right: 0;
      width: 320px;
      background: rgba(15, 23, 42, 0.96);
      border: 1px solid #334155;
      border-radius: 12px;
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
      padding: 16px;
      color: #f8fafc;
      font-size: 13px;
      backdrop-filter: blur(12px);
      display: none;
      flex-direction: column;
      gap: 12px;
    }
    .imt-card.show {
      display: flex;
    }
    .imt-card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid #334155;
      padding-bottom: 8px;
      font-weight: 600;
      color: #f97316;
    }
    .imt-card-row {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .imt-label {
      font-size: 11px;
      color: #94a3b8;
      text-transform: uppercase;
    }
    .imt-val {
      font-size: 13px;
      color: #e2e8f0;
      font-weight: 500;
      max-height: 80px;
      overflow-y: auto;
    }
    .imt-btn-group {
      display: flex;
      gap: 8px;
      margin-top: 4px;
    }
    .imt-btn {
      flex: 1;
      padding: 6px 10px;
      border-radius: 6px;
      font-size: 12px;
      border: none;
      cursor: pointer;
      background: #334155;
      color: #f8fafc;
      text-align: center;
      transition: background 0.15s;
    }
    .imt-btn:hover {
      background: #475569;
    }
    .imt-btn-primary {
      background: #f97316;
      color: #fff;
    }
    .imt-btn-primary:hover {
      background: #ea580c;
    }
    /* 划选快速添加术语气泡 */
    #imt-quick-add-btn {
      position: absolute;
      display: none;
      z-index: 10000000;
      background: #f97316;
      color: #fff;
      padding: 4px 10px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      box-shadow: 0 4px 12px rgba(0,0,0,0.4);
      border: 1px solid #ea580c;
      animation: imtFadeIn 0.15s ease;
    }
    #imt-quick-add-btn:hover {
      background: #ea580c;
    }
    @keyframes imtFadeIn {
      from { opacity: 0; transform: translateY(4px); }
      to { opacity: 1; transform: translateY(0); }
    }
    /* 网页内迷你术语录入弹窗 */
    #imt-quick-dialog {
      position: fixed;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      z-index: 10000001;
      width: 360px;
      background: rgba(15, 23, 42, 0.98);
      border: 1px solid #475569;
      border-radius: 12px;
      box-shadow: 0 20px 40px rgba(0,0,0,0.6);
      padding: 18px;
      color: #f8fafc;
      display: none;
      flex-direction: column;
      gap: 12px;
      backdrop-filter: blur(12px);
    }
    #imt-quick-dialog input, #imt-quick-dialog textarea {
      width: 100%;
      background: #0f172a;
      border: 1px solid #334155;
      color: #fff;
      padding: 8px 10px;
      border-radius: 6px;
      font-size: 13px;
      box-sizing: border-box;
    }
    #imt-quick-dialog input:focus, #imt-quick-dialog textarea:focus {
      outline: none;
      border-color: #f97316;
    }
    #imt-quick-dialog label {
      font-size: 11px;
      color: #94a3b8;
      text-transform: uppercase;
    }
  `);

  // 创建 UI 容器
  const container = document.createElement("div");
  container.id = "imt-booster-root";
  container.innerHTML = `
    <div class="imt-capsule" id="imt-capsule-btn">
      <span class="dot" id="imt-status-dot"></span>
      <span id="imt-status-text">💡 准备语境分析</span>
    </div>
    <div class="imt-card" id="imt-detail-card">
      <div class="imt-card-header">
        <span>🏀 沉浸式翻译 · 语境中枢</span>
        <span style="cursor: pointer; color: #94a3b8;" id="imt-close-card">&times;</span>
      </div>
      <div class="imt-card-row">
        <span class="imt-label">识别领域</span>
        <span class="imt-val" id="imt-card-domain">未分析</span>
      </div>
      <div class="imt-card-row">
        <span class="imt-label">讨论主题 / 视频核心</span>
        <span class="imt-val" id="imt-card-topic">--</span>
      </div>
      <div class="imt-card-row">
        <span class="imt-label">全局背景 (已注入翻译 Prompt)</span>
        <span class="imt-val" id="imt-card-summary">--</span>
      </div>
      <div class="imt-btn-group">
        <button class="imt-btn imt-btn-primary" id="imt-btn-quick-term">+ 录入术语</button>
        <button class="imt-btn" id="imt-btn-reanalyze">🔄 重新分析</button>
        <button class="imt-btn" id="imt-btn-open-panel">⚙️ 控制台</button>
      </div>
    </div>
    <!-- 划词录入悬浮按钮 -->
    <div id="imt-quick-add-btn">📖 录入术语</div>
    <!-- 网页内迷你录入对话框 -->
    <div id="imt-quick-dialog">
      <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #334155; padding-bottom:8px;">
        <span style="font-weight:600; color:#f97316;">➕ 录入/纠正专有术语</span>
        <span style="cursor:pointer; font-size:18px;" id="imt-dialog-close">&times;</span>
      </div>
      <div>
        <label>英文原词 / 缩写 *</label>
        <input type="text" id="imt-input-src" />
      </div>
      <div>
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:2px;">
          <label>准确中文地道译法 *</label>
          <span style="font-size:11px; color:#38bdf8; cursor:pointer;" id="imt-btn-keep-src">⚡ 保持英文不翻译</span>
        </div>
        <input type="text" id="imt-input-tgt" placeholder="输入期望的地道译法，或点击上方保持英文..." />
      </div>
      <div>
        <label>分类 / 语境说明</label>
        <input type="text" id="imt-input-note" placeholder="例如: 篮球防守战术，严禁直译" />
      </div>
      <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:6px;">
        <button class="imt-btn" id="imt-btn-cancel-dialog">取消</button>
        <button class="imt-btn imt-btn-primary" id="imt-btn-save-dialog">立即保存生效</button>
      </div>
    </div>
  `;
  document.body.appendChild(container);

  const capsuleBtn = document.getElementById("imt-capsule-btn");
  const detailCard = document.getElementById("imt-detail-card");
  const statusDot = document.getElementById("imt-status-dot");
  const statusText = document.getElementById("imt-status-text");
  const cardDomain = document.getElementById("imt-card-domain");
  const cardTopic = document.getElementById("imt-card-topic");
  const cardSummary = document.getElementById("imt-card-summary");
  const btnClose = document.getElementById("imt-close-card");
  const btnReanalyze = document.getElementById("imt-btn-reanalyze");
  const btnOpenPanel = document.getElementById("imt-btn-open-panel");
  const btnQuickTerm = document.getElementById("imt-btn-quick-term");

  const quickAddBtn = document.getElementById("imt-quick-add-btn");
  const quickDialog = document.getElementById("imt-quick-dialog");
  const inputSrc = document.getElementById("imt-input-src");
  const inputTgt = document.getElementById("imt-input-tgt");
  const inputNote = document.getElementById("imt-input-note");
  const dialogClose = document.getElementById("imt-dialog-close");
  const btnCancelDialog = document.getElementById("imt-btn-cancel-dialog");
  const btnSaveDialog = document.getElementById("imt-btn-save-dialog");

  // ----------------- 划词一键纠错与术语录入 -----------------
  let selectedText = "";
  document.addEventListener("mouseup", (e) => {
    // 忽略点击内部面板时的划选
    if (container.contains(e.target)) return;

    const sel = window.getSelection();
    const txt = sel ? sel.toString().trim() : "";
    if (txt && txt.length >= 2 && txt.length <= 50 && !txt.includes("\n")) {
      selectedText = txt;
      const x = Math.min(e.pageX + 10, window.innerWidth - 120);
      const y = e.pageY - 35;
      quickAddBtn.style.left = `${x}px`;
      quickAddBtn.style.top = `${y}px`;
      quickAddBtn.style.display = "block";
    } else {
      setTimeout(() => {
        if (!quickDialog.style.display || quickDialog.style.display === "none") {
          quickAddBtn.style.display = "none";
        }
      }, 200);
    }
  });

  function openQuickDialog(presetSrc = "") {
    inputSrc.value = presetSrc;
    inputTgt.value = "";
    inputNote.value = currentContext ? (currentContext.domain || "") : "";
    quickDialog.style.display = "flex";
    quickAddBtn.style.display = "none";
    inputTgt.focus();
  }

  document.getElementById("imt-btn-keep-src").addEventListener("click", () => {
    inputTgt.value = inputSrc.value.trim() || "[保持原文]";
    inputNote.value = inputNote.value || "保持英文原词不翻译";
  });

  quickAddBtn.addEventListener("click", () => {
    openQuickDialog(selectedText);
  });
  btnQuickTerm.addEventListener("click", () => {
    openQuickDialog("");
  });
  dialogClose.addEventListener("click", () => quickDialog.style.display = "none");
  btnCancelDialog.addEventListener("click", () => quickDialog.style.display = "none");

  btnSaveDialog.addEventListener("click", () => {
    const src = inputSrc.value.trim();
    const tgt = inputTgt.value.trim();
    const note = inputNote.value.trim();
    if (!src || !tgt) {
      alert("英文原词和中文译法为必填项！");
      return;
    }

    GM_xmlhttpRequest({
      method: "POST",
      url: `${GATEWAY_URL}/api/terms`,
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify({
        source: src,
        target: tgt,
        category: (currentContext && currentContext.domain) ? currentContext.domain : "通用/快捷录入",
        note: note,
        source_url: window.location.href
      }),
      onload: function (resp) {
        if (resp.status === 200) {
          quickDialog.style.display = "none";
          statusText.innerText = `✅ 术语 "${src}" 已生效`;
          setTimeout(() => {
            const domainShort = (currentContext && currentContext.domain ? currentContext.domain : "语境").split("/")[0].slice(0, 8);
            statusText.innerText = `💡 ${domainShort} (已同步)`;
          }, 2500);
        } else {
          alert("保存术语失败，请检查网关连接状态！");
        }
      },
      onerror: function () {
        alert("无法连接至网关，请确认服务已启动！");
      }
    });
  });

  capsuleBtn.addEventListener("click", () => {
    detailCard.classList.toggle("show");
  });
  btnClose.addEventListener("click", (e) => {
    e.stopPropagation();
    detailCard.classList.remove("show");
  });
  btnOpenPanel.addEventListener("click", () => {
    window.open(GATEWAY_URL, "_blank");
  });
  btnReanalyze.addEventListener("click", () => {
    triggerPageAnalysis(true);
  });

  // ----------------- 内容抓取逻辑 -----------------

  function isReddit() {
    return window.location.hostname.includes("reddit.com");
  }

  function isQuora() {
    return window.location.hostname.includes("quora.com");
  }

  function isYouTube() {
    return window.location.hostname.includes("youtube.com") && window.location.pathname.startsWith("/watch");
  }

  function extractRedditContent() {
    const titleEl = document.querySelector("h1") || document.querySelector("shreddit-title");
    const title = titleEl ? (titleEl.innerText || titleEl.getAttribute("title") || "").trim() : document.title;
    
    // 提取正文内容或首批讨论
    let bodyText = "";
    const postBody = document.querySelector('[data-test-id="post-content"]') || document.querySelector("shreddit-post");
    if (postBody) {
      bodyText += postBody.innerText.slice(0, 1500) + "\n";
    }
    
    // 抓取前几条高赞评论
    const comments = document.querySelectorAll("shreddit-comment, [data-testid='comment']");
    for (let i = 0; i < Math.min(comments.length, 5); i++) {
      bodyText += "Comment: " + comments[i].innerText.slice(0, 300) + "\n";
    }

    return {
      title: title || document.title,
      site_type: "reddit",
      content: bodyText.slice(0, 2500)
    };
  }

  function extractQuoraContent() {
    // 1. 提取 Quora 问题标题
    const questionEl = document.querySelector(".puppeteer_test_question_title") ||
                       document.querySelector("h1") ||
                       document.querySelector(".q-text.qu-dynamicFontSize--regular");
    const title = questionEl ? questionEl.innerText.trim() : document.title.replace(" - Quora", "");

    // 2. 提取问题关联的话题标签 (Topics)
    const topicTags = Array.from(document.querySelectorAll(".q-box.qu-color--gray_light, [href*='/topic/']"))
      .map(el => el.innerText.trim())
      .filter(t => t.length > 1 && !t.includes("Follow"))
      .slice(0, 5)
      .join(", ");

    // 3. 提取前 1-2 位高赞答主的资质认证与回答片段
    let answerContext = "";
    const answerContainers = document.querySelectorAll(".q-box.qu-borderBottom");
    for (let i = 0; i < Math.min(answerContainers.length, 2); i++) {
      const container = answerContainers[i];
      // 答主资质认证 (如 'Doctor of Medicine', 'Software Engineer at Google')
      const credentialEl = container.querySelector(".q-text.qu-color--gray_light");
      const credential = credentialEl ? credentialEl.innerText.trim() : "";
      
      // 回答正文前几段
      const textEl = container.querySelector(".q-relative .q-text") || container.querySelector(".q-box");
      const textSnippet = textEl ? textEl.innerText.slice(0, 600) : "";
      
      if (textSnippet) {
        answerContext += `Answer Author Credential: ${credential || 'General User'}\nAnswer Snippet: ${textSnippet}\n---\n`;
      }
    }

    const fullContent = `Question: ${title}\nTopics: ${topicTags}\n\n${answerContext}`;

    return {
      title: title || document.title,
      site_type: "quora",
      content: fullContent.slice(0, 2500)
    };
  }

  function extractYouTubeContent() {
    const titleEl = document.querySelector("h1.style-scope.ytd-watch-metadata yt-formatted-string") ||
                    document.querySelector("#title h1 yt-formatted-string");
    const title = titleEl ? titleEl.innerText.trim() : document.title.replace(" - YouTube", "");

    const channelEl = document.querySelector("#channel-name yt-formatted-string") ||
                      document.querySelector("#owner-sub-count");
    const channel = channelEl ? channelEl.innerText.trim() : "";

    const descEl = document.querySelector("#description-inline-expander") ||
                   document.querySelector("#description");
    const desc = descEl ? descEl.innerText.slice(0, 1500) : "";

    return {
      title: title || document.title,
      site_type: "youtube",
      content: `Channel: ${channel}\nTitle: ${title}\nDescription: ${desc}`
    };
  }

  function triggerPageAnalysis(force = false) {
    const currentUrl = window.location.href;
    if (!force && currentUrl === lastAnalyzedUrl) return;

    let payload = null;
    if (isReddit()) {
      payload = extractRedditContent();
    } else if (isQuora()) {
      payload = extractQuoraContent();
    } else if (isYouTube()) {
      payload = extractYouTubeContent();
    } else {
      // 其它普通网页
      payload = {
        title: document.title,
        site_type: "web",
        content: document.body ? document.body.innerText.slice(0, 1500) : ""
      };
    }

    if (!payload || !payload.title) return;
    lastAnalyzedUrl = currentUrl;

    // UI 显示分析中
    statusDot.className = "dot syncing";
    statusText.innerText = "⏳ 语境分析中...";

    GM_xmlhttpRequest({
      method: "POST",
      url: `${GATEWAY_URL}/api/analyze`,
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify({
        url: currentUrl,
        title: payload.title,
        site_type: payload.site_type,
        content: payload.content
      }),
      onload: function (res) {
        if (res.status === 200) {
          try {
            const data = JSON.parse(res.responseText);
            currentContext = data.context;
            statusDot.className = "dot";
            
            // 简要标签显示在胶囊中
            const domainShort = (currentContext.domain || "语境").split("/")[0].slice(0, 8);
            statusText.innerText = `💡 ${domainShort} (已同步)`;
            
            // 填充详细卡片
            cardDomain.innerText = currentContext.domain || "通用";
            cardTopic.innerText = currentContext.topic || payload.title;
            cardSummary.innerText = currentContext.summary || "语境背景已就绪，沉浸式翻译将自动获得准确参考。";
          } catch (e) {
            statusText.innerText = "🏀 语境已就绪";
          }
        } else {
          statusDot.className = "dot";
          statusText.innerText = "⚠️ 网关连接未就绪";
        }
      },
      onerror: function (err) {
        statusDot.className = "dot";
        statusText.innerText = "⚠️ 连接失败(点击查看)";
        capsuleBtn.onclick = function() {
          alert(`无法连接到网关：\nURL: ${GATEWAY_URL}/api/analyze\n请检查网络或确认 Tampermonkey 是否允许跨域连接。`);
        };
      }
    });
  }

  // ----------------- 触发监控 -----------------

  // 页面初次加载检测
  setTimeout(() => {
    triggerPageAnalysis(false);
  }, 2000);

  // 针对 YouTube SPA 视频切换事件
  window.addEventListener("yt-navigate-finish", () => {
    setTimeout(() => triggerPageAnalysis(false), 1500);
  });

  // 针对 Reddit SPA 切换帖子 (popstate / pushState)
  let lastHref = location.href;
  setInterval(() => {
    if (location.href !== lastHref) {
      lastHref = location.href;
      setTimeout(() => triggerPageAnalysis(false), 1500);
    }
  }, 2000);

})();
