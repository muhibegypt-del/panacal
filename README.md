# LG OLED AutoCal on a PC

This runs BigShoots' **PGenerator-Plus LG AutoCal** from a Windows PC, with no Raspberry Pi. It is the SDR 26-point greyscale calibration: white balance and gamma, written into the TV's 1D LUT. The calibration and TV code is the author's, used with his permission (see `NOTICE.md`). `PATCHES.md` lists the few lines changed to run on Windows, plus one upstream bug fix.

## Use it

1. Connect the PC to the LG TV by HDMI. The TV must be on the same network as the PC.
2. Plug in the meter and double-click **LG AutoCal.bat**.
3. The first time only, type the PIN the TV shows.
4. When asked, put the meter on the white patch in the middle of the TV. It starts by itself.

There is nothing to configure. Everything that could stop the run (tools, TV, display, meter, video range) is checked before the TV is changed. On each run it:

| Step | What it does |
|---|---|
| Tools | Finds Perl and ArgyllCMS. If either is missing, it downloads it into `tools\` (one time; no installer and no admin rights). |
| TV | Finds the LG on the network, pairs once, and remembers it. |
| Picture mode | Calibrates the mode the TV is in (Expert, Filmmaker, Cinema, ...). If an older TV cannot report its mode, it asks you which one it is showing. It first closes a calibration session left open by a crashed run. |
| TV preparation | Once the meter is in place and the signal chain checks out, it does what PGenerator's wizard does before calibrating. It resets the picture mode to factory, which also turns off energy saving and dynamic processing. It clears the white balance and 1D LUT (the TV must confirm and verify this), and clears any leftover 3D LUT or matrix. Your OLED brightness is read first and put back. |
| Display | Finds the TV's output by name. Switches Windows from Duplicate to Extend, and turns Windows HDR off on the TV. Clears the GPU video LUT and keeps Windows from blanking the screen during the run. All of this is restored afterwards. If Windows Night light is on, it stops and says so, because Night light would be calibrated into the TV. |
| Meter | Uses a WOLED `.ccss` correction if one is installed (ArgyllCMS, DisplayCAL or the `ccss` folder). It notices the meter on the patch by flashing the patch to black. |
| Video range | Measures whether the TV expects full or limited range and draws patterns to match, so the TV's Black Level setting can stay as it is. If the PC sends limited range to a TV set to full, black is lifted; it stops before changing anything and says which setting to change. |
| Calibrate | Runs the author's worker: 100% white, then 50%, 25%, 75% and 95% down to 2.3%, uploading a corrected 1D LUT until each level is within dE ITP 0.5. It then commits the LUT and closes calibration mode. |
| Dark levels | A Spyder5 stops tracking the TV at about 0.3 cd/m² (7% on a 200 cd/m² white). Levels dimmer than that are not steered by the meter: the worker takes one look and moves on, and the committed LUT carries the curve calibrated at the next four levels down to black. The session's `dark_end.json` has the worker's table and the committed one. |
| Verify | Measures 100% down to 5% again on the finished calibration and prints dE ITP per level, using the author's formula. The numbers are a fresh measurement, not the solver's own. Levels dimmer than the meter floor are marked `*` and left out of the average. |

Ctrl+C stops safely. The worker finishes its current write and closes calibration mode.

Whenever a run stops, the last lines say why, what state the TV was left in (untouched, reset but not yet calibrated, or partly calibrated), and where the session folder is.

**Undo LG AutoCal.bat** returns the TV's current picture mode to its factory white balance and LUTs.

Each run keeps everything in `sessions\<date_time>\`: `console.txt` (what the window showed), every reading and TV request, the worker's log and state, and `verification.json`.

## Optional overrides

Copy `settings.example.json` to `settings.json` only to change a default. The file is checked before anything starts: every wrong value is listed at once, and a misspelt setting name is pointed out rather than ignored.

| Setting | Default | Meaning |
|---|---|---|
| `target_gamma` | `bt1886` | Also `2.2`, `2.4` or `srgb`. `bt1886` is the author's default; on an OLED's zero black it equals 2.4. |
| `target_delta_e` | `0.5` | dE ITP each level must reach. |
| `picture_mode` | the TV's current mode | Calibrate a different mode. |
| `tv_ip` | found automatically | For networks where discovery is blocked. |
| `patch_size` | `10` | Patch window as a percentage of screen area. |
| `meter.ccss` | found automatically | A specific correction file. |
| `meter.floor_cd_m2` | `0.3` | Dimmest level the meter is trusted to steer. Lower it for an i1Display Pro (about `0.01`); `0` lets the worker calibrate every level. |
| `reset_picture_mode` | `true` | `false` keeps your other picture settings; the white balance and LUTs are still cleared first. |

## Scope

- SDR only. HDR10 and Dolby Vision need HDR signalling and 10-bit patterns, which a Windows desktop window cannot produce.
- The 3D LUT and CMS workflows are not included.
- Patches are 8-bit RGB. This is the worker's 8-bit path. On that path the worker turns off its OLED pattern insertion (grey flashes between readings); it uses insertion only with 10-bit patterns.

## Offline tests

**Run Offline Tests.bat** checks the whole thing without a TV or meter:
- The real LG helper pairs by PIN over TLS with a fake webOS TV.
- The display script runs against a simulated duplicated display with HDR on.
- The real worker calibrates a simulated LG OLED end to end, and the independent verification checks the result. It covers a TV on full range, a TV on Black Level Low, the lifted-black case, and a stale calibration session.
