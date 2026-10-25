from __future__ import annotations

import math
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import Any

from ..catalog import APPLICATIONS
from ..config import APP_NAME, DEFAULT_REFRESH_SECONDS
from ..db import UsageDatabase
from ..preferences import get as pref_get, update as pref_update
from ..sync import SyncService
from ..utils import display_model_id
from .widgets import FONT_NUMBER, FONT_UI, NavButton, RoundedButton, StackedBar, Surface, SidebarNavigation, rounded_rect

BG = "#f5f5f7"
SIDEBAR = "#ededf1"
PANEL = "#ffffff"
PANEL_ALT = "#f7f7fa"
BORDER = "#e5e5eb"
TEXT = "#1d1d1f"
MUTED = "#80808b"
BLUE = "#007aff"
GREEN = "#32b59a"
PURPLE = "#8b80e8"
ORANGE = "#f3b35a"
RED = "#e97889"
APP_LABELS = APPLICATIONS
ASSETS = Path(__file__).with_name("assets")


def fmt(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


def time_text(value: Any) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromtimestamp(int(value)).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "—"


def percent(value: int, denominator: int) -> str:
    if denominator <= 0:
        return "—"
    return f"{value / denominator * 100:.1f}%"


def token_units(value: int) -> str:
    divisor, suffix = (1_000_000_000, "B") if value >= 1_000_000_000 else (1_000_000, "M")
    return f"{value / divisor:.2f}{suffix}"


def credit_text(value: Any, places: int = 2) -> str:
    return "—" if value is None else f"{float(value):,.{places}f}"


class HoverTooltip:
    def __init__(self, owner: tk.Tk):
        self.window = tk.Toplevel(owner)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.transient(owner)
        self.window.attributes("-topmost", True)
        background = "#fafafa"
        if sys.platform == "win32":
            background = "#ff00ff"
            self.window.attributes("-transparentcolor", background)
        self.window.configure(bg=background)
        self.surface = Surface(self.window, background=background, fill=PANEL, border="#d9d9e2",
                               radius=12, inset=12, shadow=False, width=220, height=90)
        self.surface.pack()
        self.label = tk.Label(self.surface.inner, bg=PANEL, fg=TEXT, justify="left", anchor="w",
                              font=(FONT_UI, 9), bd=0, padx=0, pady=0)
        self.label.pack(fill="both", expand=True)

    def show(self, event: tk.Event, text: str) -> None:
        self.label.configure(text=text)
        width = self.label.winfo_reqwidth() + 26
        height = self.label.winfo_reqheight() + 26
        self.surface.configure(width=width, height=height)
        self.window.update_idletasks()
        x = min(event.x_root + 14, self.window.winfo_screenwidth() - width - 8)
        y = event.y_root + 18
        if y + height > self.window.winfo_screenheight() - 8:
            y = event.y_root - height - 12
        self.window.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        self.window.deiconify()
        self.window.lift()

    def hide(self, event: tk.Event | None = None) -> None:
        self.window.withdraw()

    def bind_to(self, widget: tk.Widget, text: str) -> None:
        widget.bind("<Enter>", lambda event: self.show(event, text))
        widget.bind("<Motion>", lambda event: self.show(event, text))
        widget.bind("<Leave>", self.hide)


class TokenoteApp(tk.Tk):
    def __init__(self, db: UsageDatabase, service: SyncService):
        super().__init__()
        self.db = db
        self.service = service
        self.selected_app = "all"
        self.chart_mode = "date"
        self.qoder_unit = "credits"
        self.model_page = 0
        self.model_page_size = 8
        self.model_row_height = 54
        self.syncing = False
        self.last_sync_results = []
        self.sync_queue: queue.Queue = queue.Queue()
        self.status_var = tk.StringVar(value="准备同步本地数据")
        self.last_sync_var = tk.StringVar(value="尚未同步")
        self.kpi_subs: dict[str, tk.Label] = {}
        self.kpi_units: dict[str, tk.Label] = {}
        self.kpi_labels: dict[str, tk.Label] = {}
        self._configure_window()
        self._build_style()
        self.tooltip = HoverTooltip(self)
        self._build_layout()
        self.select_app("all")
        self.after(200, self._poll_queue)
        self.after(120, self._startup)
        self.after(DEFAULT_REFRESH_SECONDS * 1000, self._scheduled_sync)

    def _startup(self) -> None:
        self._show_first_run_notice()
        self.after(300, self.start_sync)

    def _show_first_run_notice(self) -> None:
        if not getattr(sys, "frozen", False) or pref_get("first_run_notice_seen", False):
            return
        messagebox.showinfo(
            "使用提示",
            "Tokenote 只读取本机已落盘的用量记录，统计默认保存在 %LOCALAPPDATA%\\Tokenote。\n\n"
            "Trae 首次读取需要 Trae 保持运行；程序会扫描一次 Trae 进程内存以获取数据库密钥，之后使用缓存。",
            parent=self,
        )
        pref_update(first_run_notice_seen=True, trae_memory_consent=True)

    def _configure_window(self) -> None:
        self.title(f"{APP_NAME} · 用量总览")
        self.geometry("1380x920")
        self.minsize(1080, 740)
        self.configure(bg=BG)
        self.app_icon = tk.PhotoImage(file=str(ASSETS / "tokenote.png"))
        self.iconphoto(True, self.app_icon)
        self.bind("<Unmap>", lambda event: self.tooltip.hide() if event.widget == self and hasattr(self, "tooltip") else None)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Tokenote.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=35, borderwidth=0, bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL,
                        font=(FONT_UI, 9), padding=0)
        style.configure("Tokenote.Treeview.Heading", background=PANEL_ALT, foreground=MUTED,
                        relief="flat", borderwidth=0, bordercolor=PANEL_ALT, lightcolor=PANEL_ALT,
                        darkcolor=PANEL_ALT, font=(FONT_UI, 8), padding=(5, 9))
        style.map("Tokenote.Treeview", background=[("selected", "#e9f2ff")], foreground=[("selected", "#0064d2")])
        style.map("Tokenote.Treeview.Heading", background=[("active", PANEL_ALT)])
        for orientation in ("Vertical", "Horizontal"):
            name = f"Tokenote.{orientation}.TScrollbar"
            style.configure(name, background="#d5d5dd", troughcolor=PANEL, borderwidth=0,
                            bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL, arrowcolor="#a1a1ab",
                            arrowsize=10, gripcount=0, width=9)
            style.map(name, background=[("active", "#b8b8c3"), ("pressed", "#a1a1ae")])
            style.layout(name, [(f"{orientation}.Scrollbar.trough", {
                "sticky": "nswe", "children": [(f"{orientation}.Scrollbar.thumb", {"sticky": "nswe", "expand": True})],
            })])

        style.configure("Sidebar.Vertical.TScrollbar", background="#c6c6cf", troughcolor=SIDEBAR, borderwidth=0,
                        bordercolor=SIDEBAR, lightcolor=SIDEBAR, darkcolor=SIDEBAR, arrowsize=0, width=6)
        style.layout("Sidebar.Vertical.TScrollbar", style.layout("Tokenote.Vertical.TScrollbar"))
        style.map("Sidebar.Vertical.TScrollbar", background=[("active", "#a9a9b6"), ("pressed", "#9595a4")])

    def _build_layout(self) -> None:
        self.sidebar = tk.Frame(self, bg=SIDEBAR, width=216)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        brand = tk.Frame(self.sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=18, pady=(29, 25))
        self.agent_icons = {
            key: tk.PhotoImage(file=str(ASSETS / "agents" / f"{key}.png")) for key in APP_LABELS
        }
        self.brand_icon = self.app_icon.subsample(2, 2)
        tk.Label(brand, image=self.brand_icon, bg=SIDEBAR, bd=0).pack(side="left", padx=(0, 10))
        title_box = tk.Frame(brand, bg=SIDEBAR)
        title_box.pack(side="left")
        self.brand_title = tk.Label(title_box, text="TOKENOTE", fg=BLUE, bg=SIDEBAR,
                                    font=(FONT_NUMBER, 16, "bold"), bd=0)
        self.brand_title.pack(anchor="w")
        tk.Label(title_box, text="个人用量工作区", fg=MUTED, bg=SIDEBAR,
                 font=(FONT_UI, 8), bd=0).pack(anchor="w", pady=(3, 0))
        info = Surface(self.sidebar, background=SIDEBAR, fill="#f8f8fa", border="#e0e0e6",
                       height=60, radius=12, inset=10, shadow=False)
        info.pack(side="bottom", fill="x", padx=14, pady=(12, 18))
        first = tk.Frame(info.inner, bg=info.fill)
        first.pack(fill="x")
        dot = tk.Canvas(first, width=8, height=16, bg=info.fill, bd=0, highlightthickness=0)
        dot.create_oval(0, 5, 6, 11, fill=GREEN, outline="")
        dot.pack(side="left", padx=(0, 5))
        tk.Label(first, text="本地数据", fg="#53535e", bg=info.fill, font=(FONT_UI, 9), bd=0).pack(side="left")
        tk.Label(info.inner, text=f"每 {DEFAULT_REFRESH_SECONDS} 秒自动同步", fg=MUTED, bg=info.fill,
                 font=(FONT_UI, 8), bd=0).pack(anchor="w", pady=(5, 0))
        self.navigation = SidebarNavigation(self.sidebar, background=SIDEBAR)
        self.navigation.pack(fill="both", expand=True)
        self.nav_content = self.navigation.inner
        self._sidebar_section("工作区")
        self._nav_button("用量总览", "all")
        self._sidebar_section("应用", pady=(20, 8))
        for key, label in APP_LABELS.items():
            self._nav_button(label, key)
        tk.Frame(self, bg="#dedee5", width=1).pack(side="left", fill="y")
        self.main = tk.Frame(self, bg=BG)
        self.main.pack(side="left", fill="both", expand=True)
        self._build_header()
        self._build_content()
        self._build_status()

    def _sidebar_section(self, text: str, pady=(0, 8)) -> None:
        tk.Label(self.nav_content, text=text, fg="#9696a1", bg=SIDEBAR,
                 font=(FONT_UI, 8), bd=0).pack(anchor="w", padx=27, pady=pady)

    def _nav_button(self, text: str, app_key: str) -> None:
        button = NavButton(self.nav_content, text, lambda key=app_key: self.select_app(key),
                           icon=self.agent_icons.get(app_key), overview=app_key == "all")
        button.pack(fill="x", padx=13, pady=1)
        button.bind("<MouseWheel>", self.navigation.scroll)
        setattr(self, f"nav_{app_key}", button)

    def _build_header(self) -> None:
        header = tk.Frame(self.main, bg=BG)
        header.pack(fill="x", padx=30, pady=(28, 24))
        left = tk.Frame(header, bg=BG)
        left.pack(side="left")
        self.heading = tk.Label(left, text="用量总览", fg=TEXT, bg=BG, font=(FONT_UI, 24, "bold"), bd=0)
        self.heading.pack(anchor="w")
        self.heading_sub = tk.Label(left, text="所有应用的累计 Token 用量", fg=MUTED, bg=BG, font=(FONT_UI, 10), bd=0)
        self.heading_sub.pack(anchor="w", pady=(5, 0))
        actions = tk.Frame(header, bg=BG)
        actions.pack(side="right", anchor="center")
        self.export_button = RoundedButton(actions, "↑  导出 CSV", self.export_csv, width=106, height=36)
        self.export_button.pack(side="left", padx=(0, 10))
        self.sync_button = RoundedButton(actions, "↻  立即同步", self.start_sync, width=120, height=36,
                                         fill=BLUE, foreground="#ffffff", hover="#0069e0", border="", font=(FONT_UI, 9, "bold"))
        self.sync_button.pack(side="left")
        self.qoder_unit_controls = Surface(actions, fill="#eeeef2", background=BG, border="", radius=10,
                                           inset=3, width=140, height=36, shadow=False)
        self.credit_unit_button = RoundedButton(self.qoder_unit_controls.inner, "Credits", lambda: self.set_usage_unit("credits"),
                                                width=76, height=30, radius=7, border="", background="#eeeef2", font=(FONT_NUMBER, 9))
        self.credit_unit_button.pack(side="left", fill="both", expand=True)
        self.token_unit_button = RoundedButton(self.qoder_unit_controls.inner, "Token", lambda: self.set_usage_unit("tokens"),
                                               width=58, height=30, radius=7, border="", background="#eeeef2", font=(FONT_NUMBER, 9))
        self.token_unit_button.pack(side="left", fill="both", expand=True)


    def _build_content(self) -> None:
        self.content = tk.Frame(self.main, bg=BG)
        self.content.pack(fill="both", expand=True, padx=30)
        self.trae_banner = Surface(self.content, fill="#eef5ff", background=BG, border="#dce8f7",
                                   radius=10, inset=12, height=46, shadow=False)
        tk.Label(self.trae_banner.inner, text="首次提取需要 Trae 保持运行；密钥缓存后可直接读取", fg="#3f6d9e",
                 bg="#eef5ff", font=(FONT_UI, 9), bd=0, anchor="w").pack(fill="x")
        self.kpi_row = tk.Frame(self.content, bg=BG)
        self.kpi_row.pack(fill="x", pady=(0, 20))
        self.kpis: dict[str, tk.Label] = {}
        metrics = [("total", "总 Token", BLUE), ("input", "输入 Token", GREEN),
                   ("output", "输出 Token", PURPLE), ("cache", "缓存 Token", ORANGE)]
        for index, (key, label, color) in enumerate(metrics):
            self.kpi_row.columnconfigure(index, weight=1, uniform="kpi")
            card = Surface(self.kpi_row, height=132, width=200, inset=17, radius=14)
            card.grid(row=0, column=index, sticky="nsew", padx=(0, 12 if index < 3 else 0))
            top = tk.Frame(card.inner, bg=PANEL)
            top.pack(fill="x")
            title_label = tk.Label(top, text=label, fg="#6e6e79", bg=PANEL, font=(FONT_UI, 9), bd=0)
            title_label.pack(side="left")
            self.kpi_labels[key] = title_label
            units_color = {"total": "#006ee6", "input": "#268d78", "output": "#7862c8", "cache": "#a87628"}[key]
            units = tk.Label(top, text="0.00M", fg=units_color, bg=PANEL, font=(FONT_NUMBER, 10, "bold"), bd=0)
            units.pack(side="right")
            value = tk.Label(card.inner, text="0", fg=TEXT, bg=PANEL, anchor="w", font=(FONT_NUMBER, 23), bd=0)
            value.pack(fill="x", pady=(9, 0))
            value.bind("<Configure>", lambda event: self._fit_kpi_values())
            sub = tk.Label(card.inner, text="", fg="#9696a1", bg=PANEL, font=(FONT_UI, 8), bd=0)
            sub.pack(anchor="w", pady=(7, 0))
            self.kpis[key], self.kpi_subs[key], self.kpi_units[key] = value, sub, units
        self.body = tk.Frame(self.content, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(0, weight=1)
        self.body.columnconfigure(1, minsize=300)
        self.body.rowconfigure(0, weight=3, minsize=222)
        self.body.rowconfigure(1, weight=2, minsize=160)
        self._build_chart_panel()
        self._build_models_panel()
        self._build_table_panel()

    def _build_chart_panel(self) -> None:
        self.chart_panel = Surface(self.body, height=320, width=300, inset=19)
        self.chart_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        inner = self.chart_panel.inner
        header = tk.Frame(inner, bg=PANEL)
        header.pack(fill="x")
        title = tk.Frame(header, bg=PANEL)
        title.pack(side="left")
        tk.Label(title, text="使用趋势", fg=TEXT, bg=PANEL, font=(FONT_UI, 12, "bold"), bd=0).pack(anchor="w")
        self.chart_hint = tk.Label(title, text="最近 14 天 · 总 Token", fg=MUTED, bg=PANEL, font=(FONT_UI, 8), bd=0)
        self.chart_hint.pack(anchor="w", pady=(4, 0))
        toggles = Surface(header, fill="#eeeef2", background=PANEL, border="", radius=9,
                          inset=3, width=138, height=32, shadow=False)
        toggles.pack(side="right", anchor="n", pady=1)
        self.app_chart_button = RoundedButton(toggles.inner, "按应用", self._set_group_chart_mode,
                                              width=65, height=26, radius=6, border="", background="#eeeef2", font=(FONT_UI, 8))
        self.app_chart_button.pack(side="left", fill="both", expand=True)
        self.date_chart_button = RoundedButton(toggles.inner, "按日期", lambda: self.set_chart_mode("date"),
                                               width=65, height=26, radius=6, border="", background="#eeeef2", font=(FONT_UI, 8))
        self.date_chart_button.pack(side="left", fill="both", expand=True)
        self.chart = tk.Canvas(inner, bg=PANEL, highlightthickness=0, bd=0, height=220)
        self.chart.pack(fill="both", expand=True, pady=(14, 0))
        self.chart.bind("<Configure>", lambda event: self._draw_chart())
        self.chart.bind("<Leave>", self.tooltip.hide)

    def _build_models_panel(self) -> None:
        self.models_panel = Surface(self.body, width=300, inset=18)
        self.models_panel.grid(row=0, column=1, rowspan=2, sticky="nsew")
        inner = self.models_panel.inner
        header = tk.Frame(inner, bg=PANEL)
        header.pack(fill="x")
        tk.Label(header, text="热门模型", fg=TEXT, bg=PANEL, font=(FONT_UI, 12, "bold"), bd=0).pack(side="left")
        self.model_unit_label = tk.Label(header, text="总 Token", fg=MUTED, bg=PANEL, font=(FONT_UI, 8), bd=0)
        self.model_unit_label.pack(side="right")
        self.model_legend = tk.Frame(inner, bg=PANEL)
        self.model_legend.pack(fill="x", pady=(12, 12))
        self._update_model_legend()
        self.model_pager = tk.Frame(inner, bg=PANEL)
        self.model_pager.pack(side="bottom", fill="x", pady=(12, 0))
        self.model_page_label = tk.Label(self.model_pager, text="第 1 页", fg=MUTED, bg=PANEL, font=(FONT_UI, 8), bd=0)
        self.model_page_label.pack(side="left")
        self.model_next = RoundedButton(self.model_pager, "›", lambda: self.change_model_page(1),
                                       width=28, height=26, radius=7, font=(FONT_NUMBER, 13))
        self.model_next.pack(side="right")
        self.model_prev = RoundedButton(self.model_pager, "‹", lambda: self.change_model_page(-1),
                                       width=28, height=26, radius=7, font=(FONT_NUMBER, 13))
        self.model_prev.pack(side="right", padx=(0, 6))
        self.model_list = tk.Frame(inner, bg=PANEL)
        self.model_list.pack(fill="both", expand=True)
        self.model_list.bind("<Configure>", self._resize_model_list)

    def _update_model_legend(self) -> None:
        for child in self.model_legend.winfo_children():
            child.destroy()
        metrics = (("报告用量 · Credits", BLUE),) if self._credits_view() else (("输入", GREEN), ("输出", PURPLE), ("缓存", ORANGE))
        self.model_unit_label.configure(text="Credits" if self._credits_view() else "总 Token")
        for label, color in metrics:
            dot = tk.Canvas(self.model_legend, width=6, height=14, bg=PANEL, bd=0, highlightthickness=0)
            dot.create_oval(0, 4, 6, 10, fill=color, outline="")
            dot.pack(side="left")
            tk.Label(self.model_legend, text=label, fg=MUTED, bg=PANEL, font=(FONT_UI, 8), bd=0).pack(side="left", padx=(5, 15))

    def _build_table_panel(self) -> None:
        self.table_panel = Surface(self.body, height=252, width=300, inset=17)
        self.table_panel.grid(row=1, column=0, sticky="nsew", padx=(0, 18), pady=(18, 0))
        inner = self.table_panel.inner
        header = tk.Frame(inner, bg=PANEL)
        header.pack(fill="x", pady=(0, 15))
        tk.Label(header, text="最近记录", fg=TEXT, bg=PANEL, font=(FONT_UI, 12, "bold"), bd=0).pack(side="left")
        self.table_caption = tk.Label(header, text="新增输入与缓存分列", fg=MUTED, bg=PANEL, font=(FONT_UI, 8), bd=0)
        self.table_caption.pack(side="right")
        table_frame = tk.Frame(inner, bg=PANEL)
        table_frame.pack(fill="both", expand=True)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("time", "app", "model", "input", "output", "cache", "total")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", height=5, style="Tokenote.Treeview")
        headers = {"time": "时间", "app": "应用", "model": "模型 ID", "input": "新增输入", "output": "输出", "cache": "缓存", "total": "总量"}
        widths = {"time": 104, "app": 82, "model": 128, "input": 75, "output": 65, "cache": 75, "total": 82}
        for col in columns:
            anchor = "w" if col in ("time", "app", "model") else "e"
            self.table.heading(col, text=headers[col], anchor=anchor)
            self.table.column(col, width=widths[col], minwidth=80 if col == "model" else widths[col],
                              anchor=anchor, stretch=False)
        self.table.tag_configure("even", background="#ffffff")
        self.table.tag_configure("odd", background="#fafafb")
        self.table.bind("<Configure>", self._resize_table_columns)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview, style="Tokenote.Vertical.TScrollbar")
        self.table_xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview, style="Tokenote.Horizontal.TScrollbar")
        self.table.configure(yscrollcommand=scrollbar.set, xscrollcommand=self._update_table_xscroll)
        self.table.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.table_xscroll.grid(row=1, column=0, sticky="ew")

    def _resize_table_columns(self, event: tk.Event | None = None) -> None:
        width = event.width if event else self.table.winfo_width()
        fixed_width = sum(self.table.column(col, "width") for col in self.table["columns"] if col != "model")
        self.table.column("model", width=max(80, width - fixed_width - 6))

    def _fit_kpi_values(self) -> None:
        font = tkfont.Font(self, family=FONT_NUMBER, size=23)
        for size in range(23, 7, -1):
            font.configure(size=size)
            if all(font.measure(label["text"]) <= max(1, label.winfo_width() - 4)
                   for label in self.kpis.values()):
                break
        for label in self.kpis.values():
            label.configure(font=(FONT_NUMBER, size))

    def _update_table_xscroll(self, first: str, last: str) -> None:
        self.table_xscroll.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self.table_xscroll.grid_remove()
        else:
            self.table_xscroll.grid()

    def _build_status(self) -> None:
        status = tk.Frame(self.main, bg=BG)
        status.pack(side="bottom", before=self.content, fill="x", padx=31, pady=(12, 14))
        self.status_dot = tk.Canvas(status, width=7, height=16, bg=BG, highlightthickness=0, bd=0)
        self.status_dot.create_oval(0, 5, 6, 11, fill=GREEN, outline="", tags="dot")
        self.status_dot.pack(side="left", padx=(0, 6))
        tk.Label(status, textvariable=self.status_var, fg=MUTED, bg=BG, font=(FONT_UI, 8), bd=0).pack(side="left")
        details = tk.Label(status, text="同步详情", fg="#6e8ab1", bg=BG, cursor="hand2", font=(FONT_UI, 8), bd=0, takefocus=True)
        details.pack(side="right", padx=(16, 0))
        details.bind("<Button-1>", lambda event: self.show_sync_details())
        details.bind("<Return>", lambda event: self.show_sync_details())
        tk.Label(status, textvariable=self.last_sync_var, fg="#a0a0ab", bg=BG, font=(FONT_UI, 8), bd=0).pack(side="right")

    def _credits_view(self) -> bool:
        return self.selected_app == "qoder" and self.qoder_unit == "credits"

    def select_app(self, app_key: str) -> None:
        self.selected_app = app_key
        self.model_page = 0
        if self.chart_mode in ("app", "model"):
            self.chart_mode = "app" if app_key == "all" else "model"
        title = "用量总览" if app_key == "all" else APP_LABELS.get(app_key, app_key)
        self.heading.configure(text=title)
        if app_key == "qoder":
            self.qoder_unit_controls.pack(side="left", padx=(0, 12), before=self.export_button)
        else:
            self.qoder_unit_controls.pack_forget()
            self.heading_sub.configure(text="所有应用的累计 Token 用量" if app_key == "all" else f"{title} 的累计 Token 用量")
        for key in ("all", *APP_LABELS):
            getattr(self, f"nav_{key}").set_selected(key == app_key)
        if app_key in ("all", *APP_LABELS):
            self.navigation.ensure_visible(getattr(self, f"nav_{app_key}"))
        if app_key == "trae":
            if not self.trae_banner.winfo_manager():
                self.trae_banner.pack(fill="x", pady=(0, 14), before=self.kpi_row)
        else:
            self.trae_banner.pack_forget()
        self.refresh_view()

    def set_usage_unit(self, unit: str) -> None:
        if unit not in ("credits", "tokens"):
            raise ValueError("Unknown usage unit")
        self.qoder_unit = unit
        self.model_page = 0
        self.refresh_view()

    def set_chart_mode(self, mode: str) -> None:
        if mode == "app" and self.selected_app != "all":
            mode = "model"
        elif mode == "model" and self.selected_app == "all":
            mode = "app"
        self.chart_mode = mode
        self._update_chart_toggle()
        self._draw_chart()

    def _set_group_chart_mode(self) -> None:
        self.set_chart_mode("model" if self.selected_app != "all" else "app")

    def _update_chart_toggle(self) -> None:
        self.app_chart_button.configure(text="按模型" if self.selected_app != "all" else "按应用")
        for button, active in ((self.app_chart_button, self.chart_mode in ("app", "model")),
                               (self.date_chart_button, self.chart_mode == "date")):
            button.set_colors(PANEL if active else "#eeeef2", TEXT if active else MUTED,
                              PANEL if active else "#e8e8ee", "#dedee5" if active else "")

    def refresh_view(self) -> None:
        summary = self.db.summary(self.selected_app)
        credit_summary = self.db.credit_summary("qoder") if self.selected_app == "qoder" else None
        self._qoder_missing_tokens = bool(credit_summary and credit_summary["requests"] and not credit_summary["token_requests"])
        self._qoder_credit_summary = credit_summary
        for key, label in (("total", "总 Token"), ("input", "输入 Token"), ("output", "输出 Token"), ("cache", "缓存 Token")):
            self.kpi_labels[key].configure(text=label)
            self.kpis[key].configure(text="—" if self._qoder_missing_tokens else fmt(summary[key]))
            self.kpi_units[key].configure(text="" if self._qoder_missing_tokens else token_units(summary[key]))
        self.kpi_subs["total"].configure(text=f"{summary['requests']:,} 条用量记录")
        self.kpi_subs["input"].configure(text="包含缓存输入")
        self.kpi_subs["output"].configure(text="模型报告的输出用量")
        self.kpi_subs["cache"].configure(text=f"缓存命中率  {percent(summary['cache'], summary['input'])}")
        if credit_summary is not None:
            for button, active in ((self.credit_unit_button, self.qoder_unit == "credits"), (self.token_unit_button, self.qoder_unit == "tokens")):
                button.set_colors(PANEL if active else "#eeeef2", TEXT if active else MUTED,
                                  PANEL if active else "#e8e8ee", "#dedee5" if active else "")
            if self._credits_view():
                labels = {"total": "报告用量", "input": "计费用量", "output": "已报告请求", "cache": "总 Token"}
                values = {"total": credit_text(credit_summary["credits"]), "input": credit_text(credit_summary["billed_credits"]),
                          "output": fmt(credit_summary["requests"]),
                          "cache": "未提供" if self._qoder_missing_tokens else fmt(summary["total"]) if credit_summary["token_requests"] else "—"}
                for key in self.kpis:
                    self.kpi_labels[key].configure(text=labels[key])
                    self.kpis[key].configure(text=values[key])
                    self.kpi_units[key].configure(text="Credits" if key in ("total", "input") else "次" if key == "output" else
                                                  token_units(summary["total"]) if credit_summary["token_requests"] else "")
                self.kpi_subs["total"].configure(text="报告用量（含未计费）" if credit_summary["credit_reports"] == credit_summary["requests"] else
                                                f"已报告 {credit_summary['credit_reports']} / {credit_summary['requests']} 次")
                self.kpi_subs["input"].configure(text="仅计入已计费请求" if credit_summary["billing_reports"] == credit_summary["requests"] and credit_summary["requests"] else
                                                f"计费标记 {credit_summary['billing_reports']} / {credit_summary['requests']} 次已知" if credit_summary["billing_reports"] else "计费标记未提供")
                self.kpi_subs["output"].configure(text="按模型请求去重")
                self.kpi_subs["cache"].configure(text="平台未提供 Token 明细" if self._qoder_missing_tokens else
                                                f"已提供 {credit_summary['token_requests']} / {credit_summary['requests']} 次")
                self.heading_sub.configure(text="Credits 含未计费用量")
            else:
                self.heading_sub.configure(text="当前未提供 Token 明细" if self._qoder_missing_tokens else
                                            f"Token 明细：{credit_summary['token_requests']} / {credit_summary['requests']} 次请求")
                if self._qoder_missing_tokens:
                    self.kpi_subs["total"].configure(text=f"已检测到 {credit_summary['requests']} 次请求")
                    for key in ("input", "output", "cache"):
                        self.kpi_subs[key].configure(text="平台未提供 Token 明细")
        self._fit_kpi_values()
        self._update_model_legend()
        self._update_chart_toggle()
        self._draw_chart()
        self._draw_models()
        self._draw_table()

    def _draw_chart(self) -> None:
        self.tooltip.hide()
        self.chart.delete("all")
        width = max(self.chart.winfo_width(), 100)
        height = max(self.chart.winfo_height(), 100)
        credits = self._credits_view()
        unit = "Credits" if credits else "Token"
        if self.chart_mode == "date":
            rows = self.db.credit_daily_totals(self.selected_app, days=14) if credits else self.db.daily_totals(self.selected_app, days=14)
            labels = [str(row["day"])[5:].replace("-", "/") for row in rows]
            titles = [str(row["day"]) for row in rows]
            self.chart_hint.configure(text=f"最近 14 天 · {'Credits' if credits else '总 Token'}")
        elif self.chart_mode == "model":
            since = int(time.time()) - 24 * 60 * 60
            rows = self.db.credit_model_totals(self.selected_app, since=since) if credits else self.db.model_totals(self.selected_app, since=since)
            labels = [display_model_id(row["model"]) for row in rows]
            titles = [display_model_id(row["model"]) for row in rows]
            self.chart_hint.configure(text=f"最近 24 小时 · 模型 Top 8 · {'Credits' if credits else '总 Token'}")
        else:
            rows = self.db.credit_app_totals(self.selected_app) if credits else self.db.app_totals()
            labels = [APP_LABELS.get(row["app_type"], row["app_type"]) for row in rows]
            titles = labels
            self.chart_hint.configure(text=f"按应用 · {'Credits' if credits else '总 Token'}")
        if not rows:
            self.chart.create_text(width / 2, height / 2, text="同步后，用量趋势会显示在这里", fill=MUTED, font=(FONT_UI, 10))
            return
        values = [float(row["total"] or 0) if credits else int(row["total"] or 0) for row in rows]
        peak = max(values) or 1
        magnitude = 10 ** math.floor(math.log10(peak))
        max_value = math.ceil(peak / magnitude) * magnitude
        left, right, top, bottom = 45, width - 9, 14, height - 28
        has_usage = any(values)
        for fraction in (0, 0.5, 1):
            y = bottom - (bottom - top) * fraction
            self.chart.create_line(left, y, right, y, fill="#eeeef2", dash=(3, 4) if fraction else ())
            if has_usage or fraction == 0:
                number = credit_text(max_value * fraction, max(2, min(5, 1 - math.floor(math.log10(max_value))))) if credits else self._short_number(round(max_value * fraction))
                self.chart.create_text(left - 9, y, text=number, anchor="e", fill="#a0a0ac", font=(FONT_NUMBER, 8))
        if not has_usage:
            if self._qoder_missing_tokens and not credits:
                empty = "Qoder 未提供 Token 明细"
            elif credits and self._qoder_credit_summary["requests"] and not self._qoder_credit_summary["credit_reports"]:
                empty = "当前请求未报告 Credits"
            elif self.chart_mode == "date":
                empty = "最近 14 天暂无用量记录"
            elif self.chart_mode == "model":
                empty = "最近 24 小时暂无用量记录"
            else:
                empty = "暂无用量记录"
            self.chart.create_text((left + right) / 2, (top + bottom) / 2 - 15, text=empty, fill=MUTED, font=(FONT_UI, 10))
        slot = (right - left) / len(rows)
        if self.chart_mode == "model":
            limit = max(8, int(slot / 6.5))
            labels = [self._shorten_label(label, limit) for label in labels]
        bar_width = max(2, min(38, slot * 0.57))
        label_step = max(1, math.ceil(44 / slot))
        label_indices = list(range(0, len(rows), label_step))
        if label_indices[-1] != len(rows) - 1:
            if len(label_indices) > 1 and (len(rows) - 1 - label_indices[-1]) * slot < 40:
                label_indices.pop()
            label_indices.append(len(rows) - 1)
        for index, (label, value) in enumerate(zip(labels, values)):
            center = left + slot * (index + 0.5)
            x1, x2 = center - bar_width / 2, center + bar_width / 2
            y1 = bottom - max((bottom - top) * value / max_value, 2)
            highlighted = index == (0 if self.chart_mode == "model" else len(rows) - 1)
            color = BLUE if highlighted else "#82b2f5"
            if index in label_indices:
                self.chart.create_text(center, bottom + 17, text=label, fill="#9696a2", font=(FONT_UI, 8))
            bar = rounded_rect(self.chart, x1, y1, x2, bottom, radius=min(5, (bottom - y1) / 2),
                               fill=color if value else "#eaeaf0", outline="", tags=("usage-bar",))
            amount = credit_text(value, 6) if credits and rows[index]["credit_reports"] else "未提供" if credits else fmt(value)
            tooltip_text = f"{titles[index]}\n\n{unit}    {amount}\n模型请求    {fmt(rows[index]['requests'])} 次"
            self.chart.tag_bind(bar, "<Enter>", lambda event, bar=bar, text=tooltip_text: self._chart_hover(event, bar, text))
            self.chart.tag_bind(bar, "<Motion>", lambda event, text=tooltip_text: self.tooltip.show(event, text))
            self.chart.tag_bind(bar, "<Leave>", lambda event, bar=bar, color=color if value else "#eaeaf0": self._chart_leave(bar, color))

    def _chart_hover(self, event: tk.Event, bar: int, text: str) -> None:
        self.chart.itemconfigure(bar, fill=BLUE)
        self.tooltip.show(event, text)

    def _chart_leave(self, bar: int, color: str) -> None:
        self.chart.itemconfigure(bar, fill=color)
        self.tooltip.hide()

    @staticmethod
    def _short_number(value: int) -> str:
        for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
            if value >= divisor:
                return f"{value / divisor:.0f}{suffix}" if value % divisor == 0 else f"{value / divisor:.1f}{suffix}"
        return str(value)

    @staticmethod
    def _shorten_label(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        head = (limit - 1) // 2
        tail = limit - 1 - head
        return text[:head] + "…" + text[-tail:]

    def change_model_page(self, delta: int) -> None:
        new_page = max(0, self.model_page + delta)
        if new_page != self.model_page:
            self.model_page = new_page
            self._draw_models()

    def _resize_model_list(self, event: tk.Event) -> None:
        page_size = max(1, event.height // self.model_row_height)
        if page_size != self.model_page_size:
            first_index = self.model_page * self.model_page_size
            self.model_page_size = page_size
            self.model_page = first_index // page_size
            self._draw_models()

    def _draw_models(self) -> None:
        self.tooltip.hide()
        for child in self.model_list.winfo_children():
            child.destroy()
        credits = self._credits_view()
        query = self.db.credit_model_totals if credits else self.db.model_totals
        rows = query(self.selected_app, limit=self.model_page_size + 1, offset=self.model_page * self.model_page_size)
        has_next = len(rows) > self.model_page_size
        rows = rows[:self.model_page_size]
        if not rows and self.model_page > 0:
            self.model_page -= 1
            self._draw_models()
            return
        self.model_page_label.configure(text=f"第 {self.model_page + 1} 页")
        self.model_prev.configure(state="normal" if self.model_page > 0 else "disabled")
        self.model_next.configure(state="normal" if has_next else "disabled")
        if not rows:
            empty = "Qoder 未提供 Token 明细" if self._qoder_missing_tokens and not credits else "暂无数据"
            tk.Label(self.model_list, text=empty, fg=MUTED, bg=PANEL, font=(FONT_UI, 10), bd=0).pack(anchor="w", pady=12)
            return
        max_total = max(float(row["total"] or 0) for row in rows) or 1
        for row in rows:
            item = tk.Frame(self.model_list, bg=PANEL, height=self.model_row_height)
            item.pack(fill="x")
            item.pack_propagate(False)
            line = tk.Frame(item, bg=PANEL)
            line.pack(fill="x", pady=(6, 0))
            amount = credit_text(row["credits"]) if credits else fmt(row["total"])
            tk.Label(line, text=amount, fg="#898995", bg=PANEL, font=(FONT_NUMBER, 9), bd=0).pack(side="right", padx=(7, 0))
            tk.Label(line, text=display_model_id(row["model"]), fg="#44444e", bg=PANEL, font=(FONT_UI, 9),
                     anchor="w", width=1, bd=0).pack(side="left", fill="x", expand=True)
            segments = [("Credits", float(row["credits"] or 0), BLUE)] if credits else [
                ("输入（不含缓存）", int(row["fresh_input"] or 0), GREEN),
                ("输出", int(row["output"] or 0), PURPLE), ("缓存（读取 + 写入）", int(row["cache"] or 0), ORANGE)]
            track = StackedBar(item, tuple(value for _, value, _ in segments),
                               tuple(color for _, _, color in segments), float(row["total"] or 0) / max_total)
            track.pack(fill="x", pady=(9, 0))
            if credits:
                tooltip_text = (f"{display_model_id(row['model'])}\n\n报告 Credits    {credit_text(row['credits'], 6)}"
                                f"\n计费 Credits    {credit_text(row['billed_credits'], 6)}\n模型请求    {fmt(row['requests'])} 次")
            else:
                segment_total = sum(value for _, value, _ in segments)
                lines = [display_model_id(row["model"]), "", f"总 Token    {fmt(row['total'])}"]
                lines.extend(f"{label}    {fmt(value)}  ·  {percent(value, segment_total)}" for label, value, _ in segments)
                tooltip_text = "\n".join(lines)
            self.tooltip.bind_to(track, tooltip_text)

    @staticmethod
    def _fresh_input(row) -> int:
        exclusive = {"provider_input_plus_cache_buckets", "session_cumulative_fallback", "anthropic_exclusive_cache",
                     "pi_exclusive_cache", "hermes_exclusive_cache_aggregate", "minimax_exclusive_cache", "zcode_exclusive_cache"}
        if row["input_token_semantics"] in exclusive:
            return int(row["input_tokens"])
        return max(0, int(row["input_tokens"]) - int(row["cache_read_tokens"]) - int(row["cache_creation_tokens"]))

    def _draw_table(self) -> None:
        self.table.delete(*self.table.get_children())
        credits = self._credits_view()
        if credits:
            rows = self.db.credit_recent(self.selected_app, 100)
        elif self.selected_app == "qoder":
            rows = self.db.credit_recent_with_tokens("qoder", 100)
        else:
            rows = self.db.recent(self.selected_app, 100)
        headers = {"time": "时间", "app": "应用", "model": "模型 ID", "input": "新增输入", "output": "输出", "cache": "缓存", "total": "总量"}
        widths = {"time": 104, "app": 82, "model": 128, "input": 75, "output": 65, "cache": 75, "total": 82}
        if credits:
            headers.update(input="Credits", output="原始 Credits", cache="计费", total="Token")
            widths.update(output=85, cache=65)
        if any(row["app_type"] == "deepseek_harness" for row in rows):
            widths["app"] = 120
        for col in self.table["columns"]:
            anchor = "w" if col in ("time", "app", "model") else "e"
            self.table.heading(col, text=headers[col], anchor=anchor)
            self.table.column(col, width=widths[col], minwidth=80 if col == "model" else widths[col], anchor=anchor, stretch=False)
        self._resize_table_columns()
        self.table_caption.configure(text="报告用量与计费状态" if credits else "Qoder 未提供 Token 明细" if self._qoder_missing_tokens else "新增输入与缓存分列")
        for index, row in enumerate(rows):
            if credits:
                billed = "未提供" if row["billable"] is None else "计费" if row["billable"] else "未计费"
                values = (credit_text(row["credits"], 6), credit_text(row["original_credits"], 6), billed,
                          "—" if row["token_total"] is None else fmt(row["token_total"]))
            elif self.selected_app == "qoder" and row["input_tokens"] is None:
                values = ("—", "—", "—", "—")
            else:
                values = (fmt(self._fresh_input(row)), fmt(row["output_tokens"]), fmt(row["cache_read_tokens"]),
                          fmt(row["token_total"] if self.selected_app == "qoder" else row["total_tokens"]))
            self.table.insert("", "end", tags=("odd" if index % 2 else "even",), values=(
                "会话累计" if row["source_kind"] == "hermes_session_aggregate" else time_text(row["occurred_at"]),
                APP_LABELS.get(row["app_type"], row["app_type"]), display_model_id(row["model"]), *values))

    def start_sync(self) -> None:
        if self.syncing:
            return
        self.syncing = True
        self.sync_button.configure(state="disabled", text="↻  同步中…")
        self.status_dot.itemconfigure("dot", fill=BLUE)
        self.status_var.set("正在同步本地数据…")
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self) -> None:
        try:
            self.sync_queue.put(("done", self.service.sync_all()))
        except Exception as exc:
            self.sync_queue.put(("error", str(exc)))

    def _poll_queue(self) -> None:
        try:
            kind, payload = self.sync_queue.get_nowait()
        except queue.Empty:
            self.after(200, self._poll_queue)
            return
        self.syncing = False
        self.sync_button.configure(state="normal", text="↻  立即同步")
        if kind == "error":
            self.status_var.set(f"同步失败：{payload}")
            self.status_dot.itemconfigure("dot", fill=RED)
        else:
            self.last_sync_results = payload
            errors = [item for item in payload if item.error]
            missing = sum(item.availability == "not_found" for item in payload)
            self.status_var.set(f"已同步 · {len(payload) - missing - len(errors)} 个可用来源"
                                + (f" · {len(errors)} 个失败" if errors else ""))
            self.status_dot.itemconfigure("dot", fill=ORANGE if errors else GREEN)
            self.last_sync_var.set("更新于 " + datetime.now().strftime("%H:%M:%S"))
            self.refresh_view()
        self.after(200, self._poll_queue)

    def show_sync_details(self) -> None:
        if not self.last_sync_results:
            messagebox.showinfo("同步详情", "首次同步正在进行，完成后可在此查看各来源结果。")
            return
        lines = []
        for item in self.last_sync_results:
            label = APP_LABELS.get(item.adapter, item.adapter)
            if item.availability == "not_found":
                lines.append(f"{label}：未发现数据源（安装后自动检测；公开格式已适配，本机尚未验证）")
            elif item.error:
                lines.append(f"{label}：失败 — {item.error}")
            else:
                lines.append(f"{label}：处理 {fmt(item.imported)} 条，扫描 {item.scanned} 个文件/数据源")
        lines.append("Hermes 当前读取会话/模型累计值，仅计入总览；没有逐请求时间，故不进入按日期趋势。")
        messagebox.showinfo("同步详情", "\n\n".join(lines))

    def _scheduled_sync(self) -> None:
        self.start_sync()
        self.after(DEFAULT_REFRESH_SECONDS * 1000, self._scheduled_sync)

    def export_csv(self) -> None:
        path = filedialog.asksaveasfilename(title="导出用量明细", defaultextension=".csv", filetypes=[("CSV 文件", "*.csv")])
        if not path:
            return
        try:
            if self._credits_view():
                self.db.export_credits_csv(Path(path), self.selected_app)
            elif self.selected_app == "qoder":
                self.db.export_credit_tokens_csv(Path(path), "qoder")
            else:
                self.db.export_csv(Path(path), self.selected_app)
            messagebox.showinfo("导出完成", f"已导出到：\n{path}")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))
