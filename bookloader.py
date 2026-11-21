#!/usr/bin/env python3
"""
Bookloader -- send audio, text and PDF files to an iPhone over USB without an
Apple ID, using Safari + Web Inspector over usbmuxd.

This is free software: you may redistribute and/or modify it under the terms
of the GNU General Public License version 3 or later.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Bookloader"
VERSION = "2.0"

# Drop files here from the host (e.g. with scp); the GUI adds them automatically.
INBOX_DIR = Path(__file__).resolve().parent / "inbox"

SUPPORTED_EXTS = {
    ".m4a", ".m4b", ".m4p", ".aac", ".mp3", ".mp4",
    ".alac", ".flac", ".ogg", ".opus", ".wav", ".aiff",
    ".txt", ".pdf",
}


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def python_executable() -> str:
    """Prefer the project's .venv Python so tools work however we were launched."""
    venv_python = Path(__file__).with_name(".venv") / "bin" / "python"
    if venv_python.is_file() and os.access(venv_python, os.X_OK):
        return str(venv_python)
    return sys.executable


class SafariTransferWorker(threading.Thread):
    """Run safari_cdp.py and forward its output into the GUI log."""

    def __init__(self, paths: list[Path], events: queue.Queue,
                 names: list[str] | None = None,
                 continue_event: threading.Event | None = None,
                 auto_advance: bool = False, delay: int = 8):
        super().__init__(daemon=True)
        self.paths = paths
        self.events = events
        self.names = names
        self.continue_event = continue_event or threading.Event()
        self.auto_advance = auto_advance
        self.delay = delay

    def run(self) -> None:
        script = Path(__file__).with_name("safari_cdp.py")
        if not script.is_file():
            self.events.put(("log", f"Missing helper script: {script}"))
            self.events.put(("safari_done", 1))
            return
        cmd = [python_executable(), str(script)]
        if self.names and any(n != p.name for n, p in zip(self.names, self.paths)):
            cmd += ["--names", json.dumps(self.names)]
        if self.auto_advance:
            cmd += ["--auto", "--delay", str(self.delay)]
        cmd += [str(p) for p in self.paths]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE, text=True, bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                text = line.rstrip()
                self.events.put(("log", text))
                if text.startswith("PROGRESS "):
                    parts = text.split(maxsplit=2)
                    if len(parts) >= 2 and "/" in parts[1]:
                        done, total = (int(x) for x in parts[1].split("/", 1))
                        label = parts[2] if len(parts) > 2 else "working"
                        self.events.put(("progress", done, total, label))
                elif text.startswith("Loading delivery page"):
                    self.events.put(("stage", "Loading Safari page"))
                elif text.startswith("Page loaded"):
                    self.events.put(("stage", "Sending files one at a time"))
                elif text == "AWAIT_CONFIRM":
                    self.continue_event.clear()
                    self.events.put(("await_confirm",))
                    self.continue_event.wait()
                    self.events.put(("confirm_done",))
                    if proc.stdin is not None:
                        try:
                            proc.stdin.write("\n")
                            proc.stdin.flush()
                        except (BrokenPipeError, ValueError):
                            pass
            code = proc.wait()
            self.events.put(("safari_done", code))
        except Exception as exc:  # pragma: no cover - defensive
            self.events.put(("log", f"Safari transfer failed: {exc!r}"))
            self.events.put(("safari_done", 1))


