"""Install an ordinary per-user macOS LaunchAgent; no administrator access."""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .config import ConfigurationError, ensure_home

LABEL = "de.studentwerk.dortmund-monitor"


def agent_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def agent_running() -> bool:
    return subprocess.run(["/bin/launchctl", "print", target()], capture_output=True, timeout=10).returncode == 0


def stop_agent() -> None:
    result = subprocess.run(["/bin/launchctl", "bootout", target()], capture_output=True, timeout=10)
    if result.returncode and agent_running():
        raise ConfigurationError("macOS could not stop the monitor's LaunchAgent.")


def build_plist(home: Path, python: str) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "studentwerk_monitor", "--home", str(home), "tick"],
        "WorkingDirectory": str(home / "runtime"),
        # A cheap local wake-up every five minutes handles delivery retries and
        # wake-from-sleep. The durable Berlin-time slot guard allows just one
        # website check per hour, plus Friday's final 23:30 check.
        "StartCalendarInterval": [{"Minute": minute} for minute in range(0, 60, 5)],
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LimitLoadToSessionType": "Aqua",
        "Umask": 0o077,
        "StandardOutPath": str(home / "logs/agent.log"),
        "StandardErrorPath": str(home / "logs/agent-error.log"),
    }


def install_agent(home: Path) -> Path:
    ensure_home(home)
    runtime = home / "runtime"
    runtime.mkdir(exist_ok=True, mode=0o700)
    logs = home / "logs"
    logs.mkdir(exist_ok=True, mode=0o700)
    source = Path(__file__).resolve().parent
    destination = runtime / "studentwerk_monitor"
    if source != destination.resolve():
        shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Prefer Homebrew's stable link so normal patch upgrades do not break it.
    stable_python = Path("/opt/homebrew/opt/python@3.13/bin/python3")
    python = str(stable_python if stable_python.is_file() else Path(sys.executable))
    path = agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if agent_running():
        stop_agent()
    with path.open("wb") as stream:
        plistlib.dump(build_plist(home, python), stream)
    path.chmod(0o600)
    result = subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, timeout=15)
    if result.returncode:
        raise ConfigurationError("macOS could not load the monitor's LaunchAgent. See the README troubleshooting steps.")
    return path


def remove_agent() -> None:
    stop_agent()
    agent_path().unlink(missing_ok=True)

