#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import fcntl
from pathlib import Path

APP_HOME = Path.home() / '.local' / 'share' / 'allcpumeter-linux'
CONKY_DIR = Path.home() / '.config' / 'conky'
CONKY_FILE = CONKY_DIR / 'allcpumeter-linux.conf'
AUTOSTART_DIR = Path.home() / '.config' / 'autostart'
AUTOSTART_FILE = AUTOSTART_DIR / 'allcpumeter-linux.desktop'
SETTINGS_DIR = Path.home() / '.config' / 'allcpumeter-linux'
SETTINGS_FILE = SETTINGS_DIR / 'settings.json'
SELF = APP_HOME / 'allcpumeter.py'
APP_VERSION = '0.1.1'
SETTINGS_SCHEMA_VERSION = 1

PALETTE = ['C77DFF','FFD93D','00D9FF','FF5E5E','00FF66','5DA9FF','B8F34A','FF9F43']

DEFAULTS = {
    'preset':'detailed', 'per_core':True, 'temp_mode':'package', 'show_freq':True,
    'show_swap':True, 'show_gpu':True, 'show_disk':True, 'show_network':True,
    'trendlines':True, 'multicolor':True, 'autostart':True,
    'alignment':'top_right', 'width':180, 'gap_x':12, 'gap_y':40, 'update_interval':1.0,
    'cpu_name_mode':'crop', 'cpu_custom_name':'',
    'gpu_name_mode':'crop', 'gpu_custom_name':'',
    'gpu_overall':True, 'gpu_render':True, 'gpu_video':True, 'gpu_video_enhance':False,
    'gpu_vram':True,
    'settings_version':SETTINGS_SCHEMA_VERSION,
}


def run(cmd, timeout=5):
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 127, '', str(e)


def cpu_info():
    model = 'Unknown CPU'
    try:
        for line in Path('/proc/cpuinfo').read_text(errors='replace').splitlines():
            if line.lower().startswith('model name'):
                model = line.split(':', 1)[1].strip()
                break
    except Exception:
        pass
    return {'model': model, 'logical_cpus': os.cpu_count() or 1}


def logical_core_ids():
    """Return the physical core id corresponding to each logical CPU row."""
    ids = []
    current = {}
    try:
        for line in Path('/proc/cpuinfo').read_text(errors='replace').splitlines() + ['']:
            if not line.strip():
                if current:
                    try:
                        ids.append(int(current.get('core id', len(ids))))
                    except Exception:
                        ids.append(len(ids))
                    current = {}
                continue
            if ':' in line:
                k, v = line.split(':', 1)
                current[k.strip().lower()] = v.strip()
    except Exception:
        pass

    n = os.cpu_count() or 1
    if len(ids) < n:
        ids.extend(range(len(ids), n))
    return ids[:n]


def default_interface():
    rc, out, _ = run(['ip', 'route', 'show', 'default'])
    if rc == 0:
        m = re.search(r'\bdev\s+(\S+)', out)
        if m:
            return m.group(1)

    base = Path('/sys/class/net')
    if base.exists():
        for p in sorted(base.iterdir()):
            n = p.name
            if n != 'lo' and not n.startswith(('docker', 'br-', 'veth', 'virbr', 'tun', 'tap')):
                return n
    return 'lo'


def root_device():
    rc, out, _ = run(['findmnt', '-n', '-o', 'SOURCE', '/'])
    s = out.strip() if rc == 0 else ''
    return s[5:] if s.startswith('/dev/') else s


def gpu_info():
    rc, out, _ = run(['lspci'])
    found = []
    if rc == 0:
        for line in out.splitlines():
            low = line.lower()
            if not any(k in low for k in (
                'vga compatible controller',
                '3d controller',
                'display controller',
            )):
                continue

            vendor = 'unknown'
            if 'intel' in low:
                vendor = 'intel'
            elif 'nvidia' in low:
                vendor = 'nvidia'
            elif 'amd' in low or 'ati' in low:
                vendor = 'amd'

            m = re.search(
                r'(?:VGA compatible controller|3D controller|Display controller):\s*(.*)$',
                line,
                re.I,
            )
            description = m.group(1).strip() if m else line.strip()
            found.append({'vendor': vendor, 'description': description})
    return found


def read_sensor_items():
    if not shutil.which('sensors'):
        return []

    rc, out, _ = run(['sensors', '-j'])
    if rc != 0:
        return []

    try:
        data = json.loads(out)
    except Exception:
        return []

    items = []
    for chip in data.values():
        if not isinstance(chip, dict):
            continue
        for label, vals in chip.items():
            if not isinstance(vals, dict):
                continue
            for k, v in vals.items():
                if str(k).endswith('_input') and isinstance(v, (int, float)):
                    items.append((label, float(v)))
                    break
    return items


def sensors_status():
    installed = shutil.which('sensors') is not None
    items = read_sensor_items() if installed else []
    cores = set()
    package = False

    for label, _ in items:
        low = label.lower()
        if any(x in low for x in ('package', 'tctl', 'tdie', 'cpu')):
            package = True
        m = re.search(r'core\s*(\d+)', low)
        if m:
            cores.add(int(m.group(1)))

    return {
        'installed': installed,
        'usable': bool(items),
        'package_temp': package,
        'core_temps': len(cores),
    }


