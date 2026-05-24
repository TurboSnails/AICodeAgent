/**
 * AICodeAgent — Chat Interface
 * 单页聊天 UI，支持：
 *   - SSE 实时流（任务状态 + 编码日志）
 *   - 图片上传（粘贴 / 拖拽 / 文件选择）
 *   - 内联回复（澄清问题 / L2 核准）
 */

/* ── 状态 ── */
let tasks      = [];
let activeId   = null;   // 当前选中任务 ID
let activeTask = null;   // 当前任务对象
let sseSource  = null;   // EventSource
let lastState  = null;   // 上次渲染的状态
let pendingImgs = [];    // 待上传图片 [{name, data, mime_type, objUrl}]

/* ── DOM refs ── */
const $  = id => document.getElementById(id);
const el = {
  healthDot:     () => $('healthDot'),
  sidebar:       () => $('sidebarTasks'),
  welcome:       () => $('chatWelcome'),
  thread:        () => $('chatThread'),
  messages:      () => $('chatMessages'),
  headerTitle:   () => $('chatHeaderTitle'),
  headerStatus:  () => $('chatHeaderStatus'),
  headerActions: () => $('chatHeaderActions'),
  attachPrev:    () => $('attachmentPreview'),
  msgInput:      () => $('msgInput'),
  levelSel:      () => $('levelSelect'),
  btnSend:       () => $('btnSend'),
  btnAttach:     () => $('btnAttach'),
  fileInput:     () => $('fileInput'),
  inputHint:     () => $('inputHint'),
};

/* ── 状态标签 ── */
const STATE_LABEL = {
  pending:'排队中', planning:'规划中', debating:'方案讨论',
  consensus:'共识', architect_planning:'架构规划',
  waiting_gate:'待你确认', waiting_clarification:'待你回复',
  direct_answer:'生成回答', design_output:'输出方案',
  coding:'正在编码', building:'正在构建',
  self_review:'自审', codex_review:'逻辑审查',
  architect_review:'架构评审', red_team_review:'安全审查',
  requirement_review:'需求审查', correcting:'修复中',
  git_committing:'提交代码', creating_pr:'创建PR',
  notifying:'通知', completed:'已完成',
  failed:'失败', cancelled:'已取消',
};
function stateLabel(s){ return STATE_LABEL[s] || s; }

const TERMINAL = new Set(['completed','failed','cancelled']);
const WAITING  = new Set(['waiting_gate','waiting_clarification']);

/* ── 启动 ── */
async function init() {
  // 事件绑定
  const inp = el.msgInput();
  inp.addEventListener('paste',  onPaste);
  inp.addEventListener('input',  () => autoResize(inp));
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      onSend();
    }
  });
  inp.addEventListener('dragover', e => e.preventDefault());
  inp.addEventListener('drop', e => {
    e.preventDefault();
    Array.from(e.dataTransfer.files)
      .filter(f => f.type.startsWith('image/'))
      .forEach(addImgFile);
  });

  await loadTasks();
  setInterval(loadTasks, 6000);

  checkHealth();
  setInterval(checkHealth, 12000);

  // 恢复 URL hash
  const hash = location.hash.replace('#', '');
  if (hash) selectTask(hash);
}

/* ── 健康检查 ── */
async function checkHealth() {
  try {
    const r = await fetch('/health', {signal: AbortSignal.timeout(4000)});
    el.healthDot().classList.toggle('offline', !r.ok);
  } catch {
    el.healthDot().classList.add('offline');
  }
}

/* ── 任务列表 ── */
async function loadTasks() {
  try {
    const data = await apiGet('/tasks');
    tasks = data.tasks || [];
    renderSidebar();

    // 更新 active task（若未在 SSE 流中）
    if (activeId && !sseSource) {
      const t = tasks.find(t => t.task_id === activeId);
      if (t) { activeTask = t; updateHeader(); }
    }
  } catch {}
}

