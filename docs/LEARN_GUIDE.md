# AICodeAgent 完整学习文档

> 适用版本：V4 | 作者：AI 生成 | 更新：2026-05-24  
> 本文档面向**完全不了解该项目**的学习者，从架构全貌到每个模块逐层拆解。

---

## 目录

1. [系统是什么？一句话说清楚](#1-系统是什么)
2. [整体架构：鸟瞰图](#2-整体架构)
3. [核心概念速查表](#3-核心概念速查表)
4. **模块精讲**
   - [模块 A：状态机与数据层](#模块-a-状态机与数据层--engine/state_machiney)
   - [模块 B：引擎核心](#模块-b-引擎核心--enginecorepy)
   - [模块 C：执行器入口（主循环）](#模块-c-执行器入口--enginerunnerpy)
   - [模块 D：阶段处理器体系](#模块-d-阶段处理器体系--phases)
   - [模块 E：服务层](#模块-e-服务层--services)
   - [模块 F：工具层](#模块-f-工具层--utils)
   - [模块 G：网关层](#模块-g-网关层--gateway)
5. [关键设计模式拆解](#5-关键设计模式拆解)
6. [完整任务流水线演示](#6-完整任务流水线演示)
7. [LangGraph vs 本项目的状态机：相似与区别](#7-langgraph-vs-本项目的状态机)
8. [LangSmith 可观测性详解](#8-langsmith-可观测性详解)
9. [优点与缺点](#9-优点与缺点)
10. [如何上手修改](#10-如何上手修改)

---

## 1. 系统是什么？

**AICodeAgent** 是一个 **全自动 Android 代码流水线**。

你只需要用中文发一条需求（比如"修复 VIP 滑动 bug"），它会：

```
用户发需求  →  AI 理解分类  →  多个 AI 辩论方案  →  Claude 写代码
    →  Gradle 编译验证  →  多层 AI 审查  →  自动 Git 提交 + 创建 PR  →  Telegram 通知你
```

**核心思路：** 把"人类程序员写代码"这个过程拆成多个步骤，每步用不同的 AI 角色完成，并用一个状态机管理每步之间的流转。

**技术栈：**
- 语言：Python 3.14
- AI 驱动：Claude Code CLI（通过 `subprocess` 调用命令行）
- 数据库：SQLite（WAL 模式）
- 可观测性：LangSmith（可选）
- 通知：Telegram Bot
- Web UI：纯 Python HTTP 服务（无框架）

---

## 2. 整体架构

### 2.1 层次结构图

```
┌─────────────────────────────────────────────────────────┐
│                    触发层 (Gateway)                       │
│   Web UI :6789 (/api/task)   Telegram Bot (/task)        │
└────────────────────────┬────────────────────────────────┘
                         │ 写入任务到 SQLite
                         ▼
┌─────────────────────────────────────────────────────────┐
│                   数据层 (State Machine)                  │
│   SQLite WAL   task_queue + state_history 两张表          │
│   State Enum   23 个状态定义                              │
│   Task 对象    任务数据结构（Python dataclass）            │
└────────────────────────┬────────────────────────────────┘
                         │ dequeue
                         ▼
┌─────────────────────────────────────────────────────────┐
│                   引擎层 (Engine)                         │
│   runner.py     主循环（轮询 SQLite、调度任务）            │
│   core.py       AgentEngine（状态 dispatch + 异常统一处理）│
│   state_machine.py  transition() 原子状态流转             │
└────────────────────────┬────────────────────────────────┘
                         │ dispatch
                         ▼
┌─────────────────────────────────────────────────────────┐
│                  阶段层 (Phases)                          │
│  Planning → Debate → Consensus → Coding → Building        │
│  → SelfReview → CodexReview → ArchitectReview             │
│  → RedTeamReview → RequirementReview                      │
│  → Correcting → GitCommitting → CreatingPR → Notifying    │
└────────────────────────┬────────────────────────────────┘
                         │ 调用
                         ▼
┌─────────────────────────────────────────────────────────┐
│                   服务层 (Services)                       │
│  AIClient      subprocess 调用 claude 命令行              │
│  GitService    创建分支、写文件、commit、push              │
│  BuildService  调用 Gradle 编译                           │
│  RequestClassifier  需求类型路由分类                      │
└─────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│                   工具层 (Utils)                          │
│  config_loader   三层配置（YAML + 环境变量）               │
│  tracing.py      LangSmith 可观测性                       │
│  escape_detector 无限循环检测                              │
│  memory_context  腾讯 DB 长期记忆                          │
└─────────────────────────────────────────────────────────┘
```

### 2.2 文件目录结构

```
AICodeAgent/
├── engine/               ← 引擎层
│   ├── state_machine.py  ← 状态定义 + SQLite 操作
│   ├── core.py           ← AgentEngine（dispatch 核心）
│   ├── runner.py         ← 主循环入口 + build_engine()
│   ├── exceptions.py     ← 异常体系
│   └── config_validator.py
│
├── phases/               ← 阶段处理器（每个状态对应一个文件）
│   ├── base.py           ← PhaseHandler 抽象基类
│   ├── planning.py       ← 需求分析 + 路由分类
│   ├── debate.py         ← 三方 AI 辩论
│   ├── consensus.py      ← 汇总辩论结果
│   ├── coding.py         ← Claude 编码
│   ├── building.py       ← Gradle 构建
│   ├── self_review.py    ← AI 自审查
│   ├── codex_review.py   ← 逻辑/漏洞审查
│   ├── architect_review.py  ← 架构审查
│   ├── red_team_review.py   ← 红队攻击审查
│   ├── requirement_review.py ← 需求验收
│   ├── correcting.py     ← 错误修复调度
│   ├── git_committing.py ← Git 提交
│   ├── creating_pr.py    ← 创建 PR
│   ├── notifying.py      ← 发送通知
│   └── _review_utils.py  ← 审查工具函数（共享）
│
├── services/             ← 服务层
│   ├── ai_client.py      ← Claude CLI 封装
│   ├── git_service.py    ← Git 操作
│   ├── build_service.py  ← Gradle 构建
│   ├── request_classifier.py  ← 需求路由
│   ├── task_service.py   ← 任务 CRUD
│   └── notification_service.py
│
├── utils/                ← 工具层
│   ├── config_loader.py  ← 三层配置
│   ├── tracing.py        ← LangSmith
│   ├── escape_detector.py ← 无限循环检测
│   ├── memory_context.py ← 长期记忆
│   ├── paths.py          ← 路径常量
│   └── logging_config.py
│
├── gateway/              ← 触发层（网关）
│   ├── web_ui.py         ← HTTP 服务
│   └── telegram_bot.py   ← Telegram Bot
│
├── config/
│   ├── default.yaml      ← 默认配置
│   └── local.yaml        ← 本地覆盖（不提交 git）
│
└── data/
    ├── agent.db          ← SQLite 数据库（运行时生成）
    └── uploads/          ← 图片上传目录
```

---

## 3. 核心概念速查表

| 概念 | 解释 |
|------|------|
| **State（状态）** | 任务当前处于流水线的哪个步骤（Python `Enum`，23 个值） |
| **Task（任务）** | 一条用户需求，用 Python `dataclass` 表示，存在 SQLite 里 |
| **PhaseHandler** | 每个状态对应的处理器类，必须实现 `handle()` 方法 |
| **PhaseResult** | `handle()` 返回值：包含 `next_state`（下一状态）和 `reason` |
| **transition()** | 原子状态流转函数，失败不会部分更新（SQLite 事务保证） |
| **AgentEngine** | 核心引擎，维护 `状态→处理器` 的注册表，驱动 while 循环 |
| **VALID_TRANSITIONS** | 合法状态转换表，防止非法跳转 |
| **LangSmith** | 可观测性平台，记录每个阶段的输入输出供调试 |
| **TaskTracer** | LangSmith 封装，用 `with` 语句管理任务级追踪 |
| **workspace** | 每个任务的临时工作目录 `data/workspace/{task_id}/` |

---

## 模块 A：状态机与数据层 — `engine/state_machine.py`

### A.1 这个文件做什么？

这是整个系统的**数据核心**，负责：
1. 定义所有状态（`State` 枚举）
2. 定义任务数据结构（`Task` dataclass）
3. 定义合法状态转换（`VALID_TRANSITIONS` 字典）
4. 操作 SQLite 数据库（建表、读写、原子转换）

### A.2 State 枚举 —— 23 个状态

```python
class State(Enum):
    # 入队
    PENDING = "pending"           # 等待执行

    # 规划阶段
    PLANNING = "planning"         # 需求分析 + 路由分类
    WAITING_CLARIFICATION = "waiting_clarification"  # 等用户澄清需求

    # 方案制定（L1/L2 专用）
    DEBATING = "debating"         # 三方 AI 辩论
    CONSENSUS = "consensus"       # 汇总辩论结论
    ARCHITECT_PLANNING = "architect_planning"  # 架构规划（design_only 路径）
    WAITING_GATE = "waiting_gate" # 等人工审核（L2 专用）

    # 快速路径
    DIRECT_ANSWER = "direct_answer"  # 直接回答（explain 路径）
    DESIGN_OUTPUT = "design_output"  # 输出设计文档

    # 编码 + 构建
    CODING = "coding"             # Claude 编写代码
    BUILDING = "building"         # Gradle 编译

    # 审查流水线（可配置开关）
    SELF_REVIEW = "self_review"           # 快速自查
    CODEX_REVIEW = "codex_review"         # 逻辑/漏洞审查
    ARCHITECT_REVIEW = "architect_review" # 架构合规性审查
    RED_TEAM_REVIEW = "red_team_review"   # 红队攻击视角
    REQUIREMENT_REVIEW = "requirement_review"  # 需求验收

    # 修复 + 完成
    CORRECTING = "correcting"     # 生成修复计划，重回 CODING
    GIT_COMMITTING = "git_committing"  # git commit + push
    CREATING_PR = "creating_pr"   # 创建 GitHub PR
    NOTIFYING = "notifying"       # 发 Telegram 通知

    # 终态
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

**学习重点：** 状态名和文件名是一一对应的。看到 `CODEX_REVIEW`，就找 `phases/codex_review.py`。

### A.3 合法转换表（节选）

```python
VALID_TRANSITIONS = {
    State.PLANNING: [
        State.DEBATING,              # L1/L2 代码任务
        State.CODING,                # L0 快路径
        State.WAITING_CLARIFICATION, # 需求不明确
        State.DIRECT_ANSWER,         # explain 类问题
        State.ARCHITECT_PLANNING,    # design_only 类
        State.CONSENSUS,             # review_only（直接进审查）
        State.CODEX_REVIEW,          # 同上
        State.CANCELLED,
    ],
    State.CODING: [
        State.BUILDING,              # 代码写完，去编译
        State.CORRECTING,            # 编码失败（空输出等）
        State.FAILED,
        State.CANCELLED,
    ],
    State.BUILDING: [
        State.SELF_REVIEW,           # 编译通过，先自查
        State.CODEX_REVIEW,          # 跳过自查
        State.GIT_COMMITTING,        # L0 编译通过直接提交
        State.CORRECTING,            # 编译失败
        ...
    ],
}
```

**设计思路：** 用显式白名单取代 `if/elif` 硬编码。新增状态时只需在这里加一行，引擎自动校验。

**优点：** 防止 bug 导致状态乱跳，比如不可能从 `CODING` 直接跳到 `COMPLETED`。  
**缺点：** 修改转换关系必须同时改两处（转换表 + 处理器的返回值），容易遗漏。

### A.4 Task dataclass

```python
@dataclass
class Task:
    task_id: str             # 唯一 ID（UUID）
    raw_requirement: str     # 原始需求文本
    level: str               # L0/L1/L2/auto
    site_hint: str           # 目标站点（如 haobo）
    source: str              # 来源（telegram/web）
    chat_id: str             # Telegram chat ID
    current_state: str       # 当前状态（与 State.value 对应）
    attempt_count: int       # 全局重试次数
    phase_counters: dict     # 各阶段重试次数（如 correcting_build: 2）
    request_type: str        # code/explain/review_only/design_only
    resume_from_gate: int    # 是否从 L2 人工闸门恢复（0/1）
    resume_after_clarification: int  # 是否从澄清恢复
    ...
```

**学习重点：** `phase_counters` 是一个字典，用来记录各个阶段分别重试了几次，避免所有错误共用一个全局计数器导致过早放弃。

### A.5 `transition()` 函数 —— 原子状态转换

```python
def transition(task_id, to_state, reason="", task_obj=None) -> bool:
    conn = _connect()
    conn.execute("BEGIN IMMEDIATE")   # 获取写锁，原子操作开始
    
    row = conn.execute("SELECT * FROM task_queue WHERE task_id=?", ...)
    from_state = State(row["current_state"])
    
    if to_state not in VALID_TRANSITIONS.get(from_state, []):
        conn.execute("ROLLBACK")
        return False                  # 非法转换，拒绝
    
    # 同一事务中更新任务状态 + 写入历史记录
    conn.execute("UPDATE task_queue SET current_state=? ...", ...)
    conn.execute("INSERT INTO state_history ...", ...)
    conn.commit()
```

**为什么用 SQLite WAL 模式？**  
WAL（Write-Ahead Logging）允许读写并发：Web UI 查询任务状态时不会阻塞 Executor 写入，降低锁争用。

**为什么用 `BEGIN IMMEDIATE`？**  
普通 `BEGIN` 开始时不锁定，读取后再写入之间可能有其他进程插入。`BEGIN IMMEDIATE` 立即获取写锁，保证原子性。

---

## 模块 B：引擎核心 — `engine/core.py`

### B.1 这个文件做什么？

`AgentEngine` 是整个系统的**调度大脑**。它负责：
1. 维护 `状态 → PhaseHandler` 的注册表
2. 驱动 `while True` 循环，每轮 dispatch 到对应处理器
3. 统一捕获各种异常并转换为状态流转
4. 集成 LangSmith 追踪

### B.2 AgentEngine 类结构

```python
class AgentEngine:
    def __init__(self):
        self._handlers: dict[State, PhaseHandler] = {}  # 注册表

    def register(self, state: State, handler: PhaseHandler):
        """注册处理器"""
        self._handlers[state] = handler

    def process_task(self, task: Task):
        """驱动任务直到终态或等待态"""
        with trace_task(...) as tracer:        # LangSmith 追踪
            while True:
                current = State(task.current_state)
                handler = self._handlers[current]
                result = self._execute_phase(handler, task, workspace)
                
                if result.next_state in TERMINAL_STATES:
                    transition(task_id, result.next_state, ...)
                    break
                
                if result.next_state in WAITING_STATES:
                    transition(task_id, result.next_state, ...)
                    break    # 暂停，等外部事件
                
                transition(task_id, result.next_state, ...)
                # 循环继续，处理下一状态
```

### B.3 异常统一处理 —— `_execute_phase()`

这是最重要的设计之一：

```python
def _execute_phase(self, handler, task, workspace):
    try:
        result = handler.handle(task, workspace)
        return result

    except AgentRecoverableError as e:
        # 可恢复错误 → 流转到 CORRECTING 重试
        return PhaseResult(State.CORRECTING, f"recoverable: {e}")

    except AgentFatalError as e:
        # 致命错误 → 流转到 FAILED
        return PhaseResult(State.FAILED, f"fatal: {e}")

    except TaskCancelledError:
        # 用户取消 → 停止
        raise

    except Exception as e:
        # 未知错误 → 也流转到 FAILED（防止系统卡死）
        return PhaseResult(State.FAILED, f"unexpected: {e}")
```

**优点：** 各个 Phase 只需关心业务逻辑，不用关心"出错了怎么办"，统一由引擎处理。  
**缺点：** 如果 Phase 内部发生了本该是 `AgentFatalError` 的错误却抛了 `Exception`，会被兜底捕获，但仍能流转到 FAILED，不会卡死。

### B.4 取消机制

```python
# 每轮循环开始都检查一次
if self._stop_if_cancelled(task_id, task, workspace):
    break

# _stop_if_cancelled 从数据库重新读取状态
fresh = get_task(task_id)
if fresh.current_state == State.CANCELLED.value:
    return True  # 停止
```

这样即使任务正在执行，用户在 Web UI 点"取消"，最迟在当前阶段结束后就会停下来。

---

## 模块 C：执行器入口 — `engine/runner.py`

### C.1 这个文件做什么？

`runner.py` 是程序的**主入口**，包含：
1. `build_engine()` —— 创建 AgentEngine 并注册所有 PhaseHandler
2. `run_loop()` —— 主循环（轮询数据库 → 取任务 → 执行）
3. `process_task_v4()` —— 单次任务处理（含环境快照/恢复）
4. 文件锁（保证单实例运行）

### C.2 `build_engine()` —— 依赖注入

```python
def build_engine() -> AgentEngine:
    # 1. 创建共享服务（单例）
    ai_client = AIClient()
    build_service = BuildService()
    git_service = GitService()
    notification_service = NotificationService()

    # 2. 创建引擎
    engine = AgentEngine()

    # 3. 注册所有阶段处理器（把服务注入进去）
    engine.register(State.PLANNING, PlanningHandler(
        ai_client=ai_client,
        notification_service=notification_service,
    ))
    engine.register(State.CODING, CodingHandler(
        ai_client=ai_client,
        git_service=git_service,
    ))
    engine.register(State.BUILDING, BuildingHandler(
        build_service=build_service,
    ))
    # ... 其他 handler ...
    return engine
```

**设计模式：依赖注入（Dependency Injection）**  
各个 Handler 不自己创建 `AIClient`，而是由外部传入。好处：
- 测试时可以传入 Mock 对象
- 服务对象只创建一次，复用连接和配置

### C.3 主循环

```python
def run_loop():
    init_db()                     # 初始化 SQLite 表
    engine = build_engine()       # 构建引擎（一次性）
    lock_fd = acquire_lock()      # 文件锁（防止多进程）
    
    while not _shutdown_requested:
        tasks = get_executable_tasks(limit=1)  # 从 SQLite 取一条 pending 任务
        
        if not tasks:
            time.sleep(2)          # 没任务就等 2 秒
            continue
        
        task = tasks[0]
        process_task_v4(task, engine)  # 执行任务
        time.sleep(1)              # 短暂停顿，避免 CPU 空转
```

**为什么不用异步/多线程？**  
任务串行执行（一次只处理一个）。原因：
- Claude CLI 调用是同步的
- Gradle 编译也是串行的（并行构建会竞争文件）
- 简单可靠，不用担心并发 bug

### C.4 环境快照与恢复

```python
# 任务开始前记录当前 git 分支
original_branch, _ = _snapshot_git_state()

try:
    engine.process_task(task)
finally:
    # 任务结束后恢复环境（无论成功失败）
    _restore_environment(task, original_branch)
```

如果任务编写代码后失败了，`_restore_environment` 会：
1. `git stash` 保存未提交变更
2. `git checkout` 切回原始分支
3. `git branch -D` 删除 agent 分支（如果未合并）

---

## 模块 D：阶段处理器体系 — `phases/`

### D.1 基类设计 —— `phases/base.py`

所有阶段处理器都继承 `PhaseHandler`：

```python
class PhaseHandler(ABC):
    
    @abstractmethod
    def handle(self, task: Task, workspace: Path) -> PhaseResult:
        """核心逻辑，必须实现"""
        ...

    def can_handle(self, task: Task) -> bool:
        """能否处理此任务（默认 True，子类可覆盖）"""
        return True

    def on_enter(self, task: Task, workspace: Path) -> None:
        """进入阶段前的钩子（可选）"""
        pass

    def on_exit(self, task, workspace, result) -> None:
        """离开阶段后的钩子（可选）"""
        pass
```

```python
@dataclass
class PhaseResult:
    next_state: State       # 下一个状态
    reason: str = ""        # 流转原因（写入 state_history）
    artifacts: dict = ...   # 中间产物（供后续阶段使用）
```

**设计模式：模板方法（Template Method）**  
引擎调用 `on_enter → handle → on_exit`，子类只需实现 `handle`。

### D.2 Planning 阶段 —— 需求理解与路由

**文件：** `phases/planning.py`  
**职责：** 分析需求，决定走哪条流水线

```
handle() 执行流程：

1. 自动判定 level（L0/L1/L2/auto）
         ↓
2. 请求类型分类（调用 RequestClassifier）
   code / explain / review_only / design_only
         ↓
3. 生成构建策略（build_policy.json）
         ↓
4. 生成需求文档（requirement.md）
         ↓
5. 准备资产上下文（asset_analysis.json）
         ↓
6. 需求澄清判断
   → 不明确 → WAITING_CLARIFICATION（等用户回复）
         ↓
7. 根据 request_type 路由：
   explain     → DIRECT_ANSWER
   design_only → ARCHITECT_PLANNING
   review_only → CODEX_REVIEW
   code + L0   → CODING（跳过辩论）
   code + L1/2 → DEBATING
```

**Level 自动判定规则（`_auto_level()`）：**

| 关键词 | 判定等级 |
|--------|---------|
| 重构、架构、跨模块、全站、theme | L2（复杂） |
| 文案、颜色、字体、bug 修复、typo | L0（简单） |
| 其他 | L1（中等） |

**澄清判断（`_assess_clarity()`）：**  
检查三个维度，任意缺失则提问：
1. 目标页面/模块是否明确？
2. 目标站点是否指定？
3. 验收标准是否清晰？

L0 任务豁免（最多 2 个模糊点仍放行，因为简单改动通常是显然的）。

### D.3 Debate 阶段 —— 三方 AI 辩论

**文件：** `phases/debate.py`  
**职责：** 三个 AI "角色" 并行分析需求，提出方案

三个角色（通过不同 prompt 实现，实际都是同一个 Claude 模型）：

| 角色 | 职责 | 输出文件 |
|------|------|---------|
| **Architect** | 技术方案、文件清单、接口设计 | `architect_proposal_output.md` |
| **FigmaAuditor** | 视觉资产、UI 组件映射 | `figma_audit_output.md` |
| **Guardian** | 安全风险、回归风险、边界条件 | `guardian_review_output.md` |

```python
# 并行调用三个 Agent
with ThreadPoolExecutor(max_workers=3) as pool:
    futures = []
    for agent_name, prompt_file, output_name in agents:
        future = pool.submit(self._run_single_agent, ...)
        futures.append((future, agent_name, output_name))
```

**为什么用 `ThreadPoolExecutor`？**  
三个 Claude CLI 调用都是阻塞的 subprocess。用线程池可以让它们同时执行，把总时间从 `3×T` 降到 `max(T1, T2, T3)`。

**降级机制：** 如果三方辩论失败，降级为只运行 Architect（单一视角），避免完全卡住。

### D.4 Coding 阶段 —— Claude 编写代码

**文件：** `phases/coding.py`  
**职责：** 调用 Claude CLI 生成代码，并安全写入文件

```python
def handle(self, task, workspace):
    # 1. 创建 git 分支（如果还没有）
    task.branch = git_service.create_agent_branch(task_id, base_branch)

    # 2. 构建编码上下文（consensus.md + 项目规范 + 记忆召回）
    context = self._build_coding_context(task, workspace)

    # 3. 调用 Claude CLI 生成代码
    output = ai_client.call(
        prompt,
        context=context,
        headless=False,  # False = 允许 Edit/Write 直接修改文件
        cwd=PROJECT_ROOT,
    )

    # 4. 解析 Claude 输出中的 === FILE: path === 标记
    # 5. 通过 GitService.apply_code_changes() 写入（安全校验）
```

**Claude 调用的两种模式（`headless` 参数）：**

| 模式 | headless=True | headless=False |
|------|--------------|----------------|
| 权限 | `dontAsk` | `acceptEdits` |
| 工具 | `Read,Glob,Grep`（只读） | `Read,Edit,Write,Glob,Grep` |
| 用途 | 分类、审查、问答 | 编码（可以改文件） |
| max-turns | 5 | 15 |

### D.5 Building 阶段 —— Gradle 编译验证

**文件：** `phases/building.py`  
**职责：** 调用 Gradle 编译，判断是否进入审查或修复

```
构建通过 →
  L0 任务（assemble_only）→ 直接 GIT_COMMITTING（跳过所有审查）
  有 last_fail_stage 记录  → 从断点恢复（跳过已通过的审查）
  正常路径                  → SELF_REVIEW

构建失败 →
  生成 fix_prompt.md（错误摘要 + 修复建议）
  → CORRECTING
```

**断点恢复机制：**  
如果任务在 `codex_review` 失败了，进入 `correcting` 修复后重新编译，编译通过后不需要重新从 `self_review` 走，直接从 `codex_review` 继续，节省时间。

### D.6 审查流水线 —— 四层 AI 审查

```
SELF_REVIEW (自查)
    ↓ PASS
CODEX_REVIEW (逻辑/漏洞审查)
    ↓ PASS
ARCHITECT_REVIEW (架构合规审查)
    ↓ PASS
RED_TEAM_REVIEW (红队攻击视角)
    ↓ PASS
REQUIREMENT_REVIEW (需求验收)
    ↓ PASS
GIT_COMMITTING
```

任意一层 FAIL → `CORRECTING` → 修复 → 重新编译 → 从失败的那层继续

**各层分工：**

| 层次 | 检查内容 |
|------|---------|
| **Self Review** | 命名规范、明显逻辑漏洞、是否违背 consensus | 输出 `confidence_score`（0-10），低于阈值（默认7）→ CORRECTING |
| **Codex Review** | 逻辑正确性、潜在 NPE、边界条件、API 滥用 |
| **Architect Review** | 分层合规、依赖方向、模式一致性（仅 L2） |
| **Red Team Review** | 攻击者视角：注入、越权、数据泄露（可配置仅 L2） |
| **Requirement Review** | 对照原始需求，逐条验收 |

### D.7 Correcting 阶段 —— 错误修复调度

**文件：** `phases/correcting.py`  
**职责：** 不直接修复代码，而是生成修复计划，然后转回 CODING

```python
def handle(self, task, workspace):
    task.attempt_count += 1

    # 1. 按来源阶段计数（build 失败、codex 失败 分开计数）
    fail_source = task.phase_counters.get("last_fail_stage", "build")
    source_count = task.phase_counters[f"correcting_{fail_source}"] + 1

    # 2. 超过重试次数 → FAILED
    if source_count > per_source_max:
        raise AgentFatalError("retries exceeded")

    # 3. 不可解检测
    if detect_unsolvable(error_history):
        raise AgentFatalError("unsolvable error loop")

    # 4. 加载 FixPlan（结构化修复计划）
    fix_plan = self._load_fix_plan(workspace)

    # 5. 按优先级排序，取最高优先级一批
    #    生成 single_fix_prompt.md
    # 6. 流转回 CODING
    return PhaseResult(State.CODING, "correcting: apply fix")
```

**FixPlan 优先级：**  
`Critical → High → Medium → Low`，每轮只修一批高优先级问题，避免一次给 Claude 太多任务。

### D.8 Git 提交与 PR

**GitCommitting：** `git add -A && git commit -m "..."` 只提交共识文件清单中的文件  
**CreatingPR：** 调用 `gh pr create` 或 GitHub API 创建 PR  
**Notifying：** 发 Telegram 消息，包含 PR 链接

---

## 模块 E：服务层 — `services/`

### E.1 AIClient —— Claude CLI 封装

**文件：** `services/ai_client.py`

**核心设计：不用 Anthropic SDK，用 subprocess 调用命令行**

```python
# 实际执行的命令（编码模式）
claude -p "<prompt>" \
    --output-format json \
    --permission-mode acceptEdits \
    --allowed-tools Read,Edit,Write,Glob,Grep \
    --disallowed-tools Bash \
    --max-turns 15 \
    --no-session-persistence
```

**为什么用命令行而不用 API？**
- Claude Code CLI 有自己的工具调用能力（Read/Edit/Grep），HTTP API 没有
- Claude Code CLI 可以直接操作文件系统，不需要中间解析
- 已有 Claude Code 认证，不需要额外配置 API Key

**重试机制（指数退避）：**

```python
for attempt in range(max_retries + 1):
    try:
        output = self._invoke_claude_cli_subprocess(...)
        return output
    except AgentRateLimitError:
        delay = (2 ** attempt) * 3.0  # 3s, 6s, 12s...
        time.sleep(delay)
    except AgentTimeoutError:
        raise  # 超时不重试（避免等太久）
```

**进度实时显示：**  
当 `progress_workspace` 不为空时，启用进度追踪模式：
- 主线程每 2 秒更新 `cli_progress.json`
- Web UI 通过 SSE 流读取这个文件并显示给用户

### E.2 GitService —— Git 操作封装

**文件：** `services/git_service.py`

**安全黑名单（`BLOCKED_PATHS`）：**

```python
BLOCKED_PATHS = [
    "buildSrc/src/main/kotlin/Configs.kt",  # 站点配置，不许 AI 修改
    "jg_tools/",                             # APK 保护工具
    ".github/workflows/",                    # CI 配置
    "keystore/",                             # 签名密钥
    "SiteThemeRegistryGenerated",            # 自动生成文件
]
```

任何 Claude 输出尝试修改这些路径，都会被静默跳过（不报错，不写入）。

**代码应用（`apply_code_changes()`）：**  
Claude 输出格式约定：

```
=== FILE: app/src/main/java/com/sport/view/VipCard.kt ===
package com.sport.view
// ... 代码内容 ...
=== END FILE ===
```

GitService 解析 `FILE_MARKER` 正则，逐文件写入磁盘。

### E.3 RequestClassifier —— 需求路由分类

**文件：** `services/request_classifier.py`

**三种分类模式（配置项 `routing.classifier`）：**

| 模式 | 逻辑 |
|------|------|
| `rule` | 仅关键词规则，快速但不够智能 |
| `llm` | LLM 主判，失败回退规则 |
| `hybrid`（默认） | 先快速规则，命中直接返回；未命中再调 LLM |

**快速规则优先命中（跳过 LLM，避免 60s+ 等待）：**

```python
# UI/组件 bug 描述 → 直接判定 code，跳过 LLM
CODE_UI_BUG_PATTERNS = [
    r"vippager", r"渐变", r"颜色", r"不正确", r"少了一", ...
]
# 明确动词 → 直接判定 code
CODE_STRONG_PATTERNS = [
    r"实现", r"添加", r"修复", r"fix", ...
]
```

**LLM 分类 Prompt（简化）：**

```
你是路由器，根据需求判断类型：
- explain：问答/介绍/原理，不改代码
- review_only：审查已有代码
- design_only：出方案，不实现
- code：需要动仓库的任务

只输出 JSON：{"request_type":"code","confidence":0.95,"reason":"..."}
```

---

## 模块 F：工具层 — `utils/`

### F.1 三层配置 —— `utils/config_loader.py`

```
优先级（高 → 低）：
  环境变量（如 CLAUDE_MODEL=claude-sonnet-4-6）
      ↓
  config/local.yaml（本地覆盖，不提交 git）
      ↓
  config/default.yaml（默认值，提交 git）
```

**读取方式：**

```python
from utils.config_loader import cfg_str, cfg_int, cfg_bool

model = cfg_str("ai.claude_model", "")          # 点号路径
timeout = cfg_int("timeouts.build", 900)
enabled = cfg_bool("features.self_review_enabled", True)
```

**惰性加载：** 配置在首次调用时才读取 YAML，避免 import 时发生 IO。

### F.2 逃逸检测 —— `utils/escape_detector.py`

防止系统陷入无限修复循环：

```python
def detect_unsolvable(error_history: list[str]) -> tuple[bool, str]:
    """检测连续相同错误指纹（哈希匹配）"""
    if len(error_history) < 2:
        return False, ""
    
    # 如果最近 N 次错误指纹完全相同，认为无法自动修复
    fingerprints = [hash_error(e) for e in error_history[-3:]]
    if all(f == fingerprints[0] for f in fingerprints):
        return True, "same error repeating"
    
    return False, ""
```

触发后：任务流转到 `WAITING_CLARIFICATION`（通知人类介入）或 `FAILED`。

### F.3 LangSmith 追踪 —— `utils/tracing.py`

（见第 8 节详细讲解）

---

## 模块 G：网关层 — `gateway/`

### G.1 Web UI —— `gateway/web_ui.py`

纯 Python `http.server`，无框架，提供以下 API：

| 路由 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 重定向到 `/static/chat.html` |
| `/api/task` | POST | 提交新任务（支持图片 URL） |
| `/api/stream/:id` | GET | **SSE 流**：实时任务状态推送 |
| `/api/reply/:id` | POST | 需求澄清回复 |
| `/api/continue/:id` | POST | L2 人工核准 |
| `/api/cancel/:id` | POST | 取消任务 |
| `/api/upload` | POST | 上传图片（base64） |
| `/task/:id` | GET | 任务详情（JSON） |
| `/task/:id/history` | GET | 状态流转历史 |

**SSE（Server-Sent Events）实现：**

```python
# 服务器端每 2 秒推送一次任务状态
def _handle_sse_stream(self, task_id):
    self.send_response(200)
    self.send_header("Content-Type", "text/event-stream")
    self.end_headers()
    
    deadline = time.time() + 600  # 最多 10 分钟
    while time.time() < deadline:
        task = get_task(task_id)
        status_data = json.dumps({"state": task.current_state, ...})
        self.wfile.write(f"data: {status_data}\n\n".encode())
        self.wfile.flush()
        
        if task.current_state in TERMINAL_STATES:
            break
        time.sleep(2)
```

**SSE vs WebSocket：**  
SSE 是单向的（服务器推给客户端），实现简单，不需要额外库。适合"查看进度"这种只需服务器推送的场景。

### G.2 Telegram Bot —— `gateway/telegram_bot.py`

支持以下命令：
- `/task <需求>` —— 提交任务
- `/reply <task_id> <回复>` —— 需求澄清
- `/continue <task_id>` —— L2 核准
- `/cancel <task_id>` —— 取消

---

## 5. 关键设计模式拆解

### 5.1 策略模式（Strategy Pattern）

`AgentEngine` 的注册表本质上是**策略模式**：

```python
# 注册：绑定状态到策略（处理器）
engine.register(State.CODING, CodingHandler(...))

# 使用：根据状态选择策略
handler = self._handlers[current_state]
result = handler.handle(task, workspace)
```

**好处：** 添加新状态只需新建一个类，不修改引擎代码（开闭原则）。

### 5.2 模板方法模式（Template Method）

引擎控制执行流程，Handler 实现具体逻辑：

```
Engine:  on_enter() → handle() → on_exit()
Handler:               ↑ 子类实现
```

### 5.3 工厂方法（Factory Method）

`build_engine()` 是一个工厂函数，负责创建和组装所有依赖。测试时可以替换为 `build_test_engine()`。

### 5.4 观察者/事件模式（简化版）

LangSmith 追踪是用**上下文管理器**实现的观察者模式：

```python
with trace_task(task_id, ...) as tracer:
    tracer.log_phase_start("coding", inputs={...})
    # ... 执行逻辑 ...
    tracer.log_phase_end("coding", outputs={...})
```

任务生命周期事件自动上报，主流程代码不受影响。

---

## 6. 完整任务流水线演示

### 场景：用户发送"修复 VIP 卡片滑动动画错误"

```
用户 → Telegram Bot → /task 修复 VIP 卡片滑动动画错误
                          ↓
                    TaskService.create_task()
                    写入 SQLite：state=PENDING, level=auto
                          ↓
               Executor 轮询到这条任务（2 秒内）
                          ↓
              ── PENDING → PLANNING ──
              PlanningHandler.handle():
                1. auto_level("VIP 卡片滑动") → "L0"（UI bug）
                2. RequestClassifier → "code"（置信度 0.96，走快速规则）
                3. 生成 requirement.md
                4. 澄清判断：缺目标站点，但 L0 豁免 → 不澄清
                5. L0 快路径 → 生成最小 consensus.md
                return PhaseResult(State.CODING, "L0 fast path")
                          ↓
              ── PLANNING → CODING ──
              CodingHandler.handle():
                1. 创建 git 分支 agent-{task_id}-fix-vip
                2. 构建上下文（consensus.md + 项目规范）
                3. 调用 claude -p --permission-mode acceptEdits
                   Claude 直接编辑 VipCardSection.kt
                4. 代码已写入文件
                return PhaseResult(State.BUILDING, "coding complete")
                          ↓
              ── CODING → BUILDING ──
              BuildingHandler.handle():
                ./gradlew app:assembleDebug（L0 assemble_only）
                构建通过（L0 assemble_only）
                return PhaseResult(State.GIT_COMMITTING, "L0 compile-only passed")
                          ↓
              ── BUILDING → GIT_COMMITTING ──
              GitCommittingHandler.handle():
                git add VipCardSection.kt
                git commit -m "fix: VIP 卡片滑动动画错误 [L0]"
                git push origin agent-xxx
                return PhaseResult(State.CREATING_PR, "committed")
                          ↓
              ── GIT_COMMITTING → CREATING_PR ──
              CreatingPRHandler:
                gh pr create --title "fix: VIP 卡片..."
                return PhaseResult(State.NOTIFYING, "PR created")
                          ↓
              ── CREATING_PR → NOTIFYING ──
              NotifyingHandler:
                Telegram 发送：✅ 任务完成 PR: https://github.com/.../pull/123
                return PhaseResult(State.COMPLETED, "notified")
                          ↓
              ── NOTIFYING → COMPLETED ──
              任务结束，总耗时约 3-5 分钟
```

---

## 7. LangGraph vs 本项目的状态机

很多人听到"状态机"会想到 LangGraph。下面对比说明：

| 维度 | LangGraph | 本项目 |
|------|-----------|--------|
| **持久化** | 内存（默认）/ 可接 Redis | SQLite WAL |
| **状态定义** | TypedDict（字典） | Python dataclass |
| **图定义** | `graph.add_node()` + `graph.add_edge()` | `engine.register()` + `VALID_TRANSITIONS` 字典 |
| **条件路由** | `add_conditional_edges()` | PhaseResult.next_state（Handler 内决定） |
| **并行节点** | 原生支持（Fan-out/Fan-in） | 手动 ThreadPoolExecutor（仅 Debate 阶段） |
| **可视化** | 内置图可视化 | 无（手写 Mermaid 图） |
| **依赖** | `langgraph` 包（较重） | 纯 Python 标准库 + SQLite |
| **断点恢复** | Checkpointer | `resume_from_gate` / `resume_after_clarification` 字段 |
| **学习曲线** | 中等（需理解 LangGraph 概念） | 低（纯 Python，概念直观） |

**本项目为什么不用 LangGraph？**
1. 控制粒度更细：状态合法性校验、原子转换、历史记录都是自定义的
2. 持久化用 SQLite 而非内存，进程重启后任务不丢失
3. 项目启动时 LangGraph 生态还不成熟，自研成本更低

---

## 8. LangSmith 可观测性详解

### 8.1 是什么？

LangSmith 是 LangChain 公司提供的 **AI 应用可观测性平台**。你可以在 https://smith.langchain.com 看到每次 AI 调用的：
- 输入 Prompt
- 输出文本
- 耗时
- 成本
- 错误信息

### 8.2 本项目的追踪层次

```
LangSmith 上看到的树形结构：

task-abc123 [chain]                  ← 整个任务（TaskTracer 的 __enter__/__exit__）
  ├─ planning [chain]                ← 每个阶段（log_phase_start/end）
  │    └─ llm-claude [llm]           ← 每次 LLM 调用（_try_log_llm）
  ├─ coding [chain]
  │    └─ llm-claude [llm]           ← Claude CLI 调用（prompt + output）
  ├─ building [chain]                ← 构建（无 LLM 调用）
  ├─ codex_review [chain]
  │    └─ llm-claude [llm]
  └─ completed [chain]
```

### 8.3 启用方式

```bash
# .env 或 config/local.yaml 中设置
export LANGCHAIN_API_KEY="ls__xxxxx"
export LANGCHAIN_PROJECT="AICodeAgent"
```

配置后系统自动启用，不启用时所有 tracing 调用是空操作（不影响性能）。

### 8.4 代码示例

```python
# engine/core.py 中
with trace_task(task_id=task_id, requirement=...) as tracer:
    # 阶段开始时记录 inputs
    tracer.log_phase_start("coding", inputs={
        "task_id": task_id,
        "fix_prompt_snippet": "...",  # 对调试有用的上下文
    })
    
    # ... 执行阶段逻辑 ...
    
    # 阶段结束时记录 outputs
    tracer.log_phase_end("coding", outputs={
        "next_state": "building",
        "files_applied": 3,
    })
```

### 8.5 每个阶段记录什么

| 阶段 | inputs | outputs |
|------|--------|---------|
| planning | requirement 前 500 字符 | next_state |
| coding | fix_prompt 片段（如有）| files_applied 数量 |
| building | prev_build_error（上次错误）| build_error（本次错误） |
| correcting | fix_items_count, fix_priorities | next_state |
| codex_review | prev_review_snippet | next_state |

---

## 9. 优点与缺点

### 9.1 优点

| 优点 | 说明 |
|------|------|
| **全自动端到端** | 从需求到 PR 完全无人工介入（L0/L1 路径） |
| **状态持久化** | SQLite 保证进程崩溃后任务不丢失 |
| **原子状态转换** | 不存在"半转换"状态，数据一致性强 |
| **可扩展性强** | 新增状态只需新建 Handler 类 + 注册，主流程不变 |
| **异常分层处理** | 可恢复 vs 致命错误，自动决定重试还是失败 |
| **多层审查** | 自查 + 逻辑审查 + 架构审查 + 红队 + 需求验收，质量有保证 |
| **安全黑名单** | 关键文件（密钥、CI、签名）不允许 AI 修改 |
| **可观测性** | LangSmith 可追踪每次 LLM 调用，方便调试 |
| **断点恢复** | 某个审查层失败后，修复后从该层继续，不从头跑 |
| **人工闸门** | L2 任务可配置要求人工审核后再编码 |

### 9.2 缺点

| 缺点 | 说明 |
|------|------|
| **串行执行** | 一次只处理一个任务，队列积压时等待时间长 |
| **强依赖 Claude CLI** | 必须本机安装 `claude` 命令行，版本敏感 |
| **超时风险高** | Claude 调用可能 30~20 分钟，整体任务容易超时 |
| **调试困难** | AI 输出不确定性大，同一需求两次可能走不同路径 |
| **状态转换表维护成本** | 添加状态时需同时修改 `VALID_TRANSITIONS` 和 Handler，容易遗漏 |
| **无并发任务支持** | 不支持同时处理多个任务（文件锁保证单进程） |
| **Context 长度限制** | 复杂任务的上下文可能超出 Claude 的 Context Window |
| **L0 确定性不足** | L0 快路径虽然跳过了审查，但 AI 生成代码仍有不确定性 |
| **Debate 角色单一** | 三方辩论实际都是同一个模型，只是 prompt 不同，多样性有限 |

---

## 10. 如何上手修改

### 10.1 添加一个新的审查阶段

假设要添加 "性能审查（Performance Review）" 阶段：

**第一步：** 在 `engine/state_machine.py` 添加状态

```python
class State(Enum):
    # ... 已有状态 ...
    PERFORMANCE_REVIEW = "performance_review"  # 新增
```

**第二步：** 在 `VALID_TRANSITIONS` 中添加转换规则

```python
VALID_TRANSITIONS = {
    # ... 已有规则 ...
    State.RED_TEAM_REVIEW: [
        State.PERFORMANCE_REVIEW,  # 红队通过后进性能审查
        State.CORRECTING,
        State.FAILED,
        State.CANCELLED,
    ],
    State.PERFORMANCE_REVIEW: [   # 新增
        State.REQUIREMENT_REVIEW,
        State.CORRECTING,
        State.FAILED,
        State.CANCELLED,
    ],
}
```

**第三步：** 创建 `phases/performance_review.py`

```python
from phases.base import PhaseHandler, PhaseResult
from engine.state_machine import State, Task

class PerformanceReviewHandler(PhaseHandler):
    def __init__(self, ai_client=None):
        self._ai = ai_client

    def handle(self, task: Task, workspace: Path) -> PhaseResult:
        # 调用 AI 审查性能
        prompt = "审查以下代码的性能问题..."
        output = self._ai.call(prompt, headless=True)
        
        if "FAIL" in output:
            task.phase_counters["last_fail_stage"] = "performance_review"
            return PhaseResult(State.CORRECTING, "performance issues found")
        
        return PhaseResult(State.REQUIREMENT_REVIEW, "performance review passed")
```

**第四步：** 在 `phases/__init__.py` 导出

```python
from phases.performance_review import PerformanceReviewHandler
```

**第五步：** 在 `engine/runner.py` 注册

```python
engine.register(State.PERFORMANCE_REVIEW, PerformanceReviewHandler(
    ai_client=ai_client,
))
```

完成！无需修改引擎核心代码。

### 10.2 修改 L0 任务的超时

编辑 `config/local.yaml`（没有就创建）：

```yaml
timeouts:
  claude_code_l0: 600   # 默认 900 秒，改为 600 秒

retries:
  claude_code_l0: 1     # 默认 0（不重试），改为 1
```

### 10.3 关闭某个审查层

```yaml
# config/local.yaml
features:
  self_review_enabled: false        # 关闭自审查
  architect_review_enabled: false   # 关闭架构审查
```

### 10.4 查看任务状态

```bash
# 查看数据库中的任务
sqlite3 AICodeAgent/data/agent.db "SELECT task_id, current_state, raw_requirement FROM task_queue ORDER BY created_at DESC LIMIT 10;"

# 查看状态流转历史
sqlite3 AICodeAgent/data/agent.db "SELECT * FROM state_history WHERE task_id='xxx' ORDER BY timestamp;"
```

### 10.5 手动触发任务（curl）

```bash
curl -X POST http://localhost:6789/api/task \
  -H "Content-Type: application/json" \
  -d '{"requirement": "修复 VIP 卡片滑动 bug", "level": "L0", "site_hint": "haobo"}'
```

---

## 附录：常见问题

**Q: 任务卡在某个状态不动了怎么办？**  
A: 查看 `AICodeAgent/logs/` 中的日志。也可以通过 Web UI 取消任务，然后重新提交。

**Q: Claude CLI 调用超时怎么办？**  
A: 调高 `timeouts.claude_code`（默认 1200 秒）。或者把任务拆小（L2 → L1 + L1）。

**Q: 如何知道 LangSmith 是否生效？**  
A: 启动日志中会出现 `LangSmith tracing enabled project=AICodeAgent`。或者看 `is_enabled()` 返回值。

**Q: 为什么 Debate 阶段三个 Agent 用同一个模型？**  
A: 纯粹是为了简化。三个 prompt 构造了不同的"角色"，实际上多样性来自 prompt 差异，不是模型差异。

**Q: `phase_counters` 和 `attempt_count` 有什么区别？**  
A: `attempt_count` 是全局总重试次数（安全网）。`phase_counters` 按来源阶段分别计数，例如 build 失败允许重试 3 次，codex 失败允许重试 2 次，互不影响。

---

*文档由 Claude Code 生成 | AICodeAgent V4 | 2026-05-24*
