/**
 * AICodeAgent — 任务中心
 */

const $ = id => document.getElementById(id);
let _pendingSubmit = null;
let _prevTasks = [];
const DONE = new Set(['completed', 'failed', 'cancelled']);
const WAIT = new Set(['waiting_gate', 'waiting_clarification']);

const STATE_LABEL = {
  pending: '排队中',
  planning: '规划中',
  debating: '方案讨论',
  consensus: '达成共识',
  waiting_gate: '待你确认',
  waiting_clarification: '待你回复',
  coding: '正在编码',
  building: '正在构建',
  self_review: '自审查',
  codex_review: '代码审查',
  architect_review: '架构评审',
  red_team_review: '安全审查',
  requirement_review: '需求审查',
  correcting: '修复中',
  git_committing: '提交代码',
  creating_pr: '创建 PR',
  notifying: '发送通知',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

function stateLabel(s) { return STATE_LABEL[s] || s; }

function pillClass(state) {
  if (WAIT.has(state)) return 'pill-wait';
  if (state === 'completed') return 'pill-done';
  if (state === 'failed' || state === 'correcting') return 'pill-fail';
  if (DONE.has(state)) return 'pill-idle';
  return 'pill-running';
}

function statusIcon(state) {
  if (WAIT.has(state)) return '⏳';
  if (state === 'completed') return '✓';
  if (state === 'failed') return '✕';
  if (state === 'cancelled') return '—';
  if (DONE.has(state)) return '—';
  return '◉';
}

function iconBoxClass(state) {
  if (WAIT.has(state)) return 's-wait';
  if (state === 'completed') return 's-done';
  if (state === 'failed') return 's-fail';
  if (DONE.has(state)) return 's-idle';
  return 's-running';
}

/* ---------- 工具 ---------- */
function toast(msg, ok = true) {
  const c = $('toast-container');
  const t = document.createElement('div');
  t.className = 'toast ' + (ok ? 'success' : 'error');
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => {
    t.style.opacity = '0';
    setTimeout(() => t.remove(), 280);
  }, 3200);
}

function notify(html, type) {
  const box = document.createElement('div');
  box.className = 'notify-item ' + (type === 'completed' ? 'done' : type === 'failed' ? 'fail' : 'wait');
  box.innerHTML = '<span>' + html + '</span><button type="button" class="btn btn-ghost btn-sm" onclick="this.parentElement.remove()">关闭</button>';
  $('notifyCenter').prepend(box);
  setTimeout(() => { if (box.parentElement) box.remove(); }, 16000);
}

async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  return (await fetch(path, opts)).json();
}

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function truncate(s, n) { return s && s.length > n ? s.slice(0, n) + '…' : (s || ''); }
function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const diff = (now - d) / 60000;
  if (diff < 1) return '刚刚';
  if (diff < 60) return Math.floor(diff) + ' 分钟前';
  if (diff < 1440) return Math.floor(diff / 60) + ' 小时前';
  return (d.getMonth() + 1) + '月' + d.getDate() + '日';
}
function fmtFullTime(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') + ' '
    + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}

function setLastRefresh() {
  const el = $('lastRefresh');
  if (el) el.textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

function updateStats(all) {
  $('statTotal').textContent = all.length;
  $('statActive').textContent = all.filter(t => !DONE.has(t.current_state)).length;
  $('statWaiting').textContent = all.filter(t => WAIT.has(t.current_state)).length;
  $('statDone').textContent = all.filter(t => t.current_state === 'completed').length;
}

async function checkHealth() {
  const dot = $('healthDot');
  const label = $('healthLabel');
  if (!dot) return;
  try {
    const res = await api('GET', '/health');
    if (res.status === 'ok') {
      dot.className = 'online-dot ok';
      label.textContent = '服务正常';
    } else {
      dot.className = 'online-dot err';
      label.textContent = '异常';
    }
  } catch {
    dot.className = 'online-dot err';
    label.textContent = '无法连接';
  }
}

function syncTabs(state) {
  document.querySelectorAll('#quickFilters .tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.state === state);
  });
}

function onFilterChange() {
  syncTabs($('filterState').value);
  loadTasks();
}

function initTabs() {
  document.querySelectorAll('#quickFilters .tab').forEach(btn => {
    btn.addEventListener('click', () => {
      $('filterState').value = btn.dataset.state || '';
      syncTabs(btn.dataset.state || '');
      loadTasks();
    });
  });
}

