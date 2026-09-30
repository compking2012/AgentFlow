from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio
from pydantic import SecretStr

from agentflow.runtime.contracts import TaskEnvelope
from agentflow.storage.store import Store


@pytest_asyncio.fixture
async def store(tmp_path):
    value = Store(tmp_path / "data")
    await value.start()
    yield value
    await value.close()


@pytest.fixture
def task(tmp_path):
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "original.py").write_text("ORIGINAL = True\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return TaskEnvelope(
        attempt_id="runtime-attempt", operation_id="runtime-operation", work_item_id="work-one",
        run_id="run-one", iteration_id="iteration-one", role="review", goal="Protocol fixture only",
        input_fingerprint="sha256:" + "a" * 64, fencing_token=1,
        workspace=workspace, artifact_dir=artifacts, model_profile_id="local-fixture",
        model="fixture-model", proxy_base_url="http://127.0.0.1:1/v1",
        proxy_token=SecretStr("scoped-fixture-token"), max_active_seconds=30,
        output_schema={"type": "object", "properties": {"review": {"type": "string"}},
                       "required": ["review"], "additionalProperties": False},
    )


@pytest.fixture
def http_fixture():
    @contextmanager
    def serve(callback):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.loads(raw)
                call = {"path": self.path, "body": body, "authorization": self.headers.get("Authorization")}
                calls.append(call)
                status, headers, content = callback(call, len(calls))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                for chunk in content if isinstance(content, list) else [content]:
                    self.wfile.write(chunk)
                    self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", calls
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    return serve
