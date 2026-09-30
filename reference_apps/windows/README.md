Restore and commit `packages.lock.json` using the selected .NET SDK before freezing
the first source candidate. Frozen builds use locked restore; the node does not
silently accept a new dependency graph. Build `TicketTests/TicketTests.csproj`,
which also builds the WPF product. Formal tests use the prebuilt test assembly and
`dotnet test --no-build --no-restore`. Set `AGENTFLOW_APP_PATH` to the exact frozen
WPF executable and `AGENTFLOW_API_URL` to the real reference service. A Windows
interactive desktop with UI Automation support is mandatory; source availability
is not a claim that this fixture has already executed on Windows.