/* ---------- 主 Tab 切换 ---------- */
function switchMainTab(tabName) {
  document.querySelectorAll('.main-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabName);
  });
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === 'tab-' + tabName);
  });
  localStorage.setItem('agentMainTab', tabName);
}
function initMainTabs() {
  document.querySelectorAll('.main-tab').forEach(btn => {
    btn.addEventListener('click', () => switchMainTab(btn.dataset.tab));
  });
  const saved = localStorage.getItem('agentMainTab');
  if (saved && document.querySelector('.main-tab[data-tab="' + saved + '"]')) {
    switchMainTab(saved);
  }
}

/* ---------- 提交 ---------- */
function onSubmitClick() {
  const requirement = $('req').value.trim();
  if (!requirement) { toast('请先填写需求', false); return; }
  const level = $('level').value;
  let body = '';
  if (level === 'L2') body = '这是<b>大改动</b>，系统会先做好方案，<b>等你确认后</b>才会开始写代码。';
  else if (level === 'auto') body = '系统会自动判断改动大小。若判定为大改动，同样需要你确认后再继续。';
  else body = '提交后将按 <b>' + $('level').selectedOptions[0].text + '</b> 开始处理。';
  $('submitModalBody').innerHTML = body;
  $('submitModalTitle').textContent = '确认提交？';
  _pendingSubmit = { requirement, level, site_hint: $('site').value.trim() };
  $('submitModal').classList.add('show');
}
function closeModal() { $('submitModal').classList.remove('show'); _pendingSubmit = null; }
async function confirmSubmit() {
  if (!_pendingSubmit) return;
  closeModal();
  const res = await api('POST', '/api/task', _pendingSubmit);
  if (res.task_id) {
    toast('已提交，任务编号 ' + res.task_id);
    $('req').value = '';
    loadTasks();
  } else {
    toast(res.error || '提交失败', false);
  }
}

function showConfirm(title, body, btnText, btnClass, callback) {
  $('confirmModalTitle').textContent = title;
  $('confirmModalBody').innerHTML = body;
  const btn = $('confirmModalBtn');
  btn.textContent = btnText;
  btn.className = 'btn ' + (btnClass === 'btn-success' ? 'btn-success' : btnClass === 'btn-danger' ? 'btn-danger' : 'btn-primary');
  btn.onclick = () => { closeConfirmModal(); callback(); };
  $('confirmModal').classList.add('show');
}
function closeConfirmModal() { $('confirmModal').classList.remove('show'); }

async function continueTask(id) {
  showConfirm('确认继续', '确定让任务 <b>' + id + '</b> 开始编码？', '确认', 'btn-success', async () => {
    const res = await api('POST', '/api/continue/' + id);
    toast(res.success ? '已开始执行' : '操作失败', res.success);
    loadTasks();
  });
}
async function replyTask(id) {
  const el = document.getElementById('r-' + id);
  if (!el || !el.value.trim()) { toast('请先输入回复内容', false); return; }
  const val = el.value.trim();
  showConfirm('发送回复', '确定发送这条澄清回复？', '发送', 'btn-primary', async () => {
    const res = await api('POST', '/api/reply/' + id, { reply: val });
    toast(res.success ? '已发送' : '发送失败', res.success);
    loadTasks();
  });
}
async function cancelTask(id) {
  showConfirm('取消任务', '确定取消任务 <b>' + id + '</b>？取消后无法恢复。', '取消任务', 'btn-danger', async () => {
    const res = await api('POST', '/api/cancel/' + id);
    toast(res.success ? '已取消' : '操作失败', res.success);
    loadTasks();
  });
}

/* ---------- 渲染 ---------- */
function renderActionCards(tasks) {
  const waiting = tasks.filter(t => WAIT.has(t.current_state));
  const area = $('actionArea');
  const empty = $('actionEmpty');
  const badge = $('actionBadge');
  badge.textContent = waiting.length;
  badge.style.display = waiting.length ? 'inline-flex' : 'none';
  if (!waiting.length) {
    area.hidden = true;
    if (empty) empty.hidden = false;
    return;
  }
  area.hidden = false;
  if (empty) empty.hidden = true;
  $('actionCount').textContent = waiting.length;
  $('actionList').innerHTML = waiting.map(t => {
    const isGate = t.current_state === 'waiting_gate';
    return `<div class="action-card">
      <div class="action-card-title">${isGate ? '需要你确认方案' : '需要你补充说明'}</div>
      <div class="action-card-meta">${t.task_id} · ${fmtFullTime(t.created_at)}</div>
      <div class="action-card-body">${escapeHtml(t.raw_requirement)}</div>
      <div class="action-card-footer">
        ${isGate
          ? `<button type="button" class="btn btn-success btn-sm" onclick="continueTask('${t.task_id}')">确认并继续</button>`
          : `<input id="r-${t.task_id}" class="input" placeholder="输入你的回复…" style="flex:1;min-width:140px;" /><button type="button" class="btn btn-primary btn-sm" onclick="replyTask('${t.task_id}')">发送</button>`}
        <button type="button" class="btn btn-secondary btn-sm" onclick="cancelTask('${t.task_id}')">取消任务</button>
      </div>
    </div>`;
  }).join('');
}

