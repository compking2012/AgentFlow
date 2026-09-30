"""Closed, user-safe runtime diagnostics. Raw exceptions never become UI text."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from agentflow.common import DomainError, canonical_digest
from agentflow.runtime.codex_output import parse_codex_final
from agentflow.runtime.events import CodexEventNormalizer

FAILURE_MESSAGES = {
    'review_disposition_invalid': '审查归属分析未通过校验，需依据原意见修正分析结果。',
    'review_disposition_unavailable': '审查源码、需求或原任务权限尚不能完整核验。',
    'review_contract_stale': '审查返工依据已变化，需要核对当前源码和需求版本。',
    'review_contract_binding_invalid': '审查返工任务的身份、权限或源码绑定无法核验。',
    'test_migration_invalid': '测试迁移不符合已批准的断言调整，结果未被接受。',
    'test_coverage_invalid': '测试补齐未保留原用例或断言结构，结果未被接受；需按具体校验问题修正。',
    'review_diagnostic_failed': '既有用例诊断未通过，尚不能放行独立复审。',
    'planning_state_changed': '规划输入或目标阶段版本已变化，需要重新读取当前计划并核验已保存草稿。',
    'planning_validation_failed': '计划输出未通过字段与语义校验；已保留草稿，可在原配置和预算内修正当前规划。',
    'controller_validation_failed': '控制器校验未通过，请查看保存的原始诊断；尚未安排自动重试。',
    'response_usage_incompatible': '模型响应已收到，但执行组件无法解析用量信息（SDK 兼容问题）。',
    'invalid_model_output': '模型未返回符合要求的结果，当前阶段无法继续。',
    'model_output_limit': '本次模型响应达到输出上限，未作为完整结果接受。可从已核验的代码或草稿检查点分步继续。',
    'reasoning_output_limit': '本次响应的输出额度被推理耗尽，未返回可执行输出。平台将核验检查点并分析能否缩小步骤继续；连续无进展时暂停。',
    'final_schema_invalid': '编码 Agent 返回的最终结果不符合约定格式，结果未被接受。',
    'final_output_missing': '编码 Agent 已退出，但未找到可用的最终结果文件。',
    'coding_budget_exhausted': '本步骤执行额度已用尽。请查看用量明细，调整额度后从保存的进度重试。',
    'coding_no_progress': '本次小步尚未形成代码改动；请查看原因分析与自动恢复状态。',
    'coding_workspace_unwritable': '获准源码目录无法安全写入，Agent 尚未启动；请查看路径与隔离检查结果。',
    'invalid_coding_checkpoint': '代码进度检查点的身份、版本或内容无法核验，执行已暂停。',
    'stale_coding_step': '收到过期的小步执行回执，结果未被接受。',
    'terminal_event_missing': '编码进程未报告完整结束事件，本次结果无法确认。',
    'execution_receipt_missing': '未找到编码进程的完成回执，执行结果无法确认。',
    'model_authentication_failed': '模型接口鉴权失败，请检查 API Key 与访问权限。',
    'model_credentials_missing': '模型凭据不可用，请检查配置文件或环境变量。',
    'model_rate_limited': '模型调用受到限额限制，请查看本轮运行额度与供应商状态。',
    'model_request_limit_reached': '本轮模型调用次数已达到配置上限，无法继续发起模型请求。',
    'model_connection_failed': '无法连接模型接口，请检查接口地址和网络。',
    'model_request_failed': '模型接口返回错误，请检查供应商服务状态。',
    'model_transport_not_sent': '模型代理未能建立供应商连接，本次调用未发送。',
    'model_transport_connect_timeout': '模型代理连接供应商时超时，调用结果未知，需先核对原调用状态。',
    'model_transport_read_timeout': '模型代理读取供应商响应时超时，调用结果未知，需先核对原调用状态。',
    'model_transport_write_timeout': '模型代理发送供应商请求时超时，调用结果未知，需先核对原调用状态。',
    'model_transport_pool_timeout': '模型代理等待连接池可用连接时超时，调用结果按未知保留，需先核对原调用状态。',
    'model_transport_protocol_error': '模型代理与供应商通信时发生协议错误，调用结果未知，需先核对原调用状态。',
    'model_transport_connection_error': '模型代理与供应商传输数据时发生连接错误，调用结果未知，需先核对原调用状态。',
    'model_request_outcome_unknown': '模型代理无法确认供应商请求结果，未记录可核验的具体异常类型，需先核对原调用状态。',
    'role_tool_transcript_invalid': '角色工具调用与结果消息未配对，需要在原额度内使用新的角色对话重试。',
    'role_tool_limit_exceeded': '角色任务的工具调用额度已用尽，执行已停止，不再请求模型。',
    'role_cancelled': '角色任务已取消，执行已停止，不再请求模型。',
    'task_authorization_expired': '本次执行的模型授权窗口已到期，执行未完成。请核对剩余额度；不会按普通进程失败盲目重试或清零用量。',
    'worker_exited': 'Agent 执行进程异常退出。',
    'worker_timeout': 'Agent 执行超过时长限制。',
    'launcher_startup_timeout': 'Agent 启动等待超时，已核验原启动许可失效、进程已停止，Agent 尚未执行。可在原额度内重试当前步骤。',
    'worker_log_limit': 'Agent 输出超过允许大小，执行已停止。',
    'worker_internal_error': 'Agent 执行组件发生内部错误，请查看受保护的执行日志。',
    'worker_environment_unavailable': 'Agent 无法启动，请检查执行组件及本机运行环境。',
    'execution_unconfirmed': '执行结果无法确认，请先核对原执行状态，避免重复启动。',
    'isolation_unverified': '执行隔离检查未通过，请检查本机执行环境。',
    'isolation_probe_timeout': '启动前的隔离检查超时，Agent 尚未开始执行。可重试当前步骤；持续超时请检查本机负载。',
    'invalid_role_output_checkpoint': '角色草稿恢复检查未通过，Agent 尚未启动；原草稿已保留，请先核验恢复检查点。',
    'coding_budget_unaccounted': '历史编码步骤缺少用量回执，先核对执行耗时与工具用量；未启动新步骤。',
    'coding_budget_uncertain': '编码步骤的累计用量无法核验，不能重置额度继续执行。',
    'source_snapshot_missing': '前序阶段当前版本的代码快照缺失或不一致，尚未开始执行。',
    'assembly_required': '源码存在尚未确认汇总关系的分支，当前审查或测试尚未启动；需先核验提交关系。',
    'assembly_conflict': '并行模块的代码合并存在冲突，需要分析并修复冲突后重新汇总。',
    'assembly_base_mismatch': '待汇总代码使用的基础版本不一致，需要先核对各模块的来源版本。',
    'write_scope_violation': 'Agent 修改了任务授权范围以外的文件，结果未被接受。',
    'no_code_changes': '开发任务没有产生代码改动，结果未被接受。',
    'invalid_task_plan': '任务拆解结果引用了不存在或不明确的阶段。',
    'review_identity_mismatch': '代码审查未对应当前待交付的代码版本，结果未被接受。',
    'budget_unbounded': '模型费用上限尚未配置或验证，任务暂不能执行。',
    'review_failed': '代码审查发现阻塞问题，请查看审查意见。',
    'test_failed': '测试未通过，请查看测试报告中的失败用例。',
    'test_incomplete': '测试存在遗漏或未完成的用例，请查看测试报告中的失败原因和缺失用例。',
    'test_not_passed': '测试执行未完成或报告未通过核验，请查看节点执行结果。',
    'quality_failed': '当前阶段未通过质量检查，请查看对应产物。',
    'work_blocked': '当前阶段缺少执行前提，请查看工作台中的任务详情。',
}

_REASONS = {
    'A required platform test failed or is unknown; inspect original reports': 'test_not_passed',
    'Node result did not pass controller verification': 'test_not_passed',
    'unsafe_sandbox_roots': 'isolation_unverified',
    'sdk_version_unverified': 'worker_environment_unavailable',
    'capability_unverified': 'worker_environment_unavailable',
    'timeout': 'worker_timeout', 'log_limit': 'worker_log_limit',
    'launcher_spawn_failed': 'worker_environment_unavailable',
    'launcher_exited_before_handshake': 'worker_environment_unavailable',
    'FileNotFoundError': 'worker_environment_unavailable', 'PermissionError': 'worker_environment_unavailable',
    **{reason: 'execution_unconfirmed' for reason in (
        'identity_handshake_mismatch', 'launcher_handshake_timeout', 'completion_identity_mismatch',
        'process_disappeared_without_receipt', 'cancel_cannot_confirm_process_identity',
        'forced_stop_requires_cleanup_verification', 'unacknowledged_launch_requires_reconciliation',
        'descendants_not_confirmed_stopped', 'inherited_output_pipes_still_open',
        'invalid_launch_permit', 'launch_not_authorized',
    )},
}

_CONTROLLER_MESSAGES = {
    'Parallel code branches require reviewed candidate assembly': 'assembly_required',
    'No accepted code contributions were collected': 'assembly_required',
    'Role did not complete': 'worker_exited',
    'Execution/collection failed; inspect protected logs': 'worker_internal_error',
    'Dispatch failed; inspect protected controller logs': 'worker_environment_unavailable',
    'Configured model credential is unavailable': 'model_credentials_missing',
    'The configured model credential is unavailable': 'model_credentials_missing',
    'The configured environment credential is unavailable': 'model_credentials_missing',
    'The configured local credential is unavailable': 'model_credentials_missing',
    'Installed SDK has not passed this adapter\'s API contract': 'worker_environment_unavailable',
    'Role worker sandbox did not pass': 'isolation_unverified',
    'Filesystem/network isolation probe failed; execution blocked': 'isolation_unverified',
    'Launcher identity could not be verified': 'execution_unconfirmed',
    'Launch handshake was not observed': 'execution_unconfirmed',
    'Coding attempt did not produce any source changes': 'no_code_changes',
    'Collected code changes exceeded the assigned file scope': 'write_scope_violation',
    'A read-only professional role modified source code': 'write_scope_violation',
    'Plan named an unknown or ambiguous stage': 'invalid_task_plan',
    'Review did not identify the exact candidate commit': 'review_identity_mismatch',
    'Model pricing bound is not configured': 'budget_unbounded',
    'No reviewed pricing bound is configured': 'budget_unbounded',
    'Model input/output cost bounds are not verified': 'budget_unbounded',
    'No persisted launch intent; no new process was started.': 'execution_unconfirmed',
    'Frozen execution configuration is unavailable; existing logs retained and no new process started.': 'execution_unconfirmed',
    'Frozen input/fence changed; existing logs retained and no new execution started.': 'execution_unconfirmed',
    **{message: code for code, message in FAILURE_MESSAGES.items()},
}

_NESTED_PROVIDER_EXCEPTION = re.compile(
    r'(?<![\w.])(?:litellm(?:\.exceptions)?\.|openai(?:\._exceptions)?\.)?'
    r'(AuthenticationError|PermissionDeniedError|RateLimitError|BadRequestError|'
    r'APIStatusError|InternalServerError|APIConnectionError|APITimeoutError|LengthFinishReasonError)\s*:'
)

_CODEX_OUTPUT_LIMIT = re.compile(r'\bIncomplete response returned,\s*reason:\s*max_output_tokens\b', re.I)
_CODEX_RATE_LIMIT = re.compile(r'\blast status:\s*429\s+Too Many Requests\b', re.I)


def runtime_failure_code(value) -> str | None:
    return value if isinstance(value, str) and value in FAILURE_MESSAGES else None


def runtime_failure_message(value) -> str | None:
    code = runtime_failure_code(value)
    return FAILURE_MESSAGES[code] if code else None


def known_failure_reason(value) -> str | None:
    if not isinstance(value, str):
        return None
    return runtime_failure_code(value) or _REASONS.get(value) or _CONTROLLER_MESSAGES.get(value)


def valid_planning_failure_details(details):
    return (isinstance(details, dict) and details.get('origin') == 'planning_validation'
        and details.get('phase') == 'plan_validation' and details.get('category') == 'correctable_output'
        and isinstance(details.get('issues'), list) and bool(details['issues'])
        and all(isinstance(issue, dict)
            and all(isinstance(issue.get(field), str) for field in ('code', 'path', 'message'))
            and all(field in issue and (issue[field] is None or isinstance(issue[field], str))
                    for field in ('stage_key', 'child_key')) for issue in details['issues']))


def classify_role_error(value) -> str | None:
    """Classify bounded error evidence, emitting no captured text or arbitrary type."""
    if not isinstance(value, dict):
        return None
    kind, message = value.get('type'), value.get('message')
    if not isinstance(kind, str) or not isinstance(message, str):
        return None
    # This field is written by the controlled worker from its own protocol
    # validation/proxy failure, never inferred from arbitrary model prose.
    explicit = runtime_failure_code(value.get('runtime_failure_code'))
    if explicit in {'model_output_limit', 'reasoning_output_limit', 'role_tool_limit_exceeded', 'role_cancelled', 'worker_internal_error'}:
        return explicit
    if explicit == 'planning_validation_failed' and kind == 'DomainError' and valid_planning_failure_details(value.get('failure_details')):
        return explicit
    lower = message.lower()
    if ('prompttokensdetailswrapper' in lower and 'has no attribute' in lower
            and 'cache_creation_tokens' in lower):
        return 'response_usage_incompatible'
    if kind == 'ConversationRunError':
        # The SDK wraps provider exceptions, e.g. "... id=...:
        # litellm.InternalServerError: ...". Only explicit closed-set exception
        # labels are evidence; status numbers or arbitrary response text are not.
        nested = _NESTED_PROVIDER_EXCEPTION.search(message)
        if nested:
            kind = nested.group(1)
    if kind == 'LengthFinishReasonError':
        return 'model_output_limit'
    if kind in {'AuthenticationError', 'PermissionDeniedError'}:
        return 'model_authentication_failed'
    if kind == 'RateLimitError':
        return 'model_rate_limited'
    if kind in {'APIConnectionError', 'ConnectError', 'ConnectionError', 'ConnectTimeout'}:
        return 'model_connection_failed'
    if kind in {'APITimeoutError', 'ReadTimeout', 'TimeoutError'}:
        return 'worker_timeout'
    if kind == 'BadRequestError' and re.search(
            r"\bAn assistant message with ['\"]tool_calls['\"] must be followed by tool messages responding to each ['\"]tool_call_id['\"]", message, re.I):
        return 'role_tool_transcript_invalid'
    if kind in {'BadRequestError', 'APIStatusError', 'InternalServerError'}:
        return 'model_request_failed'
    if kind in {'ValidationError', 'JSONDecodeError'} or 'schema-valid finish result' in lower:
        return 'invalid_model_output'
    return 'worker_internal_error'


def classify_codex_failure(*, state, reason=None, exit_code=None, events=(), errors=()) -> str | None:
    """Classify only control events and validated outcomes, never model prose."""
    if state == 'execution_unknown':
        return ('execution_receipt_missing' if reason == 'process_disappeared_without_receipt'
                else 'execution_unconfirmed')
    if state not in {'completed', 'failed'}:
        return None
    known = known_failure_reason(reason)
    if known:
        return known
    for event in events:
        if not isinstance(event, dict):
            continue
        raw = event.get('raw', event)
        if not isinstance(raw, dict) or raw.get('type') not in {'error', 'turn.failed'}:
            continue
        detail = raw.get('error')
        details = [raw, detail] if isinstance(detail, dict) else [raw]
        for item in details:
            message = item.get('message')
            if item.get('code') == 'model_output_limit' or item.get('reason') == 'max_output_tokens' or (
                    isinstance(message, str) and _CODEX_OUTPUT_LIMIT.search(message)):
                return 'model_output_limit'
            if item.get('code') == 'rate_limit_exceeded' or (
                    isinstance(message, str) and _CODEX_RATE_LIMIT.search(message)):
                return 'model_rate_limited'
    if state == 'failed' or (isinstance(exit_code, int) and exit_code != 0):
        return 'worker_exited'
    codes = {str(error).split(':', 1)[0] for error in errors}
    if 'final_schema_invalid' in codes:
        return 'final_schema_invalid'
    if 'final_output_missing' in codes:
        return 'final_output_missing'
    if codes & {'terminal_event_missing', 'events_missing'}:
        return 'terminal_event_missing'
    return 'invalid_model_output' if codes else None


@contextmanager
def _private_attempt_directory(data_dir, folder, identity):
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    parent = os.open(Path(data_dir) / folder, flags)
    try:
        descriptor = os.open(identity, flags, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if info.st_mode & 0o077 or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
                raise ValueError('unsafe_evidence_directory')
            yield descriptor
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def _private_evidence(descriptor, name, maximum):
    file = os.open(name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                   | getattr(os, 'O_NONBLOCK', 0), dir_fd=descriptor)
    try:
        info = os.fstat(file)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022
                or (hasattr(os, 'getuid') and info.st_uid != os.getuid()) or info.st_size > maximum):
            raise ValueError('unsafe_or_oversized_evidence')
        raw = os.read(file, maximum + 1)
        if len(raw) > maximum:
            raise ValueError('oversized_evidence')
        return raw
    finally:
        os.close(file)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_json_field')
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError('nonfinite_json_value')


def _reasoning_only_terminal(raw, recorded_usage):
    """Inspect event kinds and terminal accounting only; never extract reasoning."""
    text = raw.decode('utf-8', errors='strict').replace('\r\n', '\n')
    if not text.endswith('\n\n'):
        return False
    terminal = None
    response_ids = set()
    passive = {'response.created', 'response.in_progress',
               'response.reasoning_text.delta', 'response.reasoning_text.done',
               'response.reasoning_summary_text.delta', 'response.reasoning_summary_text.done',
               'response.reasoning_summary_part.added', 'response.reasoning_summary_part.done'}
    blocks = text.split('\n\n')
    if len(blocks) > 20000:
        return False
    for block in blocks:
        if not block.strip():
            continue
        if len(block.encode('utf-8')) > 2 * 1024 * 1024:
            return False
        lines = block.split('\n')
        data = [line[5:].lstrip(' ') for line in lines if line.startswith('data:')]
        if not data:
            if any(line and not line.startswith(':') for line in lines):
                return False
            continue
        if terminal is not None:
            return False
        event = json.loads('\n'.join(data), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if not isinstance(event, dict) or not isinstance(event.get('type'), str) or event.get('error'):
            return False
        kind = event['type']
        names = [line[6:].lstrip(' ') for line in lines if line.startswith('event:')]
        if len(names) > 1 or (names and names[0] != kind):
            return False
        if 'response_id' in event:
            if not isinstance(event['response_id'], str) or not event['response_id']:
                return False
            response_ids.add(event['response_id'])
        response = event.get('response')
        if response is not None:
            if not isinstance(response, dict) or not isinstance(response.get('id'), str) or not response['id']:
                return False
            response_ids.add(response['id'])
            output = response.get('output')
            if output is not None and (not isinstance(output, list) or any(
                    not isinstance(item, dict) or item.get('type') != 'reasoning' for item in output)):
                return False
        if len(response_ids) > 1:
            return False
        if kind in {'response.output_item.added', 'response.output_item.done'}:
            if not isinstance(event.get('item'), dict) or event['item'].get('type') != 'reasoning':
                return False
        elif kind in {'response.content_part.added', 'response.content_part.done'}:
            if not isinstance(event.get('part'), dict) or event['part'].get('type') != 'reasoning_text':
                return False
        elif kind == 'response.incomplete':
            terminal = response
        elif kind not in passive:
            # Text/function/custom-tool events, other terminal states and unknown
            # output shapes cannot substantiate a reasoning-only response.
            return False
    if (not terminal or terminal.get('status') != 'incomplete' or terminal.get('error')
            or not isinstance(terminal.get('incomplete_details'), dict)
            or terminal['incomplete_details'].get('reason') != 'max_output_tokens'
            or not isinstance(terminal.get('output'), list)):
        return False
    usage = terminal.get('usage')
    if not isinstance(usage, dict) or not isinstance(recorded_usage, dict):
        return False
    details = usage.get('output_tokens_details')
    if not isinstance(details, dict):
        return False
    tokens, reasoning, inputs = usage.get('output_tokens'), details.get('reasoning_tokens'), usage.get('input_tokens')
    return (type(tokens) is int and type(reasoning) is int and type(inputs) is int
            and tokens > 0 and reasoning == tokens and inputs >= 0
            and type(recorded_usage.get('output_tokens')) is int and recorded_usage['output_tokens'] == tokens
            and type(recorded_usage.get('input_tokens')) is int and recorded_usage['input_tokens'] == inputs)


def _read_reasoning_limit(data_dir, invocation):
    """Open one fixed private complete SSE file, validating its raw-byte digest."""
    identity = invocation['id']
    if str(UUID(identity)) != identity or invocation.get('operation_id') != identity:
        return False
    receipt = invocation.get('response_receipt')
    if (not isinstance(receipt, dict) or receipt.get('media_type') != 'text/event-stream'
            or type(receipt.get('status_code')) is not int or not 200 <= receipt['status_code'] < 300
            or not isinstance(receipt.get('digest'), str)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', receipt['digest'])):
        return False
    data_dir = Path(data_dir)
    if data_dir.is_symlink():
        return False
    data_dir = data_dir.resolve()
    if receipt.get('path') != str(data_dir / 'model_invocations' / (identity + '.sse')):
        return False
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    root = os.open(data_dir, flags)
    directory = file = None
    try:
        directory = os.open('model_invocations', flags, dir_fd=root)
        info = os.fstat(directory)
        if info.st_mode & 0o077 or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
            return False
        file = os.open(identity + '.sse', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                       | getattr(os, 'O_NONBLOCK', 0), dir_fd=directory)
        info = os.fstat(file)
        maximum = 16 * 1024 * 1024
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                or (hasattr(os, 'getuid') and info.st_uid != os.getuid()) or info.st_size > maximum):
            return False
        raw = os.read(file, maximum + 1)
        after = os.fstat(file)
        if (len(raw) != info.st_size or len(raw) > maximum
                or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or 'sha256:' + hashlib.sha256(raw).hexdigest() != receipt['digest']):
            return False
        return _reasoning_only_terminal(raw, invocation.get('usage'))
    finally:
        for descriptor in (file, directory, root):
            if descriptor is not None:
                os.close(descriptor)


async def refine_codex_failure(store, data_dir: Path, attempt_id: str, *, fencing_token: int,
                               input_fingerprint: str, fallback: str | None) -> str | None:
    """Refine a trusted generic truncation using exact, complete proxy evidence.

    No request bodies, raw reasoning, credentials or provider text are returned.
    The caller supplies the frozen task identity. Missing, changing, ambiguous or
    unverified evidence keeps the original closed-set diagnostic unchanged.
    For a historical attempt, take the fence/fingerprint from that attempt or its
    dispatch context, never from a work item that may already be in a newer generation.
    """
    fallback = runtime_failure_code(fallback)
    if fallback != 'model_output_limit':
        return fallback
    try:
        if type(fencing_token) is not int or fencing_token < 1 or not isinstance(input_fingerprint, str):
            return fallback
        attempt = await store.read('attempt', attempt_id)
        context = await store.read('dispatch_context', attempt_id)
        task = (context or {}).get('task', {})
        if (not attempt or type(attempt.get('fencing_token')) is not int or attempt.get('fencing_token') != fencing_token
                or attempt.get('input_fingerprint') != input_fingerprint
                or attempt.get('status') not in {'running', 'completed', 'failed', 'blocked'}
                or any(not isinstance(attempt.get(field), str) or not attempt[field]
                       for field in ('run_id', 'iteration_id', 'work_item_id'))
                or not isinstance(task, dict) or task.get('attempt_id') != attempt_id
                or type(task.get('fencing_token')) is not int or task.get('fencing_token') != fencing_token
                or task.get('input_fingerprint') != input_fingerprint
                or not isinstance(task.get('profile_id'), str) or not task['profile_id']
                or task.get('run_id') != attempt.get('run_id')
                or task.get('iteration_id') != attempt.get('iteration_id')
                or task.get('work_item_id') != attempt.get('work_item_id')):
            return fallback
        calls = [i for i in await store.list('model_invocation') if i.get('attempt_id') == attempt_id]
        if not calls or len(calls) > 2000:
            return fallback
        dated = []
        for invocation in calls:
            if (type(invocation.get('fencing_token')) is not int or invocation.get('fencing_token') != fencing_token
                    or invocation.get('input_fingerprint') != input_fingerprint
                    or invocation.get('run_id') != attempt.get('run_id')
                    or invocation.get('iteration_id') != attempt.get('iteration_id')
                    or invocation.get('profile_id') != task.get('profile_id')
                    or invocation.get('protocol') != 'responses'
                    or invocation.get('state') not in {'settled', 'completed_unpriced', 'released'}):
                return fallback
            created = datetime.fromisoformat(invocation['created_at'])
            if created.tzinfo is None:
                return fallback
            dated.append((created.astimezone(UTC), invocation))
        latest_time = max(time for time, _ in dated)
        latest = [call for time, call in dated if time == latest_time]
        if len(latest) != 1 or latest[0].get('state') not in {'settled', 'completed_unpriced'}:
            return fallback
        if not await asyncio.to_thread(_read_reasoning_limit, data_dir, latest[0]):
            return fallback
        # A new call or concurrent ownership/accounting change invalidates the
        # selection. This helper never reconciles or rewrites the ledger itself.
        current_calls = [i for i in await store.list('model_invocation') if i.get('attempt_id') == attempt_id]
        if (await store.read('attempt', attempt_id) != attempt
                or await store.read('dispatch_context', attempt_id) != context
                or sorted(current_calls, key=lambda i: i['id']) != sorted(calls, key=lambda i: i['id'])):
            return fallback
        return 'reasoning_output_limit'
    except (OSError, ValueError, TypeError, KeyError, NotImplementedError, DomainError):
        return fallback


def _codex_event_evidence(raw):
    events, errors, completed = [], [], False
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            errors.append('invalid_event_line')
            continue
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            errors.append('invalid_event_line')
            continue
        kind = event['type']
        if kind not in CodexEventNormalizer.KNOWN:
            errors.append('unrecognized_event')
        if kind in {'error', 'turn.failed'}:
            errors.append(kind)
            events.append(event)
        completed = completed or kind == 'turn.completed'
    if not completed:
        errors.append('terminal_event_missing')
    return events, errors


def read_codex_failure(data_dir: Path, attempt_id: str, task_schema: dict | None = None) -> str | None:
    """Read bounded fixed Codex evidence; callers must align the current attempt/fence.

    No launch configuration, stderr, caller-supplied evidence path or raw message is
    returned. The optional schema is the frozen task schema, never a relaxed repair.
    """
    try:
        identity = canonical_digest({'attempt_id': attempt_id}).split(':')[1]
        with _private_attempt_directory(data_dir, 'supervisor', identity) as directory:
            try:
                receipt = json.loads(_private_evidence(directory, 'result.json', 65536))
            except FileNotFoundError:
                return 'execution_receipt_missing'
            if (not isinstance(receipt, dict) or receipt.get('attempt_id') != attempt_id
                    or receipt.get('execution_status') not in {'completed', 'failed', 'cancelled', 'execution_unknown'}):
                return None
            try:
                events, errors = _codex_event_evidence(_private_evidence(directory, 'stdout.jsonl', 16 * 1024 * 1024))
            except FileNotFoundError:
                events, errors = [], ['events_missing']
        code = classify_codex_failure(state=receipt['execution_status'], reason=receipt.get('reason'),
            exit_code=receipt.get('exit_code'), events=events)
        if code or receipt['execution_status'] != 'completed':
            return code
        artifact_identity = canonical_digest(attempt_id).split(':')[1]
        if task_schema is None:
            try:
                with _private_attempt_directory(data_dir, 'codex_homes', artifact_identity) as directory:
                    task_schema = json.loads(_private_evidence(directory, 'output_schema.json', 256 * 1024))
            except FileNotFoundError:
                pass
        try:
            with _private_attempt_directory(data_dir, 'attempt_artifacts', artifact_identity) as directory:
                raw = _private_evidence(directory, 'codex_final.json', 2 * 1024 * 1024)
        except FileNotFoundError:
            errors.append('final_output_missing')
        else:
            try:
                parse_codex_final(raw, task_schema)
            except (ValidationError, ValueError, UnicodeError):
                errors.append('final_schema_invalid')
        return classify_codex_failure(state=receipt['execution_status'], exit_code=receipt.get('exit_code'),
                                      events=events, errors=errors)
    except (OSError, ValueError, TypeError, SchemaError, NotImplementedError):
        return None


async def read_frozen_codex_failure(store, data_dir: Path, attempt_id: str, *, run_id: str,
                                   work_item_id: str, fencing_token: int, input_fingerprint: str) -> str | None:
    """Use the identity-matched dispatch schema even after the ephemeral home is gone.

