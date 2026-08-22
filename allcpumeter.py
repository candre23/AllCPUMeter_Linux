#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import struct
import zlib
import sys
import time
from collections import deque
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, GLib, Gdk, Pango

APP_VERSION = "0.2.0"
APP_ID = "io.github.allcpumeterlinux"
APP_HOME = Path.home() / ".local" / "share" / "allcpumeter-linux"
SETTINGS_DIR = Path.home() / ".config" / "allcpumeter-linux"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
AUTOSTART_DIR = Path.home() / ".config" / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "allcpumeter-linux.desktop"
SELF = APP_HOME / "allcpumeter.py"

PALETTE = [
    "#c77dff", "#ffd93d", "#00d9ff", "#ff5e5e",
    "#00ff66", "#5da9ff", "#b8f34a", "#ff9f43",
]

DEFAULTS = {
    "width": 180,
    "gap_x": 12,
    "gap_y": 40,
    "position": "top_right",
    "update_interval": 1.0,
    "per_core": True,
    "cpu_detail": "logical",
    "temp_mode": "package",
    "show_freq": True,
    "show_swap": True,
    "show_network": True,
    "show_disk": True,
    "trendlines": True,
    "multicolor": True,
    "autostart": True,
    "cpu_name_mode": "crop",
    "cpu_custom_name": "",
    "gpu_name_mode": "crop",
    "gpu_custom_name": "",
    "gpu_overall": True,
    "gpu_render": True,
    "gpu_video": True,
    "gpu_video_enhance": False,
    "gpu_vram": True,
    "gpu_enabled": {},
    "gpu_names": {},
}


def run(cmd, timeout=5):
    try:
        p = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, check=False
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 127, "", str(e)


def load_settings():
    out = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
        if isinstance(saved, dict):
            out.update(saved)
    except Exception:
        pass

    return out


def save_settings(settings):
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


def normalize_bdf(value):
    value = (value or "").strip().lower()
    parts = value.split(":")
    if len(parts) == 3:
        return f"{parts[0][-4:].zfill(4)}:{parts[1].zfill(2)}:{parts[2]}"
    return value


def cpu_info():
    model = "Unknown CPU"
    logical = os.cpu_count() or 1
    try:
        for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
            if line.lower().startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    except Exception:
        pass
    return {"model": model, "logical_cpus": logical}


def logical_core_ids():
    ids = []
    current = {}
    try:
        for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines() + [""]:
            if not line.strip():
                if current:
                    try:
                        ids.append(int(current.get("core id", len(ids))))
                    except Exception:
                        ids.append(len(ids))
                    current = {}
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                current[k.strip().lower()] = v.strip()
    except Exception:
        pass
    n = os.cpu_count() or 1
    if len(ids) < n:
        ids.extend(range(len(ids), n))
    return ids[:n]



def physical_core_groups():
    """Group logical CPU indices by physical package/core topology."""
    groups = {}
    cpu_base = Path("/sys/devices/system/cpu")
    for cpu_dir in sorted(cpu_base.glob("cpu[0-9]*"), key=lambda p: int(p.name[3:])):
        try:
            logical = int(cpu_dir.name[3:])
            topo = cpu_dir / "topology"
            package = int((topo / "physical_package_id").read_text().strip())
            core = int((topo / "core_id").read_text().strip())
        except Exception:
            continue
        groups.setdefault((package, core), []).append(logical)

    if not groups:
        return [
            {"package": 0, "core_id": i, "logicals": [i]}
            for i in range(os.cpu_count() or 1)
        ]

    result = []
    for (package, core), logicals in sorted(groups.items()):
        result.append({
            "package": package,
            "core_id": core,
            "logicals": sorted(logicals),
        })
    return result

def default_interface():
    rc, out, _ = run(["ip", "route", "show", "default"])
    if rc == 0:
        m = re.search(r"\bdev\s+(\S+)", out)
        if m:
            return m.group(1)
    base = Path("/sys/class/net")
    if base.exists():
        for p in sorted(base.iterdir()):
            n = p.name
            if n != "lo" and not n.startswith(("docker", "br-", "veth", "virbr", "tun", "tap")):
                return n
    return "lo"


def root_device():
    rc, out, _ = run(["findmnt", "-n", "-o", "SOURCE", "/"])
    s = out.strip() if rc == 0 else ""
    if s.startswith("/dev/"):
        s = s[5:]
    return s


def nvidia_inventory():
    if not shutil.which("nvidia-smi"):
        return {}
    rc, out, _ = run([
        "nvidia-smi",
        "--query-gpu=index,uuid,pci.bus_id,name",
        "--format=csv,noheader,nounits",
    ], 3)
    if rc != 0:
        return {}
    result = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",", 3)]
        if len(parts) == 4:
            index, uuid, bdf, name = parts
            result[normalize_bdf(bdf)] = {
                "index": index, "uuid": uuid, "name": name
            }
    return result


def classify_gpu_vendor(description):
    low = (description or "").lower().strip()
    if low.startswith("nvidia corporation") or low.startswith("nvidia "):
        return "nvidia"
    if low.startswith("intel corporation") or low.startswith("intel "):
        return "intel"
    if low.startswith("advanced micro devices") or low.startswith("amd ") or low.startswith("ati technologies"):
        return "amd"
    if low.startswith("aspeed technology") or low.startswith("aspeed "):
        return "aspeed"
    return "unknown"


def gpu_info():
    rc, out, _ = run(["lspci", "-D"])
    if rc != 0:
        return []

    nv = nvidia_inventory()
    result = []
    rx = re.compile(
        r"^(\S+)\s+(VGA compatible controller|3D controller|Display controller):\s*(.*)$",
        re.I,
    )
    for line in out.splitlines():
        m = rx.match(line.strip())
        if not m:
            continue
        bdf = normalize_bdf(m.group(1))
        desc = m.group(3).strip()
        vendor = classify_gpu_vendor(desc)
        gpu = {
            "key": bdf,
            "bdf": bdf,
            "vendor": vendor,
            "description": desc,
            "sys_path": str(Path("/sys/bus/pci/devices") / bdf),
        }
        dev = Path(gpu["sys_path"])
        if vendor == "nvidia":
            info = nv.get(bdf, {})
            gpu["nvidia_uuid"] = info.get("uuid", "")
            gpu["backend_available"] = bool(gpu["nvidia_uuid"])
            gpu["backend_label"] = "nvidia-smi" if gpu["backend_available"] else "NVIDIA counters unavailable"
        elif vendor == "amd":
            busy = dev / "gpu_busy_percent"
            gpu["amd_busy_path"] = str(busy) if busy.exists() else ""
            gpu["backend_available"] = busy.exists()
            gpu["backend_label"] = "amdgpu sysfs" if busy.exists() else "AMD counters unavailable"
        elif vendor == "intel":
            gpu["intel_device"] = "sys:" + gpu["sys_path"]
            gpu["backend_available"] = shutil.which("intel_gpu_top") is not None
            gpu["backend_label"] = "intel_gpu_top" if gpu["backend_available"] else "intel-gpu-tools not installed"
        else:
            gpu["backend_available"] = False
            gpu["backend_label"] = "No supported monitoring backend"
        result.append(gpu)
    return result


