#!/usr/bin/env python3
"""
system_monitor.py
------------------
Standalone touchscreen System Monitor -- CPU / RAM / network graphs for
one or more machines, switchable via a bottom nav bar.

Runs standalone (no dependency on relay_touch_wiz_remote.py). Configured
machines live in config.json next to this script:

    {
        "machines": [
            {"id": "local", "label": "This Machine", "type": "local"}
        ],
        "poll_interval_ms": 1000,
        "history_length": 60
    }

To add a remote machine, run system_monitor_agent.py on it (see that
file for setup) and add an entry like:

    {"id": "shackpi", "label": "Graywolf", "type": "remote",
     "host": "192.168.1.50", "port": 5006}

A new tab appears in the nav bar automatically -- no code changes needed.

Requires: psutil  (pip install psutil --break-system-packages)
"""

import json
import os
import socket
import time
from collections import deque
from tkinter import Button, Canvas, Frame, Label, Tk
from tkinter import font

try:
    import psutil
except ImportError:
    psutil = None
    print(
        "[MONITOR WARNING] psutil not installed -- local monitoring will be "
        "unavailable. Install it with: pip install psutil --break-system-packages"
    )

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_monitor_config.json")

DEFAULT_MACHINES = [
    {"id": "local", "label": socket.gethostname(), "type": "local"},
]
DEFAULT_POLL_INTERVAL_MS = 1000
DEFAULT_HISTORY_LENGTH = 60
REMOTE_TIMEOUT_SECONDS = 2

is_fullscreen = True


# ============================================================
# CONFIG FILE PERSISTENCE
# ============================================================

def load_config():
    machines = DEFAULT_MACHINES
    poll_interval_ms = DEFAULT_POLL_INTERVAL_MS
    history_length = DEFAULT_HISTORY_LENGTH

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            machines = data.get("machines", machines)
            poll_interval_ms = data.get("poll_interval_ms", poll_interval_ms)
            history_length = data.get("history_length", history_length)
            print(f"[CONFIG] Loaded {len(machines)} machine(s) from {CONFIG_FILE}")
        except Exception as err:
            print(f"[CONFIG ERROR] Failed to load {CONFIG_FILE}: {err}")
    else:
        save_config(machines, poll_interval_ms, history_length)

    return machines, poll_interval_ms, history_length


def save_config(machines, poll_interval_ms, history_length):
    data = {
        "machines": machines,
        "poll_interval_ms": poll_interval_ms,
        "history_length": history_length,
    }
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=4)
        print(f"[CONFIG] Saved to {CONFIG_FILE}")
    except Exception as err:
        print(f"[CONFIG ERROR] Failed to save {CONFIG_FILE}: {err}")


MACHINES, POLL_INTERVAL_MS, HISTORY_LENGTH = load_config()


# ============================================================
# STATS SOURCES
# ============================================================

_local_last_counters = None
_local_last_time = None


def get_local_stats():
    """Read this machine's own stats directly via psutil."""
    global _local_last_counters, _local_last_time

    if psutil is None:
        return None

    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory().percent

    now = time.monotonic()
    counters = psutil.net_io_counters()
    up_kbs = down_kbs = 0.0
    if _local_last_counters is not None and _local_last_time is not None:
        elapsed = max(now - _local_last_time, 0.001)
        up_kbs = max((counters.bytes_sent - _local_last_counters.bytes_sent) / 1024 / elapsed, 0)
        down_kbs = max((counters.bytes_recv - _local_last_counters.bytes_recv) / 1024 / elapsed, 0)
    _local_last_counters = counters
    _local_last_time = now

    return {"cpu": cpu, "ram": ram, "net_up": up_kbs, "net_down": down_kbs}