def amd_busy_path():
    for p in sorted(Path('/sys/class/drm').glob('card*/device/gpu_busy_percent')):
        try:
            p.read_text()
            return str(p)
        except Exception:
            pass
    return ''


def scan():
    gpus = gpu_info()
    return {
        'cpu': cpu_info(),
        'network': {'primary': default_interface()},
        'disk': {'root_device': root_device()},
        'gpus': gpus,
        'sensors': sensors_status(),
        'intel_gpu': {
            'installed': shutil.which('intel_gpu_top') is not None,
            'usable': None,
            'error': '',
            'engines': [],
        },
        'nvidia': {'installed': shutil.which('nvidia-smi') is not None},
        'amd_busy_path': amd_busy_path(),
    }


def package_temp(items):
    for needle in ('package id 0', 'package', 'tctl', 'tdie', 'cpu'):
        for label, val in items:
            if needle in label.lower():
                return val
    return items[0][1] if items else None


def helper_temp(mode, logical_count=None, width=180, multicolor=True):
    items = read_sensor_items()

    if mode == 'package':
        v = package_temp(items)
        print('N/A' if v is None else f'{v:.0f}°C')
        return

    core_temps = {}
    for label, val in items:
        m = re.search(r'core\s*(\d+)', label.lower())
        if m:
            core_temps[int(m.group(1))] = val

    count = int(logical_count or (os.cpu_count() or 1))
    core_ids = logical_core_ids()
    bar_width = max(42, int(width) - 115)

    for i in range(1, count + 1):
        color = metric_color(i - 1, bool(multicolor))
        physical_core = core_ids[i - 1] if i - 1 < len(core_ids) else i - 1
        temp = core_temps.get(physical_core)
        temp_text = f'{temp:.0f}°C' if temp is not None else '--°C'
        print(
            f'${{color {color}}}CPU {i:02d} '
            f'${{cpubar cpu{i} 6,{bar_width}}} '
            f'${{alignr}}{temp_text} ${{cpu cpu{i}}}%'
        )


def _busy_number(value):
    try:
        return max(0.0, min(100.0, float(value)))
    except Exception:
        return None


def _intel_parse_json(text):
    """Return normalized Intel engine utilization from intel_gpu_top JSON."""
    text = (text or '').strip()
    if not text:
        return {'error': 'intel_gpu_top returned no data'}

    try:
        data = json.loads(text)
    except Exception as e:
        cleaned = re.sub(r',\s*]', ']', text)
        if cleaned.startswith('[') and not cleaned.endswith(']'):
            cleaned += ']'
        try:
            data = json.loads(cleaned)
        except Exception:
            return {'error': f'Could not parse intel_gpu_top JSON: {e}'}

    if isinstance(data, list):
        samples = [x for x in data if isinstance(x, dict)]
        if not samples:
            return {'error': 'intel_gpu_top returned no samples'}
        sample = samples[-1]
    elif isinstance(data, dict):
        sample = data
    else:
        return {'error': 'Unexpected intel_gpu_top data format'}

    engines = sample.get('engines', {}) if isinstance(sample, dict) else {}
    vals = {}

    if isinstance(engines, dict):
        for name, info in engines.items():
            if isinstance(info, dict):
                v = _busy_number(info.get('busy'))
                if v is not None:
                    vals[str(name)] = v

    def by_names(*names):
        for wanted in names:
            for k, v in vals.items():
                if k.lower().replace(' ', '') == wanted.lower().replace(' ', ''):
                    return v
        return 0.0

    render = by_names('Render/3D', 'Render', '3D')
    video = by_names('Video')
    enhance = by_names('VideoEnhance', 'Video Enhance')
    compute = by_names('Compute')
    overall = max(vals.values()) if vals else None

    if overall is None:
        return {'error': 'No Intel GPU engine counters were returned'}

    return {
        'overall': overall,
        'render': render,
        'video': video,
        'videoenhance': enhance,
        'compute': compute,
        'engines': vals,
        'error': '',
    }


def intel_gpu_stats(force=False):
    """Sample Intel GPU once and cache it so several Conky rows share one PMU read."""
    cache_dir = Path.home() / '.cache' / 'allcpumeter-linux'
    cache = cache_dir / 'intel-gpu.json'
    lock_path = cache_dir / 'intel-gpu.lock'
    cache_dir.mkdir(parents=True, exist_ok=True)

    def cached_fresh():
        try:
            if force or time.time() - cache.stat().st_mtime > 1.25:
                return None
            d = json.loads(cache.read_text())
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    hit = cached_fresh()
    if hit is not None:
        return hit

    try:
        with lock_path.open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            hit = cached_fresh()
            if hit is not None:
                return hit

            if not shutil.which('intel_gpu_top'):
                result = {'error': 'intel_gpu_top is not installed'}
            else:
                rc, out, err = run(
                    ['intel_gpu_top', '-J', '-s', '500', '-n', '2', '-o', '-'],
                    timeout=3.5,
                )
                if rc != 0:
                    msg = (err or out or f'intel_gpu_top exited with code {rc}').strip()
                    result = {'error': msg.splitlines()[-1] if msg else 'intel_gpu_top failed'}
                else:
                    result = _intel_parse_json(out)

            try:
                cache.write_text(json.dumps(result))
            except Exception:
                pass
            return result
    except Exception as e:
        return {'error': str(e)}