def scan_hardware():
    return {
        "cpu": cpu_info(),
        "network": {"primary": default_interface()},
        "disk": {"root_device": root_device()},
        "gpus": gpu_info(),
        "sensors_installed": shutil.which("sensors") is not None,
    }


def read_sensor_items():
    if not shutil.which("sensors"):
        return []
    rc, out, _ = run(["sensors", "-j"], 4)
    if rc != 0:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    found = []
    for chip in data.values():
        if not isinstance(chip, dict):
            continue
        for label, values in chip.items():
            if not isinstance(values, dict):
                continue
            for k, v in values.items():
                if str(k).endswith("_input") and isinstance(v, (int, float)):
                    found.append((label, float(v)))
                    break
    return found


def package_temp(items):
    for needle in ("package id 0", "package", "tctl", "tdie", "cpu"):
        for label, value in items:
            if needle in label.lower():
                return value
    return items[0][1] if items else None


def per_core_temps(items):
    out = {}
    for label, value in items:
        m = re.search(r"core\s*(\d+)", label.lower())
        if m:
            out[int(m.group(1))] = value
    return out


def cpu_frequency_ghz():
    vals = []
    base = Path("/sys/devices/system/cpu")
    for p in base.glob("cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            vals.append(float(p.read_text().strip()) / 1_000_000.0)
        except Exception:
            pass
    if vals:
        return sum(vals) / len(vals)
    return 0.0


def read_proc_stat():
    overall = None
    cores = []
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if not line.startswith("cpu"):
                continue
            parts = line.split()
            name = parts[0]
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals)
            row = (idle, total)
            if name == "cpu":
                overall = row
            elif name[3:].isdigit():
                cores.append(row)
    except Exception:
        pass
    return overall, cores


def cpu_usage(prev, cur):
    if not prev or not cur:
        return 0.0
    idle_delta = cur[0] - prev[0]
    total_delta = cur[1] - prev[1]
    if total_delta <= 0:
        return 0.0
    return max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))


def mem_stats():
    vals = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            vals[k] = int(v.strip().split()[0]) * 1024
    except Exception:
        pass
    total = vals.get("MemTotal", 0)
    available = vals.get("MemAvailable", 0)
    used = max(0, total - available)
    swap_total = vals.get("SwapTotal", 0)
    swap_free = vals.get("SwapFree", 0)
    return {
        "total": total,
        "used": used,
        "percent": 0 if total <= 0 else used / total * 100.0,
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - swap_free),
        "swap_percent": 0 if swap_total <= 0 else (swap_total - swap_free) / swap_total * 100.0,
    }


def net_bytes(iface):
    try:
        for line in Path("/proc/net/dev").read_text().splitlines():
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            if name.strip() != iface:
                continue
            parts = rest.split()
            return int(parts[0]), int(parts[8])
    except Exception:
        pass
    return 0, 0


def disk_stats(device):
    base = device.rsplit("/", 1)[-1]
    try:
        for line in Path("/proc/diskstats").read_text().splitlines():
            parts = line.split()
            if len(parts) < 14 or parts[2] != base:
                continue
            sectors_read = int(parts[5])
            sectors_written = int(parts[9])
            return sectors_read * 512, sectors_written * 512
    except Exception:
        pass
    return 0, 0


def root_fs_stats():
    st = os.statvfs("/")
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    return used, total, (0 if total <= 0 else used / total * 100.0)


def format_bytes(value):
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    v = float(value)
    for unit in units:
        if abs(v) < 1024.0 or unit == units[-1]:
            return f"{v:.1f} {unit}" if unit != "B" else f"{v:.0f} B"
        v /= 1024.0



def gpu_hwmon_temp(gpu):
    """Best-effort PCI-device temperature in Celsius."""
    dev = Path(gpu.get("sys_path", ""))
    candidates = []
    for hwmon in dev.glob("hwmon/hwmon*"):
        for temp_file in sorted(hwmon.glob("temp*_input")):
            try:
                value = float(temp_file.read_text().strip())
                if value > 1000:
                    value /= 1000.0
                if -20 <= value <= 150:
                    candidates.append(value)
            except Exception:
                pass
    return candidates[0] if candidates else None


def intel_clock_mhz(gpu):
    """Read a current Intel GT clock from sysfs when exposed."""
    dev = Path(gpu.get("sys_path", ""))
    patterns = (
        "drm/card*/gt_cur_freq_mhz",
        "drm/card*/gt/gt*/freq0/cur_freq",
        "drm/card*/device/gt_cur_freq_mhz",
    )
    for pattern in patterns:
        for p in dev.glob(pattern):
            try:
                return float(p.read_text().strip())
            except Exception:
                pass
    return None


def amd_clock_mhz(gpu):
    """Read the active AMD core clock from pp_dpm_sclk when exposed."""
    p = Path(gpu.get("sys_path", "")) / "pp_dpm_sclk"
    try:
        for line in p.read_text().splitlines():
            if "*" not in line:
                continue
            m = re.search(r"([0-9.]+)\\s*(MHz|Mhz|mhz)", line)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return None

