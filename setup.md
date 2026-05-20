# Headless Agent V2 — 环境搭建指南

## 1. 依赖安装

```bash
# Python 3.9+
pip3 install requests

# GitHub CLI (用于自动创建 PR)
brew install gh  # macOS
gh auth login

# Claude Code CLI (非交互模式)
npm install -g @anthropic-ai/claude-code

# 验证安装
claude --version
gh --version
```

## 2. 环境变量配置

添加到 `~/.zshrc` 或 `~/.bash_profile`：

```bash
# ===== Headless Agent 核心开关 =====
export CLAUDE_CODE_AUTO_ALLOW_BASH=true

# ===== Android 工具链 =====
export ANDROID_HOME=$HOME/Library/Android/sdk
export PATH=$PATH:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools
export JAVA_HOME=/Applications/Android\ Studio.app/Contents/jbr/Contents/Home
export PATH=$JAVA_HOME/bin:$PATH

# ===== Gradle 性能优化 =====
export GRADLE_OPTS="-Dorg.gradle.daemon=true -Dorg.gradle.parallel=true -Dorg.gradle.jvmargs=-Xmx8g"

# ===== GitHub CLI =====
export GH_TOKEN=ghp_xxxxxxxx  # 或 gh auth login 后无需此变量

# ===== Telegram Bot (通知用) =====
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id

# ===== Figma（必填 TOKEN；FILE_KEY 可不设，由站点从 platform-figma-list 解析）=====
export FIGMA_TOKEN=figma_personal_access_token
# export FIGMA_FILE_KEY=...   # 仅无 site_hint 时的兜底

# ===== Code Review Graph HTTP（可选，增强 Architect 影响面检索）=====
export CRG_AUTO_START=1
export CRG_HTTP_URL=http://127.0.0.1:5555
export CRG_HTTP_PORT=5555
```

## 3. 文件权限

```bash
chmod +x AICodeAgent/scripts/*.sh
```

## 4. 启动服务

### 方式一：Web UI + Executor

```bash
# 终端 1: 启动串行执行器（核心引擎，常驻后台）
python3 AICodeAgent/orchestrator/executor.py

# 终端 2: 启动 Web UI
python3 AICodeAgent/gateway/web_ui_v2.py
# 打开浏览器 http://localhost:6789
```

### 方式二：Telegram Bot + Executor

```bash
# 终端 1: 启动串行执行器
python3 AICodeAgent/orchestrator/executor.py

# 终端 2: 启动 Telegram Bot
python3 AICodeAgent/gateway/telegram_bot_v2.py
```

### 方式三：同时启动（推荐）

```bash
# 使用 screen/tmux 或 systemd
# 或简单的后台运行
nohup python3 AICodeAgent/orchestrator/executor.py > /tmp/agent_executor.log 2>&1 &
nohup python3 AICodeAgent/gateway/web_ui_v2.py > /tmp/agent_web.log 2>&1 &
nohup python3 AICodeAgent/gateway/telegram_bot_v2.py > /tmp/agent_bot.log 2>&1 &
```

## 5. 首次测试

```bash
# 一键 smoke（需先 ./AICodeAgent/start.sh）
chmod +x AICodeAgent/scripts/smoke_l0.sh
AICodeAgent/scripts/smoke_l0.sh

# 或手动 L0
curl -X POST http://localhost:6789/api/trigger \
  -H "Content-Type: application/json" \
  -d '{
    "raw_requirement": "帮我在 app/src/main/res/values/strings.xml 里加一个名为 clear_cache 的字符串，值为 清除缓存",
    "level": "L0"
  }'

# 查看任务状态
curl http://localhost:6789/api/tasks

# L1 常规任务测试
curl -X POST http://localhost:6789/api/trigger \
  -H "Content-Type: application/json" \
  -d '{
    "raw_requirement": "帮我在 SettingsScreen 里加一个清除缓存的 M3 Confirm Dialog，点击后调用 CacheManager.clear()",
    "level": "L1",
    "site_hint": "haobo"
  }'
```

## 6. 目录结构确认

```
AICodeAgent/
├── data/
│   ├── agent.db              # SQLite 数据库 (自动创建)
│   └── executor.lock         # 文件锁 (自动创建)
├── workspace/                # 任务工作区 (运行时自动创建)
│   └── {task_id}/
└── [各模块文件]
```

## 7. 故障排查

| 问题 | 排查 |
|------|------|
| Executor 启动失败 | 检查 `pip3 install -r AICodeAgent/requirements.txt`，确认 `AICodeAgent/data/` 可写 |
| Web UI 无法访问 | 确认端口 6789 未被占用，检查防火墙 |
| Claude --print 无输出 | 运行 `claude --print "hello"` 验证 CLI 安装 |
| Gradle 构建超时 | 检查 `ANDROID_HOME` 和 `JAVA_HOME` 配置 |
| PR 创建失败 | 运行 `gh auth status` 确认 GitHub CLI 已登录 |
| Telegram 无通知 | 检查 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` |
| L2 任务核准后未执行 | 确认 Executor 正在运行（读取 SQLite waiting_gate 状态） |

## 8. 停止服务

```bash
# 查找并 kill 进程
ps aux | grep "executor.py\|web_ui_v2.py\|telegram_bot_v2.py"
kill <pid>
```
