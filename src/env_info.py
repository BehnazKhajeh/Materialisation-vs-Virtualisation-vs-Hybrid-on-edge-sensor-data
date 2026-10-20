"""Print a Markdown 'Environment' block to paste into the README.

Usage (project root, virtual environment active):
    python src/env_info.py
"""
import ctypes
import os
import platform
import sqlite3
import subprocess
import sys
from importlib import metadata


def version(pkg):
    try:
        return metadata.version(pkg)
    except metadata.PackageNotFoundError:
        return "not installed"


def cpu_name():
    try:
        system = platform.system()
        if system == "Windows":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
                capture_output=True, text=True, timeout=30).stdout.strip()
            if out:
                return out.splitlines()[0].strip()
        elif system == "Linux":
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        elif system == "Darwin":
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                  capture_output=True, text=True).stdout.strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def ram_gb():
    try:
        system = platform.system()
        if system == "Windows":
            class MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MemStatus()
            st.dwLength = ctypes.sizeof(MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys / 2 ** 30
        if system == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        return int(line.split()[1]) / 2 ** 20
        if system == "Darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout
            return int(out.strip()) / 2 ** 30
    except Exception:
        pass
    return None


ram = ram_gb()
print("## Environment")
print(f"- OS: {platform.system()} {platform.release()} ({platform.version()})")
print(f"- CPU: {cpu_name()} ({os.cpu_count()} logical cores)")
print(f"- RAM: {ram:.1f} GB" if ram else "- RAM: unknown")
print(f"- Python: {platform.python_version()}")
print(f"- Packages: rdflib {version('rdflib')}, owlrl {version('owlrl')}, matplotlib {version('matplotlib')}")
print(f"- SQLite library: {sqlite3.sqlite_version}")
print("- All timings come from this single machine; absolute times varied by about 2x between runs.")
