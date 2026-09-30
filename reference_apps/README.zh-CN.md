# AgentFlow 冻结参考应用

[English](README.md) | 简体中文

这些夹具使用真实 API 和原生控件，是测试应用，不是生产认证示例。两个模拟身份为 `reference.manager` 和 `reference.member`。业务预期由 `expectations.json` 声明，不得为了适配失败的实现而修改。

共享 Web/API 夹具可在本机运行。原生夹具包含源码、构建方案、单元测试和 GUI 测试，但需要实际操作系统、SDK 和桌面环境。环境不可用必须报告阻塞；原生测试不能把跳过当作通过。

| 目标 | 源码 | 主机与工具链要求 |
| --- | --- | --- |
| Web / API | `web_api` | Node 22.13+、npm、已安装的 Playwright Chromium |
| iOS | `apple/ios` | macOS、Xcode、XcodeGen、已启动且符合声明的 iOS Simulator |
| macOS | `apple/macos` | macOS、Xcode、XcodeGen、GUI 自动化权限 |
| Android | `android` | JDK 17、Gradle 8.7、Android SDK 35、已授权的 AVD/设备 |
| Windows | `windows` | Windows 交互式桌面、.NET 8 SDK、NuGet 依赖 |
| Linux 原生 | `linux_native` | Linux GNOME/X11、Python、GTK3、AT-SPI2、dogtail、pytest |

原生 GUI 测试要求 `AGENTFLOW_API_URL` 指向真实参考 API；Android instrumentation 通过 `apiBaseUrl` 接收同一 URL。缺少后端或控件会使测试失败，不会静默跳过。

`AGENTFLOW_FAULT_MODE=lost_save` 和 `allow_unauthorized` 用于注入故意的后端缺陷，同时保持测试预期不变，以证明测试能检出故障。正常参考运行不要启用缺陷。

通过 `node web_api/bundle/product/server.js` 启动 API，默认端口为 8765。每次测试执行必须使用独立数据目录和预留端口。

正式测试前必须冻结原生构建输出和测试包。Apple 方案使用 build-for-testing / test-without-building；Windows 使用 no-build；Android 提供预构建 APK、instrumentation 和独立 JVM 测试包。

节点监督器独立于生成项目的进程。节点必须使用已验证的操作系统隔离，或为这些已知夹具显式配置可信项目模式。OS/SDK 可用与应用测试报告通过是不同事实；仓库存在并不代表已经验证平台部署。

## 七目标配置模板

以下文件描述完整参考范围，均为已完成 schema 校验的**配置模板**，不是执行回执或预批准计划。

| 文件 | 契约与用途 |
| --- | --- |
| `agentflow.project.json` | `ProjectExecutionSpec`；每个目标各一套构建、单元和集成方案，并显式包含 iOS/Android 安装。 |
| `target-configs.json` | 七个 `TargetConfig` 的数组，用于所有者运行计划的 `target_configs` 字段；稳定 ID 关联方案与用例。 |
| `test-plan.json` | `TEST_PLAN_SCHEMA`；26 个必需用例映射。普通工作流按阶段登记已接受的单元/集成计划产物，保持身份和映射不变。 |

方案中的路径均相对于**将本目录作为独立源码仓库根目录**的布局。导入项目前，应把版本化参考源码和配置放入独立 Git 仓库。流水线读取候选提交根目录的 `agentflow.project.json`；直接指向上层 AgentFlow 仓库而不迁移和调整方案并不等价。源码快照不要包含本机 `node_modules`、`bundle`、报告或原生构建目录。

模板故意保留 `OWNER_*` 值。接受运行计划前必须全部替换，并将最终配置与源码一起提交。未解析值不是通配符，也不允许放宽要求。

