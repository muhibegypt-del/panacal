# Panasonic GT60 / VT60 White-Balance AutoCal V5

AutoCal sets a D65 white point at every grey level on a Panasonic GT60/VT60 plasma in Professional 1 (ISF Day). It adjusts the two-point and 10-point white balance only. The 10-point gamma controls, the colour-management system and every picture control are left exactly as they are. Gamma comes from the TV's Gamma 2.4 preset.

## Before you run

1. **Warm up.** Run the TV for 30–60 minutes first. Plasma white drifts while the panel warms.
2. **Brightness and Contrast first.** Set them with the AVS HD 709 black and white clipping patterns before calibrating. On a VT60 in Professional mode Brightness usually lands near 0. Contrast moves the top of the grey scale and Brightness the bottom, so changing either afterwards partly undoes the calibration. AutoCal reads black before it changes anything and stops if black is raised (for example Brightness left at +30).
3. Use the HDMI input and source chain you intend to watch, and keep the AMD/TV HDMI range configuration from the successful range sweep.
4. In Windows, use Extend mode with the Panasonic as the sole 1920x1080 secondary screen.
5. Open isfccc Network on the TV and leave it at Waiting for Connection.
6. Check `tv.default_ip` and `meter.default_executable` in `config.json`. Run **Run Settle Test.bat** once (see below).

## Run AutoCal

Double-click **Run AutoCal V4.bat**. There is one prompt: when the white patch appears, place the Spyder5 flat on its centre and press Enter. To use another IP or spotread path for one run: `python autocal.py run --ip 192.168.1.50 --meter C:\path\spotread.exe`.

1. Opens the pattern window, connects to ISFccc, initialises the Spyder5 (plasma CCSS, 60 Hz), saves every TV setting for recovery, and loads a linear PC video LUT.
2. **Reference white.** 100% white after a 3 s settle, three reads. Every target depends on it.
3. **Black check.** One reading of 0%. If black is above 0.1% of white, it stops without changing anything.
4. **Two-point high** at 100% (guards 60 and 80%), then **two-point low** at 20% (guards 30 and 40%).
5. **10-point white balance** from the top down: 95% for slot 100, then 90% … 10%.
6. **Final high polish**, then a **verification sweep**: 0%, 10–90%, 95% and 100%.

### How each point is corrected

- **Learn.** The first stage nudges red and then blue by 6 steps to measure how one step moves the colour (u′v′). That response model is kept.
- **Reuse.** Each lower 10-point slot starts from the model its neighbour has just refined, so most points need no probe at all. A point probes only when a move from a borrowed model fails, or when the borrowed model says no step would help.
- **Simulate.** Every whole-step red/blue setting within the step limit is tried against the model. The one with the lowest predicted tint is applied. Small corrections are no longer rounded away.
- **Check.** The result is measured. A change is kept only if it reduces the tint by more than the meter's own noise at that level and does not worsen a guard level. Otherwise the previous setting is restored immediately. Every measured result also updates the model.
- **Stop** when the tint is within 0.0005 u′v′ of D65, or within the meter's noise at that level if that is larger, or when no step helps.

Tint is the u′v′ distance to D65, which is independent of brightness. The dark end is held to the same colour accuracy as white. (dE2000 relative to white scores the same tint about ten times smaller at 10% than at 100%.)

Noise is measured during the run at no extra cost. Readings at unchanged controls, such as before and after each probe and the repeated reads at 10% and white, show how much the Spyder5 wanders at each level.

### Measurement safety

