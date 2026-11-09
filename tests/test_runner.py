from contextlib import chdir
import json
from types import SimpleNamespace
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from io import BytesIO
from urllib.error import HTTPError
import unittest
from unittest.mock import patch

from pantadek import run, worker


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.suite = self.directory / "suite"
        (self.suite / "scenarios").mkdir(parents=True)
        self.scenario = self.suite / "scenarios" / "Sample.md"
        self.scenario.write_text("Goal: check the page\nPreconditions: target is ready\nPlatform: web\n"
                                 "## Steps\n1. Open the target page.\nExpected result: page is visible\n")
        self.config_file = self.directory / "pantadek.toml"
        self.write_config()
        self.config = run.read_config(self.config_file)
        auth = self.directory / "auth.json"
        auth.write_text('{"tokens":{}}')
        self.config["codex_auth_file"] = str(auth)

    def write_config(self, **updates):
        values = {"suite_dir": "suite", "base_url": "http://web:8000",
                  "docker_network": "target-net", "docker_command": ["docker"],
                  "model": "gpt-6.1-terra", "case_timeout_seconds": 600,
                  "command_timeout_seconds": 300, "max_cases": 20,
                  "locale": "en-US", "timezone": "UTC"}
        values.update(updates)
        lines = []
        for key, value in values.items():
            if isinstance(value, str):
                rendered = json.dumps(value)
            elif isinstance(value, list):
                rendered = "[" + ", ".join(json.dumps(part) for part in value) + "]"
            elif isinstance(value, int):
                rendered = str(value)
            else:
                continue
            lines.append(f"{key} = {rendered}")
        self.config_file.write_text("\n".join(lines) + "\n")

    def test_suite_scenarios_expand_across_viewports_and_variants(self):
        self.scenario.write_text("Goal: check a page\nPreconditions: test data exists\nPlatform: web\n"
                                 'Variants: account: "first/second"\n## Steps\n1. Open the page.\n'
                                 "Expected result: page is visible\n")
        cases = run.expand_cases([self.scenario], self.config, ["desktop", "mobile"])
        self.assertEqual(len(cases), 4)
        self.assertTrue(all(c.steps for c in cases))

    def test_scenarios_are_selected_only_by_file_path(self):
        scenario_dir = self.suite / "scenarios"
        other = scenario_dir / "Other.md"
        other.write_text(self.scenario.read_text())
        (scenario_dir / "README.md").write_text("# Suite")
        (scenario_dir / "Notes.txt").write_text("Not a scenario")
        outside = self.directory / "Outside.md"
        outside.write_text(self.scenario.read_text())
        self.assertEqual(run.select_scenarios([], scenario_dir), [other.resolve(), self.scenario.resolve()])
        self.assertEqual(run.select_scenarios([str(self.scenario), str(self.scenario)], scenario_dir),
                         [self.scenario.resolve()])
        with chdir(self.suite):
            self.assertEqual(run.select_scenarios(["scenarios/Other.md"], scenario_dir), [other.resolve()])
        with chdir(scenario_dir):
            for argument in ["Sample", "../scenarios/Sample"]:
                with self.subTest(argument=argument), self.assertRaises(ValueError):
                    run.select_scenarios([argument], scenario_dir)
        for argument in [scenario_dir / "README.md", scenario_dir / "Notes.txt", outside,
                         scenario_dir / "Missing.md", scenario_dir]:
            with self.subTest(argument=argument), self.assertRaises(ValueError):
                run.select_scenarios([str(argument)], scenario_dir)

    def test_toml_config_rejects_invalid_docker_command_model_and_limits(self):
        for updates in [{"docker_command": []}, {"model": "model with spaces"},
                        {"max_cases": 0}, {"case_timeout_seconds": True}]:
            with self.subTest(updates=updates):
                self.write_config(**updates)
                with self.assertRaises(ValueError):
                    run.read_config(self.config_file)

    def test_variant_expansion_is_scoped_and_deduplicated(self):
        path = self.directory / "Variants.md"
        path.write_text('Goal: test\nPreconditions: test\nPlatform: web\n'
                        'Variants: account: "first/first/second".\n'
                        '## Kroki\n1. Otwórz pn/śr/pt.\nOczekiwany wynik: test\n')
        cases = run.expand_cases([path], self.config, ["desktop"])
        self.assertEqual([c.variant for c in cases], [{"account": "first"}, {"account": "second"}])
        self.config["max_cases"] = 1
        with self.assertRaises(ValueError):
            run.expand_cases([path], self.config, ["desktop"])

    def test_incomplete_scenarios_and_empty_alternatives_are_rejected(self):
        path = self.directory / "Invalid.md"
        path.write_text("## Kroki\n2. Test")
        with self.assertRaises(ValueError):
            run.parse_scenario(path)
        path.write_text('Cel: test\nWarunki początkowe: test\nPlatforma: web\n'
                        'Warianty: konto: "panda/"\n## Kroki\n1. Test\nOczekiwany wynik: test')
        with self.assertRaises(ValueError):
            run.parse_scenario(path)

    def report(self):
        (self.directory / "observed.txt").write_text("Rzeczywista obserwacja")
        case = run.Case(Path("Example.md"), {}, "desktop", {}, ["Sprawdź"])
        report = {"status": "passed", "summary": "OK", "steps": [
            {"number": 1, "status": "passed", "expected": "Panel", "observed": "Panel widoczny",
             "evidence": ["observed.txt"]}]}
        return case, report

    def test_report_needs_all_steps_actual_observations_and_existing_evidence(self):
        case, report = self.report()
        run.validate_report(report, case, self.directory)
        report["steps"][0]["evidence"] = ["missing.txt"]
        with self.assertRaises(ValueError):
            run.validate_report(report, case, self.directory)
        report["steps"] = []
        with self.assertRaises(ValueError):
            run.validate_report(report, case, self.directory)

    def test_report_rejects_escaping_paths_symlinks_and_false_success(self):
        case, report = self.report()
        (self.directory / "link.txt").symlink_to(self.directory / "observed.txt")
        for name in ["../outside.txt", "/etc/passwd", "link.txt"]:
            report["steps"][0]["evidence"] = [name]
            with self.assertRaises(ValueError):
                run.validate_report(report, case, self.directory)
        report["steps"][0].update(status="failed", evidence=[])
        with self.assertRaises(ValueError):
            run.validate_report(report, case, self.directory)

    def test_compose_isolates_agent_from_optional_target_network(self):
        with patch.dict("os.environ", {}, clear=True):
            self.config["codex_auth_file"] = "/tmp/auth.json"
            document = run.compose_config(self.config, self.directory / "inputs", self.directory / "results",
                                          "worker", auth_dir=self.directory / "auth")
        browser, agent = [document["services"][name] for name in ["browser", "agent"]]
        self.assertEqual(browser["networks"], ["browser_egress", "control", "target"])
        self.assertEqual(document["networks"]["target"], {"external": True, "name": "target-net"})
        self.assertNotIn("target", agent["networks"])
        self.assertTrue(document["networks"]["control"]["internal"])
        self.assertNotIn("app", document["services"])
        self.assertTrue(agent["read_only"])
        serialized = json.dumps(agent)
        self.assertNotIn("docker.sock", serialized)
        self.assertNotIn("browser.json", serialized)
        self.assertNotIn("privileged", serialized)

    def test_docker_runs_argv_without_shell_or_stdin_redirection(self):
        with patch("pantadek.run.subprocess.run", return_value=SimpleNamespace(returncode=0)) as called:
            run.Docker(self.config).call("compose", "version")
        args, kwargs = called.call_args
        self.assertEqual(args[0], ["docker", "compose", "version"])
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("stdin", kwargs)

    def test_timeout_always_cleans_up_owned_resources(self):
        case = run.expand_cases([self.scenario], self.config, ["desktop"])[0]
        calls = []
        class FakeDocker:
            def call(self, *args, **kwargs):
                calls.append(args)
                if "agent" in args:
                    raise subprocess.TimeoutExpired("docker", 1)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
        result = run.run_case(FakeDocker(), self.config, case, self.directory / "case", "worker", False)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(c[0] == "rm" for c in calls))
        self.assertTrue(any("down" in c and "--volumes" in c for c in calls))
        self.assertFalse((self.directory / "case/browser.json").exists())

    def test_cli_exit_does_not_substitute_for_browser_events(self):
        events, usage = run.browser_events('{"type":"turn.completed","usage":{"input_tokens":1}}')
        self.assertEqual(events, [])
        self.assertEqual(usage, {"input_tokens": 1})

    def test_mcp_notifications_accept_empty_response_and_preserve_session(self):
        class Response:
            headers = {"Mcp-Session-Id": "test-session"}
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return self.body
        with patch("pantadek.worker.build_opener") as factory:
            factory.return_value.open.side_effect = [
                Response(b'{"id":1,"result":{"protocolVersion":"2025-03-26"}}'), Response(b'')]
            client = worker.BrowserClient()
            response = client.call("initialize")
            client.protocol = response["protocolVersion"]
            self.assertEqual(client.call("notifications/initialized", notification=True), {})
            request = factory.return_value.open.call_args.args[0]
            self.assertNotIn("id", json.loads(request.data))
            self.assertEqual(client.session, "test-session")

    def test_mcp_http_errors_include_diagnostic_and_tools_do_not_allow_arbitrary_code(self):
        with patch("pantadek.worker.build_opener") as factory:
            factory.return_value.open.side_effect = HTTPError(
                "http://browser:8931/mcp", 403, "Forbidden", {}, BytesIO(b'Host not allowed'))
            with self.assertRaisesRegex(worker.BrowserProtocolError, "Host not allowed"):
                worker.BrowserClient().call("initialize")
        self.assertNotIn("browser_evaluate", worker.TOOLS)
        self.assertNotIn("browser_run_code", worker.TOOLS)


if __name__ == "__main__":
    unittest.main()
