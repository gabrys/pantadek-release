# Pantadek

Pantadek is an application-agnostic end-to-end browser testing tool. Add a human-readable QA suite and a `pantadek.toml` file to your project, then run the scenarios with `uvx pantadek`. Pantadek uses Codex and a Playwright MCP browser in an isolated Docker environment.

The target application must already be running and reachable from the browser container. Pantadek does not build, seed, reset, or modify the target application. The user project supplies a scenario suite and a TOML configuration with the base URL and runner options. Results are written to `artifacts/` in the current project.

```bash
uvx pantadek --init
# Edit suite_dir and base_url, then:
uvx pantadek --list
uvx pantadek
```

See [Runner_usage.md](Runner_usage.md) for scenario and configuration details. The design and isolation boundaries are in [Runner_technical_plan.md](Runner_technical_plan.md). A web interface is a possible future client; the initial release is a CLI.

Pantadek is licensed under [GNU AGPL v3 or later](LICENSE).
