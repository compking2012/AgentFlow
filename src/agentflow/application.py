"""Composition root for the one-writer local application."""

from __future__ import annotations

import hashlib
import os

from agentflow.common import DomainError
from agentflow.control.api import create_app
from agentflow.control.node_routes import create_executor_app
from agentflow.control.owner_ipc import OwnerBroker
from agentflow.control.product_launch import ProductLauncher
from agentflow.control.product_routes import product_router
from agentflow.control.products import ProductService
from agentflow.control.scheduler import Scheduler
from agentflow.execution.service import NodeService
from agentflow.models.profiles import AttemptContext, ModelProfile
from agentflow.models.secrets import LocalSecretStore
from agentflow.models.service import ModelService
from agentflow.runtime.service import RuntimeService
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store

DEFAULT_PROFILE_ID = "00000000-0000-4000-8000-000000000021"


def resolve_secret(profile: ModelProfile) -> str:
    reference = profile.credential_reference
    if not reference.startswith("env:") or not reference[4:].isidentifier():
        raise DomainError("credential_reference_invalid", "Use an explicit env:VARIABLE credential reference")
    value = os.environ.get(reference[4:])
    if not value:
        raise DomainError("credential_missing", "The configured model credential is unavailable")
    return value


class Application:
    def __init__(self, settings: Settings, *, configuration=None):
        self.settings = settings
        self.configuration = configuration
        self.store = Store(settings.data_dir)
        self.models = None
        self.runtime = None
        self.scheduler = None
        self.nodes = None
        self.owner_app = None
        self.executor_app = None
        self._closed = False
        self.owner_broker = None
        self.local_execution = None
        self.products = None
        self.secrets = None

    async def authorize_attempt(self, token: str, protocol: str) -> AttemptContext:
        if not token or len(token) > 8192:
            raise DomainError("unauthorized", "A task credential is required", 401)
        raw = await self.store.read("task_authorization", hashlib.sha256(token.encode()).hexdigest())
        if not raw:
            raise DomainError("unauthorized", "Task credential is not recognized", 401)
        context = AttemptContext.model_validate({k: v for k, v in raw.items() if k in AttemptContext.model_fields})
        attempt = await self.store.read("attempt", context.attempt_id)
        work = await self.store.read("work_item", attempt["work_item_id"]) if attempt else None
        run = await self.store.read("run", context.run_id)
        profile = await self.models.registry.get(context.model_profile_id)
        if (not work or work["status"] != "running" or not run or run["execution_state"] not in {"running", "paused"}
                or work["attempt_id"] != context.attempt_id or work["fencing_token"] != context.fencing_token
                or work["input_fingerprint"] != context.input_fingerprint
                or profile.revision != raw.get("expected_profile_revision", profile.revision)):
            raise DomainError("stale_task_token", "Task authority no longer matches active work", 403)
        from agentflow.runtime.task_authorization import execution_context, record_expiry
        context = await execution_context(self.store, self.settings.data_dir, raw, context)
        try:
            context.assert_current(protocol)
        except DomainError as error:
            if error.code == 'task_authorization_expired':
                await record_expiry(self.store, raw, context, attempt, work, protocol)
            raise
        return context

    async def start(self, *, schedule: bool = True):
        await self.store.start()
        try:
            self.secrets = LocalSecretStore(self.settings.data_dir)
            self.models = ModelService(self.store, self.settings.data_dir, self.authorize_attempt,
                                       lambda profile: self.secrets.read(profile.credential_reference))
            if self.configuration:
                await self.configuration.apply_models(self.models.registry, self.secrets)
            if not await self.store.list("model_profile"):
                await self.models.registry.register(ModelProfile(
                    model_profile_id=DEFAULT_PROFILE_ID, provider="deepseek", requested_model="deepseek-v4-flash",
                    provider_documented_version="DeepSeek-V4.1-Flash", base_url="https://api.deepseek.com",
                    protocols=["chat_completions", "responses"], credential_reference="env:DEEPSEEK_API_KEY",
                    acceptance_status="pending_user_confirmation"), "default-pending-profile")
            await self.models.ledger.reconcile_interrupted()
            self.runtime = RuntimeService(self.store, self.settings.data_dir, self.models, settings=self.settings)
            if self.settings.executor_host:
                self._configure_gateway()
                self.nodes = NodeService(self.store, self.settings.data_dir, self.settings.executor_origin)
                self.executor_app = create_executor_app(self.nodes)
            from agentflow.local_execution import LocalExecutionService
            self.local_execution = LocalExecutionService(self.store, self.settings.data_dir,
                node_service=self.nodes, protected_ports=(self.settings.port,),
                package_fetch_timeout_seconds=self.settings.package_fetch_timeout_seconds)
            await self.local_execution.start()
            self.nodes = self.local_execution.nodes
            self.owner_app = create_app(self.settings, store=self.store,
                artifacts=LocalArtifactStore(self.settings.data_dir / "artifacts"), models=self.models,
                runtime=self.runtime, node_service=self.nodes)
            self.scheduler = Scheduler(self.owner_app.state.workflow, self.store, self.runtime, self.models,
                                       self.settings, node_service=self.nodes, configuration=self.configuration,
                                       on_pending_execution=self.local_execution.resume_pending)
            self.products = ProductService(self.store, self.owner_app.state.workflow, self.models, self.runtime,
                                           self.local_execution, self.secrets, self.settings,
                                           configuration=self.configuration)
            self.products.launcher = ProductLauncher(self.store, self.products.exporter, self.settings,
                                                     protected_ports=(self.local_execution.port,))
            self.owner_app.include_router(product_router(self.products))
            self.owner_app.state.products = self.products
            self.owner_broker = OwnerBroker(self.settings.data_dir, self.owner_app.state.tokens, self.settings.origin)
            await self.owner_broker.start()
            if schedule:
                await self.scheduler.start()
                await self.local_execution.resume_pending()
                await self.products.start()
            return self
        except BaseException:
            await self.close()
            raise

    def _configure_gateway(self):
        """Issue the managed listener leaf only; preserve CA and enrolled local nodes."""
        from urllib.parse import urlsplit

        from agentflow.execution.pki import NodeCertificateAuthority
        directory = self.settings.data_dir / 'nodes/pki'
        managed = (self.settings.tls_certificate == directory / 'server.pem'
                   and self.settings.tls_private_key == directory / 'server.key'
                   and self.settings.tls_client_ca == directory / 'ca.pem')
        if managed:
            hostname = urlsplit(self.settings.executor_origin).hostname
            pki = NodeCertificateAuthority(directory, hostname)
            pki.configure_primary_server_hostname(hostname)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            try:
                if self.products:
                    await self.products.close()
            finally:
                try:
                    if self.scheduler:
                        await self.scheduler.close()
                    elif self.runtime:
                        await self.runtime.close()
                finally:
                    if self.local_execution:
                        await self.local_execution.close()
        finally:
            try:
                if self.models:
                    await self.models.close()
            finally:
                try:
                    if self.owner_broker:
                        await self.owner_broker.close()
                finally:
                    await self.store.close()
