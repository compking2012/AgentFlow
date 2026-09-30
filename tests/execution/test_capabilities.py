import platform
import sys

from agentflow.execution import capabilities
from agentflow.execution.capabilities import observe_tool, probe_target_sync, version_matches
from agentflow.execution.models import TargetConfig


def test_actual_host_os_and_cpu_cannot_satisfy_a_different_target():
    target = TargetConfig(app_target="api", os_name="unsupported-operating-system",
        os_version_constraint="*", cpu_architecture="unsupported-architecture",
        required_display_protocol="not_required", required_device_mode="not_required")
    result = probe_target_sync(target)
    assert result.os_name == platform.system()
    assert result.state == "blocked" and not result.functional_verified
    assert any(reason.startswith("target_os_mismatch:") for reason in result.blocking_reasons)
    assert any(reason.startswith("cpu_architecture_mismatch:") for reason in result.blocking_reasons)


def test_explicit_missing_browser_is_blocked_without_falling_back(monkeypatch):
    monkeypatch.setenv("AGENTFLOW_BROWSER_EXECUTABLE", "/nonexistent/owner-selected-browser")
    target = TargetConfig(app_target="web", os_name=platform.system(), os_version_constraint="*",
        cpu_architecture="*", required_display_protocol="not_required", required_device_mode="not_required")
    result = probe_target_sync(target)
    assert result.state == "blocked"
    assert any(reason.startswith("tool_unavailable:browser:") for reason in result.blocking_reasons)


def test_gradle_banner_is_not_mistaken_for_its_version(monkeypatch, tmp_path):
    executable = tmp_path / "gradle"
    executable.write_text("test-only tool observation fixture")
    monkeypatch.setattr(capabilities.shutil, "which", lambda _name: str(executable))
    monkeypatch.setattr(capabilities, "_command", lambda _argv: (0, "------------------------------------------------------------\nGradle 8.7\n------------------------------------------------------------"))
    observed = observe_tool("gradle")
    assert observed.available and version_matches(observed.version, "8.7")


def test_real_slow_version_probe_can_finish_after_the_old_eight_second_limit(monkeypatch):
    monkeypatch.setattr(capabilities.shutil, "which", lambda _name: sys.executable)
    script = "import sys,time; time.sleep(8.1); print('Python ' + sys.version.split()[0])"
    observed = observe_tool("test-python", ("-c", script))
    assert observed.available
    assert observed.version == "Python " + sys.version.split()[0]
    assert version_matches(observed.version, f"{sys.version_info.major}.{sys.version_info.minor}.*")
    assert not version_matches(observed.version, "999.*")


def test_expired_probe_stays_unavailable_and_error_text_is_not_a_version(monkeypatch):
    command = capabilities._command
    monkeypatch.setattr(capabilities.shutil, "which", lambda _name: sys.executable)
    monkeypatch.setattr(capabilities, "_command", lambda argv: command(argv, timeout=.05))
    observed = observe_tool("test-python", ("-c", "import time; time.sleep(2); print('Python 999.0')"))
    assert not observed.available and observed.version is None
    assert "timed out" in observed.detail
    assert not version_matches(observed.version, "*")
