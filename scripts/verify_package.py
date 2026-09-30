"""Isolated POSIX wheel smoke test for the five-command CLI; no model requests."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description="Install a wheel and verify its fixed configuration and local application")
parser.add_argument("wheel", type=Path)
args = parser.parse_args()
root = Path(tempfile.mkdtemp(prefix="agentflow-wheel-")).resolve()
isolated_home = root / "home"
isolated_home.mkdir(mode=0o700)
# Avoid opening the user's desktop browser during an unattended package check.
# The separate strict E2E drives the actual compiled UI in a real browser.
environment = {**os.environ, "HOME": str(isolated_home), "BROWSER": "/usr/bin/true"}
environment.pop("PYTHONPATH", None)
python = root / "venv/bin/python"
uv = shutil.which("uv")
if not uv:
    raise SystemExit("Install uv before running the package check")
subprocess.run([uv, "venv", str(root / "venv"), "--python", sys.executable], check=True, capture_output=True)
wheel = args.wheel.resolve(strict=True)
subprocess.run([uv, "pip", "install", "--python", str(python), str(wheel)], check=True, capture_output=True)
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    assert all(n in names for n in ["agentflow/web/index.html", "agentflow/design.openapi.json",
        "agentflow/configuration.py", "agentflow/server.py", "node_agent/__main__.py"])
    assert not any(".env" in n or "node_modules" in n or ".playwright-browsers" in n for n in names)
cli = root / "venv/bin/agentflow"


def command(*arguments, check=True):
    return subprocess.run([str(cli), *arguments], check=check, capture_output=True, text=True,
                          cwd=root, env=environment, timeout=90)


help_text = command("--help").stdout
assert "{start,run,status,launch,stop}" in help_text
run_help = command("run", "--help").stdout
for forbidden in ("--data-dir", "--config", "--json", "--goal", "--target", "--review-mode", "--idempotency-key"):
    assert forbidden not in help_text + run_help, forbidden
for obsolete in ("serve", "open", "backup", "restore", "model-import", "init-gateway",
                 "setup-model", "product-status", "stop-product", "retry-product"):
    assert command(obsolete, check=False).returncode == 2, obsolete
assert not (isolated_home / ".config/agentflow/config.toml").exists(), "Help or rejected commands created configuration"
subprocess.run([str(python), "-m", "node_agent", "--help"], check=True, capture_output=True, cwd=root, env=environment)
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
data = root / "data"
(isolated_home / ".config").mkdir(mode=0o700)
config = isolated_home / ".config/agentflow/config.toml"
config.parent.mkdir(mode=0o700)
config.write_text(f"[app]\ndata_dir = {json.dumps(str(data))}\nport = {port}\n\n[product]\n"
                  + 'target = "api"\nreview_mode = "auto"\nmax_model_requests = 0\n'
                  + f"output_root = {json.dumps(str(root / 'products'))}\n")
config.chmod(0o600)
origin = f"http://127.0.0.1:{port}"
http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
started = False
try:
    start = command("start")
    started = True
    assert origin in start.stdout and str(config) in start.stdout, start.stdout
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if http.open(origin + "/health", timeout=2).status == 200:
                break
        except (urllib.error.URLError, TimeoutError):
            time.sleep(.1)
    else:
        raise AssertionError("Packaged public start did not expose its actual health endpoint")
    assert http.open(origin + "/", timeout=5).status == 200
    assert "还没有产品" in command("status").stdout
    # Internal installed-package IPC is an audit mechanism, not an extra public
    # command or a CLI configuration override.
    url = subprocess.check_output([str(python), "-c",
        "import asyncio; from agentflow.configuration import load_configuration; "
        "from agentflow.control.owner_ipc import launch_url; "
        "print(asyncio.run(launch_url(load_configuration().settings.data_dir)))"],
        text=True, cwd=root, env=environment, timeout=20).strip()
    assert url.startswith(origin + "/#bootstrap=")
    request = urllib.request.Request(origin + "/api/v1/session",
        data=json.dumps({"bootstrap_token": url.split("bootstrap=")[1]}).encode(),
        headers={"Content-Type": "application/json", "Origin": origin, "Idempotency-Key": "isolated-smoke-session"},
        method="POST")
    owner_token = json.loads(http.open(request, timeout=10).read()).get("owner_token")
    assert owner_token
    setup_request = urllib.request.Request(origin + '/api/v1/product_setup',
        headers={'Authorization': 'Bearer ' + owner_token})
    setup = json.loads(http.open(setup_request, timeout=10).read())
    assert setup['product_defaults']['max_model_requests'] == 0
finally:
    stopped = command("stop", check=False)
    if started:
        assert stopped.returncode == 0, stopped.stderr
    instance = config.parent / "active-instance.json"
    deadline = time.monotonic() + 20
    while instance.exists() and time.monotonic() < deadline:
        time.sleep(.1)
    assert not instance.exists(), "Public stop did not finish the actual server lifecycle"
assert "平台未运行" in command("status").stdout

# Backup/restore remain internal application APIs; there are no hidden public
# backup/restore commands. Exercise the installed implementation after shutdown.
backup = root / "backup"
restored = root / "restored"
maintenance = """
import asyncio,json,sys
from pathlib import Path
from agentflow.control.backups import ApplicationBackup
from agentflow.storage import Store
async def main():
    data,backup,restored=map(Path,sys.argv[1:])
    store=Store(data);await store.start()
    try:await ApplicationBackup(store,data).create(backup)
    finally:await store.close()
    await ApplicationBackup.restore(backup,restored)
    store=Store(restored);await store.start()
    try:print(json.dumps({'projects':len(await store.list('project'))}))
    finally:await store.close()
asyncio.run(main())
"""
status = json.loads(subprocess.check_output([str(python), "-c", maintenance, str(data), str(backup), str(restored)],
    text=True, cwd=root, env=environment, timeout=90))
assert status["projects"] == 0
result = {"wheel": wheel.name, "wheel_bytes": wheel.stat().st_size,
    "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "isolated_install": True,
    "isolated_home": True, "fixed_toml_configuration": True, "unlimited_request_configuration": True,
    "public_commands": ["start", "run", "status", "launch", "stop"], "obsolete_commands_rejected": True,
    "packaged_dashboard": True, "packaged_contract": True, "owner_bootstrap": True,
    "owner_ipc_reopen": True, "public_start_status_stop": True, "graceful_shutdown": True,
    "application_backup_restore_api": True, "node_cli": True, "browser_open_suppressed": True,
    "paid_model_requests": 0}
(repo / "validation").mkdir(exist_ok=True)
(repo / "validation/package-smoke.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
shutil.rmtree(root)
