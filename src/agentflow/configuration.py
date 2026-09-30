"""The owner's single private TOML file and its offline model configuration.

Loading is side-effect free unless ``create=True`` is explicit. A configuration
instance remembers its original path, including when a caller later changes HOME.
Models are immutable, content-addressed registrations. New attempts on the same
model route inherit the file's current output allowance through a new profile;
previously dispatched attempts retain their original authorization.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from agentflow.common import DomainError, canonical_digest
from agentflow.control.product_models import DEFAULT_PRODUCT_LANGUAGE, ModelSetupRequest, ProductLanguage
from agentflow.models.profiles import ModelProfile, ModelRegistry, PricingPolicy
from agentflow.models.secrets import LocalSecretStore
from agentflow.runtime.contracts import ReasoningPolicy
from agentflow.settings import Settings

_MAX_CONFIG_BYTES = 1024 * 1024
_ROLE_BINDINGS = {'roles': 'role_model_profile_id', 'coding': 'coding_model_profile_id'}


def configuration_path() -> Path:
    """There are no working-directory, environment-variable, or CLI overrides."""
    return Path.home() / '.config' / 'agentflow' / 'config.toml'


class ProductDefaults(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, validate_default=True)
    target: Literal['web', 'api'] = 'web'
    review_mode: Literal['auto', 'milestones', 'every_step'] = 'auto'
    language: ProductLanguage = DEFAULT_PRODUCT_LANGUAGE
    max_model_requests: int = Field(default=200, ge=0, le=2000, strict=True)
    max_active_seconds: float = Field(default=1800, gt=0, le=86400)
    max_tool_calls: int = Field(default=100, ge=0)
    output_root: Path = Field(default_factory=lambda: Path.home() / 'AgentFlowProducts')

    @field_validator('output_root')
    @classmethod
    def absolute_output_root(cls, value: Path) -> Path:
        value = value.expanduser()
        if not value.is_absolute():
            raise ValueError('output_root must be absolute')
        return value.resolve()


class ModelConfiguration(ReasoningPolicy):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True, validate_default=True)
    provider: Literal['deepseek', 'openai_compatible', 'local_test'] = 'openai_compatible'
    base_url: str = Field(default='', max_length=2048)
    model: str = Field(default='', max_length=256)
    api_key: SecretStr | None = Field(default=None, repr=False)
    api_key_env: str | None = None
    max_output_tokens: int = Field(default=8192, ge=1)
    provider_documented_version: str | None = None
    pricing: PricingPolicy | None = None
    max_request_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    max_response_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    request_timeout_seconds: float = Field(default=120, gt=0, le=3600)
    allow_loopback_upstream: bool = False
    allowed_tool_names: list[str] | None = None

    @field_validator('api_key_env', mode='before')
    @classmethod
    def empty_environment_is_unset(cls, value):
        return None if value == '' else value

    @model_validator(mode='after')
    def explicit_complete_model(self):
        if not self.model and not self.base_url:
            if self.api_key is not None or self.api_key_env:
                raise ValueError('A credential requires an explicit model and base_url')
            return self
        if not self.model.strip() or not self.base_url or self.model != self.model.strip():
            raise ValueError('Set both base_url and an explicit model')
        if (self.api_key is not None) == bool(self.api_key_env):
            raise ValueError('Set exactly one of api_key and api_key_env')
        if self.api_key_env and not self.api_key_env.isidentifier():
            raise ValueError('api_key_env must be a literal environment variable name')
        if self.api_key is not None:
            value = self.api_key.get_secret_value()
            if not value.strip() or value != value.strip() or len(value) > 8192 or any(c in value for c in '\x00\n\r'):
                raise ValueError('api_key must be a nonempty single line')
        # Reuse the actual provider policy without contacting the provider.
        self.profile('validation', ['chat_completions'])
        return self

    @property
    def enabled(self) -> bool:
        return bool(self.model and self.base_url)

    def profile(self, identity: str, protocols: list[str]) -> ModelProfile:
        values = self.model_dump(exclude={'api_key', 'api_key_env', 'model'})
        return ModelProfile(**values, model_profile_id=identity, requested_model=self.model,
            accepted_api_model=self.model, acceptance_status='accepted', protocols=protocols,
            credential_reference='local:' + identity if self.api_key is not None else 'env:' + (self.api_key_env or ''))

    @property
    def fingerprint(self) -> str:
        return canonical_digest(_plain(self))


class ModelConfigurations(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    roles: ModelConfiguration = Field(default_factory=ModelConfiguration)
    coding: ModelConfiguration = Field(default_factory=ModelConfiguration)


class Configuration(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, populate_by_name=True, hide_input_in_errors=True)
    settings: Settings = Field(default_factory=Settings, alias='app')
    product: ProductDefaults = Field(default_factory=ProductDefaults)
    models: ModelConfigurations = Field(default_factory=ModelConfigurations)
    _config_path: Path = PrivateAttr(default_factory=configuration_path)
    _source_digest: str | None = PrivateAttr(default=None)
    _source_document: dict = PrivateAttr(default_factory=dict)
    _update_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

    @model_validator(mode='before')
    @classmethod
    def executor_defaults(cls, raw):
        if not isinstance(raw, dict):
            return raw
        raw = dict(raw)
        name = 'app' if 'app' in raw else 'settings'
        app = raw.get(name)
        if not isinstance(app, dict):
            return raw
        app = dict(app)
        for field in ('tls_certificate', 'tls_private_key', 'tls_client_ca', 'dashboard_dir'):
            if app.get(field) is not None:
                app[field] = str(Path(app[field]).expanduser())
        if app.get('executor_host'):
            data = Path(app.get('data_dir', Settings().data_dir)).expanduser()
            pki = data / 'nodes' / 'pki'
            for field, filename in [('tls_certificate', 'server.pem'), ('tls_private_key', 'server.key'),
                                    ('tls_client_ca', 'ca.pem')]:
                app.setdefault(field, str(pki / filename))
            host = app['executor_host']
            host = f'[{host}]' if ':' in host else host
            app.setdefault('executor_origin', f"https://{host}:{app.get('executor_port', 9443)}")
        raw[name] = app
        return raw

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def fingerprint(self) -> str:
        """A semantic digest, never a public serialization of API credentials."""
        return canonical_digest(_plain(self))

    def reload(self) -> Configuration:
        return _load(self.config_path, create=False)

    def restart_required(self) -> bool:
        """Output allowances alone are read at dispatch and need no restart."""
        loaded, saved = _plain(self), _plain(self.reload())
        for values in (loaded, saved):
            for role in _ROLE_BINDINGS:
                values['models'][role].pop('max_output_tokens')
        return canonical_digest(loaded) != canonical_digest(saved)

    async def apply_models(self, registry: ModelRegistry, secrets: LocalSecretStore) -> dict:
        return await apply_models(self, registry, secrets)

    async def resolve_output_profile(self, registry: ModelRegistry, profile: ModelProfile,
                                     *, role: Literal['roles', 'coding']) -> tuple[ModelProfile, dict]:
        """Freeze the current same-route output allowance for one new dispatch.

        Only this allowance follows manual file edits immediately. Model routing,
        credentials, reasoning and pricing remain the run/owner's explicit choice.
        Never revise an existing profile or authorization in place.
        """
        configured = getattr(self.reload().models, role)
        evidence = {'source': 'runtime_binding', 'role': role,
                    'base_profile_id': profile.model_profile_id, 'base_profile_revision': profile.revision,
                    'max_output_tokens': profile.max_output_tokens}
        if (not configured.enabled or configured.provider != profile.provider
                or configured.base_url.rstrip('/') != profile.base_url
                or configured.model != profile.requested_model
                or configured.model != profile.accepted_api_model):
            return profile, evidence
        evidence.update(source='configuration_file', max_output_tokens=configured.max_output_tokens)
        if configured.max_output_tokens == profile.max_output_tokens:
            return profile, evidence
        values = profile.model_dump(exclude={'model_profile_id', 'revision'})
        values['max_output_tokens'] = configured.max_output_tokens
        identity = str(uuid5(NAMESPACE_URL, 'agentflow:output-profile:' + canonical_digest(values)))
        effective = ModelProfile(model_profile_id=identity, **values)
        await registry.register(effective, 'output-profile:' + identity)
        registered = await registry.get(identity)
        if registered != effective:
            raise DomainError('configuration_model_conflict', '单次输出额度档案已变化，请检查模型配置记录。', 409)
        return registered, evidence

    async def update_model(self, request: ModelSetupRequest, key: str,
                           registry: ModelRegistry, secrets: LocalSecretStore) -> dict:
        """Persist only model edits, then immediately apply them to this instance.

        The file is authoritative: a crash after its atomic replacement is repaired
        by startup's offline ``apply_models``. App/product edits are preserved on
        disk, but the running instance retains its original app/product settings.
        """
        async with self._update_lock:
            return await _update_model(self, request, key, registry, secrets)

    def execution_settings(self) -> dict:
        from agentflow.configuration_execution import execution_settings_view
        return execution_settings_view(self)

    async def update_execution_settings(self, payload: dict, key: str, store) -> dict:
        from agentflow.configuration_execution import update_execution_settings
        async with self._update_lock:
            return await update_execution_settings(self, payload, key, store)


def _plain(value):
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, BaseModel):
        return {field.alias or name: _plain(getattr(value, name))
                for name, field in type(value).model_fields.items()
                if not (name == 'reasoning_effort' and getattr(value, name) is None)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _safe_validation_error(error: ValidationError, path: Path) -> DomainError:
    known = set(Settings.model_fields) | set(ProductDefaults.model_fields) | set(ModelConfiguration.model_fields)
    known |= set(PricingPolicy.model_fields) | {'app', 'settings', 'product', 'models', 'roles', 'coding'}
    fields = sorted({'.'.join(str(part) if part in known else '<unknown>' for part in item['loc']) or '<root>'
                     for item in error.errors(include_url=False, include_context=False, include_input=False)})
    return DomainError('configuration_invalid', f'配置文件字段无效：{path}（{", ".join(fields)}）', 422)


def _private_directory(path: Path):
    if path.is_symlink():
        raise DomainError('configuration_unsafe', '配置目录不能是符号链接', 422)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    except OSError:
        raise DomainError('configuration_unwritable', '无法创建私有配置目录', 422) from None


def _read(path: Path) -> bytes:
    if path.parent.is_symlink():
        raise DomainError('configuration_unsafe', '配置目录不能是符号链接', 422)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CONFIG_BYTES:
                raise DomainError('configuration_unsafe', '配置文件必须是小于 1 MiB 的普通文件', 422)
            if os.name != 'nt' and info.st_mode & 0o077:
                raise DomainError('configuration_private_required', f'配置文件需仅当前用户可读写：chmod 600 "{path}"', 422)
            data = stream.read(_MAX_CONFIG_BYTES + 1)
            if len(data) > _MAX_CONFIG_BYTES:
                raise DomainError('configuration_unsafe', '配置文件必须小于 1 MiB', 422)
            return data
    except FileNotFoundError:
        raise DomainError('configuration_missing', f'尚未创建配置文件：{path}；请先运行 agentflow start', 409) from None
    except OSError:
        raise DomainError('configuration_unreadable', f'无法读取配置文件：{path}', 422) from None


def _load(path: Path, *, create: bool) -> Configuration:
    if create and not path.exists() and not path.is_symlink():
        _private_directory(path.parent)
        _write(path, _template(), expected_digest=None)
    raw = _read(path)
    try:
        document = tomllib.loads(raw.decode('utf-8'))
        configuration = Configuration.model_validate(document)
    except (tomllib.TOMLDecodeError, UnicodeError):
        raise DomainError('configuration_invalid', f'TOML 配置格式无效：{path}', 422) from None
    except ValidationError as error:
        raise _safe_validation_error(error, path) from None
    except (TypeError, ValueError):
        raise DomainError('configuration_invalid', f'配置文件字段类型无效：{path}', 422) from None
    configuration._config_path = path
    configuration._source_digest = hashlib.sha256(raw).hexdigest()
    configuration._source_document = document
    return configuration


def load_configuration(*, create: bool = False) -> Configuration:
    return _load(configuration_path(), create=create)


def _value(value) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '[' + ', '.join(_value(item) for item in value) + ']'
    raise TypeError('Unsupported configuration value')


def _toml(configuration: Configuration) -> str:
    return _toml_document(_plain(configuration))


def _toml_document(document: dict) -> str:
    result = ['# AgentFlow 私有配置；手工修改后重新启动生效。',
              '# API Key 也可由模型设置页面保存；请保持此文件权限为 0600。', '']
    def section(name, values):
        result.append('[' + name + ']')
        for key, value in values.items():
            if value is not None and not isinstance(value, dict):
                result.append(f'{key} = {_value(value)}')
        result.append('')
        for key, value in values.items():
            if isinstance(value, dict):
                section(name + '.' + key, value)
    for name, values in document.items():
        if name == 'models':
            for role, model in values.items():
                if model.get('api_key') is None and model.get('api_key_env') is None:
                    model['api_key_env'] = ''
                section('models.' + role, model)
        else:
            section(name, values)
    return '\n'.join(result)


def _template() -> str:
    return _toml(Configuration())


def _write(path: Path, content: str, *, expected_digest: str | None):
    """Atomic private replacement; concurrent manual edits are not overwritten."""
    _private_directory(path.parent)
    temp_path = None
    try:
        fd, filename = tempfile.mkstemp(prefix='.config-', suffix='.tmp', dir=path.parent)
        temp_path = Path(filename)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if expected_digest is None:
            try:
                # Creation cannot overwrite a template another process just wrote.
                os.link(temp_path, path)
            except FileExistsError:
                return
        else:
            if hashlib.sha256(_read(path)).hexdigest() != expected_digest:
                raise DomainError('configuration_changed', '配置文件已被修改，请重新操作', 409)
            os.replace(temp_path, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError:
        raise DomainError('configuration_unwritable', f'无法保存配置文件：{path}', 422) from None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


async def apply_models(configuration: Configuration, registry: ModelRegistry, secrets: LocalSecretStore) -> dict:
    """Apply complete sections only; an empty template preserves legacy defaults."""
    groups = {}
    for role in _ROLE_BINDINGS:
        model = getattr(configuration.models, role)
        if model.enabled:
            group = groups.setdefault(model.fingerprint, {'model': model, 'roles': []})
            group['roles'].append(role)
    selected = {}
    for group in groups.values():
        model, roles = group['model'], group['roles']
        protocols = [protocol for role, protocol in [('roles', 'chat_completions'), ('coding', 'responses')]
                     if role in roles]
        identity = str(uuid5(NAMESPACE_URL, 'agentflow:file-model:' + canonical_digest({
            'configuration': model.fingerprint, 'protocols': protocols})))
        profile = model.profile(identity, protocols)
        if model.api_key is not None:
            secrets.put(identity, model.api_key.get_secret_value())
        current = await registry.store.read('model_profile', identity)
        if current is None:
            await registry.register(profile, 'file-model:' + identity)
        elif {name: current.get(name) for name in ModelProfile.model_fields if name != 'revision'
              and not (name == 'reasoning_effort' and current.get(name) is None)} != profile.model_dump(exclude={'revision'}):
            raise DomainError('configuration_model_conflict', '配置模型记录与私有配置不一致', 409)
        selected.update({_ROLE_BINDINGS[role]: identity for role in roles})

    def bind(tx):
        previous = tx.get('product_model_binding', 'default')
        if not selected or all(previous and previous.get(name) == value for name, value in selected.items()):
            return previous or {}
        value = tx.put('product_model_binding', 'default', {**(previous or {}), **selected},
                       previous['revision'] if previous else None)
        tx.event('product.models_configured', {'source': 'configuration_file', 'profile_ids': sorted(set(selected.values()))})
        return value
    bindings = await registry.store.command('configuration.model.bind', str(uuid4()), selected, bind)
    return {'configured': bool(selected), 'bindings': bindings, 'configuration_fingerprint': configuration.fingerprint}


async def _update_model(configuration, request, key, registry, secrets):
    if not isinstance(request, ModelSetupRequest):
        raise TypeError('update_model requires a validated ModelSetupRequest')
    values = request.model_dump(exclude={'role', 'credential_env'})
    values['api_key_env'] = request.credential_env
    try:
        model = ModelConfiguration.model_validate(values)
    except ValidationError as error:
        raise _safe_validation_error(error, configuration.config_path) from None
    if model.api_key_env:
        secrets.read('env:' + model.api_key_env)
    roles = ['roles', 'coding'] if request.role == 'both' else [request.role]
    disk = configuration.reload()
    safe_payload = {'role': request.role, 'model_fingerprint': model.fingerprint}
    operation_id = str(uuid5(NAMESPACE_URL, 'agentflow:configuration-update:' + key))
    def prepare(tx):
        return tx.put('configuration_model_update', operation_id, {'state': 'pending',
            'base_models': {role: getattr(disk.models, role).fingerprint for role in roles},
            'target_model': model.fingerprint})
    operation = await registry.store.command('configuration.model.prepare', key, safe_payload, prepare)
    current = await registry.store.read('configuration_model_update', operation_id)
    if current and current['state'] == 'completed':
        return current['result']
    disk = configuration.reload()
    if any(getattr(disk.models, role).fingerprint not in {operation['base_models'][role], model.fingerprint}
           for role in roles):
        raise DomainError('configuration_changed', '模型配置已被其他操作修改，请重新操作', 409)
    document = copy.deepcopy(disk._source_document)
    document.setdefault('models', {}).update({role: _plain(model) for role in roles})
    _write(configuration.config_path, _toml_document(document), expected_digest=disk._source_digest)
    saved = configuration.reload()
    try:
        applied = await apply_models(saved, registry, secrets)
    except Exception:
        raise DomainError('configuration_apply_pending', '模型配置已保存；应用尚未完成，请重启 AgentFlow 或重试本次保存', 409) from None
    object.__setattr__(configuration, 'models', saved.models)
    configuration._source_digest = saved._source_digest
    restart = configuration.fingerprint != saved.fingerprint
    result = {**applied, 'profile_id': applied['bindings'][_ROLE_BINDINGS[roles[0]]],
              'configuration_fingerprint': configuration.fingerprint, 'restart_required': restart}
    def complete(tx):
        operation = tx.get('configuration_model_update', operation_id)
        tx.put('configuration_model_update', operation_id, {**operation, 'state': 'completed', 'result': result},
               operation['revision'])
        return result
    return await registry.store.command('configuration.model.complete', key, safe_payload, complete)