function renderSidebar() {
  const sorted = [...tasks].sort((a, b) =>
    (b.updated_at||'').localeCompare(a.updated_at||''));

  if (!sorted.length) {
    el.sidebar().innerHTML = '<div class="sidebar-empty">暂无任务</div>';
    return;
  }

  el.sidebar().innerHTML = sorted.map(t => {
    const dot   = dotClass(t.current_state);
    const title = (t.raw_requirement || t.task_id).slice(0, 38)
                  + (t.raw_requirement?.length > 38 ? '…' : '');
    const meta  = timeAgo(t.updated_at || t.created_at);
    const act   = t.task_id === activeId ? ' active' : '';
    return `<div class="task-item${act}" data-id="${t.task_id}" onclick="selectTask('${t.task_id}')">
      <div class="task-item-header">
        <div class="task-status-dot ${dot}"></div>
        <div class="task-item-title">${esc(title)}</div>
      </div>
      <div class="task-item-meta">${esc(stateLabel(t.current_state))} · ${meta}</div>
    </div>`;
  }).join('');
}

function dotClass(state) {
  if (WAITING.has(state))    return 'dot-waiting';
  if (state === 'completed') return 'dot-done';
  if (state === 'failed' || state === 'cancelled') return 'dot-failed';
  if (TERMINAL.has(state))   return 'dot-idle';
  return 'dot-running';
}

/* ── 选择任务 ── */
function selectTask(id) {
  activeId   = id;
  activeTask = tasks.find(t => t.task_id === id) || null;

  history.replaceState(null, '', '#' + id);

  // Sidebar highlight
  document.querySelectorAll('.task-item').forEach(n => {
    n.classList.toggle('active', n.dataset.id === id);
  });

  // 切换视图
  el.welcome().style.display = 'none';
  el.thread().style.display  = 'flex';

  // 清空并重建消息
  el.messages().innerHTML = '';
  lastState = null;

  if (activeTask) {
    buildThread(activeTask);
  } else {
    // 从 API 拉一次
    apiGet(`/task/${id}`).then(d => {
      activeTask = d.task;
      buildThread(activeTask);
      startSSE(id);
    });
    return;
  }

  startSSE(id);
  updateInputBar();
}

/* ── 新建任务 ── */
function startNewTask() {
  stopSSE();
  activeId   = null;
  activeTask = null;
  lastState  = null;

  document.querySelectorAll('.task-item').forEach(n => n.classList.remove('active'));
  history.replaceState(null, '', location.pathname);

  el.welcome().style.display = '';
  el.thread().style.display  = 'none';
  el.messages().innerHTML    = '';
  updateInputBar();
  el.msgInput().focus();
}

/* ── Tip chips ── */
function fillTip(node) {
  el.msgInput().value = node.textContent.trim();
  el.msgInput().focus();
  autoResize(el.msgInput());
}

/* ═══════════════════════════════════
   Thread rendering
═══════════════════════════════════ */

/** 从任务对象重建完整对话线索 */
function buildThread(task) {
  // 1. 用户消息
  appendUserMsg(task.raw_requirement, task.image_urls || []);

  // 2. 状态对应的 AI 消息
  renderStateBlock(task);

  updateHeader();
}

/**
 * 根据 current_state 渲染（或替换）AI 侧的消息块。
 * 终态已存在就不重复添加。
 */
function renderStateBlock(task) {
  const state = task.current_state;

  // 移除上一个 AI 状态块（id="ai-block"）
  $('ai-block')?.remove();
  $('ai-clarify-block')?.remove();
  $('ai-gate-block')?.remove();

  if (state === 'waiting_gate') {
    appendThinking('规划', '📐');
    appendGateBlock(task);
  } else if (state === 'waiting_clarification') {
    appendThinking('规划', '📐');
    appendClarifyBlock(task);
  } else if (state === 'completed') {
    appendDoneBlock(task);
  } else if (state === 'failed') {
    appendErrorBlock(task);
  } else if (state === 'cancelled') {
    appendAI(`<div class="msg-phase-label">— 已取消</div>`, 'ai-block');
  } else {
    // 进行中
    appendActiveBlock(state, task);
  }
}

