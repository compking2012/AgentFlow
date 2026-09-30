"""Real starter build/process/browser tests, not evidence of a finished business product."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from agentflow.execution.manifests import tree_digest
from agentflow.testing.reports import parse_junit, parse_playwright

REPOSITORY = Path(__file__).resolve().parents[2]
STARTER = REPOSITORY / "src/agentflow/resources/web_api_starter"


def execute(project, argv, *, environment=None, timeout=90):
    return subprocess.run(argv, cwd=project, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(environment or {})})


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    project = tmp_path_factory.mktemp("starter")
    shutil.copytree(STARTER, project, dirs_exist_ok=True)
    installed = execute(project, ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"])
    assert installed.returncode == 0, installed.stderr
    result = execute(project, ["npm", "run", "build"])
    assert result.returncode == 0, result.stderr
    assert json.loads((project / "build/product/package.json").read_text()).get("dependencies", {}) == {}
    return project


def test_real_build_starts_on_os_allocated_port_and_does_not_fake_business_implementation(built, tmp_path):
    ready = tmp_path / "ready.json"
    data = tmp_path / "application-data"
    process = subprocess.Popen(["node", str(built / "build/product/server.mjs")], cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "HOST": "127.0.0.1", "PORT": "0",
             "AGENTFLOW_DATA_DIR": str(data), "AGENTFLOW_READY_FILE": str(ready)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            assert process.poll() is None, process.stderr.read()
            time.sleep(0.02)
        assert ready.exists()
        receipt = json.loads(ready.read_text())
        assert receipt["pid"] == process.pid and 0 < receipt["port"] <= 65535
        with httpx.Client(base_url=receipt["url"], trust_env=False) as client:
            assert client.get("/health").json() == {"status": "ok"}
            assert "业务功能尚未实现" in client.get("/").text
            response = client.post("/api/items", json={"title": "not an implemented business"})
            assert response.status_code == 501
            assert response.json()["error"] == "business_not_implemented"
            assert client.get("/../../package.json").status_code == 404
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_sqlite_infrastructure_uses_external_data_and_reopens_real_committed_state(built, tmp_path):
    module = (built / "build/product/storage.mjs").as_uri()
    create = f"""import {{openDatabase}} from {json.dumps(module)};
const db=openDatabase();db.exec('CREATE TABLE infrastructure_probe(value TEXT NOT NULL)');
db.prepare('INSERT INTO infrastructure_probe(value) VALUES (?)').run('persisted');db.close();"""
    read = f"""import {{openDatabase}} from {json.dumps(module)};
const db=openDatabase();console.log(db.prepare('SELECT value FROM infrastructure_probe').get().value);db.close();"""
    env = {"AGENTFLOW_DATA_DIR": str(tmp_path / "data")}
    first = execute(built, ["node", "--input-type=module", "-e", create], environment=env)
    second = execute(built, ["node", "--input-type=module", "-e", read], environment=env)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert second.stdout.strip() == "persisted"
    assert (tmp_path / "data/product.sqlite").is_file()
    assert not list((built / "build/product").rglob("*.sqlite"))


def test_placeholder_unit_api_and_real_browser_cases_fail_and_preserve_frozen_packages(built):
    product_digest = tree_digest(built / "build/product")
    tests_digest = tree_digest(built / "build/tests")
    unit_report = built / "reports/unit-placeholder.xml"
    result = execute(built, ["node", "--test", "--test-reporter=junit", f"--test-reporter-destination={unit_report}",
        "build/tests/unit.test.mjs"])
    unit = parse_junit(unit_report)
    assert result.returncode != 0 and unit.quality_result == "failed"
    assert len(unit.cases) == 1 and unit.cases[0].case_id == "test::starter has no goal-specific unit tests"
    browser_path = REPOSITORY / "apps/dashboard/.playwright-browsers"
    assert browser_path.is_dir(), "Install the pinned Playwright browser before this real-browser test"
    for target in ("api", "web"):
        report = built / f"reports/{target}-placeholder.json"
        result = execute(built, ["node", "build/tests/node_modules/@playwright/test/cli.js", "test",
            "--config", f"build/tests/playwright.{target}.config.mjs"], environment={
                "PLAYWRIGHT_BROWSERS_PATH": str(browser_path), "PLAYWRIGHT_JSON_OUTPUT_NAME": str(report)})
        parsed = parse_playwright(report)
        assert result.returncode != 0, result.stdout
        assert parsed.execution_status == "completed" and parsed.quality_result == "failed", result.stdout + result.stderr
        assert len(parsed.cases) == 1
        assert "Replace this failing placeholder" in parsed.cases[0].message
        assert tree_digest(built / "build/product") == product_digest
        assert tree_digest(built / "build/tests") == tests_digest


def test_build_refuses_source_links_instead_of_packaging_external_files(built, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("outside the product source")
    link = built / "src/external.txt"
    link.symlink_to(target)
    try:
        result = execute(built, ["npm", "run", "build"])
        assert result.returncode != 0 and "Source links cannot enter frozen output" in result.stderr
    finally:
        link.unlink()
