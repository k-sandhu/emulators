"""Run the official airtable.js client from an optional external npm installation.

Install outside the repository with the command in docs/providers/airtable.md.
The test never installs packages or calls the vendor API.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from emulators.core.hub import EmulatorHub
from emulators.testing.server import EmulatorServer

pytestmark = pytest.mark.sdk


def test_official_airtable_js_crud_select_pagination_and_errors() -> None:
    node = shutil.which("node.exe") or shutil.which("node")
    modules = Path(
        os.environ.get(
            "AIRTABLE_SDK_NODE_MODULES",
            str(Path(tempfile.gettempdir()) / "emulators-airtable-sdk" / "node_modules"),
        )
    )
    if not node or not (modules / "airtable" / "package.json").exists():
        pytest.skip(
            "Optional official airtable.js 0.12.2 installation is unavailable; see Airtable provider docs."
        )
    hub = EmulatorHub(providers=["airtable"], anchor="2026-10-16")
    base = next(b for b in hub.context("airtable").store.collection("bases") if b["_key"] == "operations")
    script = Path(__file__).resolve().parents[1] / "providers" / "airtable" / "sdk_contract.cjs"
    with EmulatorServer(hub) as server:
        result = subprocess.run(
            [node, str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env={
                **os.environ,
                "AIRTABLE_SDK_NODE_MODULES": str(modules),
                "AIRTABLE_EMULATOR_URL": server.url_for("airtable"),
                "AIRTABLE_BASE_ID": base["id"],
            },
        )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["version"] == "0.12.2"
