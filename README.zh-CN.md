# AgentFlow

[English](README.md) | 简体中文

把软件产品目标交给 Agent，在本机工作台查看调研、需求拆解、并行开发、独立 Review、测试和交付进展。当前主入口支持 Node.js Web/API 产品，日常只使用五个命令。

工作台提供“创建产品”“我的产品”“需求变更”和执行台，支持多角色并行、具名 Markdown、质量证据及显式恢复。当前完整方案见 [PRD v0.8](docs/AgentFlow-PRD-v0.8.md) 和 [MVP 技术方案 v0.6](docs/AgentFlow-MVP-产品工作台架构与实现方案-v0.6.md)。原生平台可作为产品元数据识别，当前本机自动产品执行明确暂不支持。

| 要做的事 | 使用入口 | 运行与数据变化 |
| --- | --- | --- |
| 创建或导入产品 | 创建产品 | 受理后进入“我的产品”；导入保留原仓库 |
| 改名、编辑未来默认、删除或恢复 | 我的产品 | 不自动运行；删除进入可恢复列表，源码和历史保留 |
| 改变产品目标或平台 | 我的产品 → 编辑配置 → 从头运行 | 保存标记需要重跑；单独确认后建立从目标开始的完整新运行和新迭代 |
| 调整原目标下的功能 | 需求变更 | 从 PRD 开始新迭代，复用当前配置的有效背景 |
| 恢复失败或暂停进度 | 执行台 → 重试中断步骤 / 继续运行 | 沿用原运行与累计预算，保留成功结果，重新验证受影响部分 |
| 刷新工作台 | 当前浏览器标签页 | 自动续接已有连接；首次或控制器重启时使用本机启动链接，无锁定功能 |

首次使用请先按下方「安装开发版本」安装依赖并启用 `.venv`，再按 [本地操作手册](docs/local-operations.md) 填写模型接口。手册中标注「当前这台 Mac」的路径和环境信息是维护者的本机示例，请替换为自己的路径。以下命令在已启用的项目环境中执行。

```sh
agentflow start
agentflow run "做一个团队阅读清单，支持新增、已读标记、筛选，重启后数据保留"
agentflow status
agentflow launch PRODUCT_ID
agentflow stop PRODUCT_ID
```

`start` 自动在后台启动服务并打开 WebUI；已经运行时重新打开工作台。首次启动会创建私有配置文件 **`~/.config/agentflow/config.toml`**。在页面设置模型后，可以使用 `run "产品目标"`，也可以直接在“创建产品”页提交目标。

| 命令 | 用途 |
| --- | --- |
| `agentflow start` | 启动平台并打开工作台。 |
| `agentflow run "目标"` | 创建产品并跟随进展；Ctrl-C 只退出观察。 |
| `agentflow status [PRODUCT_ID]` | 查看总体状态或指定产品。 |
| `agentflow launch PRODUCT_ID` | 启动已经通过交付门禁的产品预览。 |
| `agentflow stop [PRODUCT_ID]` | 无 ID 停止平台，有 ID 停止该产品预览。研发取消在工作台执行。 |

`run` 只提供两个可选的本次输入：`--name` 和 `--output`。例如：

```sh
agentflow run "开发一个支持新增和归档事项的页面" --name "事项清单" --output "$HOME/Products/tasks"
```

模型、目标类型、人审方式、调用次数、执行时限、默认输出目录、平台数据目录和端口全部集中在固定 TOML 文件，不通过命令参数切换。手工修改后运行 `agentflow stop`，等待退出，再运行 `agentflow start`。页面保存模型也写入同一文件，不维护另一套默认配置。具体格式见 [本地操作手册](docs/local-operations.md)。

专业角色需要原生 **Chat Completions**，编码角色需要原生 **Responses**，可分别配置供应商、接口和模型。平台不会偷偷转换不兼容协议或替换模型。Key 可在页面安全输入，也可通过配置文件引用受保护的环境变量；不要把含 Key 的配置文件放入产品仓库。

