"use strict";

// 清除旧版表单提交可能遗留的凭据查询参数，不读取或记录其值。
const pageUrl = new URL(window.location.href);
if (pageUrl.searchParams.has("username") || pageUrl.searchParams.has("password")) {
  pageUrl.searchParams.delete("username");
  pageUrl.searchParams.delete("password");
  window.history.replaceState(null, "", pageUrl.pathname + pageUrl.search + pageUrl.hash);
}

const $ = (selector) => document.querySelector(selector);
const state = { chatBusy: false, uploadBusy: false, file: null, chunks: [], adminToken: "", previewName: "", user: null, conversationId: null, historyBusy: false, registrationMode: false, authBusy: false };
let toastTimer;

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, 4500);
}

function switchView(view) {
  const isChat = view === "chat";
  $("#chat-view").hidden = !isChat;
  $("#documents-view").hidden = isChat;
  $("#breadcrumb-current").textContent = isChat ? "问答助手" : "公司知识库";
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (window.matchMedia("(max-width: 760px)").matches) setSidebar(false);
  if (!isChat) loadLibrary();
}
document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));

function setSidebar(expanded) {
  const mobile = window.matchMedia("(max-width: 760px)").matches;
  $(".workspace").classList.toggle("sidebar-open", mobile && expanded);
  $(".workspace").classList.toggle("sidebar-collapsed", !mobile && !expanded);
  $("#sidebar").inert = !expanded;
  $("#sidebar-toggle").setAttribute("aria-expanded", String(expanded));
  $("#sidebar-toggle").setAttribute("aria-label", expanded ? "收起导航" : "展开导航");
  $("#sidebar-backdrop").hidden = !mobile || !expanded;
}
setSidebar(!window.matchMedia("(max-width: 760px)").matches);
$("#sidebar-toggle").addEventListener("click", () => setSidebar($("#sidebar-toggle").getAttribute("aria-expanded") !== "true"));
$("#sidebar-backdrop").addEventListener("click", () => { setSidebar(false); $("#sidebar-toggle").focus(); });
window.matchMedia("(max-width: 760px)").addEventListener("change", (event) => setSidebar(!event.matches));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && window.matchMedia("(max-width: 760px)").matches) {
    setSidebar(false);
    $("#sidebar-toggle").focus();
  }
});

async function requestJSON(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 90000);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      if (response.status === 401 && !url.startsWith("/auth/")) resetAccountUI();
      const detail = payload?.detail;
      const message = typeof detail === "string" ? detail : Array.isArray(detail)
        ? "输入内容不符合要求，请检查后重试。" : `请求失败（${response.status}），请稍后重试。`;
      throw new Error(message);
    }
    if (payload === null) throw new Error("服务未返回有效结果，请稍后重试。");
    return payload;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("等待响应超时，请稍后重试。");
    if (error instanceof TypeError) throw new Error("无法连接服务，请确认服务已启动。");
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

async function checkHealth() {
  const status = $("#service-status");
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    let response;
    try { response = await fetch("/health", { signal: controller.signal, cache: "no-store" }); }
    finally { clearTimeout(timeout); }
    const payload = await response.json();
    if (!response.ok || payload.status !== "healthy") throw new Error("unhealthy");
    status.className = "service-status online";
    status.querySelector("span").textContent = "服务已连接";
  } catch {
    status.className = "service-status offline";
    status.querySelector("span").textContent = "服务未连接";
  }
}
checkHealth();
setInterval(checkHealth, 30000);

function addMessage(role, text, pending = false) {
  const message = document.createElement("article");
  message.className = `message ${role}${pending ? " pending" : ""}`;
  const avatar = document.createElement("span");
  avatar.className = "message-avatar";
  avatar.textContent = role === "user" ? "你" : "✧";
  const content = document.createElement("div");
  content.className = "message-content";
  const label = document.createElement("span");
  label.className = "message-label";
  label.textContent = role === "user" ? "你的问题" : "企业制度助手";
  const body = document.createElement("div");
  body.className = "message-body";
  body.textContent = text;
  content.append(label, body);
  message.append(avatar, content);
  $("#conversation").append(message);
  $("#conversation").scrollTop = $("#conversation").scrollHeight;
  return message;
}

