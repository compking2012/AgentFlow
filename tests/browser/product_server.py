"""UI contract fixture only; never a generated-product or live-model acceptance run.

Owner authentication and persistence are real. Product lifecycle changes below
are explicitly driven by browser tests, with no model or scheduler instantiated.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import secrets
import socket
import zipfile
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from agentflow.common import canonical_digest
from agentflow.control.api import create_app
from agentflow.settings import Settings
from agentflow.storage import Store


async def main(directory: Path):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    settings = Settings(data_dir=directory / "data", port=sock.getsockname()[1],
        dashboard_dir=Path(os.environ.get('AGENTFLOW_BROWSER_DASHBOARD_DIR') or
                           Path(__file__).resolve().parents[2] / "src/agentflow/web"))
    store = Store(settings.data_dir)
    await store.start()
    app = create_app(settings, store=store)
    # Override only the product contract in this dedicated test service. No
    # fixture route, data or lifecycle adapter is installed by Application.
    app.router.routes[:] = [route for route in app.router.routes
                            if not getattr(route, "path", "").startswith(("/api/v1/products", "/api/v1/product_setup"))]
    fixture_key = secrets.token_urlsafe(32)
    state = {"roles": False, "coding": False, "local_state": "unprepared", "local_detail": "",
             "model_requests": [], "product_requests": [], "prepare_requests": 0,
             "launch_requests": [], "retry_requests": [], "product_defaults": None,
             "restart_required": False, "launch_url": "http://127.0.0.1:18888"}

    def key(request):
        return request.headers["idempotency-key"]

    def permitted(request):
        return request.headers.get("x-fixture-key") == fixture_key

    @app.get("/api/v1/product_setup")
    async def setup():
        ready_models = state["roles"] and state["coding"]
        profiles = []
        bindings = {}
        for role, binding in (("roles", "role_model_profile_id"), ("coding", "coding_model_profile_id")):
            bindings[binding] = role if state[role] else None
            if state[role]:
                profiles.append({"id": role, "model_profile_id": role, "revision": 1,
                    "accepted_api_model": "browser-contract-model", "acceptance_status": "accepted", "credential_status": "configured"})
        return {"ready": bool(ready_models and state["local_state"] == "ready"), "models_ready": bool(ready_models),
            "requirements": [] if ready_models else [{"code": "models_missing", "message": "请设置分析与编码模型"}],
            "profiles": profiles, "model_bindings": bindings,
            "local_execution": {"state": state["local_state"], "detail": state["local_detail"]},
            "product_defaults": state["product_defaults"], "restart_required": state["restart_required"]}

    @app.post("/api/v1/product_setup/models")
    async def models(request: Request):
        payload = await request.json()
        state["model_requests"].append({**{k: v for k, v in payload.items() if k != "api_key"},
                                        "key_provided": bool(payload.get("api_key"))})
        if payload["model"] == "reject-this-model":
            return JSONResponse({"error": {"code": "unsupported_model", "message": "该模型不支持所选研发用途"}}, 422)
        if not payload.get("api_key") and not payload.get("credential_env"):
            return JSONResponse({"error": {"code": "credential_missing", "message": "请填写凭据"}}, 422)
        for role in ("roles", "coding") if payload["role"] == "both" else (payload["role"],):
            state[role] = True
        return {"saved": True}

    @app.post("/api/v1/product_setup/local_execution")
    async def local_setup():
        state["prepare_requests"] += 1
        state["local_state"] = "preparing"
        state["local_detail"] = "正在执行本机环境检查"
        return {"state": "preparing"}

    @app.get("/api/v1/products")
    async def products():
        return {"items": await store.list("product")}

    @app.post("/api/v1/products", status_code=202)
    async def create_product(request: Request):
        payload = await request.json()
        state["product_requests"].append({"key": key(request), "payload": payload})
        if not state["roles"] or not state["coding"]:
            return JSONResponse({"error": {"code": "models_missing", "message": "先设置研发模型"}}, 409)
        identity = str(uuid4())
        def create(tx):
            selected = payload.get('targets') or [payload.get('target', 'web')]
            return tx.put("product", identity, {**payload, 'targets': selected,
                'target': 'web' if 'web' in selected else 'api', "state": "preparing",
                "output_directory": payload.get("output_directory") or str(directory / "products" / identity),
                "blocking_reasons": [], "fixture_scope": "browser_contract_only_no_generated_product"})
        return await store.command("fixture.product.create", key(request), payload, create)

    @app.get("/api/v1/products/{identity}")
    async def product(identity: str):
        return await store.read("product", identity)

    @app.post("/api/v1/products/{identity}/launch")
    async def launch(identity: str):
        state["launch_requests"].append(identity)
        return await save_launch(identity, {"url": state["launch_url"], "state": "running"})

    @app.post("/api/v1/products/{identity}/stop")
    async def stop(identity: str):
        return await save_launch(identity, {"url": None, "state": "stopped"})

    async def save_launch(identity, launch_state):
        def update(tx):
            row = tx.get("product", identity)
            tx.put("product", identity, {**row, "launch": launch_state}, row["revision"])
            return launch_state
        return await store.command("fixture.product.launch", str(uuid4()), {}, update)

    @app.post("/api/v1/products/{identity}/retry", status_code=202)
    async def retry(identity: str, request: Request):
        state["retry_requests"].append({"product_id": identity, "key": key(request)})
        def update(tx):
            row = tx.get("product", identity)
            return tx.put("product", identity, {**row, "state": "running" if row.get("finalization_error") else "preparing",
                "finalization_error": False, "blocking_reasons": []}, row["revision"])
        return await store.command("fixture.product.retry", key(request), {"product_id": identity}, update)

    @app.get("/api/v1/products/{identity}/download")
    async def download(identity: str):
        product = await store.read("product", identity)
        if not product or product["state"] != "completed" or not product.get("delivery"):
            return JSONResponse({"error": {"code": "not_delivered", "message": "尚未交付"}}, 409)
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("browser-contract-fixture.txt", "UI download transport fixture, not generated software.")
        return Response(content.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="product-source.zip"'})

    @app.get("/__fixture/state")
    async def observed(request: Request):
        if not permitted(request):
            return JSONResponse({}, 403)
        return {**state, "products": await store.list("product")}

    @app.post("/__fixture/setup")
    async def change_setup(request: Request):
        if not permitted(request):
            return JSONResponse({}, 403)
        changes = await request.json()
        for name in ("roles", "coding", "local_state", "local_detail", "launch_url", "product_defaults", "restart_required"):
            if name in changes:
                state[name] = changes[name]
        return {"ok": True}

    review_work_id = str(uuid4())

    @app.post("/__fixture/product")
    async def change_product(request: Request):
        if not permitted(request):
            return JSONResponse({}, 403)
        changes = await request.json()
        def change(tx):
            current = tx.get("product", changes["id"])
            updates = {"state": changes["state"], "blocking_reasons": changes.get("blocking_reasons", [])}
            for name in ("launch", "restore_reconciliation_required", "finalization_error"):
                if name in changes:
                    updates[name] = changes[name]
            if changes["state"] in {"running", "waiting_approval", "completed"} and not current.get("run_id"):
                run_id, plan_id, project_id, work_id = (str(uuid4()) for _ in range(4))
                tx.put("plan", plan_id, {"plan_id": plan_id, "goal": current["goal"], "state": "started",
                    "actual_steps": ["implementation"], "missing_inputs": [], "work_specs": [], "base_commit": "a" * 40,
                    "app_targets": [current["target"]], "target_configs": []})
                tx.put("run", run_id, {"run_id": run_id, "plan_id": plan_id, "project_id": project_id, "goal": current["goal"],
                    "execution_state": "running", "quality_result": "unknown", "purpose": "code_delivery",
                    "input_fingerprint": canonical_digest(current["goal"]), "delivery_ids": [], "blocking_reasons": []})
                tx.put("work_item", work_id, {"run_id": run_id, "project_id": project_id, "key": "implementation",
                    "step": "implementation", "role": "development", "status": "running", "quality_result": "unknown",
                    "generation": 1, "fencing_token": 1, "attempt_id": "fixture-attempt", "dependencies": [],
                    "artifact_ids": [], "approval_required": False, "required": True})
                updates.update(run_id=run_id, project_id=project_id)
            if changes.get('review_quality') in {'failed', 'unknown', 'passed'}:
                previous = tx.get('work_item', review_work_id)
                tx.put('work_item', review_work_id, {'run_id': updates.get('run_id', current.get('run_id')),
                    'project_id': updates.get('project_id', current.get('project_id')),
                    'key': 'code_review', 'step': 'code_review', 'role': 'review', 'status': 'completed',
                    'quality_result': changes['review_quality'], 'generation': 1, 'fencing_token': 1,
                    'attempt_id': None, 'dependencies': [], 'artifact_ids': [], 'approval_required': False,
                    'required': True}, previous['revision'] if previous else None)
                tx.event('fixture.review-verdict', {'work_item_id': review_work_id},
                         run_id=updates.get('run_id', current.get('run_id')))
            if changes.get("delivery"):
                updates["delivery"] = {"source_commit": "b" * 40, "path": current["output_directory"],
                    "archive_download_url": changes.get("download_url") or f"/api/v1/products/{current['id']}/download",
                    "launch_command": "node server.js", "working_directory": current["output_directory"]}
            return tx.put("product", current["id"], {**current, **updates}, current["revision"])
        return await store.command("fixture.product.advance", str(uuid4()), changes, change)

    print(json.dumps({"origin": settings.origin, "bootstrap": app.state.tokens.bootstrap_code,
                      "fixture_key": fixture_key, "directory": str(directory)}), flush=True)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning", access_log=False))
    try:
        await server.serve(sockets=[sock])
    finally:
        await store.close()
        sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    asyncio.run(main(parser.parse_args().directory))
