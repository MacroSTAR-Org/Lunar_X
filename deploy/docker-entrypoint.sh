#!/bin/sh
# 容器入口：把可变状态（配置/数据/日志/插件）统一收进挂载卷，
# 镜像本身保持无状态，重建容器不丢配置与已装插件。
set -eu

STATE_DIR="${LUNARX_STATE_DIR:-/app/state}"
mkdir -p "$STATE_DIR"

# 配置文件通常由 WebUI 首次启动时生成；先建好符号链接，写入即落进卷。
for name in config.json admin114.json appsettings.json webui.json webui.initial_password; do
    if [ ! -e "/app/$name" ] && [ ! -L "/app/$name" ]; then
        ln -s "$STATE_DIR/$name" "/app/$name"
    fi
done

for dir in data logs uploads plugins; do
    target="$STATE_DIR/$dir"
    mkdir -p "$target"
    if [ -d "/app/$dir" ] && [ ! -L "/app/$dir" ]; then
        # 首次启动：把镜像内置的插件种子复制进卷，之后以卷为准。
        cp -an "/app/$dir/." "$target/" 2>/dev/null || true
        rm -rf "/app/$dir"
    fi
    if [ ! -e "/app/$dir" ] && [ ! -L "/app/$dir" ]; then
        ln -s "$target" "/app/$dir"
    fi
done

exec "$@"