def get_remote_stats(host, port):
    """Query a machine running system_monitor_agent.py over TCP."""
    try:
        with socket.create_connection((host, port), timeout=REMOTE_TIMEOUT_SECONDS) as sock:
            sock.sendall(b"STATS\n")
            data = sock.recv(1024).decode("utf-8", errors="replace").strip()
        _hostname, cpu, ram, up_kbs, down_kbs = data.split("|")
        return {"cpu": float(cpu), "ram": float(ram), "net_up": float(up_kbs), "net_down": float(down_kbs)}
    except Exception as err:
        print(f"[REMOTE ERROR] {host}:{port} -- {err}")
        return None


def get_stats(machine):
    if machine["type"] == "local":
        return get_local_stats()
    elif machine["type"] == "remote":
        return get_remote_stats(machine["host"], machine["port"])
    return None


# ============================================================
# PER-MACHINE HISTORY
# ============================================================

def make_history():
    return {
        "cpu": deque(maxlen=HISTORY_LENGTH),
        "ram": deque(maxlen=HISTORY_LENGTH),
        "net_up": deque(maxlen=HISTORY_LENGTH),
        "net_down": deque(maxlen=HISTORY_LENGTH),
    }


histories = {m["id"]: make_history() for m in MACHINES}
machine_unreachable = {m["id"]: False for m in MACHINES}


# ============================================================
# UI SETUP
# ============================================================

root = Tk()
root.title("System Monitor")
root.attributes("-fullscreen", is_fullscreen)
root.configure(bg="#1e1e1e")
root.bind("<Escape>", lambda e: root.destroy())

big_font = font.Font(family="Helvetica", size=28, weight="bold")
medium_font = font.Font(family="Helvetica", size=22, weight="bold")
small_font = font.Font(family="Helvetica", size=16)

content_frame = Frame(root, bg="#1e1e1e")
content_frame.pack(side="top", expand=True, fill="both", padx=20, pady=(10, 10))
content_frame.pack_propagate(False)

page_title_frame = Frame(root, bg="#1e1e1e")
page_title_frame.pack(side="bottom", fill="x")

page_title_label = Label(page_title_frame, text="", font=medium_font, bg="#1e1e1e", fg="white")
page_title_label.pack(pady=(6, 6))

tab_bar = Frame(root, bg="#111111", height=80)
tab_bar.pack(side="bottom", fill="x")
tab_bar.pack_propagate(False)

active_machine_id = MACHINES[0]["id"] if MACHINES else None
monitor_job = None
monitor_canvas = None


def toggle_fullscreen():
    global is_fullscreen
    is_fullscreen = not is_fullscreen
    root.attributes("-fullscreen", is_fullscreen)
    if not is_fullscreen:
        root.geometry("800x480")


def clear_content():
    stop_updates()
    for widget in content_frame.winfo_children():
        widget.destroy()


def stop_updates():
    global monitor_job
    if monitor_job is not None:
        try:
            root.after_cancel(monitor_job)
        except Exception:
            pass
        monitor_job = None


def rebuild_tabs():
    for widget in tab_bar.winfo_children():
        widget.destroy()

    tabs = [(m["id"], m["label"]) for m in MACHINES] + [("__system__", "SYSTEM")]

    for index, (machine_id, label) in enumerate(tabs):
        tab_bar.grid_columnconfigure(index, weight=1)

        if machine_id == "__system__":
            bg, active_bg = "#8e44ad", "#9b59b6"
            command = open_system_drawer
        elif machine_id == active_machine_id:
            bg, active_bg = "#27ae60", "#2ecc71"
            command = lambda mid=machine_id: show_machine_page(mid)
        else:
            bg, active_bg = "#34495e", "#46627f"
            command = lambda mid=machine_id: show_machine_page(mid)

        Button(
            tab_bar, text=label.upper(), font=font.Font(family="Helvetica", size=13, weight="bold"),
            bg=bg, fg="white", activebackground=active_bg, activeforeground="white",
            relief="flat", command=command,
        ).grid(row=0, column=index, sticky="nsew", padx=2, pady=2)

    tab_bar.grid_rowconfigure(0, weight=1)


# ============================================================
# SYSTEM DRAWER (fullscreen / exit)
# ============================================================

