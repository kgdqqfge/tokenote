from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime
import json
import os
import sqlite3
import struct
import tempfile
import time
from pathlib import Path

PAGE = 4096
RESERVE = 80
CHUNK = 8 * 1024 * 1024
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    Cipher = algorithms = modes = None


def has_cryptography() -> bool:
    return Cipher is not None


def _cipher():
    if Cipher is None:
        raise RuntimeError("缺少 cryptography，无法读取 Trae 用量")
    return Cipher, algorithms, modes


def default_db_path() -> Path:
    appdata = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    return Path(appdata) / "Trae CN" / "ModularData" / "ai-agent" / "database.db"


class _MemoryInfo(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


_API = None


def _api():
    global _API
    if _API is not None:
        return _API
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.CloseHandle.restype = wt.BOOL
    kernel.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype = wt.BOOL
    kernel.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                      ctypes.POINTER(_MemoryInfo), ctypes.c_size_t]
    kernel.VirtualQueryEx.restype = ctypes.c_size_t
    kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                                  ctypes.POINTER(wt.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wt.BOOL

    psapi.EnumProcesses.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD,
                                    ctypes.POINTER(wt.DWORD)]
    psapi.EnumProcesses.restype = wt.BOOL
    psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(wt.HMODULE), wt.DWORD,
                                           ctypes.POINTER(wt.DWORD), wt.DWORD]
    psapi.EnumProcessModulesEx.restype = wt.BOOL
    psapi.GetModuleFileNameExW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]
    psapi.GetModuleFileNameExW.restype = wt.DWORD
    _API = kernel, psapi
    return _API


def find_trae_pid() -> int | None:
    kernel, psapi = _api()
    processes = (wt.DWORD * 4096)()
    needed = wt.DWORD()
    if not psapi.EnumProcesses(processes, ctypes.sizeof(processes), ctypes.byref(needed)):
        return None
    for index in range(needed.value // ctypes.sizeof(wt.DWORD)):
        pid = processes[index]
        if not pid:
            continue
        handle = kernel.OpenProcess(0x0400, False, pid)
        if not handle:
            continue
        try:
            name = ctypes.create_unicode_buffer(260)
            if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(wt.DWORD(260))):
                continue
            if not name.value.lower().endswith("trae cn.exe"):
                continue
            module_handle = kernel.OpenProcess(0x0410, False, pid)
            if not module_handle:
                continue
            try:
                modules = (wt.HMODULE * 2048)()
                size = wt.DWORD()
                if not psapi.EnumProcessModulesEx(module_handle, modules, ctypes.sizeof(modules),
                                                  ctypes.byref(size), 0x03):
                    continue
                for module_index in range(size.value // ctypes.sizeof(wt.HMODULE)):
                    module_name = ctypes.create_unicode_buffer(260)
                    psapi.GetModuleFileNameExW(module_handle, modules[module_index], module_name, 260)
                    if module_name.value.lower().endswith("ai_agent.dll"):
                        return pid
            finally:
                kernel.CloseHandle(module_handle)
        finally:
            kernel.CloseHandle(handle)
    return None


def _read(handle, address: int, size: int) -> bytes:
    if size <= 0 or not 0x10000 <= address < 0x7FFFFFFEFFFF:
        return b""
    buffer = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t()
    kernel, _ = _api()
    if kernel.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read)):
        return buffer.raw[:read.value]
    return b""


def _regions(handle):
    kernel, _ = _api()
    info = _MemoryInfo()
    address = 0
    while address < 0x7FFFFFFEFFFF:
        if not kernel.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(info),
                                     ctypes.sizeof(info)):
            break
        base = info.BaseAddress or 0
        size = info.RegionSize or 0x1000
        if (info.State == 0x1000 and info.Type in (0x20000, 0x1000000)
                and (info.Protect & 0xFF) in READABLE and not info.Protect & 0x100):
            yield base, size
        next_address = base + size
        if next_address <= address:
            break
        address = next_address


def verify_key(db_path: Path, key: bytes) -> bool:
    if len(key) != 32:
        return False
    Cipher, algorithms, modes = _cipher()
    try:
        with Path(db_path).open("rb") as handle:
            page = handle.read(PAGE)
        iv = page[PAGE - RESERVE:PAGE - RESERVE + 16]
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = decryptor.update(page[16:PAGE - RESERVE]) + decryptor.finalize()
        return plain[:2] == b"\x10\x00" and plain[2] in (1, 2) and plain[4] == RESERVE
    except (OSError, ValueError):
        return False


def _scan_memory(handle, salt: bytes, cipher_head: bytes, iv: bytes,
                 verify, timeout: float) -> bytes | None:
    deadline = time.monotonic() + timeout
    regions: list[tuple[int, int]] = []
    for base, size in _regions(handle):
        for offset in range(0, size, CHUNK):
            if time.monotonic() > deadline:
                return None
            data = _read(handle, base + offset, min(CHUNK, size - offset))
            if salt in data:
                regions.append((base, size))
                break
    if not regions:
        return None

    def matches(candidate: bytes) -> bool:
        decryptor = Cipher(algorithms.AES(candidate), modes.CBC(iv)).decryptor()
        head = decryptor.update(cipher_head) + decryptor.finalize()
        return head[:2] == b"\x10\x00" and head[2] in (1, 2) and head[4] == RESERVE

    # The key sits in heap blocks alongside a copy of the database salt.
    for alignment in (16, 8):
        for base, size in regions:
            for offset in range(0, size, CHUNK):
                if time.monotonic() > deadline:
                    return None
                data = _read(handle, base + offset, min(CHUNK + 32, size - offset))
                limit = min(len(data), CHUNK) - 32 + 1
                for index in range(0, max(0, limit), alignment):
                    candidate = data[index:index + 32]
                    if matches(candidate) and verify(candidate):
                        return candidate
    return None


