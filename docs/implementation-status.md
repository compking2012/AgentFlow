# AgentFlow 本地实施状态

本表逐项对应本地实施清单的全部 **38 个 LWP**，按当前工作区代码、已运行的模块测试和明确的参考执行记录整理。**七目标 MVP 的正式出口验收尚未完成**：真实付费 LLM 链路、五种原生 SDK/GUI 组合及真实跨端业务链仍没有完整验收证据。

来源：[本地实施清单 v0.3](AgentFlow-MVP-本地多平台设计评审包-v0.3/AgentFlow-MVP-本地实施清单-v0.3.csv)。本文不改写清单中的范围、必需目标或验收分母。

当前 Web/API 产品入口已具备安全模型配置、CLI/WebUI目标提交、按目标自动准备本机执行器、完整研发与人审、真实测试、保留测试的失败修复，以及可运行 ZIP 导出/启动。两条完整入口均通过 [严格工程验收](../validation/goal-to-product/20260919T152350Z/README.md)：模型响应为显式协议测试接口，SDK/CLI工具执行、产品代码、构建、报告、Git与运行检查均真实完成。下面38项状态仍表示原始多平台范围，不能据此宣称真实模型和全部原生目标已验收。

## 状态口径

- **implemented**：对应的控制/存储/协议机制已有实现和有意义的本地执行证据；仅限列明的范围，不自动表示所有平台和全部 LAC 通过。
- **partial**：已有部分能力或本地集成，但清单仍有功能差距、业务闭环或适用组合未覆盖。
- **unverified**：该工作包要求的核心真实出口尚未验证；即使已有源码、适配器和模拟回执测试，也不能作为验收通过。

当前分布：**10 implemented、21 partial、7 unverified**。这不是测试通过率，也不是产品完成百分比。

