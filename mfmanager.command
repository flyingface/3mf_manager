#!/bin/bash
# 双击即启动 3MF Manager（已在运行则直接打开页面）
cd "$(dirname "$0")" || exit 1
./service.sh start