def gpu_value(vendor, amd_path='', metric='overall'):
    if vendor == 'nvidia':
        if metric in ('vram_used', 'vram_total', 'vram_percent'):
            rc, out, _ = run([
                'nvidia-smi',
                '--query-gpu=memory.used,memory.total',
                '--format=csv,noheader,nounits',
            ], 2)
            if rc == 0 and out:
                try:
                    used, total = [
                        float(x.strip())
                        for x in out.splitlines()[0].split(',')[:2]
                    ]
                    if metric == 'vram_used':
                        return used
                    if metric == 'vram_total':
                        return total
                    return 0.0 if total <= 0 else (used / total) * 100.0
                except Exception:
                    pass
        else:
            rc, out, _ = run([
                'nvidia-smi',
                '--query-gpu=utilization.gpu',
                '--format=csv,noheader,nounits',
            ], 2)
            if rc == 0 and out:
                try:
                    return float(out.splitlines()[0])
                except Exception:
                    pass

    elif vendor == 'amd':
        try:
            busy_path = Path(amd_path)
            if metric in ('vram_used', 'vram_total', 'vram_percent'):
                dev = busy_path.parent
                used = float((dev / 'mem_info_vram_used').read_text().strip())
                total = float((dev / 'mem_info_vram_total').read_text().strip())
                if metric == 'vram_used':
                    return used / (1024 * 1024)
                if metric == 'vram_total':
                    return total / (1024 * 1024)
                return 0.0 if total <= 0 else (used / total) * 100.0
            return float(busy_path.read_text().strip())
        except Exception:
            pass

    elif vendor == 'intel':
        stats = intel_gpu_stats()
        v = stats.get(metric)
        if isinstance(v, (int, float)):
            return float(v)

    return None


def helper_gpu(vendor, amd_path='', metric='overall'):
    if metric == 'vram_text':
        used = gpu_value(vendor, amd_path, 'vram_used')
        total = gpu_value(vendor, amd_path, 'vram_total')
        if used is None or total is None or total <= 0:
            print('N/A')
        elif total >= 1024:
            print(f'{used / 1024:.2f} / {total / 1024:.2f} GiB')
        else:
            print(f'{used:.0f} / {total:.0f} MiB')
        return

    v = gpu_value(vendor, amd_path, metric)
    print('N/A' if v is None else f'{max(0, min(100, v)):.0f}')


def intel_gpu_probe():
    if not shutil.which('intel_gpu_top'):
        return {'installed': False, 'usable': False, 'error': 'not installed', 'engines': []}

    d = intel_gpu_stats(force=True)
    return {
        'installed': True,
        'usable': not bool(d.get('error')),
        'error': d.get('error', ''),
        'engines': sorted((d.get('engines') or {}).keys()),
    }


def load_settings():
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
        if isinstance(saved, dict):
            settings.update(saved)
    except Exception:
        pass

    settings['settings_version'] = SETTINGS_SCHEMA_VERSION
    return settings


def save_settings(s):
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


def section(title):
    return f'${{color FFFFFF}}${{font Sans:bold:size=10}}{title}${{font}}${{hr 1}}'


def metric_color(i, multi):
    return PALETTE[i % len(PALETTE)] if multi else '57C7FF'


def _png_chunk(kind, data):
    import struct
    import zlib
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    )


def write_grid_png(lines, width, logical_cpus):
    """Generate one transparent PNG containing all decorative graph grids."""
    import struct
    import zlib

    boxes = grid_boxes_for_lines(lines, width, logical_cpus)
    canvas_w = max(180, int(width))
    canvas_h = max(800, int(max((y + h for _, y, _, h in boxes), default=700) + 20))
    rows = [bytearray(canvas_w * 4) for _ in range(canvas_h)]
    rgba = (112, 112, 112, 72)

    def pixel(x, y):
        if 0 <= x < canvas_w and 0 <= y < canvas_h:
            i = x * 4
            rows[y][i:i + 4] = bytes(rgba)

    for x, y, w, h in boxes:
        x = int(round(x))
        y = int(round(y))
        w = int(round(w))
        h = int(round(h))

        for i in range(1, 6):
            gx = int(round(x + (w * i / 6)))
            for yy in range(y, y + h + 1):
                pixel(gx, yy)

        for i in range(1, 4):
            gy = int(round(y + (h * i / 4)))
            for xx in range(x, x + w + 1):
                pixel(xx, gy)

    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", canvas_w, canvas_h, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )

    path = APP_HOME / "grid_overlay.png"
    APP_HOME.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


