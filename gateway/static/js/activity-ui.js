/**
 * 任务实时活动状态展示（phase_detail + AI 输出摘要）
 */

function sanitizeCliFeedback(text) {
  if (!text) return '';
  return text.split('\n').map(function (line) {
    if (line.indexOf('cmd:') === 0 && line.length > 240) {
      return line.slice(0, 200) + ' … (truncated)';
    }
    return line;
  }).join('\n');
}

function activityFingerprint(task) {
  return [
    task.current_state,
    task.phase_detail || '',
    task.cli_feedback || '',
    task.cli_running ? '1' : '0',
    task.cli_status || '',
    String(task.cli_pid || ''),
    task.ai_output_preview || '',
    task.error_log || '',
  ].join('\u0001');
}

function renderActivityBlock(task, doneStates) {
  const done = doneStates || new Set(['completed', 'failed', 'cancelled']);
  const st = task.current_state;
  const detail = (task.phase_detail || '').trim();
  const preview = (task.ai_output_preview || '').trim();
  const err = (task.error_log || '').trim();
  const show = !done.has(st) || preview || (st === 'failed' && (detail || err));

  if (!show && !detail && !preview && !err) return '';

  const updated = task.phase_updated_at
    ? '<span class="activity-updated">更新 ' + escapeHtml(fmtActivityTime(task.phase_updated_at)) + '</span>'
    : '';
  let title = stateLabel(st);
  if (st === 'failed') {
    const at = task.failed_at_state || (task.phase_status_state !== st ? task.phase_status_state : '');
    title = '失败' + (at ? ' · ' + stateLabel(at) : '');
  } else if (task.phase_status_state && task.phase_status_state !== st) {
    title += ' · ' + stateLabel(task.phase_status_state);
  }

  let body = '';
  if (st === 'failed' && err) {
    body += '<div class="activity-detail activity-error">' + escapeHtml(err) + '</div>';
  } else if (detail) {
    body += '<div class="activity-detail">' + escapeHtml(detail) + '</div>';
  }
  if (err && st !== 'failed' && !done.has(st)) {
    body += '<div class="activity-detail activity-error">' + escapeHtml(err) + '</div>';
  }
  if (preview) {
    body += '<pre class="activity-ai-preview" data-scroll-preserve="1">' + escapeHtml(preview) + '</pre>';
  }

  const cliFb = sanitizeCliFeedback((task.cli_feedback || '').trim());
  const cliRunning = task.cli_running === true;
  const cliStatus = (task.cli_status || '').trim();
  if (cliFb || cliRunning || cliStatus) {
    let badge = cliRunning ? '<span class="cli-badge cli-alive">进程运行中</span>'
      : (cliStatus === 'done' ? '<span class="cli-badge cli-done">已结束</span>'
        : (cliStatus === 'failed' ? '<span class="cli-badge cli-fail">异常退出</span>'
          : '<span class="cli-badge">等待中</span>'));
    if (task.cli_pid) badge += ' <span class="text-muted">PID ' + escapeHtml(String(task.cli_pid)) + '</span>';
    body += '<div class="activity-cli"><div class="activity-cli-head">CLI 反馈 ' + badge + '</div>'
      + '<pre class="activity-cli-log" data-scroll-preserve="1">' + escapeHtml(cliFb || '（等待 CLI 日志…）') + '</pre></div>';
  } else if (!done.has(st) && st === 'coding') {
    body += '<div class="activity-cli-hint text-muted text-sm">Claude CLI 已启动，日志写入 workspace/coding_cli.log …</div>';
  }

  return `<div class="activity-box${done.has(st) ? ' activity-box-done' : ''}">
    <div class="activity-head"><strong>当前进展</strong><span>${escapeHtml(title)}</span>${updated}</div>
    ${body}
  </div>`;
}

function fmtActivityTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch (e) {
    return iso;
  }
}
