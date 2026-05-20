#!/usr/bin/env python3
"""
Headless Agent Telegram Bot Gateway V3
- SQLite 持久化替代内存字典
- Bot 重启后恢复 waiting_gate 任务
- 支持 /continue 核准 L2 任务
"""

import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_FILE = PROJECT_ROOT / "AICodeAgent" / "data" / "agent.db"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
POLL_TIMEOUT = 30

sys.path.insert(0, str(PROJECT_ROOT / "AICodeAgent" / "orchestrator"))
from state_machine import (
    init_db, save_task, get_task, Task, transition, State,
    approve_gate_resume, approve_clarification_reply, cancel_task,
)
from platform_figma import resolve_platform_site


def parse_task_args(arg: str) -> tuple[str, str, str]:
    """解析 /task 参数：等级、站点（platform-figma-list）、需求正文"""
    level = "auto"
    site_hint = ""
    rest = arg.strip()
    for prefix in ("L0 ", "L1 ", "L2 "):
        if rest.startswith(prefix):
            level = prefix.strip()
            rest = rest[3:].strip()
            break
    if rest:
        first, _, tail = rest.partition(" ")
        if first and resolve_platform_site(first):
            site_hint = first
            rest = tail.strip()
    return level, site_hint, rest


def _http_json(method: str, url: str, data: dict = None, timeout: int = 10) -> dict:
    """使用标准库 urllib 发送 JSON HTTP 请求"""
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8") if data else None
    req = Request(url, data=payload, method=method,
                  headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except URLError as e:
        print(f"[HTTP ERROR] {e}")
        return {}
    except Exception as e:
        print(f"[HTTP ERROR] {e}")
        return {}


def send_message(text, chat_id=None, parse_mode="HTML"):
    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid or not TELEGRAM_BOT_TOKEN:
        print(f"[TELEGRAM SKIP] {text}")
        return
    _http_json("POST", f"{TELEGRAM_API}/sendMessage",
               data={"chat_id": cid, "text": text, "parse_mode": parse_mode})


def start_agent_task(requirement, level="auto", site_hint="", chat_id=None):
    task_id = str(uuid.uuid4())[:8]
    task = Task(
        task_id=task_id, raw_requirement=requirement, level=level,
        site_hint=site_hint, source="telegram", chat_id=chat_id or TELEGRAM_CHAT_ID
    )
    save_task(task)
    send_message(
        f"<b>任务已启动</b>\n需求: {requirement[:120]}\n任务ID: <code>{task_id}</code>",
        chat_id=chat_id
    )
    return task_id


def handle_command(message):
    text = message.get("text", "").strip()
    chat_id = message["chat"]["id"]
    if not text:
        return
    parts = text.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/task":
        if not arg:
            send_message("用法: /task 帮我在 SettingsScreen 里加一个清除缓存功能", chat_id=chat_id)
            return
        level, site_hint, requirement = parse_task_args(arg)
        if not requirement:
            send_message("用法: /task [L0|L1|L2] [站点] <需求>\n例: /task L1 haobo 在设置页加清除缓存", chat_id=chat_id)
            return
        start_agent_task(requirement, level=level, site_hint=site_hint, chat_id=chat_id)

    elif cmd == "/status":
        tid = arg.strip()
        task = get_task(tid)
        if not task:
            send_message(f"未找到任务 {tid}", chat_id=chat_id)
            return
        send_message(
            f"<b>任务状态</b>\n任务ID: {task.task_id}\n状态: {task.current_state}\n等级: {task.level}\n"
            f"{f'PR: {task.pr_url}' if task.pr_url else ''}",
            chat_id=chat_id
        )

    elif cmd == "/history":
        conn = sqlite3.connect(str(DB_FILE))
        cursor = conn.execute(
            "SELECT task_id, level, raw_requirement, current_state FROM task_queue ORDER BY created_at DESC LIMIT 5"
        )
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            send_message("暂无历史任务", chat_id=chat_id)
            return
        lines = ["<b>最近任务</b>"]
        for r in rows:
            lines.append(f"<code>{r[0]}</code> [{r[1]}] {r[2][:40]}... [{r[3]}]")
        send_message("\n".join(lines), chat_id=chat_id)

    elif cmd == "/continue":
        tid = arg.strip()
        if not tid:
            send_message("用法: /continue <task_id>", chat_id=chat_id)
            return
        if approve_gate_resume(tid):
            send_message(
                f"L2 任务 <code>{tid}</code> 已核准，已重新入队，Executor 将从编码阶段继续…",
                chat_id=chat_id,
            )
        else:
            send_message(f"任务 {tid} 不存在或不处于 waiting_gate 状态", chat_id=chat_id)

    elif cmd == "/reply":
        parts = arg.split(None, 1)
        if len(parts) < 2:
            send_message("用法: /reply <task_id> <澄清内容>", chat_id=chat_id)
            return
        tid, reply_text = parts[0].strip(), parts[1].strip()
        ws = PROJECT_ROOT / "AICodeAgent" / "workspace" / tid
        if approve_clarification_reply(tid, reply_text):
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "user_clarification.md").write_text(reply_text, encoding="utf-8")
            send_message(
                f"已收到澄清，任务 <code>{tid}</code> 将重新入队并进入三方辩论…",
                chat_id=chat_id,
            )
        else:
            send_message(f"任务 {tid} 不存在或不处于 waiting_clarification", chat_id=chat_id)

    elif cmd == "/cancel":
        tid = arg.strip()
        if not tid:
            send_message("用法: /cancel <task_id>", chat_id=chat_id)
            return
        task = get_task(tid)
        if not task:
            send_message(f"未找到任务 {tid}", chat_id=chat_id)
            return
        if task.current_state in (State.COMPLETED.value, State.FAILED.value, State.CANCELLED.value):
            send_message(f"任务 {tid} 已结束，无需取消", chat_id=chat_id)
            return
        if cancel_task(tid, reason="user cancelled via telegram"):
            send_message(f"✅ 任务 <code>{tid}</code> 已取消", chat_id=chat_id)
        else:
            send_message(f"❌ 取消任务 {tid} 失败", chat_id=chat_id)

    elif cmd == "/help":
        send_message(
            "<b>Android Headless Agent V3</b>\n"
            "/task [L0|L1|L2] <需求> 提交任务（站点用 site_hint 或 Web 下拉）\n"
            "站点示例: haobo / gangdao / 港岛 / 好博体育（platform-figma-list）\n"
            "/status <task_id> 查询状态\n"
            "/history 最近任务\n"
            "/continue <task_id> L2 核准\n"
            "/reply <task_id> <澄清> 需求反问后回复\n"
            "/cancel <task_id> 取消任务\n"
            "/help 显示帮助",
            chat_id=chat_id
        )


