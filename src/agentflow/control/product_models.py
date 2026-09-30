"""Small product-oriented commands over the durable engineering workflow."""
from __future__ import annotations

from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

ProductTarget = Literal['web', 'api', 'ios', 'android', 'macos', 'windows', 'linux']
ProductLanguage = Literal['zh-CN', 'en']
DEFAULT_PRODUCT_LANGUAGE: ProductLanguage = 'zh-CN'
TARGET_ORDER = ('web', 'api', 'ios', 'android', 'macos', 'windows', 'linux')


def product_identity(key: str) -> str:
    return str(uuid5(NAMESPACE_URL, 'agentflow:product:' + key))


class ModelSetupRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    role: Literal['roles', 'coding', 'both']
    provider: Literal['deepseek', 'openai_compatible']
    base_url: str = Field(min_length=8, max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    api_key: SecretStr | None = None
    credential_env: str | None = None
    max_output_tokens: int = Field(default=8192, ge=1024)

    @model_validator(mode='after')
    def exactly_one_secret(self):
        if bool(self.api_key and self.api_key.get_secret_value()) == bool(self.credential_env):
            raise ValueError('Supply an API key or one environment variable name')
        if self.credential_env and not self.credential_env.isidentifier():
            raise ValueError('Credential environment variable must be a literal variable name')
        return self


class ProductRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    goal: str = Field(min_length=8, max_length=16000)
    output_directory: str | None = Field(default=None, max_length=2048)
    target: ProductTarget = 'web'
    targets: list[ProductTarget] | None = Field(default=None, min_length=1, max_length=7)
    creation_mode: Literal['new', 'import'] = 'new'
    project_path: str | None = Field(default=None, max_length=2048)
    review_mode: Literal['auto', 'milestones', 'every_step'] = 'auto'
    language: ProductLanguage = DEFAULT_PRODUCT_LANGUAGE
    max_model_requests: int = Field(default=200, ge=0, le=2000, strict=True)
    role_model_profile_id: str | None = None
    coding_model_profile_id: str | None = None

    @field_validator('targets')
    @classmethod
    def distinct_targets(cls, value):
        if value is not None:
            if len(value) != len(set(value)):
                raise ValueError('Product targets must be unique')
            return [target for target in TARGET_ORDER if target in value]
        return value

    @model_validator(mode='after')
    def consistent_creation(self):
        if self.targets and 'target' in self.model_fields_set and self.target not in self.targets:
            raise ValueError('Legacy target must be included in targets')
        if (self.creation_mode == 'import') != bool(self.project_path):
            raise ValueError('Imported products require project_path; new products must omit it')
        return self


class ProductDiagnosisRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    project_path: str = Field(min_length=1, max_length=2048)


class ProductChangeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str | None = Field(default=None, min_length=1, max_length=100)
    description: str = Field(min_length=8, max_length=16000)
    acceptance_criteria: str | None = Field(default=None, max_length=16000)
    expected_revision: int = Field(ge=1)
    review_mode: Literal['auto', 'milestones', 'every_step'] | None = None
    language: ProductLanguage | None = None


class ProductLanguageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    language: ProductLanguage
    expected_revision: int = Field(ge=1)


class ProductManagementRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    expected_revision: int = Field(ge=1, strict=True)
    reason: str = Field(default='用户管理产品', min_length=1, max_length=2000)


class ProductTestRuntimeRepairRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    expected_run_revision: int = Field(ge=1)
    candidate_id: str = Field(min_length=1, max_length=200)
    failed_job_id: str = Field(min_length=1, max_length=200)
    replace_repair_id: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class ProductUpdateRequest(ProductManagementRequest):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    goal: str | None = Field(default=None, min_length=8, max_length=16000)
    targets: list[ProductTarget] | None = Field(default=None, min_length=1, max_length=7)
    language: ProductLanguage | None = None
    review_mode: Literal['auto', 'milestones', 'every_step'] | None = None
    max_model_requests: int | None = Field(default=None, ge=0, le=2000, strict=True)

    @field_validator('targets')
    @classmethod
    def distinct_targets(cls, value):
        return ProductRequest.distinct_targets(value)

    @model_validator(mode='after')
    def meaningful_update(self):
        fields = self.model_fields_set - {'expected_revision', 'reason'}
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError('Provide at least one non-null product setting')
        return self
