"""Runs inside the unprivileged execution container; never invokes Docker."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

TOOLS = ["browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click",
         "browser_fill_form", "browser_type", "browser_press_key", "browser_select_option",
         "browser_wait_for", "browser_tabs", "browser_close", "browser_take_screenshot"]


class BrowserProtocolError(Exception):
    pass


class BrowserClient:
    def __init__(self):
        self.session = None
        self.sequence = 0
        self.protocol = None
        # Internal MCP traffic must not be sent to an operator's HTTP proxy.
        self.opener = build_opener(ProxyHandler({}))

    def call(self, method, params=None, notification=False, timeout=30):
        self.sequence += 1
        message = {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params or {}}
        if notification:
            del message["id"]
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        try:
            with self.opener.open(Request("http://browser:8931/mcp", data=json.dumps(message).encode(), headers=headers), timeout=timeout) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                body = response.read().decode()
        except HTTPError as error:
            raise BrowserProtocolError(f"MCP HTTP {error.code}: " + error.read().decode(errors="replace")[:1000]) from error
        if notification:
            return {}
        if body.lstrip().startswith("{"):
            answer = json.loads(body)
        else:
            answers = [json.loads(line[5:].strip()) for line in body.splitlines() if line.startswith("data:")]
            answer = next(item for item in answers if item.get("id") == self.sequence)
        if "error" in answer or answer.get("result", {}).get("isError"):
            raise RuntimeError("MCP tool error: " + method + ": " + str(answer.get("error", answer.get("result")))[:1000])
        return answer.get("result", {})


def main():
    if os.geteuid() == 0 or os.environ.get("DOCKER_HOST") or shutil.which("docker") or Path("/var/run/docker.sock").exists():
        raise RuntimeError("The worker does not meet the Docker isolation requirements.")
    Path(os.environ["HOME"]).mkdir(exist_ok=True)
    manifest = json.loads(Path("/inputs/manifest.json").read_text())
    client = BrowserClient()
    deadline = time.monotonic() + 45
    last_error = ""
    while time.monotonic() < deadline:
        try:
            initialized = client.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                       "clientInfo": {"name": "pantadek-preflight", "version": "1"}}, timeout=5)
            client.protocol = initialized["protocolVersion"]
            client.call("notifications/initialized", notification=True, timeout=5)
            catalog = client.call("tools/list")
            break
        except (OSError, URLError, RuntimeError) as error:
            last_error = str(error)
            time.sleep(1)
    else:
        raise RuntimeError("The browser server is not ready: " + last_error)
    names = {tool["name"] for tool in catalog.get("tools", [])}
    if not {"browser_navigate", "browser_snapshot", "browser_click", "browser_fill_form"} <= names:
        raise RuntimeError("Required browser tools are missing.")
    if "--smoke" in sys.argv:
        client.call("tools/call", {"name": "browser_navigate", "arguments": {"url": manifest["base_url"]}})
        snapshot = client.call("tools/call", {"name": "browser_snapshot", "arguments": {}})
        if not json.dumps(snapshot, ensure_ascii=False).strip():
            raise RuntimeError("The target page returned an empty browser snapshot.")
        print(json.dumps({"status": "target-ready", "tools": sorted(names), "base_url": manifest["base_url"],
                          "docker_access": False, "uid": os.geteuid()}))
        return 0
    version = subprocess.run(["codex", "--version"], check=True, capture_output=True, text=True).stdout.strip()
    print(json.dumps({"type": "pantadek.preflight", "codex_version": version}), flush=True)
    if Path("/run/pantadek-auth/auth.json").is_file():
        os.environ["CODEX_HOME"] = "/run/pantadek-auth"
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir(exist_ok=True)
    # Docker is the filesystem/privilege boundary. Only ephemeral /tmp and /results are writable.
    config = 'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
    config += '[mcp_servers.browser]\nurl = "http://browser:8931/mcp"\nrequired = true\n'
    config += 'startup_timeout_sec = 45\ntool_timeout_sec = 45\ndefault_tools_approval_mode = "approve"\n'
    config += 'enabled_tools = ' + json.dumps(TOOLS) + '\n'
    (home / "config.toml").write_text(config)
    prompt = ("Execute the scenario in /inputs/manifest.json according to "
              "/inputs/pantadek/Runner_instructions.md. Return a report matching the provided schema.")
    return subprocess.run([
        "codex", "exec", "--skip-git-repo-check", "--ephemeral", "--json",
        "--model", manifest["model"], "--output-schema", "/inputs/pantadek/Result_schema.json",
        "--output-last-message", "/results/model-result.json", prompt,
    ]).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Pantadek worker: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(2)
