# AgentFlow frozen reference applications

English | [简体中文](README.zh-CN.md)

These fixtures exercise real APIs and native controls. They are test applications,
not production authentication examples. The two synthetic identities are
`reference.manager` and `reference.member`. Business expectations are declared in
`expectations.json` and must not be changed to accommodate a failing implementation.

The shared Web/API fixture is locally runnable. Native fixtures include source,
build recipes, unit tests and GUI tests, but require their actual OS/SDK/desktop.
An unavailable environment must report blocked; no native test uses a skip as a pass.

| Target | Source | Required host/toolchain |
|---|---|---|
| Web / API | `web_api` | Node 22.13+, npm, installed Playwright Chromium |
| iOS | `apple/ios` | macOS, Xcode, XcodeGen, booted declared iOS Simulator |
| macOS | `apple/macos` | macOS, Xcode, XcodeGen, GUI automation permissions |
| Android | `android` | JDK 17, Gradle 8.7, Android SDK 35, authorized AVD/device |
| Windows | `windows` | Windows interactive desktop, .NET 8 SDK, NuGet dependencies |
| Linux native | `linux_native` | Linux GNOME/X11, Python, GTK3, AT-SPI2, dogtail, pytest |

Native GUI tests require `AGENTFLOW_API_URL` pointing at the real reference API;
Android instrumentation receives the same URL through `apiBaseUrl`. Missing
backends and controls fail the tests rather than silently skipping them.

`AGENTFLOW_FAULT_MODE=lost_save` and `allow_unauthorized` select deliberate backend
defects while leaving test expectations unchanged. Their purpose is to prove that
the suite detects faults. Do not enable a defect for the normal reference run.

Start the API with `node web_api/bundle/product/server.js`. The default port is 8765.
Each test execution must have an isolated data directory and a reserved port.

Native build outputs and test packages must be frozen before formal tests. The
Apple recipes use build-for-testing / test-without-building; Windows uses no-build;
Android ships prebuilt APK/instrumentation and standalone JVM test packages.

The node supervisor is distinct from generated project processes. A node must use
verified OS isolation or explicitly configured trusted-project mode for these known
fixtures. OS/SDK availability and an application's passing test report are separate
facts; this repository's presence does not advertise a verified platform deployment.

## Seven-target configuration templates

The following files describe the complete reference scope. They are **configuration
templates**, with schema validation completed. They are not execution receipts or
pre-approved plans.

| File | Contract and use |
| --- | --- |
| `agentflow.project.json` | `ProjectExecutionSpec`; one build, unit and integration recipe for every target, plus explicit iOS/Android installation. |
| `target-configs.json` | Array of seven `TargetConfig` values for the owner's run-plan `target_configs` field. Stable IDs associate recipes and cases. |
| `test-plan.json` | `TEST_PLAN_SCHEMA`; 26 required case mappings. For the ordinary workflow, register accepted unit/integration planning artifacts by phase, keeping these identities and mappings intact. |

All paths in the recipes are relative to **this directory as a standalone source
repository root**. Materialize the versioned reference source and configuration into
a separate Git repository before importing it as a project. The pipeline reads
`agentflow.project.json` at the candidate commit's root; pointing it at the parent
AgentFlow repository without relocating and updating the recipes is not equivalent.
Do not include local `node_modules`, `bundle`, reports or native build directories in
the source snapshot.

The templates deliberately retain `OWNER_*` values. Replace every such value before
accepting the run plan, and commit the resulting configuration together with the
source. Unresolved values are not wildcards or permission to relax a requirement.

- Set each OS version and CPU architecture from the selected environment's actual
  capability report. Web/API currently show a Darwin host example; selecting a Linux
  host requires an explicit configuration revision. iOS/Android fields describe the
  device runtime, while their build host and SDK still have to meet the requirements.
- Set verified tool/SDK version constraints. iOS and macOS require actual Xcode and
  XcodeGen versions; Android source declares compile SDK 35, JDK 17 and Gradle 8.7.
  A source declaration is not proof that the SDK exists on a node.
