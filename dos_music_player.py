#!/usr/bin/env python3
"""DOS Music Player - retro Windows console audio player.

Supports MP3, WAV, M3U/M3U8 and PLS files.
Designed for Windows CMD/Windows Terminal and packageable with PyInstaller.
"""

from __future__ import annotations

import argparse
import configparser
import ctypes
import json
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

try:
    import pygame
except ImportError as exc:
    raise SystemExit(
        "pygame-ce is required. Install it with: python -m pip install pygame-ce"
    ) from exc

AUDIO_EXTENSIONS = {".mp3", ".wav"}
PLAYLIST_EXTENSIONS = {".m3u", ".m3u8", ".pls"}
SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | PLAYLIST_EXTENSIONS

ESC = "\x1b["
RESET = f"{ESC}0m"
BRIGHT_CYAN = f"{ESC}96m"
CYAN = f"{ESC}36m"
BRIGHT_GREEN = f"{ESC}92m"
GREEN = f"{ESC}32m"
YELLOW = f"{ESC}93m"
WHITE = f"{ESC}97m"
GRAY = f"{ESC}90m"
RED = f"{ESC}91m"
BG_BLUE = f"{ESC}44m"


def suspend_process(proc: subprocess.Popen) -> bool:
    """Suspend a Windows process immediately without relying on ffplay keyboard input."""
    if os.name != "nt" or proc.poll() is not None:
        return False
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x0800 | 0x0002, False, proc.pid)
        if not handle:
            return False
        try:
            return ctypes.windll.ntdll.NtSuspendProcess(handle) == 0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def resume_process(proc: subprocess.Popen) -> bool:
    """Resume a process previously suspended with suspend_process()."""
    if os.name != "nt" or proc.poll() is not None:
        return False
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x0800 | 0x0002, False, proc.pid)
        if not handle:
            return False
        try:
            return ctypes.windll.ntdll.NtResumeProcess(handle) == 0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def enable_ansi() -> None:
    """Enable ANSI escape processing in Windows consoles when possible."""
    if os.name != "nt":
        return
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)
    mode = ctypes.c_uint32()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def set_console_title(title: str) -> None:
    if os.name == "nt":
        ctypes.windll.kernel32.SetConsoleTitleW(title)


def set_console_font(face_name: str = "Consolas", height: int = 18) -> None:
    """Request a stable monospaced font in the classic Windows console.

    Windows Terminal controls fonts through its profile and may ignore this
    request, but its default Cascadia Mono is also panel-safe.
    """
    if os.name != "nt":
        return
    try:
        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class CONSOLE_FONT_INFOEX(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("nFont", ctypes.c_ulong),
                ("dwFontSize", COORD),
                ("FontFamily", ctypes.c_uint),
                ("FontWeight", ctypes.c_uint),
                ("FaceName", ctypes.c_wchar * 32),
            ]

        info = CONSOLE_FONT_INFOEX()
        info.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
        info.dwFontSize = COORD(0, height)
        info.FontFamily = 54
        info.FontWeight = 400
        info.FaceName = face_name
        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        ctypes.windll.kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(info))
    except Exception:
        pass


def user_music_folder() -> Path:
    """Return the current Windows user's Music folder with safe fallbacks."""
    candidates: list[Path] = []
    if os.name == "nt":
        try:
            from ctypes import wintypes
            shell32 = ctypes.windll.shell32
            buffer = ctypes.create_unicode_buffer(260)
            # CSIDL_MYMUSIC = 0x000d
            if shell32.SHGetFolderPathW(None, 0x000D, None, 0, buffer) == 0:
                candidates.append(Path(buffer.value))
        except Exception:
            pass
    candidates.extend([Path.home() / "Music", Path.home()])
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_dir():
                return candidate.resolve()
        except OSError:
            continue
    return Path.home()


def app_data_folder() -> Path:
    """Return a writable per-user settings folder."""
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    folder = base / "DOSMusicPlayer"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path.home()
    return folder


def playlist_state_file() -> Path:
    return app_data_folder() / "playlist.json"


def is_stream(value: str | Path) -> bool:
    text = str(value).strip()
    return text.lower().startswith(("http://", "https://", "icy://"))


def media_name(value: str | Path) -> str:
    text = str(value)
    if is_stream(text):
        parsed = urlparse(text)
        tail = Path(parsed.path).name
        return tail or parsed.netloc or "Internet Radio"
    return Path(text).name


