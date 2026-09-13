# macOS 使用指南

3MF Manager 以 macOS 为主要平台开发和打磨（pyproject `Operating System :: MacOS`）。本文汇总 Mac 上的安装方式、后台常驻、开机自启，以及所有与 macOS 深度集成的功能细节和已知注意事项。

**能力速览**

| 场景 | Mac 上的体验 |
|---|---|
| 启动 | `service.sh` 一键后台守护；launchd 开机自启（示例文件现成） |
| 打开模型 | 卡片/详情抽屉 🖨 一键调起 Bambu Studio；📁 在 Finder 中打开归档目录 |
| 上传 | Finder 拖拽 3MF 到页面即可，支持上传进度条 |
| 缩略图 | 从「照片」拖出的 HEIC 上传后由系统自带 `sips` 自动转 JPEG，零安装 |
| 外观 | 深色模式默认跟随系统外观，可手动三态切换 |
| 隐私 | 全部数据保存在本机，服务只监听 `127.0.0.1`，不暴露局域网 |

## 安装与启动

### 前提

- **Python 3.10+**：macOS 自带的 `python3`（Xcode Command Line Tools 提供）版本通常偏老，不一定满足。推荐用 [uv](https://docs.astral.sh/uv/)，它会自动准备合适版本的 Python：

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh   # 安装 uv（已装可跳过）
  cd 3mf_manager
  uv sync                                            # 自动创建 .venv 并装好一切
  ```

- **浏览器**：Chrome / Edge / Arc 体验最佳；Safari 基本可用，但有一处限制（见 [Safari 与「照片」图库](#safari-与照片图库的限制)）。
- **Bambu Studio**（可选）：想用「🖨 Bambu 打开」功能需先安装 [Bambu Studio](https://bambulab.com/en/support/download-studio)，默认装到 `/Applications/BambuStudio.app`。

### 启动

```bash
# 前台运行（试用/开发）
uv run mfmanager            # 默认 http://127.0.0.1:8000
uv run mfmanager 9000       # 指定端口

# 浏览器打开（macOS open 命令）
open http://127.0.0.1:8000
```

首次启动进入「设置」页配置 LLM 与模型根目录（见 [README 配置说明](README.md#%EF%B8%8F-配置)）。

## 后台常驻：service.sh

日常使用推荐 `service.sh` 后台守护，关闭终端后服务继续运行：

```bash
./service.sh start      # 后台启动
./service.sh status     # ● 运行中 (PID 1234)  HTTP 200 @ http://127.0.0.1:8000
./service.sh logs       # 最近 50 行日志；logs -f 持续跟随
./service.sh restart    # 重启
./service.sh stop       # 停止
```

- 换端口：`PORT=9000 ./service.sh start`
- PID 与日志分别落在 `./run/server.pid`、`./logs/server.log` / `./logs/server.err`；启动失败先看 err 文件。
- Python 解释器优先用项目 `.venv`，没有则回退系统 `python3`。
- 脚本本质是 `nohup` 守护，重启 Mac 后不会自动恢复，需要自启请看下一节。

## 开机自启：launchd

项目自带 launchd 模板 `scripts/com.mfmanager.plist.example`，登录后自动启动并由 launchd 保活（崩溃自动拉起）：

```bash
# 1. 生成 plist（把占位路径整体替换为你的实际项目路径）
mkdir -p ~/Library/LaunchAgents
sed -e "s|/Users/USER/path/to/3mf_manager|$HOME/workspace/3mf_manager|g" \
    scripts/com.mfmanager.plist.example > ~/Library/LaunchAgents/com.mfmanager.plist

# 2. 加载并立即启动
launchctl load ~/Library/LaunchAgents/com.mfmanager.plist
```

> 新版 macOS 也可用 `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mfmanager.plist`，卸载对应 `bootout gui/$(id -u)/com.mfmanager`。模板端口默认 8000，需要改端口就编辑 plist 里 `8000` 那行。

**验证与卸载**

```bash
launchctl list | grep mfmanager      # 有输出且第一列是数字（PID）即正常
curl -s http://127.0.0.1:8000/api/stats   # 或刷新浏览器页面
tail -n 20 logs/launchd.err          # 未生效时看这里

# 卸载
launchctl unload ~/Library/LaunchAgents/com.mfmanager.plist
rm ~/Library/LaunchAgents/com.mfmanager.plist
```

> ⚠️ **launchd 与 service.sh 二选一**。launchd 启动的进程不写 `run/server.pid`，`service.sh` 无法感知它；两者同时开启会因 8000 端口冲突导致后启动的一方绑定失败（日志出现 `Address already in use`）。选一种方式即可。

## 与 macOS 的系统集成

### 🖨 用 Bambu Studio 一键打开

在「待整理」或「模型库」中，点卡片/详情抽屉里的打印机图标，服务端执行 `open -a BambuStudio <文件>` 直接把该 3MF 送入 Bambu Studio 切片。

- 未安装 BambuStudio 时自动回退：交给系统默认的 3MF 关联应用打开；两者都失败才报错。
- 打开失败排查：确认 App 名称是 `BambuStudio`（`ls /Applications | grep -i bambu`）；装好后第一次打开请在系统弹窗中点「允许」，并确认它能正常关联 `.3mf`。

### 📁 在 Finder 中打开目录

详情抽屉的归档路径行与操作按钮里都有「打开目录」：服务端对归档目录执行 `open`，直接在 Finder 中定位该分类文件夹，方便复制、备份或手动整理。

### 拖拽上传

从 Finder 把 3MF（可多选）拖到「待整理」页上传区即自动解析入库，上传过程有进度条。截图、邮件附件等任何来源的 3MF 同样适用。

## 照片与缩略图

### HEIC 自动转换

卡片上 hover 点 📷 上传自定义缩略图时，直接选 Apple 图库导出的 **HEIC/HEIF**（以及 BMP/TIFF）都没问题：服务端调用 macOS 自带的 `sips` 自动转成 JPEG 存储，**无需安装 ImageMagick**。转换成功会有「已上传并自动转换」提示。

### Safari 与「照片」图库的限制

在 Safari 中点 📷 → 从「照片」图库选图，Safari 有时给出 **0 字节**文件，上传会提示改用 Chrome 或先从「照片」导出。两种绕过方式：

- 缩略图上传这一步改用 Chrome / Edge / Arc；
- 或在「照片」App 里把图片拖到桌面/先导出，再从 Finder 选择上传（拖拽过来的文件不受此限制）。

## 外观：跟随系统的深色模式

界面默认跟随系统「深色/浅色」外观自动切换；侧边栏底部的主题按钮可在 **跟随系统 → 深色 → 浅色** 三态间循环，选择会记忆在浏览器本地。

## 可选：本地 LLM（Ollama）

Mac（尤其 Apple Silicon）跑本地模型很省心，配合本工具的 AI 分类/语义搜索可做到完全离线：

```bash
brew install ollama
brew services start ollama        # 后台常驻（或手动 ollama serve）
ollama pull qwen2.5:14b
```

然后在「设置」页把 Base URL 填 `http://127.0.0.1:11434/v1`、模型填 `qwen2.5:14b` 即可。也可改用任意 OpenAI 兼容云端（DeepSeek 等），配置方法见 README。

## 数据安全与备份

- 所有数据都在本机：`library.db`（索引）、`library_root`（模型文件）、`thumbs/`、`attachments/`、`.trash/`（回收站），不上传任何云端。
- **Time Machine 兼容**：整目录备份即可。备份/恢复 `library.db` 前建议先 `./service.sh stop`，避免拷到写一半的 SQLite 文件。
- **模型根目录别放进 iCloud Drive**：iCloud 的「优化存储」会把文件替换成占位符，解析会读到空文件，还可能触发全库同步；`library_root` 请放在纯本地磁盘路径。
- 服务只绑定 `127.0.0.1`，仅本机浏览器可访问，不会出现在局域网其他设备上，macOS 防火墙一般也不会拦截。

## 常见问题

**启动报 `Address already in use` / 浏览器打不开 8000？**
端口被占用（可能是之前没关干净的开发服务）。查一下并决定杀掉或换端口：

```bash
lsof -iTCP:8000 -sTCP:LISTEN     # 看谁占着 8000
kill <PID>                        # 或换端口：PORT=9000 ./service.sh start
```

**launchd 配好了但开机没起来？**
`launchctl list | grep mfmanager` 确认已加载；再看 `logs/launchd.err`。最常见原因是 plist 里的路径没有替换成实际项目路径，或 `.venv` 在加载时还不存在（先跑一次 `uv sync`）。

**点 🖨 没反应或报「Bambu Studio 打开失败」？**
见上文 [Bambu Studio 一节](#-用-bambu-studio-一键打开)；若装的是 beta 版或改过名的 App，`open -a BambuStudio` 匹配不到，回退逻辑会尝试系统默认应用。

**缩略图上传没内容？**
Safari 图库 0 字节问题（见上文），换 Chrome 或从「照片」导出后再传。

**升级后行为异常？**
`git pull` 后执行 `uv sync` 同步依赖即可；数据库 schema 会自动增量迁移，无需手动操作。