function renderTaskItem(t) {
  const st = t.current_state;
  const highlight = WAIT.has(st);
  const faded = DONE.has(st);
  let actions = '';
  if (st === 'waiting_gate') {
    actions = `<button type="button" class="btn btn-success btn-sm" onclick="continueTask('${t.task_id}')">确认</button>`;
  } else if (st === 'waiting_clarification') {
    actions = `<input id="r-${t.task_id}" class="input" placeholder="回复…" /><button type="button" class="btn btn-primary btn-sm" onclick="replyTask('${t.task_id}')">发送</button>`;
  }
  if (!DONE.has(st)) {
    actions += (actions ? ' ' : '') + `<button type="button" class="btn btn-secondary btn-sm" onclick="cancelTask('${t.task_id}')">取消</button>`;
  }
  const pr = t.pr_url ? ` <a href="${escapeHtml(t.pr_url)}" target="_blank" rel="noopener" class="text-sm">查看 PR</a>` : '';

  return `<article class="task-item${highlight ? ' is-highlight' : ''}${faded ? ' is-faded' : ''}">
    <div class="task-status-icon ${iconBoxClass(st)}">${statusIcon(st)}</div>
    <div class="task-body">
      <div class="task-top">
        <a class="task-title" href="/static/task.html?id=${t.task_id}">${t.task_id}</a>
        <span class="pill pill-level">${t.level}</span>
        <span class="pill ${pillClass(st)}">${stateLabel(st)}</span>
      </div>
      <p class="task-req-text">${escapeHtml(t.raw_requirement || '（无描述）')}</p>
      <div class="task-foot">
        <span class="task-time">${fmtTime(t.created_at)}</span>${pr}
      </div>
    </div>
    <div class="task-side">
      <div class="task-actions">${actions || ''}</div>
    </div>
  </article>`;
}

let _searchDebounce = null;
function onSearchInput() {
  clearTimeout(_searchDebounce);
  _searchDebounce = setTimeout(loadTasks, 280);
}

async function loadTasks() {
  const res = await api('GET', '/tasks');
  const allTasks = res.tasks || [];
  updateStats(allTasks);
  setLastRefresh();

  let tasks = [...allTasks];
  const stateFilter = $('filterState').value;
  const search = $('filterSearch').value.trim().toLowerCase();
  if (stateFilter) tasks = tasks.filter(t => t.current_state === stateFilter);
  if (search) {
    tasks = tasks.filter(t =>
      (t.raw_requirement || '').toLowerCase().includes(search) ||
      (t.task_id || '').toLowerCase().includes(search) ||
      stateLabel(t.current_state).includes(search)
    );
  }

  if (_prevTasks.length) {
    const prevMap = Object.fromEntries(_prevTasks.map(t => [t.task_id, t.current_state]));
    for (const t of tasks) {
      const prev = prevMap[t.task_id];
      if (prev && prev !== t.current_state) {
        if (t.current_state === 'completed') {
          notify('任务 <b>' + t.task_id + '</b> 已完成' + (t.pr_url ? ' <a href="' + t.pr_url + '" target="_blank">查看 PR</a>' : ''), 'completed');
          toast('任务已完成');
        } else if (t.current_state === 'failed') {
          notify('任务 <b>' + t.task_id + '</b> 失败了', 'failed');
          toast('任务失败', false);
        } else if (WAIT.has(t.current_state)) {
          notify('任务 <b>' + t.task_id + '</b> ' + stateLabel(t.current_state), 'waiting');
          toast('有任务需要你处理');
        }
      }
    }
  }
  _prevTasks = JSON.parse(JSON.stringify(allTasks));

  renderActionCards(allTasks);

  const list = $('taskList');
  if (!tasks.length) {
    list.innerHTML = '<p class="empty">还没有任务，先在上方提交一个吧</p>';
    return;
  }

  tasks.sort((a, b) => {
    const ad = DONE.has(a.current_state) ? 1 : 0;
    const bd = DONE.has(b.current_state) ? 1 : 0;
    if (ad !== bd) return ad - bd;
    return (b.created_at || '').localeCompare(a.created_at || '');
  });

  list.innerHTML = tasks.map(renderTaskItem).join('');
}

initTabs();
initMainTabs();
checkHealth();
loadTasks();
setInterval(loadTasks, 3000);
setInterval(checkHealth, 30000);