def media_parent(value: str | Path, fallback: Path) -> Path:
    if is_stream(value):
        return fallback
    return Path(str(value)).parent


def media_suffix(value: str | Path) -> str:
    if is_stream(value):
        return "STREAM"
    return Path(str(value)).suffix[1:].upper() or "AUDIO"


def media_key(value: str | Path) -> str:
    text = str(value).strip()
    return text.lower() if is_stream(text) else os.path.normcase(str(Path(text)))


def load_saved_playlist() -> list[str]:
    path = playlist_state_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in data if isinstance(data, list) else []:
        text = str(raw).strip()
        if not text:
            continue
        if is_stream(text):
            key = media_key(text)
            if key not in seen:
                seen.add(key)
                result.append(text)
            continue
        try:
            candidate = Path(text).expanduser().resolve()
        except (OSError, ValueError):
            continue
        key = media_key(candidate)
        if candidate.is_file() and candidate.suffix.lower() in AUDIO_EXTENSIONS and key not in seen:
            seen.add(key)
            result.append(str(candidate))
    return result


def save_playlist(paths: Iterable[str | Path]) -> None:
    target = playlist_state_file()
    try:
        target.write_text(
            json.dumps([str(path) for path in paths], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _playlist_entry_to_media(value: str, playlist_path: Path) -> str | None:
    """Resolve local M3U/PLS entries and preserve HTTP radio streams."""
    text = value.strip().strip('"').strip("'")
    if not text:
        return None
    if text.lower().startswith("icy://"):
        text = "http://" + text[6:]
    if is_stream(text):
        return text
    if text.lower().startswith("file://"):
        parsed = urlparse(text)
        text = unquote(parsed.path)
        if os.name == "nt" and text.startswith("/") and len(text) >= 3 and text[2] == ":":
            text = text[1:]
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = playlist_path.parent / candidate
    try:
        candidate = candidate.resolve()
    except OSError:
        return None
    if candidate.is_file() and candidate.suffix.lower() in AUDIO_EXTENSIONS:
        return str(candidate)
    return None


def bundled_ffplay() -> Path | None:
    """Locate FFplay together with its companion DLL runtime folder."""
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidates = [
        bundle_root / "ffplay" / "ffplay.exe",
        bundle_root / "ffplay.exe",
        Path(__file__).resolve().parent / "vendor" / "ffplay.exe",
        Path(__file__).resolve().parent / "ffplay.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def clear_screen() -> None:
    print(f"{ESC}2J{ESC}H", end="")


def format_time(seconds: float | int) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def ellipsize(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text.ljust(width)
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def _read_playlist_text(path: Path) -> str | None:
    try:
        raw_bytes = path.read_bytes()
    except OSError:
        return None
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def parse_m3u(path: Path) -> list[str]:
    text = _read_playlist_text(path)
    if text is None:
        return []
    items: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        media = _playlist_entry_to_media(line, path)
        if media is not None:
            items.append(media)
    return items


def parse_pls(path: Path) -> list[str]:
    text = _read_playlist_text(path)
    if text is None:
        return []
    items: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#", "[")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.lower().startswith("file"):
            continue
        try:
            number = int(key[4:].strip())
        except ValueError:
            number = 999999
        media = _playlist_entry_to_media(value, path)
        if media is not None:
            items.append((number, media))
    return [item for _, item in sorted(items, key=lambda pair: pair[0])]


def collect_files(inputs: Iterable[str | Path]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    def add_media(candidate: str | Path) -> None:
        text = str(candidate)
        if is_stream(text):
            key = media_key(text)
            if key not in seen:
                seen.add(key)
                result.append(text)
            return
        try:
            resolved = Path(text).resolve()
        except OSError:
            return
        key = media_key(resolved)
        if resolved.exists() and resolved.suffix.lower() in AUDIO_EXTENSIONS and key not in seen:
            seen.add(key)
            result.append(str(resolved))

    for item in inputs:
        if is_stream(item):
            add_media(str(item))
            continue
        path = Path(item).expanduser()
        if path.is_dir():
            for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                if child.is_file() and child.suffix.lower() in AUDIO_EXTENSIONS:
                    add_media(child)
        elif path.is_file():
            suffix = path.suffix.lower()
            if suffix in AUDIO_EXTENSIONS:
                add_media(path)
            elif suffix in {".m3u", ".m3u8"}:
                for child in parse_m3u(path):
                    add_media(child)
            elif suffix == ".pls":
                for child in parse_pls(path):
                    add_media(child)
    return result


def get_audio_length(path: str | Path) -> float:
    if is_stream(path):
        return 0.0
    try:
        sound = pygame.mixer.Sound(str(path))
        return float(sound.get_length())
    except pygame.error:
        return 0.0


@dataclass
class PlayerState:
    playlist: list[str] = field(default_factory=list)
    index: int = 0
    paused: bool = False
    playing: bool = False
    shuffle: bool = False
    repeat_mode: str = "OFF"
    volume: float = 0.75
    started_at: float = 0.0
    paused_at: float = 0.0
    pause_started: float = 0.0
    duration: float = 0.0
    status: str = "READY"

    @property
    def current(self) -> str | None:
        if not self.playlist:
            return None
        self.index = max(0, min(self.index, len(self.playlist) - 1))
        return self.playlist[self.index]

    def elapsed(self) -> float:
        if not self.playing:
            return 0.0
        if self.paused:
            return self.paused_at
        return max(0.0, time.monotonic() - self.started_at)


def set_console_geometry(columns: int = 120, rows: int = 32) -> None:
    """Best-effort viewport/buffer sizing to avoid the console scrollbar."""
    if os.name != "nt":
        return
    try:
        os.system(f"mode con: cols={columns} lines={rows} >nul")
    except Exception:
        pass
    try:
        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]
        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                        ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]
        h = ctypes.windll.kernel32.GetStdHandle(-11)
        # Shrink window first, then set exact buffer and viewport.
        tiny = SMALL_RECT(0, 0, 1, 1)
        ctypes.windll.kernel32.SetConsoleWindowInfo(h, True, ctypes.byref(tiny))
        size = COORD(columns, rows)
        ctypes.windll.kernel32.SetConsoleScreenBufferSize(h, size)
        rect = SMALL_RECT(0, 0, columns - 1, rows - 1)
        ctypes.windll.kernel32.SetConsoleWindowInfo(h, True, ctypes.byref(rect))
    except Exception:
        pass


class DOSMusicPlayer:
    SCREEN_WIDTH = 120
    SCREEN_HEIGHT = 32
    PANE_ROWS = 12

    def __init__(self, files: list[str], startup_folder: Path) -> None:
        pygame.mixer.pre_init(44100, -16, 2, 2048)
        pygame.mixer.init()
        pygame.mixer.music.set_volume(0.75)
        self.state = PlayerState(playlist=files)
        self.startup_folder = startup_folder
        first_local = next((Path(x) for x in files if not is_stream(x)), None)
        self.last_folder = first_local.parent if first_local else startup_folder
        self.browser_folder = self.last_folder if self.last_folder.exists() else startup_folder
        self.ffplay_path = bundled_ffplay()
        self.stream_process: subprocess.Popen[str] | None = None
        self.stream_url: str | None = None
        self.browser_selected = 0
        self.browser_top = 0
        self.playlist_selected = 0
        self.playlist_top = 0
        self.active_panel = "playlist"
        self.running = True
        self.last_draw = 0.0
        self.message_until = 0.0
        self.first_draw = True

    def flash(self, message: str, seconds: float = 2.0) -> None:
        self.state.status = message
        self.message_until = time.monotonic() + seconds

    def _stop_stream(self) -> None:
        """Detach and terminate FFplay immediately so the UI never blocks."""
        proc = self.stream_process
        self.stream_process = None
        self.stream_url = None
        if proc is None or proc.poll() is not None:
            return
        try:
            # Resume first in case PAUSE suspended the process, then kill it.
            resume_process(proc)
            proc.kill()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    def _play_stream(self, url: str) -> bool:
        if self.ffplay_path is None:
            self.flash("BUNDLED FFPLAY.EXE IS MISSING - REBUILD THE EXE", 6)
            return False
        self._stop_stream()
        pygame.mixer.music.stop()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        runtime_dir = self.ffplay_path.parent
        command = [
            str(self.ffplay_path),
            "-nodisp",
            "-autoexit",
            "-hide_banner",
            "-loglevel", "warning",
            "-volume", str(int(self.state.volume * 100)),
            "-user_agent", "Mozilla/5.0 DOSMusicPlayer/1.2.6",
            "-reconnect", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            url,
        ]
        try:
            env = os.environ.copy()
            env["PATH"] = str(runtime_dir) + os.pathsep + env.get("PATH", "")
            log_path = Path(os.environ.get("TEMP", str(runtime_dir))) / "DOSMusicPlayer_ffplay.log"
            log_handle = open(log_path, "wb")
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=log_handle,
                cwd=str(runtime_dir),
                env=env,
                creationflags=flags,
            )
            log_handle.close()
            self.stream_process = proc
            self.stream_url = url
            # Do not sleep or wait here: FFplay connects in the background and
            # the main loop will report an early exit without freezing the UI.
            return True
        except OSError as exc:
            self.stream_process = None
            self.stream_url = None
            self.flash(f"STREAM ERROR: {exc}", 6)
            return False

    def play(self, index: int | None = None) -> None:
        if not self.state.playlist:
            self.flash("NO AUDIO FILES LOADED", 3)
            return
        if index is not None:
            self.state.index = index % len(self.state.playlist)
        self.playlist_selected = self.state.index
        current = self.state.current
        if current is None:
            return
        try:
            if is_stream(current):
                if not self._play_stream(current):
                    self.state.playing = False
                    return
            else:
                self._stop_stream()
                pygame.mixer.music.load(str(current))
                pygame.mixer.music.play()
                self.last_folder = Path(current).parent
            self.state.duration = get_audio_length(current)
            self.state.started_at = time.monotonic()
            self.state.paused_at = 0.0
            self.state.paused = False
            self.state.playing = True
            self.flash(f"PLAYING: {media_name(current)}")
        except pygame.error as exc:
            self.state.playing = False
            self.flash(f"PLAYBACK ERROR: {exc}", 5)

    def toggle_pause(self) -> None:
        if not self.state.playing:
            self.play(self.playlist_selected if self.state.playlist else None)
            return
        if self.state.paused:
            if self.stream_process:
                if not resume_process(self.stream_process):
                    self.flash("STREAM RESUME FAILED", 3)
                    return
            else:
                pygame.mixer.music.unpause()
            self.state.started_at += time.monotonic() - self.state.pause_started
            self.state.paused = False
            self.flash("RESUMED")
        else:
            if self.stream_process:
                if not suspend_process(self.stream_process):
                    self.flash("STREAM PAUSE FAILED", 3)
                    return
            else:
                pygame.mixer.music.pause()
            self.state.paused_at = self.state.elapsed()
            self.state.pause_started = time.monotonic()
            self.state.paused = True
            self.flash("PAUSED")

    def stop(self) -> None:
        pygame.mixer.music.stop()
        self._stop_stream()
        self.state.playing = False
        self.state.paused = False
        self.state.paused_at = 0.0
        self.flash("STOPPED")

    def next_track(self, automatic: bool = False) -> None:
        if not self.state.playlist:
            return
        if automatic and self.state.repeat_mode == "ONE":
            self.play(self.state.index)
            return
        if self.state.shuffle and len(self.state.playlist) > 1:
            choices = [i for i in range(len(self.state.playlist)) if i != self.state.index]
            self.play(random.choice(choices))
            return
        nxt = self.state.index + 1
        if nxt >= len(self.state.playlist):
            if self.state.repeat_mode == "ALL":
                nxt = 0
            else:
                self.stop()
                self.state.index = len(self.state.playlist) - 1
                self.playlist_selected = self.state.index
                self.flash("END OF PLAYLIST")
                return
        self.play(nxt)

    def previous_track(self) -> None:
        if self.state.playlist:
            self.play((self.state.index - 1) % len(self.state.playlist))

    def change_volume(self, amount: float) -> None:
        self.state.volume = max(0.0, min(1.0, self.state.volume + amount))
        pygame.mixer.music.set_volume(self.state.volume)
        if self.stream_process:
            self._stream_key(b"0" if amount > 0 else b"9")
        self.flash(f"VOLUME {int(self.state.volume * 100)}%")

    def cycle_repeat(self) -> None:
        modes = ["OFF", "ONE", "ALL"]
        self.state.repeat_mode = modes[(modes.index(self.state.repeat_mode) + 1) % len(modes)]
        self.flash(f"REPEAT {self.state.repeat_mode}")

    def add_paths(self, raw: str, play_first_new: bool = False) -> int:
        raw = raw.strip().strip('"')
        if not raw:
            return 0
        requested = Path(raw).expanduser()
        new_files = collect_files([requested])
        if requested.is_dir():
            self.last_folder = requested.resolve()
        elif requested.is_file():
            self.last_folder = requested.resolve().parent
        existing = {media_key(p) for p in self.state.playlist}
        first_index = len(self.state.playlist)
        added = 0
        for path in new_files:
            key = media_key(path)
            if key not in existing:
                existing.add(key)
                self.state.playlist.append(str(path))
                added += 1
        if added:
            save_playlist(self.state.playlist)
        if added and play_first_new:
            self.play(first_index)
        if requested.suffix.lower() in PLAYLIST_EXTENSIONS and not new_files:
            self.flash(f"PLAYLIST EMPTY OR PATHS NOT FOUND: {requested.name}", 4)
        else:
            self.flash(f"ADDED {added} TRACK(S)", 3)
        return added

    def prompt_add(self) -> None:
        sys.stdout.write(f"{ESC}?25h{ESC}2J{ESC}H")
        sys.stdout.flush()
        print(f"{BRIGHT_CYAN}TYPE A FILE, PLAYLIST, OR DIRECTORY PATH{RESET}")
        print(f"{GRAY}Supported: MP3, WAV, M3U, M3U8, PLS{RESET}")
        print(f"{GRAY}Default: {self.startup_folder}{RESET}\n")
        try:
            value = input("Path [Music]> ").strip() or str(self.startup_folder)
        except (EOFError, KeyboardInterrupt):
            value = ""
        self.add_paths(value)
        self.first_draw = True

    def _browser_entries(self, folder: Path) -> list[tuple[str, Path, bool]]:
        entries: list[tuple[str, Path, bool]] = []
        if folder.parent != folder:
            entries.append(("[..]  Parent Directory", folder.parent, True))
        try:
            children = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except (OSError, PermissionError):
            return entries
        for child in children:
            try:
                if child.is_dir():
                    entries.append((f"[{child.name}]", child, True))
                elif child.is_file() and child.suffix.lower() in SUPPORTED_EXTENSIONS:
                    entries.append((child.name, child, False))
            except OSError:
                continue
        return entries

    def remove_selected(self) -> None:
        if not self.state.playlist:
            return
        idx = max(0, min(self.playlist_selected, len(self.state.playlist)-1))
        removed_current = idx == self.state.index
        name = media_name(self.state.playlist[idx])
        del self.state.playlist[idx]
        if not self.state.playlist:
            self.stop()
            self.state.index = 0
            self.playlist_selected = 0
        else:
            self.playlist_selected = min(idx, len(self.state.playlist)-1)
            if idx < self.state.index:
                self.state.index -= 1
            elif removed_current:
                self.state.index = min(idx, len(self.state.playlist)-1)
                self.play(self.state.index)
        save_playlist(self.state.playlist)
        self.flash(f"REMOVED: {name}")

    def activate_browser_entry(self) -> None:
        entries = self._browser_entries(self.browser_folder)
        if not entries:
            return
        self.browser_selected = max(0, min(self.browser_selected, len(entries)-1))
        label, target, is_dir = entries[self.browser_selected]
        if is_dir:
            self.browser_folder = target
            self.last_folder = target
            self.browser_selected = self.browser_top = 0
            self.flash(f"FOLDER: {target}")
        else:
            suffix = target.suffix.lower()
            before = len(self.state.playlist)
            added = self.add_paths(str(target), play_first_new=True)
            if not added and suffix in AUDIO_EXTENSIONS:
                for i, p in enumerate(self.state.playlist):
                    if media_key(p) == media_key(target.resolve()):
                        self.play(i)
                        break
            elif suffix in PLAYLIST_EXTENSIONS and added:
                self.play(before)
                self.flash(f"OPENED {target.name}: {added} TRACK(S)", 4)

    def move_selection(self, delta: int) -> None:
        if self.active_panel == "playlist":
            if self.state.playlist:
                self.playlist_selected = max(0, min(len(self.state.playlist)-1, self.playlist_selected + delta))
        else:
            entries = self._browser_entries(self.browser_folder)
            if entries:
                self.browser_selected = max(0, min(len(entries)-1, self.browser_selected + delta))

    def page_selection(self, delta_pages: int) -> None:
        self.move_selection(delta_pages * self.PANE_ROWS)

    def handle_key(self, key: str) -> None:
        low = key.lower()
        if key == "\t":
            self.active_panel = "browser" if self.active_panel == "playlist" else "playlist"
        elif key == "\r":
            if self.active_panel == "browser":
                self.activate_browser_entry()
            elif self.state.playlist:
                self.play(self.playlist_selected)
        elif key == "\x08" and self.active_panel == "browser":
            if self.browser_folder.parent != self.browser_folder:
                self.browser_folder = self.browser_folder.parent
                self.last_folder = self.browser_folder
                self.browser_selected = self.browser_top = 0
                self.flash(f"FOLDER: {self.browser_folder}")
        elif low in {" ", "p"}:
            self.toggle_pause()
        elif low == "s":
            self.stop()
        elif low in {"n", ">"}:
            self.next_track()
        elif low in {"b", "<"}:
            self.previous_track()
        elif low in {"+", "="}:
            self.change_volume(0.05)
        elif low in {"-", "_"}:
            self.change_volume(-0.05)
        elif low == "h":
            self.state.shuffle = not self.state.shuffle
            self.flash(f"SHUFFLE {'ON' if self.state.shuffle else 'OFF'}")
        elif low == "r":
            self.cycle_repeat()
        elif low == "a":
            self.prompt_add()
        elif low == "f":
            self.active_panel = "browser"
        elif low == "g":
            if self.state.playlist:
                self.play(self.playlist_selected)
        elif low == "q" or key == "\x1b":
            self.running = False

    def poll_keyboard(self) -> None:
        if os.name != "nt":
            return
        import msvcrt
        while msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in {"\x00", "\xe0"}:
                code = msvcrt.getwch()
                if code == "H": self.move_selection(-1)
                elif code == "P": self.move_selection(1)
                elif code == "I": self.page_selection(-1)
                elif code == "Q": self.page_selection(1)
                elif code == "M": self.active_panel = "browser"
                elif code == "K": self.active_panel = "playlist"
                elif code == "S" and self.active_panel == "playlist": self.remove_selected()
            elif char == "\x7f" and self.active_panel == "playlist":
                self.remove_selected()
            else:
                self.handle_key(char)

    @staticmethod
    def _visible(text: str) -> int:
        # Colors are added around complete cells only; all callers pass plain text.
        return len(text)

    def _pane_rows(self, width: int) -> tuple[list[str], list[str]]:
        rows = self.PANE_ROWS
        # Playlist
        if self.state.playlist:
            self.playlist_selected = max(0, min(self.playlist_selected, len(self.state.playlist)-1))
            if self.playlist_selected < self.playlist_top:
                self.playlist_top = self.playlist_selected
            if self.playlist_selected >= self.playlist_top + rows:
                self.playlist_top = self.playlist_selected - rows + 1
        else:
            self.playlist_selected = self.playlist_top = 0
        p_rows=[]
        for slot in range(rows):
            idx=self.playlist_top+slot
            if idx < len(self.state.playlist):
                selected=idx==self.playlist_selected
                playing=idx==self.state.index
                marker=">" if playing else " "
                txt=f" {marker} {idx+1:03d}.  {media_name(self.state.playlist[idx])}"
                plain=ellipsize(txt,width)
                if selected and self.active_panel=="playlist":
                    p_rows.append(f"{ESC}30;103m{plain}{RESET}")
                elif playing:
                    p_rows.append(f"{YELLOW}{plain}{RESET}")
                else:
                    p_rows.append(f"{GRAY}{plain}{RESET}")
            else:
                p_rows.append(" "*width)
        # Browser
        entries=self._browser_entries(self.browser_folder)
        if entries:
            self.browser_selected=max(0,min(self.browser_selected,len(entries)-1))
            if self.browser_selected < self.browser_top:
                self.browser_top=self.browser_selected
            if self.browser_selected >= self.browser_top+rows:
                self.browser_top=self.browser_selected-rows+1
        else:
            self.browser_selected=self.browser_top=0
        b_rows=[]
        for slot in range(rows):
            idx=self.browser_top+slot
            if idx < len(entries):
                label,target,is_dir=entries[idx]
                plain=ellipsize(" "+label,width)
                if idx==self.browser_selected and self.active_panel=="browser":
                    b_rows.append(f"{ESC}30;103m{plain}{RESET}")
                elif is_dir:
                    b_rows.append(f"{WHITE}{plain}{RESET}")
                else:
                    b_rows.append(f"{GRAY}{plain}{RESET}")
            else:
                b_rows.append(" "*width)
        return p_rows,b_rows

    def draw(self) -> None:
        width = self.SCREEN_WIDTH
        inner = width - 2
        current = self.state.current
        title = " DOS MUSIC PLAYER v1.2.6 RESPONSIVE "
        elapsed = self.state.elapsed()
        duration = self.state.duration
        ratio = min(1.0, elapsed / duration) if duration > 0 else 0.0
        remaining = max(0.0, duration - elapsed) if duration > 0 else 0.0
        time_text = " LIVE " if current and is_stream(current) else f"-{format_time(remaining)}"

        lines=[]; emit=lines.append
        emit(f"{BRIGHT_CYAN}╔{'═'*inner}╗{RESET}")
        emit(f"{BRIGHT_CYAN}║{BG_BLUE}{WHITE}{title.center(inner)}{RESET}{BRIGHT_CYAN}║{RESET}")
        emit(f"{BRIGHT_CYAN}╠{'═'*inner}╣{RESET}")
        track = media_name(current) if current else "< no track loaded >"
        folder = (urlparse(current).netloc if current and is_stream(current) else str(media_parent(current, self.browser_folder) if current else self.browser_folder))
        state_text = "PAUSED" if self.state.paused else "PLAYING" if self.state.playing else "STOPPED"
        emit(f"{BRIGHT_CYAN}║{RESET}{WHITE}{ellipsize(' TRACK  : '+track,inner)}{RESET}{BRIGHT_CYAN}║{RESET}")
        emit(f"{BRIGHT_CYAN}║{RESET}{WHITE}{ellipsize(' PATH   : '+folder,inner)}{RESET}{BRIGHT_CYAN}║{RESET}")
        state_plain=(f" STATE  : {state_text:<8}    VOL : {int(self.state.volume*100):3d}%    "
                     f"SHUFFLE : {'ON' if self.state.shuffle else 'OFF':<3}    REPEAT : {self.state.repeat_mode:<3}")
        colored=ellipsize(state_plain,inner).replace(state_text,f"{BRIGHT_GREEN}{state_text}{RESET}{WHITE}",1)
        emit(f"{BRIGHT_CYAN}║{RESET}{WHITE}{colored}{RESET}{BRIGHT_CYAN}║{RESET}")
        emit(f"{BRIGHT_CYAN}╠{'═'*inner}╣{RESET}")
        label=" PROGRESS: ["
        suffix=f"]  {time_text:>7} "
        bar_width=max(10,inner-len(label)-len(suffix))
        fill=int(bar_width*ratio)
        bar="#"*fill+"-"*(bar_width-fill)
        emit(f"{BRIGHT_CYAN}║{RESET}{BRIGHT_CYAN}{label}{GREEN}{bar}{BRIGHT_CYAN}{suffix}{RESET}{BRIGHT_CYAN}║{RESET}")
        emit(f"{BRIGHT_CYAN}╠{'═'*inner}╣{RESET}")

        left_w=(inner-1)//2
        right_w=inner-1-left_w
        left_title=f" PLAYLIST ({len(self.state.playlist)}) "
        right_title=f" FILE BROWSER - {self.browser_folder} "
        emit(f"{BRIGHT_CYAN}║{BG_BLUE}{WHITE}{ellipsize(left_title.center(left_w),left_w)}{RESET}{BRIGHT_CYAN}│{BG_BLUE}{WHITE}{ellipsize(right_title.center(right_w),right_w)}{RESET}{BRIGHT_CYAN}║{RESET}")
        emit(f"{BRIGHT_CYAN}╟{'─'*left_w}┼{'─'*right_w}╢{RESET}")
        # Build both pane buffers at fixed visible widths.
        p_rows,b_rows=self._pane_rows(left_w)
        # browser rows need right width; rebuild if widths differ
        if right_w != left_w:
            entries=self._browser_entries(self.browser_folder)
            b_rows=[]
            for slot in range(self.PANE_ROWS):
                idx=self.browser_top+slot
                if idx < len(entries):
                    label2,target,is_dir=entries[idx]
                    plain=ellipsize(" "+label2,right_w)
                    if idx==self.browser_selected and self.active_panel=="browser": b_rows.append(f"{ESC}30;103m{plain}{RESET}")
                    elif is_dir: b_rows.append(f"{WHITE}{plain}{RESET}")
                    else: b_rows.append(f"{GRAY}{plain}{RESET}")
                else: b_rows.append(" "*right_w)
        for l,r in zip(p_rows,b_rows):
            emit(f"{BRIGHT_CYAN}║{RESET}{l}{BRIGHT_CYAN}│{RESET}{r}{BRIGHT_CYAN}║{RESET}")
        emit(f"{BRIGHT_CYAN}╠{'═'*inner}╣{RESET}")
        controls1=" SPACE Play/Pause   ENTER Open/Play   DEL Remove   TAB Switch Panels   +/- Volume"
        controls2=" N Next   B Back   F Browser   A Type Path   PgUp/PgDn Page   R Repeat   H Shuffle   Q Quit"
        emit(f"{BRIGHT_CYAN}║{RESET}{WHITE}{ellipsize(controls1,inner)}{RESET}{BRIGHT_CYAN}║{RESET}")
        emit(f"{BRIGHT_CYAN}║{RESET}{WHITE}{ellipsize(controls2,inner)}{RESET}{BRIGHT_CYAN}║{RESET}")
        emit(f"{BRIGHT_CYAN}╠{'═'*inner}╣{RESET}")
        ext=(media_suffix(current) if current else "---")
        clock=time.strftime('%H:%M')
        status=f" STATUS: {self.state.status}   |   {ext} / 44.1 kHz Stereo   |   Playlist: {len(self.state.playlist)}   |   {clock} "
        color=RED if "ERROR" in self.state.status else BRIGHT_GREEN
        # Color status text only while keeping exact width.
        plain=ellipsize(status,inner)
        plain=plain.replace(self.state.status,f"{color}{self.state.status}{RESET}{BRIGHT_CYAN}",1)
        emit(f"{BRIGHT_CYAN}║{RESET}{BRIGHT_CYAN}{plain}{RESET}{BRIGHT_CYAN}║{RESET}")
        emit(f"{BRIGHT_CYAN}╚{'═'*inner}╝{RESET}")

        frame="\n".join(lines)
        prefix=f"{ESC}2J{ESC}H{ESC}?25l" if self.first_draw else f"{ESC}H"
        self.first_draw=False
        sys.stdout.write(prefix+frame+f"{ESC}J")
        sys.stdout.flush()

    def run(self) -> int:
        if self.state.playlist:
            self.play(0)
        try:
            while self.running:
                self.poll_keyboard()
                if self.state.playing and not self.state.paused and self.state.elapsed() > 0.25:
                    if self.stream_process is not None:
                        if self.stream_process.poll() is not None:
                            self.stream_process = None
                            self.next_track(automatic=True)
                    elif not pygame.mixer.music.get_busy():
                        self.next_track(automatic=True)
                if self.message_until and time.monotonic()>self.message_until:
                    self.state.status="READY"; self.message_until=0.0
                now=time.monotonic()
                if now-self.last_draw>=0.20:
                    self.draw(); self.last_draw=now
                time.sleep(0.02)
        finally:
            pygame.mixer.music.stop(); self._stop_stream(); pygame.mixer.quit()
            sys.stdout.write(f"{ESC}?25h{ESC}0m{ESC}2J{ESC}H")
            sys.stdout.flush()
            print("DOS Music Player closed.")
        return 0


def main() -> int:
    parser=argparse.ArgumentParser(description="Retro DOS-style MP3/WAV music and playlist player")
    parser.add_argument("paths",nargs="*",help="MP3, WAV, M3U, PLS, or directory")
    args=parser.parse_args()
    enable_ansi()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    set_console_title("DOS Music Player")
    set_console_font("Consolas",18)
    set_console_geometry(DOSMusicPlayer.SCREEN_WIDTH,DOSMusicPlayer.SCREEN_HEIGHT)
    startup_folder=user_music_folder()
    if args.paths:
        files=collect_files(args.paths)
        save_playlist(files)
    else:
        state_path = playlist_state_file()
        if state_path.exists():
            files=load_saved_playlist()
        else:
            files=collect_files([startup_folder])
            save_playlist(files)
    return DOSMusicPlayer(files,startup_folder).run()


if __name__ == "__main__":
    raise SystemExit(main())
