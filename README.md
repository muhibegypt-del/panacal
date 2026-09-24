# Panasonic GT60 / VT60 Grayscale AutoCal V4

This version calibrates grayscale and gamma only. It does not touch the colour-management system.

## Before you run

1. Use the HDMI input and source chain you intend to watch.
2. In Windows, use Extend mode with the Panasonic as the sole 1920x1080 secondary screen.
3. Keep the AMD/TV HDMI range configuration unchanged from the successful range sweep.
4. Select the ISF picture mode to calibrate.
5. Put the Spyder5 flat against the centre of the Panasonic.
6. Open isfccc Network on the TV and leave it at Waiting for Connection.

## Run AutoCal

Double-click **Run AutoCal V4.bat**.

V4 starts from your current settings in the configured ISF Day mode:

1. Opens a 100% white window on the Panasonic and connects to ISFccc.
2. Initialises the Spyder5 with the Plasma CCSS and 60 Hz refresh setting.
3. Saves your current TV settings for recovery and saves/linearises the PC video LUT. Existing picture and grayscale settings are used as the calibration starting point.
4. Reads white once and starts two-point high correction using that reading. Small red/blue probes measure the TV's response; the solver calculates the correction, applies it, and checks the result.
5. Corrects the low end at 10%, then continues with detailed white balance and gamma.
6. Runs one verification sweep at the end (0%, the ten control points 10–90% and 95%, and 100%) and checks measured black/shadow/headroom symptoms.

There is no opening 0-100% sweep and no separate discarded meter-check reading. Two-point correction reuses the first white reading, the restored response measurement, and the latest accepted measurement at unchanged controls. Measurements at affected levels still check that a proposed change improves the overall result. Black is measured later when the gamma stage needs its luminance target.

Measurements are taken only at levels the TV can correct. The Panasonic has a 10-point grayscale and gamma control, so 5% and the in-between 15/25/…/85% patches are never measured. Slot 100 acts on the 95% patch, so 95% is measured for that slot and 100% is set by two-point high.

Each control receives up to four bounded correction attempts, but stops early as soon as it reaches target or stops improving. Every proposed change is measured before it is accepted. A rejected change is immediately restored. If any setup or calibration stage fails, V4 restores the complete original TV snapshot and the saved Panasonic video LUT.

## Read the result

The console prints:

- average grayscale plus gamma dE2000;
- maximum dE2000 and the IRE where it occurred;
- chroma-only average dE2000;
- median measured gamma.

The complete evidence is saved in a timestamped folder under `sessions`:

- `autocal.log` — every patch, measurement, TV command and accept/restore decision;
- `pre_calibration_snapshot.json` — all original controls;
- `final_snapshot.json` — final controls;
- `autocal_result.json` — starting white, response measurements, solver proposals, decisions and final sweep;
- `effective_config.json` — exact settings used for the run.

## Verify without changing anything

Double-click **Run Read-Only Verification V4.bat**. It performs the same 12-point sweep and writes no calibration values.

## After AutoCal

Use the AVS HD 709 black and white clipping patterns to optimise Brightness and Contrast for the final viewing chain, as Panasonic's CalMAN workflow specifies. Then run the read-only verifier again if those controls changed materially. Confirm the final result in HCFR with the same plasma correction, patch size, range chain, Rec.709/D65 target and 2.4 gamma target.

## Accuracy boundary

V4's mathematics and decision loop are covered by offline tests, including the published CIEDE2000 reference pair and a complete simulated TV calibration. A real dE below 1 is the target, not a promise: it still depends on Spyder5 repeatability, plasma drift, meter correction quality, the unchanged HDMI range chain and the TV's available control resolution.

Run **Run Offline Tests.bat** to execute the hardware-free checks.


## Meter transport

The persistent spotread process uses Argyll's documented ARGYLL_NOT_INTERACTIVE=1 environment setting. Prompt detection handles text without a newline, and each trigger plus newline is sent in one pipe write. A reading completes only after its XYZ result and the next ready prompt arrive. Startup and measurement failures close and reap the owned child process; error messages include the last meter output. Meter initialisation still occurs before picture controls or the video LUT are changed. The first XYZ reading is used directly for two-point correction.

Offline tests include real child-process fixtures for prompt fragmentation, repeated readings, timeouts, USB error output, invalid results and cleanup. These tests cover transport behaviour, not physical calibration accuracy.

## Documentation check (24 September 2026)

- Panasonic's official 60/65/600 guide recommends small plasma windows, two-point first, followed by grayscale/gamma and then CMS. It does not document the proprietary ASCII commands or prescribe our solver's step sizes. https://app.spectracal.com/Documents/QSGs/Panasonic%2060_65_600%20Series%20QuickStart.pdf
- Argyll documents emissive XYZ measurements, Spyder5 CCSS support, and a refresh-rate override. The configured 60 Hz override assumes the source remains at 60 Hz; it is not automatic refresh detection. https://www.argyllcms.com/doc/spotread.html
- Argyll documents prompts without newlines and the Windows single-write input requirement. https://www.argyllcms.com/doc/Environment.html
- Socket framing, handshake, and point selectors were compared with the user's working gt60_control.py and recorded TV replies. Numeric readback now rejects a reply for a different control.
- Window area (6.5%), settle delay (2 s), step sizes and damping remain implementation choices, not values certified by those documents. Final measurements remain necessary to judge the physical result.
