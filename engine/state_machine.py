#!/usr/bin/env python3
"""
LangGraph-style State Machine for Headless Agent
提供状态定义、流转、持久化到 SQLite（WAL 模式 + 原子事务）
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from utils.logging_config import get_logger
from utils.paths import DATA_DIR

DB_FILE = DATA_DIR / "agent.db"
logger = get_logger(__name__)


class State(Enum):
    PENDING = "pending"
    PLANNING = "planning"
    WAITING_CLARIFICATION = "waiting_clarification"
    DEBATING = "debating"
    CONSENSUS = "consensus"
    ARCHITECT_PLANNING = "architect_planning"
    WAITING_GATE = "waiting_gate"
    DIRECT_ANSWER = "direct_answer"
    DESIGN_OUTPUT = "design_output"
    CODING = "coding"
    BUILDING = "building"
    SELF_REVIEW = "self_review"
    CODEX_REVIEW = "codex_review"
    ARCHITECT_REVIEW = "architect_review"
    RED_TEAM_REVIEW = "red_team_review"
    REQUIREMENT_REVIEW = "requirement_review"
    CORRECTING = "correcting"
    GIT_COMMITTING = "git_committing"
    CREATING_PR = "creating_pr"
    NOTIFYING = "notifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


VALID_TRANSITIONS = {
    State.PENDING: [State.PLANNING, State.CANCELLED],
    State.PLANNING: [State.DEBATING, State.CODING, State.WAITING_CLARIFICATION, State.DIRECT_ANSWER, State.ARCHITECT_PLANNING, State.CONSENSUS, State.CANCELLED],
    State.WAITING_CLARIFICATION: [State.PENDING, State.CANCELLED],
    State.DEBATING: [State.CONSENSUS, State.CORRECTING, State.CANCELLED],
    State.CONSENSUS: [State.ARCHITECT_PLANNING, State.WAITING_GATE, State.CANCELLED],
    State.ARCHITECT_PLANNING: [State.CODING, State.DESIGN_OUTPUT, State.WAITING_CLARIFICATION, State.CANCELLED],
    State.WAITING_GATE: [State.CODING, State.CANCELLED, State.PENDING, State.WAITING_GATE],
    State.DIRECT_ANSWER: [State.COMPLETED, State.FAILED, State.CANCELLED],
    State.DESIGN_OUTPUT: [State.COMPLETED, State.FAILED, State.CANCELLED],
    State.CODING: [State.BUILDING, State.CORRECTING, State.FAILED, State.CANCELLED],
    State.BUILDING: [State.SELF_REVIEW, State.CORRECTING, State.FAILED, State.CANCELLED],
    State.SELF_REVIEW: [State.CODEX_REVIEW, State.CORRECTING, State.FAILED, State.CANCELLED],
    State.CODEX_REVIEW: [State.ARCHITECT_REVIEW, State.RED_TEAM_REVIEW, State.CORRECTING, State.FAILED, State.CANCELLED],
    State.ARCHITECT_REVIEW: [State.RED_TEAM_REVIEW, State.CORRECTING, State.FAILED, State.CANCELLED],
    State.RED_TEAM_REVIEW: [State.REQUIREMENT_REVIEW, State.CORRECTING, State.FAILED, State.CANCELLED],
    State.REQUIREMENT_REVIEW: [State.GIT_COMMITTING, State.CORRECTING, State.FAILED, State.CANCELLED],
    State.CORRECTING: [State.CODING, State.WAITING_CLARIFICATION, State.FAILED, State.CANCELLED],
    State.GIT_COMMITTING: [State.CREATING_PR, State.CANCELLED],
    State.CREATING_PR: [State.NOTIFYING, State.CANCELLED],
    State.NOTIFYING: [State.COMPLETED, State.FAILED, State.CANCELLED],
}


@dataclass
class Task:
    task_id: str
    raw_requirement: str
    level: str
    site_hint: str
    source: str
    chat_id: str
    status: str = "pending"
    current_state: str = "pending"
    attempt_count: int = 0
    max_retries: int = 3
    pr_url: str = ""
    branch: str = ""
    error_log: str = ""
    base_branch: str = ""
    created_at: str = ""
    updated_at: str = ""
    gate_deadline: str = ""
    resume_from_gate: int = 0
    clarification_deadline: str = ""
    resume_after_clarification: int = 0
    # 各阶段重试计数器，如 {"codex": 1, "red_team": 0, "acceptance": 2}
    phase_counters: dict = field(default_factory=dict)
    # 代码级澄清历史（architect_planning / correcting 阶段的不确定性）
    code_clarification_history: list = field(default_factory=list)
    # 澄清类型来源标记："planning" | "code"
    clarification_type: str = ""
    # 请求类型：code | explain | review_only | design_only
    request_type: str = "code"




def _connect() -> sqlite3.Connection:
    """创建启用 WAL 模式的连接（使用列名访问）"""
    conn = sqlite3.connect(str(DB_FILE), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _migrate_schema(conn: sqlite3.Connection):
    """向后兼容：为旧库补充新列"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(task_queue)").fetchall()}
    if "resume_from_gate" not in cols:
        conn.execute("ALTER TABLE task_queue ADD COLUMN resume_from_gate INTEGER DEFAULT 0")
    if "clarification_deadline" not in cols:
        conn.execute("ALTER TABLE task_queue ADD COLUMN clarification_deadline TEXT DEFAULT ''")
    if "resume_after_clarification" not in cols:
        conn.execute("ALTER TABLE task_queue ADD COLUMN resume_after_clarification INTEGER DEFAULT 0")
    if "phase_counters" not in cols:
        conn.execute("ALTER TABLE task_queue ADD COLUMN phase_counters TEXT DEFAULT '{}'")
    if "code_clarification_history" not in cols:
        conn.execute("ALTER TABLE task_queue ADD COLUMN code_clarification_history TEXT DEFAULT '[]'")
    if "clarification_type" not in cols:
        conn.execute("ALTER TABLE task_queue ADD COLUMN clarification_type TEXT DEFAULT ''")
    if "request_type" not in cols:
        conn.execute("ALTER TABLE task_queue ADD COLUMN request_type TEXT DEFAULT 'code'")


