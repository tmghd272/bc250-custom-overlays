#!/usr/bin/env python3
import os
import time
import subprocess
import re
from datetime import datetime
from library.lcd.lcd_comm_rev_a import LcdCommRevA, Orientation
from library.log import logger
import psutil
import copy

# Set working directory to script's location
os.chdir(os.path.dirname(os.path.abspath(__file__)))

COM_PORT = "AUTO"
WIDTH, HEIGHT = 480, 320
REVISION = "A"

# =========================================================
# Hardware Stats Functions
# =========================================================

# ---------------- GPU Load (%) ----------------
def get_gpu_load():
    """Read GPU load (%) from patched gpu_metrics (offset 0x1C)."""
    try:
        with open("/sys/class/drm/card1/device/gpu_metrics", "rb") as f:
            data = f.read(128)

        load = int.from_bytes(data[28:30], byteorder="little")

        # If broken (65535) or invalid, return 0
        if load == 65535 or load > 100:
            return 0
        
        return load

    except Exception as e:
        logger.warning(f"GPU load read error: {e}")
        return 0

# ---------------- GPU Clock (MHz), Temperature (°C), VRAM Usage (GB) ----------------
def get_gpu_stats():
    """Read GPU clock (MHz), temperature (°C), and VRAM usage (GB)."""
    clock = temp = 0
    used_gb = 0.0
    try:
        clock_path = "/sys/class/drm/card1/device/pp_dpm_sclk"
        if os.path.exists(clock_path):
            with open(clock_path) as f:
                for line in f:
                    if "*" in line:
                        parts = line.strip().split(":")[-1].strip().split("Mhz")[0]
                        clock = int(parts)
                        break

        temp_path = "/sys/class/drm/card1/device/hwmon"
        for entry in os.listdir(temp_path):
            sensor_path = os.path.join(temp_path, entry, "temp1_input")
            if os.path.exists(sensor_path):
                with open(sensor_path) as f:
                    temp = int(f.read()) / 1000
                    break

        try:
            vram_used = int(open("/sys/class/drm/card1/device/mem_info_vram_used").read().strip())
            gtt_used  = int(open("/sys/class/drm/card1/device/mem_info_gtt_used").read().strip())
            used_gb = round((vram_used + gtt_used) / (1024**3), 2)
        except Exception as e:
            logger.warning(f"VRAM sysfs parse error: {e}")
    except Exception as e:
        logger.warning(f"GPU stats error: {e}")
    return clock, temp, used_gb

# ---------------- CPU Frequency (MHz) ----------------
def get_cpu_freq():
    """Compute average CPU frequency across all cores."""
    try:
        freqs = []
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "cpu MHz" in line:
                    freqs.append(float(line.strip().split(":")[1]))
        if freqs:
            return int(sum(freqs) / len(freqs))
        return 0
    except Exception:
        return 0

# ---------------- CPU Temperature (°C) ----------------
def get_cpu_temp_from_sensors():
    """Read CPU temperature from k10temp hwmon driver."""
    try:
        hwmon_path = "/sys/class/hwmon"
        for hw in os.listdir(hwmon_path):
            name_path = os.path.join(hwmon_path, hw, "name")
            if os.path.exists(name_path):
                with open(name_path) as f:
                    name = f.read().strip().lower()
                if "k10temp" in name:
                    temp_path = os.path.join(hwmon_path, hw, "temp1_input")
                    if os.path.exists(temp_path):
                        return int(open(temp_path).read().strip()) // 1000
    except Exception as e:
        logger.warning(f"CPU temp read error: {e}")
    return 0

# ---------------- CPU Load (%) ----------------
def get_cpu_load():
    """Compute CPU usage percentage from /proc/stat."""
    try:
        with open("/proc/stat") as f:
            cpu_line = f.readline()
        fields = [float(x) for x in cpu_line.strip().split()[1:]]
        idle_time = fields[3] + fields[4]
        total_time = sum(fields)
        time.sleep(0.1)
        with open("/proc/stat") as f:
            cpu_line2 = f.readline()
        fields2 = [float(x) for x in cpu_line2.strip().split()[1:]]
        idle_delta = (fields2[3] + fields2[4]) - idle_time
        total_delta = sum(fields2) - total_time
        if total_delta > 0:
            return int(round(100.0 * (1.0 - idle_delta / total_delta), 0))
    except Exception as e:
        logger.warning(f"CPU load error: {e}")
    return 0

