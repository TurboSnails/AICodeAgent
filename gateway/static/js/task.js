/**
 * AICodeAgent V4 — 任务详情页交互
 * 功能：任务详情展示、状态历史时间线、进度条、操作按钮
 */

const $ = id => document.getElementById(id);

/* ---------- 工具函数 ---------- */
function toast(msg, ok = true) {
  const container = $('toast-container');
  const t = document.createElement('div');
  t.className = 'toast ' + (ok ? 'success' : 'error');
  t.textContent = msg;
  container.appendChild(t);
  setTimeout(() => {
    t.style.opacity = '0';
    t.style.transform = 'translateY(12px)';
    setTimeout(() => t.remove(), 300);
  }, 3500);
}

async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  return r.json();
}

function badgeClass(state) {
  return 'badge badge-' + state.replace(/[^a-z0-9]/g, '_');
}
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

const STATE_LABEL = {
  pending: '排队中', planning: '规划中', debating: '方案讨论', consensus: '达成共识',
  waiting_gate: '待你确认', waiting_clarification: '待你回复',
  coding: '正在编码', building: '正在构建',
  self_review: '自审查', codex_review: '代码审查', architect_review: '架构评审',
  red_team_review: '安全审查', requirement_review: '需求审查',
  correcting: '修复中', git_committing: '提交代码', creating_pr: '创建 PR',
  notifying: '发送通知', completed: '已完成', failed: '失败', cancelled: '已取消',
};
function stateLabel(s) { return STATE_LABEL[s] || s; }
function fmtFullTime(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0') + ' ' + String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0') + ':' + String(d.getSeconds()).padStart(2,'0');
}
function fmtTime(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  return (d.getMonth()+1) + '/' + d.getDate() + ' ' + String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
}

/* ---------- 通用确认弹窗 ---------- */
let _confirmCallback = null;
function showConfirm(title, body, btnText, btnClass, callback) {
  $('confirmModalTitle').textContent = title;
  $('confirmModalBody').innerHTML = body;
  const btn = $('confirmModalBtn');
  btn.textContent = btnText;
  btn.className = 'btn ' + (btnClass === 'btn-success' ? 'btn-success' : btnClass === 'btn-danger' ? 'btn-danger' : 'btn-primary');
  _confirmCallback = callback;
  btn.onclick = () => { closeConfirmModal(); callback(); };
  $('confirmModal').classList.add('show');
}
function closeConfirmModal() { $('confirmModal').classList.remove('show'); _confirmCallback = null; }

/* ---------- 任务操作 ---------- */
async function continueTask(id) {
  showConfirm('核准继续', '确认核准任务 <code>' + id + '</code> 继续执行？', '确认继续', 'btn-success', async () => {
    const res = await api('POST', '/api/continue/' + id);
    toast(res.success ? '已核准继续' : '操作失败', res.success);
    loadTask();
  });
}
async function replyTask(id) {
  const val = document.getElementById('replyInput').value.trim();
  if (!val) { toast('请输入回复内容', false); return; }
  showConfirm('回复澄清', '确认发送澄清回复？', '确认发送', 'btn-primary', async () => {
    const res = await api('POST', '/api/reply/' + id, { reply: val });
    toast(res.success ? '已回复' : '操作失败', res.success);
    loadTask();
  });
}
async function cancelTask(id) {
  showConfirm('取消任务', '确认取消任务 <code>' + id + '</code>？此操作不可撤销。', '确认取消', 'btn-danger', async () => {
    const res = await api('POST', '/api/cancel/' + id);
    toast(res.success ? '已取消' : '操作失败', res.success);
    loadTask();
  });
}

/* ---------- 进度条 ---------- */
const PHASE_ORDER = [
  'pending', 'planning', 'debating', 'consensus',
  'coding', 'building', 'codex_review', 'red_team_review',
  'requirement_review', 'git_committing', 'creating_pr',
  'notifying', 'completed'
];
const PHASE_LABELS = {
  pending: '队列',
  planning: '规划',
  debating: '辩论',
  consensus: '共识',
  coding: '编码',
  building: '构建',
  codex_review: '逻辑审查',
  red_team_review: '红队审查',
  requirement_review: '需求审查',
  git_committing: 'Git提交',
  creating_pr: '创建PR',
  notifying: '通知',
  completed: '完成'
};

function renderProgress(currentState, isFailed) {
  const track = $('progressTrack');
  if (!track) return;

  const idx = PHASE_ORDER.indexOf(currentState);
  if (idx < 0) {
    track.innerHTML = '<span class="text-muted">暂无进度信息</span>';
    return;
  }

  const parts = [];
  for (let i = 0; i < PHASE_ORDER.length; i++) {
    const s = PHASE_ORDER[i];
    const label = PHASE_LABELS[s] || s;
    let cls = '';
    if (i < idx) cls = 'done';
    else if (i === idx) cls = isFailed ? 'failed' : 'active';

    parts.push(`<div class="progress-step ${cls}"><div class="progress-dot"></div><div class="progress-label">${label}</div></div>`);
    if (i < PHASE_ORDER.length - 1) {
      parts.push(`<div class="progress-line ${i < idx ? 'done' : ''}"></div>`);
    }
  }
  track.innerHTML = parts.join('');
}