def restore_pending_gates():
    """Bot 启动时恢复 waiting_gate / waiting_clarification 任务通知"""
    conn = sqlite3.connect(str(DB_FILE))
    for state_val, hint_tpl in (
        (State.WAITING_GATE.value, "/continue {tid}"),
        (State.WAITING_CLARIFICATION.value, "/reply {tid} <你的回答>"),
    ):
        cursor = conn.execute(
            "SELECT task_id, raw_requirement, chat_id, gate_deadline, clarification_deadline FROM task_queue WHERE current_state = ?",
            (state_val,),
        )
        rows = cursor.fetchall()
        for row in rows:
            tid, req, cid = row[0], row[1], row[2]
            deadline = row[3] if state_val == State.WAITING_GATE.value else (row[4] if len(row) > 4 else "")
            if deadline and datetime.fromisoformat(deadline) < datetime.now():
                transition(tid, State.CANCELLED, "timeout on bot restart")
                send_message(f"任务 {tid} 已超时取消", chat_id=cid)
                continue
            label = "L2 核准" if state_val == State.WAITING_GATE.value else "需求澄清"
            send_message(
                f"<b>Bot 已重启，{label}待处理</b>\n任务ID: <code>{tid}</code>\n"
                f"请回复: <code>{hint_tpl.format(tid=tid)}</code>",
                chat_id=cid,
            )
    conn.close()


def poll_updates():
    offset = 0
    while True:
        try:
            url = f"{TELEGRAM_API}/getUpdates?offset={offset}&timeout={POLL_TIMEOUT}"
            req = Request(url, method="GET",
                          headers={"Accept": "application/json"})
            with urlopen(req, timeout=POLL_TIMEOUT + 10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                if "message" in update:
                    handle_command(update["message"])
        except Exception as e:
            print(f"[Poll error] {e}")
            time.sleep(5)


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        print("[ERROR] TELEGRAM_BOT_TOKEN 未设置")
        sys.exit(1)
    init_db()
    restore_pending_gates()
    print("[Headless Agent V3 Telegram Bot] 启动中...")
    send_message("Android Headless Agent V3 Bot 已上线\n发送 /help 查看命令")
    poll_updates()
