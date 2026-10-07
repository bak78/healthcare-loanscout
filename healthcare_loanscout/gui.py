"""A desktop window for Healthcare LoanScout, for people who'd rather not type commands.

It drives exactly the same run code as the command line, on a background
thread so the window stays responsive. Log lines are handed to the window
through a queue, because Tk may only be touched from its own thread.

Start it by double-clicking "Healthcare LoanScout GUI.bat", or:
    .venv\\Scripts\\python -m healthcare_loanscout.gui
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from . import cli
from .config import PROJECT_ROOT, ConfigError, load_settings
from .npi import load_health_rules

STATES = [
    "AK", "AL", "AR", "AZ", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "HI", "IA", "ID",
    "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN", "MO", "MS", "MT", "NC",
    "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH", "OK", "OR", "PA", "RI", "SC", "SD",
    "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY",
]

# What each exit code means, in words for the status line.
OUTCOMES = {
    cli.EXIT_OK: "Done.",
    cli.EXIT_NO_MATCHES: "No organizations found here — try a wider area.",
    cli.EXIT_CONFIG_ERROR: "Couldn't start — see the message above.",
    cli.EXIT_DEPLETED: "Nothing new: every matching organization is already in the results.",
    cli.EXIT_LOOKUPS_FAILED: "Every lookup failed — check the internet connection.",
    cli.EXIT_SERPER_UNAVAILABLE: "Serper refused the key or is out of credits. Finished rows are saved.",
    cli.EXIT_INTERRUPTED: "Stopped. Finished rows are saved; run again to continue.",
}

OUT_PATH = cli.DEFAULT_OUT

# Capital Sense Funding's colours: the blues and teal from capitalsensefunding.com,
# and the navy sampled from the logo's lettering so the title matches it.
BRAND_BLUE = "#00618C"
BRAND_BLUE_HOVER = "#3381A3"
BRAND_NAVY = "#0A1833"
BRAND_TEAL = "#11A79C"
BRAND_TEAL_HOVER = "#0E8C83"
BORDER = "#C9D6E2"
MUTED = "#5F6B7A"
WARNING = "#A15C00"
LOG_BACKGROUND = "#F4F7FA"
DISABLED = "#B8C4CF"

ASSETS = PROJECT_ROOT / "assets"
LOGO_FILE = ASSETS / "csf_logo.png"
ICON_FILE = ASSETS / "csf_icon.png"


def apply_brand(root: tk.Tk) -> ttk.Style:
    """Colour every ttk widget in the brand palette.

    Windows' native theme ignores most colour settings — its buttons stay grey
    whatever they're told — so this switches to Tk's built-in 'clam' theme,
    which honours them.
    """
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background="white")

    base_font = ("Segoe UI", 10)
    style.configure(".", background="white", foreground=BRAND_NAVY, font=base_font,
                    bordercolor=BORDER, focuscolor=BRAND_BLUE)
    style.configure("TLabel", background="white", foreground=BRAND_NAVY)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Warning.TLabel", foreground=WARNING)
    style.configure("Title.TLabel", font=("Segoe UI Semibold", 16), foreground=BRAND_NAVY)
    style.configure("Subtitle.TLabel", font=("Segoe UI", 9), foreground=MUTED)
    style.configure("Status.TLabel", foreground=BRAND_BLUE, font=("Segoe UI Semibold", 10))

    style.configure("TLabelframe", background="white", bordercolor=BORDER, relief="solid")
    style.configure("TLabelframe.Label", background="white", foreground=BRAND_BLUE,
                    font=("Segoe UI Semibold", 11))

    for widget in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(widget, fieldbackground="white", bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, arrowcolor=BRAND_BLUE)
        style.map(widget, bordercolor=[("focus", BRAND_BLUE)], lightcolor=[("focus", BRAND_BLUE)],
                  # clam greys out read-only fields; the lists are read-only
                  # only so people pick from them rather than type.
                  fieldbackground=[("readonly", "white")])

    # Ordinary buttons: white with blue text and border.
    style.configure("TButton", background="white", foreground=BRAND_BLUE, bordercolor=BRAND_BLUE,
                    lightcolor="white", darkcolor="white", padding=(12, 5))
    style.map("TButton",
              background=[("disabled", "white"), ("active", "#E8F1F6")],
              foreground=[("disabled", DISABLED)],
              bordercolor=[("disabled", DISABLED)])

    # Main actions: solid brand blue; Run in teal so it stands out.
    for name, colour, hover in (("Primary.TButton", BRAND_BLUE, BRAND_BLUE_HOVER),
                                ("Accent.TButton", BRAND_TEAL, BRAND_TEAL_HOVER)):
        style.configure(name, background=colour, foreground="white", bordercolor=colour,
                        lightcolor=colour, darkcolor=colour, font=("Segoe UI Semibold", 10),
                        padding=(14, 5))
        style.map(name,
                  background=[("disabled", DISABLED), ("active", hover)],
                  bordercolor=[("disabled", DISABLED), ("active", hover)],
                  lightcolor=[("disabled", DISABLED), ("active", hover)],
                  darkcolor=[("disabled", DISABLED), ("active", hover)],
                  foreground=[("disabled", "white")])
    return style


def load_image(path: Path) -> tk.PhotoImage | None:
    """The image, or None if the file is missing or unreadable — a lost logo
    must never stop the tool from opening."""
    try:
        return tk.PhotoImage(file=str(path))
    except tk.TclError:
        return None


class QueueHandler(logging.Handler):
    """Sends log lines to the window from whichever thread wrote them."""

    def __init__(self, lines: queue.Queue):
        super().__init__()
        self.lines = lines
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.put(self.format(record))


def options_from_inputs(state: str, zips: str, limit: str, dry_run: bool) -> cli.RunOptions:
    """Turn the form's boxes into run options, or raise ValueError with a
    message a person can act on. Every healthcare field is searched; the
    spreadsheet's healthcare_industry column tells them apart."""
    try:
        cap = int(limit)
    except ValueError:
        raise ValueError("'Organizations to look up' must be a whole number.") from None
    if cap < 1:
        raise ValueError("'Organizations to look up' must be at least 1.")
    prefixes = tuple(z.strip() for z in zips.split(",") if z.strip())
    if any(not z.isdigit() or not 2 <= len(z) <= 5 for z in prefixes):
        raise ValueError("ZIP codes must be 2 to 5 digits, e.g. 191 or 191, 190 — "
                         "or leave it blank for the whole state.")
    return cli.RunOptions(state=state, zip_prefixes=prefixes, limit=cap,
                          dry_run=dry_run, out=OUT_PATH)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.lines: queue.Queue[str] = queue.Queue()
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.outcome: int | None = None

        health = load_health_rules()
        root.title("Healthcare LoanScout — Capital Sense Funding")
        root.minsize(720, 560)
        apply_brand(root)

        self.icon = load_image(ICON_FILE)
        if self.icon is not None:
            root.iconphoto(True, self.icon)

        # --- header: logo, title, and a brand-blue stripe ---
        header = ttk.Frame(root, padding=(16, 12, 16, 10))
        header.pack(fill="x")
        self.logo = load_image(LOGO_FILE)  # kept on self, or Tk discards the image
        if self.logo is not None:
            ttk.Label(header, image=self.logo).pack(side="left")
        else:
            ttk.Label(header, text="Capital Sense Funding", style="Title.TLabel").pack(side="left")
        titles = ttk.Frame(header)
        titles.pack(side="right", anchor="e")
        ttk.Label(titles, text="Healthcare LoanScout", style="Title.TLabel").pack(anchor="e")
        ttk.Label(titles, text="Lead finder · healthcare organizations (NPI)",
                  style="Subtitle.TLabel").pack(anchor="e")
        tk.Frame(root, height=4, background=BRAND_BLUE).pack(fill="x")

        frame = ttk.Frame(root, padding=14)
        frame.pack(fill="both", expand=True)

        # --- step 1: where ---
        where = ttk.LabelFrame(frame, text="1. Where (NPI registry, free · every healthcare field)",
                               padding=10)
        where.pack(fill="x")
        ttk.Label(where, text="State:").grid(row=0, column=0, sticky="w")
        self.state = ttk.Combobox(where, values=STATES, width=5, state="readonly")
        self.state.set(health.state)
        self.state.grid(row=0, column=1, sticky="w", padx=4)

        ttk.Label(where, text="ZIP codes starting with:").grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.zips = ttk.Entry(where, width=14)
        self.zips.insert(0, ", ".join(health.zip_prefixes))
        self.zips.grid(row=0, column=3, sticky="w", padx=4)
        ttk.Label(where, text="191 = Philadelphia · blank = whole state", style="Muted.TLabel").grid(
            row=1, column=2, columnspan=3, sticky="w", padx=(16, 0))


        # --- step 2: find leads ---
        find = ttk.LabelFrame(frame, text="2. Find leads", padding=10)
        find.pack(fill="x", pady=(10, 0))
        ttk.Label(find, text="Organizations to look up:").grid(row=0, column=0, sticky="w")
        self.limit = ttk.Spinbox(find, from_=1, to=5000, width=7)
        self.limit.set(health.default_limit)
        self.limit.grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(find, text="about 1 Serper credit each · best-ranked first", style="Muted.TLabel").grid(
            row=0, column=2, columnspan=3, sticky="w")

        buttons = ttk.Frame(find)
        buttons.grid(row=1, column=0, columnspan=5, sticky="w", pady=(12, 0))
        self.preview_button = ttk.Button(buttons, text="Preview (free)", style="Primary.TButton",
                                         command=lambda: self.start_run(dry_run=True))
        self.preview_button.pack(side="left")
        self.run_button = ttk.Button(buttons, text="▶ Run", style="Accent.TButton",
                                     command=lambda: self.start_run(dry_run=False))
        self.run_button.pack(side="left", padx=6)
        self.stop_button = ttk.Button(buttons, text="■ Stop", command=self.request_stop, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Button(buttons, text="Open results", command=self.open_results).pack(side="left", padx=(18, 0))
        ttk.Button(buttons, text="Open folder", command=self.open_folder).pack(side="left", padx=6)
        self.key_status = ttk.Label(find, text="", style="Warning.TLabel")
        self.key_status.grid(row=2, column=0, columnspan=5, sticky="w", pady=(8, 0))

        # --- log and status ---
        self.log = scrolledtext.ScrolledText(
            frame, height=8, state="disabled", wrap="word", font=("Consolas", 9),
            background=LOG_BACKGROUND, foreground=BRAND_NAVY, relief="flat",
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=BRAND_BLUE,
            padx=8, pady=6,
        )
        self.log.pack(fill="both", expand=True, pady=(10, 0))
        self.status = ttk.Label(frame, text="Ready.", style="Status.TLabel")
        self.status.pack(fill="x", pady=(6, 0))

        self.handler = QueueHandler(self.lines)
        root_logger = logging.getLogger()
        root_logger.addHandler(self.handler)
        root_logger.setLevel(logging.INFO)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_status()
        self.root.after(100, self.pump)

    # --- status ---

    def refresh_status(self) -> None:
        try:
            has_key = bool(load_settings().serper_api_key)
        except ConfigError:
            has_key = False
        self.key_status.config(
            text="" if has_key else
            "No Serper key found in .env — Preview works; Run needs a key (see README)."
        )
        if has_key:
            self.key_status.grid_remove()  # an empty row would still take up space
        else:
            self.key_status.grid()

    # --- running work on a background thread ---

    def _launch(self, label: str, work) -> None:
        self.stop.clear()
        self.outcome = None
        self.set_busy(True)
        self.status.config(text=f"{label}…")

        def target():
            try:
                self.outcome = work()
            except Exception as exc:  # shown in the window instead of vanishing
                logging.getLogger("healthcare_loanscout").exception("unexpected error: %s", exc)
                self.outcome = cli.EXIT_CONFIG_ERROR

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def start_run(self, dry_run: bool) -> None:
        try:
            options = options_from_inputs(
                self.state.get(), self.zips.get(), self.limit.get(), dry_run,
            )
        except ValueError as exc:
            messagebox.showwarning("Healthcare LoanScout", str(exc), parent=self.root)
            return
        label = "Previewing" if dry_run else "Running"
        self._launch(label, lambda: cli.run_leads(options, self.stop))

    def request_stop(self) -> None:
        self.stop.set()
        self.status.config(text="Stopping — finishing the organizations already in progress…")
        self.stop_button.config(state="disabled")

    def set_busy(self, busy: bool) -> None:
        idle = "disabled" if busy else "normal"
        for button in (self.preview_button, self.run_button):
            button.config(state=idle)
        self.stop_button.config(state="normal" if busy else "disabled")

    def pump(self) -> None:
        """Move queued log lines into the window, and notice when work ends."""
        while True:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                break
            self.log.config(state="normal")
            self.log.insert("end", line + "\n")
            self.log.see("end")
            self.log.config(state="disabled")

        if self.worker is not None and not self.worker.is_alive():
            self.worker = None
            self.set_busy(False)
            self.status.config(text=OUTCOMES.get(self.outcome, f"Finished (code {self.outcome})."))
            self.refresh_status()
        self.root.after(100, self.pump)

    # --- results ---

    def results_file(self) -> Path:
        return PROJECT_ROOT / OUT_PATH

    def open_results(self) -> None:
        main = self.results_file()
        preview = PROJECT_ROOT / cli.preview_path(OUT_PATH)
        existing = [p for p in (main, preview) if p.exists()]
        if not existing:
            messagebox.showinfo("Healthcare LoanScout", "No results yet — run a Preview or Run first.",
                                parent=self.root)
            return
        newest = max(existing, key=lambda p: p.stat().st_mtime)
        os.startfile(newest)  # opens in Excel, or whatever opens .csv files

    def open_folder(self) -> None:
        folder = self.results_file().parent
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno(
                "Healthcare LoanScout",
                "A job is still running. Stop it and close?\n\nFinished rows are kept.",
                parent=self.root,
            ):
                return
            self.stop.set()
            self.worker.join(timeout=15)
        logging.getLogger().removeHandler(self.handler)
        self.root.destroy()


def main() -> None:
    # Relative paths (out/, the .env lookup) resolve from the project folder
    # no matter how the window was started.
    os.chdir(PROJECT_ROOT)
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