def intel_gpu_stats(device=""):
    cmd = ["intel_gpu_top"]
    if device:
        cmd += ["-d", device]
    cmd += ["-J", "-s", "250", "-n", "2", "-o", "-"]
    rc, out, err = run(cmd, 2.5)
    if rc != 0 or not out:
        return {}
    cleaned = out.strip()
    try:
        data = json.loads(cleaned)
    except Exception:
        cleaned = re.sub(r",\s*]", "]", cleaned)
        if not cleaned.endswith("]"):
            cleaned += "]"
        try:
            data = json.loads(cleaned)
        except Exception:
            return {}
    samples = data if isinstance(data, list) else [data]
    sample = samples[-1] if samples else {}
    engines = sample.get("engines", {}) if isinstance(sample, dict) else {}
    result = {"overall": 0.0, "render": 0.0, "video": 0.0, "videoenhance": 0.0}
    for name, values in engines.items():
        if not isinstance(values, dict):
            continue
        busy = values.get("busy", 0)
        try:
            busy = float(busy)
        except Exception:
            busy = 0.0
        low = name.lower()
        result["overall"] = max(result["overall"], busy)
        if "render" in low or "3d" in low:
            result["render"] = max(result["render"], busy)
        if "videoenhance" in low or "video enhance" in low:
            result["videoenhance"] = max(result["videoenhance"], busy)
        elif "video" in low:
            result["video"] = max(result["video"], busy)
    # Frequency is not consistently present in intel_gpu_top JSON across
    # generations, so sysfs is used by gpu_stats() as the first choice.
    return result


def nvidia_stats(uuid):
    if not uuid:
        return {}
    rc, out, _ = run([
        "nvidia-smi", "-i", uuid,
        "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,clocks.current.graphics",
        "--format=csv,noheader,nounits",
    ], 2)
    if rc != 0 or not out:
        return {}
    try:
        util, used, total, temp, clock = [
            float(x.strip()) for x in out.splitlines()[0].split(",")[:5]
        ]
        return {
            "overall": util,
            "vram_used_mib": used,
            "vram_total_mib": total,
            "vram_percent": 0 if total <= 0 else used / total * 100.0,
            "temp": temp,
            "clock_mhz": clock,
        }
    except Exception:
        return {}


def amd_stats(gpu):
    try:
        busy = float(Path(gpu["amd_busy_path"]).read_text().strip())
    except Exception:
        return {}
    result = {"overall": busy}
    dev = Path(gpu["sys_path"])
    try:
        used = float((dev / "mem_info_vram_used").read_text().strip()) / 1024 / 1024
        total = float((dev / "mem_info_vram_total").read_text().strip()) / 1024 / 1024
        result.update({
            "vram_used_mib": used,
            "vram_total_mib": total,
            "vram_percent": 0 if total <= 0 else used / total * 100.0,
        })
    except Exception:
        pass

    temp = gpu_hwmon_temp(gpu)
    if temp is not None:
        result["temp"] = temp
    clock = amd_clock_mhz(gpu)
    if clock is not None:
        result["clock_mhz"] = clock

    return result


def display_name(text, mode, custom, width):
    text = (custom if mode == "custom" and custom else text) or ""
    text = text.replace("$", "").strip()
    if mode == "crop":
        chars = max(14, int((width - 12) / 7))
        return text[:chars]
    return text


CSS = b"""
window.meter {
    background: #101010;
    color: #dddddd;
}
.meter-root {
    padding: 8px;
}
.section-title {
    color: #ffffff;
    font-weight: 700;
    font-size: 13px;
    border-bottom: 1px solid #777777;
    padding-bottom: 2px;
}
.metric-label {
    font-family: monospace;
    font-size: 11px;
}
.muted {
    color: #aaaaaa;
    font-family: monospace;
    font-size: 11px;
}
progressbar {
    min-width: 0;
}
progressbar > trough {
    background: #222222;
    min-width: 0;
    min-height: 6px;
}
progressbar > trough > progress {
    min-width: 0;
    min-height: 6px;
}
progressbar.bar-purple > trough > progress { background-image: none; background-color: #c77dff; }
progressbar.bar-yellow > trough > progress { background-image: none; background-color: #ffd93d; }
progressbar.bar-cyan > trough > progress { background-image: none; background-color: #00d9ff; }
progressbar.bar-red > trough > progress { background-image: none; background-color: #ff5e5e; }
progressbar.bar-green > trough > progress { background-image: none; background-color: #00ff66; }
progressbar.bar-blue > trough > progress { background-image: none; background-color: #5da9ff; }
progressbar.bar-lime > trough > progress { background-image: none; background-color: #b8f34a; }
progressbar.bar-orange > trough > progress { background-image: none; background-color: #ff9f43; }
"""



def _png_chunk(kind, data):
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    )


