"""Seven concrete toolchain adapters; formal test plans never compile new binaries."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from agentflow.common import DomainError
from agentflow.execution.models import AppTarget, JobKind, TargetConfig, WireModel
from agentflow.execution.process import CommandSpec


def contained(root: Path, relative: str) -> Path:
    p = Path(relative)
    if p.is_absolute() or ".." in p.parts or not relative or relative.startswith("-"):
        raise DomainError("unsafe_recipe_path", "Recipe paths must stay inside the frozen workspace", 422)
    base = root.resolve()
    result = (base / p).resolve()
    if not result.is_relative_to(base):
        raise DomainError("unsafe_recipe_path", "Linked path escapes the frozen workspace", 422)
    return result


class BuildRecipe(WireModel):
    adapter: AppTarget
    project_path: str = "."
    test_project_path: str | None = None
    project_file: str | None = None
    scheme: str | None = None
    configuration: str = "Debug"
    unit_project: str | None = None
    gui_project: str | None = None
    xctestrun: str | None = None
    product_path: str | None = None
    test_product_path: str | None = None
    device_id: str | None = None
    application_id: str | None = None
    instrumentation_runner: str | None = None
    gradle_executable: str = "./gradlew"
    test_kind: Literal["unit", "gui", "api", "integration"] = "integration"
    test_selectors: list[str] = Field(default_factory=list)
    report_path: str = "test-results/report.xml"
    expected_case_ids: list[str] = Field(default_factory=list)
    framework_config: str | None = None
    xcodegen_spec: str | None = None
    output_paths: dict[str, str] = Field(default_factory=dict)
    service_urls: dict[str, str] = Field(default_factory=dict)
    service_target_config_ids: dict[str, str] = Field(default_factory=dict)
    runtime_port: int | None = Field(default=None, ge=1024, le=65535)
    scenario_inputs: dict[Literal["scene_id", "namespace", "ticket_title", "ticket_id", "expected_assignee"], str] = Field(default_factory=dict)

    @field_validator("service_urls")
    @classmethod
    def credential_free_service_origins(cls, values: dict[str, str]) -> dict[str, str]:
        for name, value in values.items():
            parsed = urlsplit(value)
            if (name not in {"api", "web"} or parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
                    or not re.fullmatch(r"https?://[A-Za-z0-9_.:\[\]-]+/?", value)):
                raise ValueError("Services require a literal credential-free HTTP(S) origin")
        return values

    @field_validator("scenario_inputs")
    @classmethod
    def literal_scenario_identifiers(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", value) for value in values.values()):
            raise ValueError("Cross-client ticket identifiers must be literal scenario-generated names")
        return values

    @field_validator("test_selectors")
    @classmethod
    def safe_selectors(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z0-9_./#:-]{1,240}", value) for value in values):
            raise ValueError("test selectors must be literal framework identifiers")
        return values

    @field_validator("scheme", "configuration", "application_id", "instrumentation_runner", "device_id")
    @classmethod
    def safe_identifier(cls, value: str | None) -> str | None:
        if value and (value.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.:/ -]{1,240}", value)):
            raise ValueError("invalid toolchain identifier")
        return value


class AdapterPlan(WireModel):
    app_target: AppTarget
    phase: JobKind
    commands: list[CommandSpec]
    report_format: Literal["junit", "playwright", "xcresult", "instrumentation", "none"]
    report_path: Path | None
    frozen_input_paths: list[Path] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class TestAdapter:
    target: AppTarget

    def plan(self, phase: JobKind, target: TargetConfig, recipe: BuildRecipe, workspace: Path) -> AdapterPlan:
        raise NotImplementedError

    @staticmethod
    def command(argv: list[str], cwd: Path, label: str, **env: str) -> CommandSpec:
        return CommandSpec(argv=tuple(argv), cwd=cwd, label=label, environment=env)


class WebApiAdapter(TestAdapter):
    def __init__(self, target: AppTarget):
        self.target = target

    def plan(self, phase: JobKind, target: TargetConfig, recipe: BuildRecipe, workspace: Path) -> AdapterPlan:
        cwd = contained(workspace, recipe.test_project_path if phase == JobKind.TEST and recipe.test_project_path else recipe.project_path)
        report = contained(workspace, recipe.report_path)
        if phase == JobKind.BUILD:
            commands = [self.command(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd, "restore locked dependencies"),
                        self.command(["npm", "run", "build"], cwd, "build frozen project")]
            fmt = "none"
        elif phase == JobKind.TEST:
            if recipe.test_kind == "unit":
                if not recipe.unit_project:
                    raise DomainError("invalid_recipe", "An explicit prebuilt Node unit suite is required", 422)
                return AdapterPlan(app_target=self.target, phase=phase, commands=[self.command([
                    "node", "--test", "--test-reporter=junit", f"--test-reporter-destination={report}",
                    str(contained(workspace, recipe.unit_project))], cwd, "run prebuilt Node unit tests")],
                    report_format="junit", report_path=report)
            cli = cwd / "node_modules" / "@playwright" / "test" / "cli.js"
            if not cli.is_file():
                raise DomainError("environment_blocked", "Preinstalled pinned Playwright is required", 409)
            args = ["node", str(cli), "test", "--reporter=json", "--output", str(report.parent / "playwright-artifacts")]
            if recipe.framework_config:
                args.extend(["--config", str(contained(workspace, recipe.framework_config))])
            if recipe.test_selectors:
                args += ["--grep", "|".join(re.escape(s) for s in recipe.test_selectors)]
            commands = [self.command(args, cwd, "execute prebuilt web/API tests",
                                     PLAYWRIGHT_JSON_OUTPUT_NAME=str(report),
                                     AGENTFLOW_SKIP_BUILD="1")]
            fmt = "playwright"
        elif phase == JobKind.INSTALL:
            commands = []  # Web/API uses the explicitly provisioned local service, not a native installer.
            fmt = "none"
        else:
            raise DomainError("unsupported_job", "Probe jobs use the capability runner", 422)
        return AdapterPlan(app_target=self.target, phase=phase, commands=commands,
                           report_format=fmt, report_path=report if fmt != "none" else None,
                           limitations=["Target service must be launched from the frozen manifest before testing"])


class AppleAdapter(TestAdapter):
    def __init__(self, target: AppTarget):
        self.target = target

    def plan(self, phase: JobKind, target: TargetConfig, recipe: BuildRecipe, workspace: Path) -> AdapterPlan:
        cwd = contained(workspace, recipe.project_path)
        report = contained(workspace, recipe.report_path)
        destination = "platform=macOS"
        if self.target == AppTarget.IOS:
            if not recipe.device_id:
                raise DomainError("environment_blocked", "An explicit simulator/device ID is required")
            destination = f"platform={'iOS Simulator' if target.required_device_mode == 'simulator' else 'iOS'},id={recipe.device_id}"
        common = ["-destination", destination]
        commands = []
        frozen = []
        fmt = "none"
        if phase == JobKind.BUILD:
            if not recipe.project_file or not recipe.scheme:
                raise DomainError("invalid_recipe", "Xcode project and scheme are required", 422)
            if recipe.xcodegen_spec:
                commands.append(self.command(["xcodegen", "generate", "--spec", str(contained(workspace, recipe.xcodegen_spec))],
                                             cwd, "generate declared Xcode project"))
            args = ["xcodebuild", "build-for-testing", "-project", str(contained(workspace, recipe.project_file)),
                    "-scheme", recipe.scheme, "-configuration", recipe.configuration,
                    "-derivedDataPath", str(workspace / "build"), *common]
            commands.append(self.command(args, cwd, "compile application and XCTest bundles"))
        elif phase == JobKind.INSTALL:
            if not recipe.product_path:
                raise DomainError("invalid_recipe", "Frozen application path is required", 422)
            product = contained(workspace, recipe.product_path)
            frozen.append(product)
            if self.target == AppTarget.IOS:
                if target.required_device_mode != "simulator":
                    raise DomainError("environment_blocked", "Physical iOS install requires a verified device adapter")
                commands.append(self.command(["xcrun", "simctl", "install", recipe.device_id, str(product)], cwd,
                                             "install exact iOS simulator bundle"))
            # macOS reference app is a staged portable bundle. XCUITest launches it in the test phase.
        elif phase == JobKind.TEST:
            if not recipe.xctestrun:
                raise DomainError("invalid_recipe", "A frozen .xctestrun and prebuilt bundles are required", 422)
            run = contained(workspace, recipe.xctestrun)
            if "*" in recipe.xctestrun:
                matches = sorted(workspace.glob(recipe.xctestrun))
                if len(matches) != 1 or matches[0].is_symlink():
                    raise DomainError("xctestrun_ambiguous", "Frozen bundle must contain exactly one matching xctestrun")
                run = matches[0]
            frozen.append(run)
            if recipe.product_path:
                frozen.append(contained(workspace, recipe.product_path))
            if recipe.test_product_path:
                frozen.append(contained(workspace, recipe.test_product_path))
            args = ["xcodebuild", "test-without-building", "-xctestrun", str(run), *common,
                    "-resultBundlePath", str(report)]
            args.extend(f"-only-testing:{selector}" for selector in recipe.test_selectors)
            commands.append(self.command(args, cwd, "run prebuilt XCTest/XCUITest"))
            fmt = "xcresult"
        else:
            raise DomainError("unsupported_job", "Unsupported Apple job phase", 422)
        return AdapterPlan(app_target=self.target, phase=phase, commands=commands, report_format=fmt,
                           report_path=report if fmt != "none" else None, frozen_input_paths=frozen)


class AndroidAdapter(TestAdapter):
    target = AppTarget.ANDROID

    def plan(self, phase: JobKind, target: TargetConfig, recipe: BuildRecipe, workspace: Path) -> AdapterPlan:
        cwd = contained(workspace, recipe.project_path)
        report = contained(workspace, recipe.report_path)
        commands, frozen = [], []
        fmt = "none"
        if phase == JobKind.BUILD:
            gradle = "gradle" if recipe.gradle_executable == "gradle" else str(contained(cwd, recipe.gradle_executable))
            commands.append(self.command([gradle, "--no-daemon", "assembleDebug", "assembleDebugAndroidTest",
                                           "packageAgentFlowTests"], cwd, "compile APK and test artifacts"))
        elif phase == JobKind.INSTALL:
            if not recipe.device_id or not recipe.product_path or not recipe.test_product_path:
                raise DomainError("invalid_recipe", "ADB device and exact product/test APKs are required", 422)
            for value in [recipe.product_path, recipe.test_product_path]:
                path = contained(workspace, value)
                frozen.append(path)
                commands.append(self.command(["adb", "-s", recipe.device_id, "install", "-r", str(path)], cwd,
                                             "install exact frozen APK"))
        elif phase == JobKind.TEST:
            if recipe.test_kind == "unit":
                if not recipe.test_product_path:
                    raise DomainError("invalid_recipe", "Prebuilt JUnit Console standalone test jar is required", 422)
                jar = contained(workspace, recipe.test_product_path)
                frozen.append(jar)
                commands.append(self.command(["java", "-jar", str(jar), "execute", f"--scan-class-path={jar}",
                                             "--reports-dir", str(report.parent)], cwd, "run prebuilt Android JVM unit tests"))
                fmt = "junit"
            else:
                if not recipe.device_id or not recipe.instrumentation_runner:
                    raise DomainError("invalid_recipe", "Device and instrumentation runner are required", 422)
                args = ["adb", "-s", recipe.device_id, "shell", "am", "instrument", "-w", "-r"]
                if recipe.test_selectors:
                    args += ["-e", "class", ",".join(recipe.test_selectors)]
                if recipe.service_urls.get("api"):
                    args += ["-e", "apiBaseUrl", recipe.service_urls["api"]]
                if recipe.scenario_inputs.get("ticket_title"):
                    args += ["-e", "crossClientTicketTitle", recipe.scenario_inputs["ticket_title"]]
                for name, value in recipe.scenario_inputs.items():
                    argument = {"scene_id": "sceneId", "ticket_title": "ticketTitle", "ticket_id": "ticketId",
                                "namespace": "namespace", "expected_assignee": "expectedAssignee"}[name]
                    args += ["-e", argument, value]
                args.append(recipe.instrumentation_runner)
                commands.append(self.command(args, cwd, "execute installed Espresso/UI Automator tests"))
                fmt = "instrumentation"
        else:
            raise DomainError("unsupported_job", "Unsupported Android phase", 422)
        return AdapterPlan(app_target=self.target, phase=phase, commands=commands, report_format=fmt,
                           report_path=report if fmt != "none" else None, frozen_input_paths=frozen)


class WindowsAdapter(TestAdapter):
    target = AppTarget.WINDOWS

    def plan(self, phase: JobKind, target: TargetConfig, recipe: BuildRecipe, workspace: Path) -> AdapterPlan:
        cwd = contained(workspace, recipe.project_path)
        report = contained(workspace, recipe.report_path)
        if not recipe.project_file:
            raise DomainError("invalid_recipe", "A frozen solution/project is required", 422)
        project = contained(workspace, recipe.project_file)
        commands, frozen = [], []
        fmt = "none"
        if phase == JobKind.BUILD:
            commands.append(self.command(["dotnet", "restore", str(project), "--locked-mode"], cwd, "restore locked packages"))
            commands.append(self.command(["dotnet", "build", str(project), "--no-restore", "--configuration",
                                           recipe.configuration], cwd, "compile product and test assemblies"))
        elif phase == JobKind.INSTALL:
            if not recipe.product_path:
                raise DomainError("invalid_recipe", "Frozen executable path required", 422)
            frozen.append(contained(workspace, recipe.product_path))
            # Portable WPF reference application is already staged; GUI runner launches it.
        elif phase == JobKind.TEST:
            test = recipe.unit_project if recipe.test_kind == "unit" else recipe.gui_project
            if not test:
                raise DomainError("invalid_recipe", "Explicit prebuilt test assembly/project required", 422)
            test_path = contained(workspace, test)
            frozen.append(test_path)
            args = ["dotnet", "test", str(test_path), "--no-build", "--no-restore",
                    "--configuration", recipe.configuration, "--logger", f"junit;LogFilePath={report}"]
            if recipe.test_selectors:
                args.extend(["--filter", "|".join(f"FullyQualifiedName~{selector}" for selector in recipe.test_selectors)])
            commands.append(self.command(args, cwd, "run prebuilt NUnit/FlaUI tests"))
            fmt = "junit"
        else:
            raise DomainError("unsupported_job", "Unsupported Windows phase", 422)
        return AdapterPlan(app_target=self.target, phase=phase, commands=commands, report_format=fmt,
                           report_path=report if fmt != "none" else None, frozen_input_paths=frozen)


class LinuxNativeAdapter(TestAdapter):
    target = AppTarget.LINUX

    def plan(self, phase: JobKind, target: TargetConfig, recipe: BuildRecipe, workspace: Path) -> AdapterPlan:
        cwd = contained(workspace, recipe.project_path)
        report = contained(workspace, recipe.report_path)
        if target.required_display_protocol not in {"x11", "wayland"}:
            raise DomainError("display_not_frozen", "Linux reference tests require an explicit X11/Wayland combination")
        if target.required_display_protocol == "wayland":
            raise DomainError("environment_blocked", "Wayland input/capture adapter has not been functionally verified")
        if phase == JobKind.BUILD:
            commands = [self.command(["python3", "-m", "compileall", "-q", "."], cwd,
                                      "validate/package frozen GTK Python application")]
            fmt = "none"
        elif phase == JobKind.INSTALL:
            commands, fmt = [], "none"  # Frozen Python application is staged; no system-wide install.
        elif phase == JobKind.TEST:
            tests = recipe.unit_project if recipe.test_kind == "unit" else recipe.gui_project
            if not tests:
                raise DomainError("invalid_recipe", "Explicit frozen native test directory required", 422)
            commands = [self.command(["python3", "-m", "pytest", str(contained(workspace, tests)),
                                       f"--junitxml={report}", "-p", "no:cacheprovider"], cwd,
                                      "run GTK/AT-SPI/dogtail tests", PYTHONDONTWRITEBYTECODE="1")]
            fmt = "junit"
        else:
            raise DomainError("unsupported_job", "Unsupported Linux phase", 422)
        return AdapterPlan(app_target=self.target, phase=phase, commands=commands, report_format=fmt,
                           report_path=report if fmt != "none" else None)


ADAPTERS: dict[AppTarget, TestAdapter] = {
    AppTarget.WEB: WebApiAdapter(AppTarget.WEB), AppTarget.API: WebApiAdapter(AppTarget.API),
    AppTarget.IOS: AppleAdapter(AppTarget.IOS), AppTarget.MACOS: AppleAdapter(AppTarget.MACOS),
    AppTarget.ANDROID: AndroidAdapter(), AppTarget.WINDOWS: WindowsAdapter(),
    AppTarget.LINUX: LinuxNativeAdapter(),
}


def plan_execution(phase: JobKind, target: TargetConfig, recipe: BuildRecipe, workspace: Path) -> AdapterPlan:
    if target.app_target != recipe.adapter:
        raise DomainError("adapter_target_mismatch", "Recipe adapter cannot replace the declared target", 422)
    return ADAPTERS[target.app_target].plan(phase, target, recipe, workspace)
