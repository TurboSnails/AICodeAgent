# Headless Agent V3 — 环境搭建与部署指南

> 版本: 4.0.0 | 对应代码版本见 README.md / ARCHITECTURE_v4.md

---

## 1. 依赖安装

```bash
# Python 3.9+
pip3 install -r AICodeAgent/requirements.txt

# 或创建虚拟环境（推荐，避免污染系统 Python）
cd AICodeAgent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# GitHub CLI (用于自动创建 PR)
brew install gh  # macOS
gh auth login

# Claude Code CLI (非交互模式，编码主引擎)
npm install -g @anthropic-ai/claude-code

# 可选：Node.js
brew install node

# 验证安装
claude --version
gh --version
python3 -m pytest --version
```

---

## 2. 环境变量配置

添加到 `~/.zshrc` 或 `~/.bash_profile`，**重启终端或 `source ~/.zshrc` 生效**。

```bash
# ===== Headless Agent V3 核心开关 =====
export CLAUDE_CODE_AUTO_ALLOW_BASH=true

# ===== AICodeAgent：claude --print 端点（自行 export，与 IDE/cc-use 无关）=====
# export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
# export ANTHROPIC_API_KEY=sk-...
# export CLAUDE_MODEL=claude-sonnet-4-6
# 见 AICodeAgent/docs/CLAUDE_GATEWAY.md

# ===== Android 工具链 =====
export ANDROID_HOME=$HOME/Library/Android/sdk
export PATH=$PATH:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools
export JAVA_HOME=/Applications/Android\ Studio.app/Contents/jbr/Contents/Home
export PATH=$JAVA_HOME/bin:$PATH

# ===== Gradle 性能优化 =====
export GRADLE_OPTS="-Dorg.gradle.daemon=true -Dorg.gradle.parallel=true -Dorg.gradle.jvmargs=-Xmx8g"

# ===== GitHub CLI =====
export GH_TOKEN=ghp_xxxxxxxx  # 或 gh auth login 后无需此变量

# ===== Web API 认证（建议生产设置）=====
export AGENT_API_KEY=your-secret

# ===== Telegram Bot (通知用) =====
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id

# ===== Figma（UI 类任务必填 TOKEN；FILE_KEY 可不设，由站点从 platform-figma-list 解析）=====
export FIGMA_TOKEN=figma_personal_access_token
# export FIGMA_FILE_KEY=...   # 仅无 site_hint 时的兜底

# ===== 编排超时 / 重试（V4 新增）=====
export AGENT_DEBATE_TIMEOUT=600          # 辩论总超时（秒）
export AGENT_CONSENSUS_MAX_RETRY=2       # 共识生成最大重试
export CODEX_REVIEW_MAX_RETRY=2          # Codex 逻辑审查最大重试
export ACCEPTANCE_REVIEW_MAX_RETRY=2     # 需求验收审查最大重试
export AGENT_CLARIFICATION_TIMEOUT_HOURS=48  # 需求澄清超时
export AGENT_TASK_TOTAL_TIMEOUT=7200     # 任务总超时（秒）

# ===== Codex 审查（可选；未设置则回退 claude --print 审查员）=====
export CODEX_CMD=""                      # 例: codex exec -a never --
export CODEX_REVIEW_TIMEOUT=900

# ===== 跳过需求反问（调试用）=====
# export AGENT_SKIP_CLARIFICATION=1

# ===== Code Review Graph（可选，增强 Architect 影响面检索）=====
export CRG_AUTO_START=1
export CRG_HTTP_URL=http://127.0.0.1:5555
export CRG_HTTP_PORT=5555
```

---

## 3. 文件权限

```bash
chmod +x AICodeAgent/scripts/*.sh
chmod +x AICodeAgent/*.sh
```

---

## 4. 启动服务

### 推荐方式：一键启停

```bash
# 在 Android 工程根目录（AICodeAgent 的父目录）
./AICodeAgent/start.sh   # 启动 Web UI + Telegram Bot + Executor
./AICodeAgent/status.sh  # 查看运行状态
./AICodeAgent/stop.sh    # 停止所有服务
```

### 手动方式（调试用）

```bash
# 终端 1: 启动串行执行器（核心引擎，常驻后台）
source AICodeAgent/.venv/bin/activate
python3 AICodeAgent/engine/runner.py

# 终端 2: 启动 Web UI
source AICodeAgent/.venv/bin/activate
python3 AICodeAgent/gateway/web_ui.py
# 打开浏览器 http://localhost:6789

# 终端 3: 启动 Telegram Bot
source AICodeAgent/.venv/bin/activate
python3 AICodeAgent/gateway/telegram_bot.py
```

