"""Real host probes; tool installation never means a native GUI is verified."""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

import psutil

from agentflow.common import canonical_digest, utc_now
from agentflow.execution.manifests import file_digest
from agentflow.execution.models import (
    AppTarget,
    CapabilityReport,
    DisplayObservation,
    TargetConfig,
    ToolObservation,
)
from agentflow.runtime.process_identity import current_boot_identity

HOST_FOR_TARGET = {AppTarget.IOS: "Darwin", AppTarget.MACOS: "Darwin", AppTarget.WINDOWS: "Windows",
                   AppTarget.LINUX: "Linux"}


def _command(argv: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, (result.stdout + "\n" + result.stderr).strip()[:16000]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def version_matches(actual: str | None, constraint: str) -> bool:
    if not actual:
        return False
    if constraint == "*":
        return True
    observed = re.search(r"\d+(?:\.\d+){0,4}", actual)
    if not observed:
        return False
    av = tuple(int(x) for x in observed.group().split("."))
    for part in constraint.split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|>|<)?\s*(\d+(?:\.\d+)*)(\.\*)?\s*", part)
        if not match:
            return False
        op, value, wildcard = match.groups()
        bv = tuple(int(x) for x in value.split("."))
        if wildcard:
            if op not in {None, "=="} or av[:len(bv)] != bv:
                return False
            continue
        n = max(len(av), len(bv))
        a, b = av + (0,) * (n - len(av)), bv + (0,) * (n - len(bv))
        if not {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b, "==": a == b, None: a == b}[op]:
            return False
    return True


def observe_tool(name: str, args: tuple[str, ...] = ("--version",)) -> ToolObservation:
    path = shutil.which(name)
    if not path:
        return ToolObservation(name=name, path=None, version=None, available=False, detail="not_installed")
    code, output = _command([path, *args])
    try:
        digest = file_digest(Path(path).resolve())
    except Exception:
        digest = None
    lines = output.splitlines()
    version = lines[0] if lines and code == 0 else None
    if name == "gradle":
        version = next((line for line in lines if re.match(r"^Gradle\s+\d", line)), None)
    elif name == "dotnet":
        version = next((line for line in lines if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+].*)?", line.strip())), None)
    if code != 0:
        version = None
    return ToolObservation(name=name, path=path, version=version,
                           available=code == 0, executable_fingerprint=digest,
                           detail=output if code else "")


def observe_display() -> DisplayObservation:
    system = platform.system()
    identity = {"os": system, "display": os.getenv("DISPLAY"), "wayland": os.getenv("WAYLAND_DISPLAY"),
                "session": os.getenv("XDG_SESSION_ID"), "uid": getattr(os, "getuid", lambda: 0)()}
    display = DisplayObservation(session_fingerprint=canonical_digest(identity))
    if system == "Linux":
        protocol = "wayland" if os.getenv("XDG_SESSION_TYPE") == "wayland" or os.getenv("WAYLAND_DISPLAY") else (
            "x11" if os.getenv("DISPLAY") else "none")
        display.protocol = protocol
        display.mode = "physical" if protocol != "none" else "headless"
        display.compositor = os.getenv("XDG_CURRENT_DESKTOP")
        if protocol == "x11":
            code, output = _command(["xdpyinfo"])
            display.interactive = code == 0
            virtual = "Xvfb" in output
            for process in psutil.process_iter():
                try:
                    virtual = virtual or "Xvfb" in process.name()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
            if virtual:
                display.mode = "virtual"
        elif protocol == "wayland":
            runtime = Path(os.getenv("XDG_RUNTIME_DIR", "/nonexistent"))
            display.interactive = (runtime / os.getenv("WAYLAND_DISPLAY", "missing")).exists()
        session = os.getenv("XDG_SESSION_ID")
        if session:
            code, output = _command(["loginctl", "show-session", session, "-p", "LockedHint", "--value"])
            if code == 0 and output in {"yes", "no"}:
                display.unlocked = output == "no"
        if os.getenv("DBUS_SESSION_BUS_ADDRESS"):
            code, output = _command(["gdbus", "call", "--session", "--dest", "org.a11y.Bus",
                                     "--object-path", "/org/a11y/bus", "--method", "org.a11y.Bus.GetAddress"])
            if code == 0 and "unix:" in output:
                display.accessibility = "verified"
                display.accessibility_backend = "AT-SPI2 bus (target actions require functional probe)"
        # Do not infer input/capture permissions from environment variables.
    elif system == "Darwin":
        display.protocol = "macos_aqua"
        try:
            uid = Path("/dev/console").stat().st_uid
            display.interactive = uid not in {0}
        except OSError:
            pass
        try:
            lib = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            lib.AXIsProcessTrusted.restype = ctypes.c_bool
            display.accessibility = "verified" if lib.AXIsProcessTrusted() else "unavailable"
            display.accessibility_backend = "AXIsProcessTrusted"
        except (OSError, AttributeError):
            pass
        try:
            lib = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            lib.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            display.screen_capture = "verified" if lib.CGPreflightScreenCaptureAccess() else "unavailable"
        except (OSError, AttributeError):
            pass
        display.mode = "physical" if display.interactive else "headless"
    elif system == "Windows":
        display.protocol = "windows_desktop"
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        desktop = user32.OpenInputDesktop(0, False, 0x0100)
        display.interactive = bool(desktop)
        display.unlocked = bool(desktop)
        if desktop:
            user32.CloseDesktop(ctypes.c_void_p(desktop))
        display.mode = "physical" if desktop else "headless"
        code, output = _command(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                 "Add-Type -AssemblyName UIAutomationClient; "
                                 "[System.Windows.Automation.AutomationElement]::RootElement.Current.Name"])
        if code == 0 and output:
            display.accessibility = "verified"
            display.accessibility_backend = "Windows UI Automation root"
    return display


