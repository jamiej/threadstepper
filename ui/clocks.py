import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox, PhotoImage
import subprocess
import threading
import time
import os
import queue
from datetime import datetime
import platform
import re
import ttkbootstrap as tb
from ttkbootstrap.constants import *
from PIL import Image, ImageTk

def reset_clock_speed(self):
    """Reset both the legacy global clock and the new per-core/per-CCX clocks data."""
    try:
        with open("./logs/clock.log", 'w') as f:
            f.write("0")
        with open("./logs/clocks.log", 'w') as f:
            f.write("GLOBAL=0\n")
        update_clocks(self)
        self.log_message("Clocks reset (global + per-core/CCX)", "info")
        self.status_bar.config(text="Clocks reset")
    except Exception as e:
        self.log_message(f"Error resetting clocks: {str(e)}", "error")
        messagebox.showerror("Error", f"Failed to reset clocks: {str(e)}")

def monitor_clock_speed(self):
    last_clock_mtime = 0
    last_clocks_mtime = 0
    while True:
        try:
            if os.path.exists("./logs/clock.log"):
                m = os.path.getmtime("./logs/clock.log")
                if m > last_clock_mtime:
                    last_clock_mtime = m
                    self.root.after(0, lambda: update_clock_speed(self))
            if os.path.exists("./logs/clocks.log"):
                m = os.path.getmtime("./logs/clocks.log")
                if m > last_clocks_mtime:
                    last_clocks_mtime = m
                    self.root.after(0, lambda: update_clocks(self))
        except:
            pass
        time.sleep(0.5)

def update_clock_speed(self):
    try:
        if os.path.exists("./logs/clock.log"):
            with open("./logs/clock.log", "r") as f:
                clock_speed = f.read().strip()

            if clock_speed:
                self.clock_label.config(
                    text=clock_speed,
                    fg="#17a2b8",
                    bg="#e8f4f8"
                )
            else:
                self.clock_label.config(
                    text="No data",
                    fg="#6c757d",
                    bg="#f8f9fa"
                )
        else:
            self.clock_label.config(
                text="No clock.log file",
                fg="#6c757d",
                bg="#f8f9fa"
            )

    except:
        self.clock_label.config(
            text="Error reading",
            fg="#721c24",
            bg="#f8d7da"
        )

    # Also refresh the CCX summary if the legacy updater is called directly (robustness)
    try:
        update_ccx_summary(self)
    except Exception:
        pass


def load_clocks_data(self):
    """Parse logs/clocks.log (key=value) into structured data for per-core and per-CCX display.

    Safe against missing file, partial content, or parse errors.
    """
    data = {
        "global": 0.0,
        "cores": {},       # core_id -> ghz (float)
        "ccxs": {},        # ccx_id -> ghz (float)
        "cpu_to_ccx": {},  # core_id -> ccx_id
        "ccx_cores": {},   # ccx_id -> "0,1,2,3" comma string (the cores in that CCX)
    }
    path = "./logs/clocks.log"
    if not os.path.exists(path):
        return data
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = [part.strip() for part in line.split("=", 1)]
                try:
                    if k == "GLOBAL":
                        data["global"] = float(v)
                    elif k.startswith("CPU") and k[3:].isdigit():
                        core = int(k[3:])
                        data["cores"][core] = float(v)
                    elif k.startswith("CCX") and k[3:].isdigit():
                        # plain CCXN= (not the FOR or CORES lines)
                        ccx = int(k[3:])
                        data["ccxs"][ccx] = float(v)
                    elif k.startswith("CCX_FOR_CPU"):
                        core = int(k[len("CCX_FOR_CPU"):])
                        data["cpu_to_ccx"][core] = int(v)
                    elif k.startswith("CORES_IN_CCX"):
                        ccx = int(k[len("CORES_IN_CCX"):])
                        data["ccx_cores"][ccx] = v
                except (ValueError, IndexError):
                    continue  # ignore malformed entries
        return data
    except Exception:
        return data


