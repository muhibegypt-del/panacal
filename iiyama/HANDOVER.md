# iiyama Vision Master Pro 514: handover for the ChArUco geometry project

This file is the complete brief for a new session. Everything else in this
repository (LG AutoCal, `lgcal/`, `pgen/`, the `LG *.bat` files) is a
different project: ignore it. The only other iiyama-related file is
`crt_low_slider.py` (see section 6).

The goal of the new session: **build "photo mode" ChArUco geometry
measurement for the user's CRT** (section 8), then walk the user through
tightening the geometry round by round (section 9). Live mode (section 10)
comes later and only if photo mode proves itself.

---

## 1. The user and how to work with them

- Hobbyist calibrator with a strong eye; not a programmer, and says maths
  is not their strong suit. **Go slowly, one step at a time, and annotate
  pictures** (they explicitly asked for annotated images; the two in this
  folder, `vlin_which_boxes.png` and `geometry_what_to_fix.png`, worked
  well).
- Wants things done for them, with no configuration. Plain language. Numbers
  in mm and simple percentages, not jargon.
- **Do not reinvent the wheel.** The previous session wasted rounds building a
  gamma-ramp slider tool when the NVIDIA Control Panel already had
  per-channel sliders. Before building anything, check whether an existing
  tool does it, and **prove the simplest version visibly works on their
  setup first**.
- Be honest about what is certain and what is a guess. They push back
  hard ("are you sure?", "what have you not considered?") and value
  candour.
- Keep token use low: analyse photos with code and read short text output;
  view images only when needed. Don't re-derive what this file already says.
- They use Windows; repo is cloned at `C:\LGAutoCal` (they `git pull` there).
  Python 3.13.1 is installed (`py -3` works).

## 2. Hardware and system

| Item | Detail |
|---|---|
| Monitor | **iiyama Vision Master Pro 514**, model **HM204DT**. EDID name in CRU: `IVM216A - HM204DA/DTA`. |
| Tube | 22" Mitsubishi **Diamondtron** (aperture grille, 0.24 mm pitch, ~20" viewable). Grille ≈ 395 mm / 0.24 mm ≈ **1,650 triads across**, so 1920+ wide is beyond the tube's resolution. |
| Scan range | 30–142 kHz horizontal, 50–200 Hz vertical (owners report higher), max 2048×1536, video bandwidth 390 MHz. |
| PC control | Label/spec: **"Plug & Play VESA DDC2B"** only. Research found **no DDC/CI** (no MCCS), so software can't change geometry. Not tested with ControlMyMonitor; the service manual shows the main MCU wired to the DDC lines (pins DDC-SDA/SCL) plus two 24C21 EDID EEPROMs, so it's not 100% ruled out. Treat it as **no PC control of geometry**. |
| GPU | **NVIDIA GT 710**, native VGA output (no adapter). |
| OS | Windows 11 23H2 (10.0.22631). |
| Displays | `\\.\DISPLAY13`: 3840×2160 LCD (main, at 0,0). `\\.\DISPLAY10`: the CRT, 1600×1200, placed at (3840,0). Both report "Generic PnP Monitor". |
| Meter | Datacolor **Spyder5**, used with **HCFR**. |
| Other | **CRU** (Custom Resolution Utility) installed; phone camera available (user will use their phone, no webcam). |

**Main mode: 1600×1200 at 85 Hz, VESA DMT timings** (the user found these
via tinyvga.com and they look sharpest):

| | Active | Front porch | Sync | Back porch | Total | Polarity |
|---|---|---|---|---|---|---|
| H | 1600 | 64 | 192 | 304 | 2160 | + |
| V | 1200 | 1 | 3 | 46 | 1250 | + |

Pixel clock 229.5 MHz, 106.25 kHz, 85.00 Hz. This is exactly the service
manual's **MODE 6**. When the incoming timing matches a factory preset, the
factory menu's bottom line shows **"Fh" with a capital F** (small "f" means
not a preset). That is why VESA timings look sharper: the factory-aligned
settings for that mode are used.

## 3. Service manual facts (the user has the PDF; it is not in the repo)