def extract_key_from_memory(db_path: Path, timeout: float = 45.0) -> bytes | None:
    _cipher()
    try:
        page = Path(db_path).read_bytes()[:PAGE]
    except OSError as exc:
        raise RuntimeError(f"无法读取 Trae 数据库: {exc}") from exc
    if len(page) < PAGE:
        raise RuntimeError("Trae 数据库文件为空")
    salt = page[:16]
    iv = page[PAGE - RESERVE:PAGE - RESERVE + 16]
    cipher_head = page[16:32]
    pid = find_trae_pid()
    if pid is None:
        raise RuntimeError("未检测到运行中的 Trae CN")
    kernel, _ = _api()
    handle = kernel.OpenProcess(0x0410, False, pid)
    if not handle:
        raise RuntimeError(f"无法读取 Trae 进程（错误 {ctypes.get_last_error()}）")
    try:
        return _scan_memory(handle, salt, cipher_head, iv,
                            lambda key: verify_key(db_path, key), timeout)
    finally:
        kernel.CloseHandle(handle)


def decrypt_db(db_path: Path, key: bytes, out_path: Path) -> None:
    Cipher, algorithms, modes = _cipher()
    db_path = Path(db_path)

    def decrypt_page(page_number: int, page: bytes) -> bytes:
        decryptor = Cipher(algorithms.AES(key),
                           modes.CBC(page[PAGE - RESERVE:PAGE - RESERVE + 16])).decryptor()
        if page_number == 1:
            plain = decryptor.update(page[16:PAGE - RESERVE]) + decryptor.finalize()
            return b"SQLite format 3\x00" + plain[:PAGE - 96] + b"\x00" * RESERVE
        plain = decryptor.update(page[:PAGE - RESERVE]) + decryptor.finalize()
        return plain + b"\x00" * RESERVE

    data = db_path.read_bytes()
    pages = {index + 1: decrypt_page(index + 1, data[index * PAGE:(index + 1) * PAGE])
             for index in range(len(data) // PAGE)}

    wal_path = Path(str(db_path) + "-wal")
    if wal_path.exists():
        wal = wal_path.read_bytes()
        if len(wal) > 32:
            magic, _, page_size, _, salt1, salt2, _, _ = struct.unpack(">8I", wal[:32])
            if magic in (0x377F0682, 0x377F0683) and page_size == PAGE:
                offset, frames, last_commit = 32, [], 0
                while offset + 24 + page_size <= len(wal):
                    page_number, db_size, frame1, frame2, _, _ = struct.unpack(
                        ">6I", wal[offset:offset + 24])
                    if frame1 == salt1 and frame2 == salt2:
                        frames.append((page_number, db_size, wal[offset + 24:offset + 24 + page_size]))
                        if db_size:
                            last_commit = len(frames)
                    offset += 24 + page_size
                for page_number, _, frame in frames[:last_commit]:
                    pages[page_number] = decrypt_page(page_number, frame)

    with Path(out_path).open("wb") as handle:
        for index in range(1, max(pages) + 1):
            handle.write(pages.get(index, b"\x00" * PAGE))


def fetch_request_records(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        "SELECT extra_info, created_at FROM server_history_info "
        "WHERE extra_info LIKE '%exact_prompt_tokens_v1%'").fetchall()
    records = []
    for extra_info, created_at in rows:
        try:
            value = json.loads(extra_info)
            records.append({
                "time": datetime.datetime.fromtimestamp(created_at).isoformat(sep=" ", timespec="seconds"),
                "model": value.get("config_name"),
                "prompt_tokens": int(value.get("exact_prompt_tokens_v1") or 0),
                "output_tokens": int(value.get("exact_output_tokens_v1") or 0),
                "cache_read_tokens": int(value.get("exact_cache_read_input_tokens_v1") or 0),
                "total_tokens": int(value.get("exact_total_tokens_v1") or 0),
                "semantics": value.get("exact_token_semantics_v1"),
                "workspace": value.get("workspace_path"),
            })
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return records


def read_request_records(db_path: Path, key: bytes) -> list[dict]:
    descriptor, name = tempfile.mkstemp(prefix="tokenote-trae-", suffix=".db")
    os.close(descriptor)
    plain_path = Path(name)
    try:
        decrypt_db(db_path, key, plain_path)
        connection = sqlite3.connect(f"file:{plain_path.as_posix()}?mode=ro", uri=True)
        try:
            return fetch_request_records(connection)
        finally:
            connection.close()
    finally:
        try:
            plain_path.unlink(missing_ok=True)
        except OSError:
            pass