def grid_boxes_for_lines(lines, width, logical_cpus):
    """Estimate graph rectangles from Conky's fixed text flow for the generated panel."""
    x = 9.0
    graph_w = max(120.0, float(width) - 18.0)
    y = 8.0
    boxes = []
    graph_y_offset = 13.0
    graph_index = 0

    for line in lines:
        graph_h = 0
        for token, h in (
            ('cpugraph ', 34),
            ('memgraph ', 28),
            ('execigraph ', 28),
            ('downspeedgraph ', 32),
            ('diskiograph_read ', 28),
        ):
            if token in line:
                graph_h = h
                break

        if graph_h:
            cumulative_section_offset = 20.0 * graph_index
            boxes.append((
                x + 1.0,
                y + graph_y_offset + cumulative_section_offset,
                graph_w - 2.0,
                float(graph_h) - 2.0,
            ))
            graph_index += 1
            y += float(graph_h)
            continue

        if '--helper-temp corelines' in line:
            y += 13.0 * max(1, int(logical_cpus))
        elif '${font Sans:bold:size=10}' in line:
            y += 15.0
        elif line == '':
            y += 9.0
        else:
            y += 13.0

    return boxes


def display_name_lines(detected, mode, custom, width):
    """Format a detected hardware name according to Crop, Wrap or Custom."""
    detected = (detected or '').replace('$', '').strip()
    custom = (custom or '').replace('$', '').strip()
    text = custom if mode == 'custom' and custom else detected

    if mode != 'wrap':
        return [text]

    chars = max(16, int((int(width) - 16) / 6.1))
    return textwrap.wrap(
        text,
        width=chars,
        break_long_words=False,
        break_on_hyphens=False,
    ) or ['']