提交后平台自动准备受管本机执行环境，再按质量门禁推进研发。缺少环境、模型、依据或测试证据会阻塞，内置环境样本不能作为目标产品的实现结果。准备失败或导出失败可在“我的产品”分别点击“重试准备”“重试导出”；后者只重新导出已确认的交付，不重跑模型。已开始的研发在执行台查看恢复选项：重试只处理失败及受影响工作，继续恢复暂停调度，均不自动增加调用额度或清零费用。

交付完成后，页面提供源码包下载和本机启动。输出目录包含 `repository/`、初版 `release/`、后续版本 `releases/<run-id>/` 和独立的 `runtime/`；交付中保留运行包、精确源码、研发文档、可读报告及原始测试证据。当前交付是本地 Git 与文件，不等同于远程推送、合并或上线。

## 安装开发版本

当前受支持的受管本机路径是 macOS。准备 Python 3.12、Git、uv、Node.js 22.13+、npm 10+ 和受支持的 Codex CLI；专业角色运行组件通过下方依赖安装。编码使用你配置的模型接口，不用个人订阅登录替代供应商配置。

```sh
git clone https://github.com/compking2012/AgentFlow.git
cd AgentFlow
uv sync --locked --extra openhands --extra dev
npm --prefix apps/dashboard ci
npm --prefix apps/dashboard run build
source .venv/bin/activate
agentflow start
```

平台数据、产品目录和用户 Git 仓库应分开。已有标签页刷新或 owner 凭据到期时自动续接；页面没有锁定按钮，无法续接时显示“打开本机工作台”。首次打开、控制器重启或票据七天未使用而过期后，再运行 `agentflow start` 建立新连接，现有研发任务不会因此重建。

owner 凭据仅在页面内存中；受限续接票据只保存在按 origin 和端口隔离的 `sessionStorage`，不使用 Cookie 或 localStorage。票据只能续接，不能直接执行管理命令；成功使用滚动续期七天，控制器重启立即失效。后台继续校验精确 Host/Origin。业务结果未知时不自动重发，明确认证失败后的重发保留原幂等键。

## 验证与边界

这是开发版本。不把协议服务器、静态模板或内置样本当成真实 LLM 生成产品的证据。本机 `validation/` 中的运行记录、用户产品信息和测试产物不随公开源码发布；请按下面的检查入口在自己的环境复验。请求次数和执行时限不等于金额预算；没有可靠价格时费用保持未知，实际计费以供应商为准。

普通使用见 [操作手册](docs/local-operations.md)。既有工程、自有节点、原生环境、证据对账和备份恢复已移至 [高级 Python API 手册](docs/advanced-python-api.md)，不增加公开 CLI 命令或隐藏参数。原生 SDK、设备、签名、桌面及跨端后端仍须分别满足实际条件并验收。

开发检查由 `uv run pytest tests -q`、`uv run ruff check src tests`、Dashboard 的类型检查/构建/浏览器测试及打包验证执行。这些检查命令是复验入口，不是验收结果声明。

## 项目结构

| 目录 | 内容 |
| --- | --- |
| `src/agentflow/` | 本机控制器、CLI、工作流与运行时 |
| `src/node_agent/` | 执行节点组件 |
| `apps/dashboard/` | React / TypeScript 工作台 |
| `contracts/` | 接口契约 |
| `tests/` | 单元、集成、浏览器与端到端测试 |
| `reference_apps/` | 环境与跨平台验证样本，不代表自动生成产品 |
| `docs/` | 产品方案、架构与操作手册 |
| `scripts/` | 打包与验证工具 |

## 贡献与安全

提交问题和改进请参阅 [贡献指南](CONTRIBUTING.md)。不要在 Issue、PR、日志或截图中包含 API Key、用户配置或产品隐私数据；安全问题的反馈方式见 [安全政策](SECURITY.md)。模型服务可能产生费用，运行前请确认供应商配置与额度。

## 许可证

项目采用 [MIT License](LICENSE)。第三方依赖及外部工具仍遵循各自的许可证和服务条款。
