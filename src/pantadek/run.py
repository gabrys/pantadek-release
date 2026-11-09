#!/usr/bin/env python3
"""Isolated QA orchestration. Host-side Python uses only the standard library."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parent
STATUSES = {"passed", "failed", "blocked", "skipped"}
CONFIG_TEMPLATE = '''# Pantadek configuration for this project.
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

# Optional existing Docker network for the browser to reach your app.
# docker_network = "my-app_default"

# Map browser-visible secret names to environment variable names.
[secrets]
# PASSWORD = "TEST_PASSWORD"
'''
TOOLS = ["browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click",
         "browser_fill_form", "browser_type", "browser_press_key", "browser_select_option",
         "browser_wait_for", "browser_tabs", "browser_close", "browser_take_screenshot"]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_config(config_path):
    config_path = Path(config_path).expanduser().resolve()
    try:
        raw = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"Cannot read TOML configuration {config_path}: {error}") from error
    allowed = {"suite_dir", "base_url", "docker_network", "docker_command", "model",
               "case_timeout_seconds", "command_timeout_seconds", "max_cases", "locale",
               "timezone", "codex_auth_file", "secrets"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("Configuration has unknown keys: " + ", ".join(sorted(set(raw) - allowed)))
    required = {"suite_dir", "base_url"}
    if required - set(raw):
        raise ValueError("Configuration requires suite_dir and base_url.")
    def setting(key, default):
        return raw.get(key, default)

    command = setting("docker_command", ["docker"])
    if (not isinstance(command, list) or not command or
        any(not isinstance(part, str) or not part or "\x00" in part for part in command)):
        raise ValueError("docker_command must be a non-empty array of command arguments.")
    model = setting("model", "gpt-6.1-terra")
    if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9_.:/-]+", model):
        raise ValueError("Invalid model name.")
    config = {"model": model, "docker_command": command}
    for key, value in [("case_timeout_seconds", setting("case_timeout_seconds", 600)),
                       ("command_timeout_seconds", setting("command_timeout_seconds", 300)),
                       ("max_cases", setting("max_cases", 20))]:
        if type(value) is not int or value < 1:
            raise ValueError(f"{key} must be a positive integer.")
        config[key] = value
    config["viewports"] = {"desktop": {"width": 1280, "height": 1000},
                            "mobile": {"width": 390, "height": 844}}
    base_url = raw["base_url"]
    if not isinstance(base_url, str):
        raise ValueError("base_url must be a string.")
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("base_url must be an HTTP(S) URL without embedded credentials.")
    config["base_url"] = base_url.rstrip("/")
    suite_dir = raw["suite_dir"]
    if not isinstance(suite_dir, str) or not suite_dir:
        raise ValueError("suite_dir must be a non-empty path.")
    config["suite_dir"] = (config_path.parent / Path(suite_dir).expanduser()).resolve()
    if not (config["suite_dir"] / "scenarios").is_dir():
        raise ValueError("suite_dir must contain a scenarios directory.")
    config["docker_network"] = setting("docker_network", None)
    if config["docker_network"] and (not isinstance(config["docker_network"], str) or
                                     not re.fullmatch(r"[a-zA-Z0-9_.-]+", config["docker_network"])):
        raise ValueError("Invalid Docker network name.")
    config["locale"] = setting("locale", "en-US")
    if not isinstance(config["locale"], str) or not re.fullmatch(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*", config["locale"]):
        raise ValueError("Invalid browser locale.")
    config["timezone"] = setting("timezone", "UTC")
    try:
        if not isinstance(config["timezone"], str):
            raise ValueError("timezone must be a string")
        ZoneInfo(config["timezone"])
    except Exception as error:
        raise ValueError("Invalid timezone.") from error
    config["config_dir"] = config_path.parent
    secrets = {}
    secret_map = setting("secrets", {})
    if not isinstance(secret_map, dict):
        raise ValueError("secrets must be a TOML table mapping browser names to environment variable names.")
    for name, env_name in secret_map.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or not isinstance(env_name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
            raise ValueError("Secret mappings must use NAME = \"ENVIRONMENT_VARIABLE\".")
        if env_name not in os.environ:
            raise ValueError(f"Environment variable {env_name} is not set.")
        secrets[name] = os.environ[env_name]
    config["secrets"] = secrets
    auth = setting("codex_auth_file", "~/.codex/auth.json")
    if not isinstance(auth, str) or not auth:
        raise ValueError("codex_auth_file must be a non-empty path.")
    config["codex_auth_file"] = str((config_path.parent / Path(auth).expanduser()).resolve())
    return config


@dataclass
class Case:
    scenario: Path
    variant: dict
    platform: str
    viewport: dict
    steps: list

    def describe(self):
        return f"{self.scenario.stem} | {json.dumps(self.variant, ensure_ascii=False)} | {self.platform}"


def parse_scenario(path):
    text = path.read_text()
    expected_header = next((value for value in ("Expected result:", "Oczekiwany wynik:") if value in text), None)
    steps_header = next((value for value in ("## Steps", "## Kroki") if value in text), None)
    required_groups = [("Goal:", "Cel:"), ("Preconditions:", "Warunki początkowe:"),
                       ("Platform:", "Platforma:")]
    if expected_header is None or steps_header is None or any(
        not any(label in text for label in group) for group in required_groups
    ):
        raise ValueError(f"Incomplete scenario: {path.name}")
    section = text.split(steps_header, 1)[1].split(expected_header, 1)[0]
    numbered = re.findall(r"^(\d+)\.\s+(.+)$", section, re.M)
    if not numbered or [int(n) for n, _ in numbered] != list(range(1, len(numbered) + 1)):
        raise ValueError(f"Invalid step numbering: {path.name}")
    variants = {}
    # Restrict alternative expansion to the variants section or inline declaration.
    lines = text.split(steps_header, 1)[0].splitlines()
    in_variants = False
    for line in lines:
        if re.match(r"^##\s+(Warianty|Variants)", line):
            in_variants = True
            continue
        if line.startswith("## "):
            in_variants = False
        if line.startswith(("Warianty:", "Variants:")) or in_variants:
            line = line.removeprefix("Warianty:").removeprefix("Variants:")
            for key, values in re.findall(r'([^:"\n]+):\s*"([^"]*)"', line):
                key = key.strip().lstrip("- ")
                options = list(dict.fromkeys(v.strip() for v in values.split("/")))
                if not key or not all(options) or key in variants:
                    raise ValueError(f"Invalid variants: {path.name}")
                variants[key] = options
    return [step for _, step in numbered], variants


def select_scenarios(arguments, scenario_dir):
    """Resolve scenario file paths; without arguments select every scenario in the suite."""
    scenario_dir = scenario_dir.resolve()
    if not arguments:
        return sorted(p for p in scenario_dir.glob("*.md") if p.name != "README.md")
    paths = []
    for argument in arguments:
        path = Path(argument).resolve()
        if (not path.is_file() or path.suffix != ".md" or path.name == "README.md"
                or path.parent != scenario_dir):
            raise ValueError(f"Not a scenario file in {scenario_dir}: {argument}")
        paths.append(path)
    return list(dict.fromkeys(paths))


def expand_cases(paths, config, platforms, selected=None):
    result = []
    for path in paths:
        steps, variants = parse_scenario(path)
        for values in itertools.product(*variants.values()):
            variant = dict(zip(variants, values))
            if selected and any(variant.get(k) != v for k, v in selected.items()):
                continue
            for platform in platforms:
                result.append(Case(path, variant, platform,
                                   config["viewports"][platform], steps))
                if len(result) > config["max_cases"]:
                    raise ValueError("Case count exceeds max_cases; select a smaller set.")
    return result


def validate_report(report, case, directory):
    if not isinstance(report, dict) or set(report) != {"status", "summary", "steps"}:
        raise ValueError("Invalid report format.")
    if not isinstance(report["status"], str) or report["status"] not in STATUSES - {"skipped"} or not isinstance(report["summary"], str):
        raise ValueError("Invalid report status or summary.")
    steps = report["steps"]
    if not isinstance(steps, list) or len(steps) != len(case.steps):
        raise ValueError("The report does not contain every step.")
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict) or set(step) != {"number", "status", "expected", "observed", "evidence"}:
            raise ValueError("Invalid report step.")
        if type(step["number"]) is not int or step["number"] != index or not isinstance(step["status"], str) or step["status"] not in STATUSES:
            raise ValueError("Invalid step number or status.")
        if any(not isinstance(step[k], str) for k in ("expected", "observed")):
            raise ValueError("Invalid step description.")
        evidence = step["evidence"]
        if not isinstance(evidence, list) or any(not isinstance(e, str) for e in evidence):
            raise ValueError("Invalid evidence list.")
        if step["status"] == "passed" and (not step["observed"].strip() or not evidence):
            raise ValueError("A passed step needs an observation and evidence.")
        for entry in evidence:
            file = directory / entry
            if Path(entry).is_absolute() or file.is_symlink() or not file.resolve().is_relative_to(directory.resolve()):
                raise ValueError("Evidence is outside the case directory.")
            if not file.is_file() or not file.stat().st_size or file.suffix not in {".txt", ".md", ".png", ".json"}:
                raise ValueError("Evidence is missing, empty, or unsupported.")
    expected = "failed" if any(s["status"] == "failed" for s in steps) else (
        "passed" if all(s["status"] == "passed" for s in steps) else "blocked")
    if report["status"] != expected:
        raise ValueError("The report status does not match its step statuses.")


class Docker:
    def __init__(self, config):
        self.command = config["docker_command"]
        self.timeout = config["command_timeout_seconds"]
        self.secrets = [value for value in config["secrets"].values() if value]

    def call(self, *args, timeout=None, check=True, env=None):
        result = subprocess.run([*self.command, *args], capture_output=True, text=True,
                                timeout=timeout or self.timeout, env=env)
        if check and result.returncode:
            diagnostic = result.stderr[-1500:]
            for secret in self.secrets:
                diagnostic = diagnostic.replace(secret, "[REDACTED]")
            if os.environ.get("CODEX_API_KEY"):
                diagnostic = diagnostic.replace(os.environ["CODEX_API_KEY"], "[REDACTED]")
            raise RuntimeError(f"Docker operation {args[0]} exited with code {result.returncode}: {diagnostic}")
        return result


def mount(source, target, readonly=True):
    return {"type": "bind", "source": str(source), "target": target, "read_only": readonly}


def compose_config(config, inputs, results, worker_image, smoke=False, auth_dir=None):
    common = {"read_only": True, "cap_drop": ["ALL"],
              "security_opt": ["no-new-privileges:true"], "init": True,
              "tmpfs": ["/tmp:rw,nosuid,size=512m,mode=1777"]}
    uid = f"{os.getuid()}:{os.getgid()}"
    browser_networks = ["browser_egress", "control"]
    networks = {"browser_egress": {}, "control": {"internal": True}, "api_egress": {}}
    if config["docker_network"]:
        browser_networks.append("target")
        networks["target"] = {"external": True, "name": config["docker_network"]}
    browser = {**common, "image": worker_image, "user": uid, "networks": browser_networks,
               "entrypoint": ["playwright-mcp"], "working_dir": "/results",
               "command": ["--config", "/run/browser.json", "--host", "0.0.0.0", "--port", "8931",
                           "--allowed-hosts", "browser", "browser:8931", "--headless", "--isolated", "--no-sandbox",
                           "--no-webmcp", "--ignore-roots", "--executable-path", "/usr/bin/chromium"],
               "shm_size": "512m", "volumes": [mount(inputs.parent / "browser.json", "/run/browser.json"), mount(results, "/results", False)]}
    agent_env = {"HOME": "/tmp/pantadek-home", "CODEX_HOME": "/tmp/codex",
                 "NO_PROXY": "browser,localhost,127.0.0.1", "no_proxy": "browser,localhost,127.0.0.1"}
    agent_mounts = [mount(inputs, "/inputs"), mount(results, "/results", False)]
    if not smoke:
        if os.environ.get("CODEX_API_KEY"):
            # Pass by name, never embed the key in argv or the generated compose file.
            agent_env["CODEX_API_KEY"] = "${CODEX_API_KEY:?CODEX_API_KEY required}"
        else:
            agent_mounts.append(mount(auth_dir, "/run/pantadek-auth", False))
    return {"services": {
        "browser": browser,
        "agent": {**common, "image": worker_image, "user": uid, "networks": ["control", "api_egress"],
                  "environment": agent_env, "volumes": agent_mounts}},
        "networks": networks}


def browser_events(output):
    events, usage = [], None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage")
        item = event.get("item", {})
        if item.get("type") == "mcp_tool_call" and event.get("type") == "item.completed":
            events.append({"server": item.get("server"), "tool": item.get("tool"),
                           "status": item.get("status")})
    return events, usage


def run_case(docker, config, case, work, worker_image, smoke):
    inputs, results = work / "inputs", work / "results"
    inputs.mkdir(parents=True)
    results.mkdir()
    suite = config["suite_dir"]
    sources = [(suite / name, Path("suite") / name) for name in
               ("Specification.md", "Setup.md", "Glossary.md") if (suite / name).is_file()]
    sources.append((case.scenario, Path("suite") / "scenarios" / case.scenario.name))
    sources += [(PROJECT / name, Path("pantadek") / name)
                for name in ("Runner_instructions.md", "Result_schema.json")]
    hashes = {}
    for source, relative in sources:
        destination = inputs / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        hashes[str(relative)] = hashlib.sha256(source.read_bytes()).hexdigest()
    reference = datetime.now(ZoneInfo(config["timezone"]))
    next_week = reference.date() - timedelta(days=reference.weekday()) + timedelta(days=7)
    manifest = {"scenario": "/inputs/suite/scenarios/" + case.scenario.name,
                "variant": case.variant, "platform": case.platform, "viewport": case.viewport,
                "base_url": config["base_url"], "timezone": config["timezone"],
                "reference_date": reference.date().isoformat(),
                "next_monday": next_week.isoformat(), "steps": case.steps, "input_hashes": hashes,
                "model": config["model"], "results_dir": "/results", "worker_image": worker_image}
    write_json(inputs / "manifest.json", manifest)
    write_json(work / "browser.json", {"browser": {"browserName": "chromium", "isolated": True,
               "contextOptions": {"viewport": case.viewport, "locale": config["locale"],
                                   "timezoneId": config["timezone"]}},
               "secrets": config["secrets"], "outputDir": "/results"})
    project = "pantadek-" + uuid.uuid4().hex[:12]
    compose = work / "compose.json"
    credentials = tempfile.TemporaryDirectory(prefix="pantadek-auth-")
    auth_source = None
    auth_before = None
    if not smoke and not os.environ.get("CODEX_API_KEY"):
        auth_source = Path(config["codex_auth_file"])
        auth_before = auth_source.read_bytes()
        staged = Path(credentials.name) / "auth.json"
        staged.write_bytes(auth_before)
        staged.chmod(0o600)
    write_json(compose, compose_config(config, inputs, results, worker_image, smoke,
                                      Path(credentials.name)))
    prefix = ["compose", "--project-name", project, "--file", str(compose)]
    agent_name = project + "-case"
    started = time.monotonic()
    result = {"case": case.describe(), "status": "blocked", "reason": "", "manifest": manifest,
              "smoke": smoke, "usage": None}
    cleanup_errors = []
    stage = "browser startup"
    try:
        docker.call(*prefix, "up", "--detach", "browser")
        invocation = [*prefix, "run", "--rm", "--no-deps", "--name", agent_name, "agent"]
        if smoke:
            invocation.append("--smoke")
        stage = "MCP browser" if smoke else "Codex"
        print("  Executing: " + stage, flush=True)
        completed = docker.call(*invocation, timeout=config["case_timeout_seconds"], check=False)
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict) and event.get("type") == "pantadek.preflight":
                    result["codex_version"] = event.get("codex_version")
            except ValueError:
                pass
        if completed.returncode:
            diagnostic = completed.stderr[-2000:]
            for secret in config["secrets"].values():
                if secret:
                    diagnostic = diagnostic.replace(secret, "[REDACTED]")
            if os.environ.get("CODEX_API_KEY"):
                diagnostic = diagnostic.replace(os.environ["CODEX_API_KEY"], "[REDACTED]")
            raise RuntimeError(f"The worker exited with code {completed.returncode}. " + diagnostic)
        if datetime.now(ZoneInfo(config["timezone"])).date() != reference.date():
            raise RuntimeError("The date changed during the case; a fresh setup is required.")
        if smoke:
            probe = json.loads(completed.stdout)
            if probe.get("status") != "target-ready":
                raise ValueError("Invalid browser smoke-check result.")
            result.update(status="infrastructure-ready", reason="The target URL and MCP browser work; the scenario was not executed.")
            write_json(results / "browser-probe.json", probe)
        else:
            report_file = results / "model-result.json"
            if report_file.is_symlink() or report_file.stat().st_size > 2_000_000:
                raise ValueError("Invalid report file.")
            report = json.loads(report_file.read_text())
            validate_report(report, case, results)
            events, usage = browser_events(completed.stdout)
            if report["status"] == "passed" and not any(
                e["server"] == "browser" and e["status"] == "completed" for e in events
            ):
                raise ValueError("No completed browser tool calls were recorded.")
            write_json(results / "browser-events.json", events)
            result.update(status=report["status"], reason=report["summary"], report=report, usage=usage)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        result["reason"] = f"Time limit exceeded during {stage}." if isinstance(error, subprocess.TimeoutExpired) else str(error)
    finally:
        if result["status"] == "blocked":
            try:
                logs = docker.call(*prefix, "logs", "--no-color", "--tail", "30", "browser", check=False, timeout=60)
                safe_log = logs.stdout
                for secret in config["secrets"].values():
                    if secret:
                        safe_log = safe_log.replace(secret, "[REDACTED]")
                if os.environ.get("CODEX_API_KEY"):
                    safe_log = safe_log.replace(os.environ["CODEX_API_KEY"], "[REDACTED]")
                (results / "infrastructure.log").write_text(safe_log)
            except (OSError, subprocess.TimeoutExpired):
                pass
        # docker compose run can outlive a killed client; remove its named container first.
        for args in [("rm", "--force", agent_name), (*prefix, "down", "--volumes", "--remove-orphans", "--timeout", "5")]:
            try:
                cleaned = docker.call(*args, timeout=45, check=False)
                if cleaned.returncode and args[0] != "rm":
                    cleanup_errors.append("Compose cleanup failed.")
            except (OSError, subprocess.TimeoutExpired):
                cleanup_errors.append("QA resource cleanup failed.")
        if cleanup_errors:
            result.update(status="blocked", reason=result["reason"] + " " + " ".join(cleanup_errors))
        # Preserve refreshed login tokens, without mounting the operator's Codex home.
        if auth_source is not None:
            try:
                staged_auth = Path(credentials.name) / "auth.json"
                if staged_auth.is_symlink():
                    raise RuntimeError("Invalid refreshed authentication file.")
                refreshed = staged_auth.read_bytes()
                if refreshed != auth_before:
                    if auth_source.read_bytes() != auth_before:
                        raise RuntimeError("The authentication file changed concurrently; it was not overwritten.")
                    json.loads(refreshed)
                    with tempfile.NamedTemporaryFile(dir=auth_source.parent, delete=False) as handle:
                        replacement = Path(handle.name)
                        handle.write(refreshed)
                    try:
                        os.replace(replacement, auth_source)
                    finally:
                        replacement.unlink(missing_ok=True)
            except (OSError, ValueError, RuntimeError) as error:
                result.update(status="blocked", reason="Could not persist refreshed authentication: " + str(error))
        credentials.cleanup()
        result["duration_seconds"] = round(time.monotonic() - started, 2)
        # The browser configuration contains the demo secret; do not retain it with artifacts.
        (work / "browser.json").unlink(missing_ok=True)
        write_json(results / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run browser scenarios through Pantadek and Codex in isolated Docker containers")
    parser.add_argument("scenarios", nargs="*", help="Path to a scenario file in suite_dir/scenarios/; defaults to all scenarios")
    parser.add_argument("--config", type=Path, default=Path("pantadek.toml"),
                        help="TOML configuration file (default: ./pantadek.toml)")
    parser.add_argument("--init", action="store_true", help="Create a starter pantadek.toml in this project")
    parser.add_argument("--platform", action="append", help="Repeat to select multiple platforms; defaults to all")
    parser.add_argument("--variant", action="append", default=[], metavar="KEY=VALUE", help="Select a scenario variant")
    parser.add_argument("--list", action="store_true", help="List cases without starting Docker or a model")
    parser.add_argument("--smoke", action="store_true", help="Check the target URL and browser without a model")
    parser.add_argument("--no-build", action="store_true", help="Use the previously built Pantadek worker image")
    args = parser.parse_args(argv)
    try:
        if args.init:
            destination = args.config.expanduser()
            if destination.exists():
                raise ValueError(f"Configuration already exists: {destination}")
            destination.write_text(CONFIG_TEMPLATE)
            print(f"Created {destination}. Edit suite_dir and base_url before running scenarios.")
            return 0
        config = read_config(args.config)
        platforms = list(dict.fromkeys(args.platform or config["viewports"]))
        if any(p not in config["viewports"] for p in platforms):
            raise ValueError("Unknown platform.")
        paths = select_scenarios(args.scenarios, config["suite_dir"] / "scenarios")
        selected = {}
        for item in args.variant:
            key, separator, value = item.partition("=")
            if not separator or not key or not value:
                raise ValueError("Specify a variant as KEY=VALUE.")
            selected[key] = value
        cases = expand_cases(paths, config, platforms, selected)
        if not cases:
            raise ValueError("No matching cases.")
        for index, case in enumerate(cases, 1):
            print(f"{index}. {case.describe()}", flush=True)
        if args.list:
            return 0
        if not args.smoke and not os.environ.get("CODEX_API_KEY") and not (
            config["codex_auth_file"] and Path(config["codex_auth_file"]).is_file()
        ):
            raise ValueError("Set CODEX_API_KEY or a valid codex_auth_file. --smoke requires no authentication.")
        docker = Docker(config)
        docker.call("version", "--format", "{{.Server.Version}}")
        docker.call("compose", "version")
        if not args.no_build:
            print("Building Pantadek worker image...", flush=True)
            docker.call("build", "--file", str(PROJECT / "Dockerfile"), "--target", "runtime",
                        "--tag", "pantadek-worker:local", str(PROJECT), timeout=900)
        worker_image = docker.call("image", "inspect", "pantadek-worker:local", "--format", "{{.Id}}").stdout.strip()
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        output = Path.cwd() / "artifacts" / run_id
        output.mkdir(parents=True)
        commit_result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=config["suite_dir"],
                                       capture_output=True, text=True, check=False)
        suite_commit = commit_result.stdout.strip() if commit_result.returncode == 0 else None
        results = []
        for index, case in enumerate(cases[:1] if args.smoke else cases, 1):
            print("Running: " + case.describe(), flush=True)
            result = run_case(docker, config, case, output / f"case-{index:03}", worker_image, args.smoke)
            result["suite_commit"] = suite_commit
            results.append(result)
            write_json(output / "report.json", {"suite_commit": suite_commit, "cases": results})
            (output / "Report.md").write_text("# Pantadek results\n\n" + "\n".join(
                f"- {r['case']}: **{r['status']}** — {r['reason']}" for r in results) + "\n")
            print(result["status"] + ": " + result["reason"], flush=True)
        print("Report: " + str(output / "Report.md"))
        return 0 if all(r["status"] in {"passed", "infrastructure-ready"} for r in results) else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Runner error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("QA execution interrupted.", file=sys.stderr)
        return 130


def cli():
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    return main()


if __name__ == "__main__":
    sys.exit(cli())
