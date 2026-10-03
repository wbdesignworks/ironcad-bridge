#!/usr/bin/env python3
"""
IronCAD Bridge - the window.

WHY A WINDOW AT ALL
    The person who commissions a receiver is a volunteer at a station, not a
    developer. Before this, configuring the bridge meant finding a text file
    beside an executable and typing two opaque values into it, and knowing
    whether it was working meant reading a console. The window removes both.

WHAT IT IS NOT
    It is not a second implementation. bridge.run_loop() is the only poll loop;
    this file resolves settings, starts that loop on a worker thread, and draws
    what it emits. Anything the loop learns, the console learns too.

COLOUR AND SHAPE
    Taken from the portal's own design system (ironcad-portal/tailwind.config.js),
    not approximated: the six-step cool surface ramp anchored on the brand black,
    three text tiers, rules drawn from the ramp rather than in white, crimson as
    the single brand accent and never as small text, and square corners
    throughout. An operator who uses both should not feel they have changed
    products.

THREADING
    Tk is not thread-safe. The worker never touches a widget: it puts events on a
    queue and the Tk main loop drains that queue on a timer. Every widget write
    in this file happens on the main thread.

THE KEY
    The ingest key is a secret. It is masked by default, revealed only while the
    operator holds the control, and never written to the log pane or the log
    file - including inside error text, which is where a secret usually leaks.
"""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

import bridge

VERSION = "1.1.0"

# ---- the portal's palette, verbatim ---------------------------------------
GROUND = "#0A0A0A"   # s-0, brand black
PANEL = "#111A23"    # s-1
TILE = "#1A2734"     # s-2
RAISED = "#25343F"   # s-3
SELECTED = "#31424F"  # s-4
HOVER = "#40535F"    # s-5

RULE_SOFT = "#1F2C38"
RULE = "#2C3C48"
RULE_HARD = "#4A5D6B"

T1 = "#FFFFFF"       # primary text
T2 = "#A9B7C6"       # secondary
T3 = "#8496A8"       # tertiary

CRIMSON = "#C8102E"
CRIMSON_LIT = "#E01B38"
CRIMSON_DEEP = "#8B0A1F"

OK = "#5BD07A"
WARN = "#F0B429"
URGENT = "#F2691B"
INFO = "#7DB8FA"
INACTIVE = "#93A6C4"

LOG_LINES = 500


def pick_family(root, wanted, fallback):
    """Use the brand face when the station has it, otherwise a sane stand-in."""
    available = {f.lower() for f in tkfont.families(root)}
    for name in wanted:
        if name.lower() in available:
            return name
    return fallback


class Flat(tk.Button):
    """A square, flat button. Tk's default is a raised grey chip; this is not."""

    def __init__(self, parent, text, command, kind="secondary", font=None, width=None):
        fills = {
            "primary": (CRIMSON, CRIMSON_LIT, CRIMSON_DEEP, T1, CRIMSON),
            "secondary": (TILE, HOVER, SELECTED, T1, RULE_HARD),
            "quiet": (PANEL, TILE, TILE, T2, RULE),
        }
        bg, hov, press, fg, border = fills[kind]
        super().__init__(
            parent, text=text, command=command,
            bg=bg, fg=fg, activebackground=press, activeforeground=T1,
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=border, highlightcolor=border,
            cursor="hand2", font=font, padx=16, pady=7,
            width=width, disabledforeground=T3,
        )
        self._bg, self._hov = bg, hov
        self.bind("<Enter>", lambda _e: self.configure(bg=self._hov) if self["state"] != "disabled" else None)
        self.bind("<Leave>", lambda _e: self.configure(bg=self._bg))

    def set_kind_colours(self, bg, hov, border):
        self._bg, self._hov = bg, hov
        self.configure(bg=bg, highlightbackground=border, highlightcolor=border)


