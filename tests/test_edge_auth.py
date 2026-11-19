"""The Lambda@Edge login wall, run by Node.js with stubs for SSM and Cognito."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from n8n_on_aws.cdk.edge import EDGE_AUTH_SOURCE

HERE = Path(__file__).parent

SSM_STUB = """
export const state = { value: null, fail: false, reads: 0 };
export class GetParameterCommand {
  constructor(input) { this.input = input; }
}
export class SSMClient {
  constructor(options) { this.options = options; }
  async send(command) {
    state.reads += 1;
    if (state.fail || state.value === null) throw new Error("ParameterNotFound");
    if (command.input.Name !== "/n8n-on-aws/n8n-test/edge-auth") {
      throw new Error("unexpected parameter " + command.input.Name);
    }
    if (this.options.region !== "us-east-1") throw new Error("wrong region");
    return { Parameter: { Value: state.value } };
  }
}
"""

UNCONFIGURED = """
import assert from "node:assert/strict";
import { state } from "@aws-sdk/client-ssm";
const { handler } = await import("./index.mjs");
const event = { Records: [{ cf: { request: {
  uri: "/", method: "GET", querystring: "", headers: {} } } }] };
state.fail = true;
assert.equal((await handler(event)).status, "503");
assert.equal((await handler(event)).status, "503");
assert.equal(state.reads, 2);  // a failure is not cached
state.fail = false;
state.value = "{}";  // the placeholder the us-east-1 stack writes first
assert.equal((await handler(event)).status, "503");
console.log("edge auth: unconfigured checks passed");
"""


def _workspace(tmp_path: Path) -> Path:
    shutil.copy(EDGE_AUTH_SOURCE / "index.mjs", tmp_path / "index.mjs")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "domain": "n8n.example.com",
                "callbackPath": "/_auth/callback",
                "signoutPath": "/_auth/signout",
                "sessionSeconds": 4 * 3600,
                "parameter": "/n8n-on-aws/n8n-test/edge-auth",
                "parameterRegion": "us-east-1",
            }
        )
    )
    package = tmp_path / "node_modules" / "@aws-sdk" / "client-ssm"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {"name": "@aws-sdk/client-ssm", "type": "module", "main": "index.js"}
        )
    )
    (package / "index.js").write_text(SSM_STUB)
    return tmp_path


def _node(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", script.name],
        cwd=script.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")


@needs_node
def test_login_wall_flow(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    script = workspace / "harness.mjs"
    shutil.copy(HERE / "edge_auth_harness.mjs", script)
    result = _node(script)
    assert result.returncode == 0, result.stderr
    assert "all checks passed" in result.stdout


@needs_node
def test_unconfigured_wall_answers_503_and_retries(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    script = workspace / "unconfigured.mjs"
    script.write_text(UNCONFIGURED)
    result = _node(script)
    assert result.returncode == 0, result.stderr
    assert "unconfigured checks passed" in result.stdout