$("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.chatBusy || state.historyBusy || !state.user) return;
  const question = $("#question").value.trim();
  if (!question) { toast("先写下你的问题吧。"); $("#question").focus(); return; }
  state.chatBusy = true;
  $("#send-button").disabled = true;
  $("#new-chat").disabled = true;
  $("#question").disabled = true;
  $("#welcome").hidden = true;
  $("#conversation").hidden = false;
  $("#chat-view").classList.add("has-conversation");
  addMessage("user", question);
  $("#question").value = "";
  const answer = addMessage("assistant", "正在整理回答…", true);
  try {
    const result = await requestJSON("/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, conversation_id: state.conversationId }) });
    if (typeof result.answer !== "string" || !result.answer.trim()) throw new Error("助手未返回有效回答，请重试。");
    answer.querySelector(".message-body").textContent = result.answer;
    if (Array.isArray(result.sources) && result.sources.length) renderSources(answer, result.sources);
    state.conversationId = result.conversation_id;
    await loadHistory();
  } catch (error) {
    answer.classList.add("error");
    answer.querySelector(".message-body").textContent = error.message;
    $("#question").value = question;
    if (state.user) await loadHistory();
  } finally {
    answer.classList.remove("pending");
    state.chatBusy = false;
    $("#send-button").disabled = false;
    $("#new-chat").disabled = false;
    $("#question").disabled = false;
    $("#conversation").scrollTop = $("#conversation").scrollHeight;
    if (!$("#chat-view").hidden) $("#question").focus();
  }
});

$("#question").addEventListener("keydown", (event) => {
  if (!event.isComposing && event.keyCode !== 229 && event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#chat-form").requestSubmit();
  }
});
$("#new-chat").addEventListener("click", () => {
  if (state.chatBusy || state.historyBusy) return;
  state.conversationId = null;
  document.querySelectorAll(".history-item").forEach((row) => row.classList.remove("active"));
  $("#conversation").replaceChildren();
  $("#conversation").hidden = true;
  $("#welcome").hidden = false;
  $("#chat-view").classList.remove("has-conversation");
  switchView("chat");
  $("#question").value = "";
  $("#question").focus();
});

function clearPreview() {
  state.chunks = [];
  $("#preview-panel").hidden = true;
  $("#chunk-list").replaceChildren();
  $("#chunk-search").value = "";
  $("#no-results").hidden = true;
  $("#upload-status").textContent = "";
  $("#upload-status").classList.remove("error");
  state.previewName = "";
}

function selectFile(file) {
  if (state.uploadBusy || !file) return;
  if (!file.name.toLowerCase().endsWith(".docx")) {
    $("#document-file").value = "";
    toast("请选择 .docx 格式的 Word 文档。");
    return;
  }
  state.file = file;
  clearPreview();
  $("#selected-file").hidden = false;
  $("#file-name").textContent = file.name;
  $("#file-size").textContent = file.size >= 1024 * 1024 ? `${(file.size / (1024 * 1024)).toFixed(2)} MB` : `${(file.size / 1024).toFixed(1)} KB`;
}
$("#document-file").addEventListener("change", (event) => selectFile(event.target.files[0]));
$("#remove-file").addEventListener("click", () => {
  if (state.uploadBusy) return;
  state.file = null;
  $("#document-file").value = "";
  $("#selected-file").hidden = true;
  clearPreview();
});
["dragenter", "dragover"].forEach((name) => $("#dropzone").addEventListener(name, (event) => {
  event.preventDefault();
  if (!state.uploadBusy) $("#dropzone").classList.add("dragover");
}));
["dragleave", "drop"].forEach((name) => $("#dropzone").addEventListener(name, (event) => {
  event.preventDefault();
  $("#dropzone").classList.remove("dragover");
}));
$("#dropzone").addEventListener("drop", (event) => selectFile(event.dataTransfer.files[0]));
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => event.preventDefault());