| ID | 工作包（CSV 原名） | 状态 | 当前实现与本地证据 | 未完成 / 未验证边界 |
| --- | --- | --- | --- | --- |
| LWP-01 | 本地配置与模型选择 | implemented | 本地 Settings、模型请求名/文档版本/接受状态与凭据引用分离；默认 DeepSeek profile 保持 pending，不自动确认或发付费请求。[配置](../src/agentflow/settings.py)、[模型/计划测试](../tests/control/test_workflow.py) | 当前真实模型路由与供应商返回版本尚未付费验收；七目标声明不表示七目标已实测。 |
| LWP-02 | SQLite WAL 单写命令账本 | implemented | SQLite WAL 单写 actor、事务内业务/事件/幂等回执、CAS 和进程锁；提交/回执丢失可重放。[Store](../src/agentflow/storage/store.py)、[存储测试](../tests/storage/test_store.py) | 结论限于本地 SQLite/文件系统测试；不包含多控制器写入或云数据库扩展。 |
| LWP-03 | 本地文件产物与备份 | implemented | 内容摘要、原子发布、校验读；应用 checkpoint 覆盖 DB、普通/节点产物、模型收据、独立 Git 对象及工作区，恢复时暂停/隔离旧执行。[应用备份](../src/agentflow/control/backups.py)、[恢复测试](../tests/control/test_application_backup.py) | 应用备份 9 项真实文件/Git/SQLite 测试通过；外部用户仓库须另行保全。未做真实生产数据灾难演练；恢复不是旧进程已停止的证明。 |
| LWP-04 | 本地服务与管理边界 | implemented | 回环 owner API、Origin/Host 校验、内存 Bearer、独立 mTLS 节点入口；Unix owner IPC 可换启动码而不重启任务。[边界测试](../tests/control/test_api.py)、[IPC测试](../tests/control/test_owner_ipc.py) | IPC 为 POSIX 控制机实现。隔离运行的部署前提仍适用；不将同 UID 的可信模式当作强进程身份隔离。 |
| LWP-05 | Runtime：CodingAgent / ModelProvider 双接口 | implemented | CodingAgentAdapter / ModelProvider 独立契约、能力声明与受管运行服务；有实际 SDK/CLI 后端，不以空适配器表示支持。[契约](../src/agentflow/runtime/contracts.py)、[真实后端协议测试](../tests/runtime/test_backend_protocols.py) | 已验证的是已安装后端及本地协议服务；更换到其他后端/供应商需重新验证能力。 |
| LWP-06 | Runtime：版本与能力探针 | partial | 记录版本、选项、实际本机沙箱与 apply_patch 协议结果。实测 OpenHands 1.49.2、Codex 0.154.0-alpha.6.2。[版本与边界记录](../src/agentflow/runtime/README.md) | 设计清单的 CLI 0.135 基线不是本轮实际安装版本；真实 LLM 路由、供应商版本/计费与全部恢复组合尚未验收。 |
| LWP-07 | Runtime：OpenHands 非编码适配 | partial | 实际 OpenHands SDK 使用固定受控工具，只读源码、写独立产物，不自行启动编码后端；受控并行提案交控制器建项。[工具](../src/agentflow/adapters/openhands/tools.py)、[SDK测试](../tests/runtime/test_backend_protocols.py) | 尚未由真实 LLM 完成调研/产品/架构/Review 的业务质量验收；通用子任务协商仍受现有规划 schema 和阶段约束。 |
| LWP-08 | Runtime：Codex exec JSONL 适配 | partial | 受管 Codex exec、私有配置/认证目录、JSONL、输出 schema、真实 apply_patch 与工具回传已在本地 Responses fixture 验证。[适配器](../src/agentflow/adapters/codex/adapter.py)、[协议测试](../tests/runtime/test_backend_protocols.py) | 未做选定付费模型驱动的真实编码任务验收；不能把脚本化 HTTP fixture 当作模型推理/任务完成能力证明。 |
| LWP-09 | Runtime：受管进程 supervisor | partial | 真实子进程启动意图、launcher 握手、PID/boot 身份、日志、宽限取消和观察恢复。[supervisor](../src/agentflow/runtime/supervisor.py)、[进程测试](../tests/runtime/test_supervisor.py) | POSIX 进程组与已观察后代是 observed containment；任意脱离子进程的完全清理尚不能承诺。 |
| LWP-10 | Runtime：工作区与认证隔离 | partial | 独立克隆和私有 HOME/CODEX_HOME，macOS 外层 Seatbelt 实测文件/网络允许与拒绝，越界 apply_patch 被拒。[沙箱](../src/agentflow/runtime/sandbox.py)、[隔离测试](../tests/runtime/test_sandbox_tools.py) | Linux/Windows 控制机强沙箱未实现。Codex 内层开放模式依赖已验证的强制外层 Seatbelt；不能单独启用。节点强身份隔离见 LWP-30。 |
| LWP-11 | Runtime：本地双协议模型代理 | partial | 原生 Chat Completions 与 Responses 双协议代理、attempt 令牌、固定上游、流式收据、实际工具回传；不做协议转换。[代理](../src/agentflow/models/service.py)、[HTTP测试](../tests/models/test_proxy.py) | 只有真实本地 HTTP/SDK/CLI 联调证据，没有选定 DeepSeek/其他付费供应商的正式调用验收。 |
| LWP-12 | Runtime：本地限额与费用账本 | partial | Run/Iteration 双额度原子预留、累计请求上限、幂等结算、未知责任与恢复后冻结；OpenHands 工具入口限额在副作用前检查。[费用账本](../src/agentflow/models/budget.py)、[预算测试](../tests/models/test_budget.py) | Codex 工具次数仅 observed，hard_required 会阻止该后端；输入/输出费用上界依赖明确审阅配置。节点资源时间/工具费用尚未形成统一货币账本，不能报作已确认零费用。 |
| LWP-13 | Runtime：事件与日志归一 | implemented | 后端 JSONL/原始日志保留、归一与脱敏；SQLite 事件为业务事实，未知/坏行和退出0无产物不算成功。[事件归一](../src/agentflow/runtime/events.py)、[事件测试](../tests/runtime/test_sandbox_tools.py)、[节点结果测试](../tests/execution/test_result_validation.py) | 新 SDK/CLI 事件类型需补解析与兼容验证；UI 展示摘要和证据，不展示内部推理链。 |
| LWP-14 | Runtime：进程重启与调用对账 | partial | 启动扫描、原进程/回执检查、节点作业恢复、未知调用保留、旧 fence 拒绝；不盲重启或退款。[进程恢复](../tests/runtime/test_supervisor.py)、[节点恢复](../tests/execution/test_daemon_recovery.py)、[账本对账](../tests/models/test_budget.py) | 真实供应商断流后计费对账未验；不承诺 ephemeral 后端能恢复远端会话。任意原生设备/桌面的恢复仍需实机验证。 |
| LWP-15 | Runtime：可信diff与源码候选冻结 | implemented | 可信完整 diff、范围/链接检查、真实 Git commit/tree 冻结、精确 blob tar；测试实现有独立 Review 阶段。[Git收集](../src/agentflow/repository/git.py)、[源码/门禁测试](../tests/control/test_execution_pipeline.py) | 拒绝尚不支持的源码 symlink/submodule 归档；代码证据链已测，真实 LLM 的审查质量另行验收。 |
| LWP-16 | Runtime：本地DAG与七角色调度 | partial | 持久 DAG、七角色可展开、统一3槽位/写范围互斥、独立节点资源租约、待审核/节点等待不占 Agent 槽。[展开](../src/agentflow/domain/expansion.py)、[并发测试](../tests/domain/test_expansion.py)、[调度测试](../tests/control/test_workflow.py) | 已验证调度分配与角色/质量门禁约束；尚未用真实 LLM 完成“七角色各2实例、开发3实例”的业务并行验收。 |
| LWP-17 | 目标、调研与PRD/需求 | partial | 目标/调研/PRD/需求阶段、版本化产物、复用输入和只允许授权主机的资料读取；来源 URL/时间/摘要可留证。[调度提示与输入](../src/agentflow/control/scheduler.py)、[资料工具](../src/agentflow/adapters/openhands/tools.py) | 尚未完成真实产品目标→矛盾/不可访问资料处理→稳定需求ID的 LLM 验收；需求覆盖与矛盾处理主要仍由文档内容和提示约束。 |
| LWP-18 | 基本架构与API资产 | partial | 架构/Mermaid/API 设计可作为版本化文档产物，输入/返工沿依赖失效。[阶段与产物](../src/agentflow/control/scheduler.py) | 尚无完整的类型化架构/API资产仓库、接口消费者图、删除/兼容性规则及已交付设计与目标设计专门视图；生成文档不等于这些能力已实现。 |
| LWP-19 | 开发/验证工作项规划 | partial | 规划 schema、受控子任务展开、DAG/范围校验、必需汇总、并行 Git 组装与冲突阻塞。[展开测试](../tests/domain/test_expansion.py)、[真实组装测试](../tests/repository/test_assembly.py) | 尚未以结构化需求全集验证“无遗漏覆盖”，架构/API契约就绪与代码验证的细粒度资产依赖仍不足；真实模型规划有效性未验。 |
| LWP-20 | HITL与版本失效 | partial | 按步审核、revision/fingerprint 绑定、驳回换代、选择性失效、过期批准拒绝及失败质量不被人审抹除。[流程测试](../tests/control/test_workflow.py)、[浏览器审核测试](../tests/browser/dashboard.spec.ts) | 单用户实现有 approve/reject/运行取消；清单中的审核“转交”没有独立流程。跨平台完整多轮人审/返工仍待最终验收。 |
| LWP-21 | 独立代码Review | partial | 只读 Review、精确 commit、阻断 findings、分阶段测试代码 Review；可从完整审查快照定向返工并行模块并重新审核。默认最多100次质量返工（auto_review_repair_limit=100），-1持续到通过、0关闭或正数限轮，保留共享预算。[Review/修复](../src/agentflow/control/remediation.py)、[修复测试](../tests/control/test_remediation.py) | 尚无完整问题去重/关闭责任工作流；未知执行、未核对预算、活跃受影响任务或待人审仍须先解决。执行结束不等于审查通过，不承诺自动解决任意缺陷。 |
| LWP-22 | 独立单测与测试编写 | partial | 独立单测计划/实现/Review/执行阶段、不可缩减的 framework case ID、原始报告解析与零用例/失败阻断。[执行管线测试](../tests/control/test_execution_pipeline.py)、[报告解析](../tests/testing/test_reports.py) | Web/API Node 单测有真实执行；五原生单测尚无实际 SDK/设备编译与运行证据；覆盖率采集也不是完整跨框架能力。 |
| LWP-23 | Web/API真实集成测试 | partial | 真实 NodeRunner 构建并冻结 Web/API，Node 3单测、API 3集成、Web 2集成通过；lost_save/allow_unauthorized 均被实际断言检出。[参考源](../reference_apps/web_api)、[适配器](../src/agentflow/testing/adapters.py) | 这些结果属于下文给出的固定参考快照；真实模型方案→编码→Review→交付整体闭环尚未运行，原生跨端部分见 LWP-38。 |
| LWP-24 | 阶段化迭代与局部终点 | implemented | full/from_to/selected 编译、有效产物复用、已有套件诊断子集、局部终点与代码交付必需验证分离。[计划测试](../tests/domain/test_planning.py)、[晚入口与子集测试](../tests/control/test_execution_pipeline.py)、[复用输入审查](../tests/audit/test_control_invariants.py) | 控制层范围/门禁有测试；五原生已有应用的实机晚入口验收尚未完成，不能从规划就绪推断执行通过。 |
| LWP-25 | 纯Git候选与本地交付 | implemented | 真实 Git bundle导入、OID/tree核对、base/ref事务与DeliveryIntent，Git已写/DB确认丢失可协调且不覆盖用户工作区。[交付](../src/agentflow/control/delivery.py)、[真实Git故障测试](../tests/control/test_delivery.py) | 质量检查输入在交付单元/集成测试中部分为明确fixture；该结果不表示真实 LLM和七平台已产出可交付产品。远端PR不作为本地出口要求。 |
| LWP-26 | 精简Dashboard与证据导航 | partial | React/TypeScript 工作台读取真实API，展示运行/DAG/角色/节点/审核/产物/七目标证据/交付；18项真实Chromium交互验证。[Dashboard说明](../apps/dashboard/README.md)、[浏览器测试](../tests/browser/dashboard.spec.ts) | 架构/API专门资产编辑/消费者导航、完整费用明细、跨端场景专门可视化仍不齐备；模型配置当前只读。 |
| LWP-27 | 七目标参考样本与能力注册 | partial | 共享工单API/Web和五原生源码、稳定控件、26个模板case映射、可控缺陷与证据格式；owner公开API可登记资源、基于冻结candidate发起integration功能探针，并凭真实validated result确认能力。[参考模板](../reference_apps/README.md)、[节点能力注册测试](../tests/control/test_owner_node_workflow.py) | 原生SDK/设备/报告ID仍待校准；Windows锁文件需实机构建准备；不是七平台已实装/实测的能力注册。 |
| LWP-28 | 本地打包与七目标出口验收 | unverified | 具备本地Python应用、CLI、静态Dashboard、依赖锁、备份恢复及多模块自动化测试。[应用入口](../src/agentflow/application.py)、[验收清单](../docs/AgentFlow-MVP-本地多平台设计评审包-v0.3/AgentFlow-MVP-本地验收清单-v0.3.md) | 未完成LAC-01~34全部正式出口：缺真实模型选择/计费链路、五原生组合、真实跨端链、新项目完整交付及已有项目新增/修改/删除各一轮验收。不能标记MVP整体通过。 |
| LWP-29 | 自有执行节点与受限协议 | implemented | 真实mTLS配对/吊销、scope令牌、分块摘要校验、build与formal输入区分、恢复/回执去重；owner公开API提供资源登记、功能探针/确认、作业检查/取消和校验证据下载；真实TLS→npm build→冻结Node test→上传ACK恢复测试。[节点协议](../src/agentflow/execution/service.py)、[真实TLS闭环](../tests/execution/test_client_tls.py) | 所证实运行组合是本机真实工具链与已声明可信参考项目模式；不抵扣其他OS的原生测试或强隔离验收。 |
| LWP-30 | 原生环境预检与资源隔离 | partial | 实际主机/工具/显示/设备探针、目标配置身份核对、排他租约、失联隔离和签名清理；Windows/macOS/Linux/移动端有明确检查路径；同环境静态刷新仅在原始功能证明完整时保留能力，boot/工具/目标变化将旧能力supersede。[环境探针](../src/agentflow/execution/capabilities.py)、[清理](../src/node_agent/cleanup.py)、[节点/资源测试](../tests/execution/test_nodes.py)、[能力生命周期测试](../tests/execution/test_capability_lifecycle.py) | 移动停止命令未在真实设备验收；测试进程与持钥collector的全平台强身份隔离尚未完成。Wayland明确blocked，不静默降为X11；trusted-project模式必须显式声明。 |
| LWP-31 | iOS原生测试适配 | unverified | SwiftUI/XCTest/XCUITest源码、XcodeGen、build-for-testing/test-without-building、安装及xcresult解析路径已实现。[Apple参考源](../reference_apps/apple/ios)、[无重编译命令测试](../tests/testing/test_manifests_adapters.py) | 没有实际iOS SDK/模拟器/设备构建、单测、GUI或生命周期结果；OWNER_DEVICE_ID等须配置，原生case ID为provisional，Apple输出目录symlink布局仍需真实验证。 |
| LWP-32 | Android原生测试适配 | unverified | Android Views、Gradle/JUnit打包、Espresso/instrumentation、精确APK安装及force-stop/pidof清理代码。[Android参考源](../reference_apps/android)、[适配器](../src/agentflow/testing/adapters.py) | 没有真实Android SDK/设备构建、JUnit、原生GUI与权限结果；包布局、runtime、case ID及清理命令都需实机验收。 |
| LWP-33 | Windows原生测试适配 | unverified | WPF/NUnit/FlaUI源码、锁定restore、--no-build/--no-restore、RulesTests/GuiTests类过滤。[Windows准备说明](../reference_apps/windows/README.md)、[命令测试](../tests/testing/test_manifests_adapters.py) | 无真实Windows SDK/交互桌面运行；两个项目的packages.lock.json须生成、审阅并冻结，程序集布局/原始ID/权限仍未验证。 |
| LWP-34 | macOS原生测试适配 | unverified | macOS SwiftUI/XCTest/XCUITest源与预构建测试路径，原生窗口测试通过实际后端观察业务。[macOS参考源](../reference_apps/apple/macos)、[适配器](../src/agentflow/testing/adapters.py) | 本机存在macOS不等于执行过此原生客户端：Xcode构建、单测、GUI授权、bundle布局与xcresult均未完成正式实测。 |
| LWP-35 | Linux桌面原生测试适配 | unverified | GTK3/dogtail/AT-SPI/pytest源码、X11明确要求、Python包冻结与只读正式测试路径。[Linux参考源](../reference_apps/linux_native)、[显示协议拒绝测试](../tests/testing/test_manifests_adapters.py) | 没有实际Linux GNOME/X11/GTK桌面单测+GUI链路；Web/API/普通Python通过不得抵扣。Wayland不支持，Xvfb与物理桌面需分别验证。 |
| LWP-36 | 七目标测试方案与代码生成 | partial | 七目标TestPlan/BuildRecipe/ProjectExecutionSpec、计划/编码/Review/执行分离、框架case映射和独立模板。[测试计划](../reference_apps/test-plan.json)、[阶段管线](../src/agentflow/control/execution_pipeline.py) | 未做真实LLM按七目标生成/审查测试代码的验收；五原生工具与报告ID仍未实测，模板不构成已执行证据。 |
| LWP-37 | 候选构建冻结与七目标矩阵门禁 | partial | SourceManifest→BuildArtifact→PlatformManifest两次冻结、完整矩阵、原始报告和产品/测试摘要核对，变包/缺键/零用例/诊断子集不能放行。[清单](../src/agentflow/execution/manifests.py)、[门禁测试](../tests/domain/test_gates.py)、[管线测试](../tests/control/test_execution_pipeline.py) | 统一控制逻辑与Web/API冻结执行有证据；五原生真实产物/清单仍缺，不能宣称七目标总门禁已正式通过。 |
| LWP-38 | 真实跨端业务链路 | unverified | 持久跨端Coordinator已实现：API创建intent→Android UI→GTK UI→Web，绑定候选/后端/ticket/namespace，末端统一写checks；32项控制层测试。[场景控制器](../src/agentflow/control/scenarios.py)、[场景测试](../tests/control/test_scenarios.py) | API使用真实本地ASGI，native回执/效果是明确fixture，不是真实Android→Linux→Web验收。冻结后端生命周期必须预先配置，不存在已实现的后端托管。 |

