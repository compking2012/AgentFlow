# 高级 Python API 与证据运维

本文收纳早期操作手册中的高级模型策略、自有节点、原生参考诊断、对账和备份内容。面向维护工具开发者；普通用户使用 [五命令操作手册](local-operations.md)。这些能力不以额外 CLI 子命令、隐藏参数或命令行 JSON 暴露。

以下 Python 片段调用实际内部接口，须在审阅过的维护环境中使用。它们不是供应商 LLM、原生平台或完整跨端链已验收的声明。当前范围和证据见 [实施状态](implementation-status.md)、[验证记录](../validation/README.md)。

## 配置与 owner API

固定配置来源仍是 `~/.config/agentflow/config.toml`。读取同一配置连接已运行的控制器，不另造一套配置路径或在脚本中启动第二个服务：

```python
from agentflow.configuration import load_configuration
from agentflow.control.owner_client import OwnerClient

config = load_configuration(create=False)
client = await OwnerClient.connect(config.settings, start=False)
try:
    products = await client.request("GET", "/api/v1/products")
finally:
    await client.close()
```

异步片段须放入维护程序的异步函数中。OwnerClient 通过私有本机 IPC 换取短期会话，认证仅发送到本机 owner API；不要打印、保存或复制 owner token 到节点。脚本使用实际返回的产品、运行、候选和节点 ID。

内部写 API 仍以持久命令标识保障幂等。维护工具必须保存意图与请求体；未知结果只能用原意图对账，不能换标识重发。公开 CLI 在内部管理这一机制，用户无需操作标识或 JSON。

| 操作 | owner API |
| --- | --- |
| 产品状态与准备条件 | `GET /api/v1/products`、`/products/{id}`、`/product_setup` |
| 模型与受管本机环境 | `POST /api/v1/product_setup/models`、`/product_setup/local_execution` |
| 准备或导出重试 | `POST /api/v1/products/{id}/retry`；服务判定是否允许 |
| 下载与预览 | `GET /api/v1/products/{id}/download`；`POST /api/v1/products/{id}/launch`、`/stop` |
| 既有工程和计划 | `POST /api/v1/projects`、`/run_plans`、`/runs` |
| 质量证据 | `GET /api/v1/runs/{id}/checks`、`/target_matrix`、`/scenarios`、`/deliveries` |
| 运行控制 | `POST /api/v1/runs/{id}/control`，带最新 `expected_revision`、非空原因和动作 |

读取当前 revision 后才能提交控制或审批。人审绑定精确版本，测试失败不能因人工批准改为通过。二进制证据应通过认证下载读取，不能把认证信息写入产物文件。

## 高级模型与费用策略

普通模型默认由 TOML 的 `models.roles`、`models.coding` 或页面维护。内部模型策略由 `agentflow.models.profiles.ModelProfile` 校验；直接构造高级 profile 不等于改变普通产品的配置来源。

专业角色与编码分别要求 Chat Completions、Responses。协议标签不是兼容性证明，待确认 profile 不可派发。模型接受仅是所有者明确选择，不是供应商工具循环、流式行为和计费事实已经验证。

普通产品使用请求限额模式。维护工具若选用有价格上界的严格策略，还须审阅以下字段：

| 字段 | 需要核对的事实 |
| --- | --- |
| `currency` | 实际计价货币，须与运行预算一致。 |
| `input_micros_per_million`、`output_micros_per_million` | 实际每百万 token 价格，单位为该货币的百万分之一。 |
| `input_token_upper_bound` | 已审阅的单次输入上界。 |
| `input_bound_verified`、`output_control_verified` | 仅在对应边界有依据时标记为真。 |
| `source_version` | 本次价格与上界依据的版本。 |

缺少价格事实时不能把费用显示为零，也不能把配置假设当成 SDK 的强制保证。未知调用保留责任和请求额度。Codex 工具次数是观测能力；要求执行前强制工具次数时，相关后端可能阻塞。

## 自有节点的身份和资源

受管本机 Web/API 自动处理执行通道；以下步骤仅用于自有节点。节点只运行构建、安装和测试，不需要 OpenHands、Codex、模型 Key、owner token 或控制器 CA 私钥。