class FilePicker(tk.Toplevel):
    """Two-pane file picker: folder tree on top, multi-select file list below.

    Click any folder in the top tree to list its files underneath.  The tree
    loads subfolders lazily as you expand them.  Files are shown one per line
    as 'name    size' in a single wide column; Ctrl/Shift-click selects several.
    """

    def __init__(self, parent: tk.Tk, initial_dir: Path | None = None):
        super().__init__(parent)
        self.title(f"{APP_NAME} — Add files")
        self.geometry("960x680")
        self.minsize(700, 500)
        self.transient(parent)
        self.grab_set()
        self.resizable(True, True)

        self.result: list[Path] = []
        self.current_dir = (
            initial_dir if initial_dir and initial_dir.is_dir() else Path.home()
        )
        self.search_var = tk.StringVar()
        self.only_var = tk.BooleanVar(value=True)
        self._loaded_dirs: set[str] = set()
        self.search_var.trace_add("write", lambda *_a: self._show_files(self.current_dir))

        # ---- top pane: folder tree ----
        dir_frame = ttk.LabelFrame(self, text="1. Folders — click one to list its files below", padding=(8, 4))
        dir_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 4))

        self.dirtree = ttk.Treeview(dir_frame, show="tree", selectmode="browse", height=9)
        dscroll = ttk.Scrollbar(dir_frame, orient=tk.VERTICAL, command=self.dirtree.yview)
        self.dirtree.configure(yscrollcommand=dscroll.set)
        self.dirtree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        dscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.dirtree.bind("<<TreeviewOpen>>", self._on_dir_open)
        self.dirtree.bind("<<TreeviewSelect>>", self._on_dir_select)

        # Roots: Home, the watched inbox, and the filesystem root.
        self._add_dir_node("", Path.home(), "Home")
        if INBOX_DIR.is_dir() and INBOX_DIR != Path.home():
            self._add_dir_node("", INBOX_DIR, "Inbox")
        self._add_dir_node("", Path("/"), "/ (filesystem root)")

        # ---- bottom pane: files in the selected folder ----
        file_frame = ttk.LabelFrame(self, text="2. Files — Ctrl/Shift-click to select multiple", padding=(8, 6))
        file_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 4))

        bar = ttk.Frame(file_frame)
        bar.pack(fill=tk.X, pady=(0, 4))
        self.path_label = ttk.Label(bar, text=str(self.current_dir), foreground="#555555")
        self.path_label.pack(side=tk.LEFT, anchor=tk.W)
        ttk.Label(bar, text="Search:").pack(side=tk.RIGHT)
        ttk.Entry(bar, textvariable=self.search_var, width=22).pack(side=tk.RIGHT, padx=(4, 6))

        self.filetree = ttk.Treeview(file_frame, columns=("details",), show="headings", height=10,
                                     selectmode="extended")
        self.filetree.heading("details", text="Name    Size")
        self.filetree.column("details", anchor=tk.W, width=880, stretch=True)
        fscroll = ttk.Scrollbar(file_frame, orient=tk.VERTICAL, command=self.filetree.yview)
        self.filetree.configure(yscrollcommand=fscroll.set)
        self.filetree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        fscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.filetree.bind("<Double-1>", lambda _e: self._add_selected())

        # ---- buttons ----
        bottom = ttk.Frame(self, padding=(10, 6, 10, 10))
        bottom.pack(fill=tk.X)
        ttk.Checkbutton(
            bottom, text="Show only supported files", variable=self.only_var,
            command=lambda: self._show_files(self.current_dir),
        ).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Select all", command=self._select_all).pack(side=tk.LEFT, padx=(12, 2))
        ttk.Button(bottom, text="Select none", command=self._select_none).pack(side=tk.LEFT, padx=2)
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=2)
        ttk.Button(bottom, text="Add selected", command=self._add_selected).pack(side=tk.RIGHT, padx=2)

        # Show the starting folder and select it in the tree when possible.
        self._show_files(self.current_dir)
        start_iid = str(self.current_dir)
        if self.dirtree.exists(start_iid):
            self.dirtree.selection_set(start_iid)
            self.dirtree.focus(start_iid)
            self.dirtree.see(start_iid)
        self.wait_window(self)

    # ---- folder tree helpers --------------------------------------------- #
    def _add_dir_node(self, parent_iid: str, path: Path, label: str) -> None:
        """Insert a directory node with a dummy child so it shows an arrow."""
        iid = str(path)
        if self.dirtree.exists(iid):
            return
        self.dirtree.insert(parent_iid, tk.END, iid=iid, text=label, open=False)
        self.dirtree.insert(iid, tk.END, iid="dummy:" + iid, text="")

    def _on_dir_open(self, _event: tk.Event) -> None:
        iid = self.dirtree.focus()
        if not iid or iid in self._loaded_dirs or not self.dirtree.exists(iid):
            return
        self._loaded_dirs.add(iid)
        for child in self.dirtree.get_children(iid):
            self.dirtree.delete(child)
        try:
            dirs = sorted(
                (p for p in Path(iid).iterdir() if p.is_dir()),
                key=lambda p: p.name.lower(),
            )
        except OSError:
            return
        for p in dirs:
            self._add_dir_node(iid, p, p.name)

    def _on_dir_select(self, _event: tk.Event) -> None:
        selected = self.dirtree.selection()
        if not selected:
            return
        path = Path(selected[0])
        if path.is_dir():
            self.current_dir = path
            self.search_var.set("")
            self._show_files(path)

    # ---- file list helpers ------------------------------------------------ #
    def _show_files(self, path: Path) -> None:
        self.current_dir = path
        self.path_label.configure(text=str(path))
        self.filetree.delete(*self.filetree.get_children())
        if not path.is_dir():
            return
        query = self.search_var.get().strip().lower()
        try:
            entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for p in entries:
            if not p.is_file():
                continue
            if query and query not in p.name.lower():
                continue
            if self.only_var.get() and p.suffix.lower() not in SUPPORTED_EXTS:
                continue
            try:
                size = human_size(p.stat().st_size)
            except OSError:
                size = "?"
            self.filetree.insert("", tk.END, iid=str(p), values=(f"{p.name}    {size}",))

    def _select_all(self) -> None:
        self.filetree.selection_set(self.filetree.get_children())

    def _select_none(self) -> None:
        self.filetree.selection_remove(self.filetree.selection())

    def _add_selected(self) -> None:
        self.result = [
            Path(i) for i in self.filetree.selection() if Path(i).is_file()
        ]
        if self.result:
            self.destroy()