/* ---------- 详情网格 ---------- */
function renderDetailGrid(task) {
  const grid = $('detailGrid');
  const items = [
    { k: '任务 ID', v: task.task_id },
    { k: '需求描述', v: task.raw_requirement },
    { k: '任务等级', v: task.level },
    { k: '来源', v: task.source + (task.chat_id ? ' (' + task.chat_id + ')' : '') },
    { k: '目标站点', v: task.site_hint || '-' },
    { k: '分支', v: task.branch || '-' },
    { k: 'PR 链接', v: task.pr_url ? '<a href="' + task.pr_url + '" target="_blank">' + task.pr_url + '</a>' : '-' },
    { k: '重试次数', v: task.attempt_count + ' / ' + task.max_retries },
    { k: '创建时间', v: fmtFullTime(task.created_at) },
    { k: '更新时间', v: fmtFullTime(task.updated_at) },
    { k: '状态', v: '<span class="' + badgeClass(task.current_state) + '">' + stateLabel(task.current_state) + '</span>' },
  ];
  if (task.error_log) {
    items.push({ k: '错误日志', v: '<pre>' + escapeHtml(task.error_log) + '</pre>' });
  }
  grid.innerHTML = items.map(it =>
    '<div class="detail-item"><div class="detail-key">' + it.k + '</div><div class="detail-val">' + it.v + '</div></div>'
  ).join('');
}

/* ---------- 时间线 ---------- */
function renderTimeline(history, currentState) {
  const container = $('timeline');
  if (!history || !history.length) {
    container.innerHTML = '<div class="empty-state">暂无状态历史</div>';
    return;
  }
  container.innerHTML = history.map((h, i) => {
    let cls = '';
    if (h.to_state === currentState) cls = 'active';
    else if (['completed'].includes(h.to_state)) cls = 'done';
    else if (['failed','cancelled'].includes(h.to_state)) cls = 'failed';
    else cls = 'done';
    return `<div class="timeline-item ${cls}">
      <div class="timeline-dot"></div>
      <div class="timeline-time">${fmtTime(h.timestamp)}</div>
      <div class="timeline-content">
        <div class="timeline-state">${stateLabel(h.from_state) || '开始'} → ${stateLabel(h.to_state)}</div>
        ${h.reason ? '<div class="timeline-reason">' + escapeHtml(h.reason) + '</div>' : ''}
      </div>
    </div>`;
  }).join('');
}

/* ---------- 操作按钮 ---------- */
function renderActions(task) {
  const done = ['completed','failed','cancelled'].includes(task.current_state);
  const container = $('d-actions');
  let html = '';
  if (task.current_state === 'waiting_gate') {
    html += '<button class="btn btn-success btn-sm" onclick="continueTask(\'' + task.task_id + '\')">确认并继续</button>';
  } else if (task.current_state === 'waiting_clarification') {
    html += '<input id="replyInput" class="input" placeholder="输入你的回复…" style="min-width:200px;"><button class="btn btn-primary btn-sm" onclick="replyTask(\'' + task.task_id + '\')">发送</button>';
  }
  if (!done) {
    html += (html ? ' ' : '') + '<button class="btn btn-secondary btn-sm" onclick="cancelTask(\'' + task.task_id + '\')">取消任务</button>';
  }
  container.innerHTML = html || '<span class="text-muted">—</span>';
}

/* ---------- 加载 ---------- */
async function loadTask() {
  const params = new URLSearchParams(location.search);
  const id = params.get('id');
  if (!id) {
    $('loading').style.display = 'none';
    $('error').style.display = 'block';
    $('error').textContent = '缺少任务 ID';
    return;
  }

  // 并行加载任务和状态历史
  const [taskRes, histRes] = await Promise.all([
    api('GET', '/task/' + id),
    api('GET', '/task/' + id + '/history'),
  ]);

  $('loading').style.display = 'none';

  if (!taskRes.task) {
    $('error').style.display = 'block';
    return;
  }

  const task = taskRes.task;
  $('detail').style.display = 'block';

  // 标题和元信息
  $('d-title').textContent = '任务 ' + task.task_id;
  $('d-meta').innerHTML = '<span class="pill pill-level">' + task.level + '</span> · <span class="pill ' + (task.current_state === 'completed' ? 'pill-done' : 'pill-running') + '">' + stateLabel(task.current_state) + '</span> · ' + fmtFullTime(task.created_at);

  // 进度条
  renderProgress(task.current_state, task.current_state === 'failed');

  // 详情
  renderDetailGrid(task);

  // 时间线
  renderTimeline(histRes.history || [], task.current_state);

  // 操作按钮
  renderActions(task);
}

/* ---------- 启动 ---------- */
loadTask();
setInterval(loadTask, 5000);
