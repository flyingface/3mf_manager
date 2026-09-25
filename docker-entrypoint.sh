#!/bin/sh
# 3MF Manager 容器入口：准备数据盘目录 + 首启种子配置，再启动服务。
# 数据盘根目录由 MFMANAGER_DATA_DIR 指定（镜像内默认 /data），全部用户数据都在其中。
set -e

DATA_DIR="${MFMANAGER_DATA_DIR:-/data}"

mkdir -p "$DATA_DIR/library" "$DATA_DIR/thumbs" "$DATA_DIR/attachments" \
         "$DATA_DIR/.trash" "$DATA_DIR/logs"

# 首次启动：生成默认配置（LLM 留空，稍后在设置页填写）；已存在则绝不覆盖、绝不改写
if [ ! -f "$DATA_DIR/config.json" ]; then
  cat > "$DATA_DIR/config.json" <<EOF
{
  "llm": {
    "base_url": "",
    "api_key": "",
    "model": ""
  },
  "paths": {
    "library_root": "$DATA_DIR/library"
  }
}
EOF
  echo "[entrypoint] 已生成默认配置 $DATA_DIR/config.json"
fi

# 提示（不修改）：config.json 里的 library_root 若指向容器内不存在的路径，多半是迁移时没改
if [ -f "$DATA_DIR/config.json" ] && ! grep -q '"library_root"' "$DATA_DIR/config.json"; then
  echo "[entrypoint] 警告: $DATA_DIR/config.json 缺少 paths.library_root，将回退默认值" >&2
fi

exec python server.py "${PORT:-8000}"