网关设置通过固定 TOML 的 `app` 部分配置。证书创建与指纹核对属于内部维护接口 `NodeCertificateAuthority`；证书必须匹配明确的私网或回环地址，不使用未指定地址或忽略 SAN/证书错误。加载配置本身不等于证书已生成或节点可用。

节点初始化使用实际 Python 接口：

```python
from pathlib import Path
from agentflow.execution.pki import create_node_key_and_csr

node_directory = Path("/absolute/private/node-data")
csr, public_key_fingerprint = create_node_key_and_csr(node_directory, "Reviewed node")
```

在 owner 侧，通过 `POST /api/v1/executor_pairings` 提交标签、位置、所核对的公钥指纹、允许目标和有效时间。把返回的一次性配对码通过受保护输入交给节点的 `node_agent.client.enroll_node(...)`；同时固定真实控制器证书指纹。配对成功后保存节点身份并清除临时配对码，不把配对码放进命令参数、日志或 URL。

资源通过 `POST /api/v1/executor_nodes/{node_id}/resources` 登记，包含真实资源 ID、类型和身份指纹。可用类型包括 workspace、port、device、simulator、desktop_session、display 和 test_data_namespace。登记记录不能证明端口未被其他进程占用，也不能替代设备和会话检查。

目标要求由 `TargetConfig` 校验。它包含明确的系统、CPU、工具、SDK、显示/设备条件及资源 ID；缺失条件不能改成“任意”。Web/API 的原生 UI 框架字段不适用；原生目标须提供实际框架与版本。参考文件的 `OWNER_*` 和 provisional 值须替换为现场事实。

节点维护循环使用 `NodeDaemon`：

```python
from node_agent.daemon import NodeDaemon

daemon = NodeDaemon(node_directory, reviewed_targets,
                    resource_ids=reviewed_resource_ids, trusted_project_mode=False)
try:
    await daemon.initialize()
    # 在维护程序的受控循环内调用，并处理停止、未知执行和资源清理。
    result = await daemon.run_once()
finally:
    await daemon.close()
```

`reviewed_targets` 必须是真实 `TargetConfig` 对象，资源列表必须覆盖其要求。原生环境缺少操作系统隔离证明时，不应宣称可安全执行恶意仓库；任何受信任项目例外都要作为维护程序的明确审阅决定，不能默认为开。未知执行或清理未确认时保持隔离。

## 参考诊断与功能能力确认

参考源码须在独立 Git 根中审阅并提交，`agentflow.project.json` 位于仓库根。它定义版本化构建、安装和测试配方。不要把主仓库子目录直接当作独立工程，或把预先生成的二进制/报告当作真实构建结果。

已有套件诊断可通过 owner API 创建 Project、RunPlan 和 Run。该诊断明确使用 `purpose: diagnostic`、已冻结配方及所需测试阶段；无需模型的诊断不能证明自主研发，也不能用于缩减 `code_delivery` 必需目标和门禁。

引导顺序必须保留：

1. 静态能力匹配的节点领取 build，真实构建并上传产品/测试包。
2. 控制器校验构建后形成平台清单与矩阵绑定。
3. 正式 test 可能等待功能能力；此时不能新建运行绕过。
4. 所有者针对当前唯一候选调用 `POST /api/v1/executor_nodes/{node_id}/functional_probes`。
5. 完整冻结的参考测试真实执行并通过验证后，以其 `result_id` 调用 capabilities confirm，原正式测试继续。

功能探针请求绑定 `candidate_id`、`expected_candidate_fingerprint`、`target_config_id`、`capability_id`。确认路由为 `POST /api/v1/executor_nodes/{node_id}/capabilities/{capability_id}/confirm`，请求只有结果 ID；不能自报 `quality_result: passed`、替换 recipe 或手工上传一个“通过候选”。

通过 `GET /api/v1/executor_jobs/{job_id}` 核对完成状态、通过结论和 validated 结果。服务还核验节点、目标/环境、候选、原始报告和冻结配方。配置、工具、boot 或节点身份变化后，必须使用当前能力记录；过去的 functional_verified 字样不能自动放行。