- 根据所选环境真实能力报告填写每个 OS 版本和 CPU 架构。Web/API 当前是 Darwin 主机示例；选择 Linux 主机必须显式修订配置。iOS/Android 字段描述设备运行时，其构建主机和 SDK 仍须满足要求。
- 填写已验证的工具/SDK 版本约束。iOS 和 macOS 需要实际 Xcode、XcodeGen 版本；Android 源码声明 compile SDK 35、JDK 17、Gradle 8.7。源码声明不证明节点已安装 SDK。
- 用已登记的工作区、设备或桌面资源替换全部资源 ID。将**每一个** `OWNER_DEVICE_ID` 替换为目标已启动 iOS Simulator UDID 或已授权 Android emulator serial，并选择匹配的设备型号与运行时。
- 将 `OWNER_REFERENCE_API_HOST` 替换为执行环境可访问的真实参考服务地址。Android 模拟器可能需要主机网关地址；远程原生节点不能假定自己的回环地址就是控制器。预留端口和数据目录，从当前冻结产品启动 API 并建立其产物身份。启动时设置 `AGENTFLOW_SOURCE_FINGERPRINT` 为冻结源码指纹；`/api/version` 计算运行包真实内容摘要。`service_target_config_ids` 选择冻结 API 组件；接受原生测试前，节点/控制器要求源码与产品身份均匹配。未绑定服务会阻塞。Web/API 方案默认自行启动冻结的本机服务。
- Web GUI 执行需要固定版本的 Playwright 浏览器和真实浏览器探针。原生桌面自动化需要相应屏幕、输入和辅助功能权限。缺少资源或权限仍为阻塞。

## 冻结输出布局

| 目标 | 产品输出 | 预构建测试输出 | 正式测试选择 |
| --- | --- | --- | --- |
| Web / API | `web_api/bundle/product` | `web_api/bundle/tests` | Node 单元文件；分别打包的 Playwright Web/API 配置。 |
| iOS | `build/Build/Products/Debug-iphonesimulator/TicketIOS.app` | `build/Build/Products` | 唯一匹配的 `.xctestrun` 中的 `TicketIOSUnitTests` / `TicketIOSUITests`。 |
| Android | `android/app/build/outputs/apk/debug` | `android/app/build/agentflow-tests` | 独立 JVM 单元 jar / 已安装的 Espresso instrumentation APK。 |
| Windows | `windows/TicketClient/bin/Debug/net8.0-windows` | `windows/TicketTests/bin/Debug/net8.0-windows` | 冻结 `TicketTests.dll` 中的 `RulesTests` / `GuiTests`，分别使用类过滤器。 |
| macOS | `build/Build/Products/Debug/TicketMac.app` | `build/Build/Products` | 唯一匹配的 `.xctestrun` 中的 `TicketMacUnitTests` / `TicketMacUITests`。 |
| Linux | `linux_native` | `linux_native/tests` | 声明的 X11 环境中分别运行单元和 GUI pytest 目录。 |

Apple 完整 Products 包保留 `.xctestrun`、依赖测试 bundle 和宿主应用。Linux 产品包也包含参考测试；控制器物化包时，重叠文件必须逐字节一致。上述布局仍需在实际平台成功构建。

报告写入工作区根目录的 `reports/`，位于冻结产品和测试包之外。测试框架临时文件也必须位于冻结包之外；正式运行若修改冻结包必须失败。Android 单元报告模板预期 JUnit Vintage XML，因为源码使用 JUnit 4；请按所选 console 版本确认文件名和原始用例标识。

首次冻结构建前，Windows 两个项目都需要真实 `packages.lock.json`；见 [Windows 说明](windows/README.zh-CN.md)。同一测试程序集同时包含 NUnit 规则与 FlaUI 类，必须遵守两个阶段分别指定的类选择器。以单元标签运行完整测试不能证明阶段分离。

## 用例身份与证据状态

三个 Node 单元 ID、三个 API 和两个 Web Playwright ID 已通过生产报告解析器从本机 `web_api/reports/` 的实际报告读取。Playwright ID 故意保留末尾 `::` 项目名分隔符。报告是本机忽略的证据，新候选必须重新生成。

模板中的全部 iOS、Android、Windows、macOS、Linux 原始报告 ID 均为**根据源码声明推导的暂定值**，尚未在所选环境通过原生执行确认。XCTest 导出标识、NUnit logger 名称、JUnit Vintage 名称和 pytest classname 前缀可能随工具版本与发现根目录变化。正式验证前，请根据实际原始发现/执行报告校准，确认仍代表预期源码用例，并创建新的已接受计划/配置版本。不得替换失败用例、缩小计划，或把改名/缺失用例当作通过。

`test-plan.json` 显式记录这些未知项。七个 schema 合法的目标项、完成的静态探针或已有本机 Web/API 报告，均不能证明支持七个平台或已交付 Git 候选。