class Field(tk.Frame):
    """An uppercase label over a flat entry, the way the portal sets a form."""

    def __init__(self, parent, label, label_font, entry_font, secret=False, hint=None, hint_font=None):
        super().__init__(parent, bg=PANEL)
        self.locked = False
        tk.Label(self, text=label.upper(), bg=PANEL, fg=T2, font=label_font, anchor="w").pack(fill="x")
        row = tk.Frame(self, bg=PANEL)
        row.pack(fill="x", pady=(3, 0))
        self.var = tk.StringVar()
        self.entry = tk.Entry(
            row, textvariable=self.var, bg=TILE, fg=T1, insertbackground=T1,
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=RULE, highlightcolor=INFO,
            font=entry_font, show="•" if secret else "",
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=5, ipadx=6)
        if secret:
            # Held, not toggled: a key left on screen is a key read over a
            # shoulder, and the operator only needs it visible while typing.
            self.reveal = Flat(row, "HOLD TO SHOW", lambda: None, kind="quiet", font=label_font)
            self.reveal.pack(side="left", padx=(6, 0))
            self.reveal.bind("<ButtonPress-1>", lambda _e: self.entry.configure(show=""))
            self.reveal.bind("<ButtonRelease-1>", lambda _e: self.entry.configure(show="•"))
            self.reveal.bind("<Leave>", lambda _e: self.entry.configure(show="•"), add="+")
        if hint:
            # wraplength has to track the column, not a guessed constant: a
            # fixed value wider than the column clips the sentence instead of
            # wrapping it, and the operator reads half an instruction.
            # Start at roughly one settings column. A placeholder of 1 would
            # wrap one character per line and make the frame report an absurd
            # height to anything that measures it before the first Configure.
            self.hint = tk.Label(self, text=hint, bg=PANEL, fg=T3, font=hint_font,
                                 anchor="w", justify="left", wraplength=430)
            self.hint.pack(fill="x", pady=(3, 0))
            self.bind("<Configure>",
                      lambda e: self.hint.configure(wraplength=max(e.width - 2, 80)))

    def get(self):
        return self.var.get().strip()

    def set(self, value):
        self.var.set(value or "")

    def lock(self, why):
        """
        Show the value but refuse edits.

        Used for a value the environment supplies: editing it here would have
        no effect on the next run, and a control that does nothing is worse
        than one that is plainly unavailable.
        """
        self.locked = True
        self.entry.configure(state="readonly", readonlybackground=GROUND,
                             fg=T3, highlightbackground=RULE_SOFT)
        if getattr(self, "reveal", None) is not None:
            self.reveal.configure(state="disabled")
        if getattr(self, "hint", None) is not None:
            self.hint.configure(text=why)


