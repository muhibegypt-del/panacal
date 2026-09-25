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
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(data: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(data, indent=1), encoding="utf-8")


def download(url: str, target: Path, say, sha256: str = "") -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done, shown = 0, -1
        while chunk := response.read(1 << 20):
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            percent = done * 100 // total if total else -1
            if percent >= shown + 10:
                shown = percent
                say(f"  {target.name}: {percent}%" if total else f"  {target.name}: {done >> 20} MB")
    if sha256 and digest.hexdigest().lower() != sha256.lower():
        partial.unlink(missing_ok=True)
        raise SystemExit(f"Download of {url} failed its checksum; run again.")
    os.replace(partial, target)


# --- Perl -------------------------------------------------------------------

def perl_ok(perl: str) -> bool:
    """Native Windows Perl (not Git's msys Perl) with the modules the helper uses."""
    try:
        check = subprocess.run(
            [perl, "-MIO::Socket::SSL", "-MJSON::PP", "-MDigest::SHA", "-e", "print $^O"],
            capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return check.returncode == 0 and check.stdout.strip() in ("MSWin32", "linux", "darwin")


def perl_candidates(settings: dict, cache: dict) -> list[str]:
    found = [settings.get("perl"), cache.get("perl"),
             str(TOOLS / "strawberry" / "perl" / "bin" / "perl.exe"),
             r"C:\Strawberry\perl\bin\perl.exe", shutil.which("perl")]
    return [p for p in found if p and (Path(p).is_file() or shutil.which(p))]


def install_perl(say) -> str:
    """Strawberry Perl portable into tools/ (no installer, no admin rights)."""
    say("Perl is not installed. Downloading Strawberry Perl (one time, about 300 MB) ...")
    with urllib.request.urlopen(PERL_RELEASES, timeout=60) as response:
        releases = json.load(response)
    for release in releases:
        if release.get("archname", "").startswith("MSWin32-x64"):
            # The edition labels in releases.json are not reliable; match the file name.
            for entry in (release.get("edition") or {}).values():
                url = entry.get("url", "")
                if url.endswith("-64bit-portable.zip"):
                    archive = TOOLS / url.rsplit("/", 1)[-1]
                    download(url, archive, say, entry.get("sha256", ""))
                    say("  unpacking ...")
                    target = TOOLS / "strawberry"
                    shutil.rmtree(target, ignore_errors=True)
                    with zipfile.ZipFile(archive) as zf:
                        zf.extractall(target)
                    archive.unlink(missing_ok=True)
                    return str(target / "perl" / "bin" / "perl.exe")
    raise SystemExit("Could not find a Strawberry Perl download. Install it from strawberryperl.com.")


def find_perl(settings: dict, say) -> str:
    cache = load_cache()
    for perl in perl_candidates(settings, cache):
        if perl_ok(perl):
            if cache.get("perl") != perl:
                cache["perl"] = perl
                save_cache(cache)
            return perl
    perl = install_perl(say)
    if not perl_ok(perl):
        raise SystemExit(f"The downloaded Perl at {perl} does not work.")
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


def install_argyll(say) -> str:
    say("ArgyllCMS is not installed. Downloading it (one time, about 15 MB) ...")
    with urllib.request.urlopen(ARGYLL_PAGE, timeout=60) as response:
        page = response.read().decode("latin-1")
    links = re.findall(r'https?://[^"\']*Argyll_V[\d.]+_win64_exe\.zip', page)
    if not links:
        raise SystemExit("Could not find the ArgyllCMS download. Install it from argyllcms.com.")
    url = sorted(set(links), key=version_key)[-1]
    archive = TOOLS / url.rsplit("/", 1)[-1]
    download(url, archive, say)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(TOOLS)
    archive.unlink(missing_ok=True)
    found = sorted(glob.glob(str(TOOLS / "Argyll*" / "bin")), key=version_key)
    if not found:
        raise SystemExit("The ArgyllCMS download did not contain spotread.")
    return found[-1]


def find_argyll(settings: dict, say) -> Path:
    cache = load_cache()
    candidates = argyll_candidates(settings, cache)
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
    spotread = (settings.get("meter") or {}).get("spotread") or str(
        argyll_bin / ("spotread.exe" if (argyll_bin / "spotread.exe").exists() else "spotread"))
    command = [spotread, *[str(a) for a in (settings.get("meter") or {}).get("args", ["-e"])]]
    if ccss:
        command += ["-X", ccss]
    return command
