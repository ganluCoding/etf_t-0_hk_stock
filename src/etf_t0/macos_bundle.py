"""Build a local macOS app that opens the tested desktop runtime in Terminal."""

from __future__ import annotations

import argparse
import plistlib
import shlex
import stat
from pathlib import Path

APP_NAME = "T0 ETF Observation.app"


def _make_executable(path: Path) -> None:
    path.chmod(
        path.stat().st_mode
        | stat.S_IXUSR
        | stat.S_IXGRP
        | stat.S_IXOTH
    )


def build_macos_app(*, workspace: Path, output_directory: Path) -> Path:
    """Create a local launcher without requesting Apple Events permission."""

    workspace = workspace.resolve()
    python = workspace / ".venv" / "bin" / "python"
    app = output_directory.resolve() / APP_NAME
    if app.exists():
        raise FileExistsError(f"refusing to overwrite existing app: {app}")

    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    logs = workspace / "reports" / "generated" / "desktop_app"
    command_file = resources / "launch.command"
    command_file.write_text(
        "#!/bin/zsh\n"
        "set -eu\n"
        f"cd {shlex.quote(str(workspace))}\n"
        f"mkdir -p {shlex.quote(str(logs))}\n"
        f"export PYTHONPATH={shlex.quote(str(workspace / 'src'))}\n"
        f"exec {shlex.quote(str(python))} -m etf_t0.desktop_app "
        f"--workspace {shlex.quote(str(workspace))} "
        f">> {shlex.quote(str(logs / 'stdout.log'))} "
        f"2>> {shlex.quote(str(logs / 'stderr.log'))}\n",
        encoding="utf-8",
    )
    _make_executable(command_file)

    launcher = macos / "launcher"
    launcher.write_text(
        "#!/bin/zsh\n"
        "set -eu\n"
        f"/usr/bin/open -a Terminal {shlex.quote(str(command_file))}\n",
        encoding="utf-8",
    )
    _make_executable(launcher)

    info = {
        "CFBundleDisplayName": "T0 ETF Observation",
        "CFBundleExecutable": "launcher",
        "CFBundleIdentifier": "com.ganlu.etft0.observation",
        "CFBundleName": "T0 ETF Observation",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.2.0",
        "CFBundleVersion": "2",
        "LSMinimumSystemVersion": "13.0",
    }
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    (resources / "workspace.txt").write_text(str(workspace), encoding="utf-8")
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    app = build_macos_app(
        workspace=args.workspace,
        output_directory=args.output,
    )
    print(app)


if __name__ == "__main__":
    main()