drawer_overlay = Frame(root, bg="#000000")
side_drawer = Frame(drawer_overlay, bg="#2c3e50", width=260)


def open_system_drawer():
    drawer_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
    side_drawer.pack(side="right", fill="y")
    side_drawer.pack_propagate(False)
    rebuild_drawer_contents()


def close_system_drawer():
    drawer_overlay.place_forget()


drawer_overlay.bind("<Button-1>", lambda e: close_system_drawer() if e.widget == drawer_overlay else None)


def rebuild_drawer_contents():
    for widget in side_drawer.winfo_children():
        widget.destroy()

    header = Frame(side_drawer, bg="#1a252f", height=60)
    header.pack(fill="x")
    header.pack_propagate(False)
    Label(header, text="SYSTEM", font=medium_font, bg="#1a252f", fg="white").pack(side="left", padx=15, pady=10)
    Button(
        header, text="\u2715", font=medium_font, bg="#1a252f", fg="white",
        activebackground="#c0392b", activeforeground="white", relief="flat", bd=0,
        command=close_system_drawer,
    ).pack(side="right", padx=10)

    items_frame = Frame(side_drawer, bg="#2c3e50")
    items_frame.pack(fill="both", expand=True, padx=10, pady=15)

    def cmd_fullscreen():
        toggle_fullscreen()
        rebuild_drawer_contents()

    fs_text = "\U0001F5D7  WINDOWED" if is_fullscreen else "\u26F6  FULLSCREEN"
    Button(
        items_frame, text=fs_text, font=small_font, bg="#34495e", fg="white",
        activebackground="#46627f", activeforeground="white", relief="flat",
        anchor="w", padx=15, command=cmd_fullscreen,
    ).pack(fill="x", pady=6, ipady=10)

    Button(
        items_frame, text="\u2715  EXIT PROGRAM", font=small_font, bg="#7f8c8d", fg="white",
        activebackground="#95a5a6", activeforeground="white", relief="flat",
        anchor="w", padx=15, command=root.destroy,
    ).pack(fill="x", pady=6, ipady=10)


# ============================================================
# GRAPH DRAWING (same combined dual-axis style as the wiz remote)
# ============================================================

def scaled_line_points(values, plot_w, plot_h, pad_left, pad_top, scale_max):
    points = []
    for i, val in enumerate(values):
        x = pad_left + (i / (HISTORY_LENGTH - 1)) * plot_w
        y = pad_top + plot_h - (min(val, scale_max) / scale_max) * plot_h
        points.append((x, y))
    shift = (plot_w + pad_left) - points[-1][0]
    return [coord for x, y in points for coord in (x + shift, y)]


