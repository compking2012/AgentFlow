"""Only the model HTTP boundary is synthetic; all downstream work executes normally.

These scripted responses do not establish LLM intelligence, quality or provider
compatibility. They make the same reading-list user workflow reproducible.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
STARTER = ROOT / "src/agentflow/resources/web_api_starter"
BUSINESS = Path(__file__).with_name("fixture_product")
MODEL = "gpt-5.4"  # CLI tool metadata only; the provider is a local HTTP fixture.
ROLE_MODEL = "agentflow-protocol-role-fixture"  # Native Chat tools, with no model-name protocol heuristics.
PROVIDER_SECRET = "e2e-model-fixture-only"
GOAL = "创建阅读清单Web/API产品：图书包含书名、作者、已读状态；支持新增、编辑、删除、按已读筛选；刷新与服务进程重启后数据仍保留；空书名必须被API拒绝。"
UNIT_TITLES = ["book validation rejects blank titles", "book store supports CRUD and read filters",
               "book state persists after reopening SQLite"]
API_TITLES = ["API CRUD and read filters preserve exact business state", "API invalid title does not create a book",
              "API data remains available to a fresh request"]
WEB_TITLES = ["reader can add edit and delete a book", "read status filters and persists after reload"]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)


def context(body):
    candidates = [s for s in _strings(body) if "Current stage:" in s and "Product goal:\n" in s
                  and 'Stage input documents' in s and 'Final response contract:' in s]
    if not candidates:
        raise AssertionError("Actual runtime request did not include its workflow context")
    prompt = max(candidates, key=len)
    stage = re.search(r"Current stage: ([a-z_]+)", prompt)[1]
    if stage in {'goal', 'research', 'prd', 'requirements'}:
        assert 'Frozen target configurations:' not in prompt and 'Frozen code commit:' not in prompt
        commit, targets = None, []
    else:
        commit = re.search(r"Frozen code commit: ([a-f0-9]{40,64})", prompt)[1]
        raw = prompt.split("Frozen target configurations:\n", 1)[1]
        targets, _ = json.JSONDecoder().raw_decode(raw)
        assert {t["app_target"] for t in targets} in ({"api"}, {"web", "api"})
    repair = stage == "implementation" and "Required correction: Fix the product source so the existing frozen tests pass." in prompt
    return stage, commit, targets, repair


def case_ids(target, phase):
    if phase == "unit":
        return ["test::" + title for title in UNIT_TITLES]
    titles = API_TITLES if target == "api" else WEB_TITLES
    return [f"{target}.spec.mjs::{title}::" for title in titles]


def document(stage, commit, targets):
    if stage == "code_review":
        return {"summary": "Scripted protocol-fixture review of the exact code/test snapshot; not a live model review.",
                "reviewed_commit": commit, "findings": []}
    result = {"title": "阅读清单 / " + stage,
        "summary": "Protocol fixture: reading-list CRUD, read filtering, validation and persistence.",
        "content": "READ-01 新增和查看图书；READ-02 编辑书名、作者及已读状态；READ-03 删除；READ-04 按已读筛选；"
            "READ-05 刷新和重新打开服务后保留数据；READ-06 空书名不保存且给出明确提示。",
        "sources": [], "unknowns": ["This is a deterministic model-protocol fixture, not a real LLM or external market research result."]}
    if stage == 'goal':
        result['content'] = '为个人提供简洁阅读清单，方便记录图书、更新已读状态和筛选。支持编辑和删除，重新打开后仍能查看原记录。'
    elif stage == 'research':
        result['content'] = '尚未完成外部调研。本测试仅验证单人阅读记录、整理与回看需求；竞品能力、商业模式和真实用户反馈有待查证。'
    elif stage not in {'prd', 'requirements'}:
        result['content'] += '使用Node原生HTTP/SQLite和HTML/JS；API为/api/books与/api/books/{id}，字段id/title/author/read。'
    if stage in {"goal", "development_plan"}:
        result["parallel_work"] = []
    if stage in {"unit_test_plan", "integration_test_strategy"}:
        phase = "unit" if stage == "unit_test_plan" else "integration"
        result["test_cases"] = [{"case_id": f"{target['app_target']}-{phase}-{index + 1}",
            "requirement_id": ["READ-06", "READ-01", "READ-05"][index % 3],
            "target_config_id": target["target_config_id"], "phase": phase, "framework_case_ids": [identity]}
            for target in targets for index, identity in enumerate(case_ids(target["app_target"], phase))]
    return result


def coding_result(body, stage):
    """Honor the actual requested coding contract while retaining legacy fixtures."""
    schema = ((body.get('text') or {}).get('format') or {}).get('schema')
    if schema is None:
        schema = ((body.get('response_format') or {}).get('json_schema') or {}).get('schema')
    result = {'summary': 'Protocol fixture applied real reading-list source changes for ' + stage}
    if isinstance(schema, dict):
        fields = set(schema.get('properties', {}))
        if {'summary', 'status', 'next_action'} <= fields:
            result.update(status='complete', next_action='')
        Draft202012Validator(schema).validate(result)
    return result


def server_source():
    source = (STARTER / "src/server.mjs").read_text()
    source = source.replace("import {sendJson} from './http.mjs';", "import {sendJson, readJson} from './http.mjs';\nimport {BookStore} from './books.mjs';")
    source = source.replace("  return http.createServer(async (request, response) => {", "  const books = new BookStore();\n  const server = http.createServer(async (request, response) => {")
    old = """      // The implementation Agent replaces this branch with goal-specific APIs.
      if (url.pathname === '/api' || url.pathname.startsWith('/api/')) {
        return sendJson(response, 501, {error: 'business_not_implemented'});
      }"""
    new = """      if (url.pathname === '/api' || url.pathname.startsWith('/api/')) {
        try {
          if (url.pathname === '/api/books' && request.method === 'GET') return sendJson(response, 200, {books: books.list(url.searchParams.get('read') || 'all')});
          if (url.pathname === '/api/books' && request.method === 'POST') return sendJson(response, 201, books.create(await readJson(request)));
          const match = url.pathname.match(/^\\/api\\/books\\/(\\d+)$/);
          if (match && request.method === 'GET') { const book = books.get(Number(match[1])); return sendJson(response, book ? 200 : 404, book || {error: 'not_found'}); }
          if (match && request.method === 'PATCH') return sendJson(response, 200, books.update(Number(match[1]), await readJson(request)));
          if (match && request.method === 'DELETE') { books.remove(Number(match[1])); response.writeHead(204); return response.end(); }
          return sendJson(response, 404, {error: 'not_found'});
        } catch (error) { return sendJson(response, error.status || 500, {error: error.status ? error.message : 'request_failed'}); }
      }"""
    assert old in source
    source = source.replace(old, new)
    source = source.replace("  });\n}\n\nexport async function startApplication", "  });\n  server.once('close', () => books.close());\n  return server;\n}\n\nexport async function startApplication")
    return source


def execution_spec(targets):
    result = []
    for target in targets:
        kind = target["app_target"]
        common = {"adapter": kind, "project_path": ".", "product_path": "build/product"}
        result.append({"target_config_id": target["target_config_id"],
            "build": {**common, "output_paths": {"product": "build/product", "test": "build/tests"}},
            "unit": {**common, "test_kind": "unit", "test_project_path": "build/tests",
                "unit_project": "build/tests/unit.test.mjs", "expected_case_ids": case_ids(kind, "unit"), "report_path": f"reports/{kind}-unit.xml"},
            "integration": {**common, "test_kind": "api" if kind == "api" else "integration", "test_project_path": "build/tests",
                "framework_config": f"build/tests/playwright.{kind}.config.mjs", "expected_case_ids": case_ids(kind, "integration"),
                "report_path": f"reports/{kind}-integration.json"}})
    return {"schema_version": 1, "targets": result}


def reading_index(*, broken=False):
    source = (BUSINESS / "index.html").read_text()
    if broken:
        # The Web path deliberately requires a real failing report and a later
        # controller-authorized repair; the accepted Web assertions stay intact.
        source = source.replace('<label for="filter">阅读状态</label><select id="filter">',
                                '<label>阅读状态<select id="filter">')
        source = source.replace('</select><ul id="books"', '</select></label><ul id="books"')
    return source


def patch_for(stage, targets, repair=False):
    if repair:
        assert stage == "implementation" and {target["app_target"] for target in targets} == {"api", "web"}
        lines = ["*** Begin Patch", "*** Update File: public/index.html", "@@"]
        lines += ["-" + line for line in reading_index(broken=True).splitlines()]
        lines += ["+" + line for line in reading_index().splitlines()]
        return "\n".join([*lines, "*** End Patch"])
    if stage == "implementation":
        files = {"src/books.mjs": (BUSINESS / "books.mjs").read_text(), "src/server.mjs": server_source(),
            "public/index.html": reading_index(broken=any(target["app_target"] == "web" for target in targets)),
            "public/app.mjs": (BUSINESS / "app.mjs").read_text(),
            "public/styles.css": (BUSINESS / "styles.css").read_text()}
    elif stage == "unit_test_implementation":
        files = {"tests/unit.test.mjs": (BUSINESS / "unit.test.mjs").read_text()}
    elif stage == "integration_test_implementation":
        files = {"tests/api.spec.mjs": (BUSINESS / "api.spec.mjs").read_text(),
            "tests/web.spec.mjs": (BUSINESS / "web.spec.mjs").read_text(),
            "agentflow.project.json": json.dumps(execution_spec(targets), ensure_ascii=False, indent=2) + "\n"}
    else:
        raise AssertionError("Unexpected coding stage: " + stage)
    lines = ["*** Begin Patch"]
    for filename, content in files.items():
        original = STARTER / filename
        if original.is_file():
            lines += ["*** Update File: " + filename, "@@"]
            lines += ["-" + line for line in original.read_text().splitlines()]
        else:
            lines += ["*** Add File: " + filename]
        lines += ["+" + line for line in content.splitlines()]
    return "\n".join([*lines, "*** End Patch"])


def sse_response(item, model, identity):
    response = {"id": identity, "object": "response", "created_at": 1, "status": "completed", "model": model,
        "output": [item], "usage": {"input_tokens": 50, "output_tokens": 50, "total_tokens": 100}}
    events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}}]
    if item["type"] == "custom_tool_call":
        events += [{"type": "response.output_item.added", "output_index": 0, "item": {**item, "input": ""}},
            {"type": "response.custom_tool_call_input.delta", "output_index": 0, "item_id": item["id"], "delta": item["input"]}]
    else:
        text = item["content"][0]["text"]
        events += [{"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
            {"type": "response.content_part.added", "output_index": 0, "item_id": item["id"], "content_index": 0,
             "part": {"type": "output_text", "text": "", "annotations": []}},
            {"type": "response.output_text.delta", "output_index": 0, "item_id": item["id"], "content_index": 0, "delta": text},
            {"type": "response.output_text.done", "output_index": 0, "item_id": item["id"], "content_index": 0, "text": text}]
    events += [{"type": "response.output_item.done", "output_index": 0, "item": item},
               {"type": "response.completed", "response": response}]
    return [f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode() for event in events]


class ScriptedModel:
    def __init__(self):
        self.calls = []
        self.errors = []
        self.lock = threading.Lock()

    def answer(self, path, body, authorization):
        assert authorization == "Bearer " + PROVIDER_SECRET
        assert body["model"] == (ROLE_MODEL if path == "/v1/chat/completions" else MODEL)
        stage, commit, targets, repair = context(body)
        with self.lock:
            self.calls.append({"protocol": path, "stage": stage, "commit": commit, "repair": repair,
                "targets": sorted(t["app_target"] for t in targets),
                "tool_outputs": [i for i in body.get("input", []) if i.get("type") == "custom_tool_call_output"]})
            ordinal = len(self.calls)
        if path == "/v1/chat/completions":
            seen_tools = sum(m.get("role") == "tool" for m in body["messages"])
            reads = ["src/books.mjs", "public/index.html", "tests/unit.test.mjs", "tests/api.spec.mjs", "tests/web.spec.mjs"] if stage == "code_review" else []
            if seen_tools < len(reads):
                name = "agentflow_io"
                arguments = {"operation": "read_code", "arguments": {"path": reads[seen_tools]}}
            elif not reads and seen_tools == 0:
                name = "agentflow_io"
                arguments = {"operation": "write_document", "arguments": {"path": stage + ".md",
                    "content": document(stage, commit, targets)["content"] + "\n\nMODEL_PROTOCOL_FIXTURE_ONLY"}}
            else:
                name = "finish"
                arguments = {"message": "Protocol fixture stage result", "result": document(stage, commit, targets)}
            response = {"id": f"chatcmpl-{ordinal}", "object": "chat.completion", "created": 1, "model": ROLE_MODEL,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [{"id": f"call_{ordinal}",
                    "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]},
                    "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 50, "completion_tokens": 50, "total_tokens": 100}}
            return "application/json", [json.dumps(response, ensure_ascii=False).encode()]
        assert path == "/v1/responses"
        tool_outputs = [item for item in body["input"] if item.get("type") == "custom_tool_call_output"]
        identifier = hashlib.sha256((stage + commit).encode()).hexdigest()[:16]
        if not tool_outputs:
            assert any(t.get("name") == "apply_patch" and t.get("type") == "custom" for t in body["tools"])
            item = {"type": "custom_tool_call", "id": "ctc_" + identifier, "call_id": "patch_" + identifier,
                "name": "apply_patch", "input": patch_for(stage, targets, repair)}
        else:
            assert any(item.get("call_id") == "patch_" + identifier for item in tool_outputs)
            item = {"type": "message", "id": "message_" + identifier, "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(coding_result(body, stage)), "annotations": []}]}
        return "text/event-stream", sse_response(item, MODEL, "resp_" + identifier + "_" + str(ordinal))

    @contextmanager
    def serve(self):
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    mime, content = fixture.answer(self.path, body, self.headers.get("Authorization"))
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.end_headers()
                    for chunk in content:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except Exception as error:
                    fixture.errors.append(f"{type(error).__name__}: {error}")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"model protocol fixture failed"}}')
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
