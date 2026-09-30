"""Owner-only execution defaults, saved without changing running authorizations."""
from __future__ import annotations

import copy
import hashlib
import math
import os
import stat
import tomllib
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.configuration import Configuration, ProductDefaults, _plain, _toml_document, _write
from agentflow.settings import Settings

MAX_SAFE_INTEGER = 2**53 - 1
# Bounds and defaults come from the same models that validate the TOML file.
EXECUTION_FIELDS = (
    ('product.max_model_requests', '每次运行的模型请求额度', '次', 'common',
     '重启后创建的新运行；现有运行的请求额度与累计用量不变。',
     '应用层对整次产品运行的请求计数，不是模型的输出长度。', '不限制请求次数'),
    ('product.max_tool_calls', 'Agent 工作的工具调用额度', '次', 'common',
     '重启后创建的新运行；已有运行保持已授权额度，编码工作可在执行台单独追加。',
     '编码工作按多次尝试累计计数，重试不清零；超时续跑可按已启用策略追加一份原额度。专业角色按单次尝试限制。', '不允许工具调用'),
    ('product.max_active_seconds', 'Agent 工作的执行时长额度', '秒', 'common',
     '重启后创建的新运行；已有运行保持已授权额度，编码工作可在执行台单独追加。',
     '编码工作累计执行时长；超时续跑可按已启用策略追加一份原额度。专业角色按单次尝试限制；不包括排队时间。', None),
    ('app.agent_concurrency', '同时工作的 Agent 数量', '个', 'common',
     '重启后的调度，包括现有运行中尚未派发的工作。',
     '控制平台同时派发的 Agent 数量。', None),
    ('app.max_coding_steps', '每个编码工作的编码小步额度', '步', 'advanced',
     '重启后首次建立执行额度的新编码工作；已有工作的小步额度与累计用量不变。',
     '限制同一编码工作推进的编码小步数，需更多小步时可在执行台追加。', None),
    ('app.max_role_iterations', '每次专业角色执行的循环上限', '轮', 'advanced',
     '重启后新派发的分析、规划、审查等角色尝试；已派发尝试的授权不变。',
     '应用层 Agent 循环次数；每次模型输出长度与推理参数仍取自模型配置。', None),
    ('app.auto_review_repair_limit', '自动审查修复次数', '次', 'advanced',
     '重启后的审查修复调度；现有修复记录与累计次数保留。',
     '-1 表示持续返工直到审查通过；0 关闭，正整数限制返工次数。每轮仍受原执行额度、权限、人工审批与发布门禁约束，历史用量保留。', '关闭自动审查修复'),
    ('app.auto_test_repair_limit', '测试失败自动修复次数', '次', 'advanced',
     '重启后的测试失败修复调度；现有产品的修复记录与累计次数保留。',
     '-1 表示持续修复直到测试通过；0 关闭，正整数限制修复次数。只修复原授权产品源码，保留冻结测试、计划、模型、预算与人工审批门禁。', '关闭测试自动修复'),
    ('app.auto_failure_retry_limit', '每个工作自动恢复次数', '次', 'advanced',
     '重启后的失败恢复调度；现有工作的累计恢复次数保留。',
     '功能实现、单元和集成测试编写及各类审查的执行失败共用此策略；仅对可安全重试的失败自动恢复，保存设置不会立即重试。', '关闭单个工作的自动恢复'),
    ('app.auto_failure_run_limit', '每次运行自动恢复总次数', '次', 'advanced',
     '重启后的失败恢复调度；现有运行的累计恢复次数保留。',
     '同一产品运行内执行失败的自动恢复次数合计；审查质量返工单独计数。', '关闭整次运行的自动恢复'),
    ('app.auto_timeout_retry_limit', '超时自动续跑次数', '次', 'advanced',
     '重启后生效，包括现有运行中尚未恢复的超时任务；同时受单个工作和整轮自动恢复次数限制。',
     '确认原进程停止并保留进度后，为下一次重试补足原单次执行额度。历史用量不清零；按调用次数计费时保留超时断连的未知用量并继续。严格金额预算仍需核验费用。',
     '关闭超时自动续跑'),
    ('app.auto_failure_retry_delay_seconds', '自动恢复等待时间', '秒', 'advanced',
     '重启后的失败恢复调度；不会改变已经发生的执行记录。',
     '失败后等待多久才允许调度下一次自动恢复。', '不额外等待'),
    ('app.node_active_seconds', '节点单次执行时长上限', '秒', 'advanced',
     '重启后新派发到执行节点的任务；已经派发的节点任务授权不变。',
     '节点执行一次任务的时长上限，和编码工作的累计时长额度分别生效。', None),
    ('app.agent_max_log_bytes', 'Agent 进程日志大小上限', '字节', 'advanced',
     '重启后新派发的 Agent 尝试；已有尝试保留原上限。',
     '超出时停止该进程；这是运行日志容量，和模型单次输出长度不同。', None),
    ('app.agent_startup_timeout_seconds', 'Agent 启动握手等待时间', '秒', 'advanced',
     '重启后新启动进程使用；已经开始的启动握手保留原截止时间。',
     '控制端和启动器共用一次启动窗口，等待启动身份确认；独立于工作执行时长、工具额度和模型请求等待。启动失败不能算作代码或测试失败。', None),
    ('app.controller_startup_timeout_seconds', '控制器启动等待时间', '秒', 'advanced',
     '下一次 CLI 启动控制器时读取；不改变当前 Agent、模型或测试任务。',
     '等待本机控制器建立私有连接通道；进程退出立即报告失败，轮询不会延长本次等待期限。', '不限等待时间'),
    ('app.isolation_probe_timeout_seconds', '执行隔离检查等待时间', '秒', 'advanced',
     '重启后新执行的隔离探针。',
     '每项执行隔离检查的等待时间，检查未通过时不会启动 Agent。', None),
    ('app.node_output_limit_bytes', '节点作业输出容量', '字节', 'advanced',
     '重启后新派发的执行节点作业。',
     '限制节点命令输出与相关日志大小，不改变模型响应额度。', None),
    ('app.research_public_web_enabled', '公开网页调研', '开关', 'advanced',
     '重启后用于新派发的调研任务；开启不会扩大旧任务授权，关闭会阻止公开读取。',
     '允许调研角色读取公开 HTTP(S) 来源；不开放内网、登录凭据或代码执行。可在固定文件用 research_web_hosts 限制来源域名。', None),
    ('app.package_fetch_timeout_seconds', '依赖下载无进展超时', '秒', 'advanced',
     '重启后新启动的本机依赖安装命令。',
     '用于 npm 请求等待和包源代理的数据空闲截止；发送背压仍受节点作业总时长约束。', None),
)


class ExecutionSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    expected_configuration_revision: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    values: dict[str, bool | int | float] = Field(min_length=1, max_length=len(EXECUTION_FIELDS))


def _revision(configuration):
    return 'sha256:' + configuration._source_digest


def _schema(key):
    section, name = key.split('.')
    model = ProductDefaults if section == 'product' else Settings
    schema = dict(model.model_json_schema()['properties'][name])
    if schema['type'] != 'boolean':
        schema['maximum'] = min(schema.get('maximum', MAX_SAFE_INTEGER), MAX_SAFE_INTEGER)
    return schema


def _get(configuration, key):
    section, name = key.split('.')
    return getattr(configuration.product if section == 'product' else configuration.settings, name)


def _source_section(document, section):
    return 'settings' if section == 'app' and 'app' not in document and 'settings' in document else section


def _restart_required(loaded, saved):
    values = [_plain(loaded), _plain(saved)]
    for value in values:
        for role in ('roles', 'coding'):
            value['models'][role].pop('max_output_tokens', None)
    return canonical_digest(values[0]) != canonical_digest(values[1])


def execution_settings_view(configuration, saved=None):
    saved = saved or configuration.reload()
    fields, saved_values, loaded_values = [], {}, {}
    for key, label, unit, group, scope, description, zero in EXECUTION_FIELDS:
        schema = _schema(key)
        section, name = key.split('.')
        source_section = _source_section(saved._source_document, section)
        current, loaded = _get(saved, key), _get(configuration, key)
        saved_values[key], loaded_values[key] = current, loaded
        fields.append({'key': key, 'label': label, 'unit': unit, 'group': group,
            'integer': schema['type'] == 'integer', 'boolean': schema['type'] == 'boolean', 'minimum': schema.get('minimum'),
            'maximum': schema.get('maximum'), 'exclusive_minimum': schema.get('exclusiveMinimum'),
            'default_value': schema['default'], 'default_source': 'application_default',
            'source': 'configuration_file' if name in saved._source_document.get(source_section, {}) else 'application_default',
            'saved_value': current, 'loaded_value': loaded, 'restart_required': current != loaded,
            'restart_on_change': True, 'effect_scope': scope, 'description': description, 'zero_meaning': zero})
    return {'configuration_path': str(configuration.config_path), 'configuration_revision': _revision(saved),
        'restart_required': _restart_required(configuration, saved), 'saved_values': saved_values,
        'loaded_values': loaded_values, 'fields': fields, 'current_runs_changed': False,
        'model_parameters': {'source': 'model_configuration',
            'description': '模型名称、输出长度与推理参数以模型配置为准；此处只读显示当前文件，不修改已派发任务的授权。',
            **{role: {'source': 'models.' + role, 'model': getattr(saved.models, role).model,
                      'max_output_tokens': getattr(saved.models, role).max_output_tokens,
                      'configured': getattr(saved.models, role).enabled} for role in ('roles', 'coding')}}}