---

## 5. 验证安装

### 5.1 运行单元测试

```bash
cd AICodeAgent
source .venv/bin/activate
python3 -m pytest tests/unit/ -v
```

### 5.2 一键 Smoke 测试

```bash
chmod +x AICodeAgent/scripts/smoke_l0.sh
AICodeAgent/scripts/smoke_l0.sh
```

### 5.3 手动 L0 任务

```bash
# 检查健康
 curl http://localhost:6789/health

# 提交 L0 任务
curl -X POST http://localhost:6789/api/trigger \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AGENT_API_KEY" \
  -d '{"raw_requirement":"在 app/src/main/res/values/strings.xml 里加一个名为 agent_smoke_test 的字符串，值为 Agent Smoke Test","level":"L0"}'

# 查看任务状态
curl http://localhost:6789/api/tasks
```

### 5.4 L1 常规任务测试

```bash
curl -X POST http://localhost:6789/api/trigger \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AGENT_API_KEY" \
  -d '{
    "raw_requirement": "帮我在 SettingsScreen 里加一个清除缓存的 M3 Confirm Dialog，点击后调用 CacheManager.clear()",
    "level": "L1",
    "site_hint": "haobo"
  }'
```

---

## 6. 目录结构确认

```
AICodeAgent/
├── data/
│   ├── agent.db              # SQLite 数据库 (自动创建)
│   ├── executor.lock         # 文件锁 (自动创建)
│   ├── pids/                 # 进程 PID 文件
│   └── logs/                 # 服务日志
├── workspace/                # 任务工作区 (运行时自动创建)
│   └── {task_id}/
│       ├── clarification_questions.md
│       ├── user_clarification.md
│       ├── consensus.md
│       ├── codex_review.md
│       ├── requirement_review.md
│       ├── asset_map.json
│       └── ...
├── tests/
│   ├── conftest.py
│   └── unit/
│       ├── test_state_machine.py
│       ├── test_asset_manager.py
│       ├── test_codex_review.py
│       └── test_orchestrator_core.py
└── [各模块文件]
```

---

## 7. 故障排查

| 问题 | 排查 |
|------|------|
| Executor 启动失败 | 检查 `pip3 install -r AICodeAgent/requirements.txt`，确认 `AICodeAgent/data/` 可写 |
| Web UI 无法访问 | 确认端口 6789 未被占用，检查防火墙 |
| Claude --print 无输出 | 确认 `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY`；见 `docs/CLAUDE_GATEWAY.md` |
| start.sh 端点 WARN | 本机 LiteLLM 是否监听；`curl $ANTHROPIC_BASE_URL/v1/models` |
| Gradle 构建超时 | 检查 `ANDROID_HOME` 和 `JAVA_HOME` 配置 |
| PR 创建失败 | 运行 `gh auth status` 确认 GitHub CLI 已登录 |
| Telegram 无通知 | 检查 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` |
| L2 任务核准后未执行 | 确认 Executor 正在运行（读取 SQLite waiting_gate 状态） |
| 单元测试失败 | 确认在 `.venv` 中运行，且 `pytest` 已安装 |
| 日志文件过大 | 检查 `AICodeAgent/data/logs/` 下的轮转配置 |

---

## 8. 停止服务

```bash
# 推荐
./AICodeAgent/stop.sh

# 手动查找并 kill
ps aux | grep "runner.py\|web_ui.py\|telegram_bot.py"
kill <pid>
```

---

## 9. 升级说明 (V3 → V4)

| 变更项 | V3 | V4 |
|--------|----|----|
| 决策机制 | Orchestrator 单点 | Multi-Agent Debate + Consensus |
| 需求 intake | 直接进入辩论 | 不明确时 `waiting_clarification` |
| 审查 | 构建全绿即过 | Codex 逻辑审查 + 需求验收审查 |
| 闸门 | L1/L2 自动判定 | L2 共识后 `waiting_gate` 人工核准 |
| 资产 | 手动放置 | Visual Asset Manager 自动嗅探入库 |
| 上下文 | 静态 `ARCHITECTURE_v4.md` | RAG 动态索引 + Code Review Graph |
| 测试 | 无 | pytest 单元测试基线 |
| 日志 | `print` | `logging` 结构化日志 + 轮转 |
