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

APP_VERSION = "0.2.2"
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
    "compact_cores": False,
    "temp_mode": "package",
    "show_freq": True,
    "show_swap": True,
    "show_network": True,
    "network_enabled": {},
    "network_names": {},
    "network_order": [],
    "compact_network_trends": False,
    "show_disk": True,
    "disk_enabled": {},
    "disk_names": {},
    "disk_order": [],
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
    "compact_gpu_trends": False,
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


def network_interfaces():
    """Return real network interfaces, excluding loopback and common virtual links."""
    base = Path("/sys/class/net")
    if not base.exists():
        return []

    default = default_interface()
    result = []

    for p in sorted(base.iterdir(), key=lambda x: x.name):
        name = p.name
        if name == "lo" or name.startswith(
            ("docker", "br-", "veth", "virbr", "tun", "tap", "wg", "tailscale")
        ):
            continue

        try:
            state = (p / "operstate").read_text().strip()
        except Exception:
            state = "unknown"

        try:
            mac = (p / "address").read_text().strip()
        except Exception:
            mac = ""

        # Best-effort physical-device information.
        device_path = ""
        try:
            device_path = str((p / "device").resolve())
        except Exception:
            pass

        result.append({
            "key": name,
            "name": name,
            "operstate": state,
            "mac": mac,
            "device_path": device_path,
            "is_default": name == default,
        })

    return result


def network_display_name(nic, settings=None):
    settings = settings or {}
    custom = (
        settings.get("network_names", {}).get(nic.get("key", ""), "") or ""
    ).strip()
    return custom or nic.get("name", "NIC")


def ordered_networks(networks, settings):
    by_key = {n["key"]: n for n in networks}
    result = []

    for key in settings.get("network_order", []):
        nic = by_key.pop(key, None)
        if nic is not None:
            result.append(nic)

    # Default route first for newly discovered interfaces, then the rest.
    remaining = list(by_key.values())
    remaining.sort(key=lambda n: (not n.get("is_default", False), n.get("name", "")))
    result.extend(remaining)
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



def _walk_lsblk(node):
    """Yield a lsblk node and all descendants."""
    yield node
    for child in node.get("children") or []:
        yield from _walk_lsblk(child)


def physical_disks():
    """Discover real block devices, including currently unmounted disks."""
    columns = "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,FSTYPE,MOUNTPOINTS"
    rc, out, _ = run(["lsblk", "-J", "-b", "-o", columns], 5)
    if rc != 0 or not out:
        return []

    try:
        data = json.loads(out)
    except Exception:
        return []

    result = []
    for root_node in data.get("blockdevices", []):
        if root_node.get("type") != "disk":
            continue

        name = root_node.get("kname") or root_node.get("name") or ""
        if not name or name.startswith(("loop", "ram", "zram")):
            continue

        mountpoints = []
        seen = set()
        for node in _walk_lsblk(root_node):
            for mountpoint in node.get("mountpoints") or []:
                if not mountpoint or mountpoint in seen:
                    continue
                # Ignore swap and pseudo mountpoints. statvfs() will verify the rest.
                if mountpoint == "[SWAP]":
                    continue
                seen.add(mountpoint)
                mountpoints.append(mountpoint)

        model = (root_node.get("model") or "").strip()
        serial = (root_node.get("serial") or "").strip()
        transport = (root_node.get("tran") or "").strip()

        result.append({
            "key": name,
            "name": name,
            "path": root_node.get("path") or f"/dev/{name}",
            "size": int(root_node.get("size") or 0),
            "model": model,
            "serial": serial,
            "transport": transport,
            "mountpoints": mountpoints,
        })

    return result


