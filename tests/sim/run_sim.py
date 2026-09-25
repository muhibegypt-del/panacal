"""Run the real, unmodified-logic AutoCal worker against the simulated TV.

    python -m tests.sim.run_sim [session_dir]

Everything between the worker and the TV is the production path: the
worker's HTTP calls reach lgcal.server, the LG routes build helper requests
exactly as on the PC, and only the final hop (pgenerator-lg -> TV) is
replaced by stub_helper.pl -> SimTV.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from lgcal import app
from tests.sim.sim_tv import Panel, SimMeter, SimPattern, SimTV, serve

HERE = Path(__file__).resolve().parent
FAST = {
    # The worker's own waits for a real panel; nothing settles in a simulation.
    "post_commit_settle_ms": 0,
    "post_commit_white_resettle_ms": 0,
    "post_commit_low_shadow_settle_ms": 0,
    "post_commit_low_shadow_read_settle_ms": 0,
}


def simulate(session_dir: Path, settings: dict | None = None) -> tuple[int, dict, SimTV, SimMeter]:
    data_dir = Path(tempfile.mkdtemp(prefix="lgcal-sim-data-"))
    app.DATA_DIR = data_dir
    (data_dir / "clients.json").write_text(json.dumps({
        "ip": "192.0.2.10", "manual_ip": "192.0.2.10", "client_key": "sim-key",
        "model_name": SimTV.MODEL["model_name"]}), encoding="utf-8")
    panel = Panel()
    tv = SimTV(panel)
    server = serve(tv)
    pattern = SimPattern()
    meter = SimMeter(panel, pattern)
    settings = settings or {"picture_mode": "expert1", "target_gamma": "2.2", "api_port": 18765}
    try:
        code = app.run(settings, pattern=pattern, meter=meter, helper=HERE / "stub_helper.pl",
                       perl=shutil.which("perl"), interactive=False, session_dir=session_dir,
                       extra_env={"PGEN_SIM_PORT": str(server.server_address[1])},
                       config_overrides=FAST, delay_scale=0.0)
    finally:
        server.shutdown()
        shutil.rmtree(data_dir, ignore_errors=True)
    state = json.loads((session_dir / "worker_state.json").read_text(encoding="utf-8"))
    return code, state, tv, meter


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="lgcal-sim-"))
    code, state, tv, meter = simulate(target)
    print(f"exit={code} status={state.get('status')} uploads={tv.uploads} reads={meter.reads}")
    print("helper actions:", {a: tv.requests.count(a) for a in sorted(set(tv.requests))})
