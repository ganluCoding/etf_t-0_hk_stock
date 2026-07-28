"""Build the supported Finder launcher for the local desktop workbench."""

from __future__ import annotations

import argparse
import shlex
import stat
from pathlib import Path

LAUNCHER_NAME = "启动 T0 ETF 工作台.command"


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def build_macos_launcher(*, workspace: Path, output_directory: Path) -> Path:
    """Create a Finder-double-clickable launcher bound to this local workspace.

    The workspace currently sits in macOS' protected Documents directory.
    A `.command` file is intentionally used: Finder hands it to Terminal, which
    can receive the user's folder permission.  An unsigned `.app` cannot
    reliably inherit that access or automate Terminal without an extra macOS
    Automation permission, so it is not presented as a supported launcher.
    """

    workspace = workspace.resolve()
    python = workspace / ".venv" / "bin" / "python"
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    launcher = output_directory / LAUNCHER_NAME
    if launcher.exists():
        raise FileExistsError(f"refusing to overwrite existing launcher: {launcher}")

    logs = workspace / "reports" / "generated" / "desktop_app"
    launcher.write_text(
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
    _make_executable(launcher)
    return launcher


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    launcher = build_macos_launcher(
        workspace=args.workspace,
        output_directory=args.output,
    )
    print(launcher)


if __name__ == "__main__":
    main()