The user uploaded `iiyama_vision_master_pro514_hm204d_dt.pdf` in the old
session. **Ask them to attach it again if you need page images.** The facts
that matter:

### Adjustment conditions
- Warm up **at least one hour** on a signal (MODE 5), degauss first.
- Brightness at **centre**, contrast at **maximum**, unless stated.
- The factory faces the screen **east** (magnetic field matters): don't
  move the monitor during a session.
- Data saves when the OSD disappears or another signal arrives.

### Timing chart (chapter 2, page 16)
| Mode | Timing | Used for |
|---|---|---|
| 1 | 640×480 @60 | |
| 2 | 640×480 @85 | |
| 3 | 800×600 @85 | |
| 4 | 1024×768 @85 | |
| **5** | **1280×1024 @85** (91.146 kHz) | Almost all factory adjustments (colour, cutoff, distortion 1-12) |
| **6** | **1600×1200 @85** (106.25 kHz) | Focus, anode voltage |
| **7** | **1920×1440 @85** (128.52 kHz) | Vertical linearity (1-9) |
| 8 | 2048×1536 @85 | |

### Factory mode
- Entry without hardware: MENU → **Function → Language**, then:
  Select **Svenska**, press MENU twice → select **English**, MENU twice →
  select **Nederlands**, MENU twice → select **Svenska**, MENU twice →
  select **English**, press MENU once → Factory Mode Menu appears.
  Power off to exit.
- Or: short pins 2 and 4 of the RS connector on PWB-MAIN before power-on
  (required for the automatic colour routine only).
- **Factory menu items** (full list): Contrast, Brightness, V-size,
  V-position, H-size, H-position, Pincushion, Trapezoid, Parallelogram,
  Pinbalance, Sidepin Top, Sidepin Bottom, Pinbalance Top, Pinbalance Btm,
  DBF Para, DBF Phase, V DBF, H moire, V moire, H convergence, Tilt-Dy,
  V linear side, V linear corner, Red gain, Temp cont, Blue gain, rrc,
  V-conver, Bottom-right, Top-right, Top-left, Bottom-left, CRT check,
  DA TEST 1/2/3.
- Bottom line of the factory OSD: `Fh**.* Fv**` (F capital = factory
  preset timing), refresh, video input, adjustment range boxes.
- The four corner items (Top-left … Bottom-right) and rrc are **colour
  purity/landing** corrections, not geometry. DBF Para/Phase/V DBF are
  **dynamic focus uniformity**, not geometry.
- **Photograph every factory menu page before changing anything.**

### Geometry procedure and tolerances
- **1-9 V-LIN** (MODE 7 in the manual; the user did it at 1600×1200):
  set V-size 295±4 mm; **V linear corner** so top and bottom squares
  differ ≤ 0.5 mm; **V linear side** so top, middle, bottom squares are
  about equal.
- **1-11 Tilt** (Tilt-Dy): |X| ≤ 0.5 mm.
- **1-12** (MODE 5, criteria): H-size **395±4 mm**, V-size **295±4 mm**,
  H-position |A−B| < 4 mm, V-position |C−D| < 4 mm; side distortion
  |X| ≤ **0.5 mm per 30 mm** (pincushion, trapezoid, pinbalance,
  parallelogram, sidepin top/bottom, pinbalance top/bottom).