- Replace every resource ID with a registered workspace, device or desktop resource.
  Replace **each** `OWNER_DEVICE_ID` with the intended booted iOS Simulator UDID or
  authorized Android emulator serial, and select the matching device model/runtime.
- Replace `OWNER_REFERENCE_API_HOST` with the real reference service reachable from
  that execution environment. An Android emulator may need a host gateway address;
  a remote native node must not assume that its own loopback is the controller.
  Reserve the port and data directory, start the API from the current frozen product,
  and establish its artifact identity. Set `AGENTFLOW_SOURCE_FINGERPRINT` to the
  frozen source fingerprint when launching that API. Its `/api/version` calculates
  the running package's actual content digest. `service_target_config_ids` selects
  the frozen API component, and the node/controller require both source and product
  identities to match before accepting a native test. An unbound service is blocked.
  Web/API recipes instead start their own frozen local server by default.
- Web GUI execution needs the pinned Playwright browser and an actual browser probe.
  Native desktop automation needs the applicable screen, input and accessibility
  permissions. Missing resources or permissions remain blocked.

## Frozen output layout

| Target | Product output | Prebuilt test output | Formal test selection |
| --- | --- | --- | --- |
| Web / API | `web_api/bundle/product` | `web_api/bundle/tests` | Node unit file; separate bundled Playwright Web/API configs. |
| iOS | `build/Build/Products/Debug-iphonesimulator/TicketIOS.app` | `build/Build/Products` | `TicketIOSUnitTests` / `TicketIOSUITests` in the one matching `.xctestrun`. |
| Android | `android/app/build/outputs/apk/debug` | `android/app/build/agentflow-tests` | Standalone JVM unit jar / installed Espresso instrumentation APK. |
| Windows | `windows/TicketClient/bin/Debug/net8.0-windows` | `windows/TicketTests/bin/Debug/net8.0-windows` | `RulesTests` / `GuiTests` in the frozen `TicketTests.dll`, using separate class filters. |
| macOS | `build/Build/Products/Debug/TicketMac.app` | `build/Build/Products` | `TicketMacUnitTests` / `TicketMacUITests` in the one matching `.xctestrun`. |
| Linux | `linux_native` | `linux_native/tests` | Separate unit and GUI pytest directories on the declared X11 environment. |

Apple's complete Products package retains the `.xctestrun`, dependent test bundles
and its host app. Linux's product package also contains the reference tests. The
overlap must contain byte-identical files when the controller materializes the
packages. These layouts still require a successful build on their actual platform.

Reports are written under workspace-root `reports/`, outside frozen product and test
packages. Test-framework scratch files must also stay outside those packages; a
formal run that mutates a frozen package must fail. The Android unit report template
expects the JUnit Vintage XML because the source uses JUnit 4; confirm its filename
and raw case identifiers with the selected console version.

Windows requires actual `packages.lock.json` files for both projects before the
first frozen build; see `windows/README.md`. The same test assembly contains the
NUnit rules and FlaUI classes, so the two phase-specific class selectors must be
honored. A full-suite run under the unit label does not establish phase separation.

## Case identity and evidence status

The three Node unit IDs and the three API/two Web Playwright IDs were read with the
production report parsers from actual local reports under `web_api/reports/`.
Playwright IDs intentionally retain their trailing `::` project-name separator.
The reports are local ignored evidence and must be regenerated for a new candidate.

All iOS, Android, Windows, macOS and Linux raw report IDs in the templates are
**provisional, derived from source declarations**. They have not been confirmed by
native execution on the selected environments. XCTest export identifiers, NUnit
logger names, JUnit Vintage names and pytest classname prefixes can vary by tool
version and discovery root. Calibrate them from actual raw discovery/execution
reports, verify that they still represent the intended source cases, and create a
new accepted plan/configuration version before formal validation. Do not replace
failed cases, shrink the plan, or treat a renamed/missing case as a pass.

`test-plan.json` records these unknowns explicitly. Seven schema-valid target
entries, a completed static probe, or the existing local Web/API reports do not
establish seven-platform support or a delivered Git candidate.
