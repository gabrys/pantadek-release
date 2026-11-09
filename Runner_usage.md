# Using Pantadek

Pantadek is installed on demand with [`uvx`](https://docs.astral.sh/uv/guides/tools/). Your project keeps its own scenario suite and `pantadek.toml`; there is no need to install Pantadek into the project's Python environment.

Requirements: Python 3.11+, `uv`, Docker Engine with Compose v2, and Codex authentication for scenario execution. The target application must already be running.

## Set up in your project

From the root of the project that contains the application under test:

```bash
uvx pantadek --init
```

Edit `suite_dir` and `base_url`. Relative paths are resolved from the directory containing the TOML file. The suite directory must contain `scenarios/*.md`; optional `Specification.md`, `Glossary.md`, and `Setup.md` files provide additional context.

Example configuration:

```toml
suite_dir = "./qa"
base_url = "http://127.0.0.1:8000"
docker_command = ["docker"]
model = "gpt-6.1-terra"
case_timeout_seconds = 600
command_timeout_seconds = 300
max_cases = 20
locale = "en-US"
timezone = "UTC"
codex_auth_file = "~/.codex/auth.json"

# Optional existing Docker network when the target runs in Docker.
# docker_network = "my-app_default"

[secrets]
# Scenario-visible secret name = environment variable containing its value.
# PASSWORD = "TEST_PASSWORD"
```

Keep secret values in environment variables, never in the TOML file. Export any mapped variables before running the command. The browser receives the secret under the left-hand name.

## Run scenarios

```bash
uvx pantadek --list
uvx pantadek
uvx pantadek qa/scenarios/Login.md
uvx pantadek qa/scenarios/Login.md qa/scenarios/Checkout.md
uvx pantadek --platform desktop qa/scenarios/Login.md
uvx pantadek --variant account=primary qa/scenarios/Login.md
```

`--list` expands variants and desktop/mobile viewports without contacting Docker or Codex. By default all scenarios run on both viewports. Select scenarios by passing one or more paths to their files, relative to the current directory or absolute; each must be a `.md` file directly in `suite_dir/scenarios/`. Scenario names without a path are not accepted. Repeat `--platform` to select multiple platforms.

Codex authentication is read from `CODEX_API_KEY` or the configured auth file (default `~/.codex/auth.json`). Optional `docker_network` lets only the browser container reach the target's existing Docker network; without it, the target URL must be reachable from the browser's default network.

## Browser smoke check

```bash
uvx pantadek --smoke
```

This builds Pantadek's worker image, starts an isolated browser, and checks that the target URL and browser tools respond. It does not execute a scenario or require Codex authentication.

## Scenario suite format

Scenario files use English or Polish headings: `Goal`/`Cel`, `Preconditions`/`Warunki początkowe`, `Platform`/`Platforma`, `## Steps`/`## Kroki`, and `Expected result`/`Oczekiwany wynik`. Variant alternatives are written in a `Variants`/`Warianty` section, with slash-separated values in double quotes.

The operator prepares the target and test data. Pantadek does not seed, reset, or modify the target. Each case gets a fresh browser and isolated Codex container, but cases share target application state. Prepare independent data for scenarios that mutate state.

## Results and cleanup

Reports, manifests, and evidence are saved under `artifacts/<run_id>/` in the current project. Pantadek removes only its own per-run containers, network, and volumes after each case; worker images and reports remain.

Exit code `0` means every case passed (or the smoke check succeeded), `1` means a case failed or was blocked, `2` means runner configuration or infrastructure failed, and `130` means interrupted. Pantadek validates each step, status, observation, and evidence path before reporting a pass.

The current interface is a CLI. A web interface may be added later and can use the same TOML configuration and scenario suite.