- **1-13** repeated for **every preset mode**, but only **size, position,
  pincushion and trapezoid** ("No other adjustment items for distortion
  than the above should be adjusted"). Inference used so far: **V-lin,
  sidepin/pinbalance top/bottom, parallelogram, pinbalance, tilt,
  convergence and moiré are shared by all modes**; size, position,
  pincushion, trapezoid are **per mode**. The user manual confirms
  parallelogram, convergence, moiré (and colour) apply to all timings.
  Verify by checking another mode after adjusting.
- **1-24 Focus**: FOCUS-A (horizontal lines) and FOCUS-B (vertical lines)
  pots on the flyback (T501), at MODE 6, green crosshatch. Inside the case
  near 27 kV: only with a plastic tool, one hand; the user hasn't done it.
- **1-25** luminance uniformity ≤ 22.5 cd/m² difference.

### User-menu controls (user manual)
- Screen: H-Size, V-Size, H-Position, V-Position.
- Control: Pin-Cushion, Trapezoid, Parallelogram, Pin-Balance, Tilt (and
  "Shape").
- H-Convergence, V-Convergence, H-Moire, Gamma Correction (midtones), Degauss.
- Colour: Contrast, Colour Temp (≈4500–10000K, factory 9300K, plus sRGB
  ≈6500K which **locks** Contrast, Brightness, Red, Blue, OPQ and Gamma
  Correction), Red and Blue gains per colour temperature, OPQ, Brightness.

## 4. Geometry: where things stand

- Done: **V-size 295 mm exactly**; **vertical linearity** fixed (the user
  adjusted V linear side then V linear corner until top, middle and bottom
  squares matched, around 24.6 mm each).
- Not done: sides and corners. The user says the geometry is "nuked" and
  "weirdly bad" but **no photo has been received yet**. Unknown whether it's
  pincushion/corner geometry, convergence, purity, or something magnetic.
- Not yet collected: the **current values of every user-menu and
  factory-menu geometry setting**. The user offered to send them; **ask for
  photos of each menu page first**.
- Guidance images made: `vlin_which_boxes.png` (which squares to measure)
  and `geometry_what_to_fix.png` (8 distortion types → control, work 1→8:
  pincushion, trapezoid, parallelogram, pin-balance, sidepin top, sidepin
  bottom, pinbalance top, pinbalance btm).
- Patterns in `patterns/`: `crosshatch_1600x1200.png` (16×12 grid, centre
  cross, circle, 1 px lines, edge lines on the outermost pixels),
  `crosshatch_1920x1440.png`, `crt_warmup_50pct_grey.png` (RGB 128 warm-up),
  `iiyama_16_step_greyscale.png` (black left → white right, codes
  0,17,…,255). Show fullscreen at 1:1 with Windows Photos (F11); Windows
  display scaling for the CRT must be 100%.
- Why lower resolutions looked sharper: 1920 wide exceeds the grille
  (~1,650 triads), and focus/convergence are factory-set at particular scan
  rates. **1600×1200 @85 is the recommended main mode.**

## 5. Colour/greyscale state (context only; not this session's job)

- Black level: Brightness set so the 16-step bar 2 (RGB 17) is barely
  visible and black invisible (manual step 1-16). It was 43 at centre-ish
  and was then **bumped up a little** to lift shadows: **ask the user for
  the current value**. G2 (SCREEN pot) was not touched.
- White: OSD Red/Blue gains adjusted; white ≈ x 0.315, y 0.329, dE ~1.2–1.4,
  about 104–109 cd/m².
- The tube's **cutoffs have drifted**: green switches on earlier than red and
  blue, so dark greys were green (at 30%: R 70%, G 112%, B 75% before
  correction). The factory fix is the automatic cutoff routine (1-15) that
  needs a Minolta CA-100 colour analyser (see appendix A).
- Software plaster that works: **NVIDIA Control Panel → Display → Adjust
  desktop colour settings → the 1600×1200 display → "Use NVIDIA settings"
  → Colour channel Red Brightness +2, Blue Brightness +5** (from 50%).
  Result: average dE 1.14–1.48, tint gone from ~25% up.
- Remaining: shadows too dark (gamma ≈ 2.9 at 10%, 2.55 at 30%); the next
  step suggested was the OSD **Gamma Correction**, one step at a time
  towards brighter midtones, checking 20/30/50% and black each time.
  Slight magenta at 70–95% (y ≈ 0.324); possible fix: NVIDIA per-channel
  Contrast −1 on red and blue.
- HCFR settings used: sRGB, D65, **power-law gamma 2.2 (was wrongly L\*)**,
  Spyder5 in CRT/refresh mode, 10–20% window.

## 6. `crt_low_slider.py` (repo root): status

A tkinter tool that writes per-channel low-end trims through
`SetDeviceGammaRamp`, with guard rails, a banding meter, a diagnose mode
(`CRT Low Slider - Diagnose.bat`). The diagnose run showed Windows
accepting and reading back ramps on both displays, but **nothing visibly
changed on the CRT**, most likely because the NVIDIA Control Panel was set
to "Use NVIDIA settings" (which the user now relies on). **It is not in use.**
The user may want it removed; ask before deleting. Do not mix it with the
NVIDIA per-channel settings.

## 7. Photo protocol (agreed with the user)

- One hour warm-up, degauss once at the start, don't move the monitor or
  bring magnets, speakers or phones near it during the session.
- Pattern fullscreen at 1600×1200 @85, 1:1.
- Photo **from the user's normal seat, at eye height, centred**, with the
  whole picture and bezel in frame. Normal 1× camera (or slight zoom from
  further back), **not** ultra-wide, night or HDR mode; tap to focus;
  lower the exposure so lines aren't blown out; hold still or brace the
  phone. The user will use their phone (no tripod mentioned; suggest
  bracing it in the same place each round).
- Known pitfalls to handle: phone lens distortion (calibrate on the 4K LCD),
  glass-thickness parallax at the edges (shoot from the seat so it's right
  for their view), moiré between grille and sensor, phone processing smear,
  bezel not square to the tube, warm-up drift, coarse OSD steps,
  purity/convergence being mistaken for geometry, per-mode storage.

## 8. The build: ChArUco photo mode

### Why ChArUco
A ChArUco board (checkerboard with ArUco markers in the white squares, as
used for OpenCV camera calibration) gives **self-identifying corners with
~0.1 px accuracy**. Partial views, glare and warped edges don't break it,
and every detected corner has a known ideal screen position. Compare
detected with ideal → a dense error map of the whole screen.

### Patterns to generate (exact 1600×1200, pixel-accurate)
- A ChArUco board that fills the **whole active area to the edges**:
  geometry errors live at the edges and corners. For example 32×24 squares
  of 50 px (or 40 px squares, 40×30), `DICT_5X5_1000` or `DICT_4X4_1000`,
  marker ratio ~0.7. Check the detection rate in synthetic tests and pick
  the size that detects reliably. Add a 1 px outline on the outermost
  pixels so the true edge can be measured too.
- Consider **reduced white level** (e.g. 60–70% grey instead of 255) to
  limit bloom and ABL on the CRT; test in synthetic renders with blur.
- **Red-only, green-only, blue-only** versions for convergence (per-gun
  maps, later).
- An **LCD version** for camera calibration: the same board shown
  fullscreen on the 3840×2160 LCD (scaled board, known geometry).
- Save patterns in `iiyama/patterns/`; the user opens them with Photos + F11.

### Analyser (runs here in the sandbox on photos the user uploads)
1. **Camera model**: from the LCD photos (ideally ~8–10 views at varied
   angles, same zoom as the CRT shots), run `cv2.aruco` ChArUco detection
   and `calibrateCamera` to get intrinsics and radial/tangential distortion.
   Undistort every CRT photo with it. If only one frontal LCD photo is
   available, fit a homography plus radial k1/k2 to it.
2. **Detect** ChArUco corners on the CRT photo (`cv2.aruco.CharucoDetector`,
   OpenCV ≥ 4.7; `pip install opencv-contrib-python-headless` here).
3. **Metric reference for the glass plane.** Without one, trapezoid, tilt
   and parallelogram can't be separated from camera pose. Options, best
   first:
   - **Four printed ArUco markers taped to the bezel corners**, with their
     centre-to-centre spacing measured once with a tape measure → true
     rectification of the screen plane in mm.
   - The bezel's inner edge fitted as a rectangle (weaker: rounded,
     blurry, may not be square to the tube).
   - A **plumb line** (thread + small weight) in front of the screen gives
     true vertical for tilt.

   Pincushion, pin-balance and corner bows don't depend on this reference,
   so they are the most reliable measurements.
