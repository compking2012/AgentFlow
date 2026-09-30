from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio

from agentflow.models.profiles import AttemptContext, ModelProfile, PricingPolicy
from agentflow.storage.store import Store


@pytest_asyncio.fixture
async def store(tmp_path):
    value = Store(tmp_path / "data")
    await value.start()
    yield value
    await value.close()


@pytest.fixture
def context():
    return AttemptContext(
        attempt_id="attempt-one", run_id="run-one", iteration_id="iteration-one", model_profile_id="profile-one",
        fencing_token=1, input_fingerprint="sha256:" + "a" * 64,
        expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        max_model_requests=20, max_output_tokens=64,
    )


@pytest.fixture
def price():
    return PricingPolicy(input_micros_per_million=1_000_000, output_micros_per_million=1_000_000,
                         input_token_upper_bound=100, input_bound_verified=True,
                         output_control_verified=True, source_version="local-fixture")


@pytest.fixture
def http_stub():
    @contextmanager
    def serve(callback):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append({"path": self.path, "body": body, "authorization": self.headers.get("Authorization")})
                status, headers, content = callback(calls[-1])
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                for chunk in content if isinstance(content, list) else [content]:
                    self.wfile.write(chunk)
                    self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", calls
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    return serve


@pytest.fixture
def profile_factory(price):
    def create(base_url, **kwargs):
        return ModelProfile(model_profile_id="profile-one", provider="local_test", requested_model="fixture-model",
                            accepted_api_model="fixture-model", acceptance_status="accepted", base_url=base_url,
                            protocols=["chat_completions", "responses"], credential_reference="local-test-key",
                            pricing=price, allow_loopback_upstream=True, **kwargs)
    return create
