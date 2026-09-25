# Docker 部署指南

3MF Manager 支持 Docker 部署，**所有用户数据（数据库、缩略图、附件、回收站、日志、模型文件、配置）都外挂到宿主机数据盘**，容器本身无状态，升级镜像不丢数据。

## 快速开始

```bash
docker compose up -d --build
```

打开 `http://<主机IP>:8000`，首次进入后在「设置」页填写 LLM 地址 / Key / 模型名即可。

> 镜像运行时只依赖 Python 标准库，无需联网拉取任何第三方包。

## 数据盘布局

默认数据盘挂载在 `./data`（可用任意宿主机目录替换 `./data:/data`）：

```
data/
├── config.json      # LLM 配置 + library_root（首次启动自动生成，绝不覆盖）
├── library.db       # SQLite 索引库
├── library/         # 模型库根目录（3MF 文件、00_待整理、exports）
├── thumbs/          # 缩略图
├── attachments/     # 附件
├── .trash/          # 回收站
└── logs/            # 运行日志
```

备份 = 备份这个目录（建议先 `docker compose stop` 再拷贝，保证 SQLite 一致性）。

## 常用操作

```bash
docker compose up -d --build   # 启动 / 升级（git pull 后重建即可，数据保留在 ./data）
docker compose logs -f         # 看日志
docker compose restart         # 重启
docker compose down            # 停止（不删数据）
```

改端口：修改 `docker-compose.yml` 中 `ports` 左侧宿主机端口。

## 使用宿主机已有模型目录

如果模型文件已在宿主机某目录（如群晖 `/volume1/3mf_models`），不想挪进 `./data/library`：

1. `docker-compose.yml` 追加一行挂载：`- /volume1/3mf_models:/models`
2. 编辑 `./data/config.json`，把 `"library_root"` 改为 `"/models"`
3. `docker compose restart`

路径是容器内视角，必须指向容器内存在的挂载点。

## 从本机版迁移数据到 Docker

1. **先停掉本机服务**（`./service.sh stop`），避免 SQLite 拷贝不一致；
2. 把以下内容拷入数据盘目录（如 `./data/`）：
   - `library.db`、`thumbs/`、`attachments/`、`.trash/`
   - 原模型根目录（`config.json` 中 `paths.library_root` 指向的整个目录）→ 放到 `./data/library/`
   - `config.json` → 放到 `./data/config.json`，并把其中 `paths.library_root` 改为 `/data/library`（或按上节挂载宿主机原目录）
3. `docker compose up -d`，打开页面验证数量与缩略图正常。

## 权限说明

容器内以 UID 1000 运行。若宿主机数据目录属主不是 1000 导致读写失败，在 `docker-compose.yml` 中加：

```yaml
services:
  mfmanager:
    user: "1000:1000"    # 或改成数据目录实际属主的 UID:GID
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8000` | 容器内监听端口 |
| `MFMANAGER_HOST` | `0.0.0.0` | 容器内必须为 `0.0.0.0`（镜像已设好）；本机直接跑 `python server.py` 不受影响，仍只监听 `127.0.0.1` |
| `MFMANAGER_DATA_DIR` | `/data` | 数据盘根目录（数据库/缩略图/附件/回收站/日志/配置所在） |
| `MFMANAGER_CONFIG` | `/data/config.json` | 配置文件路径 |

本机开发模式（不用 Docker）行为完全不变：所有数据仍落在代码同目录，`config.json` 仍在项目根目录。