class DoneDialog(tk.Toplevel):
    """Small, softly styled completion notice — no bold native messagebox."""

    def __init__(self, parent: tk.Tk, failed: bool = False):
        super().__init__(parent)
        self.title(f"{APP_NAME} — {'Delivery failed' if failed else 'Delivery complete'}")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        outer = ttk.Frame(self, padding=24)
        outer.pack(fill=tk.BOTH, expand=True)

        icon = "⚠️" if failed else "✅"
        heading = "Delivery had a problem" if failed else "Files sent over USB"
        ttk.Label(
            outer, text=f"{icon}  {heading}",
            font=("TkDefaultFont", 15),
        ).pack(anchor=tk.W)

        if failed:
            body = (
                "The Safari delivery did not finish cleanly.\n"
                "Check the log below for details, then try again."
            )
        else:
            body = (
                "Confirm any download prompts on the iPhone, then open\n"
                "Files → On My iPhone → Downloads. The built-in player\n"
                "can play M4A/M4B files there."
            )
        ttk.Label(
            outer, text=body, justify=tk.LEFT,
            font=("TkDefaultFont", 11),
        ).pack(anchor=tk.W, pady=(14, 18))

        ttk.Button(outer, text="OK", command=self.destroy, width=10).pack(anchor=tk.E)

        self.update_idletasks()
        self.geometry(f"+{parent.winfo_x() + 120}+{parent.winfo_y() + 160}")
        self.wait_window(self)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{VERSION}")
        self.geometry("1040x820")
        self.minsize(900, 700)

        self.events: queue.Queue = queue.Queue()
        self.files: dict[str, Path] = {}
        self.file_origins: dict[str, str] = {}
        self.inbox_ignored: set[str] = set()
        self.busy = False
        self.continue_event = threading.Event()
        self.prefix_var = tk.BooleanVar(value=False)
        self.auto_var = tk.BooleanVar(value=True)
        self.delay_var = tk.IntVar(value=8)
        self.status_var = tk.StringVar(value="Ready - connect iPhone by USB")

        self._build_ui()
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        self.after(100, self._drain_events)
        self.after(500, self._scan_inbox)

    # ----------------------------- UI building ---------------------------- #
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        # Scrollable outer frame so nothing is ever hidden on smaller screens.
        canvas = tk.Canvas(self, highlightthickness=0)
        vscroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        root = ttk.Frame(canvas, padding=12)
        win_id = canvas.create_window((0, 0), window=root, anchor="nw")
        root.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        header = ttk.Frame(root)
        header.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(header, text=f"{APP_NAME} v{VERSION}",
                  font=("TkDefaultFont", 16, "bold")).pack(side=tk.LEFT)
        ttk.Button(header, text="Help / limitations", command=self.show_help).pack(side=tk.RIGHT)
        ttk.Button(header, text="Quit", command=self.destroy).pack(side=tk.RIGHT, padx=4)

        ttk.Label(
            root,
            text=("USB only - no Wi-Fi, hotspot, SIM or Apple ID. "
                  "On the iPhone enable Settings > Safari > Advanced > Web Inspector."),
            foreground="#555555", wraplength=940,
        ).pack(anchor=tk.W, pady=(0, 8))

        # ---- files frame ----
        files_frame = ttk.LabelFrame(root, text="1. Files to send", padding=8)
        files_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        btn_row = ttk.Frame(files_frame)
        btn_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(btn_row, text="Add files…", command=self.add_files).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="Add folder…", command=self.add_folder).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="Open inbox", command=self.open_inbox).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="Select all", command=self.select_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="Select none", command=self.select_none).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="Remove selected", command=self.remove_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="Clear list", command=self.clear_files).pack(side=tk.LEFT, padx=2)

        # Single "Details" column: one filename + size per line.  The tree and
        # its scrollbar live in their own frame so the tip below can span the
        # full width instead of being squeezed beside them.
        tree_frame = ttk.Frame(files_frame)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        self.tree = ttk.Treeview(
            tree_frame, columns=("details",), show="headings", height=12,
            selectmode="extended",
        )
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_send_buttons())
        self.tree.heading("details", text="Details")
        self.tree.column("details", anchor=tk.W, width=900, stretch=True)
        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Label(
            files_frame,
            text=("Tip: Ctrl/Shift-click to select rows. Selecting any row "
                  "greys out 'Send all' so you can only send those files."),
            foreground="#555555",
            wraplength=960,
            justify=tk.LEFT,
        ).pack(fill=tk.X, anchor=tk.W, pady=(6, 0))

        # ---- options ----
        opts = ttk.LabelFrame(root, text="2. Options", padding=8)
        opts.pack(fill=tk.X, pady=(0, 8))
        ttk.Checkbutton(
            opts,
            text=("Prefix names with folder: 'Book - 01 - chapter1.m4a' "
                  "(groups and orders files in the flat Downloads folder)"),
            variable=self.prefix_var,
        ).pack(anchor=tk.W)

        auto_row = ttk.Frame(opts)
        auto_row.pack(anchor=tk.W, pady=(6, 0))
        ttk.Checkbutton(
            auto_row,
            text="Auto-advance: wait between files instead of pressing a button",
            variable=self.auto_var,
            command=self._toggle_auto,
        ).pack(side=tk.LEFT)
        self.delay_spin = ttk.Spinbox(
            auto_row, from_=2, to=60, width=4,
            textvariable=self.delay_var,
        )
        self.delay_spin.pack(side=tk.LEFT, padx=(8, 2))
        ttk.Label(auto_row, text="seconds between files").pack(side=tk.LEFT)

        # ---- actions ----
        actions = ttk.LabelFrame(root, text="3. Send over USB", padding=8)
        actions.pack(fill=tk.X, pady=(0, 8))
        btn2 = ttk.Frame(actions)
        btn2.pack(fill=tk.X)
        self.safari_btn = ttk.Button(
            btn2, text="Send all via Safari",
            command=lambda: self.start_safari_transfer(False),
        )
        self.safari_btn.pack(side=tk.LEFT, padx=2)
        self.safari_selected_btn = ttk.Button(
            btn2, text="Send selected via Safari",
            command=lambda: self.start_safari_transfer(True),
        )
        self.safari_selected_btn.pack(side=tk.LEFT, padx=2)
        self.safari_folder_btn = ttk.Button(
            btn2, text="Send folder…", command=self.send_folder_via_safari
        )
        self.safari_folder_btn.pack(side=tk.LEFT, padx=2)
        self.confirm_btn = ttk.Button(
            btn2, text="✓ Confirmed on phone — next file",
            command=self._confirm_next, state=tk.DISABLED,
        )
        self.confirm_btn.pack(side=tk.LEFT, padx=(12, 2))
        ttk.Button(btn2, text="Clear log", command=self.clear_log).pack(side=tk.LEFT, padx=2)

        prog_row = ttk.Frame(actions)
        prog_row.pack(fill=tk.X, pady=(8, 0))
        self.progress = ttk.Progressbar(prog_row, mode="determinate", length=420, maximum=100)
        self.progress.pack(side=tk.LEFT)
        self.progress_label = ttk.Label(prog_row, text="ready")
        self.progress_label.pack(side=tk.LEFT, padx=8)
        self.status_label = ttk.Label(actions, textvariable=self.status_var, foreground="#137333")
        self.status_label.pack(anchor=tk.W, pady=(4, 0))

        # ---- log ----
        log_frame = ttk.LabelFrame(root, text="Log", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(log_frame, height=8, wrap=tk.WORD, state=tk.DISABLED)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.log(f"{APP_NAME} v{VERSION} ready.")
        self._update_send_buttons()

    # ----------------------------- helpers -------------------------------- #
    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{stamp}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ----------------------------- file list ------------------------------ #
    def add_files(self) -> None:
        picker = FilePicker(self, initial_dir=INBOX_DIR if INBOX_DIR.is_dir() else None)
        count = 0
        for path in picker.result:
            if self._add_path(path):
                count += 1
        if count:
            self.log(f"Added {count} file(s).")

    def open_inbox(self) -> None:
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", str(INBOX_DIR)])
            self.log(f"Inbox: {INBOX_DIR}")
        except OSError as exc:
            self.log(f"Cannot open file manager; inbox is {INBOX_DIR} ({exc!r})")

    def _scan_inbox(self) -> None:
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        added = 0
        for path in sorted(INBOX_DIR.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTS:
                continue
            key = str(path.resolve())
            if key in self.files or key in self.inbox_ignored:
                continue
            origin = path.parent.name if path.parent != INBOX_DIR else ""
            if self._add_path(path, origin=origin):
                added += 1
        if added:
            self.log(f"Added {added} file(s) from inbox {INBOX_DIR}")

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select a folder of audiobooks")
        if not folder:
            return
        count = 0
        root = Path(folder)
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
                if self._add_path(path, origin=root.name):
                    count += 1
        self.log(f"Added {count} file(s) from {folder}.")

    def _add_path(self, path: Path, origin: str = "") -> bool:
        key = str(path.resolve())
        if key in self.files:
            return False
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTS:
            self.log(f"Skipped (unsupported extension): {path.name}")
            return False
        self.files[key] = path
        self.file_origins[key] = origin
        self.tree.insert(
            "", tk.END, iid=key,
            values=(f"{path.name}    {human_size(path.stat().st_size)}",),
        )
        self._update_send_buttons()
        return True

    def _update_send_buttons(self) -> None:
        """Enable only the send button that matches the current selection.

        When any row is selected, 'Send all' is disabled so you can't
        accidentally send the whole list when you meant to send just the
        selected files.  With no selection, only 'Send all' is available.
        """
        if self.busy:
            self.safari_btn.configure(state=tk.DISABLED)
            self.safari_selected_btn.configure(state=tk.DISABLED)
            return
        has_files = bool(self.tree.get_children())
        has_selection = bool(self.tree.selection())
        self.safari_btn.configure(
            state=tk.NORMAL if (has_files and not has_selection) else tk.DISABLED
        )
        self.safari_selected_btn.configure(
            state=tk.NORMAL if has_selection else tk.DISABLED
        )

    def select_all(self) -> None:
        self.tree.selection_set(self.tree.get_children())
        self._update_send_buttons()

    def select_none(self) -> None:
        self.tree.selection_remove(self.tree.selection())
        self._update_send_buttons()

    def _remember_dismissed(self, paths) -> None:
        inbox = INBOX_DIR.resolve()
        for path in paths:
            try:
                if path.resolve().is_relative_to(inbox):
                    self.inbox_ignored.add(str(path.resolve()))
            except (OSError, ValueError):
                pass

    def remove_selected(self) -> None:
        for item in self.tree.selection():
            path = self.files.get(item)
            if path is not None:
                self._remember_dismissed([path])
            self.files.pop(item, None)
            self.file_origins.pop(item, None)
            self.tree.delete(item)
        self._update_send_buttons()

    def clear_files(self) -> None:
        self._remember_dismissed(self.files.values())
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.files.clear()
        self.file_origins.clear()
        self._update_send_buttons()

    # ----------------------------- transfer ------------------------------- #
    def _safari_names(self, paths: list[Path]) -> list[str]:
        """Build Bookcoder-style download names when the prefix option is on."""
        if not self.prefix_var.get():
            return []
        names: list[str] = []
        counters: dict[str, int] = {}
        for path in paths:
            key = str(path.resolve())
            origin = self.file_origins.get(key, "")
            if not origin:
                names.append(path.name)
                continue
            counters[origin] = counters.get(origin, 0) + 1
            number = counters[origin]
            stem = path.stem
            chapter_match = re.search(r"chapter\s*(\d+)", stem, re.IGNORECASE)
            if chapter_match:
                label = f"Chapter {int(chapter_match.group(1)):02d}"
            else:
                label = re.sub(r"^\d+\s*[-._]\s*", "", stem) or stem
            names.append(f"{origin} - {number:02d} - {label}{path.suffix.lower()}")
        return names

    def send_folder_via_safari(self) -> None:
        folder = filedialog.askdirectory(title="Select a folder to send via Safari")
        if not folder:
            return
        paths: list[Path] = []
        book_name = Path(folder).name
        for path in sorted(Path(folder).rglob("*")):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
                paths.append(path)
                self._add_path(path, origin=book_name)
        if not paths:
            messagebox.showinfo(APP_NAME, "No supported files found in that folder.")
            return
        self.log(f"Folder {folder}: {len(paths)} file(s) ready to send.")
        self.start_safari_transfer(paths=paths)

    def start_safari_transfer(
        self, selected_only: bool = False, paths: list[Path] | None = None
    ) -> None:
        if self.busy:
            return

        if paths is None:
            if selected_only:
                selected = self.tree.selection()
                paths = [self.files[i] for i in selected if i in self.files]
                if not paths:
                    messagebox.showinfo(APP_NAME, "Select one or more files in the list first.")
                    return
            else:
                paths = list(self.files.values())
                if not paths:
                    messagebox.showinfo(APP_NAME, "Add at least one file first.")
                    return

        script = Path(__file__).with_name("safari_cdp.py")
        if not script.is_file():
            messagebox.showerror(APP_NAME, f"Missing helper script: {script}")
            return

        self.busy = True
        self._update_send_buttons()
        self.safari_folder_btn.configure(state=tk.DISABLED)
        self.confirm_btn.configure(state=tk.DISABLED)
        self.continue_event.clear()
        self.delay_spin.configure(state=tk.DISABLED)
        self.progress.configure(value=0, maximum=100)
        self.progress_label.configure(text="starting")
        self.status_var.set("Sending over USB…")
        self.status_label.configure(foreground="#137333")

        display_names = self._safari_names(paths)
        if display_names and any(n != p.name for n, p in zip(display_names, paths)):
            self.log("Using folder-prefixed names, e.g. " +
                     next(n for n, p in zip(display_names, paths) if n != p.name))
        auto = self.auto_var.get()
        delay = self.delay_var.get()
        self.log(f"Starting Safari delivery of {len(paths)} file(s) over USB…")
        if auto:
            self.log(f"Auto-advance on: {delay}s between files. "
                     "Confirm each download on the phone as it appears.")
        else:
            self.log("For each file: confirm the download on the phone, then "
                     "press 'Confirmed on phone — next file' here.")
        SafariTransferWorker(
            paths, self.events, display_names, self.continue_event,
            auto_advance=auto, delay=delay,
        ).start()

    def _toggle_auto(self) -> None:
        """Enable/disable the delay spinner based on auto-advance."""
        if self.auto_var.get():
            self.delay_spin.configure(state=tk.NORMAL)
        else:
            self.delay_spin.configure(state=tk.DISABLED)

    def _confirm_next(self) -> None:
        """User confirmed the iOS prompt; let the worker send the next file."""
        self.confirm_btn.configure(state=tk.DISABLED)
        self.status_var.set("Confirmed — preparing next file…")
        self.continue_event.set()

    # ----------------------------- events --------------------------------- #
    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.log(event[1])
                elif kind == "stage":
                    self.progress_label.configure(text=event[1])
                elif kind == "progress":
                    done, total, label = event[1], event[2], event[3]
                    self.progress.configure(value=int(done * 100 / total) if total else 0)
                    self.progress_label.configure(text=f"{label} ({done}/{total})")
                elif kind == "await_confirm":
                    self.confirm_btn.configure(state=tk.NORMAL)
                    self.status_var.set("Confirm the download on the iPhone, "
                                        "then press the green button")
                    self.status_label.configure(foreground="#b06000")
                elif kind == "confirm_done":
                    self.confirm_btn.configure(state=tk.DISABLED)
                    self.status_label.configure(foreground="#137333")
                elif kind == "safari_done":
                    code = event[1]
                    self.busy = False
                    self._update_send_buttons()
                    self.safari_folder_btn.configure(state=tk.NORMAL)
                    self.confirm_btn.configure(state=tk.DISABLED)
                    self._toggle_auto()
                    if code == 0:
                        self.progress.configure(value=100)
                        self.progress_label.configure(text="done")
                        self.status_var.set("✓ Delivery complete — confirm prompts on iPhone")
                        DoneDialog(self)
                    else:
                        self.status_var.set("Delivery failed — see log")
                        self.status_label.configure(foreground="#b00020")
                        DoneDialog(self, failed=True)
                else:
                    self.log(f"Unknown event: {event!r}")
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    # ----------------------------- help ----------------------------------- #
    def show_help(self) -> None:
        win = tk.Toplevel(self)
        win.title(f"{APP_NAME} - help")
        win.geometry("720x560")
        notebook = ttk.Notebook(win)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        def page(title: str, text: str) -> None:
            frame = ttk.Frame(notebook, padding=12)
            label = tk.Text(frame, wrap=tk.WORD, font=("TkDefaultFont", 10))
            label.insert("1.0", text.strip())
            label.configure(state=tk.DISABLED)
            label.pack(fill=tk.BOTH, expand=True)
            notebook.add(frame, text=title)

        page(
            "Quick start",
            """
1. On the iPhone enable Settings > Safari > Advanced > Web Inspector.
2. Connect the iPhone with a USB cable (no Wi-Fi, SIM or hotspot needed).
3. Add files with 'Add files…' or 'Add folder…', or copy files into the
   'inbox' folder with scp from the host - they appear automatically.
4. Select rows if you only want some files, then press 'Send all via Safari'
   or 'Send selected via Safari'.
5. Files are sent ONE at a time to avoid the iOS modal prompt blocking later
   files and to keep only one large audiobook in the phone's memory.
   - Auto-advance (default): the app waits N seconds between files (set the
     delay in Options). Confirm each download on the phone as it appears.
   - Manual: uncheck Auto-advance; for each file confirm on the phone, then
     press 'Confirmed on phone — next file' in this window.
6. Files land in Files > On My iPhone > Downloads and play with the built-in
   player.
""",
        )
        page(
            "Selection and naming",
            """
The file list is a single detailed column: one filename with its extension
and size per line. Ctrl-click or Shift-click selects multiple rows; the
'Select all' / 'Select none' buttons are shortcuts.

'Add files…' uses the system file chooser, which supports multiple selection
with Ctrl/Shift. If that is awkward over X forwarding, use 'Add folder…' or
copy files into the 'inbox' folder with scp instead.

Enable 'Prefix names with folder' to get Bookcoder-style names such as
'My Book - 01 - Chapter 01.m4a', which keep chapters from different books
sorted and grouped in the flat Downloads folder.
""",
        )
        page(
            "Limitations",
            """
Files always land in the flat Downloads folder - iOS does not let Safari
choose a different location, so a per-book folder cannot be created.

The built-in player plays M4A/M4B but ignores chapter metadata and does not
save playback position. A dedicated player app (VLC/BookPlayer) can show
chapters but needs an App Store install with an Apple ID.

Raw AFC folder copies are not offered here: iOS apps read a private library
database, so such files are not playable in Books or Music.
""",
        )
        page(
            "Install / troubleshooting",
            """
Run ./install_deps.sh once (creates .venv with pymobiledevice3 and
websocket-client), then ./run.sh.

If Safari delivery fails: make sure Web Inspector is on, unlock the phone,
and try again. The app resets Safari's page automatically before each send.

If the file chooser looks empty over ssh -X, remember it browses the VM's
filesystem, not the host's - use scp into the 'inbox' folder instead.
""",
        )
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 8))


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
