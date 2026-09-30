# Windows 参考应用

[English](README.md) | 简体中文

冻结第一个源码候选前，使用选定的 .NET SDK 恢复依赖并提交 `packages.lock.json`。冻结构建使用锁定恢复；节点不会静默接受新的依赖图。构建 `TicketTests/TicketTests.csproj`，它也会构建 WPF 产品。正式测试使用预构建测试程序集和 `dotnet test --no-build --no-restore`。

设置 `AGENTFLOW_APP_PATH` 为准确的冻结 WPF 可执行文件路径，`AGENTFLOW_API_URL` 为真实参考服务地址。必须有支持 UI Automation 的 Windows 交互式桌面；源码存在不代表该夹具已在 Windows 上执行。
