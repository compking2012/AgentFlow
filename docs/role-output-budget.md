# 有限输出额度下的角色结果协议

非编码角色可以把长正文和数组分批保存，最后只向 `finish` 提交小引用。模型不需要在最后一次回复里重新生成整份 PRD、审查问题或测试用例表。平台在本地组装完整结果，按原始冻结 JSON Schema 校验，仍输出完整的 `openhands_final.json` 和 `role_result.json.result`，下游调度、测试计划和 StageContext 的数据格式不变。

## 受控工具

`agentflow_io` 增加以下操作，所有调用仍消耗原有工具额度：

| 操作 | 参数 | 行为 |
| --- | --- | --- |
| `result_begin` | `fields`, `streamed_fields`, `request_id` | 保存短字段，初始化声明的字符串或数组流；返回引用、游标和本次写入大小限制。 |
| `result_append` | `result_ref`, `field`, `chunk_id`, `expected_offset`, `value`, `final=false` | 字符串精确拼接，数组追加完整 JSON 项。`final=true` 显式封口。 |
| `result_status` | 可选 `result_ref`, `field`, `offset`, `limit` | 查询当前引用、游标和封口状态；指定字段时分页读取已保存内容。 |
| `finish` | `result` 或 `result_ref`，严格二选一 | 小结果可直接提交；已有草稿时必须使用引用。引用结果必须全部封口，并通过完整原 Schema。 |

例如，测试计划先用 `fields` 保存 `title`、`summary`、`sources`、`unknowns`，声明 `{"content":"string","test_cases":"array"}`；正文和用例分别追加。每次使用工具返回的最新引用和 `next_offset`，不要自行估算 Unicode 游标。`field` 支持普通顶层名称和 JSON pointer；嵌套流的父容器应先在初始字段中建立。

引用包含 `id`、`revision`、`digest`，不是文件路径。每个草稿绑定 attempt、run、iteration、work item、fencing token、输入指纹和 Schema 摘要。同一 `chunk_id`、同一参数重放不会重复追加；参数不同、游标错误、旧版本引用或向已封口字段追加都会被拒绝。未封口、截断、进程退出和额度耗尽都不会自动完成结果。

每次分块写入的完整参数按转义后的 JSON 字节数限制在 `max(1, min(1 MiB, max_output_tokens // 2))` 内，保留协议与推理开销余量；不再固定限制为 4 KB。直接提交完整结果的序列化大小上限为 `min(1 MiB, max(512, max_output_tokens * 4))`，不再固定限制为 16 KB。两者都使用该任务冻结的模型输出额度，实际模型请求的 `max_tokens` 和推理配置保持不变。Agent 应在允许大小内合并相关章节或多个用例，避免每个短段落都占用一次模型往返；较大结果仍按引用提交。

模型仍可能耗尽输出额度，此时系统明确失败并保留先前确认的分块，不能推测缺失内容。最终结果保留现有 1 MiB 上下文限制，超限要求拆分任务，不删除必要字段或用例来适配大小。模型额度调小后，旧的已确认草稿仍可读取，新的写入遵循新任务额度。

## 持久化与恢复

数据位于当前 attempt 的 `attempt_artifacts/<attempt hash>/.role_output/`。普通文档写入工具不能覆盖该空间、最终结果或错误回执。分块先以不可覆盖的文件保存，再原子提交 manifest；收到成功回执才代表该块已确认。文件操作使用逐级 `NOFOLLOW` 和目录文件描述符，避免目录符号链接把写入带出命名空间。

控制器可调用同步 helper：

```python
identity = result_identity(frozen_task)
receipt = export_partial(artifact_root, expected_identity=identity)

imported = import_partial(
    target_task,
    artifact_root=new_artifact_root,
    source_directory=verified_old_artifact_root,
    expected_digest=receipt["digest"],
    expected_source_identity=identity,
)
```

异步控制器应通过 `asyncio.to_thread` 调用文件 helper。导出文件固定为 `role_output_checkpoint.json`；同一停止进度重复导出字节稳定。回执带内容摘要、来源身份、进度摘要、已确认块数和结果字节数。进度摘要不受草稿 ID 重绑或目录顺序影响，也不会把相同内容的副本当作新进度。

跨 attempt 导入只能由恢复层验证授权后调用。控制器工作项中的 `role_output_checkpoint_id` 指向授权记录；该记录另存真实文件 digest 和来源身份。Agent 工具不能传入旧目录并自行导入。新 worker 只看到重新绑定到本次 attempt 的引用，可按游标读取和继续。降低输出额度不会使已验证的旧块失效，后续新块执行新额度。

## 截断与完成条件

worker 在 SDK 执行任何工具前拒绝 `finish_reason=length`，即使返回的工具 JSON 恰好完整。可信本机代理的闭集 `model_output_limit` 错误也会保留到私有错误回执。仅修补 JSON、推测缺失结尾或看到 EOF，均不能使输出成为成功结果。

最终文件持久化完成后才设置成功标记。失败时已保存内容以检查点保留，执行状态仍为失败；恢复不会绕过工具、请求、时间或全局预算限制。新派发角色的迭代上限由调度器结合已有工具额度设置，显式较小限制仍生效，SDK 的重复无进展检测保持开启。

## 验证

运行：

```sh
.venv/bin/python -m pytest -q tests/runtime/test_output_builder.py tests/runtime/test_openhands_chunked_output.py tests/control/test_role_output_recovery.py
```

覆盖 Unicode 正文和大用例数组精确重组、原 Schema 和 StageContext 消费、未封口拒绝、重放与冲突、初始化和 manifest 中断、目录替换、跨 attempt 授权导入、较小新额度下续写，以及真实 SDK 对完整-looking 截断工具 JSON 和代理 422 的拒绝。SDK 使用本地脚本化 HTTP，不调用付费模型。