## 证据范围

1. **控制层与存储**：真实 SQLite、文件、Git、HTTP/Unix socket、进程与 mTLS 测试证明状态机、幂等、版本、边界和恢复行为。部分质量输入来自测试专用 fixture，不能把它们当作真实模型或原生执行回执。
2. **Runtime/Models**：实际 OpenHands SDK 1.49.2 和已安装 Codex 0.154.0-alpha.6.2 连接本地 HTTP 协议 fixture；包括真实 apply_patch、工具回传、沙箱拒绝和零重试测试。没有调用付费 LLM。fixture 的模型标识只是工具元数据测试，不是替换生产模型。
3. **Web/API 参考执行**：独立 NodeRunner 曾对冻结参考提交 `87c245eec954c5f36906942e56935be8ee1ed355` 执行真实构建和测试。API 为3单测+3集成，Web为3单测+2集成；`lost_save` 和 `allow_unauthorized` 在两个目标的四轮反例均失败，冻结包保持不变。后端实际内容摘要与Python计算一致。这些结果只属于该快照，不应继承为后来任意源码版本的通过。
4. **五原生和跨端**：源码、命令生成、解析器、状态机及注入回执测试均有价值，但缺少真实原生构建/GUI/设备证据。必须保留 blocked/not_run/unverified，不用环境缺失时的 skip 抵扣，也不通过减少平台或用例让出口变绿。

