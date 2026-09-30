"""A-layer CLI/WebUI goal→actual engineering→runnable product acceptance.

Only upstream model responses are scripted. Application, HTTP, SDKs, Codex
apply_patch, local execution, raw reports, gates, Git delivery and export are real.
This is NOT evidence that a live LLM/provider produced the product independently.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import platform
import socket
import sys
from pathlib import Path

import httpx
import uvicorn

from agentflow.application import Application
from agentflow.configuration import load_configuration
from agentflow.control.instance import clear_instance, publish_instance
from agentflow.control.owner_ipc import launch_url
from agentflow.execution.manifests import file_digest, tree_digest
from agentflow.server import ManagedServer
from agentflow.storage import Store

from .cache_seed import seed_locked_npm_cache
from .model_fixture import GOAL, MODEL, PROVIDER_SECRET, ROLE_MODEL, ROOT, ScriptedModel


async def owner_session(data_dir, client, origin):
    from urllib.parse import parse_qs, urlsplit
    from uuid import uuid4
    code = parse_qs(urlsplit(await launch_url(data_dir)).fragment)["bootstrap"][0]
    response = await client.post("/api/v1/session", json={"bootstrap_token": code},
        headers={"Origin": origin, "Idempotency-Key": str(uuid4())})
    assert response.status_code == 201, response.text
    return {"Authorization": "Bearer " + response.json()["owner_token"], "Origin": origin}


async def diagnostics(application, fixture, directory):
    tables = {}
    for name in ["product", "run", "work_item", "node_job", "candidate", "check", "delivery",
                 "product_test_repair", "code_snapshot", "model_invocation"]:
        tables[name] = await application.store.list(name)
    tables["fixture_calls"] = fixture.calls
    tables["fixture_errors"] = fixture.errors
    output = directory / "workflow-diagnostics.json"
    output.write_text(json.dumps(tables, ensure_ascii=False, indent=2))
    return output


async def exercise_product(application, fixture, client, headers, settings, tmp_path, target):
    process = None
    communicated = None
    product = None
    observer_reconnects = []
    try:
        tmp_path.mkdir(parents=True, exist_ok=True)
        output = tmp_path / "product"
        goal = GOAL if target == "web" else GOAL.replace("Web/API", "API")
        if target == "api":
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "agentflow.cli",
                "run", goal, "--name", "阅读清单-" + target, "--output", str(output),
                cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
            communicated = asyncio.create_task(process.communicate())
        else:
            process = await asyncio.create_subprocess_exec("node", str(Path(__file__).with_name("create_product_ui.mjs")),
                cwd=ROOT, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(ROOT / "apps/dashboard/.playwright-browsers")})
            payload = {"bootstrap_url": await launch_url(settings.data_dir), "name": "阅读清单-web", "goal": goal,
                "output_directory": str(output), "evidence_directory": str(tmp_path)}
            communicated = asyncio.create_task(process.communicate(json.dumps(payload, ensure_ascii=False).encode()))
        last = None
        async with asyncio.timeout(900):
            while True:
                try:
                    response = await client.get("/api/v1/products", headers=headers)
                except (httpx.ReadError, httpx.RemoteProtocolError) as error:
                    # Only the independent read-only observer reconnects. No
                    # product mutation, Agent attempt or result is repeated.
                    observer_reconnects.append({"operation": "GET /api/v1/products", "error": type(error).__name__})
                    assert len(observer_reconnects) <= 3, observer_reconnects
                    await asyncio.sleep(.25)
                    continue
                assert response.status_code == 200, response.text
                products = [p for p in response.json()["items"] if p["output_directory"] == str(output)]
                assert len(products) <= 1, "A single public command created duplicate products"
                if products:
                    product = products[0]
                    phase = (product["state"], product.get("phase"))
                    if phase != last:
                        print("goal-e2e:", target, phase, product.get("blocking_reasons", []), flush=True)
                        last = phase
                if fixture.errors:
                    raise AssertionError(f"Model fixture error: {fixture.errors}")
                if communicated.done() and (process.returncode != 0 or product is None
                        or product["state"] in {"completed", "blocked", "cancelled"}):
                    break
                await asyncio.sleep(.5)
        stdout, stderr = await communicated
        (tmp_path / "cli-stderr.log").write_bytes(stderr)
        (tmp_path / "cli-stdout.log").write_bytes(stdout)
        assert process.returncode == 0, stderr.decode() + "\n" + stdout.decode()
        assert product is not None, "No product was observed through the real owner API"
        observed = await client.get(f"/api/v1/products/{product['id']}", headers=headers)
        assert observed.status_code == 200, observed.text
        product = observed.json()
        # Retain the existing evidence filename, but its source is now the real
        # owner API. Human-oriented CLI output is separately kept as plain text.
        (tmp_path / "cli-output.json").write_text(json.dumps(product, ensure_ascii=False, indent=2))
        if target == "web":
            ui_evidence = json.loads((tmp_path / "ui-entry.json").read_text())
            assert ui_evidence == {"entry": "compiled_webui", "product_id": product["id"],
                "product_posts": 1, "response_status": 202, "mocked_requests": False}
        assert product["state"] == "completed" and product["delivery"]
        assert product["goal"] == goal and product["run_id"]
        run_response = await client.get(f"/api/v1/runs/{product['run_id']}", headers=headers)
        assert run_response.status_code == 200
        run = run_response.json()
        work = run["work_items"]
        assert all(w["status"] == "completed" for w in work if w.get("required", True))
        assert {"goal", "research", "prd", "requirements", "architecture", "development_plan", "implementation",
            "code_review", "unit_test_plan", "unit_test_implementation", "integration_test_strategy",
            "integration_test_implementation", "unit_test_execution", "integration_test_execution", "delivery"} <= {w["step"] for w in work}
        assert all(w["quality_result"] == "passed" for w in work if w["step"] in {"code_review", "unit_test_execution", "integration_test_execution"})
        candidates = [c for c in await application.store.list("candidate") if c["run_id"] == run["id"]]
        current_candidates = [c for c in candidates if c["run_input_fingerprint"] == run["input_fingerprint"]]
        assert len(current_candidates) == 1
        candidate = current_candidates[0]
        all_checks = [c for c in await application.store.list("check") if c["run_id"] == run["id"]]
        checks = [c for c in all_checks if c["candidate_fingerprint"] == candidate["fingerprint"]]
        assert len(checks) == (4 if target == "web" else 2) and all(c["quality_result"] == "passed" and c["evidence_verified"] for c in checks)
        for check in checks:
            assert check["candidate_fingerprint"] == candidate["fingerprint"] and check["executed_case_count"] > 0
        for check in all_checks:
            assert check["evidence_verified"]
            artifact = await application.store.read("node_artifact", check["raw_report_artifact_id"])
            assert file_digest(application.nodes.artifacts.object_path(artifact["digest"])) == artifact["digest"]
        repairs = [r for r in await application.store.list("product_test_repair") if r["run_id"] == run["id"]]
        failed_checks = [c for c in all_checks if c["quality_result"] == "failed"]
        if target == "web":
            assert len(repairs) == 1 and len(candidates) == 2 and len(failed_checks) == 1
            repair = repairs[0]
            initial = next(c for c in candidates if c["id"] == repair["candidate_id"])
            assert initial["fingerprint"] == failed_checks[0]["candidate_fingerprint"] != candidate["fingerprint"]
            assert repair["preserved_test_source"] == initial["source_commit"]
            assert any(w["id"] == repair["repair_work_item_id"] and w["status"] == "completed" for w in work)
            assert any(w["id"] == repair["review_work_item_id"] and w["status"] == "completed" and w["quality_result"] == "passed" for w in work)
            difference = await asyncio.create_subprocess_exec("git", "-C", candidate["source_repository"], "diff", "--name-only",
                initial["source_commit"], candidate["source_commit"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            difference_out, difference_err = await difference.communicate()
            assert difference.returncode == 0, difference_err.decode()
            assert difference_out.decode().splitlines() == ["public/index.html"], "Repair must preserve all original test source, tooling and acceptance recipes"
        else:
            assert repairs == [] and failed_checks == []
        jobs = [j for j in await application.store.list("node_job") if j.get("run_id") == run["id"]]
        assert {"build", "test"} <= {j["kind"] for j in jobs}
        assert all(j["state"] == "completed" for j in jobs)
        results = [await application.store.read("node_result", j["result_id"]) for j in jobs]
        assert all(result["assessment_state"] == "validated" for result in results)
        expected_targets = ["api", "web"] if target == "web" else ["api"]
        model_calls = [call for call in fixture.calls if call["targets"] == expected_targets]
        assert {"implementation", "unit_test_implementation", "integration_test_implementation"} <= {
            call["stage"] for call in model_calls if call["tool_outputs"]}
        assert any(call["repair"] and call["tool_outputs"] for call in model_calls) == (target == "web")
        published = await asyncio.create_subprocess_exec("git", "-C", str(output / "repository"),
            "rev-parse", f"refs/heads/codex/agentflow/{run['id']}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        published_out, published_err = await published.communicate()
        assert published.returncode == 0, published_err.decode()
        assert published_out.decode().strip() == candidate["source_commit"]
        release = Path(product["delivery"]["path"])
        assert (release / "product/server.mjs").is_file()
        assert (release / "product/books.mjs").is_file()
        assert (release / "source/src/books.mjs").is_file()
        assert (release / "README.md").is_file() and (release / "start.sh").is_file()
        assert "业务功能尚未实现" not in (release / "product/public/index.html").read_text()
        immutable = tree_digest(release)
        from uuid import uuid4
        launched = await client.post(f"/api/v1/products/{product['id']}/launch", headers={**headers, "Idempotency-Key": str(uuid4())})
        assert launched.status_code == 200, launched.text
        url = launched.json()["url"]
        assert url != settings.origin
        async with httpx.AsyncClient(base_url=url, trust_env=False) as user:
            assert (await user.post("/api/books", json={"title": " "})).status_code == 400
            created = await user.post("/api/books", json={"title": "Independent API Book", "author": "Reader"})
            assert created.status_code == 201
            book = created.json()
            changed = await user.patch(f"/api/books/{book['id']}", json={"title": "Independent Persisted", "read": True})
            assert changed.status_code == 200 and changed.json()["read"] is True
            filtered = (await user.get("/api/books?read=read")).json()["books"]
            assert any(b["id"] == book["id"] for b in filtered)
        if target == "web":
            browser = await asyncio.create_subprocess_exec("node", str(Path(__file__).with_name("verify_product.mjs")), url,
                str(tmp_path / "delivered-product.png"), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(ROOT / "apps/dashboard/.playwright-browsers")})
            browser_out, browser_err = await asyncio.wait_for(browser.communicate(), 60)
            assert browser.returncode == 0, browser_out.decode() + browser_err.decode()
            assert json.loads(browser_out)["browser_verified"]
        stopped = await client.post(f"/api/v1/products/{product['id']}/stop", headers={**headers, "Idempotency-Key": str(uuid4())})
        assert stopped.status_code == 200 and stopped.json()["state"] == "stopped"
        restarted = await client.post(f"/api/v1/products/{product['id']}/launch", headers={**headers, "Idempotency-Key": str(uuid4())})
        assert restarted.status_code == 200, restarted.text
        async with httpx.AsyncClient(base_url=restarted.json()["url"], trust_env=False) as user:
            persisted = await user.get(f"/api/books/{book['id']}")
            assert persisted.json() == {"id": book["id"], "title": "Independent Persisted", "author": "Reader", "read": True}
            assert (await user.delete(f"/api/books/{book['id']}")).status_code == 204
            assert (await user.get(f"/api/books/{book['id']}")).status_code == 404
        assert tree_digest(release) == immutable
        archive = await client.get(product["delivery"]["archive_download_url"], headers=headers)
        assert archive.status_code == 200 and archive.content.startswith(b"PK")
        (tmp_path / "acceptance-scope.json").write_text(json.dumps({"scope": "A: scripted upstream model protocol; actual full workflow and executable product",
            "live_llm_verified": False, "target": target, "entry": "webui" if target == "web" else "cli",
            "receipt_source": "owner_api", "configuration": "isolated HOME/.config/agentflow/config.toml",
            "product_id": product["id"], "run_id": run["id"], "url": url,
            "source_commit": candidate["source_commit"], "candidate_fingerprint": candidate["fingerprint"],
            "fixture_model_calls": len(model_calls), "valid_checks": len(checks), "repair_verified": target == "web",
            "retained_failed_checks": len(failed_checks)}, ensure_ascii=False, indent=2))
        return {"product": product, "candidate_fingerprint": candidate["fingerprint"], "check_ids": {c["id"] for c in checks}}
    finally:
        try:
            if process and process.returncode is None:
                process.terminate()
                try:
                    if communicated:
                        await asyncio.wait_for(asyncio.shield(communicated), 15)
                    else:
                        await asyncio.wait_for(process.wait(), 15)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            if communicated and communicated.done() and not communicated.cancelled():
                stdout, stderr = communicated.result()
                (tmp_path / "cli-stderr.log").write_bytes(stderr)
                (tmp_path / "cli-stdout.log").write_bytes(stdout)
        finally:
            (tmp_path / "observer-reconnects.json").write_text(json.dumps(observer_reconnects, indent=2))
            await diagnostics(application, fixture, tmp_path)


async def test_cli_and_webui_goals_deliver_real_products_with_web_repair(tmp_path, monkeypatch):
    assert platform.system() == "Darwin", "Strict controller sandbox must be available; this test must not claim a skipped workflow passed"
    assert importlib.metadata.version("openhands-sdk") == "1.49.2"
    assert (ROOT / "apps/dashboard/.playwright-browsers").is_dir(), "Install the pinned browser for independent acceptance"
    entry = os.environ.get('AGENTFLOW_E2E_ENTRY', 'all')
    assert entry in {'all', 'cli'}, 'Select all (the default full suite) or the explicit CLI-only slice'
    targets = ('api',) if entry == 'cli' else ('api', 'web')
    (tmp_path / 'e2e-selection.json').write_text(json.dumps({'entry': entry, 'targets': targets,
        'complete_default_suite': entry == 'all'}, indent=2))
    isolated_home = tmp_path / "home"
    owner_home = Path.home()
    source_caches = [owner_home / '.npm', owner_home / 'Library/Application Support/AgentFlow/package_cache/npm']
    if extra_cache := os.environ.get('AGENTFLOW_E2E_NPM_CACHE'):
        source_caches.append(Path(extra_cache))
    cached = seed_locked_npm_cache(tmp_path / 'controller/package_cache/npm',
        source_caches,
        [ROOT / 'src/agentflow/resources/web_api_starter/package-lock.json',
         ROOT / 'src/agentflow/resources/local_reference/web_api/package-lock.json'])
    print('goal-e2e: integrity-verified local dependency archives:', len(cached), flush=True)
    isolated_home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "apps/dashboard/.playwright-browsers"))
    browser = ROOT / "apps/dashboard/.playwright-browsers/chromium-1243/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    assert browser.is_file()
    monkeypatch.setenv("AGENTFLOW_BROWSER_EXECUTABLE", str(browser))
    dashboard = tmp_path / "compiled-dashboard"
    build = await asyncio.create_subprocess_exec("npm", "run", "build", "--", "--outDir", str(dashboard),
        cwd=ROOT / "apps/dashboard", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    build_out, build_err = await asyncio.wait_for(build.communicate(), 90)
    assert build.returncode == 0, build_out.decode() + build_err.decode()
    assert (dashboard / "index.html").is_file()
    monkeypatch.setenv("AGENTFLOW_E2E_MODEL_KEY", PROVIDER_SECRET)
    fixture = ScriptedModel()
    with fixture.serve() as model_origin:
        data_dir = tmp_path / "controller"
        store = Store(data_dir)
        await store.start()
        try:
            assert await store.list("model_profile") == []
            assert await store.list("work_item") == []
            assert await store.list("check") == []
            assert await store.list("candidate") == []
        finally:
            await store.close()
        reserved = socket.socket()
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        reserved.close()
        (isolated_home / ".config").mkdir(mode=0o700)
        config_path = isolated_home / ".config/agentflow/config.toml"
        config_path.parent.mkdir(mode=0o700)
        config_text = "[app]\n" + f"data_dir = {json.dumps(str(data_dir))}\nport = {port}\n"
        config_text += (f"dashboard_dir = {json.dumps(str(dashboard))}\n\n[product]\n"
            + 'target = "api"\nreview_mode = "auto"\nmax_model_requests = 0\n'
            + f"output_root = {json.dumps(str(tmp_path / 'products'))}\n")
        for role, model in (("roles", ROLE_MODEL), ("coding", MODEL)):
            config_text += (f"\n[models.{role}]\nprovider = \"local_test\"\n"
                + f"base_url = {json.dumps(model_origin)}\nmodel = {json.dumps(model)}\n"
                + 'api_key_env = "AGENTFLOW_E2E_MODEL_KEY"\nallow_loopback_upstream = true\n'
                + 'max_output_tokens = 8192\n')
        config_path.write_text(config_text)
        config_path.chmod(0o600)
        configuration = load_configuration()
        assert configuration.product.max_model_requests == 0
        settings = configuration.settings
        application = await Application(settings, configuration=configuration).start()
        assert len(await application.store.list("model_profile")) == 2
        server = ManagedServer(uvicorn.Config(application.owner_app, host=settings.host, port=port,
            proxy_headers=False, access_log=False, log_level="warning", lifespan="off"))
        server_task = asyncio.create_task(server.serve())
        try:
            async with asyncio.timeout(15):
                while not server.started:
                    assert not server_task.done(), "Owner listener exited before readiness"
                    await asyncio.sleep(.025)
            publish_instance(settings.data_dir)
            async with httpx.AsyncClient(base_url=settings.origin, trust_env=False, follow_redirects=False, timeout=30) as client:
                headers = await owner_session(data_dir, client, settings.origin)
                outcomes = []
                for target in targets:
                    outcomes.append(await exercise_product(application, fixture, client, headers, settings, tmp_path / target, target))
                if entry == 'all':
                    assert outcomes[0]["product"]["run_id"] != outcomes[1]["product"]["run_id"]
                    assert outcomes[0]["candidate_fingerprint"] != outcomes[1]["candidate_fingerprint"]
                    assert outcomes[0]["check_ids"].isdisjoint(outcomes[1]["check_ids"])
                accounts = await application.store.list('budget_account')
                assert accounts and all(account['max_requests'] == 0 and account['request_count'] > 0 for account in accounts)
        finally:
            await diagnostics(application, fixture, tmp_path)
            server.should_exit = True
            await asyncio.wait_for(server_task, 15)
            await application.close()
            clear_instance()
