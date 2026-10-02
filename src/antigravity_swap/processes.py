"""Running agy sessions on this machine: where each one runs and which account it is on."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from antigravity_swap.paths import IS_MACOS, IS_WINDOWS

PRINT_FLAGS = ("-p", "--print", "--prompt")
HELPER_SUBCOMMANDS = {"models", "mcp", "plugin", "plugins", "agent", "agents", "changelog", "help",
                      "install", "update", "mic-serve", "remote-control"}


@dataclass
class Instance:
    pid: int
    argv: list[str]
    cwd: str | None
    started: float | None

    @property
    def gemini_dir(self) -> Path | None:
        for i, a in enumerate(self.argv):
            if a.startswith("--gemini_dir="):
                return Path(a.split("=", 1)[1])
            if a == "--gemini_dir" and i + 1 < len(self.argv):
                return Path(self.argv[i + 1])
        return None

    @property
    def mode(self) -> str:
        args = self.argv[1:]
        if any(a in PRINT_FLAGS or a.startswith(("--print=", "--prompt=")) for a in args):
            return "print"
        return "TUI"

    @property
    def is_helper(self) -> bool:
        """Short-lived subcommands (agy models, agy mcp ...), not sessions."""
        for a in self.argv[1:]:
            if a.startswith("-"):
                continue
            return a in HELPER_SUBCOMMANDS
        return False


def _is_agy(argv0: str) -> bool:
    name = Path(argv0.replace("\\", "/")).name.lower()
    return name in ("agy", "agy.exe")


def list_instances() -> list[Instance]:
    try:
        if IS_WINDOWS:
            found = _windows()
        elif IS_MACOS:
            found = _macos()
        else:
            found = _linux()
    except Exception:
        return []
    me = os.getpid()
    return [i for i in found if i.pid != me and not i.is_helper]


# --- Linux -------------------------------------------------------------------------------


def _linux() -> list[Instance]:
    out = []
    boot = None
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                boot = float(line.split()[1])
    except OSError:
        pass
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            argv = [a.decode(errors="replace") for a in (p / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        if not argv or not _is_agy(argv[0]):
            continue
        try:
            cwd = os.readlink(p / "cwd")
        except OSError:
            cwd = None
        started = None
        try:
            stat = (p / "stat").read_text()
            fields = stat[stat.rindex(")") + 2:].split()
            if boot is not None:
                started = boot + int(fields[19]) / hz
        except (OSError, ValueError, IndexError):
            pass
        out.append(Instance(int(p.name), argv, cwd, started))
    return out


# --- macOS -------------------------------------------------------------------------------


def _macos() -> list[Instance]:
    r = subprocess.run(["ps", "-axo", "pid=,etime=,command="], capture_output=True, text=True, timeout=10)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        argv = parts[2].split()
        if not argv or not _is_agy(argv[0]):
            continue
        pid = int(parts[0])
        cwd = None
        try:
            lr = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                capture_output=True, text=True, timeout=5)
            cwd = next((ln[1:] for ln in lr.stdout.splitlines() if ln.startswith("n")), None)
        except (OSError, subprocess.SubprocessError):
            pass
        out.append(Instance(pid, argv, cwd, time.time() - _etime(parts[1])))
    return out


def _etime(s: str) -> float:
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    nums = [int(x) for x in s.split(":")]
    while len(nums) < 3:
        nums.insert(0, 0)
    h, m, sec = nums
    return days * 86400 + h * 3600 + m * 60 + sec


# --- Windows -----------------------------------------------------------------------------


def _windows() -> list[Instance]:
    r = subprocess.run(["tasklist", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq agy.exe"],
                       capture_output=True, text=True, timeout=10)
    out = []
    for line in r.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == "agy.exe" and parts[1].isdigit():
            pid = int(parts[1])
            cmdline, cwd, started = _win_peb(pid)
            argv = _win_split(cmdline) if cmdline else ["agy.exe"]
            out.append(Instance(pid, argv, cwd, started))
    return out


def _win_peb(pid: int):
    """Command line, current directory and start time of another process, read from its PEB."""
    import ctypes
    import ctypes.wintypes as w

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    k32.OpenProcess.restype = w.HANDLE
    k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k32.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_size_t)]
    k32.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    k32.CloseHandle.argtypes = [w.HANDLE]

    class PBI(ctypes.Structure):
        _fields_ = [("Reserved1", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                    ("Reserved2", ctypes.c_void_p * 2), ("UniqueProcessId", ctypes.c_void_p),
                    ("Reserved3", ctypes.c_void_p)]

    h = k32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFORMATION | VM_READ
    if not h:
        return None, None, None
    try:
        started = None
        ft = [w.FILETIME() for _ in range(4)]
        if k32.GetProcessTimes(h, *[ctypes.byref(f) for f in ft]):
            v = (ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime
            started = (v - 116444736000000000) / 1e7

        def read(addr, size):
            buf = ctypes.create_string_buffer(size)
            n = ctypes.c_size_t()
            if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
                raise OSError("ReadProcessMemory failed")
            return buf.raw

        def ustr(addr):
            raw = read(addr, 16)  # UNICODE_STRING on x64
            length = int.from_bytes(raw[0:2], "little")
            ptr = int.from_bytes(raw[8:16], "little")
            return read(ptr, length).decode("utf-16-le") if length and ptr else ""

        pbi = PBI()
        if ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None) != 0:
            return None, None, started
        params = int.from_bytes(read(pbi.PebBaseAddress + 0x20, 8), "little")
        cwd = ustr(params + 0x38).rstrip("\\") or None
        cmdline = ustr(params + 0x70) or None
        return cmdline, cwd, started
    except (OSError, ValueError):
        return None, None, None
    finally:
        k32.CloseHandle(h)


def _win_split(cmdline: str) -> list[str]:
    import ctypes
    import ctypes.wintypes as w

    shell32 = ctypes.WinDLL("shell32")
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(w.LPWSTR)
    shell32.CommandLineToArgvW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    n = ctypes.c_int()
    arr = shell32.CommandLineToArgvW(cmdline, ctypes.byref(n))
    if not arr:
        return cmdline.split()
    try:
        return [arr[i] for i in range(n.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(arr)