独立 Web/API 证据已归档为可移植附件：[执行汇总](../validation/reference-web-api/retest-report.json)、[原始报告](../validation/reference-web-api/reports)、[SourceManifest](../validation/reference-web-api/source-manifest.json)、[PlatformManifest](../validation/reference-web-api/platform-manifest.json)、[矩阵](../validation/reference-web-api/matrix.json)、[源码包](../validation/reference-web-api/source.tar)与[文件校验清单](../validation/reference-web-api/files.json)。原始报告字节保留；汇总中的原始临时执行路径仅作过程记录，归档文件以校验清单为准。

专项测试覆盖 owner IPC、Scenario（含独立审查发现的DomainError确认丢失回归）、应用备份恢复、有限Review返工、能力生命周期与Dashboard。最终全量数量由统一验证记录汇总；不能把不同快照的模块测试数量相加当作一次完整验收。操作入口与当前UI/API边界见 [本地操作手册](local-operations.md)。

## 正式出口尚需完成

- 明确接受生产模型版本、供应商凭据及计费上界；分别完成 OpenHands Chat Completions 与 Codex Responses 的真实供应商调用、工具循环、取消/未知费用和恢复验收。
- 为每个原生目标落实具体 SDK/工具版本、设备或桌面、控件权限、产品与测试包布局、原始报告 ID；执行真实构建、单测、GUI/集成和故障反例。
- 对同一冻结后端和同一实体，执行真实 API → Android 原生指派 → Linux 原生再指派 → Web 校验；控制层 fixture 和各端独立成功不能替代。
- 补齐结构化架构/API资产与消费者兼容检查、需求覆盖校验、节点资源/工具费用账本及相关 Dashboard 视图；保留各自版本和实际验证状态。
- 在同一版本下完成 LAC-01～34、新项目目标到本地Git交付、已有项目新增/修改/删除各一轮，以及所需备份恢复/取消/崩溃/候选替换故障验收，再决定 LWP-28 是否可标为通过。
