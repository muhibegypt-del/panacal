"""Get the TV ready as a pattern screen, and put Windows back afterwards."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def powershell() -> str:
    return (os.environ.get("LGCAL_POWERSHELL") or shutil.which("powershell.exe")
            or shutil.which("powershell") or shutil.which("pwsh") or "powershell.exe")


class DisplaySetup:
    def __init__(self, session_dir: Path, log, script_dir: Path = HERE):
        self.state_file = session_dir / "display_state.json"
        self.script = script_dir / "display_setup.ps1"
        self.log = log
        self.state: dict = {}

    def _run(self, action: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.script),
             "-Action", action, "-StateFile", str(self.state_file)],
            capture_output=True, text=True, timeout=90, creationflags=NO_WINDOW)

    def prepare(self) -> dict:
        """Extend the desktop if needed, find the LG output by name, turn
        HDR off on it. Returns the state, including 'device' (\\\\.\\DISPLAYn)."""
        run = self._run("prepare")
        if run.returncode != 0:
            raise RuntimeError("Display setup failed: " + (run.stderr or run.stdout).strip()[-800:])
        self.state = json.loads(self.state_file.read_text(encoding="utf-8-sig"))
        self.log("DISPLAY " + json.dumps(self.state))
        return self.state

    def restore(self) -> None:
        if not self.state_file.exists():
            return
        run = self._run("restore")
        self.log("DISPLAY restored" if run.returncode == 0 else
                 "DISPLAY restore failed: " + (run.stderr or run.stdout).strip()[-400:])

    close = restore
