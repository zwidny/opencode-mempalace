#!/bin/sh
# 容器入口：
# 1. 建软链对齐宿主路径（~/.mempalace/config.json 内是宿主绝对路径如
#    $HOST_HOME/.mempalace/palace，容器内映射为 /root/.mempalace——软链
#    让两条路径指向同一挂载点，palace 解析与锁文件 key 与宿主完全一致）
# 2. 从挂载的 0600 token 文件注入 env（不进 argv，防 ps 泄漏），exec serve
# HOST_HOME 由 compose 传入（${HOME}），兼容任意用户名的宿主机
set -e
HOST_HOME="${HOST_HOME:-/home/zhao}"
HOST_HOME_DIR="$(dirname "$HOST_HOME")"
mkdir -p "$HOST_HOME_DIR"
if [ ! -e "$HOST_HOME" ]; then
    ln -s /root "$HOST_HOME"
fi
TOKEN_FILE="/root/.mempalace/server/bearer-token"
if [ -s "$TOKEN_FILE" ]; then
    MEMPALACE_MCP_HTTP_TOKEN="$(cat "$TOKEN_FILE")"
    export MEMPALACE_MCP_HTTP_TOKEN
else
    echo "warning: $TOKEN_FILE missing or empty — serving without token" >&2
fi
exec mempalace serve --host 0.0.0.0 --port 8765
