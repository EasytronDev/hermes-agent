"""Unreadable directories under a search root never cost search_files its results.

A directory the searching user cannot read makes rg exit 2 and find exit 1 while
both still print every readable path. These live runs use real rg/find on a temp
tree: results come back, a warning names the skipped path, and the skip
diagnostics never consume the row budget meant for results.
"""

import os
import shutil

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations

pytestmark = [
    pytest.mark.platforms("linux", "macos"),
    pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads mode-000 dirs"),
]


@pytest.fixture
def locked_tree(tmp_path):
    (tmp_path / "zz").mkdir()
    for i in range(5):
        (tmp_path / "zz" / f"f{i}.log").write_text("needle\n")
    locked = []
    for i in range(60):
        d = tmp_path / f"d{i:02d}"
        d.mkdir()
        d.chmod(0)
        locked.append(d)
    yield tmp_path
    for d in locked:
        d.chmod(0o755)


@pytest.mark.parametrize("target,kwargs", [
    ("files", {}),
    ("content", {"output_mode": "files_only"}),
    ("content", {}),
])
def test_many_unreadable_dirs_do_not_eat_the_result_budget(locked_tree, target, kwargs):
    if not shutil.which("rg"):
        pytest.skip("needs ripgrep")
    ops = ShellFileOperations(LocalEnvironment(cwd=str(locked_tree)), cwd=str(locked_tree))
    pattern = "*.log" if target == "files" else "needle"
    result = ops.search(pattern, path=str(locked_tree), target=target, limit=3, **kwargs)
    assert result.error is None
    found = result.files or [m.path for m in result.matches]
    assert len(found) == 3
    assert all(p.endswith(".log") for p in found)
    assert "Skipped 60 unreadable path(s)" in (result.warning or "")


def test_find_fallback_names_the_unreadable_dir(locked_tree, monkeypatch):
    ops = ShellFileOperations(LocalEnvironment(cwd=str(locked_tree)), cwd=str(locked_tree))
    real_has = ops._has_command
    monkeypatch.setattr(ops, "_has_command", lambda cmd: False if cmd == "rg" else real_has(cmd))
    result = ops.search("*.log", path=str(locked_tree), target="files", limit=50)
    assert result.error is None
    assert sorted(os.path.basename(p) for p in result.files) == [f"f{i}.log" for i in range(5)]
    assert f"Skipped 60 unreadable path(s) with restricted permissions: {locked_tree}/d" in (result.warning or "")