def _validated_request(payload):
    request = ExecutionSettingsRequest.model_validate(payload)
    allowed = {row[0] for row in EXECUTION_FIELDS}
    if set(request.values) - allowed:
        raise DomainError('invalid_request', '执行设置仅接受页面列出的应用参数。', 422)
    for key, value in request.values.items():
        schema = _schema(key)
        if schema['type'] == 'boolean':
            if type(value) is not bool:
                raise DomainError('invalid_request', '公开调研开关必须为开启或关闭。', 422)
            continue
        valid = (type(value) is int if schema['type'] == 'integer' else type(value) in {int, float})
        if (not valid or abs(value) > MAX_SAFE_INTEGER or not math.isfinite(value)
                or ('minimum' in schema and value < schema['minimum'])
                or ('exclusiveMinimum' in schema and value <= schema['exclusiveMinimum'])
                or ('maximum' in schema and value > schema['maximum'])):
            raise DomainError('invalid_request', '执行设置的数值超出允许范围，请按页面提示填写。', 422)
    return request


def _target(configuration, disk, values):
    document = copy.deepcopy(disk._source_document)
    for key, value in values.items():
        section, name = key.split('.')
        section = _source_section(document, section)
        document.setdefault(section, {})[name] = value
    content = _toml_document(document)
    saved = Configuration.model_validate(tomllib.loads(content))
    saved._config_path = configuration.config_path
    saved._source_document = tomllib.loads(content)
    saved._source_digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    return content, saved


def _unconfirmed():
    return DomainError('execution_settings_save_unconfirmed',
        '当前配置又发生变化，无法确认上次保存是否完成。当前文件已保留，请刷新核对后重新保存。',
        409, {'save_state': 'unconfirmed', 'requires_refresh': True})


def _confirm_target(path, expected_digest):
    """Finish durability if an earlier replacement lost its completion receipt."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise _unconfirmed()
            if hashlib.sha256(stream.read(1024 * 1024 + 1)).hexdigest() != expected_digest:
                raise _unconfirmed()
            os.fsync(stream.fileno())
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError:
        raise DomainError('configuration_unwritable', '无法完成配置文件的持久化确认，请重试原保存请求。', 409) from None


async def update_execution_settings(configuration, payload, key, store):
    """A prepared receipt makes a file write recoverable without storing secrets.

    After a lost acknowledgment, a completed request returns its original receipt.
    A pending request may only write its original base version or recognize its
    exact target version. A third version is never overwritten during recovery.
    The caller holds Configuration's lock shared with model-setting writes.
    """
    request = _validated_request(payload)
    command = request.model_dump()
    identity = str(uuid5(NAMESPACE_URL, 'agentflow:execution-settings:' + key))
    current = await store.read('configuration_execution_update', identity)
    if current is None:
        disk = configuration.reload()
        if _revision(disk) != request.expected_configuration_revision:
            raise DomainError('configuration_changed', '配置文件已被其他操作修改，请刷新后核对再保存。', 409)
        _, target = _target(configuration, disk, request.values)
        result = execution_settings_view(configuration, target)
        result.update(operation_id=identity, saved_at=utc_now())
        prepared = {'state': 'pending', 'base_revision': _revision(disk),
                    'target_revision': _revision(target), 'values': request.values, 'result': result}
    else:
        prepared = None

    def prepare(tx):
        return tx.put('configuration_execution_update', identity, prepared)
    await store.command('configuration.execution.prepare', key, command, prepare)
    current = await store.read('configuration_execution_update', identity)
    if current['state'] == 'completed':
        return current['result']

    disk = configuration.reload()
    if _revision(disk) == current['base_revision']:
        content, target = _target(configuration, disk, request.values)
        if _revision(target) != current['target_revision']:
            raise DomainError('configuration_changed', '待确认配置的内容已变化，请刷新后核对再保存。', 409)
        _write(configuration.config_path, content, expected_digest=disk._source_digest)
    elif _revision(disk) == current['target_revision']:
        _confirm_target(configuration.config_path, disk._source_digest)
    else:
        raise _unconfirmed()

    def complete(tx):
        operation = tx.get('configuration_execution_update', identity)
        tx.put('configuration_execution_update', identity, {**operation, 'state': 'completed'}, operation['revision'])
        tx.event('configuration.execution_saved', {'operation_id': identity,
            'changed_fields': sorted(request.values), 'restart_required': operation['result']['restart_required']})
        return operation['result']
    return await store.command('configuration.execution.complete', key, command, complete)