4. **Decompose** the displacement field (detected minus ideal, in mm) by
   least squares on basis functions that match the controls:
   position (constant), size (linear in x/y), tilt (rotation), parallelogram
   (shear, x ∝ y), trapezoid (width ∝ y), pincushion (x ∝ x·y²),
   pin-balance (x ∝ y², same direction both sides), sidepin top/bottom
   (x ∝ x·y³ or higher order on one half), pinbalance top/bottom (x ∝ y³ on
   one half), V-lin (y ∝ y³ and odd terms). Report each in mm at the screen
   edge, next to the manual's tolerance.
5. **Learn step sizes**: the first time a control is used, ask for a fixed
   change (e.g. +5 steps) and compare photos → mm per step (a Jacobian).
   After that, say exactly how many steps. Re-estimate if predictions miss.
6. **Output per round**: an annotated image (exaggerated error arrows, edge
   bows drawn) plus **one instruction**: the largest error first, in the
   manual's order (size/position roughly → V-lin → tilt → pincushion →
   trapezoid → parallelogram → pin-balance → sidepin top/bottom →
   pinbalance top/bottom → final size 395×295 and centring → second pass →
   convergence). Stop at the tolerances in section 3, or when one OSD step
   exceeds the remaining error.
7. **Reject bad photos** (angle too steep, blur, missing corners, moiré) with
   a clear reason instead of giving a wrong correction.

