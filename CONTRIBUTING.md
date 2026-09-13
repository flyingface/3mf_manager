# 参与贡献

感谢你愿意为 3MF Manager 贡献代码！请阅读以下约定。

## 开发环境

```bash
# 克隆后
uv sync --dev --extra dev            # 安装依赖（含 pytest）
uv run python -m pytest              # 运行测试
uv run mfmanager 8000                # 本地启动
```

## 分支与提交

- 使用**功能分支**：`feature/<简短描述>` 或 `fix/<简短描述>`。
- 提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：

```
feat: 新增模型辅助分类
fix: 修复中文文件名乱码
docs: 补充架构说明
test: 增加解析器测试
```

## 代码风格

- Python 3.10+，遵循 PEP 8，行宽约 100。
- 核心原则：**保持零第三方运行时依赖**。新增功能尽量用标准库实现。
- 前端 `static/index.html` 为自包含单页，尽量不用外部 CDN。

## 测试

- 所有修复/新功能应配套 `tests/` 下的 pytest 用例。
- 运行：`uv run python -m pytest`。

## 提交 PR

1. 确保 `uv run python -m pytest` 全部通过。
2. 更新 `CHANGELOG.md`。
3. 在 PR 描述中说明改动动机与验证方式。

## 行为准则

- 友好、尊重，就事论事。
- 涉及删除/移动用户文件的逻辑必须保守，避免不可逆操作。