/* 进行中的阶段 */
const PHASE_META = {
  pending:             { icon:'⏳', label:'排队中…' },
  planning:            { icon:'📐', label:'规划需求…' },
  debating:            { icon:'💬', label:'方案讨论…' },
  consensus:           { icon:'🤝', label:'达成共识…' },
  architect_planning:  { icon:'🏗️', label:'架构规划…' },
  coding:              { icon:'💻', label:'正在编码…' },
  building:            { icon:'🔨', label:'构建中…' },
  self_review:         { icon:'🔍', label:'自我审查…' },
  codex_review:        { icon:'🔬', label:'逻辑审查…' },
  architect_review:    { icon:'🏛️', label:'架构评审…' },
  red_team_review:     { icon:'🛡️', label:'安全审查…' },
  requirement_review:  { icon:'📋', label:'需求审查…' },
  correcting:          { icon:'🔧', label:'修复问题…' },
  git_committing:      { icon:'📦', label:'提交代码…' },
  creating_pr:         { icon:'🔀', label:'创建 PR…' },
  notifying:           { icon:'📬', label:'发送通知…' },
  direct_answer:       { icon:'💡', label:'生成回答…' },
  design_output:       { icon:'📐', label:'输出方案…' },
};

function appendActiveBlock(state, task) {
  const p = PHASE_META[state] || { icon:'⚡', label: stateLabel(state) + '…' };
  const log = task.cli_feedback
    ? `<div class="msg-log" id="liveLog">${esc(task.cli_feedback)}</div>` : '';

  const html = `
    <div class="msg-phase-label">${p.icon} ${esc(p.label)}</div>
    <div class="thinking-dots"><span></span><span></span><span></span></div>
    ${log}`;
  appendAI(html, 'ai-block');
}

/* AI 思考气泡（规划中占位） */
function appendThinking(label, icon) {
  const html = `
    <div class="msg-phase-label">${icon} ${esc(label)}中…</div>
    <div class="thinking-dots"><span></span><span></span><span></span></div>`;
  appendAI(html, 'thinking-placeholder');
}

/* L2 核准 */
function appendGateBlock(task) {
  const plan = esc(task.phase_detail || task.ai_output_preview || '（查看工作区了解规划详情）');
  const html = `
    <div class="gate-title">⚠️ 这是大改动，需要你确认后才开始编码</div>
    <div class="gate-plan">${plan}</div>
    <div class="gate-btns">
      <button class="btn-gate-ok" onclick="approveGate('${task.task_id}')">✅ 确认继续</button>
      <button class="btn-gate-cancel" onclick="cancelTask('${task.task_id}')">❌ 取消任务</button>
    </div>`;
  appendAISpecial(html, 'ai-gate-block', 'msg-gate');
}

/* 澄清问题 */
function appendClarifyBlock(task) {
  const qs = task.clarification_questions || [];
  const qHtml = qs.map((q, i) =>
    `<li class="clarify-q" onclick="fillClarify(${i})">${i+1}. ${esc(q)}</li>`
  ).join('');
  const html = `
    <div class="clarify-title">💬 有几个问题需要你回答</div>
    ${qHtml ? `<ul class="clarify-qs">${qHtml}</ul>` : ''}
    <div class="clarify-reply-row">
      <textarea id="clarifyTA" class="clarify-textarea" rows="2"
        placeholder="回答上面的问题，可粘贴图片…"
        onkeydown="if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();sendClarify('${task.task_id}');}"></textarea>
      <button class="btn-clarify-send" onclick="sendClarify('${task.task_id}')">发送</button>
    </div>`;
  appendAISpecial(html, 'ai-clarify-block', 'msg-clarify');
}

/* 完成 */
function appendDoneBlock(task) {
  let body = '';
  if (task.artifact_content) {
    const note = task.artifact_truncated
      ? '<div style="font-size:11px;color:#6b7280;margin-bottom:4px">内容已截断</div>' : '';
    body += `${note}<div class="done-artifact">${esc(task.artifact_content)}</div>`;
  }
  if (task.pr_url) {
    body += `<a class="done-pr-link" href="${esc(task.pr_url)}" target="_blank" rel="noopener">🔀 查看 PR</a>`;
  }
  if (task.branch) {
    body += `<div class="done-branch">📌 分支：${esc(task.branch)}</div>`;
  }
  if (!body) body = '<div class="msg-text">代码已提交</div>';
  const html = `<div class="done-title">✅ 任务完成</div>${body}`;
  appendAISpecial(html, 'ai-block', 'msg-done');
}

/* 失败 */
function appendErrorBlock(task) {
  const detail = task.phase_detail || task.ai_output_preview || '';
  const html = `
    <div class="error-title">❌ 任务失败</div>
    ${detail ? `<div class="error-detail">${esc(detail)}</div>` : ''}`;
  appendAISpecial(html, 'ai-block', 'msg-error');
}

