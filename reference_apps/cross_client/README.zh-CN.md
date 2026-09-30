# 有序跨客户端夹具（LWP-38）

[English](README.md) | 简体中文

此场景使用真实原生控件实现，但**尚未**在 Android 和 Linux 环境执行。它独立于七目标冒烟方案。控制器仍需将这些有序作业绑定并调度到相同冻结源码、平台清单、服务身份、工单 ID 和唯一标题。

1. 启动冻结参考 API。其 `/api/version` 必须返回候选源码指纹和真实 API 产品内容摘要。通过 `POST /api/tickets` 创建一个工单，保留其 ID 和唯一标题作为场景输入。
2. 使用 `apiBaseUrl` 与 `crossClientTicketTitle` 运行编译后的 Android instrumentation 类 `TicketCrossClientTest` 的 `assignApiTicketUsingNativeControl` 方法。Espresso 找到 API 创建的行，点击原生“Assign to member”控件，并在 Activity 重建后验证分配结果。
3. 使用相同 `AGENTFLOW_API_URL` 和 `AGENTFLOW_CROSS_TICKET_TITLE` 运行冻结 GTK 测试 `tests/cross_client/test_assignment.py`。AT-SPI/dogtail 必须读取 Android 的成员分配结果，并点击原生“Assign to manager”控件。
4. 使用相同标题和 API URL 运行打包的 `playwright.cross.config.mjs`。浏览器在刷新前后验证 Linux 的经理分配结果。

前一步报告缺失、失败或未绑定时，后续步骤不得启动。API 写入不能替代任一原生点击。步骤之间更换工单、后端或产物会使场景失效。协调器必须保留原始框架报告、实际组件身份、作业租约和共享服务绑定。

原生原始用例标识在实际 SDK 和 GUI 环境发现之前均为暂定值。本文和源码测试不是通过报告，也不能证明控制器已支持跨节点调度。
