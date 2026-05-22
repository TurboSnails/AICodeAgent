FROM python:3.11-slim

WORKDIR /workspace/AICodeAgent

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    bash \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 注意：claude CLI 和 gh CLI 需要在宿主机或额外层安装
# 本 Dockerfile 假设开发者在宿主机已安装 claude / gh，通过 volume 挂载使用
# 如需在容器内安装，可取消下方注释：
# RUN npm install -g @anthropic-ai/claude-code && \
#     (curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg) && \
#     echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
#     apt-get update && apt-get install -y gh

ENV PYTHONUNBUFFERED=1
