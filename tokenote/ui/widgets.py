from __future__ import annotations

import math
import tkinter as tk
from typing import Callable

FONT_UI = "Microsoft YaHei UI"
FONT_NUMBER = "Segoe UI"


def rounded_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                 radius: float = 12, **options) -> int:
    # Keep outlines on whole pixels. Fractional baselines combined with spline
    # interpolation can switch raster rows midway through a straight edge.
    if options.get("outline"):
        x1, y1, x2, y2 = math.ceil(x1), math.ceil(y1), math.floor(x2), math.floor(y2)
    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = []
    for cx, cy, angle in ((x2 - radius, y1 + radius, -90), (x2 - radius, y2 - radius, 0),
                          (x1 + radius, y2 - radius, 90), (x1 + radius, y1 + radius, 180)):
        for step in range(9):
            theta = math.radians(angle + step * 90 / 8)
            points.extend((round(cx + radius * math.cos(theta), 4), round(cy + radius * math.sin(theta), 4)))
    return canvas.create_polygon(points, smooth=False, **options)


class Surface(tk.Canvas):
    """A rounded, resizable surface with a regular Tk content frame."""

    def __init__(self, parent, *, fill: str = "#ffffff", background: str = "#f5f5f7",
                 border: str = "#e7e7ec", radius: int = 16, inset: int = 18,
                 shadow: bool = True, **kwargs):
        super().__init__(parent, bg=background, highlightthickness=0, bd=0, **kwargs)
        self.fill = fill
        self.border = border
        self.radius = radius
        self.inset = inset
        self.shadow = shadow
        self.inner = tk.Frame(self, bg=fill, bd=0)
        self._content = self.create_window(inset, inset, anchor="nw", window=self.inner)
        self.bind("<Configure>", self._resize)

    def _resize(self, event: tk.Event) -> None:
        self.delete("surface")
        width, height = event.width, event.height
        if self.shadow:
            rounded_rect(self, 1, 2, width - 2, height - 1, self.radius,
                         fill="#e9e9ee", outline="", tags="surface")
        rounded_rect(self, 0.5, 0.5, width - 0.5, height - 1.5, self.radius,
                     fill=self.fill, outline=self.border, width=1, tags="surface")
        self.tag_lower("surface")
        self.itemconfigure(self._content, width=max(1, width - self.inset * 2),
                           height=max(1, height - self.inset * 2))


class RoundedButton(tk.Canvas):
    def __init__(self, parent, text: str, command: Callable[[], None], *, width: int = 104,
                 height: int = 36, fill: str = "#ffffff", foreground: str = "#303037",
                 hover: str = "#f4f4f6", border: str = "#dfdfe5", radius: int = 10,
                 font: tuple = (FONT_UI, 9), background: str | None = None):
        super().__init__(parent, width=width, height=height, bg=background or parent.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=True)
        self.text = text
        self.command = command
        self.fill = fill
        self.foreground = foreground
        self.hover = hover
        self.border = border
        self.radius = radius
        self.font = font
        self._state = "normal"
        self._hovered = False
        self._pressed = False
        self.bind("<Configure>", lambda event: self._draw())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", lambda event: self._activate())
        self.bind("<space>", lambda event: self._activate())
        self.bind("<FocusIn>", lambda event: self._draw())
        self.bind("<FocusOut>", lambda event: self._draw())

    def configure(self, cnf=None, **kwargs):
        if "text" in kwargs:
            self.text = kwargs.pop("text")
        if "state" in kwargs:
            self._state = kwargs.pop("state")
            kwargs["cursor"] = "hand2" if self._state == "normal" else "arrow"
        result = super().configure(cnf, **kwargs)
        if hasattr(self, "text"):
            self._draw()
        return result

    config = configure

    def cget(self, key):
        if key == "text":
            return self.text
        if key == "state":
            return self._state
        return super().cget(key)

    __getitem__ = cget

    def set_colors(self, fill: str, foreground: str, hover: str, border: str = "") -> None:
        self.fill, self.foreground, self.hover, self.border = fill, foreground, hover, border
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        color = self.hover if self._hovered or self._pressed else self.fill
        foreground = "#a1a1aa" if self._state == "disabled" else self.foreground
        border = "#80b8ff" if self.focus_get() == self else self.border
        rounded_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, self.radius,
                     fill=color, outline=border, width=1)
        self.create_text(width / 2, height / 2 + (1 if self._pressed else 0),
                         text=self.text, fill=foreground, font=self.font)

    def _enter(self, event) -> None:
        self._hovered = self._state == "normal"
        self._draw()

    def _leave(self, event) -> None:
        self._hovered = False
        self._pressed = False
        self._draw()

    def _press(self, event) -> None:
        if self._state == "normal":
            self._pressed = True
            self.focus_set()
            self._draw()

    def _release(self, event) -> None:
        activate = self._pressed and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        self._pressed = False
        self._draw()
        if activate:
            self._activate()

    def _activate(self) -> None:
        if self._state == "normal":
            self.command()


