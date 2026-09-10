# 架构说明

3MF Manager 采用**前后端一体、零第三方运行时依赖**的轻量架构。

## 总览

```
┌─────────────────────────────────────────────────────┐
│                  浏览器（前端 SPA）                    │
│   仪表盘 · 待整理 · 模型库 · 智能对话 · 设置           │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP (fetch)
┌──────────────────────▼──────────────────────────────┐
│            server.py（组合根 + HTTP 路由）             │
│  ┌──────────┬──────────┬──────────┬───────────────┐  │
│  │ 上传/解析 │ 归档执行  │ 查询/对话 │ 路由表分发      │  │
│  └──────────┴──────────┴──────────┴───────────────┘  │
│   parse_3mf   classify    db        llm_client       │
└────┬──────────────┬──────────────┬──────────────┬────┘
     │              │              │              │
   SQLite        rules.json      文件系统       LLM (可选)
 library.db    分类规则(可编辑)  ~/3D模型库/   OpenAI 兼容接口
```

## 模块分层

| 模块 | 职责 |
|---|---|
| `server.py` | 组合根：持有运行时配置（库根/各产物目录/DB 路径）、HTTP Handler 与路由表（`GET_ROUTES`/`POST_ROUTES`）、LLM 编排 |
| `classify.py` | 纯逻辑：规则分类 `categorize`、目标路径 `target_of`、别名 `make_alias`/`sanitize_alias`；规则从 `rules.json` 加载 |
| `rules.json` | 分类规则数据（IP 关键词、目录映射、噪音词），可直接编辑扩充，改完重启生效 |
| `db.py` | 存储层：schema 与 `PRAGMA user_version` 版本化迁移、哈希/回收站/相对路径工具；不持有全局状态，全部显式传参 |
| `parse_3mf.py` | 3MF 解析（ZIP/XML，字节级几何计数） |
| `subcat.py` / `mc_subcat.py` | 子分类语义（Dummy13/Minecraft/功能父类子类），供 classify 引用 |
| `llm_client.py` | OpenAI 兼容 LLM 客户端 + 会话管理 |

运行时全局（`LIBRARY_ROOT`、`DB_PATH` 等）只存在于 server.py，db/classify 保持
纯函数便于测试隔离；日志写入 `logs/mfmanager.log`（轮转）。

## 分层职责

### 1. 解析层 `parse_3mf.py`
3MF 是 ZIP 容器，内含多个 `3D/*.model`（XML）。Bambu Studio 会把真实网格拆到
`3D/Objects/object_*.model` 等部件，主 `3dmodel.model` 只做装配引用。

- **几何**：跨所有 `.model` 部件做字节级子串计数（`<vertex` / `<triangle`），远快于逐节点 XML 解析。
- **元数据**：正则提取 Title/Designer/DesignModelId/CreationDate 等。
- 输出：顶点数、三角面数、对象数、切片配置、设计ID、几何指纹 `顶点|三角面`。

### 2. 分类引擎 `rules.json` + `classify.py`
分多级优先级：
1. **自定义分类优先**：用户确认过的自定义分类按最具体末段匹配（用户教过的优先）。
2. **IP 优先**：文件名/标题显式出现的 IP（Dummy13、Minecraft、高达…）优先于文件夹。
3. **文件夹 IP 映射**：位于某 IP 文件夹则归该类。
4. **关键词 IP**：`ip_kw` 列表。
5. **标题补全 IP**：`extra_ip`（鸟山明、吉卜力等）。
6. **功能规则**：`content_rules` + `rules`（武器/载具/收纳/手办等），按语义排序避免宽泛词误吞。
7. **兜底**：`UNCAT_RULES` + 文件名片段硬编码。

**LLM 增强**：规则分类后，可选调用 `llm_client` 让模型判断是否合理；不合理时给出**新分类建议**，用户确认后经 `confirm-new-category` 落地并应用到文件。

### 3. 归档执行
`/api/apply` 根据 `target_dir`（由 `target_of` 计算出的 `01_IP授权/…` 等路径）创建目录并 `shutil.move`，可选按别名重命名，随后更新 SQLite 索引状态 `pending → applied`。

### 4. 存储
- **SQLite** `library.db`：`files` 表（相对路径 rel_path/路径/分类/别名/hash/设计ID/几何/tag/缩略图/状态）+ `attachments` 表 + `settings` 表；schema 通过 `PRAGMA user_version` 做版本化迁移。切换库根时按 rel_path 重定位 abs_path。
- **文件系统**：`library_root` 下 `00_待整理` 存放未归档，分类子目录存放归档后文件。
- **SHA-256**：每个文件算内容哈希，用于精确去重提示。

### 5. LLM 客户端 `llm_client.py`
- 仅用标准库 `urllib`，兼容 OpenAI `/chat/completions`。
- 会话用进程内字典存储（可扩展持久化）。

## 关键技术决策

| 决策 | 理由 |
|---|---|
| 零第三方依赖 | 部署简单、无供应链风险、单文件可运行 |
| 字节级几何计数 | 全量 835 文件解析 <10s（对比逐节点 XML 慢几个数量级） |
| SQLite 而非 JSON | 支持并发写入、索引、SQL 查询 |
| 规则 + LLM 双分类 | 规则可离线兜底，LLM 提供语义理解与新增分类能力 |
| 软链 → 自包含文件 | 开源项目不依赖外部目录，随仓库分发 |

## 数据流（一次典型使用）

```
上传 3MF → 存 00_待整理 → parse_3mf 提取元数据
       → categorize 规则分类 → 生成别名 + SHA-256 + 目标目录
       → (可选) llm-classify 复核/新分类建议 → confirm-new-category
       → /api/apply 移动+改名 → 索引 status=applied
       → 仪表盘/查询/对话 检索展示
```
