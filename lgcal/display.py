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
    def __init__(self, session_dir: Path, log, script_dir: Path = HERE, say=print):
        self.state_file = session_dir / "display_state.json"
        self.script = script_dir / "display_setup.ps1"
        self.log = log
        self.say = say
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
            raise RuntimeError("Display setup failed: " + ((run.stderr or run.stdout).strip()[-800:]
                                                           or f"exit code {run.returncode}"))
        self.state = json.loads(self.state_file.read_text(encoding="utf-8-sig"))
        self.log("DISPLAY " + json.dumps(self.state))
        return self.state

    def restore(self) -> None:
        """Undo what prepare changed (nothing to do if prepare never
        finished: the script puts Windows back itself when it fails)."""
        if not self.state_file.exists():
            return
        try:
            run = self._run("restore")
            problem = "" if run.returncode == 0 else (
                (run.stderr or run.stdout).strip()[-400:] or f"exit code {run.returncode}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            problem = str(exc)
        if not problem:
            self.log("DISPLAY restored")
            self.state_file.unlink(missing_ok=True)
            return
        self.log("DISPLAY restore failed: " + problem)
        changed = [what for key, what in (("topology_changed", "switch the display mode back from Extend (Win+P)"),
                                          ("hdr_changed", "turn Windows HDR back on for the TV"))
                   if self.state.get(key)]
        if changed:
            self.say("Could not put Windows back automatically; please " + " and ".join(changed) + ".")

    close = restore
