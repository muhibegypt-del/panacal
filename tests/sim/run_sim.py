"""Run the real, unmodified-logic AutoCal worker against the simulated TV.

    python -m tests.sim.run_sim [session_dir]

Everything between the worker and the TV is the production path: the
worker's HTTP calls reach lgcal.server, the LG routes build helper requests
exactly as on the PC, and only the final hop (pgenerator-lg -> TV) is
replaced by stub_helper.pl -> SimTV.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

from lgcal import app
from tests.sim.sim_tv import Panel, SimMeter, SimPattern, SimTV, serve

HERE = Path(__file__).resolve().parent


def find_test_perl() -> str:
    """PATH perl, or the one the launcher found or installed."""
    from lgcal.setup import load_cache
    return shutil.which("perl") or load_cache().get("perl") or ""
FAST = {
    # The worker's own waits for a real panel; nothing settles in a simulation.
    "post_commit_settle_ms": 0,
    "post_commit_white_resettle_ms": 0,
    "post_commit_low_shadow_settle_ms": 0,
    "post_commit_low_shadow_read_settle_ms": 0,
    # Shortened in case the worker enables pattern insertion.
    "patch_insert_patch_duration_ms": 0,
    "patch_insert_time_duration_ms": 0,
    "patch_insert_post_settle_ms": 0,
}


def simulate(session_dir: Path, settings: dict | None = None, *, black_level: str = "high",
             gpu_range: str = "full", tv_mode: str = "expert2", stale_calibration: bool = False,
             reset: bool = True, peak: float = 150.0, meter_class=None, stages=("greyscale", "colour"),
             panel=None, tv=None, hdr: bool = False):
    """Returns (exit code, worker state, SimTV, SimMeter, console text)."""
    data_dir = Path(tempfile.mkdtemp(prefix="lgcal-sim-data-"))
    saved_data_dir = app.DATA_DIR
    app.DATA_DIR = data_dir
    (data_dir / "clients.json").write_text(json.dumps({
        "ip": "192.0.2.10", "manual_ip": "192.0.2.10", "client_key": "sim-key",
        "model_name": SimTV.MODEL["model_name"], "calibration_mode": stale_calibration,
        "calibration_picture_mode": tv_mode if stale_calibration else ""}), encoding="utf-8")
    if tv is None:
        panel = panel or Panel(peak=peak, black_level=black_level, gpu_range=gpu_range)
        tv = SimTV(panel, leftover_calibration=reset)
    panel = tv.panel
    tv.picture_mode = tv_mode
    tv.calibration_mode = stale_calibration
    if hdr:
        panel.hdr = True
        tv.picture_mode = tv_mode if tv_mode.startswith("hdr") else "hdrCinema"
        stages = ("hdr",)
    server = serve(tv)
    pattern = SimPattern(hdr=hdr)
    meter = (meter_class or SimMeter)(panel, pattern)
    settings = {**app.DEFAULTS, "api_port": 18765, **(settings or {})}
    console = io.StringIO()
    try:
        with contextlib.redirect_stdout(console):
            try:
                code = app.run(settings, pattern=pattern, meter=meter, helper=HERE / "stub_helper.pl",
                               perl=find_test_perl(), session_dir=session_dir,
                               extra_env={"PGEN_SIM_PORT": str(server.server_address[1])},
                               config_overrides=FAST, delay_scale=0.0, find_tv_on_network=False,
                               reset_first=reset, stages=stages)
            except SystemExit as exc:
                print(exc)
                code = 2
    finally:
        server.shutdown()
        server.server_close()
        app.DATA_DIR = saved_data_dir
        shutil.rmtree(data_dir, ignore_errors=True)
    state_file = session_dir / "worker_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    return code, state, tv, meter, console.getvalue()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="lgcal-sim-"))
    code, state, tv, meter, console = simulate(target, black_level=sys.argv[2] if len(sys.argv) > 2 else "high")
    print(console)
    print(f"exit={code} status={state.get('status')} uploads={tv.uploads} reads={meter.reads}")
    print("helper actions:", {a: tv.requests.count(a) for a in sorted(set(tv.requests))})
