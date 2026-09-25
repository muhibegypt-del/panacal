# LG OLED AutoCal on a PC

This runs BigShoots' **PGenerator-Plus LG AutoCal** from a Windows PC, with no Raspberry Pi. It is the SDR 26-point greyscale calibration: white balance and gamma, written into the TV's 1D LUT (1D DPG). The PC draws the patches on the TV over HDMI and reads your meter with ArgyllCMS. The author's worker and LG helper do the calibration and talk to the TV.

The calibration and TV code is the author's, used with his permission (see `NOTICE.md`). Only the parts that were Raspberry Pi-specific are replaced; `PATCHES.md` lists the few marked lines that changed.

## What you need

- Windows 10 or 11, with the LG TV connected by HDMI and on the same network as the PC.
- An LG webOS OLED that PGenerator-Plus supports.
- Python 3.10 or newer (python.org, tick "Add to PATH").
- Strawberry Perl (strawberryperl.com). It includes IO::Socket::SSL, which is needed to reach the TV over wss.
- ArgyllCMS and a meter that Argyll's `spotread` supports (i1Display Pro, Spyder5 and others).
- A spectral correction (`.ccss`) for WRGB OLED for your colorimeter. Without one, a colorimeter usually misreads OLED white by a few dE, and AutoCal will faithfully calibrate to that error.

## One-time setup

**Windows**
1. Settings > Display: select **Extend these displays**, so the TV is a second screen. Use 60 Hz, turn **Use HDR** off, and turn Night light off.
2. In the GPU control panel, set the TV output to **RGB, full range, 8 bpc**:
   - NVIDIA: Output colour format RGB, Output dynamic range Full.
   - AMD: Pixel Format RGB 4:4:4 PC Standard (Full RGB), Colour Depth 8 bpc.

**LG TV**
1. Set the HDMI input's **Black Level** to **High** (full range) or Auto.
2. Select the picture mode you want to calibrate on that input.
3. Turn off energy saving and the AI or dynamic picture features. Let the TV warm up for about 30 minutes.
4. Enable **LG Connect Apps** (network control) in the TV's connection settings, and note the TV's IP address.

**This folder**
1. Run **Pair LG TV.bat** once. The first time, it creates `settings.json` and stops.
2. Edit `settings.json`:

| Setting | Meaning |
|---|---|
| `tv_ip` | The TV's IP address. |
| `picture_mode` | Picture mode to calibrate: `expert1` (Expert Bright Room), `expert2` (Expert Dark Room), `filmMaker`, `cinema`, `game`, `normal`, `eco`, `sports`, `vivid` or `personalized`. |
| `target_gamma` | `2.2`, `2.4`, `bt1886` or `srgb`. |
| `target_delta_e` | dE ITP each level must reach. The dashboard default is 0.5. |
| `patch_size` | Patch window as a percentage of screen area (10 is the default). |
| `perl`, `argyll_bin`, `meter.spotread` | Paths to Perl and ArgyllCMS. |
| `meter.args` | Extra `spotread` arguments; keep `-e`. |
| `meter.ccss` | Full path to your OLED `.ccss` file. |
| `meter.synthetic_black` | Report 0% as true black without reading it, as PGenerator does for OLED. |
| `pattern.screen` | Only needed if more than one secondary display is connected, for example `\\.\DISPLAY2`. |

3. Run **Pair LG TV.bat** again. The TV shows a PIN; type it in. The key is saved in `data\lg`, and later runs connect without a PIN.

## Run

1. Double-click **Run LG AutoCal.bat**. A black window opens on the TV with a white patch.
2. Put the meter on the centre of the patch and press Enter. This current white becomes the luminance reference, as in the PGenerator dashboard.
3. The author's worker then runs:
   - It reads the TV's picture settings and opens LG calibration mode.
   - It calibrates 100% white (chromaticity only), then 50%, 25%, 75% and 95% down to 2.3%. It measures each level and uploads a corrected 1D LUT until the level is within the dE target.
   - It smooths the shadow end and commits the final LUT. Calibration mode is then closed.

A run typically takes 15–30 minutes, depending on the meter's speed in the shadows. Progress is shown in the console. At the end it prints each level's measured and target luminance and its dE ITP.

Press **Ctrl+C** to stop. The worker finishes its current TV write and closes calibration mode before it exits.

Each run writes a folder under `sessions\`:
- `worker.log`: the worker's own log.
- `autocal.log`: every patch, reading and TV request.
- `worker_config.json` and `worker_state.json`: the full per-level history and the final 1D LUT.

## Scope

- SDR only. HDR10 and Dolby Vision AutoCal need HDR signalling and 10-bit patterns, which a Windows desktop window cannot produce. Use a Pi for those.
- The 3D LUT and CMS workflows are not included.
- Patches are drawn as 8-bit full-range RGB. This is the worker's 8-bit full-range path, which PGenerator-Plus also supports on the Pi. With 8 bpc output, Windows sends the drawn codes to the TV unchanged; `dispwin` resets the GPU video LUT to linear for the run and restores it afterwards.
- The Pi checks TV power over HDMI-CEC. The PC cannot, so the first TV command is the check.

## Offline tests

Run **Run Offline Tests.bat** to test without a TV or meter:
- **Transport**: the real LG helper connects and pairs by PIN over TLS on port 3001 with a fake webOS TV.
- **Simulated run**: the real worker completes a full calibration of a simulated LG OLED through the PC server. The simulated panel has per-channel gamma and white-balance errors, and the simulated meter has 0.3% noise.
- **Units**: the step list, the meter and pattern routes, and the LG route bookkeeping.

`python -m tests.sim.run_sim` runs the simulation on its own and keeps its session folder.
