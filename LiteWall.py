"""
Live Wallpaper — production-ready desktop video wallpaper engine for Windows.

Architecture
============
  • Backend  : VLC (auto-detected system-wide or auto-downloaded portable)
  • Embedding : WorkerW / Progman Win32 trick
  • GUI       : CustomTkinter — modern dark theme, tabbed layout, tooltips,
                recent-files history, path entry, sparklines
  • Threading : all VLC / download work runs in daemon threads; every UI
                callback is marshalled back via after(0, …)
  • State     : single _WallpaperState dataclass; GUI only reads/writes through
                safe helper methods
  • Persist   : optional headless mode survives app close AND system reboot
                via Windows Registry Run key

Tested on Python 3.10 – 3.13 / Windows 10 & 11.
"""

# ──────────────────────────────────────────────────────────────────────────────
#  Bootstrap — install missing pip packages before any heavy import
# ──────────────────────────────────────────────────────────────────────────────
import subprocess, sys, importlib.util

def _pip(pkg: str) -> None:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", pkg, "--quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

for _pkg, _imp in [
    ("psutil",         "psutil"),
    ("python-vlc",     None),
    ("customtkinter",  "customtkinter"),
    ("pystray",        "pystray"),
    ("Pillow",         "PIL"),
    ("pywin32",        "win32com"),   # WScript.Shell for .lnk shortcut creation
]:
    if _imp and importlib.util.find_spec(_imp) is None:
        try: print(f"Installing {_pkg}…")
        except Exception: pass
        _pip(_pkg)
    elif _imp is None and importlib.util.find_spec("vlc") is None:
        try: print(f"Installing {_pkg}…")
        except Exception: pass
        _pip(_pkg)

# ──────────────────────────────────────────────────────────────────────────────
#  Standard imports
# ──────────────────────────────────────────────────────────────────────────────
import ctypes, json, logging, os, shutil, signal, string
import threading, time, traceback, urllib.request, zipfile
import winreg
import tkinter as tk
from tkinter import filedialog, messagebox
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import psutil
import customtkinter as ctk
import pystray
from PIL import Image, ImageDraw

# ──────────────────────────────────────────────────────────────────────────────
#  Hide the console window (works whether launched via python.exe or a shortcut)
# ──────────────────────────────────────────────────────────────────────────────
def _hide_console() -> None:
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE = 0
            ctypes.windll.kernel32.FreeConsole()        # detach so it doesn't reappear
    except Exception:
        pass

if not getattr(sys, "frozen", False):   # skip if compiled to .exe (already no console)
    _hide_console()

# ──────────────────────────────────────────────────────────────────────────────
#  CTk global appearance
# ──────────────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ──────────────────────────────────────────────────────────────────────────────
#  Logging
# ──────────────────────────────────────────────────────────────────────────────
LOG_FILE = Path(os.environ.get("APPDATA", Path.home())) / "LiveWallpaper" / "app.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        # StreamHandler removed — console is hidden; logs go to file only.
        # View logs in the app's Log tab or at: %APPDATA%\LiveWallpaper\app.log
    ],
)
log = logging.getLogger("live_wallpaper")

# ──────────────────────────────────────────────────────────────────────────────
#  Constants & paths
# ──────────────────────────────────────────────────────────────────────────────
APP_NAME    = "Live Wallpaper"
APP_VERSION = "2.2.0"

if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys._MEIPASS)
    SCRIPT_PATH = Path(sys.executable).resolve()
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
    SCRIPT_PATH = Path(__file__).resolve()

DATA_DIR            = Path(os.environ.get("APPDATA", Path.home())) / "LiveWallpaper"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE       = DATA_DIR / "settings.json"
PERSIST_FILE        = DATA_DIR / "persist.json"
VLC_PORTABLE_FOLDER = DATA_DIR / "vlc_portable"
VLC_URL             = "https://get.videolan.org/vlc/3.0.18/win64/vlc-3.0.18-win64.zip"
VLC_ZIP_VERSION     = "vlc-3.0.18"

SUPPORTED = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".gif", ".wmv", ".flv", ".m4v")
MAX_RECENT = 10

# ──────────────────────────────────────────────────────────────────────────────
#  Win32 constants for wallpaper save/restore
# ──────────────────────────────────────────────────────────────────────────────
SPI_GETDESKWALLPAPER = 0x0073
SPI_SETDESKWALLPAPER = 0x0014
SPIF_UPDATEINIFILE   = 0x01
SPIF_SENDCHANGE      = 0x02

# ──────────────────────────────────────────────────────────────────────────────
#  Settings (persistent JSON)
# ──────────────────────────────────────────────────────────────────────────────
_DEFAULT_SETTINGS = {
    "recent_files":      [],
    "mute":              True,
    "volume":            0,
    "speed":             1.0,
    "loop":              True,
    "fit_mode":          "stretch",
    "last_file":         "",
    "theme":             "dark",
    "persist_on_close":  False,
    "auto_start":        False,
    "survive_reboot":    False,       # ← NEW
    "original_wallpaper": "",         # ← NEW: saved wallpaper path
}

def load_settings() -> dict:
    try:
        if SETTINGS_FILE.is_file():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return {**_DEFAULT_SETTINGS, **data}
    except Exception as exc:
        log.warning("Could not load settings: %s", exc)
    return dict(_DEFAULT_SETTINGS)

def save_settings(s: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("Could not save settings: %s", exc)

# ──────────────────────────────────────────────────────────────────────────────
#  Windows Startup — shell:startup shortcut (.lnk)
#
#  Why not Registry?
#  • shell:startup shortcuts require zero elevated permissions.
#  • They don't trigger Windows Defender / SmartScreen warnings that Registry
#    Run keys written by unknown publishers tend to cause.
#  • Visible and manageable by the user in Task Manager → Startup Apps.
#  • Works identically for both the .py source and a PyInstaller .exe.
#
#  How it works:
#  • We drop  "LiveWallpaper.lnk"  into the per-user Startup folder:
#      %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
#  • The shortcut targets the .exe (frozen) or python.exe + script (dev),
#    passes --headless, and sets the working directory to the exe/script folder.
#  • On removal we simply delete the .lnk file.
# ──────────────────────────────────────────────────────────────────────────────

# shell:startup folder path (always exists for every Windows user)
_STARTUP_FOLDER = Path(os.environ.get("APPDATA", Path.home())) / \
    "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
_STARTUP_LNK    = _STARTUP_FOLDER / "LiveWallpaper.lnk"


def _create_shortcut(lnk_path: Path, target: str, args: str,
                     workdir: str, description: str) -> None:
    """
    Create a Windows .lnk shortcut using the WScript.Shell COM object.
    No admin rights required. Works in both .py and frozen .exe contexts.
    """
    import win32com.client          # part of pywin32 (bundled by PyInstaller)
    shell    = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(lnk_path))
    shortcut.TargetPath       = target
    shortcut.Arguments        = args
    shortcut.WorkingDirectory = workdir
    shortcut.Description      = description
    shortcut.WindowStyle      = 7   # SW_SHOWMINNOACTIVE — start minimised/hidden
    shortcut.Save()


def add_to_startup() -> bool:
    """
    Drop LiveWallpaper.lnk into the user's shell:startup folder.
    No Registry writes, no UAC prompt, no Defender warning.
    """
    try:
        _STARTUP_FOLDER.mkdir(parents=True, exist_ok=True)

        if getattr(sys, "frozen", False):
            # PyInstaller .exe — target is the exe itself
            target  = str(SCRIPT_PATH)
            args    = "--headless"
            workdir = str(SCRIPT_PATH.parent)
        else:
            # Running as plain .py (development)
            target  = sys.executable
            args    = f'"{SCRIPT_PATH}" --headless'
            workdir = str(SCRIPT_DIR)

        _create_shortcut(
            lnk_path    = _STARTUP_LNK,
            target      = target,
            args        = args,
            workdir     = workdir,
            description = "Live Wallpaper — headless background mode",
        )
        log.info("Startup shortcut created: %s  →  %s %s", _STARTUP_LNK, target, args)
        return True

    except Exception as exc:
        log.error("Failed to create startup shortcut: %s", exc)
        return False


def remove_from_startup() -> bool:
    """Delete the .lnk from shell:startup if it exists."""
    try:
        if _STARTUP_LNK.is_file():
            _STARTUP_LNK.unlink()
            log.info("Startup shortcut removed: %s", _STARTUP_LNK)
        else:
            log.debug("remove_from_startup: shortcut not found (already gone).")
        return True
    except Exception as exc:
        log.error("Failed to remove startup shortcut: %s", exc)
        return False


def is_in_startup() -> bool:
    """Return True if our .lnk exists in shell:startup."""
    return _STARTUP_LNK.is_file()


# ──────────────────────────────────────────────────────────────────────────────
#  Original wallpaper save / restore
# ──────────────────────────────────────────────────────────────────────────────
_original_wallpaper: Optional[str] = None

def _get_current_wallpaper() -> str:
    """Read the current desktop wallpaper path from Windows."""
    try:
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETDESKWALLPAPER, len(buf), buf, 0
        )
        return buf.value or ""
    except Exception as exc:
        log.warning("Could not read current wallpaper: %s", exc)
        return ""


def save_original_wallpaper() -> None:
    """Capture the current desktop wallpaper path before we replace it."""
    global _original_wallpaper

    # If we already saved one this session, don't overwrite
    if _original_wallpaper is not None:
        return

    # Check if settings already has a saved wallpaper (from previous session)
    settings = load_settings()
    saved = settings.get("original_wallpaper", "")

    current = _get_current_wallpaper()

    if saved and Path(saved).is_file():
        # Use the one from settings (original before ANY live wallpaper session)
        _original_wallpaper = saved
        log.info("Using previously saved wallpaper: %s", saved)
    else:
        # Save current
        _original_wallpaper = current
        settings["original_wallpaper"] = current
        save_settings(settings)
        log.info("Saved original wallpaper: %s",
                 current if current else "(solid color / none)")


def restore_original_wallpaper() -> None:
    """Set the desktop wallpaper back to what it was."""
    global _original_wallpaper

    settings = load_settings()

    # Try in-memory first, then settings file
    wp = _original_wallpaper or settings.get("original_wallpaper", "")

    if not wp:
        log.debug("No original wallpaper saved — skipping restore.")
        return

    log.info("Restoring original wallpaper: %s", wp if wp else "(solid color)")
    try:
        u32 = ctypes.windll.user32

        # Step 1 — tell Windows to set the wallpaper back
        u32.SystemParametersInfoW(
            SPI_SETDESKWALLPAPER, 0, wp,
            SPIF_UPDATEINIFILE | SPIF_SENDCHANGE,
        )

        # Step 2 — give Windows a moment to actually paint the wallpaper
        # before we return (without this you get a black flash)
        time.sleep(0.15)

        # Step 3 — force a full desktop repaint so the wallpaper appears
        # immediately rather than waiting for the next natural refresh
        u32.InvalidateRect(None, None, True)
        u32.UpdateWindow(u32.GetDesktopWindow())

    except Exception as exc:
        log.warning("Failed to restore wallpaper: %s", exc)

    # Clear the saved wallpaper since we've restored it
    _original_wallpaper = None
    settings["original_wallpaper"] = ""
    save_settings(settings)