# ---------------- RAM Usage (GB) ----------------
def get_ram_usage():
    """Return used and total RAM in GB."""
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        mem_total = int([x for x in lines if "MemTotal" in x][0].split()[1]) / 1024 / 1024
        mem_free = int([x for x in lines if "MemAvailable" in x][0].split()[1]) / 1024 / 1024
        used = mem_total - mem_free
        return round(used, 1), round(mem_total, 1)
    except Exception as e:
        logger.warning(f"RAM usage error: {e}")
        return 0.0, 0.0

# ---------------- APU Power (W) ----------------
def get_power_usage():
    """Read APU package power in Watts from lm-sensors."""
    try:
        output = subprocess.check_output(["sensors"], text=True)
        for line in output.splitlines():
            if "PPT" in line or "Package Power" in line:
                parts = line.split()
                for i, val in enumerate(parts):
                    if val.endswith("W"):
                        try:
                            return float(parts[i - 1])
                        except (ValueError, IndexError):
                            continue
    except Exception as e:
        logger.warning(f"PPT reading error: {e}")
    return 0.0

# ---------------- APU Voltage (mV) ----------------
def get_voltage_mV():
    """Read APU core voltage in millivolts."""

    try:
        hwmon_path = "/sys/class/hwmon"
        for hw in os.listdir(hwmon_path):
            name_file = os.path.join(hwmon_path, hw, "name")
            if os.path.exists(name_file):
                with open(name_file) as f:
                    name = f.read().strip().lower()
                if "amdgpu" in name:
                    # Usually vddgfx (core voltage) is in in0_input
                    volt_path = os.path.join(hwmon_path, hw, "in0_input")
                    if os.path.exists(volt_path):
                        return int(open(volt_path).read().strip())
    except Exception as e:
        logger.warning(f"APU voltage read error: {e}")
    return None

# ---------------- System Fan Speed (RPM) ----------------
def get_fan_rpm():
    """Read system fan speed in RPM (BC‑250, nct* hwmon, fan2_input)."""
    try:
        hwmon_path = "/sys/class/hwmon"
        for hw in os.listdir(hwmon_path):
            name_file = os.path.join(hwmon_path, hw, "name")
            if os.path.exists(name_file):
                with open(name_file) as f:
                    if "nct" in f.read().strip().lower():
                        fan_path = os.path.join(hwmon_path, hw, "fan2_input")
                        if os.path.exists(fan_path):
                            return int(open(fan_path).read().strip())
    except Exception as e:
        logger.warning(f"Fan RPM read error: {e}")
    return None

# ---------------- System NVMe SSD Temperature (°C) ----------------
def get_nvme_temp():
    """Read internal M.2 NVMe SSD temperature."""
    try:
        hwmon_path = "/sys/class/hwmon"
        for hw in os.listdir(hwmon_path):
            name_path = os.path.join(hwmon_path, hw, "name")
            if os.path.exists(name_path):
                with open(name_path) as f:
                    name = f.read().strip().lower()
                if "nvme" in name:
                    temp_path = os.path.join(hwmon_path, hw, "temp1_input")
                    if os.path.exists(temp_path):
                        return int(open(temp_path).read().strip()) // 1000
    except Exception as e:
        logger.warning(f"NVMe temp read error: {e}")
    return 0

# =========================================================
# Disk Bandwidth Monitoring
# =========================================================
disk_prev = None
disk_prev_time = time.time()

# ---------------- Total Disk Read/Write (MB/s) ----------------
def get_total_disk_rw():
    """Return read/write speed in MB/s for all disks."""
    global disk_prev, disk_prev_time
    now_time = time.time()
    interval = now_time - disk_prev_time
    disk_prev_time = now_time

    total_read = total_write = 0
    if disk_prev is None:
        disk_prev = {}

    for dev in os.listdir("/sys/block/"):
        if dev.startswith(("loop", "ram")):
            continue
        stat_path = f"/sys/block/{dev}/stat"
        if not os.path.exists(stat_path):
            continue
        try:
            with open(stat_path) as f:
                fields = list(map(int, f.read().strip().split()))
            read_bytes = fields[2] * 512
            write_bytes = fields[6] * 512
        except Exception:
            continue

        prev = disk_prev.get(dev, (read_bytes, write_bytes))
        total_read += max(read_bytes - prev[0], 0)
        total_write += max(write_bytes - prev[1], 0)
        disk_prev[dev] = (read_bytes, write_bytes)

    if interval == 0:
        return 0.0, 0.0
    return total_read / interval / 1024 / 1024, total_write / interval / 1024 / 1024

# =========================================================
# Network Speed Monitoring
# =========================================================
net_prev = copy.deepcopy(psutil.net_io_counters(pernic=True))
prev_time = time.time()