class BridgeWindow:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.worker = None
        self.stop_flag = threading.Event()
        self.last_counts = None
        self.env_locked = {}

        sans = pick_family(root, ["Inter Tight", "Inter", "Segoe UI"], "TkDefaultFont")
        mono = pick_family(root, ["Cascadia Mono", "Consolas", "DejaVu Sans Mono"], "TkFixedFont")
        self.f_title = tkfont.Font(family=sans, size=17, weight="bold")
        self.f_sub = tkfont.Font(family=sans, size=9)
        self.f_label = tkfont.Font(family=sans, size=8, weight="bold")
        self.f_body = tkfont.Font(family=sans, size=10)
        self.f_state = tkfont.Font(family=sans, size=13, weight="bold")
        self.f_num = tkfont.Font(family=sans, size=17, weight="bold")
        self.f_mono = tkfont.Font(family=mono, size=9)

        root.title("IronCAD Bridge")
        root.configure(bg=GROUND)

        self._build_header()
        self._build_status()
        self._build_settings()
        # The footer is packed before the log and anchored to the bottom, so
        # that when the window is shorter than its content the log gives up
        # the space. The receive-only notice is the one line that must never
        # be the part that falls off the bottom edge.
        self._build_footer()
        self._build_log()

        self._size_to_content()
        self._load_into_form()
        self._refresh_state()
        self.root.after(120, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _size_to_content(self):
        """
        Open at the height the window actually needs.

        A hardcoded geometry shorter than the content hides whatever sits at
        the bottom, which here is the activity log - the part that tells the
        operator whether the receiver is working. Ask the layout instead, and
        stay inside the screen on a short display.
        """
        self.root.update_idletasks()
        want_w = max(self.root.winfo_reqwidth(), 980)
        want_h = self.root.winfo_reqheight()
        max_w = self.root.winfo_screenwidth() - 80
        max_h = self.root.winfo_screenheight() - 120
        width = min(want_w, max_w)
        height = min(want_h, max_h)
        self.root.geometry(f"{width}x{height}")
        # Never let the operator shrink the log out of existence, but never
        # demand more than the screen can give either.
        self.root.minsize(min(900, max_w), min(want_h, max_h))

    # ---- chrome ----------------------------------------------------------
    def _build_header(self):
        head = tk.Frame(self.root, bg=GROUND)
        head.pack(fill="x", padx=18, pady=(16, 0))
        mark = tk.Frame(head, bg=CRIMSON, width=5, height=38)
        mark.pack(side="left", fill="y")
        mark.pack_propagate(False)
        text = tk.Frame(head, bg=GROUND)
        text.pack(side="left", padx=(11, 0))
        tk.Label(text, text="IronCAD Bridge", bg=GROUND, fg=T1,
                 font=self.f_title, anchor="w").pack(anchor="w")
        tk.Label(text, text="Radio and device ingest", bg=GROUND, fg=T2,
                 font=self.f_sub, anchor="w").pack(anchor="w")
        tk.Label(head, text=f"v{VERSION}", bg=GROUND, fg=T3,
                 font=self.f_sub).pack(side="right", pady=(6, 0))

    def _build_status(self):
        wrap = tk.Frame(self.root, bg=PANEL, highlightthickness=1,
                        highlightbackground=RULE, highlightcolor=RULE)
        wrap.pack(fill="x", padx=18, pady=(14, 0))

        top = tk.Frame(wrap, bg=PANEL)
        top.pack(fill="x", padx=14, pady=(12, 10))

        self.dot = tk.Canvas(top, width=12, height=12, bg=PANEL,
                             highlightthickness=0)
        self.dot.pack(side="left", pady=(4, 0))
        self.dot_id = self.dot.create_rectangle(1, 1, 11, 11, fill=INACTIVE, outline="")

        words = tk.Frame(top, bg=PANEL)
        words.pack(side="left", padx=(9, 0))
        self.state_label = tk.Label(words, text="Stopped", bg=PANEL, fg=T1,
                                    font=self.f_state, anchor="w")
        self.state_label.pack(anchor="w")
        self.state_detail = tk.Label(words, text="", bg=PANEL, fg=T2,
                                     font=self.f_sub, anchor="w")
        self.state_detail.pack(anchor="w")

        self.start_btn = Flat(top, "START", self._start, kind="primary", font=self.f_label, width=9)
        self.start_btn.pack(side="right")

        tiles = tk.Frame(wrap, bg=PANEL)
        tiles.pack(fill="x", padx=14, pady=(0, 13))
        self.tiles = {}
        for key, label in (("aprs_heard", "APRS heard"), ("aprs_positions", "Positions"),
                           ("aircraft", "Aircraft"), ("weather", "Weather"), ("last", "Last push")):
            cell = tk.Frame(tiles, bg=TILE, highlightthickness=1,
                            highlightbackground=RULE_SOFT, highlightcolor=RULE_SOFT)
            cell.pack(side="left", fill="both", expand=True, padx=(0, 7))
            val = tk.Label(cell, text="--", bg=TILE, fg=T1, font=self.f_num)
            val.pack(anchor="w", padx=11, pady=(8, 0))
            tk.Label(cell, text=label.upper(), bg=TILE, fg=T3,
                     font=self.f_label).pack(anchor="w", padx=11, pady=(0, 8))
            self.tiles[key] = val

    def _build_settings(self):
        box = tk.Frame(self.root, bg=PANEL, highlightthickness=1,
                       highlightbackground=RULE, highlightcolor=RULE)
        box.pack(fill="x", padx=18, pady=(12, 0))

        tk.Label(box, text="SETTINGS", bg=PANEL, fg=T2, font=self.f_label,
                 anchor="w").pack(fill="x", padx=14, pady=(11, 0))
        tk.Label(box, text="Both values come from IronCAD: Settings → Agency radio receiver → Issue ingest key. "
                           "The key is shown once; if it is lost, rotate it there.",
                 bg=PANEL, fg=T3, font=self.f_sub, anchor="w", justify="left",
                 wraplength=900).pack(fill="x", padx=14, pady=(2, 9))

        grid = tk.Frame(box, bg=PANEL)
        grid.pack(fill="x", padx=14, pady=(0, 4))
        grid.columnconfigure(0, weight=1, uniform="col")
        grid.columnconfigure(1, weight=1, uniform="col")

        self.f_agency = Field(grid, "Agency ID", self.f_label, self.f_body)
        self.f_agency.grid(row=0, column=0, sticky="ew", padx=(0, 7), pady=(0, 9))
        self.f_key = Field(grid, "Ingest key", self.f_label, self.f_body, secret=True)
        self.f_key.grid(row=0, column=1, sticky="ew", padx=(7, 0), pady=(0, 9))

        self.f_site = Field(grid, "Site label", self.f_label, self.f_body,
                            hint="Where the antenna is. Shown on the dashboard so a crew knows whose receiver they are looking at.",
                            hint_font=self.f_sub)
        self.f_site.grid(row=1, column=0, sticky="ew", padx=(0, 7), pady=(0, 9))
        self.f_poll = Field(grid, "Poll seconds", self.f_label, self.f_body,
                            hint="How often to read the receiver. Minimum 5.",
                            hint_font=self.f_sub)
        self.f_poll.grid(row=1, column=1, sticky="ew", padx=(7, 0), pady=(0, 9))

        self.f_sdr = Field(grid, "Receiver address", self.f_label, self.f_body,
                           hint="The SDR appliance on this network.", hint_font=self.f_sub)
        self.f_sdr.grid(row=2, column=0, sticky="ew", padx=(0, 7), pady=(0, 9))
        self.f_api = Field(grid, "IronCAD address", self.f_label, self.f_body,
                           hint="Leave as the default unless you self-host.", hint_font=self.f_sub)
        self.f_api.grid(row=2, column=1, sticky="ew", padx=(7, 0), pady=(0, 9))

        actions = tk.Frame(box, bg=PANEL)
        actions.pack(fill="x", padx=14, pady=(2, 12))
        Flat(actions, "SAVE SETTINGS", self._save, kind="secondary", font=self.f_label).pack(side="left")
        self.save_note = tk.Label(actions, text="", bg=PANEL, fg=T3, font=self.f_sub)
        self.save_note.pack(side="left", padx=(11, 0))

        self.env_note = tk.Label(box, text="", bg=PANEL, fg=WARN, font=self.f_sub,
                                 anchor="w", justify="left", wraplength=900)
        self.env_note.pack(fill="x", padx=14, pady=(0, 10))

    def _build_log(self):
        box = tk.Frame(self.root, bg=PANEL, highlightthickness=1,
                       highlightbackground=RULE, highlightcolor=RULE)
        box.pack(fill="both", expand=True, padx=18, pady=(12, 0))

        bar = tk.Frame(box, bg=PANEL)
        bar.pack(fill="x", padx=14, pady=(11, 6))
        tk.Label(bar, text="ACTIVITY", bg=PANEL, fg=T2, font=self.f_label).pack(side="left")
        Flat(bar, "CLEAR", self._clear_log, kind="quiet", font=self.f_label).pack(side="right")

        holder = tk.Frame(box, bg=TILE, highlightthickness=1,
                          highlightbackground=RULE_SOFT, highlightcolor=RULE_SOFT)
        holder.pack(fill="both", expand=True, padx=14, pady=(0, 13))
        self.log = tk.Text(holder, bg=TILE, fg=T2, font=self.f_mono, relief="flat",
                           bd=0, highlightthickness=0, wrap="word", padx=10, pady=8,
                           state="disabled", height=8)
        scroll = tk.Scrollbar(holder, command=self.log.yview, bg=PANEL,
                              troughcolor=TILE, relief="flat", bd=0,
                              activebackground=RULE_HARD, highlightthickness=0)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for level, colour in (("info", T2), ("ok", OK), ("warn", WARN), ("error", CRIMSON_LIT)):
            self.log.tag_configure(level, foreground=colour)

    def _build_footer(self):
        foot = tk.Frame(self.root, bg=GROUND)
        foot.pack(side="bottom", fill="x", padx=18, pady=(10, 14))
        strip = tk.Frame(foot, bg=GROUND, highlightthickness=1,
                         highlightbackground=RULE_SOFT, highlightcolor=RULE_SOFT)
        strip.pack(fill="x")
        tk.Label(strip,
                 text="RECEIVE ONLY  ·  This program reads three receive decoders and nothing else. "
                      "It never transmits, and IronCAD refuses any push carrying transmit intent.",
                 bg=GROUND, fg=T3, font=self.f_sub, anchor="w", justify="left",
                 wraplength=900).pack(fill="x", padx=11, pady=7)

    # ---- settings --------------------------------------------------------
    def _form_overrides(self):
        return {
            "IRONCAD_AGENCY_ID": self.f_agency.get(),
            "IRONCAD_INGEST_KEY": self.f_key.get(),
            "SITE_LABEL": self.f_site.get(),
            "SDR_BASE_URL": self.f_sdr.get(),
            "IRONCAD_API": self.f_api.get(),
            "POLL_SECONDS": self.f_poll.get(),
        }

    def _fields_by_key(self):
        return {
            "IRONCAD_AGENCY_ID": self.f_agency,
            "IRONCAD_INGEST_KEY": self.f_key,
            "SITE_LABEL": self.f_site,
            "SDR_BASE_URL": self.f_sdr,
            "IRONCAD_API": self.f_api,
            "POLL_SECONDS": self.f_poll,
        }

    def _load_into_form(self):
        """
        Fill the form so that what is shown is what will run.

        The form is passed to build_config as overrides, and overrides outrank
        the environment. So a field showing the file's value while the engine
        quietly used the environment's would be a lie in both directions: the
        window would claim the environment won when it had not. An environment
        value is therefore put into its own field and the field locked, which
        leaves exactly one number on screen and one number in use.
        """
        from_file = bridge.read_config_file()
        self.f_agency.set(from_file.get("IRONCAD_AGENCY_ID", ""))
        self.f_key.set(from_file.get("IRONCAD_INGEST_KEY", ""))
        self.f_site.set(from_file.get("SITE_LABEL", ""))
        self.f_sdr.set(from_file.get("SDR_BASE_URL", "") or bridge.DEFAULTS["SDR_BASE_URL"])
        self.f_api.set(from_file.get("IRONCAD_API", "") or bridge.DEFAULTS["IRONCAD_API"])
        self.f_poll.set(from_file.get("POLL_SECONDS", "") or bridge.DEFAULTS["POLL_SECONDS"])

        self.env_locked = {}
        fields = self._fields_by_key()
        for key, field in fields.items():
            value = os.environ.get(key, "").strip()
            if not value:
                continue
            self.env_locked[key] = value
            field.set(value)
            field.lock("set in this computer's environment")

        if self.env_locked:
            self.env_note.configure(
                text="These come from this computer's environment, not from "
                     + bridge.CONFIG_NAME + ", and cannot be changed here: "
                     + ", ".join(sorted(self.env_locked)) + "."
            )
        else:
            self.env_note.configure(text="")

    def _save(self):
        values = self._form_overrides()
        # A value the environment supplied was never typed here, so writing it
        # into the settings file would turn a machine-level setting into a
        # stored one and change what happens if the variable is later removed.
        # Leave whatever the file already held for those keys.
        if self.env_locked:
            existing = bridge.read_config_file()
            for key in self.env_locked:
                values[key] = existing.get(key, "")
        if not values["IRONCAD_AGENCY_ID"] or not values["IRONCAD_INGEST_KEY"]:
            messagebox.showwarning(
                "Not enough to save",
                "The agency ID and the ingest key are both required.\n\n"
                "Get them from IronCAD: Settings → Agency radio receiver → Issue ingest key.",
                parent=self.root,
            )
            return
        try:
            bridge.write_config_file(values)
        except OSError as exc:
            messagebox.showerror("Could not save", f"{bridge.CONFIG_PATH}\n\n{exc}", parent=self.root)
            return
        self.save_note.configure(text=f"Saved to {bridge.CONFIG_NAME}")
        self.root.after(4000, lambda: self.save_note.configure(text=""))
        self._append("info", f"settings saved to {bridge.CONFIG_PATH}")
        self._refresh_state()

    # ---- running ---------------------------------------------------------
    def _start(self):
        if self.worker and self.worker.is_alive():
            self._stop()
            return
        cfg = bridge.build_config(self._form_overrides())
        if not bridge.is_configured(cfg):
            messagebox.showwarning(
                "Not configured",
                "The agency ID and the ingest key are both required before the bridge can start.\n\n"
                "Get them from IronCAD: Settings → Agency radio receiver → Issue ingest key.",
                parent=self.root,
            )
            return

        self.stop_flag.clear()
        self.last_counts = None

        def emit(level, text):
            self.events.put((level, text))

        def on_counts(counts):
            self.events.put(("counts", counts))

        def work():
            try:
                bridge.run_loop(cfg, emit, self.stop_flag.is_set, on_counts)
            except Exception as exc:  # noqa: BLE001 - a crash must reach the screen
                emit("error", f"the bridge stopped unexpectedly: {exc}")
            finally:
                self.events.put(("finished", None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._refresh_state()

    def _stop(self):
        self.stop_flag.set()
        self.state_label.configure(text="Stopping", fg=WARN)
        self.state_detail.configure(text="finishing the current cycle")
        self.start_btn.configure(state="disabled")

    # ---- events ----------------------------------------------------------
    def _drain(self):
        """Tk main thread only. The worker never touches a widget."""
        try:
            while True:
                level, payload = self.events.get_nowait()
                if level == "counts":
                    self._apply_counts(payload)
                elif level == "finished":
                    self.worker = None
                    self.start_btn.configure(state="normal")
                    self._refresh_state()
                else:
                    self._append(level, payload)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _apply_counts(self, counts):
        if counts is None:
            self.state_label.configure(text="Running, not reaching", fg=WARN)
            self.state_detail.configure(text="the last cycle failed; it will keep trying and back off")
            return
        for key in ("aprs_heard", "aprs_positions", "aircraft", "weather"):
            self.tiles[key].configure(text=str(counts.get(key, 0)))
        self.tiles["last"].configure(text=time.strftime("%H:%M:%S"))
        self.last_counts = counts
        self.state_label.configure(text="Running", fg=OK)
        self.state_detail.configure(text="receiver read and pushed to IronCAD")

    def _append(self, level, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n", level)
        # Keep the pane bounded: an unattended station runs for weeks.
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > LOG_LINES:
            self.log.delete("1.0", f"{line_count - LOG_LINES}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _refresh_state(self):
        running = bool(self.worker and self.worker.is_alive())
        if running:
            self.start_btn.configure(text="STOP")
            self.start_btn.set_kind_colours(TILE, HOVER, RULE_HARD)
            self.dot.itemconfigure(self.dot_id, fill=OK)
            if self.last_counts is None:
                self.state_label.configure(text="Starting", fg=INFO)
                self.state_detail.configure(text="first read in progress")
            return

        self.start_btn.configure(text="START")
        self.start_btn.set_kind_colours(CRIMSON, CRIMSON_LIT, CRIMSON)
        cfg = bridge.build_config(self._form_overrides())
        if bridge.is_configured(cfg):
            self.dot.itemconfigure(self.dot_id, fill=INACTIVE)
            self.state_label.configure(text="Stopped", fg=T1)
            self.state_detail.configure(text="configured and ready to start")
        else:
            self.dot.itemconfigure(self.dot_id, fill=WARN)
            self.state_label.configure(text="Not configured", fg=WARN)
            self.state_detail.configure(text="enter the agency ID and ingest key below")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel(
                "Stop the bridge?",
                "The bridge is running. Closing this window stops sending to IronCAD.",
                parent=self.root,
            ):
                return
            self.stop_flag.set()
        self.root.destroy()


def run():
    root = tk.Tk()
    BridgeWindow(root)
    root.mainloop()