# ──────────────────────────────────────────────────────────────────────────────
#  Persistence state file
# ──────────────────────────────────────────────────────────────────────────────
def save_persist_state(playing: bool, video_path: Optional[Path] = None,
                       mute: bool = True, volume: int = 0,
                       speed: float = 1.0, loop: bool = True) -> None:
    data = {
        "playing":    playing,
        "video_path": str(video_path) if video_path else "",
        "mute":       mute,
        "volume":     volume,
        "speed":      speed,
        "loop":       loop,
        "pid":        os.getpid(),
        "timestamp":  time.time(),
    }
    try:
        PERSIST_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("Could not write persist state: %s", exc)


def load_persist_state() -> Optional[dict]:
    try:
        if PERSIST_FILE.is_file():
            return json.loads(PERSIST_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not read persist state: %s", exc)
    return None


def clear_persist_state() -> None:
    try:
        if PERSIST_FILE.is_file():
            PERSIST_FILE.unlink()
    except Exception as exc:
        log.debug("Could not remove persist file: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
#  Smart VLC finder
# ──────────────────────────────────────────────────────────────────────────────
def find_vlc_folder() -> Optional[Path]:
    def dll_in(folder: Path) -> Optional[Path]:
        try:
            return folder if (folder / "libvlc.dll").is_file() else None
        except (OSError, PermissionError):
            return None

    r = dll_in(VLC_PORTABLE_FOLDER)
    if r: return r

    pf_seen: list[Path] = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        v = os.environ.get(env)
        if v:
            p = Path(v)
            if p not in pf_seen:
                pf_seen.append(p)
    for root in pf_seen:
        for cand in (root / "VideoLAN" / "VLC", root / "VLC"):
            r = dll_in(cand)
            if r: return r

    try:
        for hive, sub in [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VideoLAN\VLC"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\VideoLAN\VLC"),
            (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\VideoLAN\VLC"),
        ]:
            try:
                with winreg.OpenKey(hive, sub) as key:
                    for val_name in ("InstallDir", ""):
                        try:
                            val, _ = winreg.QueryValueEx(key, val_name)
                            p = Path(val)
                            r = dll_in(p if p.is_dir() else p.parent)
                            if r: return r
                        except FileNotFoundError:
                            pass
            except (FileNotFoundError, OSError):
                pass
    except Exception:
        pass

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            r = dll_in(Path(entry))
            if r: return r

    _skip = {"windows","system32","syswow64","$recycle.bin","recovery",
             "msocache","perflogs","boot","winsxs","softwaredistribution"}
    for drv in [Path(f"{d}:\\") for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]:
        for dp, dirs, files in os.walk(drv):
            p = Path(dp)
            try:
                depth = len(p.relative_to(drv).parts)
            except ValueError:
                continue
            if depth > 4:
                dirs.clear(); continue
            if "libvlc.dll" in files:
                return p
            dirs[:] = [d for d in dirs if d.lower() not in _skip]
    return None


_vlc_folder: Optional[Path] = find_vlc_folder()

def vlc_is_ready() -> bool:
    return _vlc_folder is not None and (_vlc_folder / "libvlc.dll").is_file()

# ──────────────────────────────────────────────────────────────────────────────
#  Desktop embedding helpers (Win32)
# ──────────────────────────────────────────────────────────────────────────────
def restore_desktop() -> None:
    """
    Restore Explorer's desktop layer after we're done with WorkerW.

    The original code only called ShowWindow(workerw, 5) — that makes the
    WorkerW visible again but SHELLDLL_DefView (desktop icons / renderer)
    is still orphaned from when we embedded into it. This causes the desktop
    to freeze until Explorer is manually restarted.

    Fix: re-send 0x052C to Progman which tells Explorer to fully rebuild
    the WorkerW ownership chain, then hide the now-clean WorkerW so the
    normal desktop renders on top of it correctly.
    """
    try:
        u32     = ctypes.windll.user32
        progman = u32.FindWindowW("Progman", None)
        if not progman:
            log.debug("restore_desktop: Progman not found")
            return

        # Re-trigger Explorer's WorkerW rebuild — this re-parents
        # SHELLDLL_DefView correctly and un-orphans the desktop renderer
        u32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0, 2000,
                                ctypes.byref(ctypes.c_ulong()))

        # Small wait for Explorer to finish rebuilding its window tree
        time.sleep(0.1)

        # Now find and HIDE the WorkerW — the normal desktop sits above it
        ww = ctypes.c_void_p(None)

        def _cb(hwnd, _):
            if u32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
                candidate = u32.FindWindowExW(None, hwnd, "WorkerW", None)
                if candidate:
                    ww.value = candidate
            return True

        u32.EnumWindows(
            ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_cb), 0
        )
        if ww.value:
            # SW_HIDE = 0, not SW_SHOW — WorkerW should be hidden after restore
            # so normal desktop rendering takes over
            u32.ShowWindow(ww.value, 0)

        # Force the desktop window to repaint immediately
        hwnd_desk = u32.GetDesktopWindow()
        u32.InvalidateRect(hwnd_desk, None, True)
        u32.UpdateWindow(hwnd_desk)

        log.debug("restore_desktop: desktop layer rebuilt successfully")
    except Exception as exc:
        log.debug("restore_desktop: %s", exc)


_cached_workerw: Optional[int] = None  # cached after first successful lookup


def _get_workerw(force_refresh: bool = False) -> Optional[int]:
    """
    Return the WorkerW (or Progman) handle used for desktop embedding.

    The handle is cached after the first successful lookup — re-enumeration
    only happens when force_refresh=True (e.g. after Explorer restarts).

    Key changes vs original:
      • Timeout raised 1000 → 2000 ms so Explorer isn't hammered on slow machines
      • Blind time.sleep(0.3) removed — we block inside SendMessageTimeout itself
      • Result cached so Explorer's message queue is only interrupted once
    """
    global _cached_workerw

    if _cached_workerw and not force_refresh:
        # Verify the cached handle is still alive before returning it
        u32 = ctypes.windll.user32
        if u32.IsWindow(_cached_workerw):
            return _cached_workerw
        # Handle gone (Explorer restarted) — fall through to re-enumerate
        log.debug("_get_workerw: cached handle invalid, re-enumerating")
        _cached_workerw = None

    u32     = ctypes.windll.user32
    progman = u32.FindWindowW("Progman", None)
    if not progman:
        return None

    # 0x052C forces Progman to spawn the WorkerW layer.
    # Timeout raised to 2000 ms — gives Explorer more breathing room,
    # which paradoxically causes LESS lag than a tight 1000 ms timeout.
    u32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0, 2000,
                            ctypes.byref(ctypes.c_ulong()))
    # No sleep here — SendMessageTimeout already blocks until Explorer responds.

    ww = ctypes.c_void_p(None)

    def _cb(hwnd, _):
        if u32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
            candidate = u32.FindWindowExW(None, hwnd, "WorkerW", None)
            if candidate:
                ww.value = candidate
        return True

    u32.EnumWindows(
        ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_cb), 0
    )
    result = ww.value or progman
    if result:
        _cached_workerw = result
        log.debug("_get_workerw: handle cached (0x%x)", result)
    return result


def _create_child_window(parent: int, w: int, h: int) -> int:
    WS_CHILD   = 0x40000000
    WS_VISIBLE = 0x10000000
    u32       = ctypes.windll.user32
    hinstance = ctypes.windll.kernel32.GetModuleHandleW(None)
    return u32.CreateWindowExW(
        0, "STATIC", None,
        WS_CHILD | WS_VISIBLE,
        0, 0, w, h,
        parent, None, hinstance, None,
    )

# ──────────────────────────────────────────────────────────────────────────────
#  Wallpaper engine state
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class _WallpaperState:
    player:     object = None
    instance:   object = None
    embed_hwnd: int    = 0
    path:       Optional[Path] = None

_ws = _WallpaperState()
_ws_lock = threading.Lock()


def start_wallpaper(
    video_path: Path,
    *,
    mute: bool        = True,
    volume: int       = 0,
    speed: float      = 1.0,
    loop: bool        = True,
    status_cb: Callable[[str], None] = lambda _: None,
) -> None:
    global _vlc_folder

    if not video_path.is_file():
        raise FileNotFoundError(f"File not found: {video_path}")
    if not vlc_is_ready():
        raise RuntimeError("VLC is not available. Please wait for the download.")

    save_original_wallpaper()
    stop_wallpaper(restore_wp=False)

    vlc_dll     = _vlc_folder / "libvlc.dll"
    vlc_plugins = _vlc_folder / "plugins"

    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(str(_vlc_folder))
        except Exception as exc:
            log.debug("add_dll_directory: %s", exc)
    os.environ["PYTHON_VLC_LIB_PATH"]    = str(vlc_dll)
    os.environ["PYTHON_VLC_MODULE_PATH"] = str(vlc_plugins)
    os.environ["VLC_PLUGIN_PATH"]        = str(vlc_plugins)

    try:
        import vlc
    except Exception as exc:
        raise RuntimeError(f"Could not load VLC library: {exc}") from exc

    u32 = ctypes.windll.user32
    sw  = u32.GetSystemMetrics(0)
    sh  = u32.GetSystemMetrics(1)

    # _get_workerw is called here (already on the wallpaper worker thread,
    # never on the UI thread) so Explorer's message queue interruption
    # never freezes the GUI. The cached handle means subsequent calls
    # to start_wallpaper skip the SendMessageTimeout entirely.
    workerw = _get_workerw()
    if not workerw:
        raise RuntimeError("Could not find WorkerW / Progman handle.\n"
                           "Make sure Explorer is running.")
    u32.ShowWindow(workerw, 5)

    embed = _create_child_window(workerw, sw, sh)
    if not embed:
        raise RuntimeError("Failed to create embed window inside WorkerW.")

    try:
        inst = vlc.Instance(
            "--no-xlib",
            "--no-video-title-show",
            "--quiet",
            "--avcodec-hw=any",
            "--file-caching=500",
            "--no-snapshot-preview",
            "--input-repeat=-1",   # true infinite loop — avoids VLC 3.x memory
                                   # leak caused by very large repeat counts
        )
        player = inst.media_player_new()
        media  = inst.media_new(str(video_path))
        if loop:
            media.add_option("input-repeat=65535")  # ✅ restore looping
        else:
            media.add_option("input-repeat=0")   # play once then stop
        player.set_media(media)
        media.release()
        player.set_hwnd(embed)
        player.video_set_scale(0)
        player.audio_set_mute(mute)
        if not mute:
            player.audio_set_volume(max(0, min(200, volume)))
        player.set_rate(max(0.1, min(4.0, speed)))
        player.play()
    except Exception as exc:
        u32.DestroyWindow(embed)
        raise RuntimeError(f"VLC playback error: {exc}") from exc

    with _ws_lock:
        _ws.player     = player
        _ws.instance   = inst
        _ws.embed_hwnd = embed
        _ws.path       = video_path

    log.info("Wallpaper started: %s", video_path.name)
    status_cb(f"Playing: {video_path.name}")


