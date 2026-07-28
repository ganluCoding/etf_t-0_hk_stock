import os
import plistlib
from pathlib import Path

from etf_t0.macos_bundle import build_macos_app


def test_build_macos_app_creates_double_clickable_target_only_launcher(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace with spaces"
    python = workspace / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("placeholder", encoding="utf-8")

    app = build_macos_app(workspace=workspace, output_directory=tmp_path / "dist")

    launcher = app / "Contents/MacOS/launcher"
    command_file = app / "Contents/Resources/launch.command"
    info_path = app / "Contents/Info.plist"
    assert app.name == "T0 ETF Observation.app"
    assert os.access(launcher, os.X_OK)
    assert os.access(command_file, os.X_OK)
    assert "/usr/bin/open -a Terminal" in launcher.read_text(encoding="utf-8")
    assert "osascript" not in launcher.read_text(encoding="utf-8")
    assert str(python.resolve()) in command_file.read_text(encoding="utf-8")
    assert (app / "Contents/Resources/workspace.txt").read_text(
        encoding="utf-8"
    ) == str(workspace.resolve())
    info = plistlib.loads(info_path.read_bytes())
    assert info["CFBundleIdentifier"] == "com.ganlu.etft0.observation"
    assert info["CFBundleExecutable"] == "launcher"
