# AutoCal research: what CalMAN and calibrators do, and what we may have missed

Researched 24 September 2026 from Portrait Displays (CalMAN) guides, the PVA and AVS Forum calibrator threads on the 2013 Panasonic plasmas, Light Illusion's calibration guide, ChromaPure/CurtPalme meter tests and TFTCentral. Sources are listed at the end. Forum posts are practitioners' reports, not specifications; they are labelled as such.

## Summary

| # | Finding | Our status | Suggested action | Priority |
|---|---|---|---|---|
| 1 | The Spyder5 stops giving consistent readings below about **0.38 cd/m²**. Your 10% patch should be about 0.44 cd/m². | 10% is measured and corrected as if the readings were as reliable as the brighter levels. | Treat 10% as low-confidence, or use a better meter (i1Display Pro reads to about 0.0025 cd/m² and is about twice as fast). | High |
| 2 | 2013 Panasonics process **static, low-APL** patterns differently (black enhancement, ABL). | We draw a static 6.5% window on a black background, which is the lowest possible APL. | A/B test a constant-APL surround (about 20% grey) at 10–30% before deciding. | High |
| 3 | **Raising** two-point gains can clip a channel at 100%; the standard rule is to reduce gains and raise cuts. | The solver moves red and blue either way. Only luminance headroom is checked, at the end. | Limit two-point high to reductions (allow green too), two-point low to increases, or add a clipping check. | Medium |
| 4 | **Warm-up**: SpectraCal waits until luminance changes by less than 0.1% per minute, which takes 40–70 minutes on their displays. | README says 30–60 minutes; nothing is measured. | Optional warm-up gate: read white 3 times over about 2 minutes and warn if it is still drifting. | Medium |
| 5 | Meter **correction** matters more than the algorithm. TFTCentral measured grayscale 1.6 dE average with raw readings versus 0.5 with the right correction. | Generic `PlasmaFamily_20Jul12.ccss` for a Spyder5. | Profile the Spyder5 against a spectrometer, or use a meter with a plasma correction. | Medium |
| 6 | **Hidden PC processing**: AMD temporal dithering, Windows Auto Color Management, Night light, HDR. | The video LUT is linearised, but none of these are checked. | Add them to the setup checklist; keep 8 bpc RGB Full. | Medium |
| 7 | **Panel Brightness High** crushes whites above about 97–98% and shifts the top of the 10-point controls. | `PC:PBR` is in the snapshot but not checked. It is Low on your TV. | Warn at start-up if Panel Brightness is High. | Low |
| 8 | Calibrators re-check after the panel has **rested**, then touch up. | Only one verification, straight after calibration. | Run the read-only verifier again after 15–30 minutes of normal viewing. | Low |
| 9 | After calibration, set **Adjustment Lock** back on. The CalMAN connection procedure turns it off. | Not mentioned. | Add to the README. | Low |

## What we already match

- **Slot 100 acts on 95%.** A PVA calibrator (Chad B, 2014) independently reports that on the VT/ZT60 "the 100% control actually effects 95%", and that measuring only at 10% steps hides a luminance peak at 95%. We found the same on your VT60 (3.2× more effect at 95%) and correct slot 100 at 95%.
- **Gains at 100% only.** The same post: "Set the RGB gains by minimizing dE at 100% – do not use 80%… ONLY worry about 100% with the gains." Our two-point high corrects at 100%.
- **Cuts mild or not at all.** He suggests leaving the cuts alone, or using 10% with mild changes. We use 20%, because a 10% patch on your TV is at the Spyder5's floor (finding 1). Our cap of 3 steps keeps changes mild.
- **Target.** He runs CalMAN's 10-point AutoCal with a dE2000 target of 0.4. Our stop target of 0.0005 u′v′ is about 0.45 dE2000 at white.
- **Small windows.** Panasonic's CalMAN guide for the 60 series says to use the smallest window the meter can read, because of plasma power limiting. We use 6.5%.
- **Leave green alone, adjust red and blue.** This is the common rule (Light Illusion; AVS LG thread). We adjust only red and blue.
- **Brightness and Contrast before calibrating**, gamma preset 2.4. This matches both the PVA procedure and our README.

