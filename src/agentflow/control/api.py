"""Owner-only loopback API. Executor routes live in a separate ASGI application."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from agentflow import __version__
from agentflow.common import DomainError
from agentflow.control.security import TokenAuthority
from agentflow.control.service import WorkflowService
from agentflow.settings import Settings

logger = logging.getLogger(__name__)


class Contract:
    def __init__(self, path: Path | None = None):
        if path is None:
            path = Path(__file__).resolve().parents[1] / "design.openapi.json"
            if not path.exists():
                path = Path(__file__).resolve().parents[3] / "contracts/design.openapi.json"
        self.document = json.loads(path.read_text())

    def validate(self, name: str, payload: dict) -> dict:
        schema = {"$ref": f"#/components/schemas/{name}", "components": self.document["components"]}
        errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload))
        if errors:
            details = [{"path": "/".join(map(str, e.path)), "rule": e.validator} for e in errors[:12]]
            raise DomainError("invalid_request", "Request does not match the contract", 422, details)
        return payload


async def body(request: Request, name: str | None = None) -> dict:
    try:
        value = await request.json()
    except (ValueError, UnicodeError):
        raise DomainError("invalid_json", "Expected a JSON object", 422) from None
    if not isinstance(value, dict):
        raise DomainError("invalid_json", "Expected a JSON object", 422)
    if name:
        return request.app.state.contract.validate(name, value)
    return value


def command_key(request: Request) -> str:
    value = request.headers.get("idempotency-key", "")
    if not value or len(value) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise DomainError("idempotency_key_required", "Provide an Idempotency-Key of 1–128 printable characters", 422)
    return value


def bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer ") or len(value) > 8192:
        raise DomainError("unauthorized", "A bearer credential is required", 401)
    return value[7:]


def create_app(settings: Settings | None = None, *, store=None, artifacts=None,
               models=None, runtime=None, node_service=None, scheduler=None) -> FastAPI:
    settings = settings or Settings()
    from agentflow.storage import LocalArtifactStore, Store
    supplied_store = store is not None
    store = store or Store(settings.data_dir)
    artifacts = artifacts or LocalArtifactStore(settings.data_dir / "artifacts")
    tokens = TokenAuthority(settings.bootstrap_seconds, settings.owner_token_seconds)
    shutdown_event = asyncio.Event()
    from agentflow.control.presentation import WorkflowViewRequests
    workflow_views = WorkflowViewRequests(store, artifacts, settings)

    @asynccontextmanager
    async def lifespan(app):
        shutdown_event.clear()
        if not supplied_store:
            await store.start()
        try:
            if scheduler:
                await scheduler.start()
            yield
        finally:
            shutdown_event.set()
            await workflow_views.close()
            if scheduler:
                await scheduler.close()
            if not supplied_store:
                await store.close()

    app = FastAPI(title="AgentFlow local owner API", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.settings, app.state.store, app.state.tokens = settings, store, tokens
    app.state.models, app.state.nodes, app.state.runtime = models, node_service, runtime
    app.state.contract = Contract()
    app.state.shutdown_event = shutdown_event

    from agentflow.control.project_workflow import project_workflow_router
    app.include_router(project_workflow_router(store))

    async def profiles():
        if models:
            return await models.list_profiles()
        return await store.list("model_profile")

    workflow = WorkflowService(store, artifacts, settings, profiles)
    app.state.workflow = workflow
    from agentflow.runtime.trace import ExecutionTrace
    traces = ExecutionTrace(store)

    @app.get('/api/v1/runs/{run_id}/work_items/{work_id}/attempts')
    async def task_attempts(run_id: str, work_id: str, before: str | None = None, limit: int = 20):
        return await traces.attempts(run_id, work_id, before=before, limit=limit)

    @app.get('/api/v1/attempts/{attempt_id}/trace')
    async def attempt_trace(attempt_id: str, after: int | None = None, before: int | None = None, limit: int = 50):
        return await traces.page(attempt_id, after=after, before=before, limit=limit)

    @app.exception_handler(DomainError)
    async def domain_error(_request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
                            status_code=exc.status)

    @app.exception_handler(ValidationError)
    async def validation_error(_request, _exc):
        return JSONResponse({"error": {"code": "invalid_request", "message": "Request validation failed"}}, status_code=422)

    @app.middleware("http")
    async def boundary(request, call_next):
        try:
            authority = urlsplit(settings.origin).netloc
            if request.headers.get("host") != authority:
                raise DomainError("invalid_host", "Host authority is not allowed", 403)
            path = request.url.path
            origin = request.headers.get("origin")
            internal = path.startswith("/internal/")
            if origin is not None and origin != settings.origin:
                raise DomainError("invalid_origin", "Origin is not allowed", 403)
            if path.startswith("/executor/"):
                raise DomainError("not_found", "Executor routes are not exposed by this listener", 404)
            if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
                if not internal and origin != settings.origin:
                    raise DomainError("origin_required", "Owner commands require the control Origin", 403)
                command_key(request) if not path.startswith("/internal/v1/llm/") else None
                content = bytearray()
                async for chunk in request.stream():
                    content.extend(chunk)
                    if len(content) > settings.max_body_bytes:
                        raise DomainError("body_too_large", "Request exceeds the configured limit", 413)
                request._body = bytes(content)
            if path == '/api/v1/session/local' and request.method == 'POST':
                try:
                    local_client = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
                except ValueError:
                    local_client = False
                if not local_client or request.headers.get('sec-fetch-site') not in {None, 'same-origin', 'none'}:
                    raise DomainError('local_session_forbidden', 'Local sessions require a same-origin loopback client', 403)
            elif path == '/api/v1/session/resume' and request.method == 'POST':
                tokens.require(bearer(request), 'agentflow_browser_session', 'session:renew')
            elif path.startswith("/api/") and not (path == "/api/v1/session" and request.method == "POST"):
                tokens.require(bearer(request), "agentflow_owner", "owner:control")
            response = await call_next(request)
        except DomainError as exc:
            response = JSONResponse({"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
                                    status_code=exc.status)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__, "mode": "local_single_owner"}

    @app.post("/api/v1/session", status_code=201)
    async def bootstrap(request: Request):
        payload = await body(request, "SessionBootstrapRequest")
        token = tokens.exchange(payload["bootstrap_token"])
        return {"owner_token": token, "token_type": "Bearer",
                **({'browser_session_token': tokens.browser_session(token)} if payload.get('browser_session') else {}),
                "expires_at": (datetime.now(UTC) + timedelta(seconds=settings.owner_token_seconds)).isoformat()}

    @app.post('/api/v1/session/resume')
    async def resume_browser(request: Request):
        payload = await body(request)
        if payload != {}:
            raise DomainError('invalid_request', 'Browser continuation accepts an empty object', 422)
        token = tokens.resume_browser_session(bearer(request))
        return {'owner_token': token, 'token_type': 'Bearer',
                'expires_at': (datetime.now(UTC) + timedelta(seconds=settings.owner_token_seconds)).isoformat()}

    @app.post('/api/v1/session/local', status_code=201)
    async def local_browser(request: Request):
        await body(request, 'SessionLocalRequest')
        token = tokens.issue('agentflow_owner', {'owner:*'}, 'owner', settings.owner_token_seconds)
        return {'owner_token': token, 'browser_session_token': tokens.browser_session(token), 'token_type': 'Bearer',
                'expires_at': (datetime.now(UTC) + timedelta(seconds=settings.owner_token_seconds)).isoformat()}

    @app.get("/api/v1/session")
    async def session():
        return {"authenticated": True, "owner": "local", "origin": settings.origin}

    @app.delete("/api/v1/session", status_code=204)
    async def logout(request: Request):
        tokens.revoke(bearer(request))

    @app.get("/api/v1/meta")
    async def metadata():
        return {"version": __version__, "agent_concurrency": settings.agent_concurrency,
                "trusted_project_execution": settings.trusted_project_execution,
                "targets": ["web", "api", "ios_native", "android_native", "windows_native", "macos_native", "linux_native"],
                "executor_configured": settings.executor_host is not None}

    @app.get("/api/v1/projects")
    async def projects():
        return {"items": await store.list("project"), "next_cursor": None}

    @app.post("/api/v1/projects", status_code=201)
    async def create_project(request: Request):
        return await workflow.create_project(await body(request, "ProjectCreateRequest"), command_key(request))

    @app.post("/api/v1/run_plans", status_code=201)
    async def create_plan(request: Request):
        return await workflow.create_plan(await body(request, "RunPlanCreateRequest"), command_key(request))

    @app.get("/api/v1/run_plans/{plan_id}")
    async def get_plan(plan_id: str):
        value = await store.read("plan", plan_id)
        if value is None:
            raise DomainError("not_found", "Unknown plan", 404)
        return value

    @app.post("/api/v1/runs", status_code=201)
    async def start_run(request: Request):
        result = await workflow.start_run(await body(request, "RunStartRequest"), command_key(request))
        if scheduler:
            scheduler.wake()
        return result

    @app.get("/api/v1/runs")
    async def runs():
        from agentflow.control.product_management import filter_visible_product_runs
        return {"items": await filter_visible_product_runs(store, await store.list("run")), "next_cursor": None}

    @app.get("/api/v1/runs/{run_id}")
    async def run(run_id: str):
        return await workflow.run_detail(run_id)

    @app.get('/api/v1/runs/{run_id}/workflow')
    async def readable_workflow(run_id: str):
        return await workflow_views.workflow(run_id)

    @app.get('/api/v1/runs/{run_id}/quality_summary')
    async def readable_quality(run_id: str):
        from agentflow.control.presentation import RunPresentationService
        return await RunPresentationService(store, artifacts, settings).quality(run_id)

    @app.get('/api/v1/runs/{run_id}/recovery_options')
    async def recovery_options(run_id: str):
        from agentflow.control.model_uncertainty import ModelUncertaintyService
        from agentflow.control.recovery import RunRecoveryService
        result = await RunRecoveryService(store, workflow).options(run_id)
        result['model_uncertainties'] = await ModelUncertaintyService(store, workflow, models).view(run_id)
        return result

    @app.get('/api/v1/runs/{run_id}/model_uncertainties')
    async def model_uncertainties(run_id: str):
        from agentflow.control.model_uncertainty import ModelUncertaintyService
        return await ModelUncertaintyService(store, workflow, models).view(run_id)

    @app.post('/api/v1/runs/{run_id}/model_invocations/{invocation_id}/acknowledge_unknown')
    async def acknowledge_model_uncertainty(run_id: str, invocation_id: str, request: Request):
        from agentflow.control.model_uncertainty import ModelUncertaintyService
        return await ModelUncertaintyService(store, workflow, models).acknowledge(
            run_id, invocation_id, await body(request), command_key(request))

    @app.get('/api/v1/runs/{run_id}/work_items/{work_id}/execution_budget')
    async def work_execution_budget(run_id: str, work_id: str):
        from agentflow.control.work_execution_budget import WorkExecutionBudgetService
        return await WorkExecutionBudgetService(store, workflow).view(run_id, work_id)

    @app.post('/api/v1/runs/{run_id}/work_items/{work_id}/execution_budget/extend')
    async def extend_work_execution_budget(run_id: str, work_id: str, request: Request):
        from agentflow.control.work_execution_budget import WorkExecutionBudgetService
        return await WorkExecutionBudgetService(store, workflow).extend(run_id, work_id, await body(request), command_key(request))

    @app.get('/api/v1/runs/{run_id}/review_baseline_recovery')
    async def preview_review_baseline_recovery(run_id: str, work_item_id: str,
                                              review_repair_id: str, empty_recovery_id: str):
        from agentflow.control.review_baseline_recovery import ReviewBaselineRecovery
        return await ReviewBaselineRecovery(store, workflow).preview(
            run_id, work_item_id, review_repair_id, empty_recovery_id)

    @app.post('/api/v1/runs/{run_id}/review_repairs')
    async def schedule_review_source_repair(run_id: str, request: Request):
        from agentflow.control.review_source_repair import OwnerReviewSourceRepair
        result = await OwnerReviewSourceRepair(store, workflow).schedule(
            run_id, await body(request), command_key(request))
        if scheduler:
            scheduler.wake()
        return result

    @app.post('/api/v1/runs/{run_id}/review_baseline_recovery')
    async def restore_review_baseline(run_id: str, request: Request):
        from agentflow.control.review_baseline_recovery import ReviewBaselineRecovery
        return await ReviewBaselineRecovery(store, workflow).restore(run_id, await body(request), command_key(request))

    @app.post('/api/v1/runs/{run_id}/recover')
    async def recover_run(run_id: str, request: Request):
        from agentflow.control.recovery import RunRecoveryService
        result = await RunRecoveryService(store, workflow).recover(run_id, await body(request), command_key(request))
        if scheduler:
            scheduler.wake()
        return result

    @app.get('/api/v1/readable_artifacts/{artifact_id}')
    async def readable_artifact(artifact_id: str, download: bool = False):
        from agentflow.control.presentation import RunPresentationService
        record, verified = await RunPresentationService(store, artifacts, settings).readable(artifact_id, preview=not download)
        if download:
            return FileResponse(verified['path'], media_type='text/markdown; charset=utf-8', filename=record['name'])
        return {'name': record['name'], 'media_type': 'text/markdown',
                'content': (await artifacts.read(record['digest'])).decode('utf-8')}

    @app.post("/api/v1/runs/{run_id}/control")
    async def control(run_id: str, request: Request):
        result = await workflow.control_run(run_id, await body(request, "RunControlRequest"), command_key(request))
        if scheduler:
            scheduler.wake()
        return result

    @app.post("/api/v1/runs/{run_id}/revisions")
    async def revise(run_id: str, request: Request):
        payload = await body(request)
        if (set(payload) != {"expected_revision", "work_item_ids", "reason"}
                or not isinstance(payload["work_item_ids"], list) or not payload["reason"]):
            raise DomainError("invalid_revision", "Provide revision, affected work items and reason", 422)
        result = await workflow.revise(run_id, payload, command_key(request))
        if scheduler:
            scheduler.wake()
        return result

    @app.post('/api/v1/runs/{run_id}/request_limit')
    async def extend_request_limit(run_id: str, request: Request):
        from agentflow.control.request_limits import RequestLimitService
        return await RequestLimitService(store).extend(run_id, await body(request), command_key(request))

    @app.get("/api/v1/runs/{run_id}/work_items")
    async def work_items(run_id: str):
        await workflow.run_detail(run_id)
        return {"items": [i for i in await store.list("work_item") if i["run_id"] == run_id]}

    @app.get("/api/v1/runs/{run_id}/approvals")
    async def approvals(run_id: str):
        await workflow.run_detail(run_id)
        return {"items": [a for a in await store.list("approval") if a["run_id"] == run_id]}

    @app.post("/api/v1/approvals/{approval_id}/decisions")
    async def decide(approval_id: str, request: Request):
        result = await workflow.decide(approval_id, await body(request, "ApprovalDecisionRequest"), command_key(request))
        if scheduler:
            scheduler.wake()
        return result

    @app.get("/api/v1/artifacts/{artifact_id}")
    async def artifact(artifact_id: str, download: bool = False):
        value = await store.read("artifact", artifact_id)
        if not value:
            raise DomainError("not_found", "Unknown artifact", 404)
        if not download:
            return value
        verified = await artifacts.verify(value["digest"])
        return FileResponse(verified["path"], media_type="application/octet-stream", filename=f"{artifact_id}.bin",
                            headers={"X-Content-Digest": value["digest"]})

    @app.get("/api/v1/model_profiles")
    async def model_profiles():
        return {"items": await profiles()}

    @app.get("/api/v1/backends")
    async def backends():
        if runtime:
            items = await runtime.probe()
            return {"items": [{**item, "backend_id": str(uuid5(NAMESPACE_URL, "agentflow:backend:" + item["backend"]))}
                              for item in items]}
        return {"items": [], "blocking_reasons": ["Runtime adapter is not configured"]}

    @app.post("/api/v1/probes")
    async def probe(request: Request):
        payload = await body(request, "ProbeRequest")
        if payload.get("mode", "offline") != "offline":
            raise DomainError("live_probe_not_enabled", "Use an explicitly configured live integration harness", 409)
        if models and payload["subject_type"] == "model_profile":
            return await models.probe_profile(payload["subject_id"], mode="offline")
        if runtime:
            return {"mode": "offline", "items": await runtime.probe(), "paid_requests_started": 0}
        raise DomainError("backend_unavailable", "Configure the requested backend before probing")

    @app.get("/api/v1/probes")
    async def probes():
        return {"items": await store.list("probe")}

    @app.get("/api/v1/executor_nodes")
    async def nodes():
        return {"items": await node_service.list_nodes() if node_service else []}

    @app.get("/api/v1/executor_resources")
    async def resources():
        return {"items": await store.list("node_resource")}

    @app.get("/api/v1/runs/{run_id}/target_matrix")
    async def matrix(run_id: str):
        await workflow.run_detail(run_id)
        return {"items": [m for m in await store.list("target_matrix") if m.get("run_id") == run_id]}

    @app.get("/api/v1/runs/{run_id}/checks")
    async def checks(run_id: str):
        await workflow.run_detail(run_id)
        return {"items": [c for c in await store.list("check") if c.get("run_id") == run_id]}

    @app.get("/api/v1/runs/{run_id}/scenarios")
    async def scenarios(run_id: str):
        await workflow.run_detail(run_id)
        return {"items": [s for s in await store.list("cross_scenario") if s.get("run_id") == run_id]}

    @app.get("/api/v1/runs/{run_id}/candidates")
    async def candidates(run_id: str):
        await workflow.run_detail(run_id)
        return {"items": [c for c in await store.list("candidate") if c.get("run_id") == run_id]}

    @app.get("/api/v1/runs/{run_id}/deliveries")
    async def deliveries(run_id: str):
        await workflow.run_detail(run_id)
        return {"items": [d for d in await store.list("delivery") if d.get("run_id") == run_id]}

    @app.get("/api/v1/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = 0):
        await workflow.run_detail(run_id)
        if after < 0:
            raise DomainError("invalid_cursor", "Event cursor must be nonnegative", 422)
        token = bearer(request)
        async def stream():
            cursor = after
            while not shutdown_event.is_set() and not await request.is_disconnected():
                try:
                    tokens.require(token, "agentflow_owner", "owner:read")
                except DomainError:
                    return
                batch = await store.events(cursor, run_id, 100)
                for event in batch:
                    if shutdown_event.is_set():
                        return
                    cursor = int(event.get("sequence", event.get("id", event.get("cursor"))))
                    yield f"id: {cursor}\nevent: change\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not batch and not shutdown_event.is_set():
                    yield ": heartbeat\n\n"
                    try:
                        await asyncio.wait_for(shutdown_event.wait(), timeout=1)
                    except TimeoutError:
                        pass
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    if models:
        from agentflow.models.service import create_model_router
        app.include_router(create_model_router(models))
        from agentflow.runtime.research import create_research_router
        app.include_router(create_research_router(models, settings))

    if node_service:
        from agentflow.control.node_routes import owner_node_router
        app.include_router(owner_node_router(node_service))

    dashboard = settings.dashboard_dir or Path(__file__).resolve().parents[1] / "web"
    if (dashboard / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dashboard / "assets"), name="assets")

    @app.get("/")
    async def index():
        if (dashboard / "index.html").is_file():
            return FileResponse(dashboard / "index.html")
        return JSONResponse({"product": "AgentFlow", "status": "dashboard_build_required",
                             "instruction": "Build apps/dashboard before starting the product UI"}, status_code=503)

    return app
