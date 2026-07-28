import os
from pathlib import Path

from etf_t0.macos_bundle import LAUNCHER_NAME, build_macos_launcher


def test_build_macos_launcher_creates_double_clickable_workspace_launcher(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace with spaces"
    python = workspace / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("placeholder", encoding="utf-8")

    launcher = build_macos_launcher(
        workspace=workspace, output_directory=tmp_path / "dist"
    )

    content = launcher.read_text(encoding="utf-8")
    assert launcher.name == LAUNCHER_NAME
    assert os.access(launcher, os.X_OK)
    assert content.startswith("#!/bin/zsh")
    assert "osascript" not in content
    assert str(python.resolve()) in content
    assert "etf_t0.desktop_app" in content