def generate_conky(hw, s):
    cpu = hw['cpu']
    iface = hw['network']['primary']
    rootdev = hw['disk']['root_device']
    gpus = hw.get('gpus', [])
    py = shlex.quote(str(SELF))
    width = max(150, min(500, int(s['width'])))
    bar_width = max(42, width - 115)

    lines = [section('CPU METER')]
    for name_line in display_name_lines(
        cpu['model'],
        s.get('cpu_name_mode', 'crop'),
        s.get('cpu_custom_name', ''),
        width,
    ):
        lines.append('${color AAAAAA}' + name_line)

    lines += [
        '${color 00FF66}CPU ${alignr}${cpu cpu0}%',
        '${color 00FF66}${cpubar cpu0 7,0}',
    ]

    if s['show_freq']:
        lines.append('${color AAAAAA}Frequency ${alignr}${freq_g} GHz')

    if s['temp_mode'] == 'package' and hw['sensors']['installed']:
        lines.append(
            '${color AAAAAA}Temperature ${alignr}${execi 2 python3 '
            + py
            + ' --helper-temp package}'
        )

    if s['per_core']:
        if s['temp_mode'] == 'cores' and hw['sensors']['installed']:
            multi = '1' if s['multicolor'] else '0'
            lines.append(
                '${execpi 1 python3 '
                + py
                + f' --helper-temp corelines --logical-count {int(cpu["logical_cpus"])} '
                  f'--width {width} --multicolor {multi}'
                + '}'
            )
        else:
            for i in range(1, int(cpu['logical_cpus']) + 1):
                c = metric_color(i - 1, s['multicolor'])
                lines.append(
                    f'${{color {c}}}CPU {i:02d} '
                    f'${{cpubar cpu{i} 6,{bar_width}}} '
                    f'${{alignr}}${{cpu cpu{i}}}%'
                )

    if s['trendlines']:
        lines.append('${color 00FF66}${cpugraph cpu0 34,0 00FF66 00FF66}')

    lines += [
        '',
        section('MEMORY'),
        '${color 5DA9FF}RAM ${alignr}${mem} / ${memmax}  ${memperc}%',
        '${color 5DA9FF}${membar 7,0}',
    ]

    if s['show_swap']:
        lines += [
            '${color FFD93D}Swap ${alignr}${swap} / ${swapmax}  ${swapperc}%',
            '${color FFD93D}${swapbar 6,0}',
        ]

    if s['trendlines']:
        lines.append('${color 5DA9FF}${memgraph 28,0 5DA9FF 5DA9FF}')

    if s['show_gpu'] and gpus:
        g = gpus[0]
        vendor = g['vendor']
        lines += ['', section('GPU METER')]

        for name_line in display_name_lines(
            g['description'],
            s.get('gpu_name_mode', 'crop'),
            s.get('gpu_custom_name', ''),
            width,
        ):
            lines.append('${color AAAAAA}' + name_line)

        base = ''
        if vendor == 'intel' and hw['intel_gpu']['installed']:
            base = f'python3 {py} --helper-gpu intel'
        elif vendor == 'nvidia' and hw['nvidia']['installed']:
            base = f'python3 {py} --helper-gpu nvidia'
        elif vendor == 'amd' and hw['amd_busy_path']:
            base = (
                f'python3 {py} --helper-gpu amd '
                f'--amd-path {shlex.quote(hw["amd_busy_path"])}'
            )

        if base:
            if vendor == 'intel':
                metrics = []
                if s.get('gpu_overall', True):
                    metrics.append(('GPU', 'overall', 'C77DFF'))
                if s.get('gpu_render', True):
                    metrics.append(('Render/3D', 'render', '00FF66'))
                if s.get('gpu_video', True):
                    metrics.append(('Video/QSV', 'video', 'FFD93D'))
                if s.get('gpu_video_enhance', False):
                    metrics.append(('Video Enhance', 'videoenhance', '00D9FF'))
                if not metrics:
                    metrics = [('GPU', 'overall', 'C77DFF')]

                for label, metric, color in metrics:
                    cmd = base + ' --metric ' + metric
                    lines.append(
                        '${color ' + color + '}' + label
                        + ' ${alignr}${execi 2 ' + cmd + '}%'
                    )
                    lines.append(
                        '${color ' + color + '}${execibar 2 6,0 ' + cmd + '}'
                    )

                if s['trendlines']:
                    cmd = base + ' --metric overall'
                    lines.append(
                        '${color C77DFF}${execigraph 2 "'
                        + cmd
                        + '" 28,0 C77DFF C77DFF 100}'
                    )
            else:
                lines += [
                    '${color C77DFF}GPU ${alignr}${execi 2 ' + base + '}%',
                    '${color C77DFF}${execibar 2 7,0 ' + base + '}',
                ]
                if s['trendlines']:
                    lines.append(
                        '${color C77DFF}${execigraph 2 "'
                        + base
                        + '" 28,0 C77DFF C77DFF 100}'
                    )
        else:
            lines.append('${color AAAAAA}Utilization backend unavailable')

    if s['show_network']:
        lines += [
            '',
            section('NETWORK'),
            f'${{color AAAAAA}}Interface ${{alignr}}{iface}',
            f'${{color 5DA9FF}}Down ${{alignr}}${{downspeedf {iface}}} KiB/s',
            f'${{color FF9F43}}Up   ${{alignr}}${{upspeedf {iface}}} KiB/s',
        ]
        if s['trendlines']:
            lines.append(
                f'${{color 5DA9FF}}${{downspeedgraph {iface} 32,0 5DA9FF 5DA9FF}}'
                f'${{goto 8}}${{color 5DA9FF}}'
                f'${{upspeedgraph {iface} 32,0 FF9F43 FF9F43}}'
            )
        lines += [
            f'${{color AAAAAA}}Total down ${{alignr}}${{totaldown {iface}}}',
            f'${{color AAAAAA}}Total up   ${{alignr}}${{totalup {iface}}}',
        ]

    if s['show_disk']:
        lines += [
            '',
            section('DISK'),
            '${color FF5E5E}/ ${alignr}${fs_used /} / ${fs_size /}  ${fs_used_perc /}%',
            '${color FF5E5E}${fs_bar 7,0 /}',
        ]

        if rootdev:
            lines += [
                f'${{color 5DA9FF}}Read  ${{alignr}}${{diskio_read {rootdev}}}',
                f'${{color FF9F43}}Write ${{alignr}}${{diskio_write {rootdev}}}',
            ]
            if s['trendlines']:
                lines.append(
                    f'${{color FF5E5E}}'
                    f'${{diskiograph_read {rootdev} 28,0 5DA9FF 5DA9FF}}'
                    f'${{goto 8}}${{color FF5E5E}}'
                    f'${{diskiograph_write {rootdev} 28,0 FF9F43 FF9F43}}'
                )

    interval = max(.5, min(10, float(s['update_interval'])))
    grid_png = (
        write_grid_png(lines, width, cpu['logical_cpus'])
        if s['trendlines']
        else None
    )
    text = '\n'.join(lines)

    # Conky can have both the Wayland and X11 backends compiled in.  On a
    # Wayland GNOME session, explicitly disable X11 so startup cannot touch
    # Xwayland.  This avoids a GNOME/Xwayland startup crash observed when the
    # meter autostarts during Remote Login session initialization.
    return f"""local session_type = string.lower(os.getenv("XDG_SESSION_TYPE") or "")
local use_wayland = (session_type == "wayland")

conky.config = {{
    out_to_x = not use_wayland,
    out_to_wayland = use_wayland,
    update_interval = {interval},
    total_run_times = 0,
    double_buffer = true,
    no_buffers = true,
    cpu_avg_samples = 2,
    net_avg_samples = 2,
    own_window = true,
    own_window_type = 'normal',
    own_window_hints = 'undecorated,below,sticky,skip_taskbar,skip_pager',
    own_window_transparent = false,
    own_window_colour = '111111',
    own_window_argb_visual = true,
    own_window_argb_value = 225,
    alignment = '{s['alignment']}',
    gap_x = {int(s['gap_x'])},
    gap_y = {int(s['gap_y'])},
    minimum_width = {width},
    maximum_width = {width},
    draw_shades = false,
    draw_outline = false,
    draw_borders = true,
    border_width = 1,
    border_inner_margin = 8,
    default_color = 'DDDDDD',
    font = 'DejaVu Sans Mono:size=8',
    use_xft = true,
}};
conky.text = [[
${{image {grid_png if grid_png else ""} -p 0,0 -n}}
{text}
]];
"""


def stop_meter():
    subprocess.run(
        ['pkill', '-f', 'conky.*allcpumeter-linux.conf'],
        check=False,
    )


