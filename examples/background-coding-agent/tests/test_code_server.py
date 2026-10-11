# SPDX-License-Identifier: Apache-2.0
"""Run the remote editor recipe against a local command double."""

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "code-server-remote-dev/run.sh"


@pytest.fixture
def recipe(tmp_path):
    command = tmp_path / "microvm"
    command.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import sys
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as output:
    output.write(json.dumps(args) + '\\n')
if args[0] == 'health':
    sys.exit(0 if os.environ.get('REUSE') else 1)
if args[0] == 'build':
    print(json.dumps({'data': {'imageIdentifier': 'image'}}))
if args[0] == 'cp':
    source = pathlib.Path(args[1])
    pathlib.Path(os.environ['MODE_LOG']).write_text(str(source.stat().st_mode & 0o777))
    shutil.copyfile(source, os.environ['CONFIG_COPY'])
if args[0] == 'exec' and args[1] == 'test -f /run/code-server-authenticated':
    sys.exit(0 if os.environ.get('AUTHENTICATED') else 1)
"""
    )
    command.chmod(0o700)
    password = tmp_path / "password"
    password.write_text("example password: 'quoted'\n")
    password.chmod(0o600)
    env = os.environ | {
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "CODE_SERVER_PASSWORD_FILE": str(password),
        "CALL_LOG": str(tmp_path / "calls"),
        "CONFIG_COPY": str(tmp_path / "config"),
        "MODE_LOG": str(tmp_path / "mode"),
    }
    return tmp_path, password, env


def run_recipe(env):
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
    )


def test_editor_starts_with_a_protected_password_configuration(recipe):
    directory, _, env = recipe
    result = run_recipe(env)
    assert result.returncode == 0, result.stderr
    configuration = (directory / "config").read_text()
    assert "auth: password\n" in configuration
    assert "password: \"example password: 'quoted'\"\n" in configuration
    assert (directory / "mode").read_text() == str(0o600)
    calls = [
        json.loads(line) for line in (directory / "calls").read_text().splitlines()
    ]
    upload = next(call for call in calls if call[0] == "cp")
    assert upload[2] == "vm:/run/code-server.yaml"
    assert upload[upload.index("--mode") + 1] == "0600"
    launch = next(
        call
        for call in calls
        if call[:2] == ["exec", "code-server --config /run/code-server.yaml /workspace"]
    )
    assert launch[launch.index("--user") + 1] == "1000"
    assert "example password" not in result.stdout + result.stderr + json.dumps(calls)
    assert calls[-1][0] == "port-forward"


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_editor_requires_a_private_password_file_before_building(recipe, mode):
    directory, password, env = recipe
    password.chmod(mode)
    result = run_recipe(env)
    assert result.returncode != 0
    assert "password file must have mode 0600 or stricter" in result.stderr
    calls = [
        json.loads(line) for line in (directory / "calls").read_text().splitlines()
    ]
    assert [call[0] for call in calls] == ["health"]


def test_editor_reattaches_only_after_authenticated_initialization(recipe):
    directory, _, env = recipe
    result = run_recipe(env | {"REUSE": "1"})
    assert result.returncode != 0
    assert "Recreate" in result.stderr
    result = run_recipe(env | {"REUSE": "1", "AUTHENTICATED": "1"})
    assert result.returncode == 0, result.stderr
    calls = [
        json.loads(line) for line in (directory / "calls").read_text().splitlines()
    ]
    assert calls[-1][0] == "port-forward"
    assert not any(call[0] == "build" for call in calls)