### Validate before the user touches anything
Generate synthetic "photos": warp the board with known pincushion,
trapezoid, corner flare etc., add a random camera pose, lens distortion,
blur, bloom and noise. Check the analyser recovers the known values within
~0.5 mm. Put the analyser in `iiyama/` with offline tests in `tests/`.

### First messages to the user
1. Ask for photos of **both menus' current values** (user + factory geometry
   pages), and the current Brightness value.
2. Send the ChArUco board(s) and the photo protocol.
3. Ask for the **LCD calibration photo(s)** and the first **CRT photo**, taken
   from the same seat and zoom.

## 9. Round-by-round workflow (what the user experiences)
User shows the board → one photo → analyser → annotated image + "Pincushion:
sides bow in 3.2 mm → press + about 4 steps" → user adjusts → next photo.
Expect 6–10 rounds. Keep each reply short: the image, the one instruction,
and a one-line status of what is already within tolerance.

## 10. Later: live mode (only after photo mode works)
Phone as webcam (DroidCam, free) → a Python + OpenCV program on the PC shows
the board on the CRT, measures continuously and **draws the guidance on the
CRT itself** (arrows, "Pincushion +3"), like a spirit level. Needs
`opencv-contrib-python` on the user's PC. Estimated as 2–3× the photo-mode
build. Other ideas discussed: structured light (Gray code + phase shift
filmed as a video with a frame-number code) for a dense per-pixel map;
colour-separated boards for convergence; an A3 transparency overlay with
moiré for a quick visual check.

---

## Appendix A: CA-100 emulator (deferred hardware fix for the cutoffs)

The factory's **1-15 automatic colour adjustment** runs on the monitor's own
MCU and only needs a Minolta CA-100 to report readings over serial:
- Enter factory mode with the **short connector (pins 2–4 of the RS
  connector on PWB-MAIN)**. Wiring: monitor RS pin 1 → analyser pin 3,
  pin 3 → analyser pin 1, pin 5 → pin 2 (GND). The "interface adapter" is
  probably a level shifter (RS-232 ↔ 5 V TTL) that also holds CTS active.
  **Measure the RS pins with a multimeter before connecting anything.**
- CA-100 serial: **9600 baud, 7 data bits, even parity, 2 stop bits, CR line
  ending**; in remote mode it sends each reading unprompted as
  `P1 310;330; 150` (x = 0.310, y = 0.330, Y = 150 cd/m²). Commands:
  `F1` remote on, `M0` xyLv, `I` zero-cal, `Z` status (CA-100Plus manual,
  CA-100 compatible mode).
- Procedure: white window MODE 5, R/G/B outputs off, analyser remote on,
  adjust the **SCREEN (G2) pot** on the flyback until the back raster reads
  **0.3–0.4 cd/m²**, which starts the auto cutoff; then R/G/B on → auto
  colour temperature (CT1 9300K target x 0.283±0.008, y 0.297±0.008) and
  contrast limit (140±8 cd/m²).
- Plan: a Python emulator (USB-TTL adapter, £5–15) that answers as a CA-100
  using the Spyder5 through Argyll `spotread`. Risk: Spyder5 takes ~20 s per
  reading at 0.4 cd/m²; the routine may time out.
- Decision so far: the user chose the NVIDIA software plaster instead; the
  emulator stays deferred.
