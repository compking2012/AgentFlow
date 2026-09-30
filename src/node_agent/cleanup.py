"""Prove that this assignment released a resource; unknown cleanup remains quarantined."""
from __future__ import annotations

import asyncio
import hashlib
import plistlib
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from agentflow.common import utc_now
from agentflow.execution.capabilities import _command
from agentflow.testing.adapters import BuildRecipe, contained


def _stopped_processes(result: dict) -> list[dict] | None:
    if not result.get("cleanup_verified"):
        return None
    checks = []
    for observed in result.get("process_results", []):
        pid, created = observed.get("pid"), observed.get("process_created")
        if pid is None:
            continue
        if not created:
            return None
        try:
            process = psutil.Process(pid)
            if process.create_time() == created and process.status() != psutil.STATUS_ZOMBIE:
                return None
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            return None
        checks.append({"pid": pid, "process_created": created, "alive": False})
    return checks


def _cleanup_sync(directory: Path, assignment: dict, result: dict, kind: str) -> dict | None:
    processes = _stopped_processes(result)
    if processes is None:
        return None
    proof = {"alive_process_count": 0, "method": "process_stop_verified",
             "checked_at": utc_now(), "observed_processes": processes}
    workspace = directory / "workspaces" / hashlib.sha256(assignment["job_id"].encode()).hexdigest() / "workspace"
    if kind == "workspace":
        if workspace.is_symlink():
            return None
        shutil.rmtree(workspace, ignore_errors=False) if workspace.exists() else None
        return {**proof, "method": "workspace_cleanup_verified", "workspace_absent": not workspace.exists()}
    if kind == "test_data_namespace":
        return None  # The namespace needs an explicit reset adapter and identity, not merely stopped processes.
    recipe = BuildRecipe.model_validate(assignment["recipe"]) if assignment.get("recipe") else None
    if kind == "port":
        ports = {urlsplit(url).port for url in (recipe.service_urls.values() if recipe else [])
                 if urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}}
        ports.discard(None)
        if not ports:
            return None
        try:
            if any(connection.status == psutil.CONN_LISTEN and connection.laddr.port in ports
                   for connection in psutil.net_connections("inet")):
                return None
        except psutil.AccessDenied:
            return None
        return {**proof, "released_local_ports": sorted(ports)}
    if not result.get("process_results") or assignment["kind"] == "build":
        return proof  # No application/device action was started by this assignment.
    target = assignment["app_target"]
    if kind in {"device", "simulator"}:
        if not recipe or not recipe.device_id or not recipe.application_id:
            return None
        if target == "ios_native" and assignment["target_config"]["required_device_mode"] == "simulator":
            identifiers = {recipe.application_id}
            if recipe.test_product_path:
                for info in contained(workspace, recipe.test_product_path).rglob("*.app/Info.plist"):
                    if info.is_symlink():
                        return None
                    with info.open("rb") as stream:
                        identifier = plistlib.load(stream).get("CFBundleIdentifier")
                    if identifier:
                        identifiers.add(identifier)
            terminations = []
            for identifier in sorted(identifiers):
                code, output = _command(["xcrun", "simctl", "terminate", recipe.device_id, identifier])
                terminations.append({"bundle_id": identifier, "terminate_exit": code, "output": output[:1000]})
            status, listing = _command(["xcrun", "simctl", "spawn", recipe.device_id, "launchctl", "list"])
            if status != 0:
                return None
            for line in listing.splitlines():
                fields = line.split()
                if len(fields) >= 3 and fields[0].isdigit() and any(identifier in fields[-1] for identifier in identifiers):
                    return None
            return {**proof, "device_id": recipe.device_id, "application_checks": terminations,
                    "launchctl_exit": status, "remaining_application_pids": []}
        if target != "android_native":
            return None
        applications = {recipe.application_id}
        if recipe.instrumentation_runner:
            applications.add(recipe.instrumentation_runner.split("/", 1)[0])
        evidence = []
        for package in sorted(applications):
            code, output = _command(["adb", "-s", recipe.device_id, "shell", "am", "force-stop", package])
            if code:
                return None
            status, observed = _command(["adb", "-s", recipe.device_id, "shell", "pidof", package])
            if status not in {0, 1} or observed.strip():
                return None
            evidence.append({"application_id": package, "force_stop_exit": code,
                             "pidof_exit": status, "remaining_pids": []})
        return {**proof, "device_id": recipe.device_id, "application_checks": evidence}
    if kind in {"desktop_session", "display"}:
        if target in {"windows_native", "macos_native"}:
            if not recipe or not recipe.product_path:
                return None
            product = contained(workspace, recipe.product_path)
            try:
                processes_to_stop = []
                for process in psutil.process_iter():
                    try:
                        executable = Path(process.exe()).resolve()
                    except psutil.NoSuchProcess:
                        continue
                    if executable == product or executable.is_relative_to(product):
                        process.terminate()
                        processes_to_stop.append(process)
                _, alive = psutil.wait_procs(processes_to_stop, timeout=3)
                if alive:
                    return None
            except psutil.AccessDenied:
                return None
            proof["application_path"] = str(product)
            proof["stopped_application_pids"] = [p.pid for p in processes_to_stop]
        return proof
    return None


async def cleanup_evidence(directory: Path, assignment: dict, result: dict, kind: str) -> dict | None:
    return await asyncio.to_thread(_cleanup_sync, directory, assignment, result, kind)