def stop_wallpaper(restore_wp: bool = True) -> None:
    global _cached_workerw
    with _ws_lock:
        if _ws.player:
            try:
                _ws.player.stop()
                _ws.player.release()
            except Exception as exc:
                log.debug("player stop/release: %s", exc)
            try:
                if _ws.instance:
                    _ws.instance.release()
            except Exception as exc:
                log.debug("instance release: %s", exc)
            _ws.player   = None
            _ws.instance = None

        if _ws.embed_hwnd:
            try:
                ctypes.windll.user32.DestroyWindow(_ws.embed_hwnd)
            except Exception as exc:
                log.debug("DestroyWindow: %s", exc)
            _ws.embed_hwnd = 0

        # Invalidate the WorkerW cache — embed window is gone, next
        # start_wallpaper will re-verify the handle is still valid
        _cached_workerw = None

        _ws.path = None

    # Restore the original wallpaper FIRST (before hiding WorkerW) so
    # Windows has already painted it by the time WorkerW disappears.
    # Previous order was wrong — hiding WorkerW first left a black frame.
    if restore_wp:
        restore_original_wallpaper()
        clear_persist_state()

    restore_desktop()

    if not restore_wp:
        pass   # restore_desktop still hides WorkerW cleanly without restoring wp

    log.info("Wallpaper stopped (restore_wp=%s).", restore_wp)


def update_playback(*, mute: Optional[bool] = None, volume: Optional[int] = None,
                    speed: Optional[float] = None) -> None:
    with _ws_lock:
        p = _ws.player
        if p is None:
            return
        try:
            if mute is not None:
                p.audio_set_mute(mute)
            if volume is not None:
                p.audio_set_volume(max(0, min(200, volume)))
            if speed is not None:
                p.set_rate(max(0.1, min(4.0, speed)))
        except Exception as exc:
            log.debug("update_playback: %s", exc)

# ──────────────────────────────────────────────────────────────────────────────
#  Background persistence — headless mode
# ──────────────────────────────────────────────────────────────────────────────
def run_headless(persist: dict) -> None:
    video_path = Path(persist["video_path"])
    if not video_path.is_file():
        log.error("Headless: video not found: %s", video_path)
        clear_persist_state()
        return

    log.info("Headless mode starting for: %s", video_path.name)

    # Wait a bit for desktop to be ready (important after reboot)
    time.sleep(3)

    # Retry logic — after reboot Explorer might not be ready immediately
    max_retries = 5
    for attempt in range(max_retries):
        try:
            start_wallpaper(
                video_path,
                mute=persist.get("mute", True),
                volume=persist.get("volume", 0),
                speed=persist.get("speed", 1.0),
                loop=persist.get("loop", True),
            )
            break
        except RuntimeError as exc:
            log.warning("Headless attempt %d/%d failed: %s",
                        attempt + 1, max_retries, exc)
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                log.error("Headless: all attempts failed. Giving up.")
                clear_persist_state()
                return

    save_persist_state(
        playing=True,
        video_path=video_path,
        mute=persist.get("mute", True),
        volume=persist.get("volume", 0),
        speed=persist.get("speed", 1.0),
        loop=persist.get("loop", True),
    )

    try:
        while True:
            time.sleep(2)
            state = load_persist_state()
            if state is None or not state.get("playing", False):
                log.info("Headless: persist state cleared — stopping.")
                break
            with _ws_lock:
                if _ws.player is None:
                    log.info("Headless: player died — stopping.")
                    break

            # Check if Explorer crashed and restarted
            # (VLC window would be orphaned — need to re-embed)
            with _ws_lock:
                player_alive = _ws.player is not None
                if player_alive:
                    try:
                        import vlc as _vlc_mod
                        pstate = _ws.player.get_state()
                        if pstate in (_vlc_mod.State.Ended, _vlc_mod.State.Error):
                            log.warning("Headless: player state=%s — restarting.", pstate)
                            player_alive = False
                    except Exception:
                        pass

            if not player_alive:
                log.info("Headless: restarting playback...")
                try:
                    start_wallpaper(
                        video_path,
                        mute=persist.get("mute", True),
                        volume=persist.get("volume", 0),
                        speed=persist.get("speed", 1.0),
                        loop=persist.get("loop", True),
                    )
                except Exception as exc:
                    log.error("Headless restart failed: %s", exc)
                    break

    except KeyboardInterrupt:
        pass
    finally:
        stop_wallpaper(restore_wp=True)
        clear_persist_state()
        log.info("Headless mode ended.")


def spawn_headless_process(persist_data: dict) -> None:
    save_persist_state(**{
        "playing":    True,
        "video_path": Path(persist_data["video_path"]),
        "mute":       persist_data.get("mute", True),
        "volume":     persist_data.get("volume", 0),
        "speed":      persist_data.get("speed", 1.0),
        "loop":       persist_data.get("loop", True),
    })

    script = str(SCRIPT_PATH)
    if getattr(sys, "frozen", False):
        cmd = [script, "--headless"]
    else:
        cmd = [sys.executable, script, "--headless"]

    log.info("Spawning headless process: %s", " ".join(cmd))

    CREATE_NO_WINDOW = 0x08000000
    subprocess.Popen(
        cmd,
        creationflags=CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


def kill_existing_headless() -> None:
    state = load_persist_state()
    if state is None:
        return
    pid = state.get("pid", 0)
    if pid and pid != os.getpid():
        try:
            proc = psutil.Process(pid)
            if "python" in proc.name().lower() or "livewallpaper" in proc.name().lower():
                log.info("Killing previous headless process (PID %d)", pid)
                proc.terminate()
                proc.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            pass
    clear_persist_state()


# ──────────────────────────────────────────────────────────────────────────────
#  VLC download
# ──────────────────────────────────────────────────────────────────────────────
def download_vlc(
    progress_cb: Callable[[int], None],
    done_cb:     Callable[[bool, Optional[str]], None],
) -> None:
    global _vlc_folder
    try:
        zip_path = DATA_DIR / "vlc_temp.zip"
        VLC_PORTABLE_FOLDER.mkdir(parents=True, exist_ok=True)
        log.info("Downloading VLC from %s", VLC_URL)

        def _hook(count: int, block: int, total: int) -> None:
            if total > 0:
                progress_cb(min(int(count * block * 100 / total), 99))

        urllib.request.urlretrieve(VLC_URL, zip_path, _hook)

        log.info("Extracting VLC…")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(VLC_PORTABLE_FOLDER)
        zip_path.unlink(missing_ok=True)

        extracted = VLC_PORTABLE_FOLDER / VLC_ZIP_VERSION
        if extracted.is_dir():
            for item in extracted.iterdir():
                dst = VLC_PORTABLE_FOLDER / item.name
                if not dst.exists():
                    shutil.move(str(item), str(dst))
            shutil.rmtree(extracted, ignore_errors=True)

        _vlc_folder = find_vlc_folder()
        if not vlc_is_ready():
            raise RuntimeError("Extraction succeeded but libvlc.dll not found — "
                               "zip may be corrupt. Delete vlc_portable and retry.")

        progress_cb(100)
        log.info("VLC ready at %s", _vlc_folder)
        done_cb(True, None)

    except Exception as exc:
        log.error("VLC download failed: %s", exc, exc_info=True)
        done_cb(False, str(exc))

# ──────────────────────────────────────────────────────────────────────────────
#  Media file scanner
# ──────────────────────────────────────────────────────────────────────────────
def find_media_files() -> list[Path]:
    home = Path.home()
    search_dirs = [
        SCRIPT_DIR,
        home / "Videos",
        home / "Downloads",
        home / "Desktop",
        home / "Pictures",
        home / "Documents",
    ]
    seen: set[Path] = set()
    found: list[Path] = []
    for folder in search_dirs:
        if not folder.is_dir():
            continue
        try:
            for f in sorted(folder.iterdir()):
                if f.suffix.lower() in SUPPORTED and f not in seen:
                    seen.add(f)
                    found.append(f)
        except PermissionError:
            pass
    return found

# ──────────────────────────────────────────────────────────────────────────────
#  Colour helpers
# ──────────────────────────────────────────────────────────────────────────────
def lerp_color(pct: float,
               low: str = "#52d68a", mid: str = "#f9c74f",
               high: str = "#f96060", t1: float = 50, t2: float = 80) -> str:
    def h2r(h: str):
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    def r2h(r, g, b) -> str:
        return f"#{int(r):02x}{int(g):02x}{int(b):02x}"
    def blend(a, b, t):
        return tuple(a[i] + (b[i]-a[i])*t for i in range(3))

    pct = max(0.0, min(100.0, pct))
    cl, cm, ch = h2r(low), h2r(mid), h2r(high)
    if pct <= t1:   return r2h(*blend(cl, cm, pct / t1))
    elif pct <= t2: return r2h(*blend(cm, ch, (pct-t1)/(t2-t1)))
    else:           return r2h(*ch)

def fmt_size(path: Path) -> str:
    try:
        b = path.stat().st_size
        for unit in ("B","KB","MB","GB"):
            if b < 1024: return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} TB"
    except OSError:
        return "?"

# ──────────────────────────────────────────────────────────────────────────────
#  Colour tokens
# ──────────────────────────────────────────────────────────────────────────────
_BG       = "#0f1117"
_SURFACE  = "#161b27"
_CARD     = "#1c2133"
_CARD2    = "#222840"
_BORDER   = "#2a3050"
_ACCENT   = "#4c7cfa"
_ACCENT_D = "#3561d4"
_FG       = "#e2e8f8"
_FG2      = "#6b7a9e"
_FG3      = "#3a4260"
_SUCCESS  = "#3ecf8e"
_WARN     = "#f9c74f"
_DANGER   = "#f96060"