def disk_capacity_stats(disk):
    """Aggregate mounted filesystem capacity belonging to one physical disk."""
    total = 0
    used = 0
    mounted = []

    for mountpoint in disk.get("mountpoints", []):
        try:
            st = os.statvfs(mountpoint)
            fs_total = st.f_blocks * st.f_frsize
            fs_free = st.f_bavail * st.f_frsize
            fs_used = max(0, fs_total - fs_free)
        except Exception:
            continue

        # Avoid counting tiny pseudo filesystems that happen to appear below a
        # device-mapper tree.
        if fs_total <= 0:
            continue
        total += fs_total
        used += fs_used
        mounted.append(mountpoint)

    if total > 0:
        return {
            "used": used,
            "total": total,
            "percent": used / total * 100.0,
            "mounted": True,
            "mountpoints": mounted,
        }

    return {
        "used": 0,
        "total": int(disk.get("size") or 0),
        "percent": 0.0,
        "mounted": False,
        "mountpoints": [],
    }


def disk_display_name(disk, settings=None):
    settings = settings or {}
    custom = (settings.get("disk_names", {}).get(disk.get("key", ""), "") or "").strip()
    if custom:
        return custom

    model = (disk.get("model") or "").strip()
    name = disk.get("name") or "disk"
    return f"{name}  {model}".strip() if model else name


def ordered_disks(disks, settings):
    """Return disks in saved user order, followed by newly discovered disks."""
    by_key = {d["key"]: d for d in disks}
    result = []

    for key in settings.get("disk_order", []):
        disk = by_key.pop(key, None)
        if disk is not None:
            result.append(disk)

    for disk in disks:
        if disk["key"] in by_key:
            result.append(by_key.pop(disk["key"]))

    return result

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
        "networks": network_interfaces(),
        "disk": {"root_device": root_device()},
        "disks": physical_disks(),
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



def format_storage_gb(value):
    """Compact storage capacity using conventional GB labeling."""
    return f"{float(value) / (1024 ** 3):.1f} GB"

