# All CPU Meter for Linux v0.1.1

A compact Ubuntu desktop system monitor inspired by the classic Windows All CPU Meter gadget. The included configurator detects available hardware, lets the user choose the desired level of detail, then generates and launches a matching Conky panel.  Nothing here is revolutionary, but it is very automated so that you can (hopefully, if everything works) get a nice little resource monitor on your desktop with just a few clicks.  Look at the bars dance.  Numbers be numberin'.  Aren't they pretty?

<img width="180" alt="The Meter" src="https://github.com/user-attachments/assets/9fe9c8ca-8611-4178-8231-a5d3f49b20c1" />

<img width="600" alt="Some Settings" src="https://github.com/user-attachments/assets/6815bc45-79c1-446a-a735-9dc129438a40" />


## Testing status

This release has been tested on Ubuntu Desktop 26.04 with an Intel CPU and Intel integrated graphics.

AMD CPU temperature handling, AMD GPU utilization, and NVIDIA GPU utilization are implemented but have not been tested on actual hardware systems. Treat those backends as experimental.

## Displays

Depending on detected hardware and selected options, the panel can show:

- Overall and per-core CPU utilization and frequency
- Package and per-core temperatures
- RAM and swap usage
- GPU & VRAM utilization
- Intel Render/3D, Video/QSV, and Video Enhance utilization
- Root filesystem capacity and disk read/write activity
- Network upload/download throughput and totals
- Color-coded utilization bars

CPU and GPU names can be cropped, wrapped, or replaced with a custom display name.

## Supported metric backends

- CPU and memory: Linux `/proc` and Conky
- Temperatures: `lm-sensors`
- Intel GPU: `intel_gpu_top` from `intel-gpu-tools`
- NVIDIA GPU: `nvidia-smi`
- AMD GPU: kernel `gpu_busy_percent` interface when exposed
- Disk and network: Conky/Linux system interfaces

## Install

Clone the repo, navigate to the folder, and run:

    chmod +x install.sh
    ./install.sh

The installer uses `apt` to install the base requirements and installs the application for the current user.

After installation, launch **All CPU Meter for Linux** from the Ubuntu application menu.

### Optional dependencies

The Hardware tab reports optional monitoring components that apply to the detected machine. Where appropriate, missing packages can be installed with the GUI's **Install** button.

- CPU temperatures: `lm-sensors`
- Intel GPU utilization: `intel-gpu-tools`
- NVIDIA GPU utilization: `nvidia-smi`, normally supplied by the NVIDIA driver
- AMD GPU utilization: no extra package when the kernel exposes `gpu_busy_percent`

The application does not silently install optional monitoring tools.

### Upgrade All CPU Meter

Do a git-pull and re-run the install script.

## Uninstall

Run:

    ~/.local/share/allcpumeter-linux/uninstall.sh

## AI & Safety Disclaimer

The code and documentation included in this project is primarily vibeslop. The human writing this sentence in particular can barely code and doesn't really understand how any of this works. It Works On My Machine and hasn't caused my genitals to explode, but your mileage may vary. I make absolutely no guarantee as to the safety or security of the contents of this project. Use at your own risk. Or don't.

## Current scope

v0.1.0 targets Ubuntu/Debian systems using `apt`. Desktop placement has been tested on Ubuntu GNOME. Other distributions and desktop environments probably won't work without effort on your part.

## License

All CPU Meter for Linux is released into the public domain under [The Unlicense](LICENSE).

Copyleft 2026. Do what thou wilt shall be the whole of the law. One step closer to AGI.
