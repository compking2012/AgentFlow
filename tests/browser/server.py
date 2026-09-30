"""Browser-test service only. Fixture records never enter the production application.

The UI uses the unmodified owner API, real SQLite transactions, local artifacts,
and real temporary Git repositories. No model/scheduler/provider is instantiated.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import socket
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse

from agentflow.common import canonical_digest, utc_now
from agentflow.control.api import create_app
from agentflow.domain.planning import ROLES, STEPS, output_fingerprint
from agentflow.execution.manifests import file_digest
from agentflow.execution.transport import ArtifactTransport
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store
from agentflow.testing.reports import parse_junit, parse_playwright


async def main(directory: Path, executor: bool = False):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    root = Path(__file__).resolve().parents[2]
    settings = Settings(data_dir=directory / "data", port=port, dashboard_dir=Path(os.environ.get("AGENTFLOW_BROWSER_DASHBOARD_DIR", str(root / "src/agentflow/web"))))
    store = Store(settings.data_dir)
    await store.start()
    artifacts = LocalArtifactStore(settings.data_dir / "artifacts")
    node_service = None
    node_fingerprint = None
    if executor:
        from agentflow.execution.pki import create_node_key_and_csr
        from agentflow.execution.service import NodeService

        node_service = NodeService(store, settings.data_dir, "https://127.0.0.1:9443")
        _, node_fingerprint = create_node_key_and_csr(directory / "node-key", "browser test node")
        settings = Settings(**{**settings.model_dump(), "executor_host": "127.0.0.1",
            "executor_origin": node_service.executor_origin, "tls_certificate": node_service.pki.server_cert_path,
            "tls_private_key": node_service.pki.server_key_path, "tls_client_ca": node_service.pki.directory / "ca.pem"})
    app = create_app(settings, store=store, artifacts=artifacts, node_service=node_service)
    project = await app.state.workflow.create_project({"name": "界面验收参考项目",
        "local_path": str(directory / "reference-project"), "import_mode": "initialize_managed",
        "dirty_worktree_policy": "require_clean"}, "browser-fixture-project")
    ids = {key: str(uuid4()) for key in ("run", "plan", "iteration", "item", "approval", "artifact", "profile", "candidate", "delivery")}
    run_fingerprint = canonical_digest({"fixture": "run"})
    candidate_fingerprint = canonical_digest({"fixture": "candidate"})
    entries = [{"matrix_entry_id": str(uuid4()), "test_case_id": str(uuid4()), "app_target": target,
                "target_config_id": str(uuid4()), "target_config_revision": 1, "required": True,
                "component_roles": ["product", "test"]} for target in ("web", "web", "api", "api")]
    check_ids = [str(uuid4()) for _ in entries]
    raw = await artifacts.put_bytes("# Review 报告\n权限规则需要修复。\n<script>window.__artifactExecuted=true</script>".encode())
    item = {"run_id": ids["run"], "project_id": project["id"], "step": "code_review", "role": "review",
        "key": "review", "generation": 1, "status": "waiting_approval", "quality_result": "failed",
        "fencing_token": 1, "input_fingerprint": canonical_digest({"fixture": "review-input"}),
        "policy_fingerprint": canonical_digest({"approval": True}), "required": True,
        "approval_required": True, "dependencies": [], "write_paths": [],
        "artifact_ids": [ids["artifact"]], "attempt_id": None}
    artifact = {"id": ids["artifact"], "digest": raw["id"], "size": raw["size"], "step": "code_review",
        "name": "权限审查报告", "content_type": "text/markdown", "stale": False,
        "work_item_id": ids["item"], "run_id": ids["run"], "created_at": utc_now()}

    def seed(tx):
        tx.put("model_profile", ids["profile"], {"model_profile_id": ids["profile"], "name": "测试配置（未接受）",
            "provider": "deepseek", "requested_api_model": "deepseek-flash", "accepted_api_model": None,
            "acceptance_status": "pending_user_confirmation", "credential_status": "missing",
            "protocols": ["chat_completions", "responses"]})
        tx.put("plan", ids["plan"], {"plan_id": ids["plan"], "project_id": project["id"],
            "goal": "检查权限规则", "actual_steps": ["code_review"], "state": "started",
            "missing_inputs": [], "work_specs": [], "base_commit": project["base_commit"],
            "app_targets": ["web", "api"], "target_configs": [],
            "input_fingerprint": canonical_digest({"fixture": "plan"})})
        tx.put("run", ids["run"], {"run_id": ids["run"], "plan_id": ids["plan"],
            "project_id": project["id"], "iteration_id": ids["iteration"], "goal": "检查权限规则",
            "purpose": "diagnostic", "execution_state": "running", "quality_result": "failed",
            "runtime_bindings": {}, "input_fingerprint": run_fingerprint,
            "budget_limit": {"currency": "CNY", "limit_micros": 20_000_000}, "delivery_ids": [],
            "blocking_reasons": ["代码审查发现权限规则缺陷"], "created_at": utc_now()})
        actual_artifact = tx.put("artifact", ids["artifact"], artifact)
        initial = {**item, "id": ids["item"]}
        fingerprint = output_fingerprint(initial, [actual_artifact])
        tx.put("work_item", ids["item"], {**item, "output_fingerprint": fingerprint})
        tx.put("approval", ids["approval"], {"run_id": ids["run"], "work_item_id": ids["item"],
            "fingerprint": fingerprint, "generation": 1, "decision": None, "stale": False})
        tx.put("candidate", ids["candidate"], {"run_id": ids["run"], "run_input_fingerprint": run_fingerprint,
            "fingerprint": candidate_fingerprint, "source_commit": project["base_commit"], "state": "source_frozen"})
        tx.put("target_matrix", ids["candidate"], {"run_id": ids["run"], "state": "planned",
            "plan": {"entries": entries, "required_app_targets": ["web", "api"]},
            "plan_fingerprint": canonical_digest(entries), "required_count": len(entries)})
        tx.event("fixture.ready", {"work_item_id": ids["item"]}, run_id=ids["run"])
        return {"approval_id": ids["approval"], "fingerprint": fingerprint}

    seeded = await store.command("browser.fixture", "seed", {}, seed)
    fixture_key = secrets.token_urlsafe(32)
    trace_attempt_id = str(uuid4())

    @app.post('/__fixture/trace')
    async def trace_fixture(request: Request):
        if request.headers.get('x-fixture-key') != fixture_key:
            return JSONResponse({}, 403)
        from agentflow.runtime.trace import ExecutionTrace
        mode = (await request.json()).get('mode', 'initial')
        if mode not in {'initial', 'append'}:
            return JSONResponse({}, 422)
        if mode == 'initial':
            def prepare(tx):
                current = tx.get('work_item', ids['item'])
                tx.put('attempt', trace_attempt_id, {'run_id': ids['run'], 'iteration_id': ids['iteration'],
                    'work_item_id': ids['item'], 'generation': current['generation'],
                    'fencing_token': current['fencing_token'], 'input_fingerprint': current['input_fingerprint'],
                    'status': 'running', 'started_at': utc_now(), 'fixture_scope': 'browser_trace_contract_only'})
                return tx.put('work_item', current['id'], {**current, 'attempt_id': trace_attempt_id,
                    'status': 'running', 'quality_result': 'unknown'}, current['revision'])
            await store.command('browser.fixture.trace', 'initial', {}, prepare)
            traces = ExecutionTrace(store)
            await traces.emit(trace_attempt_id, 'instruction', '真实接口任务指令', {
                'system': '按当前目标只读核查', 'task': '检查权限边界', 'api_key': 'fixture-sensitive-value',
                'reasoning': 'PRIVATE_HIDDEN_REASONING'}, key='trace-fixture-instruction')
            await traces.emit(trace_attempt_id, 'llm_output', '真实接口可见回复', {
                'summary': '发现一项需要核对的权限边界', 'reasoning_content': 'PRIVATE_HIDDEN_REASONING'},
                key='trace-fixture-output', model='fixture-model')
        else:
            traces = ExecutionTrace(store)
            await traces.emit(trace_attempt_id, 'tool_call', '真实接口工具操作', {'tool': 'read_code', 'path': 'src/auth.ts'},
                              key='trace-fixture-tool', call_id='fixture-tool-call')
            await traces.emit(trace_attempt_id, 'tool_result', '真实接口工具返回', {'output': '已读取指定文件',
                'access_token': 'fixture-sensitive-token'}, key='trace-fixture-result', duration_ms=12)
        return {'attempt_id': trace_attempt_id, 'run_id': ids['run'], 'work_item_id': ids['item'],
                'fixture_scope': 'browser_trace_contract_only'}

    @app.get("/__fixture/state")
    async def fixture_state(request: Request):
        if request.headers.get("x-fixture-key") != fixture_key:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return {"approval": await store.read("approval", ids["approval"]),
                "item": await store.read("work_item", ids["item"]), "plans": await store.list("plan"),
                "projects": await store.list("project"), "decisions": await store.list("approval_decision"),
                "pairings": await store.list("node_pairing")}

    @app.post("/__fixture/supersede")
    async def supersede(request: Request):
        if request.headers.get("x-fixture-key") != fixture_key:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        await app.state.workflow.revise(ids["run"], {"expected_revision": (await store.read("run", ids["run"]))["revision"],
            "work_item_ids": [ids["item"]], "reason": "并发输入版本变化"}, "fixture-supersede")
        return {"ok": True}

    @app.post('/__fixture/request_count')
    async def request_count_display(request: Request):
        if request.headers.get('x-fixture-key') != fixture_key:
            return JSONResponse({}, 403)
        value = (await request.json()).get('max_model_requests')
        if type(value) is not int or not 0 <= value <= 2000:
            return JSONResponse({}, 422)
        def update(tx):
            current = tx.get('run', ids['run'])
            # Display metadata only. No account, scheduler, attempt, or quality
            # state is altered by this dedicated browser fixture route.
            return tx.put('run', current['id'], {**current, 'budget_limit': {
                **current['budget_limit'], 'cost_mode': 'request_limited',
                'max_model_requests': value, 'limit_micros': 0}}, current['revision'])
        return await store.command('browser.fixture.request_count', str(uuid4()), {'value': value}, update)

    @app.post("/__fixture/quality")
    async def quality(request: Request):
        if request.headers.get("x-fixture-key") != fixture_key:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        payload = await request.json()
        mode = payload.get("mode", "passed")
        if mode not in {"wrong_fingerprint", "partial", "unverified", "empty", "passed", "retired", "measured"}:
            return JSONResponse({"error": "unknown fixture mode"}, status_code=400)

        # Protocol fixtures with actual raw files/CAS/parser verification. These
        # records exercise UI evidence joins, not native or generated execution.
        transport = ArtifactTransport(store, settings.data_dir / 'nodes/artifacts')
        normalized = []
        for index, entry in enumerate(entries):
            if index % 2 == 0:
                cases = '' if mode == 'empty' else ''.join(f'<testcase name="case-{number}" time="0.1" />' for number in range(2))
                content = f'<testsuite>{cases}</testsuite>'.encode()
                suffix, parser = 'xml', parse_junit
            else:
                specs = []
                for number in range(0 if mode == 'empty' else 2):
                    result = {'status': 'failed' if mode == 'partial' and index == 3 and number == 0 else 'passed', 'duration': 100}
                    if mode == 'measured' and index == 3 and number == 0:
                        body = {'metrics': [{'name': '接口响应 P95', 'value': 42.5, 'unit': 'ms', 'sample_count': 20}]}
                        result['attachments'] = [{'contentType': 'application/vnd.agentflow.performance+json',
                            'body': base64.b64encode(json.dumps(body).encode()).decode()}]
                    specs.append({'title': f'case-{number}', 'tests': [{'projectName': 'fixture', 'results': [result]}]})
                content = json.dumps({'suites': [{'title': 'integration-fixture', 'specs': specs}]}).encode()
                suffix, parser = 'json', parse_playwright
            report_file = directory / f'quality-fixture-{uuid4()}.{suffix}'
            report_file.write_bytes(content)
            raw_record = await transport.register_input(report_file, file_digest(report_file))
            report = parser(transport.object_path(raw_record['digest'])).model_dump(mode='json')
            normalized.append((raw_record, report, str(uuid4())))

        def update(tx):
            candidate = tx.get("candidate", ids["candidate"])
            tx.put("candidate", candidate["id"], {**candidate, "state": "platform_artifacts_frozen",
                'matrix_mappings': {entry['matrix_entry_id']: {'phase': 'unit' if index % 2 == 0 else 'integration',
                    'target_config_id': entry['target_config_id']} for index, entry in enumerate(entries)},
                "run_input_fingerprint": canonical_digest({"fixture": "old-run"}) if mode == "retired" else run_fingerprint}, candidate["revision"])
            matrix = tx.get("target_matrix", ids["candidate"])
            tx.put("target_matrix", matrix["id"], {**matrix, "state": "bound_to_platform_manifest",
                "candidate_fingerprint": candidate_fingerprint, "binding": {"candidate_fingerprint": candidate_fingerprint}}, matrix["revision"])
            for index, entry in enumerate(entries):
                raw_record, report, result_id = normalized[index]
                if not (mode == 'partial' and index == 1):
                    tx.put('node_result', result_id, {'assessment_state': 'validated',
                        'fixture_scope': 'browser_report_contract_not_real_execution', 'verified_checks': [{
                            'matrix_entry_id': entry['matrix_entry_id'], 'raw_report_artifact_version_id': raw_record['id'],
                            'normalized_report': report}]})
                check = tx.get("check", check_ids[index])
                tx.put("check", check_ids[index], {"run_id": ids["run"], "work_item_id": ids["item"],
                    "matrix_entry_id": entry["matrix_entry_id"], "execution_key": canonical_digest(entry),
                    "candidate_fingerprint": canonical_digest({"fixture": "old-candidate"}) if mode == "wrong_fingerprint" else candidate_fingerprint,
                    "execution_status": "not_run" if mode == "partial" and index == 1 else "completed",
                    "quality_result": "failed" if mode == "partial" and index == 3 else "passed",
                    "evidence_verified": mode != "unverified", "executed_case_count": 0 if mode == "empty" else 2,
                    "raw_report_artifact_id": raw_record['id'], "node_result_id": result_id,
                    "framework_case_evidence": True}, check["revision"] if check else None)
            if payload.get("delivery"):
                tx.put("delivery", ids["delivery"], {"run_id": ids["run"], "operation_id": ids["delivery"],
                    "candidate_id": ids["candidate"], "candidate_fingerprint": candidate_fingerprint,
                    "commit_oid": project["base_commit"], "tree_oid": "a" * 40, "base_oid": project["base_commit"],
                    "delivery_ref": f"refs/heads/codex/agentflow/{ids['run']}", "confirmed_at": utc_now()})
            tx.event("fixture.quality_changed", {"mode": mode}, run_id=ids["run"])
            return {"ok": True}

        return await store.command("browser.fixture.quality", str(uuid4()), payload, update)

    @app.post('/__fixture/workflow')
    async def expanded_workflow(request: Request):
        if request.headers.get('x-fixture-key') != fixture_key:
            return JSONResponse({}, 403)
        research_id, market_id, rival_id, code_id, review_json_id = (str(uuid4()) for _ in range(5))
        code_directory = directory / 'fixture-product-code'
        (code_directory / 'tests').mkdir(parents=True)
        (code_directory / 'tests/unit.test.mjs').write_text('// UI directory fixture, never executed.\n')
        definitions = [
            (market_id, 'research', '用户与市场调研', {'content': '# 市场调研\n\n用户需要一个稳定的阅读清单。'}, research_id),
            (rival_id, 'research', '竞品功能分析', {'content': '# 竞品分析\n\n当前信息来自测试夹具，不是在线调研。'}, research_id),
            (research_id, 'research', 'research', {'summary': '独立调研已汇总。', 'content': '## 范围\n\n- 阅读清单\n- 状态筛选\n\n| 项目 | 结果 |\n| --- | --- |\n| 持久化 | 必需 |\n\n<script>window.__workflowExecuted=true</script>\n\n[不安全链接](javascript:alert(1))'}, None),
            (code_id, 'unit_test_implementation', 'unit-code', {'summary': '测试代码目录说明。'}, None),
        ]
        prepared = []
        for identity, step, name, content, parent_id in definitions:
            blob = await artifacts.put_bytes(json.dumps(content, ensure_ascii=False).encode())
            prepared.append((identity, step, name, parent_id, blob, str(uuid4())))
        findings = {'summary': '独立代码审查', 'findings': [
            {'description': '接口缺少调用者校验', 'severity': 'high', 'category': 'security', 'path': 'src/api.mjs'},
            {'description': '历史未分类事项', 'severity': 'warning', 'path': 'src/legacy.mjs'},
        ]}
        review_blob = await artifacts.put_bytes(json.dumps(findings, ensure_ascii=False).encode())
        def seed_work(tx):
            current_run = tx.get('run', ids['run'])
            tx.put('run', current_run['id'], {**current_run, 'budget_limit': {'cost_mode': 'request_limited',
                'currency': 'USD', 'limit_micros': 0, 'max_model_requests': 99}}, current_run['revision'])
            for identity, step, name, parent_id, blob, artifact_id in prepared:
                task = {**item, 'step': step, 'role': 'research' if step == 'research' else 'unit_test',
                    'key': name, 'status': 'completed', 'quality_result': 'passed', 'artifact_ids': [artifact_id],
                    'approval_required': False, 'attempt_id': identity + '-attempt'}
                if parent_id:
                    task.update(parent_stage_id=parent_id, payload={'goal': name})
                if identity == research_id:
                    task.update(kind='aggregation', dependencies=[market_id, rival_id], original_dependencies=[])
                if identity == code_id:
                    task['dependencies'] = [ids['item']]
                tx.put('work_item', identity, task)
                tx.put('artifact', artifact_id, {'run_id': ids['run'], 'work_item_id': identity, 'generation': 1,
                    'digest': blob['id'], 'size': blob['size'], 'media_type': 'application/json', 'name': 'openhands_final.json', 'step': step})
                if identity == code_id:
                    tx.put('code_snapshot', identity + '-attempt', {'work_item_id': identity, 'generation': 1,
                        'repository_path': str(code_directory), 'commit_oid': project['base_commit'], 'stale': False})
            current = tx.get('work_item', ids['item'])
            tx.put('artifact', review_json_id, {'run_id': ids['run'], 'work_item_id': current['id'], 'generation': 1,
                'digest': review_blob['id'], 'size': review_blob['size'], 'media_type': 'application/json',
                'name': 'openhands_final.json', 'step': 'code_review'})
            tx.put('work_item', current['id'], {**current, 'dependencies': [research_id],
                'artifact_ids': [*current['artifact_ids'], review_json_id]}, current['revision'])
            tx.put('review', 'fixture-review', {'work_item_id': current['id'], 'generation': 1, 'blocking_findings': []})
            tx.event('fixture.workflow', {'scope': 'browser_contract_only'}, run_id=ids['run'])
            return {'research_id': research_id, 'code_directory': str(code_directory)}
        return await store.command('browser.fixture.workflow', str(uuid4()), {}, seed_work)

    full_stage_ids = {step: str(uuid4()) for step in STEPS}

    @app.post('/__fixture/full_workflow')
    async def full_workflow(request: Request):
        if request.headers.get('x-fixture-key') != fixture_key:
            return JSONResponse({}, 403)
        # Mixed statuses are deliberate GUI contract data, never an execution
        # or quality acceptance claim. Dependency paths are persisted as usual.
        outputs = {}
        for step in STEPS[:6]:
            outputs[step] = await artifacts.put_bytes(json.dumps({'summary': '工作流图的可读产物夹具。',
                'content': '## 范围\n\n这份文档只用于 GUI 行为验证。'}, ensure_ascii=False).encode())
        dependencies = {step: [full_stage_ids[STEPS[index - 1]]] if index else [] for index, step in enumerate(STEPS)}
        dependencies['integration_test_strategy'].append(full_stage_ids['architecture'])
        def seed_graph(tx):
            old = tx.get('work_item', ids['item'])
            tx.put('work_item', old['id'], {**old, 'archived': True}, old['revision'])
            for index, step in enumerate(STEPS):
                status = 'completed' if index < 6 else 'running' if step == 'implementation' else (
                    'failed' if step == 'code_review' else 'waiting_approval' if step == 'unit_test_plan' else
                    'blocked' if step == 'unit_test_implementation' else 'pending')
                body = {**item, 'step': step, 'role': ROLES[step], 'key': step, 'status': status,
                    'quality_result': 'passed' if status == 'completed' else 'failed' if status == 'failed' else 'unknown',
                    'approval_required': status == 'waiting_approval', 'dependencies': dependencies[step],
                    'artifact_ids': [], 'attempt_id': None}
                if step == 'code_review':
                    body['blocking_reason'] = 'review_failed'
                elif step == 'unit_test_implementation':
                    body['blocking_reason'] = 'work_blocked'
                if step in outputs:
                    blob = outputs[step]
                    artifact_id = str(uuid4())
                    tx.put('artifact', artifact_id, {'run_id': ids['run'], 'work_item_id': full_stage_ids[step],
                        'generation': 1, 'step': step, 'name': 'openhands_final.json', 'media_type': 'application/json',
                        'digest': blob['id'], 'size': blob['size']})
                    body['artifact_ids'] = [artifact_id]
                tx.put('work_item', full_stage_ids[step], body)
            plan = tx.get('plan', ids['plan'])
            tx.put('plan', plan['id'], {**plan, 'actual_steps': list(STEPS)}, plan['revision'])
            tx.event('fixture.full-workflow', {'scope': 'GUI_contract_only'}, run_id=ids['run'])
            return {'stage_ids': full_stage_ids, 'dependencies': dependencies}
        return await store.command('browser.fixture.full-workflow', str(uuid4()), {}, seed_graph)

    @app.post('/__fixture/workflow_state')
    async def workflow_state(request: Request):
        if request.headers.get('x-fixture-key') != fixture_key:
            return JSONResponse({}, 403)
        values = await request.json()
        if values.get('step') not in full_stage_ids or values.get('status') not in {'pending', 'running', 'waiting_execution', 'completed', 'failed', 'blocked'}:
            return JSONResponse({}, 422)
        def update(tx):
            work = tx.get('work_item', full_stage_ids[values['step']])
            result = tx.put('work_item', work['id'], {**work, 'status': values['status'],
                'quality_result': 'passed' if values['status'] == 'completed' else 'failed' if values['status'] == 'failed' else 'unknown',
                'blocking_reason': values.get('blocking_reason')}, work['revision'])
            tx.event('fixture.workflow-updated', {'work_item_id': work['id']}, run_id=ids['run'])
            return result
        return await store.command('browser.fixture.workflow-state', str(uuid4()), values, update)

    repair_stage_ids = {key: str(uuid4()) for key in ('implementation', 'code_review', 'integration_test_implementation')}

    @app.post('/__fixture/logical_repairs')
    async def logical_repairs(request: Request):
        if request.headers.get('x-fixture-key') != fixture_key:
            return JSONResponse({}, 403)
        mode = (await request.json()).get('status', 'completed')
        if mode not in {'completed', 'running', 'failed'}:
            return JSONResponse({}, 422)
        def seed(tx):
            for step, identity in full_stage_ids.items():
                old = tx.get('work_item', identity)
                tx.put('work_item', identity, {**old, 'status': 'completed', 'quality_result': 'passed',
                       'approval_required': False, 'blocking_reason': None}, old['revision'])
            old = tx.get('work_item', full_stage_ids['code_review'])
            tx.put('work_item', old['id'], {**old, 'quality_result': 'failed', 'blocking_reason': 'review_failed'}, old['revision'])
            parent = full_stage_ids['integration_test_implementation']
            for step, identity in repair_stage_ids.items():
                existing = tx.get('work_item', identity)
                body = {**item, 'key': 'repair-' + step, 'step': step, 'role': ROLES[step],
                    'status': mode if step == 'implementation' else 'completed',
                    'quality_result': 'failed' if mode == 'failed' and step == 'implementation' else 'passed',
                    'approval_required': False, 'dependencies': [parent], 'artifact_ids': [], 'attempt_id': None}
                tx.put('work_item', identity, body, existing['revision'] if existing else None)
                parent = identity
            tx.event('fixture.logical-repairs', {'scope': 'GUI_contract_only'}, run_id=ids['run'])
            return {'repair_ids': repair_stage_ids, 'stage_ids': full_stage_ids}
        return await store.command('browser.fixture.logical-repairs', str(uuid4()), {}, seed)

    mixed_children = {'failed': str(uuid4()), 'live': str(uuid4())}

    @app.post('/__fixture/repair_context')
    async def repair_context(request: Request):
        if request.headers.get('x-fixture-key') != fixture_key:
            return JSONResponse({}, 403)
        values = await request.json()
        mode = values.get('mode', 'failed')
        if mode not in {'failed', 'pending', 'running', 'passed', 'rebound', 'own_phase'}:
            return JSONResponse({}, 422)
        blob = await artifacts.put_bytes(json.dumps({'summary': '修复复审夹具',
            'content': '当前修复复审报告。'}, ensure_ascii=False).encode())
        def seed(tx):
            for step, identity in full_stage_ids.items():
                old = tx.get('work_item', identity)
                tx.put('work_item', identity, {**old, 'status': 'completed', 'quality_result': 'passed',
                    'approval_required': False, 'blocking_reason': None}, old['revision'])
            for step in ('code_review', 'unit_test_plan', 'integration_test_strategy'):
                work = tx.get('work_item', full_stage_ids[step])
                if not work['artifact_ids']:
                    artifact_id = str(uuid4())
                    tx.put('artifact', artifact_id, {'run_id': ids['run'], 'work_item_id': work['id'],
                        'generation': work['generation'], 'created_at': '2026-09-01T00:00:00+00:00',
                        'digest': blob['id'], 'name': 'openhands_final.json', 'media_type': 'application/json'})
                    tx.put('work_item', work['id'], {**work, 'artifact_ids': [artifact_id],
                        'quality_result': 'passed' if step == 'code_review' else 'unknown'}, work['revision'])
            parent = full_stage_ids['integration_test_implementation']
            for step in ('implementation', 'code_review'):
                identity = repair_stage_ids[step]
                previous = tx.get('work_item', identity)
                body = {**item, 'key': 'product-repair-' + identity, 'step': step, 'role': ROLES[step],
                    'status': mode if step == 'code_review' and mode in {'pending', 'running'} else 'completed',
                    'quality_result': mode if step == 'code_review' and mode in {'failed', 'passed'} else 'unknown',
                    'approval_required': False, 'dependencies': [parent], 'artifact_ids': [], 'attempt_id': None,
                    'payload': {'product_frozen_repair': True} if step == 'implementation' else {}}
                tx.put('work_item', identity, body, previous['revision'] if previous else None)
                parent = identity
            receipt = tx.get('product_test_repair', repair_stage_ids['implementation'])
            tx.put('product_test_repair', repair_stage_ids['implementation'], {'run_id': ids['run'],
                'repair_work_item_id': repair_stage_ids['implementation'], 'review_work_item_id': repair_stage_ids['code_review'],
                'affected_work_item_ids': [], 'created_at': '2026-09-02T00:00:00+00:00'}, receipt['revision'] if receipt else None)
            if mode == 'rebound':
                producer = tx.get('work_item', repair_stage_ids['implementation'])
                tx.put('work_item', producer['id'], {**producer, 'archived': True}, producer['revision'])
                owner = tx.get('work_item', full_stage_ids['unit_test_implementation'])
                tx.put('work_item', owner['id'], {**owner, 'status': 'pending', 'quality_result': 'unknown'}, owner['revision'])
                original_id = 'original-unit-review'
                original = {**item, 'id': original_id, 'key': 'unit_test_implementation:review', 'step': 'code_review',
                    'role': 'review', 'dependencies': [owner['id']], 'status': 'completed', 'quality_result': 'passed',
                    'artifact_ids': [], 'generation': 1, 'attempt_id': None, 'approval_required': False}
                tx.put('work_revision', 'initial-unit-review', {'work_item_id': original_id, 'snapshot': original})
                tx.put('work_item', original_id, {**original, 'status': 'pending', 'quality_result': 'unknown', 'generation': 2})
                review = tx.get('work_item', repair_stage_ids['code_review'])
                tx.put('work_item', review['id'], {**review, 'dependencies': [owner['id']],
                    'status': 'pending', 'quality_result': 'unknown'}, review['revision'])
                tx.put('review_repair', 'late-test-owner', {'run_id': ids['run'], 'mode': 'late_test_owner',
                    'review_work_item_id': review['id'], 'producer_work_item_id': producer['id'],
                    'repair_work_item_ids': [owner['id']], 'owner_stage_work_item_ids': [owner['id']],
                    'review_bindings': {review['id']: {'dependencies': [owner['id']], 'minimum_generation': review['generation']}},
                    'affected_work_item_ids': [owner['id'], original_id, review['id']],
                    'created_at': '2026-09-03T00:00:00+00:00'})
            if mode == 'own_phase':
                producer = tx.get('work_item', repair_stage_ids['implementation'])
                tx.put('work_item', producer['id'], {**producer, 'archived': True}, producer['revision'])
                unit_review = tx.get('work_item', 'original-unit-review')
                tx.put('work_item', unit_review['id'], {**unit_review, 'status': 'completed',
                    'quality_result': 'passed'}, unit_review['revision'])
                owner = tx.get('work_item', full_stage_ids['integration_test_implementation'])
                tx.put('work_item', owner['id'], {**owner, 'status': 'pending', 'quality_result': 'unknown'}, owner['revision'])
                original_id = 'original-integration-review'
                children = ['integration-review-facet-' + str(index) for index in range(4)]
                for child_id in children:
                    tx.put('work_item', child_id, {**item, 'key': child_id, 'step': 'code_review', 'role': 'review',
                        'parent_stage_id': original_id, 'dependencies': [owner['id']], 'status': 'pending',
                        'quality_result': 'unknown', 'artifact_ids': [], 'generation': 2, 'attempt_id': None})
                original = {**item, 'id': original_id, 'key': 'integration_test_implementation:review',
                    'step': 'code_review', 'role': 'review', 'kind': 'aggregation', 'dependencies': children,
                    'original_dependencies': [owner['id']], 'status': 'completed', 'quality_result': 'passed',
                    'artifact_ids': [], 'generation': 1, 'attempt_id': None, 'approval_required': False}
                tx.put('work_revision', 'initial-integration-review', {'work_item_id': original_id, 'snapshot': original})
                tx.put('work_item', original_id, {**original, 'status': 'pending', 'quality_result': 'unknown', 'generation': 2})
                review = tx.get('work_item', repair_stage_ids['code_review'])
                tx.put('work_item', review['id'], {**review, 'dependencies': [owner['id']],
                    'status': 'pending', 'quality_result': 'unknown', 'generation': 3}, review['revision'])
                tx.put('review_repair', 'integration-own-phase', {'run_id': ids['run'], 'mode': 'late_test_owner',
                    'routing_kind': 'original_test_phase', 'review_work_item_id': original_id,
                    'full_source_review_work_item_id': review['id'], 'owner_stage_work_item_ids': [owner['id']],
                    'review_bindings': {original_id: {'dependencies': children, 'minimum_generation': 2},
                                       review['id']: {'dependencies': [owner['id']], 'minimum_generation': 3}},
                    'affected_work_item_ids': [owner['id'], original_id, review['id'], *children],
                    'created_at': '2026-09-04T00:00:00+00:00'})
            return {'stage_ids': full_stage_ids, 'repair_ids': repair_stage_ids}
        return await store.command('browser.fixture.repair-context', str(uuid4()), values, seed)

    review_children = {'finding': str(uuid4()), 'passed': str(uuid4())}

    @app.post('/__fixture/review_workflow')
    async def review_workflow(request: Request):
        if request.headers.get('x-fixture-key') != fixture_key:
            return JSONResponse({}, 403)
        values = await request.json()
        mode = values.get('mode', 'failed')
        if mode not in {'failed', 'unknown', 'passed', 'pending', 'running', 'waiting_approval'}:
            return JSONResponse({}, 422)
        # Isolated presentation fixtures: no scheduler or execution is invoked.
        status = mode if mode in {'pending', 'running', 'waiting_approval'} else 'completed'
        quality = 'failed' if mode == 'waiting_approval' else mode if mode in {'failed', 'passed'} else 'unknown'
        def update(tx):
            parent = tx.get('work_item', full_stage_ids['code_review'])
            tx.put('work_item', parent['id'], {**parent, 'kind': 'aggregation', 'status': status,
                'quality_result': quality, 'dependencies': list(review_children.values()),
                'blocking_reason': 'review_failed' if quality == 'failed' else None,
                'original_dependencies': parent.get('original_dependencies', parent['dependencies'])}, parent['revision'])
            for kind, name in [('finding', '权限边界审查'), ('passed', '数据一致性审查')]:
                existing = tx.get('work_item', review_children[kind])
                body = {**item, 'parent_stage_id': parent['id'], 'key': name, 'payload': {'goal': name},
                    'status': status if kind == 'finding' else 'completed',
                    'quality_result': quality if kind == 'finding' else 'passed',
                    'blocking_reason': 'review_failed' if kind == 'finding' and quality == 'failed' else None,
                    'approval_required': status == 'waiting_approval', 'artifact_ids': [],
                    'dependencies': parent.get('original_dependencies', []), 'attempt_id': None}
                tx.put('work_item', review_children[kind], body, existing['revision'] if existing else None)
            tx.event('fixture.review-workflow', {'scope': 'GUI_contract_only'}, run_id=ids['run'])
            return {'stage_id': parent['id'], 'mode': mode}
        return await store.command('browser.fixture.review-workflow', str(uuid4()), values, update)

    @app.post('/__fixture/workflow_mixed')
    async def workflow_mixed(request: Request):
        if request.headers.get('x-fixture-key') != fixture_key:
            return JSONResponse({}, 403)
        value = await request.json()
        active_status = value.get('active_status', 'running')
        if active_status not in {'running', 'waiting_execution', 'cancel_requested', 'completed'}:
            return JSONResponse({}, 422)
        def update(tx):
            parent = tx.get('work_item', full_stage_ids['implementation'])
            tx.put('work_item', parent['id'], {**parent, 'kind': 'aggregation', 'status': 'blocked',
                'quality_result': 'unknown', 'dependencies': list(mixed_children.values()),
                'original_dependencies': parent.get('original_dependencies', parent['dependencies'])}, parent['revision'])
            for kind, name in [('failed', '数据接口实现'), ('live', '页面交互实现')]:
                existing = tx.get('work_item', mixed_children[kind])
                status = 'failed' if kind == 'failed' else active_status
                child = {**item, 'step': 'implementation', 'role': 'development', 'key': name,
                    'parent_stage_id': parent['id'], 'payload': {'goal': name}, 'status': status,
                    'quality_result': 'failed' if kind == 'failed' else 'passed' if status == 'completed' else 'unknown',
                    'blocking_reason': 'model_output_limit' if kind == 'failed' else None,
                    'approval_required': False, 'artifact_ids': [], 'dependencies': parent.get('original_dependencies', []),
                    'attempt_id': None}
                tx.put('work_item', mixed_children[kind], child, existing['revision'] if existing else None)
            tx.event('fixture.workflow-mixed', {'scope': 'GUI_contract_only'}, run_id=ids['run'])
            return {'stage_id': parent['id'], 'active_status': active_status}
        return await store.command('browser.fixture.workflow-mixed', str(uuid4()), value, update)

    @app.post("/__fixture/large_artifact")
    async def large_artifact(request: Request):
        if request.headers.get("x-fixture-key") != fixture_key:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        content = await artifacts.put_bytes(b"x" * (1024 * 1024 + 1))

        def update(tx):
            current = tx.get("artifact", ids["artifact"])
            return tx.put("artifact", current["id"], {**current, "digest": content["id"], "size": content["size"]}, current["revision"])

        await store.command("browser.fixture.large", str(uuid4()), {}, update)
        return {"ok": True}

    @app.post("/__fixture/empty_run")
    async def empty_run(request: Request):
        if request.headers.get("x-fixture-key") != fixture_key:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        identity = str(uuid4())

        def create(tx):
            current = tx.get("run", ids["run"])
            return tx.put("run", identity, {**{key: value for key, value in current.items() if key not in {"id", "revision"}},
                "run_id": identity, "goal": "等待检查的新运行", "delivery_ids": [], "blocking_reasons": [],
                "execution_state": "queued", "quality_result": "unknown", "input_fingerprint": canonical_digest({"run": identity})})

        return await store.command("browser.fixture.empty-run", identity, {}, create)

    from project_workflow_fixture import install_project_workflow_fixture
    install_project_workflow_fixture(app, store, artifacts, directory, fixture_key)

    print(json.dumps({"origin": settings.origin, "bootstrap": app.state.tokens.bootstrap_code,
        "fixture_key": fixture_key, "directory": str(directory), "node_fingerprint": node_fingerprint,
        **ids, **seeded}), flush=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    try:
        await server.serve(sockets=[sock])
    finally:
        await store.close()
        sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--executor", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.directory, args.executor))
