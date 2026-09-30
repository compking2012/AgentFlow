"""Reads Android's change, then updates through a real GTK control."""
import os
import subprocess
import sys
import time
from pathlib import Path

from dogtail.tree import root


def wait_for(function):
    deadline = time.monotonic() + 15
    last = None
    while time.monotonic() < deadline:
        try:
            result = function()
            if result:
                return result
        except Exception as error:
            last = error
        time.sleep(.15)
    raise AssertionError(f"Expected actual native state was absent: {last}")


def test_android_assignment_is_read_and_updated_in_gtk():
    assert os.environ.get("AGENTFLOW_API_URL"), "A real backend is required"
    title = os.environ.get("AGENTFLOW_CROSS_TICKET_TITLE")
    assert title, "The previous API/Android steps must supply the same ticket title"
    app = Path(os.environ["AGENTFLOW_APP_PATH"])
    script = app / "app.py" if app.is_dir() else app
    process = subprocess.Popen([sys.executable, str(script)], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        window = wait_for(lambda: root.child(name="AgentFlow Tickets", roleName="frame", recursive=True))
        label = wait_for(lambda: window.child(name="Assignment " + title, recursive=True))
        assert "Assigned: member" in label.text
        window.child(name="Assign " + title + " to manager", roleName="push button", recursive=True).click()
        wait_for(lambda: "Assigned: manager" in window.child(name="Assignment " + title, recursive=True).text)
    finally:
        process.terminate()
        process.wait(timeout=5)
