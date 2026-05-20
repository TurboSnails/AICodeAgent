#!/usr/bin/env python3
"""
Headless Agent Web UI Gateway V2
- SQLite 队列替代内存字典
- 动态站点列表扫描
- 任务状态从 SQLite 实时读取
"""

import http.server
import json
import os
import socketserver
import sqlite3
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_FILE = PROJECT_ROOT / "AICodeAgent" / "data" / "agent.db"
PORT = 6789

sys.path.insert(0, str(PROJECT_ROOT / "AICodeAgent" / "orchestrator"))
from state_machine import init_db, save_task, Task, get_task, approve_gate_resume, cancel_task, State
from platform_figma import list_platform_sites_for_ui


def scan_sites() -> list[dict]:
    """下拉站点：来自 platform-figma-list + enName 映射"""
    options = list_platform_sites_for_ui()
    if options:
        return options
    site_dir = PROJECT_ROOT / "buildSrc" / "src" / "main" / "kotlin" / "site"
    exclude = {"Site", "SiteChannels"}
    return [{"value": f.stem.lower(), "label": f.stem} for f in site_dir.glob("*.kt") if f.stem not in exclude]


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Android Headless Agent V2</title>
<style>
  body { font-family: -apple-system, sans-serif; margin: 40px; background: #0f172a; color: #e2e8f0; }
  h1 { color: #38bdf8; }
  .card { background: #1e293b; padding: 24px; border-radius: 12px; max-width: 720px; margin-bottom: 24px; }
  label { display: block; margin-bottom: 6px; font-weight: 600; color: #94a3b8; }
  textarea, input, select { width: 100%; padding: 12px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #e2e8f0; font-size: 14px; box-sizing: border-box; }
  textarea { min-height: 120px; resize: vertical; }
  button { margin-top: 16px; padding: 12px 24px; border-radius: 8px; border: none; background: #38bdf8; color: #0f172a; font-weight: 700; cursor: pointer; }
  button:hover { background: #7dd3fc; }
  .task-item { background: #1e293b; padding: 16px; border-radius: 8px; margin-bottom: 12px; border-left: 4px solid #38bdf8; }
  .meta { font-size: 12px; color: #94a3b8; margin-top: 6px; }
  .btn-sm { padding: 6px 12px; font-size: 12px; margin-top: 8px; margin-right: 8px; }
  .btn-continue { background: #22c55e; }
  .btn-continue:hover { background: #4ade80; }
  .btn-cancel { background: #ef4444; }
  .btn-cancel:hover { background: #f87171; }
  .gate-card { border-left-color: #f59e0b; }
</style>
</head>
<body>
<h1>Android Headless Agent V2</h1>
<div class="card">
  <h2>提交新任务</h2>
  <form id="taskForm">
    <label>需求描述</label>
    <textarea name="requirement" placeholder="帮我在 SettingsScreen 里加一个清除缓存功能..."></textarea>
    <label style="margin-top:12px;">任务等级</label>
    <select name="level">
      <option value="auto">自动判定</option>
      <option value="L0">L0 - 轻量</option>
      <option value="L1">L1 - 常规</option>
      <option value="L2">L2 - 复杂</option>
    </select>
    <label style="margin-top:12px;">目标站点（platform-figma-list，可填 enName 或中文如 港岛/好博）</label>
    <select name="site_hint"><option value="">当前站点</option></select>
    <button type="submit">启动 Agent</button>
  </form>
  <div id="result" style="margin-top:12px;"></div>
</div>
<div class="card">
  <h2>L2 待核准任务</h2>
  <div id="gateList">加载中...</div>
</div>
<div class="card">
  <h2>任务历史</h2>
  <div id="taskList">加载中...</div>
</div>
<script>
async function loadSites() {
  const res = await fetch('/api/sites');
  const data = await res.json();
  const select = document.querySelector('select[name="site_hint"]');
  data.sites.forEach(s => {
    const opt = document.createElement('option');
    const item = typeof s === 'string' ? { value: s, label: s } : s;
    opt.value = item.value; opt.textContent = item.label || item.value;
    select.appendChild(opt);
  });
}
async function refreshTasks() {
  const res = await fetch('/api/tasks');
  const data = await res.json();
  const container = document.getElementById('taskList');
  if (data.tasks.length === 0) { container.innerHTML = '<p style="color:#94a3b8">暂无任务</p>'; return; }
  container.innerHTML = data.tasks.reverse().map(t =>
    `<div class="task-item" style="border-left-color:${t.current_state==='completed'?'#22c55e':t.current_state==='failed'?'#ef4444':t.current_state==='cancelled'?'#6b7280':'#38bdf8'}">
      <div><strong>${t.raw_requirement.substring(0,60)}${t.raw_requirement.length>60?'...':''}</strong></div>
      <div class="meta">ID: ${t.task_id} | 等级: ${t.level} | 站点: ${t.site_hint||'auto'} | 状态: ${t.current_state} | ${t.created_at}</div>
      ${t.pr_url ? `<div class="meta">PR: <a href="${t.pr_url}" target="_blank" style="color:#38bdf8">${t.pr_url}</a></div>` : ''}
      ${t.current_state !== 'completed' && t.current_state !== 'failed' && t.current_state !== 'cancelled' ? `<button class="btn-sm btn-cancel" onclick="cancelTask('${t.task_id}')">取消</button>` : ''}
    </div>`
  ).join('');
}
async function refreshGates() {
  const res = await fetch('/api/waiting_gates');
  const data = await res.json();
  const container = document.getElementById('gateList');
  if (data.gates.length === 0) { container.innerHTML = '<p style="color:#94a3b8">暂无待核准任务</p>'; return; }
  container.innerHTML = data.gates.map(t =>
    `<div class="task-item gate-card">
      <div><strong>${t.raw_requirement.substring(0,60)}${t.raw_requirement.length>60?'...':''}</strong></div>
      <div class="meta">ID: ${t.task_id} | 等级: ${t.level} | 站点: ${t.site_hint||'auto'} | 创建于 ${t.created_at}</div>
      <button class="btn-sm btn-continue" onclick="continueTask('${t.task_id}')">✅ 继续编码</button>
      <button class="btn-sm btn-cancel" onclick="cancelTask('${t.task_id}')">❌ 取消</button>
    </div>`
  ).join('');
}
async function continueTask(tid) {
  const res = await fetch('/api/continue', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_id: tid })
  });
  const data = await res.json();
  alert(data.message);
  refreshGates();
  refreshTasks();
}
async function cancelTask(tid) {
  if (!confirm('确定要取消任务 ' + tid + ' 吗？')) return;
  const res = await fetch('/api/cancel', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_id: tid })
  });
  const data = await res.json();
  alert(data.message);
  refreshGates();
  refreshTasks();
}
document.getElementById('taskForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const res = await fetch('/api/trigger', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ raw_requirement: form.get('requirement'), level: form.get('level'), site_hint: form.get('site_hint') })
  });
  const data = await res.json();
  document.getElementById('result').innerHTML = `<p style="color:${data.ok?'#22c55e':'#ef4444'}">${data.message}</p>`;
  if (data.ok) e.target.reset();
  refreshTasks();
});
loadSites();
refreshTasks();
refreshGates();
setInterval(refreshTasks, 5000);
setInterval(refreshGates, 5000);
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(INDEX_HTML.encode("utf-8"))
        elif path == "/api/tasks":
            conn = sqlite3.connect(str(DB_FILE))
            cursor = conn.execute("SELECT task_id, raw_requirement, level, site_hint, current_state, pr_url, created_at FROM task_queue ORDER BY created_at DESC LIMIT 50")
            rows = cursor.fetchall()
            conn.close()
            tasks = [{"task_id": r[0], "raw_requirement": r[1], "level": r[2], "site_hint": r[3],
                      "current_state": r[4], "pr_url": r[5] or "", "created_at": r[6]} for r in rows]
            self._json({"tasks": tasks})
        elif path == "/api/sites":
            self._json({"sites": scan_sites()})
        elif path == "/api/waiting_gates":
            conn = sqlite3.connect(str(DB_FILE))
            cursor = conn.execute(
                "SELECT task_id, raw_requirement, level, site_hint, current_state, pr_url, created_at FROM task_queue WHERE current_state = ? ORDER BY created_at DESC",
                (State.WAITING_GATE.value,)
            )
            rows = cursor.fetchall()
            conn.close()
            gates = [{"task_id": r[0], "raw_requirement": r[1], "level": r[2], "site_hint": r[3],
                      "current_state": r[4], "pr_url": r[5] or "", "created_at": r[6]} for r in rows]
            self._json({"gates": gates})
            return
        elif path == "/health":
            # 健康检查：统计各状态任务数
            import sqlite3
            from state_machine import DB_FILE, State
            conn = sqlite3.connect(str(DB_FILE))
            cursor = conn.execute(
                "SELECT current_state, COUNT(*) FROM task_queue GROUP BY current_state"
            )
            stats = {r[0]: r[1] for r in cursor.fetchall()}
            conn.close()
            active = sum(c for s, c in stats.items() if s not in (State.COMPLETED.value, State.FAILED.value, State.CANCELLED.value))
            self._json({
                "status": "ok",
                "active_tasks": active,
                "state_distribution": stats,
                "executor": "serial",
            })
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        if path == "/api/trigger":
            try:
                payload = json.loads(body) if body else {}
            except Exception:
                self._json({"ok": False, "message": "invalid json"}, 400)
                return
            task_id = str(uuid.uuid4())[:8]
            task = Task(
                task_id=task_id,
                raw_requirement=payload.get("raw_requirement", "").strip(),
                level=payload.get("level", "auto"),
                site_hint=payload.get("site_hint", ""),
                source="web",
                chat_id=""
            )
            if not task.raw_requirement:
                self._json({"ok": False, "message": "需求描述不能为空"}, 400)
                return
            save_task(task)
            self._json({"ok": True, "message": f"任务已提交 (ID: {task_id})", "task_id": task_id})
            return
        if path == "/api/continue":
            try:
                payload = json.loads(body) if body else {}
            except Exception:
                self._json({"ok": False, "message": "invalid json"}, 400)
                return
            tid = (payload.get("task_id") or "").strip()
            if not tid:
                self._json({"ok": False, "message": "task_id 必填"}, 400)
                return
            task = get_task(tid)
            if not task or task.current_state != State.WAITING_GATE.value:
                self._json({"ok": False, "message": f"任务 {tid} 不在 waiting_gate"}, 400)
                return
            if approve_gate_resume(tid):
                self._json({"ok": True, "message": f"L2 已核准，任务 {tid} 已重新入队"})
            else:
                self._json({"ok": False, "message": "核准失败"}, 500)
            return
        if path == "/api/cancel":
            try:
                payload = json.loads(body) if body else {}
            except Exception:
                self._json({"ok": False, "message": "invalid json"}, 400)
                return
            tid = (payload.get("task_id") or "").strip()
            if not tid:
                self._json({"ok": False, "message": "task_id 必填"}, 400)
                return
            task = get_task(tid)
            if not task or task.current_state in (State.COMPLETED.value, State.FAILED.value, State.CANCELLED.value):
                self._json({"ok": False, "message": f"任务 {tid} 不存在或已结束"}, 400)
                return
            if cancel_task(tid, reason="user cancelled via web"):
                self._json({"ok": True, "message": f"任务 {tid} 已取消"})
            else:
                self._json({"ok": False, "message": "取消失败"}, 500)
            return
        self._json({"error": "not found"}, 404)


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    init_db()
    with ThreadedTCPServer(("", PORT), Handler) as httpd:
        print(f"[Headless Agent V2 Web UI] http://localhost:{PORT}")
        httpd.serve_forever()