Missing, malformed or changing dispatch records use the original bounded evidence
reader unchanged. This returns only its closed-set diagnostic, never raw output.
"""
    schema = None
    attempt = context = None
    try:
        if (all(isinstance(value, str) and value for value in (attempt_id, run_id, work_item_id, input_fingerprint))
                and type(fencing_token) is int and fencing_token > 0):
            attempt, context = await asyncio.gather(store.read('attempt', attempt_id),
                                                    store.read('dispatch_context', attempt_id))
            task = (context or {}).get('task')
            expected = {'run_id': run_id, 'work_item_id': work_item_id,
                        'fencing_token': fencing_token, 'input_fingerprint': input_fingerprint}
            if (isinstance(attempt, dict) and attempt.get('id') == attempt_id
                    and isinstance(context, dict) and context.get('id') == attempt_id
                    and isinstance(task, dict) and task.get('attempt_id') == attempt_id
                    and type(attempt.get('fencing_token')) is int and type(task.get('fencing_token')) is int
                    and all(attempt.get(key) == value and task.get(key) == value for key, value in expected.items())
                    and isinstance(attempt.get('iteration_id'), str) and attempt['iteration_id']
                    and task.get('iteration_id') == attempt['iteration_id'] and isinstance(task.get('output_schema'), dict)):
                encoded = json.dumps(task['output_schema'], ensure_ascii=False, allow_nan=False).encode()
                if len(encoded) <= 256 * 1024:
                    Draft202012Validator.check_schema(task['output_schema'])
                    schema = json.loads(encoded)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, SchemaError, NotImplementedError, DomainError):
        schema = None
    code = await asyncio.to_thread(read_codex_failure, data_dir, attempt_id, schema)
    if schema is not None:
        try:
            current_attempt, current_context = await asyncio.gather(store.read('attempt', attempt_id),
                                                                    store.read('dispatch_context', attempt_id))
            if current_attempt == attempt and current_context == context:
                return code
        except (OSError, ValueError, TypeError, KeyError, NotImplementedError, DomainError):
            pass
        return await asyncio.to_thread(read_codex_failure, data_dir, attempt_id)
    return code


def read_role_failure(data_dir: Path, attempt_id: str) -> str | None:
    """Read only this attempt's fixed private evidence; never follow supplied paths."""
    descriptors = []
    try:
        directory_flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        root = os.open(Path(data_dir) / 'attempt_artifacts', directory_flags)
        descriptors.append(root)
        attempt = os.open(canonical_digest(attempt_id).split(':')[1], directory_flags, dir_fd=root)
        descriptors.append(attempt)
        file = os.open('role_error.json', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                       | getattr(os, 'O_NONBLOCK', 0), dir_fd=attempt)
        descriptors.append(file)
        info = os.fstat(file)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 65536 or info.st_nlink != 1
                or info.st_mode & 0o077 or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
            return None
        raw = os.read(file, 65537)
        if len(raw) > 65536:
            return None
        return classify_role_error(json.loads(raw))
    except (OSError, ValueError, TypeError, NotImplementedError):
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_role_failure_diagnostic(data_dir: Path, attempt_id: str):
    """Retain structured planning issues emitted by the controlled role worker."""
    try:
        with _private_attempt_directory(data_dir, 'attempt_artifacts', canonical_digest(attempt_id).split(':')[1]) as directory:
            value = json.loads(_private_evidence(directory, 'role_error.json', 1024 * 1024))
        if classify_role_error(value) == 'planning_validation_failed':
            return {'code': 'planning_validation_failed', 'message': value['message'], 'details': value['failure_details']}
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None