## 1. The Spyder5 is at its limit at 10%

ChromaPure's tests (CurtPalme) stepped down one 8-bit code at a time and found the Spyder5 stops returning consistent luminance at **0.38 cd/m²**. The SpyderX reaches 0.028 and the i1Display Pro about 0.0025. For white at about 105 cd/m² and gamma 2.4, your patches should measure:

| Patch | Code | Expected |
|---|---|---|
| 5% | 13 | 0.08 cd/m² (below the Spyder5's floor) |
| 10% | 26 | 0.44 cd/m² (at the floor) |
| 20% | 51 | 2.2 cd/m² (fine) |

The same test found the Spyder5's grayscale colour error was concentrated at 5%, and that from 10% upwards it was "slightly more accurate than the SpyderX". Speed was 231 s for 95 readings (2.4 s each), which matches our logs.

What this means for us:
- The 10% correction and its "within noise" stop are working at the meter's limit. The report should flag 10% as low-confidence rather than treat it like the brighter levels.
- The black preflight check cannot see a correct VT60 black. It can only see a clearly raised one, such as the 0.446 cd/m² at Brightness +30, which is still a useful guard.
- An i1Display Pro (which also has a factory plasma correction) would make 10% trustworthy and roughly halve the run time, because the dark readings dominate our timing.

## 2. Static, low-APL patterns on 2013 Panasonics

- D-Nice (DeWayne Davis), who set up the 2013 Panasonic settings threads, wrote: "static pluge patterns are invalid on these displays… per the engineers who designed them". Another poster pointed out that the disc PLUGE patterns are 24 Hz video, not static images.
- On a 2010 Panasonic plasma (AVS thread), plain windows and constant-APL windows gave different gamma. The difference was attributed to an algorithm that darkens blacks on low-APL scenes. White balance stayed consistent.
- On a VT30 (AVS thread), a 100% white window measuring 35 fL dropped to 17–18 fL full-field, and the measured gamma moved between about 2.07 and 2.22 depending on window size and APL.
- The PVA calibrator's VT60 results were taken with "6% size 22% APL windows". Other calibrators use plain windows.

Our pattern is a static window on pure black, the lowest APL possible. Because we only correct white balance, the risk is smaller than for gamma. But near-black processing could still shift the tint at 10–20%. Suggested test: add an optional grey surround (for example 20%) to the pattern host, then run the read-only verifier with and without it. If 10–30% tint differs by more than the meter noise, calibrate with the surround.

## 3. Gain clipping and cut crushing

Light Illusion's manual calibration guide gives two rules:
- "RGB values should be reduced, never increased, to prevent potential clipping issues at 100%… always double and triple check with the Contrast Test Pattern."
- "When adjusting RGB Low values the rule of thumb is to only raise the necessary RGB values… This is to prevent 0% blacks crushing."

Our integer search can raise HIR or HIB. If a channel clips, the 95→100% headroom guard only catches a luminance flatline, not a colour channel that has stopped rising. Options, simplest first:
1. Only allow two-point high moves that do not raise a gain above its starting value, and add green as a third control so tint can still be corrected by lowering the other two.
2. Only allow two-point low moves that raise the cuts.
3. After two-point high, measure 95% and 100% and check that each of X, Y and Z rises. A flat channel means clipping.

## 4. Warm-up gate

SpectraCal (Portrait Displays) will not calibrate until the reference display's luminance changes by less than 0.1% per minute. That took 40–70 minutes on the LCD, LED and CRT they measured, and they wait 60. For plasma, an AVS thread reports luminance and white balance stabilising over about half an hour. In your session 20260924_185416, two readings of 100% white at identical settings differed by 0.4% within 20 seconds.

A cheap gate: at start-up, read white three times about 40 seconds apart (about 10 seconds of meter time) and warn if the change is more than 0.1% per minute. You could then wait or continue.

## 5. Meter correction

TFTCentral calibrated an LG OLED with CalMAN AutoCal and an i1Display Pro. Using raw readings gave grayscale 1.6 dE average (2.4 max); using the correct correction mode gave 0.5 average (1.0 max). The algorithm was the same in both cases; only the meter correction differed. We use Argyll's generic plasma CCSS. It is right for the display type but not for your individual Spyder5. Profiling the Spyder5 against a spectrometer for this TV, or using a meter with a plasma correction, is likely to improve real accuracy more than further algorithm changes.

## 6. Hidden processing in the PC signal chain

- AMD GPUs have long applied temporal dithering. Calibrators using a PC as the pattern source report outputting 10 or 12 bit to avoid it (guru3D). At 8 bpc, full-range RGB, an 8-bit patch should pass unchanged, but this is worth confirming.
- Windows 11 Auto Color Management can clamp or convert colours per app (DisplayCAL forum). Night light and HDR also change the output. Our linear video LUT does not undo any of these.
- A range mismatch between the GPU, Windows and the TV's HDMI RGB Range / Black Level settings crushes or lifts the ends (Simple Home Cinema, on HCFR's own patterns). Your range sweep covered this; the TV-side settings belong in the checklist.

