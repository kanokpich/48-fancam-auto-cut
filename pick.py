"""
Interactive file selection.

Two backends:
  - native  : macOS Finder dialog via osascript (browse the SSD visually)
  - menu    : numbered terminal list (works over SSH / no GUI)

choose_file() tries native first, falls back to the menu if there's no GUI.
Dialogs open at the first external volume (your SSD) by default.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from media import list_volumes

console = Console()


class PickCancelled(Exception):
    """User cancelled a required selection."""


def default_location() -> Path:
    """First external volume (skip the boot disk), else /Volumes."""
    for v in list_volumes():
        if v.name not in ("Macintosh HD",):
            return v
    return Path("/Volumes")


def list_media(directory: str | Path, extensions: list[str] | None = None,
               limit: int = 400) -> list[Path]:
    """All matching files under a directory (recursive), sorted, capped."""
    exts = {f".{e.lower().lstrip('.')}" for e in (extensions or [])}
    root = Path(directory)
    if not root.exists():
        return []
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.startswith(".") and (not exts or p.suffix.lower() in exts):
            out.append(p)
            if len(out) >= limit:
                break
    return out


# ── native macOS dialog ──────────────────────────────────────────────────────

def choose_file_native(prompt: str, of_type: list[str] | None = None,
                       location: Path | None = None) -> Path:
    # NOTE: macOS `choose file ... of type {...}` only reliably accepts UTIs,
    # not bare extensions ("json"/"wav"/…). Passing extensions greys the files
    # out so they CAN'T be selected. So we don't filter in the native dialog;
    # the terminal-menu fallback still filters by extension via `of_type`.
    loc = str(location or default_location())
    safe_prompt = prompt.replace('"', "'")
    script = (
        f'POSIX path of (choose file with prompt "{safe_prompt}" '
        f'default location (POSIX file "{loc}"))'
    )
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if proc.returncode != 0:
        err = proc.stderr
        if "-128" in err or "User canceled" in err or "User cancelled" in err:
            raise PickCancelled()
        raise RuntimeError(err.strip() or "osascript failed")
    return Path(proc.stdout.strip())


# ── terminal menu ────────────────────────────────────────────────────────────

def choose_file_menu(prompt: str, of_type: list[str] | None = None,
                     location: Path | None = None, allow_skip: bool = False) -> Path | None:
    loc = location or default_location()
    files = list_media(loc, of_type)
    if not files:
        console.print(f"[yellow]ไม่เจอไฟล์ใน {loc}[/yellow]")
        manual = click.prompt("พิมพ์ path เอง (เว้นว่าง = ข้าม)" if allow_skip else "พิมพ์ path เอง",
                              default="", show_default=False)
        if not manual:
            if allow_skip:
                return None
            raise PickCancelled()
        return Path(manual).expanduser()

    table = Table(title=f"{prompt}  ({loc})")
    table.add_column("#", justify="right")
    table.add_column("File")
    table.add_column("Size", justify="right", style="dim")
    for i, f in enumerate(files, 1):
        size_mb = f.stat().st_size / 1e6
        rel = f.relative_to(loc) if f.is_relative_to(loc) else f
        table.add_row(str(i), str(rel), f"{size_mb:,.0f} MB")
    console.print(table)

    lo, hi = (0 if allow_skip else 1), len(files)
    hint = "0 = ข้าม, " if allow_skip else ""
    while True:
        n = click.prompt(f"เลือก ({hint}1-{hi})", type=int)
        if allow_skip and n == 0:
            return None
        if lo <= n <= hi:
            return files[n - 1]
        console.print("[red]เลขไม่ถูกต้อง[/red]")


# ── unified entry ────────────────────────────────────────────────────────────

def choose_file(prompt: str, of_type: list[str] | None = None, *,
                allow_skip: bool = False, gui: bool = True,
                location: Path | None = None) -> Path | None:
    """
    Pick a file. Returns the path, or None if skipped (allow_skip only).
    Raises PickCancelled if a required file is cancelled.
    """
    if gui:
        try:
            return choose_file_native(prompt, of_type, location)
        except PickCancelled:
            if allow_skip:
                return None
            raise
        except (RuntimeError, FileNotFoundError):
            console.print("[dim](ไม่มี GUI — ใช้เมนู terminal)[/dim]")
    return choose_file_menu(prompt, of_type, location, allow_skip=allow_skip)


# ── multiple selection (e.g. several camera clips) ───────────────────────────

def choose_files_native(prompt: str, location: Path | None = None) -> list[Path]:
    loc = str(location or default_location())
    safe = prompt.replace('"', "'")
    script = (
        f'set theFiles to choose file with prompt "{safe}" '
        f'default location (POSIX file "{loc}") with multiple selections allowed\n'
        'set out to ""\n'
        'repeat with f in theFiles\n'
        '  set out to out & POSIX path of f & linefeed\n'
        'end repeat\n'
        'return out'
    )
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if proc.returncode != 0:
        if "-128" in proc.stderr or "cancel" in proc.stderr.lower():
            raise PickCancelled()
        raise RuntimeError(proc.stderr.strip() or "osascript failed")
    return [Path(line) for line in proc.stdout.splitlines() if line.strip()]


def choose_files_menu(prompt: str, of_type: list[str] | None = None,
                      location: Path | None = None) -> list[Path]:
    loc = location or default_location()
    files = list_media(loc, of_type)
    if not files:
        console.print(f"[yellow]ไม่เจอไฟล์ใน {loc}[/yellow]")
        raise PickCancelled()
    table = Table(title=f"{prompt}  ({loc})")
    table.add_column("#", justify="right")
    table.add_column("File")
    table.add_column("Size", justify="right", style="dim")
    for i, f in enumerate(files, 1):
        rel = f.relative_to(loc) if f.is_relative_to(loc) else f
        table.add_row(str(i), str(rel), f"{f.stat().st_size / 1e6:,.0f} MB")
    console.print(table)
    while True:
        raw = click.prompt("เลือกหลายไฟล์ได้ คั่นด้วย , (เช่น 1,3,4)")
        try:
            idxs = [int(x) for x in raw.replace(" ", "").split(",") if x]
            if idxs and all(1 <= i <= len(files) for i in idxs):
                return [files[i - 1] for i in idxs]
        except ValueError:
            pass
        console.print("[red]รูปแบบไม่ถูกต้อง[/red]")


def choose_files(prompt: str, of_type: list[str] | None = None, *,
                 gui: bool = True, location: Path | None = None) -> list[Path]:
    """Pick one or more files. Raises PickCancelled if cancelled."""
    if gui:
        try:
            return choose_files_native(prompt, location)
        except PickCancelled:
            raise
        except (RuntimeError, FileNotFoundError):
            console.print("[dim](ไม่มี GUI — ใช้เมนู terminal)[/dim]")
    return choose_files_menu(prompt, of_type, location)


# ── folder selection (e.g. render output dir) ────────────────────────────────

def choose_folder_native(prompt: str, location: Path | None = None) -> Path:
    loc = str(location or Path.cwd())
    safe = prompt.replace('"', "'")
    script = (
        f'POSIX path of (choose folder with prompt "{safe}" '
        f'default location (POSIX file "{loc}"))'
    )
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if proc.returncode != 0:
        if "-128" in proc.stderr or "cancel" in proc.stderr.lower():
            raise PickCancelled()
        raise RuntimeError(proc.stderr.strip() or "osascript failed")
    return Path(proc.stdout.strip())


def choose_folder(prompt: str, *, gui: bool = True, location: Path | None = None) -> Path:
    """Pick a destination folder (Finder dialog, or a typed path). PickCancelled if cancelled."""
    if gui:
        try:
            return choose_folder_native(prompt, location)
        except PickCancelled:
            raise
        except (RuntimeError, FileNotFoundError):
            console.print("[dim](ไม่มี GUI — พิมพ์ path เอง)[/dim]")
    raw = click.prompt(f"{prompt} (พิมพ์ path โฟลเดอร์)")
    return Path(raw).expanduser()
