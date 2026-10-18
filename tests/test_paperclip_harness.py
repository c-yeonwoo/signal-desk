"""Run the local-only harness contracts without model, network, or credentials."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_paperclip_offline_contracts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node runtime unavailable; Paperclip is a separate local tool")
    root = Path(__file__).resolve().parents[1]
    scripts = sorted((root/"ops/paperclip").glob("*.test.mjs"))
    assert scripts
    result = subprocess.run([node,"--test",*map(str,scripts)],cwd=root,capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stdout+result.stderr