Suggested checklist addition: HDR off, Night light off, Auto Color Management off for the Panasonic, AMD colour depth 8 bpc, pixel format RGB 4:4:4 Full, and the TV's HDMI range matching the sweep.

## 7–9. Smaller items

- **Panel Brightness.** In the PVA report, High crushed everything above 97–98% unless Contrast was very low. Commenters recommended Low or Mid. AutoCal already reads `PC:PBR` and could warn.
- **Re-check after rest.** The PVA procedure ends with "give the display a little break and then take another pass". Run the read-only verifier again later and compare.
- **Lock after calibrating.** Panasonic's CalMAN connection procedure sets Adjustment Lock to Off. Turn it back on after calibrating so the settings are not changed by accident.

## Follow-up: PC signal chain and measurement noise

### AMD dithering is harmless with a linear LUT

AMD applies the video card LUT at more than 8 bits and dithers the result down to the link depth (DisplayCAL forum, Photography Life). Dithering only changes a pixel when the value arriving at it is not an exact 8-bit code. With the LUT reset to linear (`dispwin -c`), 8 bpc RGB 4:4:4 Full output, native 1920x1080 and no driver colour adjustments, every value is an exact code and dithering changes nothing. On the DisplayCAL forum, the LUT's effect is said to disappear once it is reset to linear ("input = output"). AMD's registry keys (`TMDS_DisableDither` for HDMI/DVI, `DP_DisableDither` for DisplayPort, per VPixx) are only needed if something upstream is not an identity.

Things that do break exactness, and must stay off: YCbCr output (the RGB to YCbCr conversion is not bit-exact), limited range generated by the GPU, AMD Custom Color / Color Temperature Control, Windows Auto Color Management, Night light, HDR, and GPU scaling (a non-native resolution). A cheap safeguard: after `dispwin -c`, read the LUT back with `dispwin -s` and check it is exactly linear.

### Real measurement noise is much larger at 20–70% than at the top

Pairs of readings at identical settings in session 20260924_185416 (before and after each 6-step probe, same level):

| Level | Same-setting difference (u′v′) | 6-step probe effect | Signal/noise |
|---|---|---|---|
| 100% (two-point) | 0.0000–0.0004 | 0.0026–0.0053 | 6–107 |
| 95% | 0.0003 | 0.0010–0.0022 | 4–9 |
| 80–90% | 0.0001–0.0004 | 0.0001–0.0013 | 0.5–8 |
| 70% | 0.0009 | 0.0002–0.0003 | 0.2–0.3 |
| 60% | 0.0011–0.0028 (one outlier 0.048) | 0.0004–0.0007 | 0.2–0.6 |
| 50% | 0.0006–0.0024 | 0.0003–0.0009 | 0.1–1.4 |
| 40% | 0.0001–0.0016 | 0.0010–0.0019 | 0.6–13 |
| 30% | 0.0024–0.0031 | 0.0006–0.0020 | 0.2–0.8 |
| 20% | 0.0010–0.0017 | 0.0012–0.0014 | 0.7–1.5 |
| 10% | 0.0005–0.0010 | 0.0000–0.0004 | 0–0.8 |