- Patches are drawn at exact 8-bit codes (full range, rounded half-up: 10% = 26, 30% = 77). Targets use the code actually drawn.
- An invalid reading (no luminance, or Argyll's equal-energy "nothing seen" result) is discarded and re-read.
- A reading at 15% or above that is below 0.35× or above 2.2× the expected brightness makes it re-show the patch and read again, up to three times. This catches a patch that was not drawn in time or a meter that has slipped.
- A spotread timeout or USB error restarts spotread and retries the reading instead of ending the run.
- If anything fails after the TV has been changed, the complete original TV snapshot and the saved video LUT are restored.

## Read the result

The console prints the white-point result (average and worst tint), chroma dE2000, dE2000 including luminance, and the measured gamma. Each run writes a timestamped folder under `sessions`:

- `report.html`: open it in a browser. It shows summary tiles, tint by level (before its correction versus final), predicted versus measured for every move, the controls per point, the final sweep and every move tried.
- `autocal.log`: every patch, reading, TV command and keep/restore decision, with the model used and the predicted and measured tint.
- `autocal_result.json`: all stages, models, moves and measurements.
- `pre_calibration_snapshot.json`, `final_snapshot.json`, `effective_config.json`.

## Settle time

Before each reading AutoCal waits `pattern.settle_seconds` (0.5 s) after the patch or a TV control changes. Double-click **Run Settle Test.bat** once to confirm this suits your TV and meter. The test takes about two minutes. It compares readings taken 0.25, 0.5 and 1 s after a black-to-white patch change, and after a two-point red change, with a fully settled white. It then prints the shortest wait that still gave a settled reading. Put that value in `config.json`. Two-point high red is always put back.

## Verify without changing anything

Double-click **Run Read-Only Verification V4.bat**. It performs the same 12-point sweep, writes no calibration values, and saves a `report.html`.

## After AutoCal

A small Brightness adjustment for your room is fine; run the read-only verifier afterwards. Confirm the result in HCFR with the same plasma correction, patch size, range chain, Rec.709/D65 target and 2.4 gamma target.

## Optional: Argyll low-light mode

PGenerator-Plus reports that a single `-Y aa` reading was steadier near black than several averaged short reads on an i1Display Pro. Argyll's documentation only describes `-Y a`, for a different meter. To see whether your Argyll accepts it with the Spyder5, run `spotread -e -Y aa` once. It is not used by AutoCal.

## Accuracy boundary

The maths and decision loop are covered by offline tests: the published CIEDE2000 reference pair, the integer move search and model update, measurement safety, real child-process meter fixtures, and complete simulated calibrations. The simulated runs include a noisy, drifting meter whose response differs from point to point. The target is a tint within 0.0005 u′v′, not a promise. The real result still depends on Spyder5 repeatability, plasma drift, meter correction quality, the unchanged HDMI range chain and the TV's control resolution.

Run **Run Offline Tests.bat** to execute the hardware-free checks.

## Meter transport

The persistent spotread process uses Argyll's documented ARGYLL_NOT_INTERACTIVE=1 environment setting. Prompt detection handles text without a newline, and each trigger plus newline is sent in one pipe write. A reading completes only after its XYZ result and the next ready prompt arrive. Startup and measurement failures close and reap the owned child process; error messages include the last meter output. A failed read restarts spotread with a fresh output queue. Meter initialisation occurs before the video LUT is changed.

## Documentation check (24 September 2026)

- Panasonic's official 60/65/600 guide recommends small plasma windows, two-point first, then grayscale and then CMS, and optimising Brightness after AutoCal for the room. It does not document the proprietary ASCII commands or prescribe our solver's step sizes. https://app.spectracal.com/Documents/QSGs/Panasonic%2060_65_600%20Series%20QuickStart.pdf
- Argyll documents emissive XYZ measurements, Spyder5 CCSS support, and a refresh-rate override. The configured 60 Hz override assumes the source remains at 60 Hz; it is not automatic refresh detection. https://www.argyllcms.com/doc/spotread.html
- Argyll documents prompts without newlines and the Windows single-write input requirement. https://www.argyllcms.com/doc/Environment.html
- Socket framing, handshake, and point selectors were compared with the user's working gt60_control.py and recorded TV replies. Numeric readback rejects a reply for a different control.
- Slot 100 acts on the 95% patch (measured on this VT60, session 20260924_185416: 3.2× more effect at 95% than at 100%), so it is corrected at 95%. 100% is set by two-point high.
- Window area (6.5%), settle delay (0.5 s), the 6-step probe, stop and improvement thresholds are implementation choices, not values certified by those documents. Final measurements remain necessary to judge the physical result.