class TrendGraph(Gtk.Picture):
    """History graph rendered to a tiny PNG for maximum GTK/RDP compatibility."""
    _serial = 0

    def __init__(self, traces=1, colors=None, height=30, width=160):
        super().__init__()
        TrendGraph._serial += 1
        self.graph_id = TrendGraph._serial
        self.histories = [deque(maxlen=120) for _ in range(traces)]
        self.colors = colors or ["#57c7ff"] * traces
        self.graph_width = max(80, int(width))
        self.graph_height = max(18, int(height))
        self.frame_no = 0

        cache = Path.home() / ".cache" / "allcpumeter-linux" / "graphs"
        cache.mkdir(parents=True, exist_ok=True)
        self.cache_dir = cache

        self.set_size_request(self.graph_width, self.graph_height)
        self.set_hexpand(False)
        self.set_vexpand(False)
        self.set_can_shrink(False)
        self._render()

    @staticmethod
    def rgb(hexcolor):
        c = hexcolor.lstrip("#")
        return tuple(int(c[i:i+2], 16) for i in (0, 2, 4))

    def push(self, *values):
        for hist, value in zip(self.histories, values):
            try:
                value = float(value)
            except Exception:
                value = 0.0
            hist.append(max(0.0, min(100.0, value)))
        self._render()

    def _render(self):
        w, h = self.graph_width, self.graph_height
        pixels = [bytearray(w * 4) for _ in range(h)]

        def put(x, y, rgba):
            if 0 <= x < w and 0 <= y < h:
                i = x * 4
                pixels[y][i:i+4] = bytes(rgba)

        def hline(y, x1, x2, rgba):
            for x in range(max(0, x1), min(w, x2 + 1)):
                put(x, y, rgba)

        def vline(x, y1, y2, rgba):
            for y in range(max(0, y1), min(h, y2 + 1)):
                put(x, y, rgba)

        def line(x0, y0, x1, y1, rgba, thickness=1):
            dx = abs(x1 - x0)
            sx = 1 if x0 < x1 else -1
            dy = -abs(y1 - y0)
            sy = 1 if y0 < y1 else -1
            err = dx + dy
            while True:
                radius = max(0, thickness // 2)
                for yy in range(y0 - radius, y0 + radius + 1):
                    for xx in range(x0 - radius, x0 + radius + 1):
                        put(xx, yy, rgba)
                if x0 == x1 and y0 == y1:
                    break
                e2 = 2 * err
                if e2 >= dy:
                    err += dy
                    x0 += sx
                if e2 <= dx:
                    err += dx
                    y0 += sy

        # Opaque dark face.
        for y in range(h):
            for x in range(w):
                put(x, y, (8, 8, 8, 255))

        # Muted gray background grid.
        grid = (78, 78, 78, 255)
        for i in range(1, 6):
            x = round((w - 1) * i / 6)
            vline(x, 1, h - 2, grid)
        for i in range(1, 4):
            y = round((h - 1) * i / 4)
            hline(y, 1, w - 2, grid)

        # Border.
        border = (135, 135, 135, 255)
        hline(0, 0, w - 1, border)
        hline(h - 1, 0, w - 1, border)
        vline(0, 0, h - 1, border)
        vline(w - 1, 0, h - 1, border)

        def py(value):
            return int(round((h - 3) - (value / 100.0) * max(1, h - 5)))

        for hist, color in zip(self.histories, self.colors):
            if not hist:
                continue
            r, g, b = self.rgb(color)
            rgba = (r, g, b, 255)
            values = list(hist)
            n = len(values)

            # History grows from right to left and fills the available graph
            # width when the 120-sample buffer is full.
            step = (w - 5) / 119.0
            x_end = w - 3
            x_start = max(2.0, x_end - step * (n - 1))

            if n == 1:
                y = py(values[0])
                line(max(2, x_end - 3), y, x_end, y, rgba, 1)
            else:
                prev_x = int(round(x_start))
                prev_y = py(values[0])
                for i, value in enumerate(values[1:], 1):
                    x = min(x_end, int(round(x_start + i * step)))
                    y = py(value)
                    line(prev_x, prev_y, x, y, rgba, 1)
                    prev_x, prev_y = x, y

            # Current value is just the final pixel of the thin trace.
            cy = py(values[-1])
            put(x_end, cy, rgba)

        raw = b"".join(b"\x00" + bytes(row) for row in pixels)
        png = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(raw, 6))
            + _png_chunk(b"IEND", b"")
        )

        self.frame_no += 1
        path = self.cache_dir / f"graph-{os.getpid()}-{self.graph_id}-{self.frame_no % 2}.png"
        path.write_bytes(png)

        try:
            texture = Gdk.Texture.new_from_filename(str(path))
            self.set_paintable(texture)
        except Exception:
            self.set_filename(str(path))


BAR_CLASSES = {
    "#c77dff": "bar-purple",
    "#ffd93d": "bar-yellow",
    "#00d9ff": "bar-cyan",
    "#ff5e5e": "bar-red",
    "#00ff66": "bar-green",
    "#5da9ff": "bar-blue",
    "#b8f34a": "bar-lime",
    "#ff9f43": "bar-orange",
}


def progress(color):
    bar = Gtk.ProgressBar()
    bar.set_show_text(False)
    bar.set_size_request(8, -1)
    bar.set_hexpand(True)
    bar.add_css_class(BAR_CLASSES.get(color.lower(), "bar-blue"))
    return bar