def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT UNIQUE NOT NULL,
            raw_requirement TEXT NOT NULL,
            level TEXT NOT NULL,
            site_hint TEXT,
            source TEXT NOT NULL,
            chat_id TEXT,
            status TEXT DEFAULT 'pending',
            current_state TEXT DEFAULT 'pending',
            attempt_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3,
            pr_url TEXT,
            branch TEXT,
            base_branch TEXT,
            error_log TEXT,
            created_at TEXT,
            updated_at TEXT,
            gate_deadline TEXT,
            resume_from_gate INTEGER DEFAULT 0,
            phase_counters TEXT DEFAULT '{}',
            code_clarification_history TEXT DEFAULT '[]',
            clarification_type TEXT DEFAULT '',
            request_type TEXT DEFAULT 'code'
        )
    """)
    _migrate_schema(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT NOT NULL,
            reason TEXT,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_task(task: Task) -> Task:
    conn = _connect()
    now = datetime.now().isoformat()
    if not task.created_at:
        task.created_at = now
    task.updated_at = now
    _migrate_schema(conn)
    conn.execute("""
        INSERT OR REPLACE INTO task_queue
        (task_id, raw_requirement, level, site_hint, source, chat_id, status,
         current_state, attempt_count, max_retries, pr_url, branch, base_branch,
         error_log, created_at, updated_at, gate_deadline, resume_from_gate,
         clarification_deadline, resume_after_clarification, phase_counters,
         code_clarification_history, clarification_type, request_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        task.task_id, task.raw_requirement, task.level, task.site_hint,
        task.source, task.chat_id, task.status, task.current_state,
        task.attempt_count, task.max_retries, task.pr_url, task.branch,
        task.base_branch, task.error_log, task.created_at, task.updated_at,
        task.gate_deadline, int(task.resume_from_gate or 0),
        task.clarification_deadline or "",
        int(task.resume_after_clarification or 0),
        json.dumps(task.phase_counters or {}),
        json.dumps(task.code_clarification_history or []),
        task.clarification_type or "",
        task.request_type or "code",
    ))
    conn.commit()
    conn.close()
    return task


def get_task(task_id: str) -> Optional[Task]:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM task_queue WHERE task_id = ?", (task_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_task(row)


