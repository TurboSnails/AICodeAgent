# AICodeAgent 与 LLM 端点

本项目 **headless** 通过 `claude --print` 调模型，只依赖 shell 环境变量，**不包含** `cc-use`、不安装个人 Claude Code 切换工具。

## 必需环境变量

在跑 `./start.sh` / `engine/runner.py` 的终端里自行配置，例如本地 LiteLLM：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export ANTHROPIC_API_KEY=sk-your-proxy-master-key
export CLAUDE_MODEL=claude-sonnet-4-6
```

未设 `ANTHROPIC_BASE_URL` 时走 Anthropic 官方（需有效 `ANTHROPIC_API_KEY`）。

## 启动前自检

```bash
curl -s -m 5 "$ANTHROPIC_BASE_URL/v1/models" -H "Authorization: Bearer $ANTHROPIC_API_KEY" | head -c 200
claude --print --model "$CLAUDE_MODEL" "ping"
```

## 日志

`engine/runner.py` 会打 `ANTHROPIC_BASE_URL` 与 `CLAUDE_MODEL`，便于对照 `data/logs/runner.log`。

---

**个人 IDE 里 Claude Code 切 Kimi/Gemini**：与本仓库无关，在你本机 `~/.claude-gateway` 自行维护（若有 `cc-use`，也不在 git 里）。