def format_bytes(value):
    # Compact conventional labels. Values still use 1024-based scaling.
    units = ["B", "KB", "MB", "GB", "TB"]
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
progressbar.compact-core {
    min-width: 0;
    min-height: 3px;
    padding: 0;
}
progressbar.compact-core > trough {
    min-width: 0;
    min-height: 3px;
    max-height: 3px;
    padding: 0;
    border-width: 0;
}
progressbar.compact-core > trough > progress {
    min-width: 0;
    min-height: 3px;
    max-height: 3px;
    padding: 0;
    border-width: 0;
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

    def __init__(self, traces=1, colors=None, height=30, width=160, auto_scale=False):
        super().__init__()
        TrendGraph._serial += 1
        self.graph_id = TrendGraph._serial
        self.histories = [deque(maxlen=120) for _ in range(traces)]
        self.colors = colors or ["#57c7ff"] * traces
        self.graph_width = max(80, int(width))
        self.graph_height = max(18, int(height))
        self.frame_no = 0
        self.auto_scale = bool(auto_scale)

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
            if self.auto_scale:
                hist.append(max(0.0, value))
            else:
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

        graph_scale = 100.0
        if self.auto_scale:
            peak = max(
                (max(hist) if hist else 0.0)
                for hist in self.histories
            )
            # Keep tiny idle noise readable but avoid a scale of zero.
            graph_scale = max(1024.0, peak * 1.10)

        def py(value):
            normalized = (value / graph_scale) * 100.0
            normalized = max(0.0, min(100.0, normalized))
            return int(round((h - 3) - (normalized / 100.0) * max(1, h - 5)))

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


def progress(color, compact=False):
    bar = Gtk.ProgressBar()
    bar.set_show_text(False)
    bar.set_size_request(8, 3 if compact else -1)
    bar.set_hexpand(True)
    bar.add_css_class(BAR_CLASSES.get(color.lower(), "bar-blue"))
    if compact:
        bar.add_css_class("compact-core")
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
        self.prev_nets = {
            nic["key"]: net_bytes(nic["name"])
            for nic in hardware.get("networks", [])
        }
        self.prev_disks = {
            d["key"]: disk_stats(d["name"])
            for d in hardware.get("disks", [])
        }
        self.prev_time = time.monotonic()

        self.cpu_rows = []
        self.gpu_rows = []
        self.core_ids = logical_core_ids()
        self.physical_groups = physical_core_groups()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        outer.add_css_class("meter-root")

        self.viewport = Gtk.ScrolledWindow()
        self.viewport.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        self.viewport.set_propagate_natural_width(True)
        self.viewport.set_propagate_natural_height(True)
        self.viewport.set_child(outer)
        self.set_child(self.viewport)

        self.outer = outer
        self.build_ui()
        self.connect("realize", self._limit_to_monitor)

        interval_ms = max(250, int(float(settings["update_interval"]) * 1000))
        self.refresh()
        GLib.timeout_add(interval_ms, self.refresh)

    def _limit_to_monitor(self, *_args):
        """Cap meter height to the monitor; excess content is clipped."""
        try:
            surface = self.get_surface()
            display = Gdk.Display.get_default()
            monitor = display.get_monitor_at_surface(surface) if surface is not None else None
            if monitor is None:
                monitors = display.get_monitors()
                monitor = monitors.get_item(0) if monitors.get_n_items() else None
            if monitor is None:
                return

            geometry = monitor.get_geometry()
            max_height = max(200, int(geometry.height) - 48)
            self.viewport.set_max_content_height(max_height)
            self.viewport.set_propagate_natural_height(True)
        except Exception:
            pass

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

        compact_cores = bool(s.get("compact_cores", False)) and detail in ("physical", "logical")
        compact_box = None
        if compact_cores and row_specs:
            compact_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            compact_box.set_hexpand(True)
            self.outer.append(compact_box)

        for i, spec in enumerate(row_specs):
            color = PALETTE[i % len(PALETTE)] if s["multicolor"] else "#5da9ff"

            if compact_cores:
                bar = progress(color, compact=True)
                compact_box.append(bar)
                self.cpu_rows.append({
                    "bar": bar,
                    "right": None,
                    "logicals": spec["logicals"],
                    "core_id": spec["core_id"],
                    "compact": True,
                })
                continue

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            label = self.row_label(spec["label"])
            label.set_size_request(38, -1)
            label.set_hexpand(False)
            bar = progress(color)
            bar.set_hexpand(True)
            right = self.row_label("")
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
                "compact": False,
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

        self.gpu_compact_graph = None
        if selected:
            self.title("GPU METER")

            gpu_colors = [
                PALETTE[i % len(PALETTE)]
                for i in range(len(selected))
            ]
            compact_gpu_trends = bool(s.get("compact_gpu_trends", False))

            for idx, (gpu, gpu_color) in enumerate(zip(selected, gpu_colors)):
                if idx > 0:
                    self.outer.append(Gtk.Separator())

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
                    if s["gpu_overall"]:
                        metrics.append(("GPU", "overall", gpu_color))
                    if s["gpu_render"]:
                        metrics.append(("Render/3D", "render", "#00ff66"))
                    if s["gpu_video"]:
                        metrics.append(("Video/QSV", "video", "#ffd93d"))
                    if s["gpu_video_enhance"]:
                        metrics.append(("Video Enhance", "videoenhance", "#00d9ff"))
                else:
                    metrics = [("GPU", "overall", gpu_color)]

                for label_text, metric, color in metrics:
                    metric_row, _, label = self.metric_pair(label_text, "")
                    bar = progress(color)
                    self.outer.append(metric_row)
                    self.outer.append(bar)
                    metric_rows.append((metric, label_text, label, bar))

                vram_label = vram_bar = None
                if s["gpu_vram"] and gpu["vendor"] in ("nvidia", "amd"):
                    vram_row, _, vram_label = self.metric_pair("VRAM", "")
                    vram_bar = progress("#5da9ff")
                    self.outer.append(vram_row)
                    self.outer.append(vram_bar)

                status_row, temp_label, clock_label = self.metric_pair("", "", "muted")
                self.outer.append(status_row)

                graph = None
                if s["trendlines"] and not compact_gpu_trends:
                    graph = TrendGraph(
                        1,
                        [gpu_color],
                        28,
                        max(80, self.panel_width - 16),
                    )
                    self.outer.append(graph)

                self.gpu_rows.append({
                    "gpu": gpu,
                    "color": gpu_color,
                    "metrics": metric_rows,
                    "vram_label": vram_label,
                    "vram_bar": vram_bar,
                    "temp_label": temp_label,
                    "clock_label": clock_label,
                    "graph": graph,
                })

            if s["trendlines"] and compact_gpu_trends:
                self.gpu_compact_graph = TrendGraph(
                    len(selected),
                    gpu_colors,
                    32,
                    max(80, self.panel_width - 16),
                )
                self.outer.append(self.gpu_compact_graph)

        self.network_rows = []
        self.network_compact_graph = None

        if s["show_network"]:
            enabled = s.get("network_enabled", {})
            selected_networks = [
                nic for nic in ordered_networks(
                    self.hardware.get("networks", []), s
                )
                if bool(enabled.get(nic["key"], True))
            ]

            if selected_networks:
                self.title("NETWORK")
                nic_colors = [
                    PALETTE[i % len(PALETTE)]
                    for i in range(len(selected_networks))
                ]
                compact_network_trends = bool(
                    s.get("compact_network_trends", False)
                )

                for idx, (nic, color) in enumerate(
                    zip(selected_networks, nic_colors)
                ):
                    if idx > 0:
                        self.outer.append(Gtk.Separator())

                    name = self.row_label(
                        network_display_name(nic, s),
                        "muted",
                    )
                    self.outer.append(name)

                    io_row = Gtk.Box(
                        orientation=Gtk.Orientation.HORIZONTAL,
                        spacing=4,
                    )

                    down_group = Gtk.Box(
                        orientation=Gtk.Orientation.HORIZONTAL,
                        spacing=3,
                    )
                    down_group.set_hexpand(False)
                    down_group.set_halign(Gtk.Align.START)
                    down_label = Gtk.Label(label="D", xalign=0)
                    down_label.add_css_class("metric-label")
                    down_value = Gtk.Label(label="", xalign=0)
                    down_value.add_css_class("metric-label")
                    down_group.append(down_label)
                    down_group.append(down_value)

                    spacer = Gtk.Box()
                    spacer.set_hexpand(True)

                    up_group = Gtk.Box(
                        orientation=Gtk.Orientation.HORIZONTAL,
                        spacing=3,
                    )
                    up_group.set_hexpand(False)
                    up_group.set_halign(Gtk.Align.END)
                    up_label = Gtk.Label(label="U", xalign=0)
                    up_label.add_css_class("metric-label")
                    up_value = Gtk.Label(label="", xalign=0)
                    up_value.add_css_class("metric-label")
                    up_group.append(up_label)
                    up_group.append(up_value)

                    io_row.append(down_group)
                    io_row.append(spacer)
                    io_row.append(up_group)
                    self.outer.append(io_row)

                    graph = None
                    if s["trendlines"] and not compact_network_trends:
                        # Individual NIC graph contains down and up traces.
                        graph = TrendGraph(
                            2,
                            [color, "#ff9f43"],
                            32,
                            max(80, self.panel_width - 16),
                            auto_scale=True,
                        )
                        self.outer.append(graph)

                    self.network_rows.append({
                        "nic": nic,
                        "color": color,
                        "down_value": down_value,
                        "up_value": up_value,
                        "graph": graph,
                    })

                if s["trendlines"] and compact_network_trends:
                    self.network_compact_graph = TrendGraph(
                        len(selected_networks),
                        nic_colors,
                        32,
                        max(80, self.panel_width - 16),
                        auto_scale=True,
                    )
                    self.outer.append(self.network_compact_graph)

        self.disk_rows = []
        self.disk_graph = None

        if s["show_disk"]:
            enabled = s.get("disk_enabled", {})
            selected_disks = [
                d for d in ordered_disks(self.hardware.get("disks", []), s)
                if bool(enabled.get(d["key"], True))
            ]

            if selected_disks:
                self.title("DISKS")

                disk_colors = [
                    PALETTE[i % len(PALETTE)]
                    for i in range(len(selected_disks))
                ]

                for disk, color in zip(selected_disks, disk_colors):
                    name = self.row_label(disk_display_name(disk, s), "muted")
                    self.outer.append(name)

                    row, _, capacity_label = self.metric_pair("", "")
                    self.outer.append(row)
                    capacity_bar = progress(color)
                    self.outer.append(capacity_bar)

                    io_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

                    read_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
                    read_group.set_hexpand(False)
                    read_group.set_halign(Gtk.Align.START)
                    r_label = Gtk.Label(label="R", xalign=0)
                    r_label.add_css_class("metric-label")
                    r_value = Gtk.Label(label="", xalign=0)
                    r_value.add_css_class("metric-label")
                    read_group.append(r_label)
                    read_group.append(r_value)

                    spacer = Gtk.Box()
                    spacer.set_hexpand(True)

                    write_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
                    write_group.set_hexpand(False)
                    write_group.set_halign(Gtk.Align.END)
                    w_label = Gtk.Label(label="W", xalign=0)
                    w_label.add_css_class("metric-label")
                    w_value = Gtk.Label(label="", xalign=0)
                    w_value.add_css_class("metric-label")
                    write_group.append(w_label)
                    write_group.append(w_value)

                    io_row.append(read_group)
                    io_row.append(spacer)
                    io_row.append(write_group)
                    self.outer.append(io_row)

                    self.disk_rows.append({
                        "disk": disk,
                        "color": color,
                        "capacity_label": capacity_label,
                        "capacity_bar": capacity_bar,
                        "read_label": r_value,
                        "write_label": w_value,
                    })

                if s["trendlines"]:
                    self.disk_graph = TrendGraph(
                        len(selected_disks),
                        disk_colors,
                        32,
                        max(80, self.panel_width - 16),
                        auto_scale=True,
                    )
                    self.outer.append(self.disk_graph)

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
            if row.get("compact"):
                continue
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

        compact_gpu_values = []
        for row in self.gpu_rows:
            stats = self.gpu_stats(row["gpu"])
            compact_gpu_values.append(float(stats.get("overall", 0.0) or 0.0))
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
                    row["vram_label"].set_text(f"{used/1024:.2f} / {total/1024:.2f} GB  {pct:.0f}%")
                row["vram_bar"].set_fraction(max(0.0, min(1.0, pct / 100.0)))
            temp = stats.get("temp")
            clock = stats.get("clock_mhz")
            row["temp_label"].set_text("" if temp is None else f"Temp {float(temp):.0f}°C")
            row["clock_label"].set_text("" if clock is None else f"Clock {float(clock):.0f} MHz")

            if row["graph"]:
                row["graph"].push(float(stats.get("overall", 0.0) or 0.0))

        if self.gpu_compact_graph and compact_gpu_values:
            self.gpu_compact_graph.push(*compact_gpu_values)

        if self.settings["show_network"] and self.network_rows:
            compact_network_values = []

            for row in self.network_rows:
                nic = row["nic"]
                current = net_bytes(nic["name"])
                previous = self.prev_nets.get(nic["key"], current)

                down = max(0, current[0] - previous[0]) / elapsed
                up = max(0, current[1] - previous[1]) / elapsed
                activity = down + up

                row["down_value"].set_text(f"{format_bytes(down)}/s")
                row["up_value"].set_text(f"{format_bytes(up)}/s")

                if row["graph"]:
                    row["graph"].push(down, up)

                compact_network_values.append(activity)
                self.prev_nets[nic["key"]] = current

            if self.network_compact_graph and compact_network_values:
                self.network_compact_graph.push(*compact_network_values)

        if self.settings["show_disk"] and self.disk_rows:
            activities = []

            for row in self.disk_rows:
                disk = row["disk"]
                capacity = disk_capacity_stats(disk)

                if capacity["mounted"]:
                    row["capacity_label"].set_text(
                        f"{format_storage_gb(capacity['used'])} / "
                        f"{format_storage_gb(capacity['total'])}  "
                        f"{capacity['percent']:.0f}%"
                    )
                    row["capacity_bar"].set_fraction(
                        max(0.0, min(1.0, capacity["percent"] / 100.0))
                    )
                else:
                    row["capacity_label"].set_text(
                        f"Unmounted / {format_storage_gb(capacity['total'])}"
                    )
                    row["capacity_bar"].set_fraction(0.0)

                current = disk_stats(disk["name"])
                previous = self.prev_disks.get(disk["key"], current)
                read_rate = max(0, current[0] - previous[0]) / elapsed
                write_rate = max(0, current[1] - previous[1]) / elapsed
                activity = read_rate + write_rate

                row["read_label"].set_text(f"{format_bytes(read_rate)}/s")
                row["write_label"].set_text(f"{format_bytes(write_rate)}/s")
                activities.append(activity)
                self.prev_disks[disk["key"]] = current

            if self.disk_graph:
                self.disk_graph.push(*activities)

        self.prev_cpu = cur_cpu
        self.prev_time = now
        return True


class ConfigWindow(Gtk.ApplicationWindow):
    def __init__(self, app, settings, hardware):
        super().__init__(application=app, title=f"All CPU Meter for Linux v{APP_VERSION}")
        self.settings = settings
        self.hardware = hardware
        self.set_default_size(760, 560)

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

        def scroll_page(box):
            box.set_margin_top(10); box.set_margin_bottom(10)
            box.set_margin_start(10); box.set_margin_end(10)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_vexpand(True)
            scroller.set_hexpand(True)
            scroller.set_child(box)
            return scroller

        notebook.append_page(scroll_page(self.hw_box), Gtk.Label(label="Hardware"))
        notebook.append_page(scroll_page(self.display_box), Gtk.Label(label="Display"))
        notebook.append_page(scroll_page(self.style_box), Gtk.Label(label="Style"))

        self.vars = {}
        self.gpu_vars = {}
        self.gpu_name_widgets = {}
        self.disk_vars = {}
        self.disk_name_entries = {}
        self.disk_order = []
        self.network_vars = {}
        self.network_name_entries = {}
        self.network_order = []
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
        self.hw_box.append(Gtk.Label(
            label=f"Primary network: {self.hardware['network']['primary']}",
            xalign=0
        ))
        for i, nic in enumerate(self.hardware.get("networks", []), 1):
            details = f"NIC {i}: {nic['name']} [{nic.get('operstate','unknown')}]"
            if nic.get("mac"):
                details += f" {nic['mac']}"
            self.hw_box.append(Gtk.Label(label=details, xalign=0, wrap=True))
        self.hw_box.append(Gtk.Label(label=f"Root device: {self.hardware['disk']['root_device']}", xalign=0))
        self.hw_box.append(Gtk.Separator())
        for i, disk in enumerate(self.hardware.get("disks", []), 1):
            mounts = ", ".join(disk.get("mountpoints", [])) or "unmounted"
            self.hw_box.append(Gtk.Label(
                label=(
                    f"Disk {i}: {disk_display_name(disk)} "
                    f"[{format_bytes(disk.get('size', 0))}; {mounts}]"
                ),
                xalign=0,
                wrap=True,
            ))

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

        self.switch(
            "compact_cores",
            "Compact cores/threads",
            self.display_box,
            False
        )
        compact_note = Gtk.Label(
            label="3 px bars with 2 px gaps; hides per-core labels, percentages, and temperatures.",
            xalign=0,
            wrap=True,
        )
        compact_note.add_css_class("muted")
        self.display_box.append(compact_note)

        self.switch("show_freq","Show CPU frequency",self.display_box,True)
        self.switch("show_swap","Show swap",self.display_box,True)
        self.name_controls("cpu", "CPU display name", self.display_box)
        self.switch("show_network","Show network interfaces",self.display_box,True)
        self.switch("show_disk","Show disks",self.display_box,True)
        self.switch("trendlines","Show trend graphs",self.display_box,True)
        self.switch("gpu_vram","Show VRAM on NVIDIA/AMD GPUs",self.display_box,True)

        self.display_box.append(Gtk.Separator())
        self.display_box.append(Gtk.Label(label="Detected network interfaces", xalign=0))

        saved_networks = self.settings.get("network_enabled", {})
        saved_network_names = self.settings.get("network_names", {})

        network_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.display_box.append(network_list)

        self.network_order = [
            nic["key"] for nic in ordered_networks(
                self.hardware.get("networks", []), self.settings
            )
        ]

        def rebuild_network_controls():
            child = network_list.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                network_list.remove(child)
                child = nxt

            by_key = {
                nic["key"]: nic
                for nic in self.hardware.get("networks", [])
            }

            for pos, key in enumerate(self.network_order):
                nic = by_key.get(key)
                if nic is None:
                    continue

                card = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL,
                    spacing=4,
                )
                network_list.append(card)

                top = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL,
                    spacing=6,
                )
                card.append(top)

                details = nic["name"]
                if nic.get("is_default"):
                    details += " [default]"
                if nic.get("operstate"):
                    details += f" [{nic['operstate']}]"

                label = Gtk.Label(
                    label=details,
                    xalign=0,
                    wrap=True,
                )
                label.set_hexpand(True)
                top.append(label)

                up_btn = Gtk.Button(label="↑")
                down_btn = Gtk.Button(label="↓")
                up_btn.set_sensitive(pos > 0)
                down_btn.set_sensitive(
                    pos < len(self.network_order) - 1
                )

                def move_up(_button, key=key):
                    idx = self.network_order.index(key)
                    if idx > 0:
                        self.network_order[idx-1], self.network_order[idx] = (
                            self.network_order[idx],
                            self.network_order[idx-1],
                        )
                        rebuild_network_controls()

                def move_down(_button, key=key):
                    idx = self.network_order.index(key)
                    if idx < len(self.network_order) - 1:
                        self.network_order[idx+1], self.network_order[idx] = (
                            self.network_order[idx],
                            self.network_order[idx+1],
                        )
                        rebuild_network_controls()

                up_btn.connect("clicked", move_up)
                down_btn.connect("clicked", move_down)
                top.append(up_btn)
                top.append(down_btn)

                sw = self.network_vars.get(key)
                if sw is None:
                    sw = Gtk.Switch(
                        active=bool(saved_networks.get(key, True))
                    )
                    self.network_vars[key] = sw
                top.append(sw)

                name_row = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL,
                    spacing=6,
                )
                name_row.set_margin_start(16)
                card.append(name_row)
                name_row.append(
                    Gtk.Label(label="Custom name", xalign=0)
                )

                entry = self.network_name_entries.get(key)
                if entry is None:
                    entry = Gtk.Entry()
                    entry.set_text(saved_network_names.get(key, ""))
                    self.network_name_entries[key] = entry
                entry.set_hexpand(True)
                entry.set_placeholder_text("Use interface name")
                name_row.append(entry)

        rebuild_network_controls()

        if len(self.hardware.get("networks", [])) > 1:
            self.switch(
                "compact_network_trends",
                "Compact network trends",
                self.display_box,
                False,
            )
            network_note = Gtk.Label(
                label=(
                    "Use one shared network activity graph with one "
                    "color-coded trace per enabled interface."
                ),
                xalign=0,
                wrap=True,
            )
            network_note.add_css_class("muted")
            self.display_box.append(network_note)

        self.display_box.append(Gtk.Separator())
        self.display_box.append(Gtk.Label(label="Detected physical disks", xalign=0))
        saved_disks = self.settings.get("disk_enabled", {})
        saved_names = self.settings.get("disk_names", {})

        disk_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.display_box.append(disk_list)
        self.disk_list_box = disk_list

        self.disk_order = [d["key"] for d in ordered_disks(
            self.hardware.get("disks", []), self.settings
        )]

        def rebuild_disk_controls():
            child = disk_list.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                disk_list.remove(child)
                child = nxt

            by_key = {d["key"]: d for d in self.hardware.get("disks", [])}
            for pos, key in enumerate(self.disk_order):
                disk = by_key.get(key)
                if disk is None:
                    continue

                card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                disk_list.append(card)

                top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                card.append(top)

                details = disk_display_name(disk)
                if disk.get("transport"):
                    details += f" [{disk['transport']}]"
                details += f"  {format_storage_gb(disk.get('size', 0))}"

                label = Gtk.Label(label=details, xalign=0, wrap=True)
                label.set_hexpand(True)
                top.append(label)

                up = Gtk.Button(label="↑")
                down = Gtk.Button(label="↓")
                up.set_sensitive(pos > 0)
                down.set_sensitive(pos < len(self.disk_order) - 1)

                def move_up(_button, key=key):
                    idx = self.disk_order.index(key)
                    if idx > 0:
                        self.disk_order[idx-1], self.disk_order[idx] = (
                            self.disk_order[idx], self.disk_order[idx-1]
                        )
                        rebuild_disk_controls()

                def move_down(_button, key=key):
                    idx = self.disk_order.index(key)
                    if idx < len(self.disk_order) - 1:
                        self.disk_order[idx+1], self.disk_order[idx] = (
                            self.disk_order[idx], self.disk_order[idx+1]
                        )
                        rebuild_disk_controls()

                up.connect("clicked", move_up)
                down.connect("clicked", move_down)
                top.append(up)
                top.append(down)

                sw = self.disk_vars.get(key)
                if sw is None:
                    sw = Gtk.Switch(active=bool(saved_disks.get(key, True)))
                    self.disk_vars[key] = sw
                top.append(sw)

                name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                name_row.set_margin_start(16)
                card.append(name_row)
                name_row.append(Gtk.Label(label="Custom name", xalign=0))

                entry = self.disk_name_entries.get(key)
                if entry is None:
                    entry = Gtk.Entry()
                    entry.set_text(saved_names.get(key, ""))
                    self.disk_name_entries[key] = entry
                entry.set_hexpand(True)
                entry.set_placeholder_text("Use detected name")
                name_row.append(entry)

        rebuild_disk_controls()

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

        if len(self.hardware.get("gpus", [])) > 1:
            self.switch(
                "compact_gpu_trends",
                "Compact GPU trends",
                self.display_box,
                False
            )
            compact_gpu_note = Gtk.Label(
                label="Use one shared GPU utilization graph with one color-coded trace per enabled GPU.",
                xalign=0,
                wrap=True,
            )
            compact_gpu_note.add_css_class("muted")
            self.display_box.append(compact_gpu_note)

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
        out["network_enabled"] = {
            k: v.get_active()
            for k, v in self.network_vars.items()
        }
        out["network_names"] = {
            key: entry.get_text()
            for key, entry in self.network_name_entries.items()
        }
        out["network_order"] = list(self.network_order)
        out["disk_enabled"] = {k:v.get_active() for k,v in self.disk_vars.items()}
        out["disk_names"] = {
            key: entry.get_text()
            for key, entry in self.disk_name_entries.items()
        }
        out["disk_order"] = list(self.disk_order)
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