# ---------------- Auto Detect Network Interface ----------------
def auto_detect_interface():
    """Return first active physical network interface."""
    virtual_prefixes = ("uap", "virbr", "tap", "docker", "veth")
    counters = psutil.net_io_counters(pernic=True)
    best_iface = None
    max_bytes = 0

    for iface, stats in counters.items():
        if iface == "lo" or iface.startswith(virtual_prefixes):
            continue
        total_bytes = stats.bytes_recv + stats.bytes_sent
        if total_bytes > max_bytes:
            max_bytes = total_bytes
            best_iface = iface

    if best_iface:
        return best_iface
    # fallback to any non-virtual interface
    for iface in counters.keys():
        if not iface.startswith(virtual_prefixes) and iface != "lo":
            return iface
    return list(counters.keys())[0]

# ---------------- Get Network Speed (Mbps) ----------------
def get_network_speed(interface):
    """Return network Rx/Tx speed in Mbps for given interface."""
    global net_prev, prev_time
    counters = psutil.net_io_counters(pernic=True)
    now_time = time.time()
    interval = now_time - prev_time
    prev_time = now_time

    if interface not in counters or interface not in net_prev:
        net_prev = copy.deepcopy(counters)
        return 0.0, 0.0

    rx_bytes = counters[interface].bytes_recv - net_prev[interface].bytes_recv
    tx_bytes = counters[interface].bytes_sent - net_prev[interface].bytes_sent
    net_prev = copy.deepcopy(counters)

    rx_mbps = max(rx_bytes * 8 / 1024 / 1024 / interval, 0)
    tx_mbps = max(tx_bytes * 8 / 1024 / 1024 / interval, 0)
    return rx_mbps, tx_mbps

# =========================================================
# Main Display Loop
# =========================================================
if __name__ == "__main__":
    lcd_comm = LcdCommRevA(com_port=COM_PORT, display_width=WIDTH, display_height=HEIGHT)
    lcd_comm.Reset()
    lcd_comm.InitializeComm()
    lcd_comm.SetBrightness(level=20)
    lcd_comm.SetBackplateLedColor(led_color=(255, 255, 255))
    lcd_comm.SetOrientation(orientation=Orientation.PORTRAIT)

    background = f"res/backgrounds/example_{lcd_comm.get_width()}x{lcd_comm.get_height()}.png"
    lcd_comm.DisplayBitmap(background)

    interface_name = auto_detect_interface()

    while True:
        now = datetime.now().strftime("%m/%d/%Y   %I:%M %p")
        gpu_load = get_gpu_load()
        gpu_clock, gpu_temp, vram_used = get_gpu_stats()
        cpu_clock = get_cpu_freq()
        cpu_temp = get_cpu_temp_from_sensors()
        cpu_load = get_cpu_load()
        ram_used, ram_total = get_ram_usage()
        ppt_watts = get_power_usage()
        voltage_mV = get_voltage_mV()
        fan_rpm = get_fan_rpm()
        nvme_temp = get_nvme_temp()
        rx_speed, tx_speed = get_network_speed(interface_name)
        disk_read, disk_write = get_total_disk_rw()

        text = (
            f"   {now}\n"
            f"         AMD BC-250\n"
            f"{'RDNA2:':<6}{int(gpu_load):>3}% {int(gpu_clock):>5} {'MHz':<4}{int(gpu_temp):>3} {'°C':<4}\n"
            f"{'Zen2:':<6}{int(cpu_load):>3}% {int(cpu_clock):>5} {'MHz':<4}{int(cpu_temp):>3} {'°C':<2}\n\n"
            f"         16GB GDDR6\n"
            f"{'VRAM:':<7}{vram_used:>5.1f} {'GB':<4}\n"
            f"{'RAM:':<7}{ram_used:>5.1f} {'GB':<4}\n\n"
            f"          Metrics\n"
            f"{'APU Power:':<14}{ppt_watts:>6} {'W':<4}\n"
            f"{'APU mV:':<16}{voltage_mV:>4} {'mV':<4}\n"
            f"{'APU Fan:':<16}{fan_rpm:>4} {'RPM':<4}\n"
            f"{'NVMe Temp:':<16}{nvme_temp:>4} {'°C':<4}\n"
            f"{'Disk Read:':<16}{disk_read:>5.1f} {'MB/s↓':<8}\n"
            f"{'Disk Write:':<16}{disk_write:>5.1f} {'MB/s↑':<8}\n"
            f"{'Net Mbps:':<13}{rx_speed:>4.1f} {'↓':<3}{tx_speed:>4.1f} {'↑':<2}"
        )

        lcd_comm.DisplayText(
            text,
            10,
            10,
            font="res/fonts/jetbrains-mono/JetBrainsMono-ExtraBold.ttf",
            font_size=18,
            font_color=(220, 220, 255),
            background_image=background
        )
        time.sleep(0.5)