/* ─ DOM 辅助 ─ */

function appendUserMsg(text, imageUrls = []) {
  const imgs = (imageUrls || []).map(u =>
    `<img src="${esc(u)}" class="msg-img" onclick="lightbox('${esc(u)}')">`
  ).join('');
  const node = mkMsg('msg-user', `
    <div class="msg-bubble">
      <div class="msg-text">${esc(text)}</div>
      ${imgs ? `<div class="msg-imgs">${imgs}</div>` : ''}
    </div>
    <div class="msg-avatar avatar-user">👤</div>`);
  el.messages().appendChild(node);
  scrollBottom();
}

function appendUserReply(text) {
  const node = mkMsg('msg-user', `
    <div class="msg-bubble">
      <div class="msg-text">${esc(text)}</div>
    </div>
    <div class="msg-avatar avatar-user">👤</div>`);
  el.messages().appendChild(node);
  scrollBottom();
}

/** 普通 AI 气泡（可更新） */
function appendAI(innerHtml, domId) {
  let node = domId ? $(domId) : null;
  if (node) {
    node.querySelector('.msg-bubble').innerHTML = innerHtml;
  } else {
    node = mkMsg('msg-ai', `
      <div class="msg-avatar avatar-ai">🤖</div>
      <div class="msg-bubble">${innerHtml}</div>`);
    if (domId) node.id = domId;
    el.messages().appendChild(node);
  }
  scrollBottom();
}

/** 特殊 AI 气泡（gate / clarify / done / error） */
function appendAISpecial(innerHtml, domId, extraCls) {
  let node = domId ? $(domId) : null;
  if (node) {
    node.querySelector('.msg-bubble').innerHTML = innerHtml;
  } else {
    node = mkMsg('msg-ai ' + extraCls, `
      <div class="msg-avatar avatar-ai">🤖</div>
      <div class="msg-bubble">${innerHtml}</div>`);
    if (domId) node.id = domId;
    el.messages().appendChild(node);
  }
  scrollBottom();
}

function mkMsg(cls, innerHTML) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  d.innerHTML = innerHTML;
  return d;
}

function updateHeader() {
  const task = activeTask;
  if (!task) {
    el.headerTitle().textContent  = 'AICodeAgent';
    el.headerStatus().textContent = '';
    el.headerActions().innerHTML  = '';
    return;
  }
  el.headerTitle().textContent  = (task.raw_requirement || task.task_id).slice(0, 60);
  el.headerStatus().textContent = stateLabel(task.current_state);

  // 终态显示查看旧界面按钮
  const actions = [];
  if (task.task_id) {
    actions.push(`<a href="/static/task.html?id=${task.task_id}" target="_blank"
      class="btn-ghost" style="font-size:11px">详情↗</a>`);
  }
  if (!TERMINAL.has(task.current_state)) {
    actions.push(`<button class="btn-ghost" onclick="cancelTask('${task.task_id}')"
      style="font-size:11px">取消</button>`);
  }
  el.headerActions().innerHTML = actions.join('');
}

/* ═══════════════════════════════════
   SSE Streaming
═══════════════════════════════════ */

function startSSE(taskId) {
  stopSSE();
  if (activeTask && TERMINAL.has(activeTask.current_state)) return;

  sseSource = new EventSource(`/api/stream/${taskId}`);

  sseSource.onmessage = evt => {
    let data;
    try { data = JSON.parse(evt.data); } catch { return; }

    if (data.type === 'done' || data.type === 'error') {
      stopSSE();
      return;
    }

    if (data.type === 'log') {
      // 增量追加到 liveLog
      const log = $('liveLog');
      if (log) {
        log.textContent += data.text;
        log.scrollTop = log.scrollHeight;
        scrollBottom();
      }
      return;
    }

    if (data.type === 'state') {
      activeTask = data;

      // 更新 sidebar 小点
      const item = document.querySelector(`.task-item[data-id="${taskId}"]`);
      if (item) {
        item.querySelector('.task-status-dot').className =
          'task-status-dot ' + dotClass(data.current_state);
        item.querySelector('.task-item-meta').textContent =
          stateLabel(data.current_state) + ' · 刚刚';
      }

      if (data.current_state === lastState) {
        // 同状态：只更新日志
        if (data.current_state === 'coding' && data.cli_feedback) {
          const log = $('liveLog');
          if (log) {
            log.textContent = data.cli_feedback;
            log.scrollTop = log.scrollHeight;
          }
        }
        return;
      }
      lastState = data.current_state;

      // 移除旧 thinking placeholder
      $('thinking-placeholder')?.remove();

      renderStateBlock(data);
      updateHeader();
      updateInputBar();

      if (TERMINAL.has(data.current_state)) {
        setTimeout(stopSSE, 3000);
        loadTasks(); // 刷新 sidebar
      }
    }
  };

  sseSource.onerror = () => stopSSE();
}