def _architecture(value: str) -> str:
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64", "arm64-v8a": "arm64"}.get(value.lower(), value.lower())


def observe_browser() -> ToolObservation:
    configured = os.getenv("AGENTFLOW_BROWSER_EXECUTABLE")
    choices = [configured] if configured else [shutil.which(name) for name in
        ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")]
    if not configured:
        choices += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
        for root in [Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache/ms-playwright")),
                     Path.home() / "Library/Caches/ms-playwright"]:
            for pattern in ("chromium-*/chrome-linux/chrome", "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
                            "chromium-*/chrome-win/chrome.exe", "chromium-*/chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium"):
                choices += [str(path) for path in sorted(root.glob(pattern))]
    selected = next((value for value in choices if value and Path(value).is_file()), None)
    if not selected:
        return ToolObservation(name="browser", path=None, version=None, available=False,
                               detail="No installed browser binary was found; declare AGENTFLOW_BROWSER_EXECUTABLE")
    observation = observe_tool(selected)
    return observation.model_copy(update={"name": "browser"})


def probe_target_sync(target: TargetConfig) -> CapabilityReport:
    system = platform.system()
    blocked, limitations = [], ["Static probe is not functional/native application verification"]
    required_host = HOST_FOR_TARGET.get(target.app_target)
    if required_host and required_host != system:
        blocked.append(f"host_os_mismatch:requires_{required_host}:observed_{system}")
    defaults: dict[AppTarget, list[tuple[str, tuple[str, ...]]]] = {
        AppTarget.WEB: [("node", ("--version",)), ("npm", ("--version",))],
        AppTarget.API: [("node", ("--version",)), ("npm", ("--version",))],
        AppTarget.IOS: [("xcodebuild", ("-version",)), ("xcrun", ("--version",))],
        AppTarget.MACOS: [("xcodebuild", ("-version",)), ("xcrun", ("--version",))],
        AppTarget.ANDROID: [("adb", ("version",)), ("java", ("-version",))],
        AppTarget.WINDOWS: [("dotnet", ("--version",))],
        AppTarget.LINUX: [("python3", ("--version",)), ("gdbus", ("help",))],
    }
    tools = [observe_tool(name, args) for name, args in defaults[target.app_target]]
    if target.app_target == AppTarget.WEB:
        tools.append(observe_browser())
    requirements = list(target.sdk_requirements) + [req for req in (target.build_backend, target.test_backend) if req]
    existing = {t.name for t in tools}
    for req in requirements:
        if req.name not in existing:
            tools.append(observe_browser().model_copy(update={"name": req.name})
                         if req.name in {"chrome", "chromium", "browser"} else observe_tool(req.name))
            existing.add(req.name)
    for tool in tools:
        if not tool.available:
            blocked.append(f"tool_unavailable:{tool.name}:{tool.detail[:200]}")
    for req in requirements:
        tool = next(t for t in tools if t.name == req.name)
        if not version_matches(tool.version, req.version_constraint):
            blocked.append(f"tool_version_not_verified:{req.name}:{req.version_constraint}")
    display = observe_display()
    if target.app_target in {AppTarget.LINUX, AppTarget.WINDOWS, AppTarget.MACOS}:
        if not display.interactive:
            blocked.append("interactive_session_unavailable")
        if display.unlocked is False:
            blocked.append("desktop_locked")
        expected = target.required_display_protocol
        if expected not in {"any", "not_required"} and expected != display.protocol:
            blocked.append(f"display_protocol_mismatch:{expected}:{display.protocol}")
        if target.app_target == AppTarget.LINUX:
            if display.accessibility != "verified":
                blocked.append("at_spi_bus_unavailable")
            code, output = _command(["python3", "-c", "import dogtail.tree; print(dogtail.tree.root.name)"])
            if code != 0:
                blocked.append("dogtail_or_accessibility_root_unavailable:" + output[:200])
            if display.protocol == "wayland":
                limitations.append("Wayland input and capture require a compositor-specific functional probe")
    observed_os = system
    observed_version = platform.mac_ver()[0] if system == "Darwin" else platform.release()
    observed_arch = platform.machine()
    if target.app_target == AppTarget.ANDROID:
        code, output = _command(["adb", "devices", "-l"])
        devices = [line for line in output.splitlines()[1:] if re.search(r"\sdevice\s", line)]
        if code or not devices:
            blocked.append("android_authorized_device_unavailable")
        if target.required_device_mode == "physical" and not any(not x.startswith("emulator-") for x in devices):
            blocked.append("physical_android_device_required")
        if target.required_device_mode == "simulator" and not any(x.startswith("emulator-") for x in devices):
            blocked.append("android_emulator_required")
        matches = []
        for device in devices:
            serial = device.split()[0]
            if target.required_device_mode == "physical" and serial.startswith("emulator-"):
                continue
            if target.required_device_mode == "simulator" and not serial.startswith("emulator-"):
                continue
            observations = {}
            for name, prop in [("version", "ro.build.version.release"), ("architecture", "ro.product.cpu.abi"), ("model", "ro.product.model")]:
                status, value = _command(["adb", "-s", serial, "shell", "getprop", prop])
                if status == 0:
                    observations[name] = value.strip()
            if (version_matches(observations.get("version"), target.os_version_constraint)
                    and (target.cpu_architecture == "*" or _architecture(observations.get("architecture", "")) == _architecture(target.cpu_architecture))
                    and (not target.device_model_constraints or observations.get("model") in target.device_model_constraints)):
                matches.append((serial, observations))
        observed_os = "Android"
        if matches:
            serial, observation = sorted(matches)[0]
            observed_version, observed_arch = observation["version"], observation["architecture"]
            display = DisplayObservation(protocol="android", mode="simulator" if serial.startswith("emulator-") else "physical",
                interactive=True, session_fingerprint=canonical_digest({"device": serial, "runtime": observation}))
            tools.append(ToolObservation(name="android_runtime", path=serial, version=observed_version, available=True,
                                         detail=json.dumps(observation, sort_keys=True)))
        else:
            observed_version, observed_arch = "unknown", "unknown"
            blocked.append("android_runtime_constraints_not_verified")
    if target.app_target == AppTarget.IOS and system == "Darwin":
        code, output = _command(["xcrun", "simctl", "list", "devices", "booted", "--json"])
        if target.required_device_mode == "physical":
            blocked.append("physical_ios_requires_explicit_devicectl_pairing_probe")
        observed_os, observed_version, observed_arch = "iOS", "unknown", "unknown"
        try:
            devices = json.loads(output).get("devices", {}) if not code else {}
        except ValueError:
            devices = {}
        matches = []
        for runtime, candidates in devices.items():
            version = runtime.rsplit("iOS-", 1)[-1].replace("-", ".")
            for candidate in candidates:
                if candidate.get("state") != "Booted" or not candidate.get("isAvailable", False):
                    continue
                if target.device_model_constraints and candidate.get("name") not in target.device_model_constraints:
                    continue
                status, arch = _command(["xcrun", "simctl", "spawn", candidate["udid"], "uname", "-m"])
                if (status == 0 and version_matches(version, target.os_version_constraint)
                        and (target.cpu_architecture == "*" or _architecture(arch) == _architecture(target.cpu_architecture))):
                    matches.append((candidate["udid"], version, arch))
        if matches and target.required_device_mode != "physical":
            device, observed_version, observed_arch = sorted(matches)[0]
            display = DisplayObservation(protocol="ios", mode="simulator", interactive=True,
                session_fingerprint=canonical_digest({"device": device, "runtime": observed_version, "arch": observed_arch}))
            tools.append(ToolObservation(name="ios_runtime", path=device, version=observed_version, available=True))
        else:
            blocked.append("booted_ios_simulator_constraints_not_verified")
    aliases = {"macos": "darwin", "osx": "darwin", "ios": "ios", "android": "android"}
    requested_os = aliases.get(target.os_name.lower(), target.os_name.lower())
    if requested_os != "*" and requested_os != observed_os.lower():
        blocked.append(f"target_os_mismatch:{target.os_name}:{observed_os}")
    if not version_matches(observed_version, target.os_version_constraint):
        blocked.append(f"os_version_not_verified:{target.os_version_constraint}:{observed_version}")
    if target.cpu_architecture != "*" and _architecture(target.cpu_architecture) != _architecture(observed_arch):
        blocked.append(f"cpu_architecture_mismatch:{target.cpu_architecture}:{observed_arch}")
    for capability in target.required_capabilities:
        if capability == "protocol_calls":
            continue
        observed = display.interactive if capability == "interactive_session" else getattr(display, capability)
        if observed in {False, "unavailable"}:
            blocked.append(f"required_capability_unavailable:{capability}")
        elif observed == "unknown":
            limitations.append(f"required_capability_needs_functional_probe:{capability}")
    if target.ui_framework_version_constraint:
        version, detail = None, "No version probe is available for the declared UI framework"
        framework = (target.ui_framework or "").lower()
        if framework in {"gtk", "gtk3"}:
            code, detail = _command(["python3", "-c", "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk; print(f'{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}')"])
            version = detail if code == 0 else None
        elif framework == "wpf":
            code, detail = _command(["dotnet", "--list-runtimes"])
            versions = re.findall(r"Microsoft.WindowsDesktop.App\s+(\d+(?:\.\d+)+)", detail) if code == 0 else []
            version = next((v for v in versions if version_matches(v, target.ui_framework_version_constraint)), None)
        elif framework == "swiftui":
            sdk = "iphonesimulator" if target.app_target == AppTarget.IOS else "macosx"
            code, detail = _command(["xcrun", "--sdk", sdk, "--show-sdk-version"])
            version = detail if code == 0 else None
            limitations.append("SwiftUI version constraint is interpreted as its Apple SDK version")
        elif framework in {"android views", "android"}:
            version = observed_version if observed_os == "Android" and observed_version != "unknown" else None
            detail = "Native Android Views framework is bound to the observed Android runtime version"
        tools.append(ToolObservation(name="ui_framework", path=None, version=version, available=bool(version), detail=detail))
        if not version_matches(version, target.ui_framework_version_constraint):
            blocked.append(f"ui_framework_version_not_verified:{target.ui_framework}:{target.ui_framework_version_constraint}")
    return CapabilityReport(app_target=target.app_target, target_config_fingerprint=target.fingerprint,
                            os_name=observed_os, os_version=observed_version, architecture=observed_arch,
                            boot_fingerprint=current_boot_identity()[1],
                            display=display, tools=tools, state="blocked" if blocked else "static_verified",
                            blocking_reasons=blocked, limitations=limitations, observed_at=utc_now())


async def probe_target(target: TargetConfig) -> CapabilityReport:
    return await asyncio.to_thread(probe_target_sync, target)