def transition(task_id: str, to_state: State, reason: str = "", task_obj: Optional[Task] = None) -> bool:
    """原子状态流转：在同一个事务中读取旧状态、校验合法性、更新任务、写入历史"""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM task_queue WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            conn.close()
            return False

        task = _row_to_task(row)
        from_state = State(task.current_state)
        if to_state not in VALID_TRANSITIONS.get(from_state, []):
            logger.error(f"非法状态流转: {from_state.value} -> {to_state.value}")
            conn.execute("ROLLBACK")
            conn.close()
            return False

        now = datetime.now().isoformat()
        conn.execute(
            """
            UPDATE task_queue
            SET current_state = ?, updated_at = ?, status = ?
            WHERE task_id = ?
            """,
            (to_state.value, now, to_state.value, task_id),
        )
        conn.execute(
            "INSERT INTO state_history (task_id, from_state, to_state, reason, timestamp) VALUES (?, ?, ?, ?, ?)",
            (task_id, from_state.value, to_state.value, reason, now),
        )
        conn.commit()
        logger.info(f"{task_id}: {from_state.value} -> {to_state.value} | {reason}")
        if task_obj is not None:
            task_obj.current_state = to_state.value
            task_obj.status = to_state.value
        return True
    except sqlite3.OperationalError as e:
        logger.error(f"SQLite operational error: {e}")
        conn.execute("ROLLBACK")
        return False
    finally:
        conn.close()


def get_pending_tasks(limit: int = 10) -> list[Task]:
    return get_executable_tasks(limit)


def get_executable_tasks(limit: int = 10) -> list[Task]:
    """待执行队列：新任务 pending；或 L2 / 澄清 核准后带 resume 标志的 pending"""
    conn = _connect()
    cursor = conn.execute(
        """
        SELECT * FROM task_queue
        WHERE current_state = ?
        ORDER BY resume_from_gate DESC, resume_after_clarification DESC, created_at ASC
        LIMIT ?
        """,
        (State.PENDING.value, limit),
    )
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_task(r) for r in rows]


def approve_gate_resume(task_id: str) -> bool:
    """L2 /continue：原子地回到 pending 队列，由 Executor 从编码阶段续跑"""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM task_queue WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            conn.close()
            return False

        task = _row_to_task(row)
        if task.current_state != State.WAITING_GATE.value:
            conn.execute("ROLLBACK")
            conn.close()
            return False

        from_state = task.current_state
        now = datetime.now().isoformat()
        conn.execute(
            """
            UPDATE task_queue
            SET current_state = ?, resume_from_gate = ?, gate_deadline = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (State.PENDING.value, 1, "", now, task_id),
        )
        conn.execute(
            "INSERT INTO state_history (task_id, from_state, to_state, reason, timestamp) VALUES (?, ?, ?, ?, ?)",
            (task_id, from_state, State.PENDING.value, "L2 gate approved, resume coding phase", now),
        )
        conn.commit()
        logger.info(f"{task_id}: {from_state} -> pending (resume_from_gate=1)")
        return True
    except sqlite3.OperationalError as e:
        logger.error(f"SQLite operational error: {e}")
        conn.execute("ROLLBACK")
        return False
    finally:
        conn.close()


def get_all_non_terminal_tasks() -> list[Task]:
    """获取所有未到达终态的任务"""
    terminal = {State.COMPLETED.value, State.FAILED.value, State.CANCELLED.value}
    conn = _connect()
    cursor = conn.execute(
        "SELECT * FROM task_queue WHERE current_state NOT IN (?, ?, ?)",
        tuple(terminal),
    )
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_task(r) for r in rows]


def get_task_state_history(task_id: str) -> list[dict]:
    """获取任务的状态流转历史，按时间升序"""
    conn = _connect()
    cursor = conn.execute(
        "SELECT from_state, to_state, reason, timestamp FROM state_history WHERE task_id = ? ORDER BY timestamp ASC",
        (task_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {"from_state": r[0], "to_state": r[1], "reason": r[2], "timestamp": r[3]}
        for r in rows
    ]


def get_waiting_gates() -> list[Task]:
    conn = _connect()
    cursor = conn.execute(
        "SELECT * FROM task_queue WHERE current_state = ?",
        (State.WAITING_GATE.value,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_task(r) for r in rows]


def get_waiting_clarifications() -> list[Task]:
    conn = _connect()
    cursor = conn.execute(
        "SELECT * FROM task_queue WHERE current_state = ?",
        (State.WAITING_CLARIFICATION.value,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_task(r) for r in rows]


def approve_clarification_reply(task_id: str, user_reply: str) -> bool:
    """用户澄清后回到 pending，Executor 从辩论前续跑（跳过 intake）"""
    reply = (user_reply or "").strip()
    if not reply:
        return False
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM task_queue WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            conn.close()
            return False

        task = _row_to_task(row)
        if task.current_state != State.WAITING_CLARIFICATION.value:
            conn.execute("ROLLBACK")
            conn.close()
            return False

        merged_req = task.raw_requirement.rstrip() + "\n\n[用户澄清]\n" + reply
        from_state = task.current_state
        now = datetime.now().isoformat()
        conn.execute(
            """
            UPDATE task_queue
            SET current_state = ?, raw_requirement = ?, resume_after_clarification = ?,
                clarification_deadline = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (State.PENDING.value, merged_req, 1, "", now, task_id),
        )
        conn.execute(
            "INSERT INTO state_history (task_id, from_state, to_state, reason, timestamp) VALUES (?, ?, ?, ?, ?)",
            (task_id, from_state, State.PENDING.value, "user clarification received", now),
        )
        conn.commit()
        logger.info(f"{task_id}: {from_state} -> pending (resume_after_clarification=1)")
        return True
    except sqlite3.OperationalError as e:
        logger.error(f"SQLite operational error: {e}")
        conn.execute("ROLLBACK")
        return False
    finally:
        conn.close()