function stopSSE() {
  if (sseSource) { sseSource.close(); sseSource = null; }
}

/* ═══════════════════════════════════
   Input bar state machine
═══════════════════════════════════ */

function updateInputBar() {
  const inp   = el.msgInput();
  const send  = el.btnSend();
  const hint  = el.inputHint();
  const level = el.levelSel();

  const state = activeTask?.current_state;

  if (!activeId || !activeTask) {
    // 新建模式
    inp.disabled  = false;
    inp.placeholder = '描述需求，可粘贴截图…';
    send.disabled = false;
    send.textContent = '提交任务';
    level.style.display = '';
    if (hint) hint.textContent = 'Ctrl+Enter 提交 · 可粘贴图片';
    el.btnAttach().style.display = '';
    return;
  }

  if (state === 'waiting_clarification') {
    inp.disabled  = false;
    inp.placeholder = '回答 AI 的问题，可粘贴截图…';
    send.disabled = false;
    send.textContent = '回复';
    level.style.display = 'none';
    if (hint) hint.textContent = 'Ctrl+Enter 发送';
    el.btnAttach().style.display = '';
    return;
  }

  if (TERMINAL.has(state)) {
    inp.disabled  = false;
    inp.placeholder = '描述新的需求…';
    send.disabled = false;
    send.textContent = '新建任务';
    level.style.display = '';
    if (hint) hint.textContent = '在这里开启一个新任务';
    el.btnAttach().style.display = '';
    return;
  }

  // 进行中：禁用
  inp.disabled  = true;
  inp.placeholder = 'AI 正在处理，请稍候…';
  send.disabled = true;
  send.textContent = '处理中…';
  level.style.display = 'none';
  if (hint) hint.textContent = '';
  el.btnAttach().style.display = 'none';
}

/* ═══════════════════════════════════
   Actions
═══════════════════════════════════ */

async function onSend() {
  const text  = el.msgInput().value.trim();
  const state = activeTask?.current_state;

  if (!text) return;

  if (!activeId || TERMINAL.has(state)) {
    await doSubmitTask(text);
  } else if (state === 'waiting_clarification') {
    await doReply(activeId, text);
  }
}

async function doSubmitTask(requirement) {
  el.btnSend().disabled = true;

  // 先上传图片
  let imageUrls = [];
  if (pendingImgs.length) {
    try { imageUrls = (await uploadImgs(pendingImgs)).map(f => f.url); }
    catch { toast('图片上传失败', false); }
  }

  try {
    const data = await apiPost('/api/task', {
      requirement,
      level: el.levelSel().value,
      source: 'web_chat',
      image_urls: imageUrls,
    });

    el.msgInput().value = '';
    autoResize(el.msgInput());
    clearAttachments();
    toast('任务已提交 🚀');

    await loadTasks();
    selectTask(data.task_id);
  } catch (err) {
    toast('提交失败：' + err.message, false);
    el.btnSend().disabled = false;
  }
}

async function doReply(taskId, reply) {
  el.btnSend().disabled = true;

  let imageUrls = [];
  if (pendingImgs.length) {
    try { imageUrls = (await uploadImgs(pendingImgs)).map(f => f.url); }
    catch {}
  }

  try {
    await apiPost(`/api/reply/${taskId}`, { reply, image_urls: imageUrls });

    // 移除澄清块，追加用户回复
    $('ai-clarify-block')?.remove();
    appendUserReply(reply);

    el.msgInput().value = '';
    autoResize(el.msgInput());
    clearAttachments();
    toast('已回复');
  } catch {
    toast('发送失败', false);
    el.btnSend().disabled = false;
  }
}