function appendHighlightedText(element, text, query) {
  if (!query) {
    element.textContent = text;
    return;
  }

  const lowerText = text.toLocaleLowerCase();
  let start = 0;
  let position = lowerText.indexOf(query);

  while (position !== -1) {
    element.append(document.createTextNode(text.slice(start, position)));

    const mark = document.createElement("mark");
    mark.textContent = text.slice(position, position + query.length);
    element.append(mark);

    start = position + query.length;
    position = lowerText.indexOf(query, start);
  }

  element.append(document.createTextNode(text.slice(start)));
}

function renderChunks() {
  const query = $("#chunk-search").value.trim().toLocaleLowerCase();
  const matching = state.chunks.filter((chunk) => {
    const searchable = [
      chunk.content,
      ...(chunk.section_path || []),
    ].join("\n").toLocaleLowerCase();

    return searchable.includes(query);
  });

  const fragment = document.createDocumentFragment();

  matching.forEach((chunk) => {
    const article = document.createElement("article");
    article.className = "chunk-card";

    const header = document.createElement("header");
    const number = document.createElement("span");
    number.textContent = `知识块 ${chunk.chunk_number}`;
    const name = document.createElement("span");
    name.textContent = chunk.document_name;
    header.append(number, name);

    const section = document.createElement("p");
    section.className = "chunk-section";
    appendHighlightedText(
      section,
      (chunk.section_path || []).join(" › ") || "未识别章节",
      query,
    );

    const location = document.createElement("p");
    location.className = "chunk-location";
    location.textContent = chunk.source_start == null
      ? "原文位置未记录"
      : `正文元素 ${chunk.source_start}～${chunk.source_end}`;

    if (chunk.source_type === "table" && chunk.table_row != null) {
      location.textContent += ` · 表格第 ${chunk.table_row} 行`;
    }

    // 搜索时展示第一次命中附近的内容，最多约 180 字符。
    const position = query
      ? chunk.content.toLocaleLowerCase().indexOf(query)
      : 0;
    const start = Math.max(0, position - 50);
    const end = Math.min(chunk.content.length, start + 180);
    const excerpt = (
      (start > 0 ? "…" : "")
      + chunk.content.slice(start, end)
      + (end < chunk.content.length ? "…" : "")
    );

    const preview = document.createElement("p");
    appendHighlightedText(preview, excerpt, query);

    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "展开原文";
    const fullText = document.createElement("p");
    appendHighlightedText(fullText, chunk.content, query);
    details.append(summary, fullText);

    article.append(header, section, location, preview, details);
    fragment.append(article);
  });

  $("#chunk-list").replaceChildren(fragment);
  $("#no-results").hidden = matching.length !== 0;
  $("#chunk-count").textContent = query
    ? `${matching.length} / ${state.chunks.length} 块`
    : `共 ${state.chunks.length} 块`;
}
$("#chunk-search").addEventListener("input", renderChunks);

$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.uploadBusy) return;
  if (!state.file) { toast("请先选择一份 Word 文档。"); return; }
  if (!state.adminToken) { toast("请先验证管理员密钥。"); return; }
  const file = state.file;
  clearPreview();
  state.uploadBusy = true;
  $("#upload-button").disabled = true;
  $("#remove-file").disabled = true;
  $("#document-file").disabled = true;
  $("#upload-button").classList.add("busy");
  $("#upload-status").textContent = "正在提取正文并保存到知识库…";
  const form = new FormData();
  form.append("file", file);
  form.append("replace_existing", String($("#replace-existing").checked));
  try {
    const document = await requestJSON("/documents/upload", { method: "POST", body: form, headers: adminHeaders() });
    const chunks = await requestJSON(`/documents/${encodeURIComponent(document.document_id)}/chunks`);
    if (!Array.isArray(chunks) || !chunks.length || chunks.some((chunk) => !chunk || typeof chunk.content !== "string" || typeof chunk.document_name !== "string" || !Number.isInteger(chunk.chunk_number))) throw new Error("服务返回的段落格式不正确，请重试。");
    state.chunks = chunks;
    state.previewName = document.document_name;
    $("#preview-panel").hidden = false;
    $("#upload-status").textContent = document.already_exists ? "相同内容已在知识库中，未重复导入。" : `导入成功，已保存 ${chunks.length} 个段落，可以开始提问。`;
    renderChunks();
    await loadLibrary();
  } catch (error) {
    $("#upload-status").textContent = error.message;
    $("#upload-status").classList.add("error");
  } finally {
    state.uploadBusy = false;
    $("#upload-button").disabled = false;
    $("#remove-file").disabled = false;
    $("#document-file").disabled = false;
    $("#upload-button").classList.remove("busy");
  }
});

