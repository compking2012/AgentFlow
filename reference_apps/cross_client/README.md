# Ordered cross-client fixture (LWP-38)

English | [简体中文](README.zh-CN.md)

This scenario is implemented in real native controls but has **not** been run on
Android and Linux environments. It is separate from the seven target smoke recipes.
The controller still needs to bind and schedule these ordered jobs against the same
frozen source, platform manifest, service identity, ticket ID and unique title.

1. Start the frozen reference API. Its `/api/version` must return the candidate's
   source fingerprint and the actual API product content digest. Create one ticket
   through `POST /api/tickets`; preserve its ID and unique title as scenario inputs.
2. Run the compiled Android instrumentation class `TicketCrossClientTest`, method
   `assignApiTicketUsingNativeControl`, with `apiBaseUrl` and `crossClientTicketTitle`.
   Espresso finds the API-created row, clicks its native “Assign to member” control,
   and verifies that assignment after Activity recreation.
3. Run the frozen GTK test `tests/cross_client/test_assignment.py` with the same
   `AGENTFLOW_API_URL` and `AGENTFLOW_CROSS_TICKET_TITLE`. AT-SPI/dogtail must read
   Android's member assignment and click the native “Assign to manager” control.
4. Run bundled `playwright.cross.config.mjs` with the same title and API URL. The
   browser verifies Linux's manager assignment before and after reload.

No later step may start when a preceding report is missing, failed or unbound. API
writes cannot replace either native click. Replacing the ticket, backend or artifact
between steps invalidates the scenario. The coordinator must preserve raw framework
reports, actual component identities, job leases and the shared service binding.

Native raw case identifiers are provisional until discovered from the real SDK and
GUI environment. This document and these source tests are not a passing report or
evidence that cross-node scheduling is already supported by the controller.
