"""The CrossCopy window: a small dark settings UI built with plain Tk.

The window is opened from the tray ("Open CrossCopy", also a double click) and
stays out of the way otherwise: closing it leaves the app running in the tray.

Tk insists on owning the thread that created it, so the window runs on its own
short-lived thread and talks to the app through :meth:`CrossCopyApp.apply_settings`
and :meth:`CrossCopyApp.status`. No new runtime dependency is needed - Tk ships
with CPython and is bundled by PyInstaller.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from . import settings_store, startup
from .tray import asset_path

try:  # pragma: no cover - exercised by every real run, skipped when Tk is absent
    import tkinter as tk
    from tkinter import font as tkfont
except Exception:  # pragma: no cover - a Tk-less Python still serves phones
    tk = None  # type: ignore[assignment]
    tkfont = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from .app import CrossCopyApp

logger = logging.getLogger("crosscopy.ui")

# -- palette (matches the app icon) ---------------------------------------

BG = "#0D101F"
CARD = "#141A2D"
FIELD = "#1B2238"
BORDER = "#242C46"
TEXT = "#E9EDFB"
MUTED = "#8B94B4"
DIM = "#5A6382"
ACCENT = "#5EEAFF"
ACCENT_HOVER = "#8BF3FF"
ACCENT_INK = "#06131B"
PURPLE = "#A78BFA"
OK = "#4ADE80"
WARN = "#FBBF24"
DANGER = "#F87171"

BASE_WIDTH = 460


def _enable_dpi_awareness() -> None:
    """Blurry text is the one thing that makes a Tk window look dated."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
    except Exception:  # pragma: no cover - older Windows
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _dark_title_bar(root: Any) -> None:
    """Ask DWM to paint the title bar dark (Windows 10 1809+)."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
            ) == 0:
                break
    except Exception:  # pragma: no cover - purely cosmetic
        logger.debug("could not darken the title bar", exc_info=True)


def round_rect(
    canvas: Any, x1: float, y1: float, x2: float, y2: float, radius: float, **options: Any
) -> int:
    """A rounded rectangle as a smoothed polygon (Tk has no native one)."""
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1,
        x2, y1 + radius, x2, y2 - radius, x2, y2,
        x2 - radius, y2, x1 + radius, y2, x1, y2,
        x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **options)


class Button:
    """A rounded, hover-highlighted button drawn on its own canvas."""

    def __init__(
        self,
        master: Any,
        text: str,
        command: Any,
        *,
        scale: float,
        width: int,
        height: int,
        font: Any,
        primary: bool = True,
        parent_bg: str = BG,
    ) -> None:
        self.command = command
        self.width = width
        self.height = height
        self.canvas = tk.Canvas(
            master,
            width=width,
            height=height,
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._primary = primary
        self._fill = ACCENT if primary else FIELD
        self._hover = ACCENT_HOVER if primary else "#232C48"
        self._shape = round_rect(
            self.canvas,
            1,
            1,
            width - 1,
            height - 1,
            (9 * scale),
            fill=self._fill,
            outline="" if primary else BORDER,
        )
        self._label = self.canvas.create_text(
            width / 2,
            height / 2,
            text=text,
            fill=ACCENT_INK if primary else TEXT,
            font=font,
        )
        self.canvas.bind("<Enter>", lambda _event: self.canvas.itemconfigure(self._shape, fill=self._hover))
        self.canvas.bind("<Leave>", lambda _event: self.canvas.itemconfigure(self._shape, fill=self._fill))
        self.canvas.bind("<Button-1>", self._click)

    def _click(self, _event: Any = None) -> None:
        if self.command is not None:
            self.command()


class Field:
    """A rounded input: a canvas for the pill plus a borderless ``Entry``."""

    def __init__(
        self,
        master: Any,
        *,
        scale: float,
        width: int,
        height: int,
        font: Any,
        readonly: bool = False,
        masked: bool = False,
        text: str = "",
        parent_bg: str = CARD,
        text_color: str = TEXT,
    ) -> None:
        self.canvas = tk.Canvas(
            master,
            width=width,
            height=height,
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
        )
        self._shape = round_rect(
            self.canvas, 1, 1, width - 1, height - 1, 9 * scale, fill=FIELD, outline=BORDER
        )
        self.entry = tk.Entry(
            self.canvas,
            bg=FIELD,
            fg=text_color,
            readonlybackground=FIELD,
            disabledbackground=FIELD,
            disabledforeground=MUTED,
            insertbackground=ACCENT,
            selectbackground="#2F3B60",
            selectforeground=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=font,
            show="\u2022" if masked else "",
        )
        self.entry.insert(0, text)
        if readonly:
            self.entry.configure(state="readonly")
        self.canvas.create_window(
            int(11 * scale),
            height / 2,
            window=self.entry,
            anchor="w",
            width=width - int(22 * scale),
            height=height - int(12 * scale),
        )
        if not readonly:
            self.entry.bind("<FocusIn>", lambda _e: self.canvas.itemconfigure(self._shape, outline=ACCENT))
            self.entry.bind("<FocusOut>", lambda _e: self.canvas.itemconfigure(self._shape, outline=BORDER))

    # -- value helpers -----------------------------------------------------

    def get(self) -> str:
        return self.entry.get()

    def set(self, value: str) -> None:
        state = str(self.entry.cget("state"))
        self.entry.configure(state="normal")
        self.entry.delete(0, "end")
        self.entry.insert(0, value)
        self.entry.configure(state=state)


class Switch:
    """A small on/off pill used for "Start with Windows"."""

    def __init__(
        self,
        master: Any,
        *,
        scale: float,
        value: bool,
        command: Any,
        parent_bg: str = BG,
    ) -> None:
        self.width = int(42 * scale)
        self.height = int(24 * scale)
        self.command = command
        self.value = value
        self.canvas = tk.Canvas(
            master,
            width=self.width,
            height=self.height,
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._draw()
        self.canvas.bind("<Button-1>", self._click)

    def _draw(self) -> None:
        self.canvas.delete("all")
        radius = (self.height - 2) / 2
        round_rect(
            self.canvas,
            1,
            1,
            self.width - 1,
            self.height - 1,
            radius,
            fill=ACCENT if self.value else "#2B3452",
            outline="",
        )
        knob = self.height - 8
        x = self.width - 4 - knob / 2 if self.value else 4 + knob / 2
        self.canvas.create_oval(
            x - knob / 2,
            4,
            x + knob / 2,
            4 + knob,
            fill=ACCENT_INK if self.value else "#8B94B4",
            outline="",
        )

    def _click(self, _event: Any = None) -> None:
        if self.command is not None:
            self.command()
            return
        self.set(not self.value)

    def set(self, value: bool) -> None:
        self.value = value
        self._draw()


class Window:
    """Widgets of one window. Everything here runs on the window's own thread."""

    def __init__(self, app: "CrossCopyApp", root: Any) -> None:
        self.app = app
        self.root = root
        self.scale = max(0.75, min(3.0, root.winfo_fpixels("1i") / 96.0))
        self.family = self._pick_family()
        self.commands: queue.Queue[str] = queue.Queue()
        self._logo: Any = None
        self._closing = False
        self._last_url = ""
        self._shown_error: str | None = None
        self._after_jobs: set[str] = set()

    # -- helpers -----------------------------------------------------------

    def S(self, value: float) -> int:
        return max(1, int(round(value * self.scale)))

    def font(self, size: float, weight: str = "normal") -> tuple[str, int, str]:
        return (self.family, -self.S(size), weight)

    def _pick_family(self) -> str:
        try:
            available = set(tkfont.families(self.root))
        except Exception:  # pragma: no cover
            return "Segoe UI"
        for name in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica Neue"):
            if name in available:
                return name
        return "TkDefaultFont"

    def _label(self, master: Any, text: str, **options: Any) -> Any:
        defaults = {
            "text": text,
            "bg": BG,
            "fg": TEXT,
            "font": self.font(10),
            "anchor": "w",
            "justify": "left",
            "bd": 0,
            "highlightthickness": 0,
        }
        defaults.update(options)
        return tk.Label(master, **defaults)

    # -- construction ------------------------------------------------------

    def build(self) -> None:
        root = self.root
        root.title("CrossCopy")
        root.configure(bg=BG)
        root.resizable(False, False)
        _enable_dpi_awareness()
        self._set_window_icon()

        margin = self.S(24)
        width = self.S(BASE_WIDTH)
        card_width = width - margin * 2
        body_width = card_width - self.S(32)

        frame = tk.Frame(root, bg=BG)
        frame.pack(fill="both", expand=True, padx=0, pady=0)

        # header ----------------------------------------------------------
        header = tk.Frame(frame, bg=BG)
        header.pack(fill="x", padx=margin, pady=(self.S(20), 0))

        logo = self._load_logo(self.S(46))
        if logo is not None:
            self._logo = logo
            tk.Label(header, image=logo, bg=BG, bd=0).pack(side="left", padx=(0, self.S(12)))

        titles = tk.Frame(header, bg=BG)
        titles.pack(side="left", fill="y")
        self._label(titles, "CrossCopy", font=self.font(18, "bold"), fg=TEXT).pack(anchor="w")
        self._label(
            titles, "Clipboard sync over Wi-Fi", font=self.font(9), fg=MUTED
        ).pack(anchor="w", pady=(self.S(2), 0))

        # status ----------------------------------------------------------
        status_row = tk.Frame(frame, bg=BG)
        status_row.pack(fill="x", padx=margin, pady=(self.S(18), 0))

        self.dot = tk.Canvas(
            status_row, width=self.S(10), height=self.S(10), bg=BG, highlightthickness=0, bd=0
        )
        self._dot_item = self.dot.create_oval(
            1, 1, self.S(9), self.S(9), fill=DIM, outline=""
        )
        self.dot.pack(side="left", pady=(self.S(5), 0))

        status_text = tk.Frame(status_row, bg=BG)
        status_text.pack(side="left", fill="x", expand=True, padx=(self.S(9), 0))
        self.status_label = self._label(status_text, "Waiting for a phone", font=self.font(11, "bold"))
        self.status_label.pack(anchor="w")
        self.detail_label = self._label(
            status_text, "", font=self.font(9), fg=MUTED, wraplength=card_width - self.S(20)
        )
        self.detail_label.pack(anchor="w", pady=(self.S(2), 0))

        # phone link card -------------------------------------------------
        link_card = self._card(frame, card_width, self.S(96), margin, top=self.S(16))
        self._label(
            link_card, "PHONE LINK", font=self.font(8, "bold"), fg=DIM, bg=CARD
        ).place(x=0, y=0)
        self.copy_button = Button(
            link_card,
            "Copy",
            self.copy_link,
            scale=self.scale,
            width=self.S(74),
            height=self.S(26),
            font=self.font(9, "bold"),
            primary=False,
            parent_bg=CARD,
        )
        self.copy_button.canvas.place(x=body_width - self.S(74), y=-self.S(2))
        self.link_field = Field(
            link_card,
            scale=self.scale,
            width=body_width,
            height=self.S(36),
            font=self.font(9),
            readonly=True,
            parent_bg=CARD,
            text_color=MUTED,
        )
        self.link_field.canvas.place(x=0, y=self.S(30))

        # settings card ---------------------------------------------------
        settings_card = self._card(frame, card_width, self.S(196), margin, top=self.S(12))
        self._label(
            settings_card, "PAIRING TOKEN", font=self.font(8, "bold"), fg=DIM, bg=CARD
        ).place(x=0, y=0)
        self.show_label = self._label(
            settings_card,
            "Show",
            font=self.font(8, "bold"),
            fg=ACCENT,
            bg=CARD,
            cursor="hand2",
            anchor="e",
        )
        self.show_label.place(x=body_width - self.S(46), y=-self.S(2), width=self.S(46))
        self.show_label.bind("<Button-1>", lambda _event: self.toggle_token_visibility())

        self.token_field = Field(
            settings_card,
            scale=self.scale,
            width=body_width,
            height=self.S(36),
            font=self.font(10),
            masked=True,
            text=self.app.settings.token or "",
            parent_bg=CARD,
        )
        self.token_field.canvas.place(x=0, y=self.S(26))

        port_width = self.S(104)
        self._label(settings_card, "PORT", font=self.font(8, "bold"), fg=DIM, bg=CARD).place(
            x=0, y=self.S(72)
        )
        self._label(
            settings_card, "DEVICE NAME", font=self.font(8, "bold"), fg=DIM, bg=CARD
        ).place(x=port_width + self.S(16), y=self.S(72))

        self.port_field = Field(
            settings_card,
            scale=self.scale,
            width=port_width,
            height=self.S(36),
            font=self.font(10),
            text=str(self.app.settings.port),
            parent_bg=CARD,
        )
        self.port_field.canvas.place(x=0, y=self.S(92))

        self.name_field = Field(
            settings_card,
            scale=self.scale,
            width=body_width - port_width - self.S(16),
            height=self.S(36),
            font=self.font(10),
            text=self.app.settings.device_name,
            parent_bg=CARD,
        )
        self.name_field.canvas.place(x=port_width + self.S(16), y=self.S(92))

        self._label(
            settings_card,
            "Your phone needs this token to connect. Leave it empty to allow any device.",
            font=self.font(8),
            fg=DIM,
            bg=CARD,
            wraplength=body_width,
        ).place(x=0, y=self.S(138))

        # autostart -------------------------------------------------------
        auto_row = tk.Frame(frame, bg=BG)
        auto_row.pack(fill="x", padx=margin, pady=(self.S(14), 0))
        self.autostart_switch = Switch(
            auto_row, scale=self.scale, value=startup.is_enabled(), command=self.toggle_startup
        )
        self.autostart_switch.canvas.pack(side="left", pady=(self.S(2), 0))
        self._label(
            auto_row, "Start with Windows", font=self.font(10), bg=BG
        ).pack(side="left", padx=(self.S(10), self.S(8)))
        self._label(
            auto_row, "runs silently at sign-in", font=self.font(8), fg=DIM, bg=BG
        ).pack(side="left")

        # message ---------------------------------------------------------
        self.message = self._label(
            frame,
            "",
            font=self.font(9),
            fg=MUTED,
            wraplength=width - margin * 2,
        )
        self.message.pack(fill="x", padx=margin, pady=(self.S(10), 0))

        # footer ----------------------------------------------------------
        footer = tk.Frame(frame, bg=BG)
        footer.pack(fill="x", padx=margin, pady=(self.S(12), self.S(18)))
        self.save_button = Button(
            footer,
            "Save & reconnect",
            self.save,
            scale=self.scale,
            width=self.S(186),
            height=self.S(40),
            font=self.font(10, "bold"),
            primary=True,
        )
        self.save_button.canvas.pack(side="left")
        self.close_button = Button(
            footer,
            "Close",
            self.close,
            scale=self.scale,
            width=self.S(104),
            height=self.S(40),
            font=self.font(10),
            primary=False,
        )
        self.close_button.canvas.pack(side="left", padx=(self.S(10), 0))

        root.update_idletasks()
        height = root.winfo_reqheight()
        x = max(0, (root.winfo_screenwidth() - width) // 2)
        y = max(0, (root.winfo_screenheight() - height) // 3)
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.protocol("WM_DELETE_WINDOW", self.close)
        _dark_title_bar(root)
        self._bind_keys()
        self.refresh()
        self._schedule(250, self._tick)

    def _card(self, parent: Any, width: int, height: int, margin: int, *, top: int) -> Any:
        holder = tk.Canvas(
            parent, width=width, height=height, bg=BG, highlightthickness=0, bd=0
        )
        holder.pack(padx=margin, pady=(top, 0), anchor="w")
        round_rect(holder, 1, 1, width - 1, height - 1, self.S(14), fill=CARD, outline=BORDER)
        body = tk.Frame(holder, bg=CARD)
        body.place(x=self.S(16), y=self.S(14), width=width - self.S(32), height=height - self.S(28))
        return body

    def _set_window_icon(self) -> None:
        path = asset_path("crosscopy.png")
        if path is None:
            return
        try:
            self.root.iconphoto(True, tk.PhotoImage(file=str(path)))
        except Exception:  # pragma: no cover - cosmetic only
            logger.debug("could not set the window icon", exc_info=True)

    def _load_logo(self, size: int) -> Any:
        path = asset_path("crosscopy.png")
        if path is None:
            return None
        try:
            from PIL import Image, ImageTk

            with Image.open(path) as image:
                resized = image.convert("RGBA").resize((size, size), Image.LANCZOS)
            return ImageTk.PhotoImage(resized)
        except Exception:  # pragma: no cover - cosmetic only
            logger.debug("could not load the logo", exc_info=True)
            return None

    def _bind_keys(self) -> None:
        self.root.bind("<Escape>", lambda _event: self.close())
        self.root.bind("<Return>", lambda _event: self.save())
        self.root.bind("<Control-s>", lambda _event: self.save())

    # -- live status -------------------------------------------------------

    def _schedule(self, delay: int, callback: Any) -> None:
        try:
            self._after_jobs.add(self.root.after(delay, callback))
        except Exception:  # pragma: no cover - window already gone
            pass

    def _cancel_after_jobs(self) -> None:
        for job in list(self._after_jobs):
            try:
                self.root.after_cancel(job)
            except Exception:  # pragma: no cover
                pass
        self._after_jobs.clear()

    def _tick(self) -> None:
        if self._closing:
            return
        try:
            if not self.root.winfo_exists():
                return
        except Exception:  # pragma: no cover - window already gone
            return
        try:
            while True:
                command = self.commands.get_nowait()
                if command == "focus":
                    self.raise_window()
                elif command == "close":
                    self.close()
                    return
        except queue.Empty:
            pass
        self.refresh()
        self._schedule(900, self._tick)

    def refresh(self) -> None:
        try:
            status = self.app.status()
        except Exception:  # pragma: no cover - app shutting down
            return

        peers = list(status.get("peers") or [])
        if peers:
            count = len(peers)
            self.status_label.configure(
                text=f"Connected to {count} phone" if count == 1 else f"Connected to {count} phones",
                fg=TEXT,
            )
            self.detail_label.configure(text=" \u00b7 ".join(peers), fg=MUTED)
            self.dot.itemconfigure(self._dot_item, fill=OK)
        else:
            self.status_label.configure(text="Waiting for a phone", fg=TEXT)
            self.detail_label.configure(
                text="Open CrossCopy on your phone and paste the link below.", fg=MUTED
            )
            self.dot.itemconfigure(self._dot_item, fill=DIM)

        url = str(status.get("url") or "")
        if url and url != self._last_url:
            self._last_url = url
            self.link_field.set(url)

        error = status.get("error")
        if error and str(error) != self._shown_error:
            self._shown_error = str(error)
            self.message.configure(text=str(error), fg=DANGER)
        elif not error:
            self._shown_error = None

        try:
            enabled = startup.is_enabled()
        except Exception:  # pragma: no cover
            enabled = False
        if enabled != self.autostart_switch.value:
            self.autostart_switch.set(enabled)

    # -- actions -----------------------------------------------------------

    def raise_window(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:  # pragma: no cover
            pass

    def copy_link(self) -> None:
        url = self.link_field.get().strip()
        if not url:
            return
        try:
            if self.app.copy_to_clipboard(url):
                self.message.configure(text="Pairing link copied.", fg=OK)
            else:
                self.message.configure(text="Could not copy the link.", fg=DANGER)
        except Exception:  # pragma: no cover
            logger.exception("could not copy the pairing link")
            self.message.configure(text="Could not copy the link.", fg=DANGER)

    def toggle_token_visibility(self) -> None:
        masked = bool(self.token_field.entry.cget("show"))
        self.token_field.entry.configure(show="" if masked else "\u2022")
        self.show_label.configure(text="Hide" if masked else "Show")

    def toggle_startup(self) -> None:
        try:
            if startup.is_enabled():
                startup.disable()
                self.autostart_switch.set(False)
                self.message.configure(text="CrossCopy will no longer start with Windows.", fg=MUTED)
            else:
                startup.enable()
                self.autostart_switch.set(True)
                self.message.configure(text="CrossCopy will start with Windows from now on.", fg=OK)
        except Exception as exc:
            logger.exception("could not change the autostart setting")
            self.autostart_switch.set(startup.is_enabled())
            self.message.configure(text=f"Could not change autostart: {exc}", fg=DANGER)

    def save(self) -> None:
        values, errors = settings_store.validate_fields(
            self.token_field.get(), self.port_field.get(), self.name_field.get()
        )
        if errors:
            self.message.configure(text=errors[0], fg=DANGER)
            return
        assert values is not None

        token = values["token"]  # type: ignore[assignment]
        port = int(values["port"])  # type: ignore[arg-type]
        name = str(values["device_name"] or "")

        changed = (
            token != (self.app.settings.token or None)
            or port != self.app.settings.port
            or (name and name != self.app.settings.device_name)
        )
        if not changed:
            self.message.configure(text="Nothing changed.", fg=MUTED)
            return

        try:
            settings_store.save(token, port, name)
        except OSError as exc:
            logger.exception("could not write the settings file")
            self.message.configure(text=f"Could not save: {exc}", fg=DANGER)
            return

        new_settings = replace(
            self.app.settings, token=token or None, port=port, device_name=name
        )
        self.app.apply_settings(new_settings)
        self.port_field.set(str(new_settings.port))
        self.name_field.set(new_settings.device_name)
        self._last_url = ""
        self.message.configure(
            text="Saved. Phones must reconnect using the new link.",
            fg=OK,
        )
        self._schedule(400, self.refresh)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._cancel_after_jobs()
        try:
            self.root.destroy()
        except Exception:  # pragma: no cover
            pass


class SettingsWindow:
    """Owns the window thread; the asyncio loop never touches Tk."""

    def __init__(self, app: "CrossCopyApp") -> None:
        self.app = app
        self._thread: threading.Thread | None = None
        self._window: Window | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return tk is not None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def open(self) -> None:
        """Show the window, or bring the existing one to the front."""
        if not self.available:
            logger.warning("the settings window needs tkinter, which is unavailable")
            return
        with self._lock:
            if self.running:
                window = self._window
                if window is not None:
                    window.commands.put("focus")
                return
            self._thread = threading.Thread(
                target=self._thread_main, name="crosscopy-window", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        window = self._window
        if window is not None:
            window.commands.put("close")

    def _thread_main(self) -> None:
        try:
            root = tk.Tk()
        except Exception:
            logger.exception("could not create the window")
            return
        window: Window | None = None
        try:
            window = Window(self.app, root)
            self._window = window
            window.build()
            root.mainloop()
        except Exception:
            logger.exception("the window crashed")
        finally:
            self._window = None
            try:
                root.destroy()
            except Exception:
                pass