def update_ccx_summary(self):
    """Update the compact Per-CCX summary label in the main clock panel (if present)."""
    try:
        if not hasattr(self, "ccx_summary_label") or not self.ccx_summary_label:
            return
        data = load_clocks_data(self)
        ccxs = data.get("ccxs", {})
        if not ccxs:
            self.ccx_summary_label.config(text="Per CCX: (no data)")
            return
        n = len(ccxs)
        # Compact format to accommodate many CCXs (e.g. 8 on 64c/128t machines).
        # "Per CCX (8): 0:4.850 1:4.847 ..."  -- no repeated "CCX" prefix saves space.
        # Wraplength + resizable window lets user see full list; Details dialog has the complete table.
        items = [f"{k}:{v}" for k, v in sorted(ccxs.items())]
        text = f"Per CCX ({n}): " + " ".join(items)
        self.ccx_summary_label.config(text=text)
    except Exception:
        try:
            self.ccx_summary_label.config(text="Per CCX: (error reading)")
        except Exception:
            pass


def update_clocks(self):
    """Update global clock display + the Per-CCX summary (when UI elements exist)."""
    update_clock_speed(self)
    update_ccx_summary(self)


def show_per_core_clocks_dialog(self):
    """Show a dialog with per-core and per-CCX highest observed clocks.

    Uses load_clocks_data and presents a Treeview table sorted by CCX then Core.
    Includes summary and a Refresh button.
    """
    try:
        data = load_clocks_data(self)

        dlg = tk.Toplevel()
        dlg.title("Per-Core & Per-CCX Highest Clocks")
        dlg.geometry("620x520")
        dlg.resizable(True, True)

        # Summary header
        g = data.get("global", 0.0)
        n_cores = len(data.get("cores", {}))
        n_ccxs = len(data.get("ccxs", {}))
        summary = ttk.Label(
            dlg,
            text=f"Global highest: {g} GHz   |   Cores: {n_cores}   |   CCXs: {n_ccxs}",
            font=("Segoe UI", 10, "bold"),
            padding=8,
        )
        summary.pack(fill="x")

        # Treeview table
        cols = ("Core", "CCX", "Highest (GHz)")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", height=24)  # taller default for high thread count machines (64c/128t); full scrollbar support
        tree.heading("Core", text="Core")
        tree.heading("CCX", text="CCX")
        tree.heading("Highest (GHz)", text="Highest (GHz)")
        tree.column("Core", width=80, anchor="center")
        tree.column("CCX", width=80, anchor="center")
        tree.column("Highest (GHz)", width=120, anchor="e")

        # Build rows: sort by CCX then core
        rows = []
        cores = data.get("cores", {})
        cpu_to_ccx = data.get("cpu_to_ccx", {})
        ccx_max = data.get("ccxs", {})
        for core in sorted(cores.keys()):
            ccx = cpu_to_ccx.get(core, -1)
            ghz = cores.get(core, 0.0)
            rows.append((core, ccx, ghz))

        rows.sort(key=lambda r: (r[1], r[0]))

        for core, ccx, ghz in rows:
            is_ccx_max = False
            try:
                is_ccx_max = abs(ghz - ccx_max.get(ccx, -1)) < 0.0005
            except Exception:
                pass
            tags = ("ccx_max",) if is_ccx_max else ()
            tree.insert("", "end", values=(core, ccx, f"{ghz:.3f}"), tags=tags)

        # Style for rows that set the CCX record
        tree.tag_configure("ccx_max", background="#d4edda", foreground="#155724")

        # Scroll if many cores
        vsb = ttk.Scrollbar(dlg, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True, padx=(8,0), pady=4)
        vsb.pack(side="right", fill="y", pady=4)

        # Buttons
        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill="x", padx=8, pady=(4,8))

        def refresh_dialog():
            # Re-read and repopulate (simple: destroy and recreate for freshness)
            try:
                dlg.destroy()
                # Re-open with fresh data (avoids complex live tree update)
                show_per_core_clocks_dialog(self)
            except Exception:
                pass

        ttk.Button(btn_frame, text="🔁 Refresh", bootstyle="success-outline", command=refresh_dialog).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Close", command=dlg.destroy).pack(side="right", padx=4)

        # Make dialog modal-ish
        dlg.transient(self.root if hasattr(self, "root") else None)
        dlg.grab_set()
        dlg.focus_set()

    except Exception as e:
        messagebox.showerror("Error", f"Failed to show per-core clocks dialog: {e}")