def draw_combined_graph(canvas, cpu_series, ram_series, up_series, down_series, unreachable=False):
    canvas.delete("all")

    width = canvas.winfo_width()
    height = canvas.winfo_height()
    if width <= 1 or height <= 1:
        return

    if unreachable:
        canvas.create_text(
            width / 2, height / 2, text="MACHINE UNREACHABLE",
            fill="#e74c3c", font=("Helvetica", 16, "bold"),
        )
        return

    pad_left, pad_right, pad_top, pad_bottom = 46, 54, 26, 10
    plot_w = max(width - pad_left - pad_right, 1)
    plot_h = max(height - pad_top - pad_bottom, 1)

    cpu_vals = list(cpu_series) if cpu_series else [0]
    ram_vals = list(ram_series) if ram_series else [0]
    up_vals = list(up_series) if up_series else [0]
    down_vals = list(down_series) if down_series else [0]

    pct_scale = 100
    net_scale = max(max(up_vals, default=0), max(down_vals, default=0), 1) * 1.2

    for frac in (0.0, 0.5, 1.0):
        y = pad_top + plot_h - (frac * plot_h)
        canvas.create_line(pad_left, y, pad_left + plot_w, y, fill="#3a4a52", dash=(2, 3))
        canvas.create_text(
            pad_left - 8, y, text=f"{pct_scale * frac:.0f}%",
            fill="#7f8c8d", font=("Helvetica", 8), anchor="e",
        )
        canvas.create_text(
            pad_left + plot_w + 8, y, text=f"{net_scale * frac:.0f}",
            fill="#7f8c8d", font=("Helvetica", 8), anchor="w",
        )

    canvas.create_text(
        pad_left + plot_w + 8, pad_top - 12, text="KB/s",
        fill="#7f8c8d", font=("Helvetica", 8), anchor="w",
    )

    for values, scale_max, color in (
        (cpu_vals, pct_scale, "#2ecc71"),
        (ram_vals, pct_scale, "#3498db"),
        (up_vals, net_scale, "#9b59b6"),
        (down_vals, net_scale, "#e67e22"),
    ):
        if len(values) >= 2:
            canvas.create_line(
                *scaled_line_points(values, plot_w, plot_h, pad_left, pad_top, scale_max),
                fill=color, width=2, smooth=True,
            )

    legend = (
        (0.0, f"CPU {cpu_vals[-1]:.0f}%", "#2ecc71"),
        (0.27, f"RAM {ram_vals[-1]:.0f}%", "#3498db"),
        (0.54, f"\u2191 {up_vals[-1]:.1f} KB/s", "#9b59b6"),
    )
    for frac, text, color in legend:
        canvas.create_text(
            pad_left + frac * plot_w, 6, text=text, fill=color,
            font=("Helvetica", 10, "bold"), anchor="nw",
        )
    canvas.create_text(
        pad_left + plot_w, 6, text=f"\u2193 {down_vals[-1]:.1f} KB/s", fill="#e67e22",
        font=("Helvetica", 10, "bold"), anchor="ne",
    )


# ============================================================
# PAGE / POLLING
# ============================================================

def find_machine(machine_id):
    return next((m for m in MACHINES if m["id"] == machine_id), None)


def poll_active_machine():
    global monitor_job

    machine = find_machine(active_machine_id)
    if machine is None:
        return

    stats = get_stats(machine)
    hist = histories[machine["id"]]

    if stats is None:
        machine_unreachable[machine["id"]] = True
    else:
        machine_unreachable[machine["id"]] = False
        hist["cpu"].append(stats["cpu"])
        hist["ram"].append(stats["ram"])
        hist["net_up"].append(stats["net_up"])
        hist["net_down"].append(stats["net_down"])

    if monitor_canvas is not None:
        draw_combined_graph(
            monitor_canvas, hist["cpu"], hist["ram"], hist["net_up"], hist["net_down"],
            unreachable=machine_unreachable[machine["id"]],
        )

    monitor_job = root.after(POLL_INTERVAL_MS, poll_active_machine)


def show_machine_page(machine_id):
    global active_machine_id, monitor_canvas

    active_machine_id = machine_id
    machine = find_machine(machine_id)

    clear_content()
    rebuild_tabs()
    page_title_label.config(text=f"SYSTEM MONITOR \u2014 {machine['label'] if machine else machine_id}")

    if machine and machine["type"] == "local" and psutil is None:
        Label(
            content_frame,
            text=(
                "psutil is not installed.\n\n"
                "Install it with:\n"
                "pip install psutil --break-system-packages"
            ),
            font=small_font, bg="#1e1e1e", fg="#e74c3c", justify="center",
        ).pack(expand=True)
        monitor_canvas = None
        return

    canvas = Canvas(content_frame, bg="#232b2f", highlightthickness=0)
    canvas.pack(fill="both", expand=True, padx=10, pady=10)
    monitor_canvas = canvas

    def initial_draw():
        if monitor_canvas is not None:
            hist = histories[machine_id]
            draw_combined_graph(
                monitor_canvas, hist["cpu"], hist["ram"], hist["net_up"], hist["net_down"],
                unreachable=machine_unreachable[machine_id],
            )

    root.update_idletasks()
    root.after(50, initial_draw)
    poll_active_machine()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    if MACHINES:
        show_machine_page(MACHINES[0]["id"])
    root.mainloop()