功能探针结束后资源仍可能等待签名清理回执。保持节点循环运行，不手工把隔离资源改成 available。只读查询可重复，创建、启动和确认的未知结果先查持久记录。

## 原生与跨端现场条件

| 目标 | 需要独立核对的条件 |
| --- | --- |
| Web 参考套件 | 配方固定的 Playwright/browser、实际浏览器、端口和独立数据；不能在正式测试中暗改依赖。 |
| iOS、macOS | Xcode、XcodeGen、实际 SDK、签名/模拟器或桌面权限、唯一 xctestrun、App/test bundle 布局；两个平台分别验收。 |
| Android | JDK、Gradle、SDK、授权设备/AVD、实际 device ID、APK/测试包、instrumentation 与停止后的进程核对。 |
| Windows | .NET、交互桌面/UIA、受审阅锁文件、实际程序集布局和类过滤；锁屏或 Session 0 不等于可交互桌面。 |
| Linux | 具体 GTK/GNOME、X11、AT-SPI/D-Bus 和输入权限；Wayland 缺口不得静默换成 X11。 |

版本与配方以 [参考应用](../reference_apps/README.md) 及实际锁文件为准。模板、静态检查或 API 接口存在不代表原生执行已验收。

原生共享后端与跨端场景需要现场部署所选冻结 API 产品，使用独立数据与可达地址，并让 `/api/version` 返回匹配的源码身份和实际产品摘要。单填 service URL 不表示后端已部署；其端口、数据和生命周期须单独核对。

跨端步骤须在源码冻结前进入 `ProjectExecutionSpec.cross_scenarios`、测试计划和矩阵；对同一实体验证各端真实操作。未知创建结果不能盲目重发 POST；API 写入不能替代原生 UI 操作。缺少功能能力、后端身份或任一必需平台证据时保持阻塞。

## 运行控制、原始证据和备份

工作项绑定输入指纹、世代、尝试和 fence。节点等待、人审等待不占 Agent 槽位。Git 发布先记录意图，再导入精确候选并创建独立交付引用；响应丢失时按引用与意图对账，不重写用户工作区。

纠正失败任务中错误的执行指令时，可在所有者 `POST /api/v1/runs/{run_id}/recover` 请求中提供 `mode="retry"`、明确的 `work_item_id`、`expected_revision` 和可选 `task_correction`。它只替换该单个任务的改正指令，仍使用已核验的代码检查点，保留已接受计划、文件范围、累计用量和人审门禁，并在恢复回执中记录原操作及新指令。自动恢复不接受此字段；不能用它隐式改写多个子任务或增加额度。不提供该字段时，重试保留原任务指令，旧请求的幂等标识不变。

已知 Review 失败的返工受授权范围和次数约束。产品测试修复还要求实际原始报告、当前候选与预算，保留原测试和测试计划，再经过独立 Review 与测试矩阵。证据损坏、活跃执行、人审或预算未对账时不自动返工。

当失败审查同时指出产品源码与测试代码问题，紧邻生产者的写入范围不足以承担全部修复时，可使用所有者专用 `POST /api/v1/runs/{run_id}/review_repairs`。请求字段为 `expected_revision`、`review_work_item_id`、`write_paths`（具体的已存在源码文件列表）和 `reason`，仍需 Origin、所有者会话和幂等键。范围必须覆盖全部 blocking 路径，并处于当前已接受编码祖先的原授权内；关联测试文件的纠正必须在所有者理由中明确说明。目录、敏感文件、运行时文件和冻结构建/配置支撑文件不接受。

该操作从失败审查的完整源码快照创建专门修复工作，保留原生产者、测试、计划和历史用量，只重新运行当前审查与下游；原审查不会被改成通过。新任务沿用本轮模型、全局预算和标准执行额度，旧任务预算不清零或增加。暂停的运行仍保持暂停，运行中的任务在成功命令后继续调度。活跃或未知执行、未核对用量、人审、已冻结候选或已交付状态须先处理。本入口不会注册为模型工具，也不属于自动扩大范围的途径。