async function approveGate(taskId) {
  try {
    await apiPost(`/api/continue/${taskId}`, {});
    $('ai-gate-block')?.remove();
    $('thinking-placeholder')?.remove();
    appendUserReply('（已确认，继续执行）');
    toast('已确认 ✅');
  } catch {
    toast('操作失败', false);
  }
}

async function cancelTask(taskId) {
  if (!confirm('确定要取消这个任务吗？')) return;
  try {
    await apiPost(`/api/cancel/${taskId}`, {});
    toast('已取消');
    await loadTasks();
    selectTask(taskId);
  } catch {
    toast('取消失败', false);
  }
}

function fillClarify(idx) {
  const qs = activeTask?.clarification_questions || [];
  if (!qs[idx]) return;
  const ta = $('clarifyTA') || el.msgInput();
  ta.value = qs[idx];
  ta.focus();
}

async function sendClarify(taskId) {
  const ta = $('clarifyTA');
  const text = (ta?.value || el.msgInput().value).trim();
  if (!text) return;
  if (ta) ta.value = '';
  await doReply(taskId, text);
}

/* ═══════════════════════════════════
   Image upload
═══════════════════════════════════ */

function triggerAttach() { el.fileInput().click(); }

function onFileSelect(e) {
  Array.from(e.target.files)
    .filter(f => f.type.startsWith('image/'))
    .forEach(addImgFile);
  e.target.value = '';
}

function onPaste(e) {
  const items = Array.from(e.clipboardData?.items || [])
    .filter(item => item.type.startsWith('image/'));
  if (!items.length) return;
  e.preventDefault();
  items.forEach(item => {
    const f = item.getAsFile();
    if (f) addImgFile(f);
  });
}

function addImgFile(file) {
  const reader = new FileReader();
  reader.onload = ev => {
    const [header, b64] = (ev.target.result || '').split(',');
    if (!b64) return;
    const objUrl = URL.createObjectURL(file);
    pendingImgs.push({
      name:      file.name || 'image.png',
      data:      b64,
      mime_type: file.type || 'image/png',
      objUrl,
    });
    renderAttachPrev();
  };
  reader.readAsDataURL(file);
}

function removeAttachment(idx) {
  URL.revokeObjectURL(pendingImgs[idx].objUrl);
  pendingImgs.splice(idx, 1);
  renderAttachPrev();
}

function clearAttachments() {
  pendingImgs.forEach(i => URL.revokeObjectURL(i.objUrl));
  pendingImgs = [];
  renderAttachPrev();
}

function renderAttachPrev() {
  el.attachPrev().innerHTML = pendingImgs.map((img, i) => `
    <div class="attachment-item">
      <img src="${img.objUrl}" class="attachment-thumb" onclick="lightbox('${img.objUrl}')">
      <button class="attachment-remove" onclick="removeAttachment(${i})">✕</button>
    </div>`).join('');
}

async function uploadImgs(imgs) {
  const data = await apiPost('/api/upload', {
    images: imgs.map(({ name, data, mime_type }) => ({ name, data, mime_type }))
  });
  return data.files || [];
}

/* ═══════════════════════════════════
   Lightbox
═══════════════════════════════════ */

function lightbox(src) {
  const overlay = document.createElement('div');
  overlay.className = 'lightbox';
  overlay.innerHTML = `<img src="${esc(src)}" alt="">`;
  overlay.onclick = () => overlay.remove();
  document.body.appendChild(overlay);
}

/* ═══════════════════════════════════
   API helpers
═══════════════════════════════════ */

async function apiGet(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function apiPost(path, body) {
  const r = await fetch(path, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

/* ═══════════════════════════════════
   Utils
═══════════════════════════════════ */

function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function timeAgo(iso) {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1)  return '刚刚';
  if (m < 60) return `${m} 分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时前`;
  return `${Math.floor(h / 24)} 天前`;
}

function autoResize(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 160) + 'px';
}

function scrollBottom() {
  const m = el.messages();
  if (m) m.scrollTop = m.scrollHeight;
}

function toast(msg, ok = true) {
  const c = $('toast-container');
  const t = document.createElement('div');
  t.className = 'toast ' + (ok ? 'success' : 'error');
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 3200);
}

/* ── 启动 ── */
init();
