import subprocess

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the whole package at a throwaway data directory."""
    d = tmp_path / "data"
    monkeypatch.setenv("RAVENS_NEST_DATA", str(d))
    return d


@pytest.fixture
def run_git():
    def _run(cwd, *args):
        proc = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
        return proc.stdout

    return _run