# ──────────────────────────────────────────────────────────────────────────────
#  System Tray Icon
# ──────────────────────────────────────────────────────────────────────────────
def _make_tray_icon_image() -> Image.Image:
    """Draw a simple ▶ play-button icon for the tray."""
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Circle background
    draw.ellipse([2, 2, size - 2, size - 2], fill="#4c7cfa")
    # Triangle (play symbol)
    margin = size // 4
    draw.polygon(
        [(margin + 4, margin), (size - margin, size // 2), (margin + 4, size - margin)],
        fill="#ffffff",
    )
    return img


# ──────────────────────────────────────────────────────────────────────────────
#  Main Application
# ──────────────────────────────────────────────────────────────────────────────
class App(ctk.CTk):

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("740x800")
        self.minsize(660, 700)
        self.configure(fg_color=_BG)

        self._settings     = load_settings()
        self._is_playing   = False
        self._selected_path: Optional[Path] = None
        self._file_map:    dict[str, Path]  = {}
        self._stats_id:    Optional[str]    = None
        self._scan_thread: Optional[threading.Thread] = None
        self._cpu_hist     = [0.0] * 60
        self._ram_hist     = [0.0] * 60
        self._tray_icon:   Optional[pystray.Icon] = None
        self._tray_thread: Optional[threading.Thread] = None

        psutil.cpu_percent(interval=None)

        kill_existing_headless()

        self._build_ui()
        self._try_auto_resume()

        last = self._settings.get("last_file", "")
        if last and Path(last).is_file():
            self._set_selected(Path(last))

        self.after(120, self._async_scan)
        self.after(800, self._update_stats)
        self.after(200, self._start_tray)

    def _try_auto_resume(self) -> None:
        if not self._settings.get("auto_start", False):
            return
        last = self._settings.get("last_file", "")
        if last and Path(last).is_file() and vlc_is_ready():
            log.info("Auto-resuming wallpaper: %s", last)
            self.after(500, lambda: self._auto_launch(Path(last)))

    def _auto_launch(self, path: Path) -> None:
        self._set_selected(path)
        self._launch(path)

    # ══════════════════════════════════════════════════════════════════════════
    #  UI CONSTRUCTION
    # ══════════════════════════════════════════════════════════════════════════
    def _build_ui(self) -> None:
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Header
        hdr = ctk.CTkFrame(self, fg_color=_SURFACE, corner_radius=0, height=68)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        hdr.grid_columnconfigure(1, weight=1)

        icon_bg = ctk.CTkFrame(hdr, fg_color=_ACCENT, corner_radius=10,
                                width=38, height=38)
        icon_bg.grid(row=0, column=0, rowspan=2, padx=(20, 14), pady=15)
        icon_bg.grid_propagate(False)
        ctk.CTkLabel(icon_bg, text="▶", text_color="#ffffff",
                     font=ctk.CTkFont("Segoe UI", 16, "bold")
                     ).place(relx=.5, rely=.5, anchor="center")

        title_col = ctk.CTkFrame(hdr, fg_color="transparent")
        title_col.grid(row=0, column=1, sticky="sw", pady=(14, 0))
        ctk.CTkLabel(title_col, text=APP_NAME,
                     font=ctk.CTkFont("Segoe UI", 15, "bold"),
                     text_color=_FG).pack(anchor="w")

        sub_col = ctk.CTkFrame(hdr, fg_color="transparent")
        sub_col.grid(row=1, column=1, sticky="nw", pady=(0, 14))
        ctk.CTkLabel(sub_col, text=f"v{APP_VERSION}  ·  Desktop Video Engine",
                     font=ctk.CTkFont("Segoe UI", 9),
                     text_color=_FG3).pack(anchor="w")

        self._vlc_pill = ctk.CTkLabel(
            hdr, text="", font=ctk.CTkFont("Segoe UI", 9),
            text_color=_FG2, fg_color=_CARD2, corner_radius=8,
            padx=12, pady=5,
        )
        self._vlc_pill.grid(row=0, column=2, rowspan=2, padx=(0, 20), pady=15)
        self._refresh_vlc_pill()

        ctk.CTkFrame(self, fg_color=_ACCENT, corner_radius=0, height=2
                     ).grid(row=1, column=0, sticky="ew")

        # Tabs
        self._tabs = ctk.CTkTabview(
            self,
            fg_color=_BG,
            segmented_button_fg_color=_SURFACE,
            segmented_button_selected_color=_CARD,
            segmented_button_selected_hover_color=_CARD2,
            segmented_button_unselected_color=_SURFACE,
            segmented_button_unselected_hover_color=_CARD,
            text_color=_FG2,
            text_color_disabled=_FG3,
            corner_radius=0,
        )
        self._tabs.grid(row=2, column=0, sticky="nsew")

        for name in ("Wallpaper", "Settings", "Monitor", "Log"):
            self._tabs.add(name)

        self._build_tab_wallpaper(self._tabs.tab("Wallpaper"))
        self._build_tab_settings(self._tabs.tab("Settings"))
        self._build_tab_monitor(self._tabs.tab("Monitor"))
        self._build_tab_log(self._tabs.tab("Log"))

        # Status bar
        ctk.CTkFrame(self, fg_color=_BORDER, corner_radius=0, height=1
                     ).grid(row=3, column=0, sticky="ew")
        sb = ctk.CTkFrame(self, fg_color=_SURFACE, corner_radius=0, height=30)
        sb.grid(row=4, column=0, sticky="ew")
        sb.grid_propagate(False)
        sb.grid_columnconfigure(1, weight=1)

        self._status_dot = ctk.CTkLabel(sb, text="●", width=22,
                                         font=ctk.CTkFont("Segoe UI", 10),
                                         text_color=_FG3)
        self._status_dot.grid(row=0, column=0, padx=(14, 0), pady=6)

        self._status_var = tk.StringVar(value="Ready")
        ctk.CTkLabel(sb, textvariable=self._status_var,
                     font=ctk.CTkFont("Segoe UI", 9),
                     text_color=_FG2, anchor="w"
                     ).grid(row=0, column=1, sticky="w", padx=4, pady=6)

        self._playing_lbl = ctk.CTkLabel(sb, text="",
                                          font=ctk.CTkFont("Segoe UI", 9, slant="italic"),
                                          text_color=_FG3)
        self._playing_lbl.grid(row=0, column=2, padx=(0, 14), pady=6)

    # ─────────────────────────────────────────────────────────────────────────
    #  Tab: Wallpaper
    # ─────────────────────────────────────────────────────────────────────────
    def _build_tab_wallpaper(self, parent: ctk.CTkFrame) -> None:
        parent.grid_rowconfigure(2, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 10))

        for text, cmd, w in [
            ("Browse…",     self._browse,           110),
            ("Scan Folders",self._async_scan,        130),
            ("Recent ▾",    self._show_recent_menu,  100),
        ]:
            ctk.CTkButton(bar, text=text, width=w, height=34,
                           fg_color=_CARD2, hover_color=_BORDER,
                           text_color=_FG, font=ctk.CTkFont("Segoe UI", 12),
                           corner_radius=8, command=cmd
                           ).pack(side="left", padx=(0, 8))

        path_row = ctk.CTkFrame(parent, fg_color=_CARD, corner_radius=10)
        path_row.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 12))
        path_row.grid_columnconfigure(0, weight=1)

        self._path_entry = ctk.CTkEntry(
            path_row,
            placeholder_text="Paste a file path here, or use Browse…",
            fg_color=_CARD, border_color=_BORDER, border_width=0,
            text_color=_FG, placeholder_text_color=_FG3,
            font=ctk.CTkFont("Segoe UI", 11),
            corner_radius=10, height=40,
        )
        self._path_entry.grid(row=0, column=0, sticky="ew", padx=(12, 6), pady=6)

        ctk.CTkButton(
            path_row, text="Open", width=70, height=30,
            fg_color=_ACCENT, hover_color=_ACCENT_D,
            text_color="#fff", font=ctk.CTkFont("Segoe UI", 11, "bold"),
            corner_radius=8, command=self._apply_from_entry,
        ).grid(row=0, column=1, padx=(0, 8), pady=6)

        list_card = ctk.CTkFrame(parent, fg_color=_CARD, corner_radius=12)
        list_card.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 10))
        list_card.grid_rowconfigure(0, weight=1)
        list_card.grid_columnconfigure(0, weight=1)

        self._listbox = tk.Listbox(
            list_card,
            bg=_CARD, fg=_FG,
            selectbackground="#1e3a6e", selectforeground=_ACCENT,
            font=("Segoe UI", 10), relief="flat", bd=0,
            activestyle="none", highlightthickness=0,
        )
        self._listbox.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)

        list_sb = ctk.CTkScrollbar(list_card, command=self._listbox.yview,
                                    fg_color=_CARD, button_color=_BORDER,
                                    button_hover_color=_FG3)
        list_sb.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=8)
        self._listbox.configure(yscrollcommand=list_sb.set)
        self._listbox.bind("<<ListboxSelect>>", self._on_list_select)
        self._listbox.bind("<Double-Button-1>", lambda _: self._apply())

        self._list_status = ctk.CTkLabel(parent, text="",
                                          font=ctk.CTkFont("Segoe UI", 10),
                                          text_color=_FG3, anchor="w")
        self._list_status.grid(row=3, column=0, sticky="w", padx=24, pady=(0, 6))

        preview = ctk.CTkFrame(parent, fg_color=_CARD, corner_radius=12)
        preview.grid(row=4, column=0, sticky="ew", padx=20, pady=(0, 10))
        preview.grid_columnconfigure(1, weight=1)

        self._prev_icon = ctk.CTkLabel(preview, text="🎬",
                                        font=ctk.CTkFont("Segoe UI Emoji", 24), width=50)
        self._prev_icon.grid(row=0, column=0, rowspan=2, padx=(16, 4), pady=14)

        self._prev_name = ctk.CTkLabel(preview, text="No file selected",
                                        font=ctk.CTkFont("Segoe UI", 12, "bold"),
                                        text_color=_FG, anchor="w")
        self._prev_name.grid(row=0, column=1, sticky="w", padx=(0, 16), pady=(14, 1))

        self._prev_meta = ctk.CTkLabel(preview, text="",
                                        font=ctk.CTkFont("Segoe UI", 10),
                                        text_color=_FG2, anchor="w")
        self._prev_meta.grid(row=1, column=1, sticky="w", padx=(0, 16), pady=(0, 14))

        act = ctk.CTkFrame(parent, fg_color="transparent")
        act.grid(row=5, column=0, sticky="ew", padx=20, pady=(0, 20))
        act.grid_columnconfigure(0, weight=1)

        self._play_btn = ctk.CTkButton(
            act, text="▶   Set as Wallpaper",
            height=46, fg_color=_ACCENT, hover_color=_ACCENT_D,
            text_color="#ffffff", font=ctk.CTkFont("Segoe UI", 13, "bold"),
            corner_radius=10, command=self._apply,
        )
        self._play_btn.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self._stop_btn = ctk.CTkButton(
            act, text="■   Stop & Restore Desktop",
            height=38, fg_color=_CARD2, hover_color=_DANGER,
            text_color=_FG2, font=ctk.CTkFont("Segoe UI", 12),
            corner_radius=10, state="disabled", command=self._stop,
        )
        self._stop_btn.grid(row=1, column=0, sticky="ew")

    # ─────────────────────────────────────────────────────────────────────────
    #  Tab: Settings
    # ─────────────────────────────────────────────────────────────────────────
    def _build_tab_settings(self, parent: ctk.CTkFrame) -> None:
        # Make it scrollable for all the settings
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(parent, fg_color=_BG)
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        container = scroll  # All sections go inside scrollable frame

        def _section(row: int, title: str, pad_top: int = 16):
            hdr = ctk.CTkFrame(container, fg_color="transparent")
            hdr.grid(row=row, column=0, sticky="ew", padx=20, pady=(pad_top, 6))
            hdr.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(hdr, text=title.upper(),
                         font=ctk.CTkFont("Segoe UI", 9, "bold"),
                         text_color=_FG3).grid(row=0, column=0, sticky="w")
            ctk.CTkFrame(hdr, fg_color=_BORDER, height=1, corner_radius=0
                         ).grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=6)
            card = ctk.CTkFrame(container, fg_color=_CARD, corner_radius=12)
            card.grid(row=row+1, column=0, sticky="ew", padx=20, pady=(0, 4))
            card.grid_columnconfigure(1, weight=1)
            return card

        def _lbl(card, text, r):
            ctk.CTkLabel(card, text=text, font=ctk.CTkFont("Segoe UI", 12),
                         text_color=_FG, anchor="w", width=160
                         ).grid(row=r, column=0, sticky="w", padx=(16, 0), pady=12)

        # ── Audio ─────────────────────────────────────────────────────────
        ac = _section(0, "Audio")
        self._mute_var = tk.BooleanVar(value=self._settings.get("mute", True))
        _lbl(ac, "Mute audio", 0)
        ctk.CTkSwitch(ac, text="", variable=self._mute_var,
                       progress_color=_ACCENT, button_color=_FG,
                       command=self._on_mute_toggle
                       ).grid(row=0, column=1, sticky="w", padx=12, pady=12)

        self._vol_var = tk.IntVar(value=self._settings.get("volume", 0))
        _lbl(ac, "Volume", 1)
        vrow = ctk.CTkFrame(ac, fg_color="transparent")
        vrow.grid(row=1, column=1, sticky="ew", padx=(0, 16), pady=8)
        vrow.grid_columnconfigure(0, weight=1)
        self._vol_lbl = ctk.CTkLabel(vrow, text=f"{self._vol_var.get()}%",
                                      font=ctk.CTkFont("Segoe UI", 11, "bold"),
                                      text_color=_ACCENT, width=50)
        self._vol_lbl.grid(row=0, column=1, padx=(8, 0))
        ctk.CTkSlider(vrow, from_=0, to=200, variable=self._vol_var,
                       progress_color=_ACCENT, button_color=_FG,
                       fg_color=_BORDER, command=self._on_volume_change
                       ).grid(row=0, column=0, sticky="ew")

        # ── Playback ─────────────────────────────────────────────────────
        pb = _section(2, "Playback", pad_top=8)
        self._speed_var = tk.DoubleVar(value=self._settings.get("speed", 1.0))
        _lbl(pb, "Speed", 0)
        srow = ctk.CTkFrame(pb, fg_color="transparent")
        srow.grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=8)
        srow.grid_columnconfigure(0, weight=1)
        self._speed_lbl = ctk.CTkLabel(srow, text=f"{self._speed_var.get():.1f}×",
                                        font=ctk.CTkFont("Segoe UI", 11, "bold"),
                                        text_color=_ACCENT, width=50)
        self._speed_lbl.grid(row=0, column=1, padx=(8, 0))
        sp_sl = ctk.CTkSlider(srow, from_=10, to=400, progress_color=_ACCENT,
                               button_color=_FG, fg_color=_BORDER,
                               command=self._on_speed_change)
        sp_sl.set(self._speed_var.get() * 100)
        sp_sl.grid(row=0, column=0, sticky="ew")

        self._loop_var = tk.BooleanVar(value=self._settings.get("loop", True))
        _lbl(pb, "Loop video", 1)
        ctk.CTkSwitch(pb, text="", variable=self._loop_var,
                       progress_color=_ACCENT, button_color=_FG,
                       command=self._save_settings
                       ).grid(row=1, column=1, sticky="w", padx=12, pady=12)

        # ── Persistence ──────────────────────────────────────────────────
        pc = _section(4, "Persistence & Startup", pad_top=8)

        # Keep wallpaper after close
        self._persist_var = tk.BooleanVar(
            value=self._settings.get("persist_on_close", False)
        )
        _lbl(pc, "Keep wallpaper\nafter app closes", 0)
        persist_row = ctk.CTkFrame(pc, fg_color="transparent")
        persist_row.grid(row=0, column=1, sticky="ew", padx=12, pady=12)
        ctk.CTkSwitch(
            persist_row, text="", variable=self._persist_var,
            progress_color=_SUCCESS, button_color=_FG,
            command=self._on_persist_toggle,
        ).pack(side="left")
        ctk.CTkLabel(
            persist_row,
            text="  Runs in background when you close the app",
            font=ctk.CTkFont("Segoe UI", 9), text_color=_FG3,
        ).pack(side="left", padx=(8, 0))

        # Auto-resume on app launch
        self._auto_start_var = tk.BooleanVar(
            value=self._settings.get("auto_start", False)
        )
        _lbl(pc, "Auto-resume on\napp launch", 1)
        auto_row = ctk.CTkFrame(pc, fg_color="transparent")
        auto_row.grid(row=1, column=1, sticky="ew", padx=12, pady=12)
        ctk.CTkSwitch(
            auto_row, text="", variable=self._auto_start_var,
            progress_color=_SUCCESS, button_color=_FG,
            command=self._on_auto_start_toggle,
        ).pack(side="left")
        ctk.CTkLabel(
            auto_row,
            text="  Auto-play last wallpaper when app opens",
            font=ctk.CTkFont("Segoe UI", 9), text_color=_FG3,
        ).pack(side="left", padx=(8, 0))

        # ── NEW: Survive Reboot ──────────────────────────────────────────
        self._reboot_var = tk.BooleanVar(
            value=self._settings.get("survive_reboot", False)
        )
        _lbl(pc, "Survive restart /\nshutdown", 2)
        reboot_row = ctk.CTkFrame(pc, fg_color="transparent")
        reboot_row.grid(row=2, column=1, sticky="ew", padx=12, pady=12)
        ctk.CTkSwitch(
            reboot_row, text="", variable=self._reboot_var,
            progress_color=_WARN, button_color=_FG,
            command=self._on_reboot_toggle,
        ).pack(side="left")
        self._reboot_desc = ctk.CTkLabel(
            reboot_row,
            text="  Wallpaper auto-starts when Windows boots",
            font=ctk.CTkFont("Segoe UI", 9), text_color=_FG3,
        )
        self._reboot_desc.pack(side="left", padx=(8, 0))

        # Startup status indicator
        self._startup_status = ctk.CTkLabel(
            pc, text="", font=ctk.CTkFont("Segoe UI", 9), text_color=_FG3,
        )
        self._startup_status.grid(row=3, column=0, columnspan=2,
                                   sticky="w", padx=16, pady=(2, 4))
        self._update_startup_status()

        # Stop persistent / kill button
        self._kill_persist_btn = ctk.CTkButton(
            pc, text="⏹  Stop Background Wallpaper & Remove Startup",
            width=340, height=32,
            fg_color=_DANGER, hover_color="#d04040",
            text_color="#ffffff", font=ctk.CTkFont("Segoe UI", 11, "bold"),
            corner_radius=8, command=self._kill_persistent,
        )
        self._kill_persist_btn.grid(row=4, column=0, columnspan=2,
                                     padx=16, pady=(4, 8), sticky="w")

        # Persist process status
        self._persist_status = ctk.CTkLabel(
            pc, text="", font=ctk.CTkFont("Segoe UI", 9), text_color=_FG3,
        )
        self._persist_status.grid(row=5, column=0, columnspan=2,
                                   sticky="w", padx=16, pady=(0, 14))
        self._update_persist_status()
        # ── END PERSISTENCE ──────────────────────────────────────────────

        # ── VLC Engine ───────────────────────────────────────────────────
        vc = _section(6, "VLC Engine", pad_top=8)
        vc.grid_columnconfigure(1, weight=1)
        vlc_info = f"Found:  {_vlc_folder}" if vlc_is_ready() \
                   else "Not found — will download portable copy on first use"
        self._vlc_info_lbl = ctk.CTkLabel(
            vc, text=vlc_info, font=ctk.CTkFont("Segoe UI", 10),
            text_color=_SUCCESS if vlc_is_ready() else _WARN,
            anchor="w", wraplength=440,
        )
        self._vlc_info_lbl.grid(row=0, column=0, sticky="w", padx=16, pady=14)
        ctk.CTkButton(vc, text="Re-scan", width=84, height=30,
                       fg_color=_CARD2, hover_color=_BORDER, text_color=_FG,
                       font=ctk.CTkFont("Segoe UI", 11), corner_radius=8,
                       command=self._rescan_vlc
                       ).grid(row=0, column=1, sticky="e", padx=16, pady=14)

        # ── Data ─────────────────────────────────────────────────────────
        dc = _section(8, "Data", pad_top=8)
        dc.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(dc, text=f"Settings & logs:  {DATA_DIR}",
                     font=ctk.CTkFont("Segoe UI", 10), text_color=_FG2, anchor="w"
                     ).grid(row=0, column=0, columnspan=2, sticky="w",
                            padx=16, pady=(14, 6))
        dr = ctk.CTkFrame(dc, fg_color="transparent")
        dr.grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 14))
        ctk.CTkButton(dr, text="Open Data Folder", width=140, height=30,
                       fg_color=_CARD2, hover_color=_BORDER, text_color=_FG,
                       font=ctk.CTkFont("Segoe UI", 11), corner_radius=8,
                       command=lambda: os.startfile(str(DATA_DIR))
                       ).pack(side="left", padx=(4, 8))
        ctk.CTkButton(dr, text="Clear Recent", width=120, height=30,
                       fg_color=_CARD2, hover_color=_BORDER, text_color=_FG,
                       font=ctk.CTkFont("Segoe UI", 11), corner_radius=8,
                       command=self._clear_recent
                       ).pack(side="left")

    # ─────────────────────────────────────────────────────────────────────────
    #  Tab: Monitor
    # ─────────────────────────────────────────────────────────────────────────
    def _build_tab_monitor(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure((0, 1), weight=1)

        def _metric_card(col, padx):
            c = ctk.CTkFrame(parent, fg_color=_CARD, corner_radius=14)
            c.grid(row=0, column=col, sticky="nsew", padx=padx, pady=(16, 8))
            c.grid_columnconfigure(0, weight=1)
            return c

        cpu = _metric_card(0, (20, 8))
        cpu_top = ctk.CTkFrame(cpu, fg_color="transparent")
        cpu_top.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 0))
        cpu_top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(cpu_top, text="CPU", font=ctk.CTkFont("Segoe UI", 11, "bold"),
                     text_color=_FG2).grid(row=0, column=0, sticky="w")
        self._cpu_status_lbl = ctk.CTkLabel(cpu_top, text="",
                                             font=ctk.CTkFont("Segoe UI", 10),
                                             text_color=_SUCCESS)
        self._cpu_status_lbl.grid(row=0, column=1, sticky="e")

        self._cpu_pct_lbl = ctk.CTkLabel(cpu, text="—%",
                                          font=ctk.CTkFont("Segoe UI Light", 34),
                                          text_color=_FG)
        self._cpu_pct_lbl.grid(row=1, column=0, sticky="w", padx=16, pady=(4, 6))

        self._cpu_bar_var = tk.DoubleVar(value=0)
        self._cpu_progress = ctk.CTkProgressBar(cpu, variable=self._cpu_bar_var,
                                                  height=5, corner_radius=3,
                                                  fg_color=_BORDER,
                                                  progress_color=_SUCCESS)
        self._cpu_progress.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))

        self._cpu_canvas = tk.Canvas(cpu, bg=_CARD, height=48,
                                      highlightthickness=0, bd=0)
        self._cpu_canvas.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 10))

        cf = ctk.CTkFrame(cpu, fg_color="transparent")
        cf.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 14))
        self._core_bars: list = []
        cores = psutil.cpu_count(logical=True) or 4
        cols  = min(cores, 8)
        for i in range(cores):
            c2 = i % cols; r2 = i // cols
            f = ctk.CTkFrame(cf, fg_color="transparent")
            f.grid(row=r2*2, column=c2, padx=2, pady=(2, 0))
            ctk.CTkLabel(f, text=f"C{i}", font=ctk.CTkFont("Segoe UI", 7),
                         text_color=_FG3, width=20).pack()
            pb_core = ctk.CTkProgressBar(cf, width=18, height=28,
                                     orientation="vertical", corner_radius=3,
                                     fg_color=_BORDER, progress_color=_SUCCESS)
            pb_core.grid(row=r2*2+1, column=c2, padx=2, pady=(0, 2))
            pb_core.set(0)
            self._core_bars.append(pb_core)
        for c3 in range(cols):
            cf.grid_columnconfigure(c3, weight=1)

        ram_card = _metric_card(1, (8, 20))
        ram_top = ctk.CTkFrame(ram_card, fg_color="transparent")
        ram_top.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 0))
        ram_top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(ram_top, text="RAM", font=ctk.CTkFont("Segoe UI", 11, "bold"),
                     text_color=_FG2).grid(row=0, column=0, sticky="w")
        self._ram_status_lbl = ctk.CTkLabel(ram_top, text="",
                                             font=ctk.CTkFont("Segoe UI", 10),
                                             text_color=_SUCCESS)
        self._ram_status_lbl.grid(row=0, column=1, sticky="e")

        self._ram_mb_lbl = ctk.CTkLabel(ram_card, text="—",
                                         font=ctk.CTkFont("Segoe UI Light", 34),
                                         text_color=_FG)
        self._ram_mb_lbl.grid(row=1, column=0, sticky="w", padx=16, pady=(4, 6))

        self._ram_bar_var = tk.DoubleVar(value=0)
        self._ram_progress = ctk.CTkProgressBar(ram_card, variable=self._ram_bar_var,
                                                  height=5, corner_radius=3,
                                                  fg_color=_BORDER,
                                                  progress_color=_ACCENT)
        self._ram_progress.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))

        self._ram_canvas = tk.Canvas(ram_card, bg=_CARD, height=48,
                                      highlightthickness=0, bd=0)
        self._ram_canvas.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 8))

        ram = psutil.virtual_memory()
        ctk.CTkLabel(ram_card, text=f"Total  {ram.total/1024**3:.1f} GB",
                     font=ctk.CTkFont("Segoe UI", 9), text_color=_FG3
                     ).grid(row=4, column=0, sticky="w", padx=16, pady=(0, 14))

        tip = ctk.CTkFrame(parent, fg_color=_CARD2, corner_radius=10)
        tip.grid(row=1, column=0, columnspan=2, sticky="ew", padx=20, pady=(4, 8))
        tip.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(tip, text="ℹ", font=ctk.CTkFont("Segoe UI", 12),
                     text_color=_ACCENT, width=28
                     ).grid(row=0, column=0, padx=(14, 4), pady=10)
        self._health_lbl = ctk.CTkLabel(tip, text="Idle — no wallpaper active",
                                         font=ctk.CTkFont("Segoe UI", 10),
                                         text_color=_FG2, anchor="w", wraplength=600)
        self._health_lbl.grid(row=0, column=1, sticky="w", padx=(0, 14), pady=10)

        proc = ctk.CTkFrame(parent, fg_color=_CARD, corner_radius=10)
        proc.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 16))
        proc.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(proc, text="PROCESS",
                     font=ctk.CTkFont("Segoe UI", 8, "bold"), text_color=_FG3
                     ).grid(row=0, column=0, sticky="w", padx=16, pady=10)
        self._proc_lbl = ctk.CTkLabel(proc, text="Not playing",
                                       font=ctk.CTkFont("Segoe UI", 10),
                                       text_color=_FG3, anchor="w")
        self._proc_lbl.grid(row=0, column=1, sticky="w", padx=(4, 16), pady=10)

    # ─────────────────────────────────────────────────────────────────────────
    #  Tab: Log
    # ─────────────────────────────────────────────────────────────────────────
    def _build_tab_log(self, parent: ctk.CTkFrame) -> None:
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        ctrl = ctk.CTkFrame(parent, fg_color="transparent")
        ctrl.grid(row=0, column=0, sticky="w", padx=20, pady=(14, 8))
        ctk.CTkButton(ctrl, text="⟳  Refresh", width=100, height=32,
                       fg_color=_CARD2, hover_color=_BORDER, text_color=_FG,
                       font=ctk.CTkFont("Segoe UI", 11), corner_radius=8,
                       command=self._refresh_log
                       ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(ctrl, text="Clear Log", width=90, height=32,
                       fg_color=_CARD2, hover_color=_DANGER, text_color=_FG2,
                       font=ctk.CTkFont("Segoe UI", 11), corner_radius=8,
                       command=self._clear_log
                       ).pack(side="left")

        lf = ctk.CTkFrame(parent, fg_color=_CARD, corner_radius=12)
        lf.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 20))
        lf.grid_rowconfigure(0, weight=1)
        lf.grid_columnconfigure(0, weight=1)

        self._log_text = tk.Text(
            lf, bg=_CARD, fg=_FG, insertbackground=_FG,
            font=("Consolas", 9), relief="flat", bd=0,
            wrap="none", state="disabled", selectbackground=_CARD2,
        )
        self._log_text.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=8)

        log_sb = ctk.CTkScrollbar(lf, command=self._log_text.yview,
                                    fg_color=_CARD, button_color=_BORDER,
                                    button_hover_color=_FG3)
        log_sb.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=8)
        self._log_text.configure(yscrollcommand=log_sb.set)

        self._log_text.tag_config("ERR",  foreground=_DANGER)
        self._log_text.tag_config("WARN", foreground=_WARN)
        self._log_text.tag_config("INFO", foreground=_FG2)
        self._log_text.tag_config("DBG",  foreground=_FG3)

        self.after(200, self._refresh_log)

    # ══════════════════════════════════════════════════════════════════════════
    #  HELPERS
    # ══════════════════════════════════════════════════════════════════════════
    def _refresh_vlc_pill(self) -> None:
        if vlc_is_ready():
            self._vlc_pill.configure(text="  ● VLC ready  ", text_color=_SUCCESS)
        else:
            self._vlc_pill.configure(text="  ● VLC not found  ", text_color=_WARN)

    def _async_scan(self) -> None:
        if self._scan_thread and self._scan_thread.is_alive():
            return
        self._list_status.configure(text="Scanning for media files…", text_color=_FG2)
        self._listbox.delete(0, "end")
        self._file_map.clear()
        self._listbox.insert("end", "  Scanning…")

        def _worker():
            files = find_media_files()
            self.after(0, lambda: self._on_scan_done(files))

        self._scan_thread = threading.Thread(target=_worker, daemon=True)
        self._scan_thread.start()

    def _on_scan_done(self, files: list[Path]) -> None:
        self._listbox.delete(0, "end")
        self._file_map.clear()
        if files:
            for f in files:
                ext   = f.suffix.upper().lstrip(".")
                size  = fmt_size(f)
                label = f"  {f.name}   ·   {ext}  ·  {size}   —   {f.parent}"
                self._listbox.insert("end", label)
                self._file_map[label] = f
            self._list_status.configure(text=f"{len(files)} file(s) found",
                                         text_color=_FG3)
        else:
            self._listbox.insert("end", "  No media files found — use Browse")
            self._list_status.configure(text="No files found — use Browse",
                                         text_color=_WARN)

    def _add_recent(self, path: Path) -> None:
        recent: list[str] = self._settings.get("recent_files", [])
        s = str(path)
        if s in recent:
            recent.remove(s)
        recent.insert(0, s)
        self._settings["recent_files"] = recent[:MAX_RECENT]
        self._save_settings()

    def _show_recent_menu(self) -> None:
        recent = [Path(p) for p in self._settings.get("recent_files", [])
                  if Path(p).is_file()]
        if not recent:
            self._set_status("No recent files yet.")
            return
        menu = tk.Menu(self, tearoff=False,
                        bg=_CARD, fg=_FG,
                        activebackground=_ACCENT_D,
                        activeforeground="#fff",
                        font=("Segoe UI", 10), relief="flat")
        for p in recent:
            menu.add_command(
                label=f"  {p.name}   —   {p.parent}",
                command=lambda _p=p: self._set_selected(_p),
            )
        menu.add_separator()
        menu.add_command(label="  Clear recent files", command=self._clear_recent)
        menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())

    def _clear_recent(self) -> None:
        self._settings["recent_files"] = []
        self._save_settings()
        self._set_status("Recent files cleared.")

    def _on_list_select(self, _=None) -> None:
        sel = self._listbox.curselection()
        if sel:
            label = self._listbox.get(sel[0])
            if label in self._file_map:
                self._set_selected(self._file_map[label])

    def _browse(self) -> None:
        exts = " ".join(f"*{e}" for e in SUPPORTED)
        path = filedialog.askopenfilename(
            title="Select a video or GIF",
            filetypes=[("Media files", exts), ("All files", "*.*")],
        )
        if path:
            self._set_selected(Path(path))

    def _apply_from_entry(self) -> None:
        raw = self._path_entry.get().strip().strip('"')
        if not raw:
            return
        p = Path(raw)
        if not p.is_file():
            self._set_status(f"File not found: {p.name}", error=True)
            return
        self._set_selected(p)
        self._apply()

    def _set_selected(self, p: Path) -> None:
        self._selected_path = p
        self._path_entry.delete(0, "end")
        self._path_entry.insert(0, str(p))
        ext  = p.suffix.upper().lstrip(".")
        size = fmt_size(p)
        self._prev_icon.configure(text="🎞️" if p.suffix.lower() == ".gif" else "🎬")
        self._prev_name.configure(text=p.name)
        self._prev_meta.configure(text=f"{ext}  ·  {size}  ·  {p.parent}")
        self._set_status(f"Selected: {p.name}")

    # ══════════════════════════════════════════════════════════════════════════
    #  PERSISTENCE & STARTUP CALLBACKS
    # ══════════════════════════════════════════════════════════════════════════
    def _on_persist_toggle(self) -> None:
        self._settings["persist_on_close"] = self._persist_var.get()
        self._save_settings()
        state = "enabled" if self._persist_var.get() else "disabled"
        self._set_status(f"Persist on close: {state}")
        self._update_persist_status()
        log.info("Persist on close: %s", state)

    def _on_auto_start_toggle(self) -> None:
        self._settings["auto_start"] = self._auto_start_var.get()
        self._save_settings()
        state = "enabled" if self._auto_start_var.get() else "disabled"
        self._set_status(f"Auto-resume on launch: {state}")
        log.info("Auto-resume: %s", state)

    def _on_reboot_toggle(self) -> None:
        enabled = self._reboot_var.get()
        self._settings["survive_reboot"] = enabled

        if enabled:
            # persist_on_close is required for reboot survival
            self._persist_var.set(True)
            self._settings["persist_on_close"] = True

            # Write persist.json RIGHT NOW so that a forced reboot (Windows
            # Update, power cut) before the user closes the app still has a
            # valid state file to load on next boot.
            if self._is_playing and _ws.path:
                save_persist_state(
                    playing=True,
                    video_path=_ws.path,
                    mute=self._mute_var.get(),
                    volume=self._vol_var.get(),
                    speed=self._speed_var.get(),
                    loop=self._loop_var.get(),
                )
                log.info("Persist state written eagerly for reboot-survive.")

            ok = add_to_startup()
            if ok:
                self._set_status("✓ Startup shortcut created — wallpaper will survive reboots")
                log.info("Startup shortcut added to shell:startup.")
            else:
                self._reboot_var.set(False)
                self._settings["survive_reboot"] = False
                self._set_status("✗ Failed to create startup shortcut", error=True)
        else:
            ok = remove_from_startup()
            if ok:
                self._set_status("Startup shortcut removed")
                log.info("Startup shortcut removed from shell:startup.")
            else:
                self._set_status("⚠ Could not remove startup shortcut", error=True)

        self._save_settings()
        self._update_startup_status()
        self._update_persist_status()

    def _update_startup_status(self) -> None:
        in_startup = is_in_startup()
        if in_startup:
            self._startup_status.configure(
                text=f"✓  Startup shortcut active  ({_STARTUP_LNK.name})",
                text_color=_SUCCESS,
            )
            # Sync toggle if the shortcut exists but toggle is off
            # (e.g. user opened the Startup folder and didn't delete it)
            self._reboot_var.set(True)
        else:
            self._startup_status.configure(
                text="No startup shortcut — wallpaper won't auto-start on boot",
                text_color=_FG3,
            )

    def _update_persist_status(self) -> None:
        state = load_persist_state()
        if state and state.get("playing", False):
            pid = state.get("pid", 0)
            video = Path(state.get("video_path", "")).name or "unknown"
            alive = False
            try:
                alive = psutil.pid_exists(pid) if pid else False
            except Exception:
                pass
            if alive:
                self._persist_status.configure(
                    text=f"🟢  Background process running (PID {pid}) — {video}",
                    text_color=_SUCCESS,
                )
            else:
                self._persist_status.configure(
                    text="⚠  Persist file exists but process not running",
                    text_color=_WARN,
                )
        else:
            self._persist_status.configure(
                text="No background wallpaper process running",
                text_color=_FG3,
            )

    def _kill_persistent(self) -> None:
        kill_existing_headless()
        stop_wallpaper(restore_wp=True)
        remove_from_startup()
        self._reboot_var.set(False)
        self._settings["survive_reboot"] = False
        self._settings["persist_on_close"] = False
        self._persist_var.set(False)
        self._save_settings()
        self._update_persist_status()
        self._update_startup_status()
        self._set_status("Background wallpaper stopped, startup removed, desktop restored.")

    # ══════════════════════════════════════════════════════════════════════════
    #  APPLY / STOP
    # ══════════════════════════════════════════════════════════════════════════
    def _apply(self) -> None:
        if not self._selected_path:
            messagebox.showwarning("No file", "Select a video or GIF first.",
                                    parent=self)
            return
        if not self._selected_path.is_file():
            messagebox.showwarning("File not found",
                                    f"Cannot find:\n{self._selected_path}\n\n"
                                    "It may have been moved or deleted.",
                                    parent=self)
            return
        if not vlc_is_ready():
            self._download_then_play(self._selected_path)
        else:
            self._launch(self._selected_path)

    def _download_then_play(self, path: Path) -> None:
        self._play_btn.configure(state="disabled")

        overlay = ctk.CTkToplevel(self)
        overlay.title("Downloading VLC")
        overlay.geometry("440x160")
        overlay.resizable(False, False)
        overlay.configure(fg_color=_CARD)
        overlay.grab_set()
        overlay.transient(self)
        overlay.protocol("WM_DELETE_WINDOW", lambda: None)

        ctk.CTkLabel(overlay, text="Downloading portable VLC  (~40 MB)",
                     font=ctk.CTkFont("Segoe UI", 12, "bold"),
                     text_color=_FG).pack(pady=(22, 4))
        ctk.CTkLabel(overlay, text="One-time download, stored in AppData.",
                     font=ctk.CTkFont("Segoe UI", 10), text_color=_FG2).pack()

        dl_var = tk.DoubleVar(value=0)
        ctk.CTkProgressBar(overlay, variable=dl_var,
                           fg_color=_BORDER, progress_color=_ACCENT,
                           width=380, height=8, corner_radius=4).pack(pady=(14, 4))
        dl_lbl = ctk.CTkLabel(overlay, text="Starting…",
                               font=ctk.CTkFont("Segoe UI", 9), text_color=_FG2)
        dl_lbl.pack()

        def on_progress(pct: int) -> None:
            dl_var.set(pct / 100)
            dl_lbl.configure(text=f"{pct}% downloaded")
            self._set_status(f"Downloading VLC… {pct}%")

        def on_done(ok: bool, err: Optional[str]) -> None:
            overlay.grab_release()
            overlay.destroy()
            self._play_btn.configure(state="normal")
            self._refresh_vlc_pill()
            if ok:
                self._update_vlc_info()
                self._set_status("VLC ready — starting wallpaper…")
                self.after(200, lambda: self._launch(path))
            else:
                log.error("VLC download failed: %s", err)
                messagebox.showerror("Download Failed",
                                      f"Could not download VLC:\n\n{err}\n\n"
                                      "Check your connection and try again.",
                                      parent=self)
                self._set_status("Download failed.", error=True)

        threading.Thread(
            target=download_vlc,
            args=(
                lambda p: self.after(0, lambda: on_progress(p)),
                lambda ok, e: self.after(0, lambda: on_done(ok, e)),
            ),
            daemon=True,
        ).start()

    def _launch(self, path: Path) -> None:
        try:
            self._set_status("Starting wallpaper…")
            start_wallpaper(
                path,
                mute=self._mute_var.get(),
                volume=self._vol_var.get(),
                speed=self._speed_var.get(),
                loop=self._loop_var.get(),
                status_cb=lambda s: self.after(0, lambda: self._on_playing(s)),
            )
            self._is_playing = True
            self._play_btn.configure(state="disabled", fg_color=_BORDER,
                                      text_color=_FG3, text="▶   Wallpaper Active")
            self._stop_btn.configure(state="normal", fg_color=_DANGER,
                                      text_color="#fff")
            self._add_recent(path)
            self._settings["last_file"] = str(path)
            self._save_settings()

            # If survive_reboot is enabled, make sure startup entry exists
            if self._reboot_var.get():
                add_to_startup()

        except Exception as exc:
            log.error("Failed to start wallpaper: %s", exc, exc_info=True)
            messagebox.showerror("Playback Error",
                                  f"Could not start wallpaper:\n\n{exc}",
                                  parent=self)
            self._set_status(f"Error: {exc}", error=True)
            self._play_btn.configure(state="normal")

    def _on_playing(self, msg: str) -> None:
        self._set_status(msg)
        self._status_dot.configure(text_color=_SUCCESS)
        self._playing_lbl.configure(text=f"▶  {_ws.path.name if _ws.path else ''}")

    def _stop(self) -> None:
        try:
            stop_wallpaper(restore_wp=True)
        except Exception as exc:
            log.error("stop_wallpaper error: %s", exc)
        self._is_playing = False
        self._play_btn.configure(state="normal", fg_color=_ACCENT,
                                  text_color="#ffffff", text="▶   Set as Wallpaper")
        self._stop_btn.configure(state="disabled", fg_color=_CARD2, text_color=_FG2)
        self._status_dot.configure(text_color=_FG3)
        self._playing_lbl.configure(text="")
        self._set_status("Wallpaper stopped — desktop restored.")

    # ══════════════════════════════════════════════════════════════════════════
    #  SETTINGS CALLBACKS
    # ══════════════════════════════════════════════════════════════════════════
    def _on_mute_toggle(self) -> None:
        update_playback(mute=self._mute_var.get())
        self._settings["mute"] = self._mute_var.get()
        self._save_settings()

    def _on_volume_change(self, val) -> None:
        v = int(float(val))
        self._vol_lbl.configure(text=f"{v}%")
        update_playback(volume=v)
        self._settings["volume"] = v
        self._save_settings()

    def _on_speed_change(self, val) -> None:
        speed = round(float(val) / 100, 1)
        self._speed_lbl.configure(text=f"{speed:.1f}×")
        self._speed_var.set(speed)
        update_playback(speed=speed)
        self._settings["speed"] = speed
        self._save_settings()

    def _save_settings(self) -> None:
        self._settings["mute"]             = self._mute_var.get()
        self._settings["volume"]           = self._vol_var.get()
        self._settings["loop"]             = self._loop_var.get()
        self._settings["persist_on_close"] = self._persist_var.get()
        self._settings["auto_start"]       = self._auto_start_var.get()
        self._settings["survive_reboot"]   = self._reboot_var.get()
        save_settings(self._settings)

    def _rescan_vlc(self) -> None:
        self._set_status("Scanning for VLC…")
        def _worker():
            global _vlc_folder
            _vlc_folder = find_vlc_folder()
            self.after(0, self._on_vlc_rescan_done)
        threading.Thread(target=_worker, daemon=True).start()

    def _on_vlc_rescan_done(self) -> None:
        self._refresh_vlc_pill()
        self._update_vlc_info()
        if vlc_is_ready():
            self._set_status(f"VLC found at {_vlc_folder}")
        else:
            self._set_status("VLC not found on this system.", error=True)

    def _update_vlc_info(self) -> None:
        if vlc_is_ready():
            self._vlc_info_lbl.configure(text=f"Found:  {_vlc_folder}",
                                          text_color=_SUCCESS)
        else:
            self._vlc_info_lbl.configure(
                text="Not found — will auto-download on first use.",
                text_color=_WARN,
            )

    # ══════════════════════════════════════════════════════════════════════════
    #  LOG TAB
    # ══════════════════════════════════════════════════════════════════════════
    def _refresh_log(self) -> None:
        try:
            content = LOG_FILE.read_text(encoding="utf-8", errors="replace") \
                      if LOG_FILE.is_file() else "(log file is empty)"
        except Exception as exc:
            content = f"(could not read log: {exc})"
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        for line in content.splitlines():
            tag = "INFO"
            ll  = line.lower()
            if "[error]"    in ll: tag = "ERR"
            elif "[warning]" in ll: tag = "WARN"
            elif "[debug]"   in ll: tag = "DBG"
            self._log_text.insert("end", line + "\n", tag)
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        if messagebox.askyesno("Clear Log", "Delete the log file?", parent=self):
            try:
                LOG_FILE.write_text("", encoding="utf-8")
                self._refresh_log()
            except Exception as exc:
                messagebox.showerror("Error", str(exc), parent=self)

    # ══════════════════════════════════════════════════════════════════════════
    #  LIVE STATS
    # ══════════════════════════════════════════════════════════════════════════
    def _update_stats(self) -> None:
        if not self.winfo_exists():
            return

        cpu_per = psutil.cpu_percent(interval=None, percpu=True) or [0.0]
        cpu_pct = sum(cpu_per) / len(cpu_per)
        self._cpu_hist.append(cpu_pct); self._cpu_hist.pop(0)
        cpu_col = lerp_color(cpu_pct)
        self._cpu_pct_lbl.configure(text=f"{cpu_pct:.0f}%", text_color=cpu_col)
        self._cpu_bar_var.set(cpu_pct / 100)
        self._cpu_progress.configure(progress_color=cpu_col)

        if cpu_pct < 5:    cl, cc = "● Idle",     _SUCCESS
        elif cpu_pct < 20: cl, cc = "● Normal",   _SUCCESS
        elif cpu_pct < 50: cl, cc = "● Moderate", _WARN
        elif cpu_pct < 80: cl, cc = "● High",     _WARN
        else:              cl, cc = "● Critical",  _DANGER
        self._cpu_status_lbl.configure(text=cl, text_color=cc)

        for i, pb in enumerate(self._core_bars):
            pb.set((cpu_per[i] if i < len(cpu_per) else 0) / 100)

        ram = psutil.virtual_memory()
        ram_pct  = ram.percent
        ram_used = ram.used / 1024**3
        self._ram_hist.append(ram_pct); self._ram_hist.pop(0)
        ram_col = lerp_color(ram_pct)
        self._ram_mb_lbl.configure(text=f"{ram_used:.2f} GB", text_color=ram_col)
        self._ram_bar_var.set(ram_pct / 100)
        self._ram_progress.configure(progress_color=ram_col)

        if ram_pct < 50:   rl, rc = "● Good",     _SUCCESS
        elif ram_pct < 75: rl, rc = "● Moderate", _WARN
        else:              rl, rc = "● High",      _DANGER
        self._ram_status_lbl.configure(text=rl, text_color=rc)

        if cpu_pct > 80:
            tip = "⚠  CPU critical — try a lighter codec or close other apps"
        elif cpu_pct > 40:
            tip = "⚠  High CPU — 1080p H.264 MP4 gives best performance"
        elif ram_pct > 85:
            tip = "⚠  RAM pressure — close unused applications"
        elif cpu_pct < 5 and self._is_playing:
            tip = "✓  GPU hardware decoding active — very efficient"
        elif self._is_playing:
            tip = "✓  Wallpaper running smoothly"
        else:
            tip = "Idle — no wallpaper active"
        self._health_lbl.configure(text=tip)

        self._draw_sparkline(self._cpu_canvas, self._cpu_hist, cpu_col)
        self._draw_sparkline(self._ram_canvas, self._ram_hist, ram_col)

        with _ws_lock:
            playing = _ws.player is not None
        if playing:
            try:
                proc = psutil.Process(os.getpid())
                pmem = proc.memory_info().rss / 1024**2
                pcpu = proc.cpu_percent(interval=None)
                self._proc_lbl.configure(
                    text=f"CPU {pcpu:.1f}%   ·   Memory {pmem:.1f} MB   ·   "
                         f"{_ws.path.name if _ws.path else '—'}",
                    text_color=_FG2,
                )
            except Exception:
                pass
        else:
            self._proc_lbl.configure(text="Not playing", text_color=_FG3)

        self._update_persist_status()
        self._stats_id = self.after(1000, self._update_stats)

    def _draw_sparkline(self, canvas: tk.Canvas,
                         history: list[float], color: str) -> None:
        canvas.delete("all")
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 4 or h < 4 or len(history) < 2:
            return
        n    = len(history)
        step = w / (n - 1)
        pts  = []
        for i, v in enumerate(history):
            pts.extend([i * step, h - max(1, (v / 100) * (h - 2))])
        canvas.create_polygon(pts + [w, h, 0, h],
                               fill=color, outline="", stipple="gray25")
        for i in range(n - 1):
            canvas.create_line(
                i * step,     h - max(1, (history[i]     / 100) * (h - 2)),
                (i+1) * step, h - max(1, (history[i + 1] / 100) * (h - 2)),
                fill=color, width=1.5, smooth=True,
            )

    # ══════════════════════════════════════════════════════════════════════════
    #  STATUS / LIFECYCLE
    # ══════════════════════════════════════════════════════════════════════════
    def _set_status(self, msg: str, *, error: bool = False) -> None:
        self._status_var.set(msg)
        self._status_dot.configure(
            text_color=_DANGER if error else
                       (_SUCCESS if self._is_playing else _FG3)
        )

    # ══════════════════════════════════════════════════════════════════════════
    #  SYSTEM TRAY
    # ══════════════════════════════════════════════════════════════════════════
    def _start_tray(self) -> None:
        """Create and run the system tray icon in a background thread."""
        menu = pystray.Menu(
            pystray.MenuItem("Show / Hide", self._tray_show_hide, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Stop Wallpaper", self._tray_stop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self._tray_icon = pystray.Icon(
            APP_NAME,
            icon=_make_tray_icon_image(),
            title=APP_NAME,
            menu=menu,
        )
        self._tray_thread = threading.Thread(
            target=self._tray_icon.run, daemon=True, name="tray-thread"
        )
        self._tray_thread.start()
        log.info("System tray icon started.")

    def _tray_show_hide(self, icon=None, item=None) -> None:
        """Toggle window visibility from the tray."""
        self.after(0, self._toggle_window)

    def _toggle_window(self) -> None:
        if self.winfo_viewable():
            self.withdraw()
        else:
            self.deiconify()
            self.lift()
            self.focus_force()

    def _tray_stop(self, icon=None, item=None) -> None:
        """Stop wallpaper from the tray menu."""
        self.after(0, self._stop)

    def _tray_quit(self, icon=None, item=None) -> None:
        """Fully quit the app from the tray menu."""
        self.after(0, self._quit_app)

    def _quit_app(self) -> None:
        """Destroy tray icon then close the window properly."""
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self.on_close()

    def on_close(self) -> None:
        log.info("Closing application.")
        if self._stats_id:
            self.after_cancel(self._stats_id)
            self._stats_id = None
        self._save_settings()

        persist = self._persist_var.get()

        if persist and self._is_playing and _ws.path:
            log.info("Persist enabled — spawning headless background process.")

            persist_data = {
                "video_path": str(_ws.path),
                "mute":       self._mute_var.get(),
                "volume":     self._vol_var.get(),
                "speed":      self._speed_var.get(),
                "loop":       self._loop_var.get(),
            }

            stop_wallpaper(restore_wp=False)
            spawn_headless_process(persist_data)

            # Make sure startup entry is set if survive_reboot is on
            if self._reboot_var.get():
                add_to_startup()

            self.destroy()
        else:
            try:
                stop_wallpaper(restore_wp=True)
            except Exception as exc:
                log.error("Error during shutdown stop: %s", exc)
            self.destroy()


# ──────────────────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────────────────
def main_gui() -> None:
    try:
        app = App()
        app.protocol("WM_DELETE_WINDOW", app.withdraw)   # X hides to tray
        signal.signal(signal.SIGINT, lambda *_: app.after(0, app._quit_app))
        log.info("App started — VLC folder: %s", _vlc_folder)
        app.mainloop()
    except Exception:
        traceback.print_exc()
        log.critical("Unhandled exception", exc_info=True)


def main_headless() -> None:
    """Called when script is run with --headless flag (on close or on boot)."""
    log.info("=== Headless mode started (PID %d) ===", os.getpid())

    state = load_persist_state()
    if state is None or not state.get("video_path"):
        # Check settings for last file (happens after reboot)
        settings = load_settings()
        last = settings.get("last_file", "")
        if last and Path(last).is_file():
            state = {
                "playing":    True,
                "video_path": last,
                "mute":       settings.get("mute", True),
                "volume":     settings.get("volume", 0),
                "speed":      settings.get("speed", 1.0),
                "loop":       settings.get("loop", True),
            }
            log.info("Headless: using last_file from settings: %s", last)
        else:
            log.error("Headless: no persist state and no last_file found.")
            return

    run_headless(state)


if __name__ == "__main__":
    if "--headless" in sys.argv:
        main_headless()
    else:
        main_gui()