def start_meter():
    stop_meter()
    subprocess.Popen(
        ['conky', '-c', str(CONKY_FILE)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def write_config(hw, s):
    CONKY_DIR.mkdir(parents=True, exist_ok=True)
    CONKY_FILE.write_text(generate_conky(hw, s))
    save_settings(s)

    AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    if s['autostart']:
        AUTOSTART_FILE.write_text(
            f"""[Desktop Entry]
Type=Application
Name=All CPU Meter
Exec=sh -c 'sleep 10; conky -c {CONKY_FILE}'
Terminal=false
X-GNOME-Autostart-enabled=true
"""
        )
    else:
        AUTOSTART_FILE.unlink(missing_ok=True)


def launch_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title('All CPU Meter for Linux')
            self.geometry('780x780')
            self.minsize(720, 680)
            self.hw = {}
            self.settings = load_settings()
            self.vars = {}
            self.build()
            self.refresh()

        def v(self, name, default, kind='bool'):
            val = self.settings.get(name, default)
            cls = {
                'bool': tk.BooleanVar,
                'str': tk.StringVar,
                'int': tk.IntVar,
                'float': tk.DoubleVar,
            }[kind]
            var = cls(value=val)
            self.vars[name] = var
            return var

        def build(self):
            outer = ttk.Frame(self, padding=12)
            outer.pack(fill='both', expand=True)

            ttk.Label(
                outer,
                text='All CPU Meter for Linux',
                font=('', 16, 'bold'),
            ).pack(anchor='w')

            ttk.Label(
                outer,
                text='Detect hardware, choose the detail you want, then generate and start the desktop meter.',
            ).pack(anchor='w', pady=(2, 10))

            nb = ttk.Notebook(outer)
            nb.pack(fill='both', expand=True)

            self.tab_hw = ttk.Frame(nb, padding=12)
            self.tab_display = ttk.Frame(nb, padding=12)
            self.tab_style = ttk.Frame(nb, padding=12)

            nb.add(self.tab_hw, text='Hardware')
            nb.add(self.tab_display, text='Display')
            nb.add(self.tab_style, text='Style')

            self.hw_text = tk.Text(self.tab_hw, height=18, wrap='word')
            self.hw_text.pack(fill='both', expand=True)

            ttk.Button(
                self.tab_hw,
                text='Rescan Hardware',
                command=self.refresh,
            ).pack(anchor='w', pady=(8, 0))

            self.dep = ttk.LabelFrame(
                self.tab_hw,
                text='Optional Dependencies',
                padding=8,
            )
            self.dep.pack(fill='x', pady=(10, 0))

            self.build_display()
            self.build_style()

            b = ttk.Frame(outer)
            b.pack(fill='x', pady=(10, 0))

            ttk.Button(
                b,
                text='Stop Meter',
                command=self.stop,
            ).pack(side='left')

            ttk.Button(
                b,
                text='Apply and Start Meter',
                command=self.apply,
            ).pack(side='right')

        def build_display(self):
            f = ttk.LabelFrame(self.tab_display, text='Preset', padding=8)
            f.pack(fill='x')
            pv = self.v('preset', 'detailed', 'str')

            for label, value in [
                ('Basic', 'basic'),
                ('Detailed', 'detailed'),
                ('Custom', 'custom'),
            ]:
                ttk.Radiobutton(
                    f,
                    text=label,
                    value=value,
                    variable=pv,
                    command=self.preset,
                ).pack(side='left', padx=(0, 16))

            f = ttk.LabelFrame(
                self.tab_display,
                text='Hardware names',
                padding=8,
            )
            f.pack(fill='x', pady=(10, 0))

            for row, (title, prefix) in enumerate([('CPU', 'cpu'), ('GPU', 'gpu')]):
                ttk.Label(f, text=title, width=5).grid(
                    row=row,
                    column=0,
                    sticky='w',
                    pady=3,
                )
                mode = self.v(prefix + '_name_mode', 'crop', 'str')
                for col, (label, value) in enumerate([
                    ('Crop', 'crop'),
                    ('Wrap', 'wrap'),
                    ('Custom', 'custom'),
                ], 1):
                    ttk.Radiobutton(
                        f,
                        text=label,
                        value=value,
                        variable=mode,
                    ).grid(
                        row=row,
                        column=col,
                        sticky='w',
                        padx=(0, 6),
                    )

                ttk.Entry(
                    f,
                    textvariable=self.v(prefix + '_custom_name', '', 'str'),
                    width=31,
                ).grid(
                    row=row,
                    column=4,
                    sticky='ew',
                    padx=(8, 0),
                )

            f.columnconfigure(4, weight=1)

            f = ttk.LabelFrame(self.tab_display, text='CPU', padding=8)
            f.pack(fill='x', pady=(10, 0))

            ttk.Checkbutton(
                f,
                text='Show each logical CPU',
                variable=self.v('per_core', True),
            ).pack(anchor='w')

            ttk.Checkbutton(
                f,
                text='Show CPU frequency',
                variable=self.v('show_freq', True),
            ).pack(anchor='w')

            tv = self.v('temp_mode', 'package', 'str')
            ttk.Label(f, text='Temperature detail:').pack(
                anchor='w',
                pady=(6, 0),
            )

            for label, value in [
                ('Package / whole-chip temperature', 'package'),
                ('Per-core temperatures when available', 'cores'),
                ('Do not show CPU temperature', 'off'),
            ]:
                ttk.Radiobutton(
                    f,
                    text=label,
                    value=value,
                    variable=tv,
                ).pack(anchor='w')

            f = ttk.LabelFrame(
                self.tab_display,
                text='Memory and Devices',
                padding=8,
            )
            f.pack(fill='x', pady=(10, 0))

            for name, label, default in [
                ('show_swap', 'Show swap', True),
                ('show_gpu', 'Show GPU', True),
                ('show_network', 'Show network', True),
                ('show_disk', 'Show root disk', True),
            ]:
                ttk.Checkbutton(
                    f,
                    text=label,
                    variable=self.v(name, default),
                ).pack(anchor='w')

            f = ttk.LabelFrame(
                self.tab_display,
                text='Intel GPU details',
                padding=8,
            )
            f.pack(fill='x', pady=(10, 0))

            ttk.Label(
                f,
                text='For Intel GPUs, choose the engine utilization rows to display. Video/QSV is the hardware video engine used by Quick Sync.',
            ).pack(anchor='w')

            row = ttk.Frame(f)
            row.pack(fill='x', pady=(4, 0))

            for name, label, default in [
                ('gpu_overall', 'Overall (busiest engine)', True),
                ('gpu_render', 'Render/3D', True),
                ('gpu_video', 'Video/QSV', True),
                ('gpu_video_enhance', 'Video Enhance', False),
            ]:
                ttk.Checkbutton(
                    row,
                    text=label,
                    variable=self.v(name, default),
                ).pack(side='left', padx=(0, 12))

        def build_style(self):
            f = ttk.LabelFrame(
                self.tab_style,
                text='Appearance',
                padding=8,
            )
            f.pack(fill='x')

            ttk.Checkbutton(
                f,
                text='Classic multicolor per-CPU bars',
                variable=self.v('multicolor', True),
            ).pack(anchor='w')

            ttk.Checkbutton(
                f,
                text='Show trend graphs',
                variable=self.v('trendlines', True),
            ).pack(anchor='w')

            ttk.Checkbutton(
                f,
                text='Start meter automatically when I log in',
                variable=self.v('autostart', True),
            ).pack(anchor='w')

            f = ttk.LabelFrame(
                self.tab_style,
                text='Position and Size',
                padding=8,
            )
            f.pack(fill='x', pady=(10, 0))

            rows = [
                ('Position', 'alignment', 'top_right', 'str'),
                ('Width (pixels)', 'width', 180, 'int'),
                ('Horizontal gap', 'gap_x', 12, 'int'),
                ('Vertical gap', 'gap_y', 40, 'int'),
                ('Refresh interval (seconds)', 'update_interval', 1.0, 'float'),
            ]

            for r, (label, name, default, kind) in enumerate(rows):
                ttk.Label(f, text=label).grid(
                    row=r,
                    column=0,
                    sticky='w',
                    pady=4,
                )

                if name == 'alignment':
                    ttk.Combobox(
                        f,
                        textvariable=self.v(name, default, kind),
                        values=[
                            'top_right',
                            'top_left',
                            'bottom_right',
                            'bottom_left',
                        ],
                        state='readonly',
                        width=18,
                    ).grid(
                        row=r,
                        column=1,
                        sticky='w',
                        padx=8,
                    )
                else:
                    minimum = 150 if name == 'width' else (.5 if kind == 'float' else 0)
                    ttk.Spinbox(
                        f,
                        from_=minimum,
                        to=500,
                        increment=.5 if kind == 'float' else 1,
                        textvariable=self.v(name, default, kind),
                        width=8,
                    ).grid(
                        row=r,
                        column=1,
                        sticky='w',
                        padx=8,
                    )

        def refresh(self):
            self.config(cursor='watch')
            self.update_idletasks()
            self.hw = scan()

            if (
                any(g.get('vendor') == 'intel' for g in self.hw.get('gpus', []))
                and self.hw['intel_gpu']['installed']
            ):
                self.hw['intel_gpu'] = intel_gpu_probe()

            self.config(cursor='')

            h = self.hw
            lines = [
                f"CPU: {h['cpu']['model']}",
                f"Logical CPUs: {h['cpu']['logical_cpus']}",
                '',
                f"Primary network interface: {h['network']['primary']}",
                f"Root disk device: {h['disk']['root_device'] or 'not determined'}",
                '',
            ]

            if h['gpus']:
                for i, g in enumerate(h['gpus'], 1):
                    lines.append(
                        f"GPU {i}: {g['vendor'].upper()} - {g['description']}"
                    )
            else:
                lines.append('GPU: none detected by lspci')

            lines += [
                '',
                'lm-sensors: '
                + (
                    'available'
                    if h['sensors']['usable']
                    else 'installed but no readable temperatures'
                    if h['sensors']['installed']
                    else 'not installed'
                ),
            ]

            if h['sensors']['usable']:
                lines.append(
                    f"Per-core temperature labels found: {h['sensors']['core_temps']}"
                )

            if (
                any(g.get('vendor') == 'intel' for g in h.get('gpus', []))
                and h['intel_gpu']['installed']
            ):
                if h['intel_gpu'].get('usable'):
                    lines.append('Intel GPU counters: available')
                    if h['intel_gpu'].get('engines'):
                        lines.append(
                            'Intel GPU engines: '
                            + ', '.join(h['intel_gpu']['engines'])
                        )
                else:
                    lines.append('Intel GPU counters: unavailable')
                    if h['intel_gpu'].get('error'):
                        lines.append(
                            'intel_gpu_top: ' + h['intel_gpu']['error']
                        )

            self.hw_text.delete('1.0', 'end')
            self.hw_text.insert('1.0', '\n'.join(lines))
            self.render_deps()

        def render_deps(self):
            for w in self.dep.winfo_children():
                w.destroy()

            row = 0

            def dep_line(text, package=None):
                nonlocal row
                ttk.Label(self.dep, text=text).grid(
                    row=row,
                    column=0,
                    sticky='w',
                )
                if package:
                    ttk.Button(
                        self.dep,
                        text='Install',
                        command=lambda p=package: self.install_pkg(p),
                    ).grid(
                        row=row,
                        column=1,
                        padx=8,
                    )
                row += 1

            dep_line(
                'CPU temperatures (lm-sensors): '
                + (
                    'Installed'
                    if self.hw['sensors']['installed']
                    else 'Not installed'
                ),
                None if self.hw['sensors']['installed'] else 'lm-sensors',
            )

            vendors = {g['vendor'] for g in self.hw['gpus']}

            if 'intel' in vendors:
                if not self.hw['intel_gpu']['installed']:
                    dep_line(
                        'Intel GPU utilization (intel-gpu-tools): Not installed',
                        'intel-gpu-tools',
                    )
                elif self.hw['intel_gpu'].get('usable') is False:
                    dep_line(
                        'Intel GPU utilization: installed, but counters are not readable'
                    )
                else:
                    dep_line(
                        'Intel GPU utilization (intel-gpu-tools): Available'
                    )

            if 'nvidia' in vendors:
                dep_line(
                    'NVIDIA GPU utility (nvidia-smi): '
                    + (
                        'Available'
                        if self.hw['nvidia']['installed']
                        else 'Not available'
                    )
                )

            if 'amd' in vendors:
                dep_line(
                    'AMD kernel utilization counter: '
                    + (
                        'Available'
                        if self.hw['amd_busy_path']
                        else 'Not exposed'
                    )
                )

        def install_pkg(self, pkg):
            try:
                rc = subprocess.run(
                    ['pkexec', 'apt-get', 'install', '-y', pkg],
                    check=False,
                ).returncode
                if rc == 0:
                    messagebox.showinfo(
                        'Installed',
                        f'{pkg} was installed successfully.',
                    )
                    self.refresh()
                else:
                    messagebox.showerror(
                        'Install failed',
                        f'Ubuntu returned exit code {rc}.',
                    )
            except Exception as e:
                messagebox.showerror('Install failed', str(e))

        def preset(self):
            p = self.vars['preset'].get()
            vals = {
                'basic': dict(
                    per_core=False,
                    temp_mode='package',
                    show_freq=False,
                    show_swap=False,
                    show_gpu=True,
                    show_disk=True,
                    show_network=True,
                    trendlines=False,
                    gpu_overall=True,
                    gpu_render=False,
                    gpu_video=False,
                    gpu_video_enhance=False,
                ),
                'detailed': dict(
                    per_core=True,
                    temp_mode='package',
                    show_freq=True,
                    show_swap=True,
                    show_gpu=True,
                    show_disk=True,
                    show_network=True,
                    trendlines=True,
                    gpu_overall=True,
                    gpu_render=True,
                    gpu_video=True,
                    gpu_video_enhance=False,
                ),
            }.get(p)

            if vals:
                for k, v in vals.items():
                    self.vars[k].set(v)

        def collect(self):
            out = {k: v.get() for k, v in self.vars.items()}
            out['settings_version'] = SETTINGS_SCHEMA_VERSION
            return out

        def apply(self):
            try:
                write_config(self.hw, self.collect())
                start_meter()
                messagebox.showinfo(
                    'Meter started',
                    f'The meter is running.\n\nConfiguration:\n{CONKY_FILE}',
                )
            except Exception as e:
                messagebox.showerror('Could not start meter', str(e))

        def stop(self):
            stop_meter()
            messagebox.showinfo(
                'Meter stopped',
                'The All CPU Meter Conky process was stopped.',
            )

    App().mainloop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--helper-temp', choices=['package', 'corelines'])
    ap.add_argument('--helper-gpu', choices=['intel', 'nvidia', 'amd'])
    ap.add_argument(
        '--metric',
        choices=[
            'overall',
            'render',
            'video',
            'videoenhance',
            'compute',
            'vram_used',
            'vram_total',
            'vram_percent',
            'vram_text',
        ],
        default='overall',
    )
    ap.add_argument('--amd-path', default='')
    ap.add_argument('--logical-count', type=int)
    ap.add_argument('--width', type=int, default=180)
    ap.add_argument('--multicolor', type=int, default=1)
    args = ap.parse_args()

    if args.helper_temp:
        helper_temp(
            args.helper_temp,
            args.logical_count,
            args.width,
            bool(args.multicolor),
        )
    elif args.helper_gpu:
        helper_gpu(
            args.helper_gpu,
            args.amd_path,
            args.metric,
        )
    else:
        launch_gui()


if __name__ == '__main__':
    main()