class NavButton(RoundedButton):
    def __init__(self, parent, text: str, command: Callable[[], None], *,
                 icon: tk.PhotoImage | None = None, overview: bool = False):
        self.icon = icon
        self.overview = overview
        self.selected = False
        super().__init__(parent, text, command, height=34, fill=parent.cget("bg"),
                         hover="#e3e3e9", border="", radius=9)

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        fill = "#ffffff" if self.selected else (self.hover if self._hovered else self.fill)
        if self.selected or self._hovered:
            rounded_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, 9,
                         fill=fill, outline="#dedee5" if self.selected else "")
        if self.focus_get() == self:
            rounded_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, 9,
                         fill="", outline="#80b8ff", width=1)
        center = height / 2
        if self.overview:
            for x in (14, 24):
                for y in (center - 8, center + 2):
                    rounded_rect(self, x, y, x + 7, y + 7, 2, fill="#007aff", outline="")
        elif self.icon is not None:
            self.create_image(23, center, image=self.icon)
        self.create_text(45, center, text=self.text, anchor="w",
                         fill="#1d1d1f" if self.selected else "#50505a",
                         font=(FONT_UI, 10, "bold" if self.selected else "normal"))


class StackedBar(tk.Canvas):
    def __init__(self, parent, values: tuple[int, ...], colors: tuple[str, ...], ratio: float):
        super().__init__(parent, height=7, bg=parent.cget("bg"), highlightthickness=0, bd=0)
        self.values, self.colors, self.ratio = values, colors, max(0, min(1, ratio))
        self.bind("<Configure>", self._draw)

    def _draw(self, event=None) -> None:
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        rounded_rect(self, 0, 0, width, height, height / 2, fill="#f0f0f4", outline="")
        total = sum(self.values)
        if not total:
            return
        filled_width = width * self.ratio
        offset = 0.0
        active = [(value, color) for value, color in zip(self.values, self.colors) if value > 0]
        for index, (value, color) in enumerate(active):
            end = offset + filled_width * value / total
            if index == 0 or index == len(active) - 1:
                rounded_rect(self, offset, 0, end, height, height / 2, fill=color, outline="")
                if len(active) > 1:
                    if index == 0:
                        self.create_rectangle(min(offset + height / 2, end), 0, end, height, fill=color, outline="")
                    else:
                        self.create_rectangle(offset, 0, max(offset, end - height / 2), height, fill=color, outline="")
            else:
                self.create_rectangle(offset, 0, end, height, fill=color, outline="")
            offset = end

class SidebarNavigation(tk.Frame):
    """Keep navigation scrollable while the brand and local status stay fixed."""

    def __init__(self, parent, *, background: str):
        super().__init__(parent, bg=background)
        from tkinter import ttk
        self.canvas = tk.Canvas(self, bg=background, highlightthickness=0, bd=0,
                                width=1, height=1, yscrollincrement=24)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                       style="Sidebar.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=background)
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self.inner.bind("<Configure>", lambda event: self._resize())
        self.canvas.bind("<Configure>", lambda event: self._resize())
        self.canvas.bind("<MouseWheel>", self.scroll)
        self.inner.bind("<MouseWheel>", self.scroll)

    def _resize(self) -> None:
        self.canvas.itemconfigure(self.window, width=max(1, self.canvas.winfo_width()))
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), self.inner.winfo_reqheight()))
        overflow = self.inner.winfo_reqheight() > self.canvas.winfo_height()
        if overflow and not self.scrollbar.winfo_manager():
            self.scrollbar.pack(side="right", fill="y", before=self.canvas)
        elif not overflow and self.scrollbar.winfo_manager():
            self.scrollbar.pack_forget()
            self.canvas.yview_moveto(0)

    def scroll(self, event):
        if event.delta and self.inner.winfo_reqheight() > self.canvas.winfo_height():
            steps = max(1, abs(event.delta) // 120)
            self.canvas.yview_scroll(-steps if event.delta > 0 else steps, "units")
            return "break"

    def ensure_visible(self, widget) -> None:
        if self.canvas.winfo_height() <= 1:
            return
        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        y = widget.winfo_y()
        height = max(1, self.inner.winfo_reqheight())
        if y < top:
            self.canvas.yview_moveto(max(0, y - 24) / height)
        elif y + widget.winfo_height() > bottom:
            self.canvas.yview_moveto((y + widget.winfo_height() - self.canvas.winfo_height() + 24) / height)
