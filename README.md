# All CPU Meter for Linux v0.1.0

A compact Ubuntu desktop system monitor inspired by the classic Windows All CPU Meter gadget. The included configurator detects available hardware, lets the user choose the desired level of detail, and generates a matching Conky panel.

## Testing status

This release has been tested on Ubuntu Desktop with an Intel CPU and Intel integrated graphics.

AMD CPU temperature handling, AMD GPU utilization, and NVIDIA GPU utilization are implemented but have not yet been tested on physical AMD or NVIDIA Linux systems. Treat those backends as experimental and please report any hardware-detection or display problems.

## Displays

Depending on detected hardware and selected options, the panel can show:

- Overall CPU utilization and frequency
- Per-logical-CPU utilization
- Package temperature or per-core temperatures
- RAM and optional swap usage
- GPU utilization
- Intel Render/3D, Video/QSV, and Video Enhance utilization
- Root filesystem capacity and disk read/write activity
- Network upload/download throughput and totals
- Color-coded utilization bars
- Optional trend graphs with a muted decorative grid

CPU and GPU names can be cropped, wrapped, or replaced with a custom display name.

## Supported metric backends

- CPU and memory: Linux `/proc` and Conky
- Temperatures: `lm-sensors`
- Intel GPU: `intel_gpu_top` from `intel-gpu-tools`
- NVIDIA GPU: `nvidia-smi`
- AMD GPU: kernel `gpu_busy_percent` interface when exposed
- Disk and network: Conky/Linux system interfaces

## Install

Extract the archive, open a Terminal in the extracted folder, and run:

    chmod +x install.sh
    ./install.sh

The installer uses `apt` to install the base requirements and installs the application for the current user.

After installation, launch **All CPU Meter for Linux** from the Ubuntu application menu.

## Optional dependencies

The Hardware tab reports optional monitoring components that apply to the detected machine. Where appropriate, missing packages can be installed with the GUI's **Install** button.

- CPU temperatures: `lm-sensors`
- Intel GPU utilization: `intel-gpu-tools`
- NVIDIA GPU utilization: `nvidia-smi`, normally supplied by the NVIDIA driver
- AMD GPU utilization: no extra package when the kernel exposes `gpu_busy_percent`

The application does not silently install optional monitoring tools.

## Files

Application:

    ~/.local/share/allcpumeter-linux/allcpumeter.py

Generated Conky configuration:

    ~/.config/conky/allcpumeter-linux.conf

Saved preferences:

    ~/.config/allcpumeter-linux/settings.json

Autostart entry, when enabled:

    ~/.config/autostart/allcpumeter-linux.desktop

## Uninstall

Run:

    ~/.local/share/allcpumeter-linux/uninstall.sh

## Current scope

v0.1.0 targets Ubuntu/Debian systems using `apt`. Desktop placement has been tested on Ubuntu GNOME. Other distributions and desktop environments may require adjustments.