class MeterWindow(Gtk.ApplicationWindow):
    def __init__(self, app, settings, hardware):
        super().__init__(application=app, title="All CPU Meter")
        self.settings = settings
        self.hardware = hardware
        self.panel_width = max(150, int(settings["width"]))
        panel_width = max(150, int(settings["width"]))
        self.set_default_size(panel_width, -1)
        self.set_size_request(panel_width, -1)
        self.set_resizable(False)
        self.set_decorated(False)
        self.set_focusable(False)
        self.set_modal(False)
        self.add_css_class("meter")
        self.connect("realize", self._apply_widget_window_hints)

        # Without a GNOME Shell placement helper, Wayland applications cannot
        # choose absolute coordinates. Keep the undecorated meter draggable as
        # a fallback instead of making an accidentally misplaced meter immovable.
        drag = Gtk.GestureClick()
        drag.set_button(1)
        drag.connect("pressed", self._begin_window_move)
        self.add_controller(drag)

        self.prev_cpu = read_proc_stat()
        self.prev_net = net_bytes(hardware["network"]["primary"])
        self.prev_disk = disk_stats(hardware["disk"]["root_device"])
        self.prev_time = time.monotonic()

        self.cpu_rows = []
        self.gpu_rows = []
        self.core_ids = logical_core_ids()
        self.physical_groups = physical_core_groups()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        outer.add_css_class("meter-root")
        self.set_child(outer)
        self.outer = outer
        self.build_ui()

        interval_ms = max(250, int(float(settings["update_interval"]) * 1000))
        self.refresh()
        GLib.timeout_add(interval_ms, self.refresh)

    def _apply_widget_window_hints(self, *_args):
        try:
            surface = self.get_surface()
            if surface is not None:
                surface.set_urgency_hint(False)
        except Exception:
            pass

    def _begin_window_move(self, gesture, n_press, x, y):
        try:
            event = gesture.get_current_event()
            surface = self.get_surface()
            if event is None or surface is None:
                return
            device = event.get_device()
            timestamp = event.get_time()
            surface.begin_move(device, 1, x, y, timestamp)
        except Exception:
            pass


    def title(self, text):
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("section-title")
        self.outer.append(label)

    def row_label(self, text="", css="metric-label"):
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class(css)
        label.set_wrap(False)
        label.set_hexpand(True)
        label.set_width_chars(1)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        return label

    def metric_pair(self, left="", right="", css="metric-label"):
        """Compact left/right metric row that uses the full panel width."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)

        left_label = Gtk.Label(label=left, xalign=0)
        left_label.add_css_class(css)
        left_label.set_hexpand(False)
        left_label.set_ellipsize(Pango.EllipsizeMode.END)
        left_label.set_width_chars(1)

        right_label = Gtk.Label(label=right, xalign=1)
        right_label.add_css_class(css)
        right_label.set_hexpand(True)
        right_label.set_halign(Gtk.Align.FILL)
        right_label.set_ellipsize(Pango.EllipsizeMode.NONE)

        row.append(left_label)
        row.append(right_label)
        return row, left_label, right_label

    def build_ui(self):
        s = self.settings
        cpu = self.hardware["cpu"]
        self.title("CPU METER")
        name = self.row_label(display_name(
            cpu["model"], s["cpu_name_mode"], s["cpu_custom_name"], s["width"]
        ), "muted")
        if s["cpu_name_mode"] == "wrap":
            name.set_ellipsize(Pango.EllipsizeMode.NONE)
            name.set_wrap(True)
            name.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.outer.append(name)

        self.cpu_total_label = self.row_label()
        self.cpu_total_bar = progress("#00ff66")
        self.outer.append(self.cpu_total_label)
        self.outer.append(self.cpu_total_bar)

        self.freq_label = None
        if s["show_freq"]:
            row, _, self.freq_label = self.metric_pair("Frequency", "", "muted")
            self.outer.append(row)

        self.package_temp_label = None
        if s["temp_mode"] == "package":
            row, _, self.package_temp_label = self.metric_pair("Temperature", "", "muted")
            self.outer.append(row)

        detail = s.get("cpu_detail", "logical")
        if not s.get("per_core", True):
            detail = "overall"

        row_specs = []
        if detail == "logical":
            for i in range(cpu["logical_cpus"]):
                row_specs.append({
                    "label": f"CPU {i+1:02d}",
                    "logicals": [i],
                    "core_id": self.core_ids[i] if i < len(self.core_ids) else i,
                })
        elif detail == "physical":
            multi_package = len({g["package"] for g in self.physical_groups}) > 1
            for i, group in enumerate(self.physical_groups, 1):
                label_text = (
                    f"P{group['package']} C{i:02d}"
                    if multi_package else f"Core {i:02d}"
                )
                row_specs.append({
                    "label": label_text,
                    "logicals": group["logicals"],
                    "core_id": group["core_id"],
                })

        for i, spec in enumerate(row_specs):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            label = self.row_label(spec["label"])
            label.set_size_request(38, -1)
            label.set_hexpand(False)
            color = PALETTE[i % len(PALETTE)] if s["multicolor"] else "#5da9ff"
            bar = progress(color)
            bar.set_hexpand(True)
            right = self.row_label("")
            # Percentage-only mode needs very little space. Per-core temperature
            # mode gets a wider value field for strings such as "42°C 100%".
            right_width = 29 if s.get("temp_mode") != "cores" else 55
            right.set_size_request(right_width, -1)
            right.set_hexpand(False)
            right.set_xalign(1)
            row.append(label); row.append(bar); row.append(right)
            self.outer.append(row)
            self.cpu_rows.append({
                "bar": bar,
                "right": right,
                "logicals": spec["logicals"],
                "core_id": spec["core_id"],
            })

        self.cpu_graph = None
        if s["trendlines"]:
            self.cpu_graph = TrendGraph(1, ["#00ff66"], 34, max(80, self.panel_width - 16))
            self.outer.append(self.cpu_graph)

        self.title("MEMORY")
        ram_row, _, self.ram_label = self.metric_pair("RAM", "")
        self.ram_bar = progress("#5da9ff")
        self.outer.append(ram_row); self.outer.append(self.ram_bar)

        self.swap_label = None
        self.swap_bar = None
        if s["show_swap"]:
            swap_row, _, self.swap_label = self.metric_pair("Swap", "")
            self.swap_bar = progress("#ffd93d")
            self.outer.append(swap_row); self.outer.append(self.swap_bar)

        self.mem_graph = None
        if s["trendlines"]:
            self.mem_graph = TrendGraph(1, ["#5da9ff"], 28, max(80, self.panel_width - 16))
            self.outer.append(self.mem_graph)

        enabled = s.get("gpu_enabled", {})
        selected = [
            g for g in self.hardware["gpus"]
            if enabled.get(g["key"], g.get("backend_available", False))
        ]
        for idx, gpu in enumerate(selected, 1):
            self.title("GPU METER" if len(selected) == 1 else f"GPU {idx} METER")
            gpu_name_settings = s.get("gpu_names", {}).get(gpu["key"], {})
            gpu_name_mode = gpu_name_settings.get(
                "mode", s.get("gpu_name_mode", "crop")
            )
            gpu_custom_name = gpu_name_settings.get(
                "custom", s.get("gpu_custom_name", "")
            )
            name = self.row_label(display_name(
                gpu["description"], gpu_name_mode, gpu_custom_name, s["width"]
            ), "muted")
            if gpu_name_mode == "wrap":
                name.set_ellipsize(Pango.EllipsizeMode.NONE)
                name.set_wrap(True)
                name.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            self.outer.append(name)

            metric_rows = []
            if gpu["vendor"] == "intel":
                metrics = []
                if s["gpu_overall"]: metrics.append(("GPU","overall","#c77dff"))
                if s["gpu_render"]: metrics.append(("Render/3D","render","#00ff66"))
                if s["gpu_video"]: metrics.append(("Video/QSV","video","#ffd93d"))
                if s["gpu_video_enhance"]: metrics.append(("Video Enhance","videoenhance","#00d9ff"))
            else:
                metrics = [("GPU","overall","#c77dff")]

            for label_text, metric, color in metrics:
                metric_row, _, label = self.metric_pair(label_text, "")
                bar = progress(color)
                self.outer.append(metric_row); self.outer.append(bar)
                metric_rows.append((metric, label_text, label, bar))

            vram_label = vram_bar = None
            if s["gpu_vram"] and gpu["vendor"] in ("nvidia","amd"):
                vram_row, _, vram_label = self.metric_pair("VRAM", "")
                vram_bar = progress("#5da9ff")
                self.outer.append(vram_row); self.outer.append(vram_bar)

            status_row, self.gpu_temp_label, self.gpu_clock_label = self.metric_pair("", "", "muted")
            self.outer.append(status_row)

            graph = None
            if s["trendlines"]:
                graph = TrendGraph(1, ["#c77dff"], 28, max(80, self.panel_width - 16))
                self.outer.append(graph)

            self.gpu_rows.append({
                "gpu": gpu, "metrics": metric_rows,
                "vram_label": vram_label, "vram_bar": vram_bar,
                "temp_label": self.gpu_temp_label,
                "clock_label": self.gpu_clock_label,
                "graph": graph
            })

        if s["show_network"]:
            self.title("NETWORK")
            row, _, self.net_iface = self.metric_pair("Interface", "", "muted")
            self.outer.append(row)
            row, _, self.net_down = self.metric_pair("Down", "")
            self.outer.append(row)
            row, _, self.net_up = self.metric_pair("Up", "")
            self.outer.append(row)
            self.net_graph = TrendGraph(2, ["#5da9ff","#ff9f43"], 32, max(80, self.panel_width - 16)) if s["trendlines"] else None
            if self.net_graph: self.outer.append(self.net_graph)
        else:
            self.net_graph = None

        if s["show_disk"]:
            self.title("DISK")
            row, self.disk_path_label, self.disk_usage = self.metric_pair("/", "")
            self.outer.append(row)
            self.disk_bar = progress("#ff5e5e")
            self.outer.append(self.disk_bar)
            row, _, self.disk_read = self.metric_pair("Read", "")
            self.outer.append(row)
            row, _, self.disk_write = self.metric_pair("Write", "")
            self.outer.append(row)
            self.disk_graph = TrendGraph(2, ["#5da9ff","#ff9f43"], 28, max(80, self.panel_width - 16)) if s["trendlines"] else None
            if self.disk_graph: self.outer.append(self.disk_graph)
        else:
            self.disk_graph = None

    def gpu_stats(self, gpu):
        if gpu["vendor"] == "nvidia":
            return nvidia_stats(gpu.get("nvidia_uuid",""))
        if gpu["vendor"] == "amd":
            return amd_stats(gpu)
        if gpu["vendor"] == "intel":
            stats = intel_gpu_stats(gpu.get("intel_device",""))
            temp = gpu_hwmon_temp(gpu)
            clock = intel_clock_mhz(gpu)
            if temp is not None:
                stats["temp"] = temp
            if clock is not None:
                stats["clock_mhz"] = clock
            return stats
        return {}

    def refresh(self):
        now = time.monotonic()
        elapsed = max(0.001, now - self.prev_time)

        cur_cpu = read_proc_stat()
        overall = cpu_usage(self.prev_cpu[0], cur_cpu[0])
        self.cpu_total_label.set_markup(f'<span foreground="#00ff66">CPU</span> {overall:.0f}%')
        self.cpu_total_bar.set_fraction(overall / 100.0)
        if self.cpu_graph:
            self.cpu_graph.push(overall)

        if self.settings["show_freq"]:
            self.freq_label.set_text(f"{cpu_frequency_ghz():.2f} GHz")

        items = read_sensor_items() if self.hardware["sensors_installed"] else []
        package = package_temp(items)
        cores_temp = per_core_temps(items)

        if self.settings["temp_mode"] == "package":
            self.package_temp_label.set_text("N/A" if package is None else f"{package:.0f}°C")

        prev_cores = self.prev_cpu[1]
        cur_cores = cur_cpu[1]
        for row in self.cpu_rows:
            values = []
            for logical in row["logicals"]:
                values.append(cpu_usage(
                    prev_cores[logical] if logical < len(prev_cores) else None,
                    cur_cores[logical] if logical < len(cur_cores) else None,
                ))
            usage = sum(values) / len(values) if values else 0.0
            row["bar"].set_fraction(usage / 100.0)
            if self.settings["temp_mode"] == "cores":
                temp = cores_temp.get(row["core_id"])
                row["right"].set_text(
                    f"{'--' if temp is None else f'{temp:.0f}°C'} {usage:.0f}%"
                )
            else:
                row["right"].set_text(f"{usage:.0f}%")

        mem = mem_stats()
        self.ram_label.set_text(f"{format_bytes(mem['used'])} / {format_bytes(mem['total'])}  {mem['percent']:.0f}%")
        self.ram_bar.set_fraction(mem["percent"] / 100.0)
        if self.mem_graph:
            self.mem_graph.push(mem["percent"])
        if self.swap_label:
            self.swap_label.set_text(f"{format_bytes(mem['swap_used'])} / {format_bytes(mem['swap_total'])}  {mem['swap_percent']:.0f}%")
            self.swap_bar.set_fraction(mem["swap_percent"] / 100.0)

        for row in self.gpu_rows:
            stats = self.gpu_stats(row["gpu"])
            for metric, label_text, label, bar in row["metrics"]:
                value = float(stats.get(metric, 0.0) or 0.0)
                label.set_text(f"{value:.0f}%")
                bar.set_fraction(max(0.0, min(1.0, value / 100.0)))
            if row["vram_label"]:
                used = stats.get("vram_used_mib")
                total = stats.get("vram_total_mib")
                pct = float(stats.get("vram_percent", 0.0) or 0.0)
                if used is None or total is None:
                    row["vram_label"].set_text("N/A")
                else:
                    row["vram_label"].set_text(f"{used/1024:.2f} / {total/1024:.2f} GiB  {pct:.0f}%")
                row["vram_bar"].set_fraction(max(0.0, min(1.0, pct / 100.0)))
            temp = stats.get("temp")
            clock = stats.get("clock_mhz")
            row["temp_label"].set_text("" if temp is None else f"Temp {float(temp):.0f}°C")
            row["clock_label"].set_text("" if clock is None else f"Clock {float(clock):.0f} MHz")

            if row["graph"]:
                row["graph"].push(float(stats.get("overall",0.0) or 0.0))

        if self.settings["show_network"]:
            iface = self.hardware["network"]["primary"]
            cur_net = net_bytes(iface)
            down = max(0, cur_net[0] - self.prev_net[0]) / elapsed
            up = max(0, cur_net[1] - self.prev_net[1]) / elapsed
            self.net_iface.set_text(iface)
            self.net_down.set_markup(f'<span foreground="#5da9ff">{format_bytes(down)}/s</span>')
            self.net_up.set_markup(f'<span foreground="#ff9f43">{format_bytes(up)}/s</span>')
            if self.net_graph:
                # Relative autoscale would be more diagnostic; for now use 100 MiB/s full scale.
                self.net_graph.push(min(100, down / (100*1024*1024) * 100),
                                    min(100, up / (100*1024*1024) * 100))
            self.prev_net = cur_net

        if self.settings["show_disk"]:
            used, total, pct = root_fs_stats()
            self.disk_usage.set_text(f"{format_bytes(used)} / {format_bytes(total)}  {pct:.0f}%")
            self.disk_bar.set_fraction(pct / 100.0)
            cur_disk = disk_stats(self.hardware["disk"]["root_device"])
            rd = max(0, cur_disk[0] - self.prev_disk[0]) / elapsed
            wr = max(0, cur_disk[1] - self.prev_disk[1]) / elapsed
            self.disk_read.set_markup(f'<span foreground="#5da9ff">{format_bytes(rd)}/s</span>')
            self.disk_write.set_markup(f'<span foreground="#ff9f43">{format_bytes(wr)}/s</span>')
            if self.disk_graph:
                self.disk_graph.push(min(100, rd / (500*1024*1024) * 100),
                                     min(100, wr / (500*1024*1024) * 100))
            self.prev_disk = cur_disk

        self.prev_cpu = cur_cpu
        self.prev_time = now
        return True


class ConfigWindow(Gtk.ApplicationWindow):
    def __init__(self, app, settings, hardware):
        super().__init__(application=app, title=f"All CPU Meter for Linux v{APP_VERSION}")
        self.settings = settings
        self.hardware = hardware
        self.set_default_size(760, 680)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(12); root.set_margin_bottom(12)
        root.set_margin_start(12); root.set_margin_end(12)
        self.set_child(root)

        title = Gtk.Label(label=f"All CPU Meter for Linux v{APP_VERSION}", xalign=0)
        title.set_markup(f"<b>All CPU Meter for Linux v{APP_VERSION}</b>")
        root.append(title)

        notebook = Gtk.Notebook()
        notebook.set_hexpand(True); notebook.set_vexpand(True)
        root.append(notebook)

        self.hw_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.display_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.style_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for box in (self.hw_box,self.display_box,self.style_box):
            box.set_margin_top(10); box.set_margin_bottom(10)
            box.set_margin_start(10); box.set_margin_end(10)
        notebook.append_page(self.hw_box, Gtk.Label(label="Hardware"))
        notebook.append_page(self.display_box, Gtk.Label(label="Display"))
        notebook.append_page(self.style_box, Gtk.Label(label="Style"))

        self.vars = {}
        self.gpu_vars = {}
        self.gpu_name_widgets = {}
        self.build_hardware()
        self.build_display()
        self.build_style()

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        root.append(buttons)
        stop = Gtk.Button(label="Stop Meter")
        stop.connect("clicked", self.stop_meter)
        buttons.append(stop)
        start = Gtk.Button(label="Apply and Start Meter")
        start.connect("clicked", self.apply_start)
        start.set_hexpand(True)
        start.set_halign(Gtk.Align.END)
        buttons.append(start)

    def name_controls(self, prefix, label_text, parent):
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        parent.append(Gtk.Separator())
        parent.append(Gtk.Label(label=label_text, xalign=0))

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label="Name handling", xalign=0))
        combo = Gtk.ComboBoxText()
        combo.append("crop", "Crop")
        combo.append("wrap", "Wrap")
        combo.append("custom", "Custom")
        combo.set_active_id(self.settings.get(f"{prefix}_name_mode", "crop"))
        combo.set_hexpand(True)
        combo.set_halign(Gtk.Align.END)
        row.append(combo)
        parent.append(row)
        self.vars[f"{prefix}_name_mode"] = combo

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label="Custom name", xalign=0))
        entry = Gtk.Entry()
        entry.set_text(self.settings.get(f"{prefix}_custom_name", ""))
        entry.set_hexpand(True)
        row.append(entry)
        parent.append(row)
        self.vars[f"{prefix}_custom_name"] = entry

        def update_custom_state(*_args):
            entry.set_sensitive(combo.get_active_id() == "custom")

        combo.connect("changed", update_custom_state)
        update_custom_state()

    def switch(self, key, label, parent, default=True):
        sw = Gtk.Switch(active=bool(self.settings.get(key, default)))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label=label, xalign=0))
        sw.set_halign(Gtk.Align.END)
        sw.set_hexpand(True)
        row.append(sw)
        parent.append(row)
        self.vars[key] = sw
        return sw

    def build_hardware(self):
        cpu = self.hardware["cpu"]
        self.hw_box.append(Gtk.Label(label=f"CPU: {cpu['model']}", xalign=0))
        self.hw_box.append(Gtk.Label(label=f"Logical CPUs: {cpu['logical_cpus']}", xalign=0))
        self.hw_box.append(Gtk.Label(label=f"Network: {self.hardware['network']['primary']}", xalign=0))
        self.hw_box.append(Gtk.Label(label=f"Root device: {self.hardware['disk']['root_device']}", xalign=0))
        self.hw_box.append(Gtk.Separator())
        for i,g in enumerate(self.hardware["gpus"],1):
            self.hw_box.append(Gtk.Label(
                label=f"GPU {i}: {g['description']} [{g['bdf']}; {g['backend_label']}]",
                xalign=0, wrap=True
            ))

    def build_display(self):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label="CPU detail", xalign=0))
        combo = Gtk.ComboBoxText()
        combo.append("overall", "Overall only")
        combo.append("physical", "Physical cores")
        combo.append("logical", "Logical CPUs / threads")
        combo.set_active_id(self.settings.get("cpu_detail", "logical"))
        combo.set_hexpand(True)
        combo.set_halign(Gtk.Align.END)
        row.append(combo)
        self.display_box.append(row)
        self.vars["cpu_detail"] = combo

        self.switch("show_freq","Show CPU frequency",self.display_box,True)
        self.switch("show_swap","Show swap",self.display_box,True)
        self.name_controls("cpu", "CPU display name", self.display_box)
        self.switch("show_network","Show network",self.display_box,True)
        self.switch("show_disk","Show root disk",self.display_box,True)
        self.switch("trendlines","Show trend graphs",self.display_box,True)
        self.switch("gpu_vram","Show VRAM on NVIDIA/AMD GPUs",self.display_box,True)

        self.display_box.append(Gtk.Separator())
        self.display_box.append(Gtk.Label(label="Detected GPUs", xalign=0))
        saved = self.settings.get("gpu_enabled",{})
        saved_names = self.settings.get("gpu_names", {})

        for i,g in enumerate(self.hardware["gpus"],1):
            gpu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            gpu_box.set_margin_bottom(8)
            self.display_box.append(gpu_box)

            default = bool(g.get("backend_available"))
            sw = Gtk.Switch(active=bool(saved.get(g["key"],default)))
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(Gtk.Label(
                label=f"GPU {i}: {g['description']} [{g['backend_label']}]",
                xalign=0, wrap=True
            ))
            sw.set_hexpand(True)
            sw.set_halign(Gtk.Align.END)
            row.append(sw)
            gpu_box.append(row)
            self.gpu_vars[g["key"]] = sw

            current = saved_names.get(g["key"], {})
            mode = Gtk.ComboBoxText()
            mode.append("crop", "Crop")
            mode.append("wrap", "Wrap")
            mode.append("custom", "Custom")
            mode.set_active_id(current.get("mode", "crop"))

            custom = Gtk.Entry()
            custom.set_text(current.get("custom", ""))
            custom.set_placeholder_text("Optional custom name")

            name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            name_row.set_margin_start(16)
            name_row.append(Gtk.Label(label="Display name", xalign=0))
            mode.set_hexpand(True)
            name_row.append(mode)
            gpu_box.append(name_row)

            custom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            custom_row.set_margin_start(16)
            custom_row.append(Gtk.Label(label="Custom name", xalign=0))
            custom.set_hexpand(True)
            custom_row.append(custom)
            gpu_box.append(custom_row)

            def update_entry(combo, entry=custom):
                entry.set_sensitive(combo.get_active_id() == "custom")

            mode.connect("changed", update_entry)
            update_entry(mode)
            self.gpu_name_widgets[g["key"]] = (mode, custom)

        # Intel-specific engine controls are only relevant when Intel graphics
        # is actually present.
        if any(g.get("vendor") == "intel" for g in self.hardware["gpus"]):
            self.display_box.append(Gtk.Separator())
            self.display_box.append(Gtk.Label(label="Intel GPU details", xalign=0))
            self.switch("gpu_overall","Overall GPU",self.display_box,True)
            self.switch("gpu_render","Render/3D",self.display_box,True)
            self.switch("gpu_video","Video/QSV",self.display_box,True)
            self.switch("gpu_video_enhance","Video Enhance",self.display_box,False)

    def build_style(self):
        self.switch("multicolor","Classic multicolor CPU bars",self.style_box,True)
        self.switch("autostart","Start meter automatically at login",self.style_box,True)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label="Preferred position", xalign=0))
        combo = Gtk.ComboBoxText()
        combo.append("top_right", "Top right")
        combo.append("top_left", "Top left")
        combo.append("bottom_right", "Bottom right")
        combo.append("bottom_left", "Bottom left")
        combo.set_active_id(self.settings.get("position", "top_right"))
        combo.set_hexpand(True)
        combo.set_halign(Gtk.Align.END)
        row.append(combo)
        self.style_box.append(row)
        self.vars["position"] = combo

        note = Gtk.Label(
            label="GNOME/Wayland may ignore exact application placement, especially "
                  "inside Remote Desktop sessions. The preference is retained, but "
                  "the undecorated meter can always be dragged manually.",
            xalign=0,
            wrap=True,
        )
        note.add_css_class("muted")
        self.style_box.append(note)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label="Horizontal edge gap", xalign=0))
        adj = Gtk.Adjustment(value=float(self.settings.get("gap_x",12)), lower=0, upper=500, step_increment=1)
        spin = Gtk.SpinButton(adjustment=adj)
        row.append(spin); self.style_box.append(row)
        self.vars["gap_x"] = spin

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label="Vertical edge gap", xalign=0))
        adj = Gtk.Adjustment(value=float(self.settings.get("gap_y",40)), lower=0, upper=500, step_increment=1)
        spin = Gtk.SpinButton(adjustment=adj)
        row.append(spin); self.style_box.append(row)
        self.vars["gap_y"] = spin

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label="Panel width", xalign=0))
        adj = Gtk.Adjustment(value=float(self.settings["width"]), lower=150, upper=500, step_increment=5)
        spin = Gtk.SpinButton(adjustment=adj)
        row.append(spin); self.style_box.append(row)
        self.vars["width"] = spin

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label="Refresh interval (seconds)", xalign=0))
        adj = Gtk.Adjustment(value=float(self.settings["update_interval"]), lower=.25, upper=10, step_increment=.25)
        spin = Gtk.SpinButton(adjustment=adj, digits=2)
        row.append(spin); self.style_box.append(row)
        self.vars["update_interval"] = spin

    def collect(self):
        out = dict(self.settings)
        for key, widget in self.vars.items():
            if isinstance(widget, Gtk.Switch):
                out[key] = widget.get_active()
            elif isinstance(widget, Gtk.SpinButton):
                out[key] = widget.get_value()
            elif isinstance(widget, Gtk.ComboBoxText):
                out[key] = widget.get_active_id() or "crop"
            elif isinstance(widget, Gtk.Entry):
                out[key] = widget.get_text()
        out["width"] = int(out["width"])
        out["gap_x"] = int(out.get("gap_x",12))
        out["gap_y"] = int(out.get("gap_y",40))
        out["gpu_enabled"] = {k:v.get_active() for k,v in self.gpu_vars.items()}
        out["gpu_names"] = {
            key: {
                "mode": widgets[0].get_active_id() or "crop",
                "custom": widgets[1].get_text(),
            }
            for key, widgets in self.gpu_name_widgets.items()
        }
        return out

    def apply_start(self, button):
        settings = self.collect()
        save_settings(settings)
        set_autostart(settings.get("autostart",True))
        subprocess.run(["pkill","-f",f"{SELF} --meter"],check=False)
        subprocess.Popen(
            [sys.executable, str(SELF), "--meter"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=dict(os.environ, ALLCPUMETER_METER="1"),
        )

    def stop_meter(self, button):
        subprocess.run(["pkill","-f",f"{SELF} --meter"],check=False)


def set_autostart(enabled):
    AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    if not enabled:
        AUTOSTART_FILE.unlink(missing_ok=True)
        return
    AUTOSTART_FILE.write_text(f"""[Desktop Entry]
Type=Application
Name=All CPU Meter
Exec=python3 {SELF} --meter
Terminal=false
X-GNOME-Autostart-enabled=true
""")


class AllCpuMeterApp(Gtk.Application):
    def __init__(self, meter_mode=False):
        flags = Gio.ApplicationFlags.NON_UNIQUE if meter_mode else Gio.ApplicationFlags.DEFAULT_FLAGS
        super().__init__(application_id=APP_ID, flags=flags)
        self.meter_mode = meter_mode

    def do_startup(self):
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def do_activate(self):
        settings = load_settings()
        hardware = scan_hardware()
        if self.meter_mode:
            gtk_settings = Gtk.Settings.get_default()
            if gtk_settings is not None:
                gtk_settings.set_property("gtk-enable-animations", False)
            win = MeterWindow(self, settings, hardware)
        else:
            win = ConfigWindow(self, settings, hardware)
        win.present()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--meter", action="store_true")
    args = parser.parse_args()
    app = AllCpuMeterApp(meter_mode=args.meter)
    return app.run(sys.argv[:1])


if __name__ == "__main__":
    raise SystemExit(main())