Luminance at those levels was steady to about 0.5%, and 30–60% is 8–35 cd/m², far above the Spyder5's 0.38 cd/m² floor. So the chromaticity scatter is not a lack of light. It may come from the plasma's temporal dithering at intermediate levels, a beat between that and the meter's integration, or slow control settling. This is not yet established; a repeatability test (repeated readings at 30%, 60% and 100% with nothing changed) would tell them apart.

The simulator's "realistic" meter used roughly a tenth of this noise at mid levels. Re-running the simulated calibration with the measured noise profile (10 seeds each):

| Variant | True average tint | True worst tint | Readings |
|---|---|---|---|
| Perfect meter | 0.00022 | 0.00036 | – |
| Current code, measured noise | 0.00055 | 0.00116 | 105 |
| Probe step 12 instead of 6 | 0.00052 | 0.00114 | 104 |
| 3 reads at every level | 0.00049 | 0.00109 | 141 |
| 6 detail iterations instead of 4 | 0.00055 | 0.00116 | 105 |
| Update the model only when the change beats 2× noise | 0.00058 | 0.00119 | 108 |

No algorithm change moves the result much. At this noise level, accuracy is limited by the measurement. Also checked: reusing an accepted reading does not make the stop decision optimistic. Noise makes a tint read higher than the truth on average (+0.00015 here), so the stop rule errs on the cautious side.

## Not relevant to this project

- CMS and saturation sweeps. The PVA procedure does CMS between two 10-point passes; we do not touch CMS.
- 10-point gamma. The PVA post describes 10-point gamma values of -23 to -40 on the VT60 and a luminance peak at 95%. We dropped gamma, so this only matters if gamma is added back.
- Observer metamerism (CIE 1931 versus newer observers). It is mainly discussed for WOLED, QD-OLED and narrow-band LED displays. We found no plasma-specific evidence.
- D-Nice's 100-hour panel prep. It applies to new panels, not a VT60 that has been in use for years.

## Sources

- Panasonic / SpectraCal, *CalMAN Setup Guide – Panasonic VT60/ZT60/GT60…* – https://app.spectracal.com/Documents/QSGs/Panasonic%2060_65_600%20Series%20QuickStart.pdf
- PVA forum, *Calibrating VT/ZT60 in panel brightness high* (Chad B, 2014; D-Nice quote) – https://pva.tv/forums/topic/calibrating-vt-zt60-in-panel-brightness-high/
- AVS Forum, *Windows vs APL windows gives different gamma for my Panasonic plasma* (2010) – https://www.avsforum.com/threads/windows-vs-apl-windows-gives-different-gamma-for-my-panasonic-plasma.1278420/
- AVS Forum, *Grayscale variances – different patterns, plasma calibration* (2012) – https://www.avsforum.com/threads/grayscale-variances-different-patterns-plasma-calibration.1441407/
- AVS Forum, *Do plasmas need to warm up?* – https://www.avsforum.com/threads/do-plasmas-need-to-warm-up.1157130/
- Portrait Displays, *Display Warm Up Rates* – https://www.portrait.com/resource-center/display-warm-up-rates/
- Portrait Displays, *Panasonic (2019/2020) Internal Pattern Generator* – https://www.portrait.com/resource-center/panasonic-2019-internal-pattern-generator/
- CurtPalme / ChromaPure, *Datacolor SpyderX with ChromaPure* (Spyder5 low-light limit, speed, repeatability) – https://www.curtpalme.com/ChromaPure_SpyderX.shtm
- Light Illusion, *Manual Calibration Guide* – https://lightillusion.com/manual_calibration_guide.html
- TFTCentral, *LG OLED Calman AutoCal – Testing Meter Mode Profiles and Accuracy* – https://tftcentral.co.uk/articles/lg-oled-calman-autocal-calibration-testing-meter-mode-profiles-and-accuracy
- guru3D, *Disabling display output dithering on Windows* – https://forums.guru3d.com/threads/disabling-display-output-dithering-on-windows.447625/
- DisplayCAL forum, *Windows – Auto Color Management (ACM)* – https://hub.displaycal.net/forums/topic/windows-auto-color-management-acm/page/3/
- Argyll CMS, *spotread* – https://www.argyllcms.com/doc/spotread.html
