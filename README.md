# LG OLED AutoCal on a PC

This runs BigShoots' **PGenerator-Plus LG AutoCal** from a Windows PC, with no Raspberry Pi. It runs two of the author's calibrations in turn: the SDR 26-point greyscale (white balance and gamma, written into the TV's 1D LUT), then colour (a 3D LUT that maps HD/BT.709 colours onto the panel). The calibration and TV code is the author's, used with his permission (see `NOTICE.md`). `PATCHES.md` lists the few lines changed to run on Windows, plus one upstream bug fix.

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
| Meter | Uses the spectral correction (`.ccss`) that best matches the TV model: one measured on the same size and series (a 55G2 correction ships in `ccss\`), then the same series, then the same model year, then any LG WOLED one. LCD corrections are never used, even though LG gives its QNED LCDs the same EDID name ("LG TV SSCR2"). The window says which one it picked and why. It notices the meter on the patch by flashing the patch to black. |
| Video range | Measures whether the TV expects full or limited range and draws patterns to match, so the TV's Black Level setting can stay as it is. If the PC sends limited range to a TV set to full, black is lifted; it stops before changing anything and says which setting to change. |
| Calibrate | Runs the author's worker: 100% white, then 50%, 25%, 75% and 95% down to 2.3%, uploading a corrected 1D LUT until each level is within dE ITP 0.5. It then commits the LUT and closes calibration mode. |
| Dark levels | A Spyder5 stops tracking the TV at about 0.3 cd/m² (7% on a 200 cd/m² white). Levels dimmer than that are not steered by the meter: the worker takes one look and moves on, and the committed LUT carries the curve calibrated at the next four levels down to black. The session's `dark_end.json` has the worker's table and the committed one. |
| Colour | The greyscale preparation clears LG's stored calibration data, which includes its factory BT.709 colour conversion, so the panel would show its native wide colours. The author's colour worker measures white, red, green, blue and black, and uploads a 3D LUT that maps BT.709 onto the measured panel. The 3D LUT leaves greys to the 1D LUT, and the greyscale table is committed again at the end. |
| Verify | Measures 100% down to 5% again on the finished calibration, then red, green, blue, cyan, magenta and yellow at full signal, and prints dE ITP for each against BT.709, using the author's formula. The numbers are a fresh measurement, not the solver's own. Levels dimmer than the meter floor are marked `*` and left out of the average. |

Ctrl+C stops safely. The worker finishes its current write and closes calibration mode.

Whenever a run stops, the last lines say why, what state the TV was left in (untouched, reset but not yet calibrated, or partly calibrated), and where the session folder is.

**LG Colour AutoCal.bat** runs only the colour stage on the picture mode the TV is in. It finds that mode's last finished greyscale calibration in `sessions\`, keeps it, and commits it again at the end.

**LG HDR AutoCal.bat** calibrates an HDR10 picture mode (Cinema, Cinema Home, Filmmaker, Game Optimizer). It runs the author's HDR flow:

| Step | What it does |
|---|---|
| Patterns | A Windows window cannot send HDR10, so patterns come from madTPG, madVR's free test pattern generator (the one DisplayCAL, HCFR and Calman use). It is downloaded into `tools\madVR` the first time. madTPG's HDR mode is a button in its window: the run asks for one click and continues when the TV reports an HDR picture mode. |
| Reset | The HDR reference reset: identity 1D LUT, BT.2020 3D LUT and matrix, factory tone map. |
| Greyscale | The worker's HDR path: 20 levels from 100% down to 1.4%, each on a 2.2 curve against the measured peak while the TV is held in LG's calibration pass-through. Levels dimmer than the meter floor on that curve (with a Spyder5 on a ~700 cd/m2 G2: below 4%) follow the curve calibrated above them. |
| Colour and tone map | The colour worker's HDR matrix run inherits that calibration session, uploads the BT.2020 3D LUT, then LG's tone map for the measured peak together with the greyscale table, and ends calibration mode. |
| Verify | PQ greyscale from 5% to 70% signal against ST 2084, and BT.709 colours inside the BT.2020 container on a 100 cd/m2 white, in dE ITP. Levels above half the peak are tone-mapped by the TV and shown, not scored. |

**Undo LG AutoCal.bat** clears the current picture mode's white balance and LUTs. It does not bring back LG's factory colours; run LG AutoCal afterwards.

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
| `madvr` | found or downloaded | A madVR folder (with `madTPG.exe`) to use for HDR patterns. |
| `meter.floor_cd_m2` | `0.3` | Dimmest level the meter is trusted to steer. Lower it for an i1Display Pro (about `0.01`); `0` lets the worker calibrate every level. |
| `reset_picture_mode` | `true` | `false` keeps your other picture settings; the white balance and LUTs are still cleared first. |

## Scope

- HDR10 needs madTPG (above). Dolby Vision is not included.
- Colour uses the author's `matrix` method (5 patches). His finer methods (ramp, lattice, skeleton) are not wired in.
- Patches are 8-bit RGB. This is the worker's 8-bit path. On that path the worker turns off its OLED pattern insertion (grey flashes between readings); it uses insertion only with 10-bit patterns.

## Offline tests

**Run Offline Tests.bat** checks the whole thing without a TV or meter:
- The real LG helper pairs by PIN over TLS with a fake webOS TV.
- The display script runs against a simulated duplicated display with HDR on.
- The real worker calibrates a simulated LG OLED end to end, and the independent verification checks the result. It covers a TV on full range, a TV on Black Level Low, the lifted-black case, and a stale calibration session.