def cancel_task(task_id: str, reason: str = "user cancelled") -> bool:
    """取消任务：从任意非终态强制流转到 CANCELLED，无需经过常规 transition 校验"""
    terminal = {State.COMPLETED.value, State.FAILED.value, State.CANCELLED.value}
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM task_queue WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            conn.close()
            return False

        current = row["current_state"]
        if current in terminal:
            conn.execute("ROLLBACK")
            conn.close()
            logger.warning(f"Task {task_id} already in terminal state {current}")
            return False

        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE task_queue SET current_state = ?, updated_at = ?, status = ? WHERE task_id = ?",
            (State.CANCELLED.value, now, State.CANCELLED.value, task_id),
        )
        conn.execute(
            "INSERT INTO state_history (task_id, from_state, to_state, reason, timestamp) VALUES (?, ?, ?, ?, ?)",
            (task_id, current, State.CANCELLED.value, reason, now),
        )
        conn.commit()
        logger.info(f"{task_id}: {current} -> cancelled | {reason}")
        try:
            from utils.task_cancel import interrupt_running_work

            interrupt_running_work(task_id)
        except Exception as e:
            logger.warning("interrupt_running_work after cancel %s: %s", task_id, e)
        return True
    except sqlite3.OperationalError as e:
        logger.error(f"{e}")
        conn.execute("ROLLBACK")
        return False
    finally:
        conn.close()


def _row_to_task(row: sqlite3.Row) -> Task:
    """通过列名映射构造 Task，避免硬编码索引"""
    return Task(
        task_id=row["task_id"],
        raw_requirement=row["raw_requirement"],
        level=row["level"],
        site_hint=row["site_hint"] or "",
        source=row["source"],
        chat_id=row["chat_id"] or "",
        status=row["status"] or "pending",
        current_state=row["current_state"] or "pending",
        attempt_count=row["attempt_count"] or 0,
        max_retries=row["max_retries"] or 3,
        pr_url=row["pr_url"] or "",
        branch=row["branch"] or "",
        base_branch=row["base_branch"] or "",
        error_log=row["error_log"] or "",
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
        gate_deadline=row["gate_deadline"] or "",
        resume_from_gate=row["resume_from_gate"] or 0,
        clarification_deadline=row["clarification_deadline"] if "clarification_deadline" in row.keys() else "",
        resume_after_clarification=row["resume_after_clarification"] if "resume_after_clarification" in row.keys() else 0,
        phase_counters=json.loads(row["phase_counters"]) if "phase_counters" in row.keys() and row["phase_counters"] else {},
        code_clarification_history=json.loads(row["code_clarification_history"]) if "code_clarification_history" in row.keys() and row["code_clarification_history"] else [],
        clarification_type=row["clarification_type"] if "clarification_type" in row.keys() else "",
        request_type=row["request_type"] if "request_type" in row.keys() else "code",
    )


if __name__ == "__main__":
    init_db()
    logger.info(f"Initialized at {DB_FILE}")
