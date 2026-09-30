# AgentFlow

English | [简体中文](README.zh-CN.md)

A local-first, single-owner workspace for agent-driven software delivery. Give agents a product goal and follow research, requirements, parallel development, independent review, testing, and delivery from your local dashboard.

The current primary workflow supports Node.js Web/API products on macOS, with five everyday commands. Native platforms can be recorded as product metadata, but automated native-product execution is not currently supported.

## Install the Development Version

Prerequisites: macOS, Python 3.12, Git, uv, Node.js 22.13+, npm 10+, and a supported Codex CLI. Install the specialist-role runtime with the dependencies below. Coding uses your configured model API; a personal subscription login does not replace provider configuration.

```sh
git clone https://github.com/compking2012/AgentFlow.git
cd AgentFlow
uv sync --locked --extra openhands --extra dev
npm --prefix apps/dashboard ci
npm --prefix apps/dashboard run build
source .venv/bin/activate
agentflow start
```

On first launch, AgentFlow creates a private configuration file at **`~/.config/agentflow/config.toml`**. Configure your model provider in the dashboard before submitting a goal. See the [local operations guide](docs/local-operations.md) for configuration details; paths labeled as the maintainer's current Mac environment are examples and must be adapted to your machine.

While the service is running, open `http://127.0.0.1:8787` directly (or your configured port). New tabs and controller restarts do not require a startup link. The service still starts manually; no login autostart is installed.

## Everyday Commands

```sh
agentflow start
agentflow run "Build a team reading list with add, read status, filtering, and persistent storage"
agentflow status
agentflow launch PRODUCT_ID
agentflow stop PRODUCT_ID
```

| Command | Purpose |
| --- | --- |
| `agentflow start` | Start the platform in the background and open the dashboard; reopen it if already running. |
| `agentflow run "goal"` | Create a product and follow progress. Ctrl-C stops observation, not development. |
| `agentflow status [PRODUCT_ID]` | Show overall status or a specific product. |
| `agentflow launch PRODUCT_ID` | Preview a product that has passed delivery gates. |
| `agentflow stop [PRODUCT_ID]` | Stop the platform without an ID, or stop a product preview with an ID. Cancel development in the dashboard. |

The `run` command accepts only two optional per-run inputs: `--name` and `--output`.

```sh
agentflow run "Build a page for adding and archiving tasks" --name "Task list" --output "$HOME/Products/tasks"
```

Models, target types, human-review policy, call limits, execution time limits, default output directories, platform data directories, and ports are configured in the fixed TOML file, not through command-line switches. Dashboard model settings write to the same file. After editing it manually, run `agentflow stop`, wait for shutdown, then run `agentflow start`.

Specialist roles require native **Chat Completions**, while coding roles require native **Responses**. Providers, endpoints, and models can be configured separately. AgentFlow does not silently convert incompatible protocols or substitute models. Enter API keys in the dashboard or reference protected environment variables from the configuration file. Never put a configuration file containing keys in a product repository.

## Dashboard Workflow

The dashboard includes product creation, product management, requirement changes, and an execution console, with parallel roles, named Markdown artifacts, quality evidence, and explicit recovery.

| Action | Entry point | Behavior |
| --- | --- | --- |
| Create or import a product | Create product | Adds it to your products; importing preserves the original repository. |
| Rename, edit future defaults, delete, or restore | My products | Does not start a run. Deletion is recoverable and preserves source and history. |
| Change the product goal or platform | My products → Edit configuration → Run from scratch | Saving marks the product for a rerun; explicit confirmation starts a new full run and iteration from the goal. |
| Change features within the existing goal | Requirement changes | Starts a new iteration from the PRD using valid background from the current configuration. |
| Recover failed or paused progress | Execution console → Retry interrupted step / Continue | Preserves the run, cumulative budget, and successful results; revalidates affected work. |
| Refresh the dashboard | Current browser tab | Reconnects automatically. Use the local startup link for first access or after a controller restart. |

AgentFlow prepares a managed local execution environment and advances through quality gates. Missing environment support, model configuration, grounding, or test evidence blocks progress. Built-in environment samples are not implementations of your target product.

If preparation or export fails, use the corresponding retry action in My products. Export retry only re-exports confirmed deliverables; it does not rerun models. Retrying development handles failed and affected work, while continuing resumes paused scheduling. Neither automatically increases call limits nor resets costs.

## Delivery and Local Access

After delivery, download the source package or launch the product locally. The output directory contains `repository/`, an initial `release/`, subsequent `releases/<run-id>/`, and a separate `runtime/`. Deliverables retain runnable packages, exact source, development documents, readable reports, and raw test evidence. Delivery currently means local Git and files—not remote pushes, merges, or deployment.

Keep platform data, product directories, and existing Git repositories separate. Existing tabs reconnect after refresh or owner-credential expiry. For first access, controller restarts, or a reconnect ticket unused for seven days, run `agentflow start` again. This does not recreate existing development tasks.

Owner credentials remain in page memory. Restricted reconnect tickets are stored only in `sessionStorage`, isolated by origin and port, not in cookies or localStorage. They can reconnect a session but cannot directly execute management commands. Successful use renews their seven-day lifetime; controller restarts invalidate them immediately. The backend validates exact Host/Origin values. Operations with unknown outcomes are not automatically resent; retries after explicit authentication failure retain the original idempotency key.

## Verification and Limitations

This is a development version. Protocol servers, static templates, and built-in samples are not evidence of autonomous delivery by real models. Local `validation/` records, user product information, and test artifacts are excluded from the public repository. Reproduce checks in your own environment rather than treating these commands as acceptance claims.

Request counts and execution time limits are not monetary budgets. Costs remain unknown when reliable pricing is unavailable; actual billing is determined by your provider.

```sh
uv run --locked ruff check src tests
uv run --locked pytest tests -q
npm --prefix apps/dashboard run typecheck
npm --prefix apps/dashboard run build
npm --prefix apps/dashboard run test:browser
```

Browser, end-to-end, and packaging verification may require additional environment setup; see the test documentation and [contribution guide](CONTRIBUTING.md). Paid live-model tests require explicit configuration and opt-in.

For existing projects, self-managed nodes, native environments, evidence reconciliation, and backup/recovery, see the [advanced Python API guide](docs/advanced-python-api.md). These capabilities do not add public CLI commands or hidden flags. Native SDKs, devices, signing, desktop environments, and cross-platform backends require their own prerequisites and verification.

## Project Structure

| Directory | Contents |
| --- | --- |
| `src/agentflow/` | Local controller, CLI, workflows, and runtime |
| `src/node_agent/` | Execution-node components |
| `apps/dashboard/` | React / TypeScript dashboard |
| `contracts/` | API contracts |
| `tests/` | Unit, integration, browser, and end-to-end tests |
| `reference_apps/` | Environment and cross-platform verification samples, not automatically generated products |
| `docs/` | Product plans, architecture, and operations guides |
| `scripts/` | Packaging and verification tools |

## Contributing and Security

See [CONTRIBUTING.md](CONTRIBUTING.md) for issue and pull-request guidance. Never include API keys, user configuration, or private product data in issues, pull requests, logs, or screenshots. See [SECURITY.md](SECURITY.md) for reporting security concerns. Model services may incur charges; check provider configuration and limits before running.

## License

AgentFlow is licensed under the [MIT License](LICENSE). Third-party dependencies and external tools remain subject to their own licenses and terms of service.
