# PC-PORT patches to the PGenerator-Plus files

`pgen/` holds files from [BigShoots/PGenerator-Plus](https://github.com/BigShoots/PGenerator-Plus) at commit `835b9b9` (5 September 2026):

| This repo | Upstream |
|---|---|
| `pgen/bin/meter_lg_autocal.pl` | `usr/bin/meter_lg_autocal.pl` (the AutoCal worker) |
| `pgen/bin/pgenerator-lg` | `usr/sbin/pgenerator-lg` (the LG webOS helper) |
| `pgen/share/PGenerator/PGMath.pm`, `PGCalibrationMath.pm`, `PGMeterReading.pm`, `PGSignalCode.pm` | `usr/share/PGenerator/` |

Commit `d71e990` imports them unmodified. Every later change is marked `PC-PORT` (portability) or `PC-PORT FIX` (one upstream bug). Run `git diff d71e990 -- pgen/` to see the full diff (35 lines added, 12 removed).

The calibration logic is the author's: targets, the solver, the 1D LUT build, the LG protocol and calibration-mode handling. The single change to it is the bug fix below. Each patch falls back to the original value when its environment variable is unset. The TLS change applies only on Windows or when `PGEN_LG_NATIVE_TLS` is set. On a Pi the files therefore behave exactly as upstream.

## Worker (`meter_lg_autocal.pl`)

| Line | Change | Why |
|---|---|---|
| `$api_host`, `$api_port` | `PGEN_API_HOST` / `PGEN_API_PORT` | The worker calls the PGenerator API on port 80. On a PC, `lgcal/server.py` serves those routes on an unprivileged local port. |
| `lg_helper_json` | When `PGEN_LG_HELPER` is set, run `"$^X" "<helper>"` with the request in `%ENV` | The original runs `timeout Ns env VAR=… /usr/sbin/pgenerator-lg` through `sh`. Windows has no `timeout`, `env` or `sh`. The helper's own socket timeouts still apply. |
| `lg_clients` | `PGEN_LG_DATA_DIR` | Location of the paired-TV store (`clients.json`). |
| `autocal_ddc_reset_diag_log` | `PGEN_LG_DATA_DIR` | Location of `last-write.log`. |

## Bug fix in the worker (`PC-PORT FIX`)

In the 8-bit **RGB limited** path, the worker converts each patch code to the 10-bit legal code that picks its 1D LUT entry:

```perl
my $code10=int(64+($code+0)*(940-64)/(235-16)+0.5);      # upstream
my $code10=int(64+($code+0-16)*(940-64)/(235-16)+0.5);   # fixed
```

Upstream maps legal black (code 16) to 128 instead of 64. That puts every entry about 60 codes too high, so the worker tunes LUT entries the patch never uses. The rest of the worker already treats 16 as black: the 10-bit branch uses the code directly, and the target is `(code - 16)/219`. With the fix, the first entry becomes the worker's own logged constant `limited_2.3_idx=21` instead of 90. In the simulation, a TV on Black Level Low went from a worst verified dE ITP of 40 to 0.55. Full range, and anything at 10 bits, never reaches this line. This is worth passing to the author.

## Helper (`pgenerator-lg`)

| Line | Change | Why |
|---|---|---|
| `websocket_connect_candidate` | On Windows, or with `PGEN_LG_NATIVE_TLS`, open `wss://TV:3001` with `IO::Socket::SSL` (certificate not verified) | The Pi pipes TLS through a `socat` child started with `sh -c`. Windows has neither. `SSL_VERIFY_NONE` matches socat's `verify=0`, because the TV presents a self-signed certificate. `ws://3000` is unchanged. |
| `socket_read_exact`, `socket_read_headers` | Check `$sock->pending` before `select()` | A TLS record can carry more than one WebSocket frame. The extra bytes sit decrypted inside OpenSSL, where `select()` cannot see them. Without this check even the first hello times out; `tests/test_offline.py` exercises this against a fake TV. |
| `$DIAG_LOG_PATH`, `$LG_DDC_DIR` | `PGEN_LG_DATA_DIR` | Data location. |
| `lg_3d_lut_reset_workflow` | `PGEN_LG_TMP_DIR` | Temporary file location (`/tmp` does not exist on Windows). |
| `use Scalar::Util ()` | added | Used by the `pending` check. |

## Left as they are

- The `PGAutoCalRun.pm` require uses an absolute Pi path inside `eval`. On a PC it simply does not load, and the worker already checks `$PGAC_LOADED` before using it (run-history snapshots only).
- Optional trace files under `/tmp` and `/var/log` are written inside `eval`. On Windows the open fails and the worker logs one line.
- The worker finds its modules from its own path split on `/`. The launcher passes forward-slash paths and also sets `PERL5LIB`.

## What replaces the Pi

| On the Pi | On the PC (`lgcal/`) |
|---|---|
| PGenerator renderer (DRM/KMS) | `pattern_host.ps1`: a borderless window on the TV. It draws the exact 8-bit code and patch area, and the GPU video LUT is linearised with Argyll `dispwin` for the run. |
| `meter_session.sh` + `spotread` | `meter.py`: one persistent `spotread`. It follows meter_session.sh: draw the patch, wait `delay_ms`, read or average; code 0 is reported as synthetic black. |
| WebUI routes in `webui.pm` / `lg.pm` | `server.py` and `lg.py`: the same routes and the same helper requests, timeouts, `clients.json` bookkeeping and held-calibration-mode rules as `lg.pm`. |
| Dashboard wizard | `app.py` and `steps.py`: the same request body and 26-point step list the dashboard sends (SDR RGB, 8-bit, full or limited range). |
| CEC, mDNS and TV scan in `lg.pm` | `discover.py`: SSDP, then a sweep of the local subnet. Every candidate is confirmed with the helper's `probe`. |
| Stale-session cleanup in `lg.pm` | `LG.clear_stale_calibration_mode`: CAL_END before a run if an earlier run died while holding calibration mode. |