$("#download-preview").addEventListener("click", () => {
  if (!state.chunks.length) return;
  const blob = new Blob([JSON.stringify(state.chunks, null, 2)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${state.previewName.replace(/\.docx$/i, "") || "知识库文档"}-段落预览.json`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

function adminHeaders() {
  return { "X-Admin-Token": state.adminToken };
}

function renderSources(message, sources) {
  const container = document.createElement("div");
  container.className = "answer-sources";
  const label = document.createElement("span");
  label.textContent = "回答引用的原文";
  container.append(label);
  sources.forEach((source) => {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `[${source.reference_number}] ${source.document_name} · 段落 ${source.chunk_number}`;
    const text = document.createElement("p");
    text.textContent = source.content;
    details.append(summary, text);
    container.append(details);
  });
  message.querySelector(".message-content").append(container);
}

async function loadLibrary() {
  $("#library-status").textContent = "正在读取文档…";
  try {
    const documents = await requestJSON("/documents");
    if (!Array.isArray(documents)) throw new Error("文档目录格式不正确。");
    $("#library-count").textContent = `共 ${documents.length} 份`;
    $("#knowledge-status").textContent = documents.length
      ? `依据 ${documents.length} 份公司文档回答 · 支持当前对话内追问`
      : "知识库还没有文档，请由管理员先导入。";
    $("#library-status").textContent = documents.length ? "" : "还没有文档。管理员导入后，所有使用者都可以查询。";
    const fragment = document.createDocumentFragment();
    documents.forEach((document) => {
      const row = window.document.createElement("div");
      row.className = "library-document";
      const name = window.document.createElement("button");
      name.type = "button";
      name.className = "library-document-name";
      name.textContent = document.document_name;
      name.addEventListener("click", async () => {
        if (state.uploadBusy) return;
        clearPreview();
        try {
          state.chunks = await requestJSON(`/documents/${encodeURIComponent(document.document_id)}/chunks`);
          state.previewName = document.document_name;
          $("#preview-panel").hidden = false;
          renderChunks();
          $("#preview-panel").scrollIntoView({ behavior: "smooth", block: "start" });
        } catch (error) { toast(error.message); }
      });
      const meta = window.document.createElement("span");
      meta.className = "library-document-meta";
      meta.textContent = `${document.chunk_count} 段 · ${new Date(document.updated_at).toLocaleDateString("zh-CN")}`;
      row.append(name, meta);
      if (state.adminToken) {
        const remove = window.document.createElement("button");
        remove.type = "button";
        remove.className = "text-button delete-document";
        remove.textContent = "删除";
        remove.setAttribute("aria-label", `删除 ${document.document_name}`);
        remove.addEventListener("click", async () => {
          if (!confirm(`删除“${document.document_name}”？该文档将不再用于问答。`)) return;
          remove.disabled = true;
          try {
            await requestJSON(`/documents/${encodeURIComponent(document.document_id)}`, { method: "DELETE", headers: adminHeaders() });
            clearPreview();
            await loadLibrary();
            toast("文档已删除。");
          } catch (error) { toast(error.message); remove.disabled = false; }
        });
        row.append(remove);
      }
      fragment.append(row);
    });
    $("#document-list").replaceChildren(fragment);
  } catch (error) {
    $("#library-status").textContent = error.message;
    $("#knowledge-status").textContent = "知识库暂时无法连接。";
  }
}

$("#admin-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = $("#admin-token").value.trim();
  if (!token) return;
  const button = $("#admin-form button");
  button.disabled = true;
  try {
    await requestJSON("/admin/check", { headers: { "X-Admin-Token": token } });
    state.adminToken = token;
    $("#admin-token").value = "";
    $("#admin-form").hidden = true;
    $("#admin-session").hidden = false;
    $("#upload-form").hidden = false;
    await loadLibrary();
  } catch (error) {
    $("#admin-status").textContent = error.message;
  } finally { button.disabled = false; }
});

$("#admin-logout").addEventListener("click", () => {
  if (state.uploadBusy) { toast("请等待导入完成后退出。"); return; }
  state.adminToken = "";
  $("#admin-form").hidden = false;
  $("#admin-session").hidden = true;
  $("#upload-form").hidden = true;
  $("#admin-status").textContent = "已退出管理，密钥已从当前页面清除。";
  $("#document-list").querySelectorAll(".delete-document").forEach((button) => button.remove());
  loadLibrary();
});
$("#refresh-library").addEventListener("click", loadLibrary);

function resetAccountUI() {
  state.user = null;
  state.conversationId = null;
  state.adminToken = "";
  state.file = null;
  state.registrationMode = false;
  $("#auth-title").textContent = "登录企业制度助手";
  $("#login-submit").textContent = "登录";
  $("#auth-toggle").textContent = "没有账号？创建账号";
  $("#confirm-password-area").hidden = true;
  $("#confirm-password").required = false;
  $("#login-password").autocomplete = "current-password";
  $(".workspace").hidden = true;
  $("#auth-view").hidden = false;
  $("#conversation").replaceChildren();
  $("#conversation").hidden = true;
  $("#welcome").hidden = false;
  $("#chat-view").classList.remove("has-conversation");
  $("#history-list").replaceChildren();
  $("#document-list").replaceChildren();
  $("#question").value = "";
  $("#admin-token").value = "";
  $("#admin-form").hidden = false;
  $("#admin-session").hidden = true;
  $("#upload-form").hidden = true;
  $("#document-file").value = "";
  $("#selected-file").hidden = true;
  clearPreview();
}

async function loadHistory() {
  const owner = state.user?.user_id;
  if (!owner) return [];
  try {
    const conversations = await requestJSON("/conversations");
    if (state.user?.user_id !== owner) return [];
    const fragment = document.createDocumentFragment();
    conversations.forEach((conversation) => {
      const row = document.createElement("div");
      row.className = `history-item${conversation.conversation_id === state.conversationId ? " active" : ""}`;
      const title = document.createElement("button");
      title.type = "button";
      title.className = "history-title";
      title.textContent = conversation.title;
      title.title = conversation.title;
      title.addEventListener("click", () => openConversation(conversation.conversation_id));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "history-delete";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `删除对话：${conversation.title}`);
      remove.addEventListener("click", async () => {
        if (state.chatBusy || state.historyBusy) return;
        if (!confirm("删除这段对话及其中的所有记录？")) return;
        remove.disabled = true;
        try {
          await requestJSON(`/conversations/${encodeURIComponent(conversation.conversation_id)}`, { method: "DELETE" });
          if (state.user?.user_id !== owner) return;
          if (state.conversationId === conversation.conversation_id) $("#new-chat").click();
          await loadHistory();
        } catch (error) { toast(error.message); remove.disabled = false; }
      });
      row.append(title, remove);
      fragment.append(row);
    });
    $("#history-list").replaceChildren(fragment);
    $("#history-status").textContent = conversations.length ? "" : "还没有对话，开始提问吧。";
    return conversations;
  } catch (error) {
    if (state.user?.user_id === owner) $("#history-status").textContent = error.message;
    return [];
  }
}

async function openConversation(conversationId) {
  if (state.chatBusy || state.historyBusy || !state.user) return;
  const owner = state.user.user_id;
  state.historyBusy = true;
  $("#question").disabled = true;
  $("#send-button").disabled = true;
  try {
    const conversation = await requestJSON(`/conversations/${encodeURIComponent(conversationId)}`);
    if (state.user?.user_id !== owner) return;
    state.conversationId = conversationId;
    $("#conversation").replaceChildren();
    $("#conversation").hidden = false;
    $("#welcome").hidden = true;
    $("#chat-view").classList.add("has-conversation");
    conversation.turns.forEach((turn) => {
      addMessage("user", turn.question);
      const answer = addMessage("assistant", turn.answer || "这次回答未完成，可以重新提问。");
      if (turn.status !== "complete") answer.classList.add("error");
      if (turn.sources.length) renderSources(answer, turn.sources);
    });
    switchView("chat");
    $("#question").value = "";
    await loadHistory();
  } catch (error) { toast(error.message); }
  finally {
    state.historyBusy = false;
    $("#question").disabled = false;
    $("#send-button").disabled = false;
  }
}

async function enterAccount(user) {
  resetAccountUI();
  state.user = user;
  $("#account-name").textContent = user.username;
  $("#auth-view").hidden = true;
  $(".workspace").hidden = false;
  $("#login-password").value = "";
  $("#confirm-password").value = "";
  switchView("chat");
  await loadLibrary();
  const conversations = await loadHistory();
  if (state.user?.user_id === user.user_id && conversations.length) await openConversation(conversations[0].conversation_id);
}

$("#auth-toggle").addEventListener("click", () => {
  if (state.authBusy) return;
  state.registrationMode = !state.registrationMode;
  $("#auth-title").textContent = state.registrationMode ? "创建你的账号" : "登录企业制度助手";
  $("#login-submit").textContent = state.registrationMode ? "创建账号并登录" : "登录";
  $("#auth-toggle").textContent = state.registrationMode ? "已有账号？返回登录" : "没有账号？创建账号";
  $("#confirm-password-area").hidden = !state.registrationMode;
  $("#confirm-password").required = state.registrationMode;
  $("#login-password").autocomplete = state.registrationMode ? "new-password" : "current-password";
  $("#login-status").textContent = "";
});

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.authBusy) return;
  const password = $("#login-password").value;
  if (state.registrationMode && password !== $("#confirm-password").value) {
    $("#login-status").textContent = "两次输入的密码不一致。";
    return;
  }
  state.authBusy = true;
  $("#login-submit").disabled = true;
  $("#login-status").textContent = "正在验证…";
  try {
    const user = await requestJSON(state.registrationMode ? "/auth/register" : "/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("#login-username").value.trim(), password }),
    });
    await enterAccount(user);
    $("#login-status").textContent = "";
  } catch (error) { $("#login-status").textContent = error.message; }
  finally { state.authBusy = false; $("#login-submit").disabled = false; }
});

$("#account-logout").addEventListener("click", async () => {
  if (state.chatBusy || state.uploadBusy || state.historyBusy || state.authBusy) {
    toast("请等待当前操作完成后退出。");
    return;
  }
  $("#account-logout").disabled = true;
  try {
    await requestJSON("/auth/logout", { method: "POST" });
    resetAccountUI();
    $("#login-password").value = "";
    $("#confirm-password").value = "";
    $("#login-status").textContent = "已退出登录。";
  } catch (error) { toast(error.message); }
  finally { $("#account-logout").disabled = false; }
});

async function restoreAccount() {
  try {
    const status = await requestJSON("/auth/status");
    $("#auth-toggle").hidden = !status.registration_enabled;
    $("#login-status").textContent = "";
    if (status.user) await enterAccount(status.user);
  } catch (error) { $("#login-status").textContent = error.message; }
  finally { $("#login-submit").disabled = false; }
}
restoreAccount();
