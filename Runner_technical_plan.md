# Pantadek — technical plan

## Purpose

Pantadek is a standalone, application-agnostic runner for human-readable end-to-end browser scenarios. It accepts a scenario suite and a reachable target URL, then asks Codex to execute each selected scenario through a browser MCP server.

Pantadek does not build, start, seed, reset, or modify the target application. The operator is responsible for preparing the target and its data. Scenarios that mutate state need independent data or an external reset process before each run.

## Inputs and outputs

- `src/pantadek/run.py` — standard-library host runner, scenario parsing, variant expansion, Docker lifecycle, and report validation.
- `worker.py` — isolated container bootstrap, MCP client, and Codex CLI invocation; contains no Docker client.
- `Runner_instructions.md` and `Result_schema.json` — generic instructions and required output structure.
- `pantadek.toml` in the user's project — suite directory, base URL, and optional runner settings. Relative suite paths resolve from the configuration file.
- `uvx pantadek` — runs the PyPI CLI on demand without adding Pantadek to the user's Python environment.
- `artifacts/` in the current project — ignored run manifests, reports, logs, and evidence.

The scenario format supports English and Polish headings. Alternatives in a `Variants`/`Warianty` section expand to combinations across selected desktop and mobile viewports. The runner validates scenario structure and limits case counts before contacting Docker.

## Execution architecture

For each case, Pantadek creates an isolated Compose project with two services: Chromium plus Playwright MCP, and a non-root Codex worker. The target app is outside this Compose project. Only the browser joins the optional target network. Codex can reach the browser MCP and model service but has no direct route to the target app.

Pantadek builds only its own pinned worker image. It mounts the selected suite read-only and gives the case result directory to the worker for evidence. Browser secrets are read from named environment variables and mounted only in browser configuration; reports and logs redact their values. Codex authentication is passed as an API key or a temporary copy of the configured auth file, never the full home directory.

Containers use read-only root filesystems, drop Linux capabilities, run without privilege escalation, and receive no Docker socket. The worker verifies these Docker isolation conditions before launching Codex. Codex's internal sandbox is disabled only inside this restricted container.

## Result validation

The result schema requires a status, summary, and one report entry for every numbered scenario step. A passed step requires a concrete observation and an existing evidence file located inside that case's result directory. A case passes only when every step passed and completed browser tool calls are recorded. Missing context, application defects, timeout, and automation problems remain visible as blocked or failed outcomes.

Each case receives its own report and evidence directory. Cleanup addresses only its uniquely named Compose project. Reports include the selected scenario, variant, viewport, model, run date/timezone, worker image, and suite commit when the suite lives in Git.

## Scope boundaries

The first version supports web applications through Chromium. It does not seed or reset databases, build application images, compare reference screenshots, or guarantee that the target state is isolated across cases. Native mobile execution and visual regression comparison remain future extensions.

The first user interface is a command-line tool. A web interface is a future client and should reuse the project-owned TOML configuration and scenario format.
