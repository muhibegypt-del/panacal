# LG OLED AutoCal on a PC

This runs BigShoots' **PGenerator-Plus LG AutoCal** from a Windows PC, with no Raspberry Pi. It is the SDR 26-point greyscale calibration: white balance and gamma, written into the TV's 1D LUT. The calibration and TV code is the author's, used with his permission (see `NOTICE.md`). `PATCHES.md` lists the few lines changed to run on Windows, plus one upstream bug fix.

## Use it

1. Connect the PC to the LG TV by HDMI. The TV must be on the same network as the PC.
2. Plug in the meter and double-click **LG AutoCal.bat**.
3. The first time only, type the PIN the TV shows.
4. When asked, put the meter on the white patch in the middle of the TV. It starts by itself.

There is nothing to configure. On each run it:

| Step | What it does |
|---|---|
| Tools | Finds Perl and ArgyllCMS. If either is missing, it downloads it into `tools\` (one time; no installer and no admin rights). |
| TV | Finds the LG on the network, pairs once, and remembers it. |
| Picture mode | Calibrates the mode the TV is in (Expert, Filmmaker, Cinema, ...). It first closes a calibration session left open by a crashed run. |
| Display | Finds the TV's output by name. Switches Windows from Duplicate to Extend, and turns Windows HDR off on the TV. Clears the GPU video LUT. All of this is restored afterwards. |
| Meter | Uses a WOLED `.ccss` correction if one is installed (ArgyllCMS, DisplayCAL or the `ccss` folder). It notices the meter on the patch by flashing the patch to black. |
| Video range | Measures whether the TV expects full or limited range and draws patterns to match, so the TV's Black Level setting can stay as it is. If the PC sends limited range to a TV set to full, black is lifted; it stops before changing anything and says which setting to change. |
| Calibrate | Runs the author's worker: 100% white, then 50%, 25%, 75% and 95% down to 2.3%, uploading a corrected 1D LUT until each level is within dE ITP 0.5. It then commits the LUT and closes calibration mode. |
| Verify | Measures 100% down to 5% again on the finished calibration and prints dE ITP per level, using the author's formula. The numbers are a fresh measurement, not the solver's own. |

Ctrl+C stops safely. The worker finishes its current write and closes calibration mode.

Each run keeps everything in `sessions\<date_time>\`: the console output, every reading and TV request, the worker's log and state, and `verification.json`.

## Optional overrides

Copy `settings.example.json` to `settings.json` only to change a default:

| Setting | Default | Meaning |
|---|---|---|
| `target_gamma` | `bt1886` | Also `2.2`, `2.4` or `srgb`. `bt1886` is the author's default; on an OLED's zero black it equals 2.4. |
| `target_delta_e` | `0.5` | dE ITP each level must reach. |
| `picture_mode` | the TV's current mode | Calibrate a different mode. |
| `tv_ip` | found automatically | For networks where discovery is blocked. |
| `patch_size` | `10` | Patch window as a percentage of screen area. |
| `meter.ccss` | found automatically | A specific correction file. |

## Scope

- SDR only. HDR10 and Dolby Vision need HDR signalling and 10-bit patterns, which a Windows desktop window cannot produce.
- The 3D LUT and CMS workflows are not included.
- Patches are 8-bit RGB. This is the worker's 8-bit path.

## Offline tests

**Run Offline Tests.bat** checks the whole thing without a TV or meter:
- The real LG helper pairs by PIN over TLS with a fake webOS TV.
- The display script runs against a simulated duplicated display with HDR on.
- The real worker calibrates a simulated LG OLED end to end, and the independent verification checks the result. It covers a TV on full range, a TV on Black Level Low, the lifted-black case, and a stale calibration session.
