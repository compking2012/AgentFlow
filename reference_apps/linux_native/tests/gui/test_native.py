import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from dogtail.tree import root


def wait_for(function, seconds=15):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            result = function()
            if result:
                return result
        except Exception as exc:
            last = exc
        time.sleep(.15)
    raise AssertionError(f"Native control/state unavailable: {last}")


def test_native_create_persists_after_reopen():
    endpoint = os.environ.get("AGENTFLOW_API_URL")
    assert endpoint, "A real reference API is required; no skip/fake success"
    assert os.environ.get("XDG_SESSION_TYPE", "x11") == "x11", "This frozen reference combination requires X11"
    app = Path(os.environ.get("AGENTFLOW_APP_PATH", Path(__file__).resolve().parents[2]))
    script = app / "app.py" if app.is_dir() else app
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen([sys.executable, str(script)], env=env)
    title = "linux-" + uuid.uuid4().hex
    try:
        window = wait_for(lambda: root.child(name="AgentFlow Tickets", roleName="frame", recursive=True))
        window.child(name="ticket-title", recursive=True).text = title
        window.child(name="Create ticket", roleName="push button", recursive=True).click()
        wait_for(lambda: window.child(name=title, recursive=True))
        request = urllib.request.Request(endpoint + "/api/tickets", headers={"Authorization": "Bearer reference.manager"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert any(item["title"] == title for item in json.load(response)["tickets"])
        process.terminate()
        process.wait(timeout=5)
        process = subprocess.Popen([sys.executable, str(script)], env=env)
        window = wait_for(lambda: root.child(name="AgentFlow Tickets", roleName="frame", recursive=True))
        wait_for(lambda: window.child(name=title, recursive=True))
    finally:
        process.terminate()
        process.wait(timeout=5)
