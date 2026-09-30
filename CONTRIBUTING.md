# 贡献指南

感谢参与 AgentFlow。当前项目为开发版本，受管本机自动执行主要面向 macOS 上的 Node.js Web/API 产品。

## 开发环境

按 [README](README.md#安装开发版本) 克隆仓库并安装锁定依赖。模型配置保存在用户配置目录，不要提交 API Key、`.env`、本机配置、运行日志或用户产品数据。

## 提交改动

1. 先在 Issue 中描述问题、预期行为或较大改动的方案。
2. 从默认分支创建自己的开发分支，保持改动聚焦。
3. 为行为变更补充相应测试，并更新必要文档。
4. 提交 Pull Request，说明目的、验证结果及已知限制。

## 验证入口

```sh
uv run --locked ruff check src tests
uv run --locked pytest tests -q
npm --prefix apps/dashboard run typecheck
npm --prefix apps/dashboard run build
npm --prefix apps/dashboard run test:browser
```

浏览器测试和端到端测试可能需要额外的浏览器、CLI 或运行时环境，具体见 `tests/` 中的说明。标记为 `live` 的付费模型测试必须显式配置和启用，不要把本地模拟结果表述为真实模型验收。

提交前检查差异，确保不包含依赖目录、构建产物、凭据和 `validation/` 中的本机记录。贡献的代码按本仓库 MIT 许可证发布。
