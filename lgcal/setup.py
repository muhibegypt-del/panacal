"""Find, or fetch once, everything the run needs: Perl, ArgyllCMS and an
OLED meter correction. Nothing has to be configured by hand; anything found
is remembered in data/setup.json.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
CACHE = ROOT / "data" / "setup.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PERL_RELEASES = "https://strawberryperl.com/releases.json"
ARGYLL_PAGE = "https://www.argyllcms.com/downloadwin.html"


def load_cache() -> dict:
    """Remembered tool paths. Missing or damaged just means "search again"."""
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(data: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(data, indent=1), encoding="utf-8")


def progress_step(done: int, total: int) -> int:
    """Progress bucket: tens of percent with a known size, else every 10 MB."""
    return done * 10 // total if total > 0 else done // (10 << 20)


def download(url: str, target: Path, say, sha256=()) -> None:
    """Download to target.part, check the SHA-256 when given (one value or
    several accepted values), then rename. A failed or interrupted download
    leaves nothing behind."""
    accepted = {sha256.lower()} if isinstance(sha256, str) and sha256 else {s.lower() for s in sha256 or ()}
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done, shown = 0, 0
            while chunk := response.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress_step(done, total) > shown:
                    shown = progress_step(done, total)
                    say(f"  {target.name}: {done * 100 // total}%" if total else
                        f"  {target.name}: {done >> 20} MB")
        if accepted and digest.hexdigest().lower() not in accepted:
            raise SystemExit(f"The download of {target.name} was damaged (checksum mismatch). Run again.")
        os.replace(partial, target)
    except OSError as exc:
        raise SystemExit(f"Could not download {target.name} ({exc}). Check the internet connection "
                         "and run again.") from None
    finally:
        partial.unlink(missing_ok=True)


def extract(archive: Path, target: Path, say) -> None:
    """Unzip, removing a half-written folder if it fails."""
    say("  unpacking ...")
    try:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(target)
    except (OSError, zipfile.BadZipFile) as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise SystemExit(f"Could not unpack {archive.name} ({exc}). Run again.") from None
    finally:
        archive.unlink(missing_ok=True)


# --- Perl -------------------------------------------------------------------

def perl_problem(perl: str) -> str:
    """Why this Perl cannot run the helper, or "" if it can: it must be a
    native Windows Perl (not Git's msys Perl) with IO::Socket::SSL, JSON::PP
    and Digest::SHA."""
    try:
        check = subprocess.run(
            [perl, "-MIO::Socket::SSL", "-MJSON::PP", "-MDigest::SHA", "-e", "print $^O"],
            capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"does not start ({exc})"
    if check.returncode != 0:
        missing = re.search(r"Can't locate (\S+)", check.stderr or "")
        return f"lacks {missing.group(1)}" if missing else "cannot load its SSL/JSON modules"
    if check.stdout.strip() not in ("MSWin32", "linux", "darwin"):
        return f"is a {check.stdout.strip() or 'non-native'} Perl, not native Windows Perl"
    return ""


def perl_ok(perl: str) -> bool:
    return perl_problem(perl) == ""


def perl_candidates(settings: dict, cache: dict) -> list[str]:
    found = [settings.get("perl"), cache.get("perl"),
             str(TOOLS / "strawberry" / "perl" / "bin" / "perl.exe"),
             r"C:\Strawberry\perl\bin\perl.exe", shutil.which("perl")]
    return [p for p in found if p and (Path(p).is_file() or shutil.which(p))]


def portable_zip(releases) -> tuple[str, set]:
    """(url, accepted sha256 values) of the newest 64-bit portable Strawberry
    Perl in releases.json, or ("", set()).

    releases.json shuffles its entries: in the 5.40.5.1 release the portable
    zip's URL sits beside the PDL zip's checksum, and the zip's real checksum
    sits beside the MSI's URL. So the file name picks the URL, and the
    download must match one of the checksums published for that release (a
    damaged download matches none of them)."""
    for release in releases if isinstance(releases, list) else []:
        if not isinstance(release, dict) or not str(release.get("archname", "")).startswith("MSWin32-x64"):
            continue
        entries = [e for e in (release.get("edition") or {}).values() if isinstance(e, dict)]
        for entry in entries:
            url = str(entry.get("url", ""))
            if url.endswith("-64bit-portable.zip"):
                return url, {str(e["sha256"]).lower() for e in entries if e.get("sha256")}
    return "", set()


def install_perl(say) -> str:
    """Strawberry Perl portable into tools/ (no installer, no admin rights)."""
    say("Downloading Strawberry Perl (one time, about 300 MB) ...")
    try:
        with urllib.request.urlopen(PERL_RELEASES, timeout=60) as response:
            releases = json.load(response)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Could not reach strawberryperl.com ({exc}). Check the internet connection "
                         "and run again.") from None
    url, sha256 = portable_zip(releases)
    if not url:
        raise SystemExit("strawberryperl.com lists no portable 64-bit Perl. Install Strawberry Perl "
                         "from strawberryperl.com, then run again.")
    archive = TOOLS / url.rsplit("/", 1)[-1]
    download(url, archive, say, sha256)
    target = TOOLS / "strawberry"
    shutil.rmtree(target, ignore_errors=True)
    extract(archive, target, say)
    return str(target / "perl" / "bin" / "perl.exe")


def find_perl(settings: dict, say) -> str:
    cache = load_cache()
    if settings.get("perl"):
        problem = perl_problem(settings["perl"])
        if problem:
            raise SystemExit(f"The Perl set in settings.json ({settings['perl']}) {problem}. Remove "
                             "\"perl\" from settings.json to use or fetch Strawberry Perl.")
        return settings["perl"]
    rejected = []
    for perl in perl_candidates({}, cache):
        problem = perl_problem(perl)
        if not problem:
            if cache.get("perl") != perl:
                cache["perl"] = perl
                save_cache(cache)
            return perl
        rejected.append(f"{perl} {problem}")
    say("No usable Perl found" + (": " + "; ".join(rejected) if rejected else "") + ".")
    perl = install_perl(say)
    problem = perl_problem(perl)
    if problem:
        raise SystemExit(f"The downloaded Perl at {perl} {problem}.")
    cache["perl"] = perl
    save_cache(cache)
    return perl


# --- ArgyllCMS --------------------------------------------------------------

def version_key(path: str) -> tuple:
    match = re.search(r"Argyll_V(\d+)\.(\d+)\.(\d+)", path, re.I)
    return tuple(int(v) for v in match.groups()) if match else (0, 0, 0)


def argyll_candidates(settings: dict, cache: dict) -> list[str]:
    bins = [settings.get("argyll_bin"), cache.get("argyll_bin")]
    spotread = shutil.which("spotread")
    if spotread:
        bins.append(str(Path(spotread).parent))
    patterns = [str(TOOLS / "Argyll*" / "bin"), r"C:\Argyll*\bin", r"C:\Argyll*\*\bin",
                r"C:\Program Files\Argyll*\bin", r"C:\Program Files\ArgyllCMS*\bin",
                r"C:\Program Files (x86)\Argyll*\bin"]
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            patterns.append(str(Path(base) / "DisplayCAL" / "dl" / "Argyll*" / "bin"))
    for pattern in patterns:
        bins += sorted(glob.glob(pattern), key=version_key, reverse=True)
    return [b for b in bins if b and any((Path(b) / name).is_file() for name in ("spotread.exe", "spotread"))]


def latest_argyll_link(page: str) -> str:
    """Newest 64-bit Windows zip linked from argyllcms.com/downloadwin.html."""
    links = re.findall(r'https?://[^"\'\s>]*Argyll_V[\d.]+_win64_exe\.zip', page or "")
    return sorted(set(links), key=version_key)[-1] if links else ""


def install_argyll(say) -> str:
    say("ArgyllCMS is not installed. Downloading it (one time, about 15 MB) ...")
    try:
        with urllib.request.urlopen(ARGYLL_PAGE, timeout=60) as response:
            page = response.read().decode("latin-1")
    except OSError as exc:
        raise SystemExit(f"Could not reach argyllcms.com ({exc}). Check the internet connection "
                         "and run again.") from None
    url = latest_argyll_link(page)
    if not url:
        raise SystemExit("argyllcms.com lists no Windows download. Install ArgyllCMS from argyllcms.com, "
                         "then run again.")
    archive = TOOLS / url.rsplit("/", 1)[-1]
    download(url, archive, say)
    extract(archive, TOOLS, say)
    found = sorted(glob.glob(str(TOOLS / "Argyll*" / "bin")), key=version_key)
    if not found:
        raise SystemExit("The ArgyllCMS download did not contain spotread.")
    return found[-1]


def find_argyll(settings: dict, say) -> Path:
    cache = load_cache()
    if settings.get("argyll_bin"):
        if not argyll_candidates({"argyll_bin": settings["argyll_bin"]}, {})[:1]:
            raise SystemExit(f"argyll_bin in settings.json ({settings['argyll_bin']}) has no spotread in it.")
        return Path(settings["argyll_bin"])
    candidates = argyll_candidates({}, cache)
    argyll = candidates[0] if candidates else install_argyll(say)
    if cache.get("argyll_bin") != argyll:
        cache["argyll_bin"] = argyll
        save_cache(cache)
    return Path(argyll)


# --- meter correction -------------------------------------------------------

def ccss_dirs() -> list[Path]:
    dirs = [ROOT / "ccss"]
    for env, sub in (("PROGRAMDATA", "ArgyllCMS"), ("APPDATA", "ArgyllCMS"), ("LOCALAPPDATA", "ArgyllCMS"),
                     ("APPDATA", "DisplayCAL"), ("LOCALAPPDATA", "DisplayCAL")):
        base = os.environ.get(env)
        if base:
            dirs.append(Path(base) / sub)
    dirs.append(Path.home() / ".local" / "share" / "ArgyllCMS")
    return dirs


def ccss_score(path: Path) -> int:
    """3: made for LG's white-subpixel (WRGB/WOLED) panels. 0: anything else.
    RGB-OLED corrections (phone/Samsung panels) are not used on a WOLED TV."""
    try:
        head = path.read_text(encoding="latin-1", errors="replace")[:4000]
    except OSError:
        return 0
    text = (path.name + " " + " ".join(re.findall(r'(?:DISPLAY|TECHNOLOGY|DESCRIPTOR)\s+"([^"]*)"', head))).upper()
    if re.search(r"WOLED|WRGB|W-OLED|WHITE OLED", text):
        return 3
    if "OLED" in text and re.search(r"\bLG\b|C\d\b|G\d\b|B\d\b|CX|GX|BX", text):
        return 3
    return 0


def find_ccss(settings: dict) -> str:
    chosen = (settings.get("meter") or {}).get("ccss") or ""
    if chosen:
        return chosen
    best = ("", 0)
    for directory in ccss_dirs():
        if directory.is_dir():
            for path in directory.rglob("*.ccss"):
                score = ccss_score(path)
                if score > best[1]:
                    best = (str(path), score)
    return best[0]


def meter_command(settings: dict, argyll_bin: Path, ccss: str) -> list[str]:
    meter = settings.get("meter") or {}
    spotread = meter.get("spotread") or str(
        argyll_bin / ("spotread.exe" if (argyll_bin / "spotread.exe").exists() else "spotread"))
    command = [spotread, *[str(a) for a in meter.get("args", ["-e"])]]
    if ccss:
        command += ["-X", ccss]
    return command
