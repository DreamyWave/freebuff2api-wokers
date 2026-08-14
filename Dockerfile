FROM node:20-alpine

WORKDIR /app

# 运行时需要的工具：wget 用于启动时拉取最新代码
RUN apk add --no-cache wget

# 预置当前版本作为本地兜底（启动时若拉取失败仍可运行）
COPY package.json server.js worker.js ./

# 创建引导器：启动时优先从 GitHub 拉取最新 worker.js/server.js/package.json
# （fscarmen/Argo-Nezha-Service-Container 模式：容器只做引导，逻辑在远程仓库）
# 拉取失败时回退本地预置副本，保证容器始终可启动
RUN printf '%s\n' \
    '#!/usr/bin/env sh' \
    '' \
    'set -e' \
    'BASE="https://raw.githubusercontent.com/pingmike2/freebuff2api-wokers/main"' \
    'TMP="/tmp/update"' \
    'mkdir -p "$TMP"' \
    '' \
    'echo "[entrypoint] fetching latest code from GitHub..."' \
    'for f in package.json server.js worker.js; do' \
    '  if wget -q --timeout=15 -O "$TMP/$f" "$BASE/$f"; then' \
    '    cp "$TMP/$f" "/app/$f" && echo "[entrypoint] updated $f"' \
    '  else' \
    '    echo "[entrypoint] fetch failed for $f, keeping local copy"' \
    '  fi' \
    'done' \
    '' \
    'exec node /app/server.js' \
    > /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Create credentials dir (mounted at runtime)
RUN mkdir -p /app/credentials && chown -R node:node /app

USER node
EXPOSE 8787

ENTRYPOINT ["/app/entrypoint.sh"]