测试启动配置错误可通过认证的所有者接口 `POST /api/v1/products/{product_id}/test-runtime-repair` 安排最小修复。请求包含 `expected_revision`（产品版本）、`expected_run_revision`、`candidate_id`、`failed_job_id` 和 `reason`；如需替代错误派发的产品源码修复，再提供其 `replace_repair_id`。运行必须先暂停，旧进程、节点作业及用量责任须完成核验。文件范围由失败阶段决定：单元测试为 `tests/unit.test.mjs`，集成测试为对应的 `tests/api.spec.mjs` 或 `tests/web.spec.mjs`，调用方不能自行扩大范围。

接口保持运行暂停并返回新修复、独立审查任务及运行版本。使用原运行的 `control` 接口恢复后才会执行；成功后重新构建并运行正式测试矩阵。修复必须保留原用例、断言和预期值；被替代任务与失败报告作为历史证据保留，其源码不能进入新候选。此入口不清零预算、不替代质量判定，也不自动进行代码交付。

对于旧版本在审查返工超时后丢失完整源码基线、导致已重生成模块与其他模块出现 `assembly_base_mismatch` 的情况，提供受限的所有者恢复入口。先以 `work_item_id`、`review_repair_id`、`empty_recovery_id` 查询 `GET /api/v1/runs/{run_id}/review_baseline_recovery`；核对预览中的原审查提交、被替代提交、影响范围后，将这三个 ID、返回的 `expected_revision`、`evidence_digest` 及 `reason` 提交到同路径的 `POST`，并提供幂等键。

该入口要求运行暂停、所有执行已停止、原审查与空恢复及错误基线的完整证据链一致、工作区无未保存修改且额度有效；提交前再次核对数据库、文件和进程证明。它只重开目标模块及必要后代，保留原错误产物和账本，恢复后仍保持暂停，随后通过正常 `control/resume` 继续。它不是通用版本回退入口，也不能代替人工审批或质量门禁。正常 retry/continue 已会保留无新增改动的精确源码基线、原返工要求，并单独记录恢复操作说明。

使用 `GET /api/v1/executor_artifacts/{artifact_id}` 获取节点证据，加下载参数取得二进制。调用 `/runs/{id}/control` 时须先读最新 revision。取消进入 stopping/cancelling 不表示已停止；未知执行不能直接重派。

备份使用内部 `ApplicationBackup` API。先通过公开 `agentflow stop` 停止平台，确认退出后，由维护工具独占 Store：

```python
from pathlib import Path
from agentflow.control.backups import ApplicationBackup
from agentflow.storage import Store

store = Store(config.settings.data_dir)
await store.start()
try:
    receipt = await ApplicationBackup(store, config.settings.data_dir).create(
        Path("/absolute/new-private-backup"))
finally:
    await store.close()

# 恢复目标必须为新目录；执行前由维护人员核对备份与外部依赖。
report = await ApplicationBackup.restore(
    Path("/absolute/new-private-backup"), Path("/absolute/new-restored-data"))
```

应用备份包含数据库、相关产物/报告、受管工作区和导出归档。外部产品输出、用户 Git 仓库、运行数据与固定 TOML/密钥须按各自的私有备份策略保全，不把备份作为公开报告包分享。

恢复后，未结束的 Product 阻塞、Run 暂停、旧预览未知、节点撤销/资源隔离和费用责任未对账均是刻意保留的状态。缺少 run_id 不足以证明从未执行，不能直接重试准备。核对 restore-report.json、旧进程、发布回执、外部目录、模型和预算后再决定继续，不通过删目录、换意图或清账本绕过。

接口实现参考：[配置](../src/agentflow/configuration.py)、[产品服务](../src/agentflow/control/products.py)、[执行管线](../src/agentflow/control/execution_pipeline.py)、[节点服务](../src/agentflow/execution/service.py)、[节点循环](../src/node_agent/daemon.py)、[备份服务](../src/agentflow/control/backups.py)。内部 Python API 随开发版本演进，维护工具应使用同版本实现和测试。
