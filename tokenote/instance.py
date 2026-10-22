from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any

WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 0x102
WAIT_FAILED = 0xFFFFFFFF
GA_ROOT = 2
SW_RESTORE = 9


class SingleInstance:
    """Windows process lock and activation signal for one normalized database path.

    The owning main thread holds the mutex for the process lifetime. Both handles
    are released by Windows if the process dies; no persistent lock file is used.
    GUI activation is polled on Tk's own thread, including requests during startup.
    """

    def __init__(self, db_path: Path):
        identity = os.path.normcase(str(Path(db_path).expanduser().resolve()))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self.name = f"Local\\Tokenote.{digest}"
        self.window_property = f"Tokenote.Instance.{digest}"
        self.acquired = False
        self._mutex = None
        self._event = None
        self._app: Any = None
        self._after_id = None
        self._window = None
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._configure_api()
        try:
            # Create the event before obtaining the mutex so a rapid second
            # launch can signal the owner even before its GUI has been built.
            self._event = self._kernel.CreateEventW(None, False, False, self.name + ".activate")
            if not self._event:
                raise ctypes.WinError(ctypes.get_last_error())
            self._mutex = self._kernel.CreateMutexW(None, False, self.name)
            if not self._mutex:
                raise ctypes.WinError(ctypes.get_last_error())
            result = self._kernel.WaitForSingleObject(self._mutex, 0)
            if result == WAIT_FAILED:
                raise ctypes.WinError(ctypes.get_last_error())
            if result not in (WAIT_OBJECT_0, WAIT_ABANDONED, WAIT_TIMEOUT):
                raise OSError(f"Unexpected instance lock result: {result}")
            self.acquired = result in (WAIT_OBJECT_0, WAIT_ABANDONED)
        except BaseException:
            self.close()
            raise

    def _configure_api(self) -> None:
        for name, args, result in (
            ("CreateMutexW", (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR), wintypes.HANDLE),
            ("CreateEventW", (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR), wintypes.HANDLE),
            ("WaitForSingleObject", (wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD),
            ("ReleaseMutex", (wintypes.HANDLE,), wintypes.BOOL),
            ("SetEvent", (wintypes.HANDLE,), wintypes.BOOL),
            ("CloseHandle", (wintypes.HANDLE,), wintypes.BOOL),
        ):
            function = getattr(self._kernel, name)
            function.argtypes, function.restype = args, result
        self._enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        for name, args, result in (
            ("GetAncestor", (wintypes.HWND, wintypes.UINT), wintypes.HWND),
            ("SetPropW", (wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE), wintypes.BOOL),
            ("GetPropW", (wintypes.HWND, wintypes.LPCWSTR), wintypes.HANDLE),
            ("RemovePropW", (wintypes.HWND, wintypes.LPCWSTR), wintypes.HANDLE),
            ("EnumWindows", (self._enum_callback, wintypes.LPARAM), wintypes.BOOL),
            ("GetWindowThreadProcessId", (wintypes.HWND, ctypes.POINTER(wintypes.DWORD)), wintypes.DWORD),
            ("AllowSetForegroundWindow", (wintypes.DWORD,), wintypes.BOOL),
            ("IsIconic", (wintypes.HWND,), wintypes.BOOL),
            ("ShowWindow", (wintypes.HWND, ctypes.c_int), wintypes.BOOL),
            ("SetForegroundWindow", (wintypes.HWND,), wintypes.BOOL),
        ):
            function = getattr(self._user, name)
            function.argtypes, function.restype = args, result

    def request_activation(self) -> None:
        """Ask the owner to restore/raise its existing window, without opening Tk."""
        @self._enum_callback
        def find_window(hwnd, parameter):
            if self._user.GetPropW(hwnd, self.window_property):
                pid = wintypes.DWORD()
                self._user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                self._user.AllowSetForegroundWindow(pid.value)
                return False
            return True

        self._user.EnumWindows(find_window, 0)
        if not self._kernel.SetEvent(self._event):
            raise ctypes.WinError(ctypes.get_last_error())

    def watch(self, app) -> None:
        if not self.acquired:
            raise RuntimeError("Only the primary instance may register a window")
        app.update_idletasks()
        self._window = self._user.GetAncestor(app.winfo_id(), GA_ROOT)
        if not self._window or not self._user.SetPropW(self._window, self.window_property, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        self._app = app
        self._poll()

    def _poll(self) -> None:
        if self._app is None:
            return
        if self._kernel.WaitForSingleObject(self._event, 0) == WAIT_OBJECT_0:
            self._activate_window()
        self._after_id = self._app.after(120, self._poll)

    def _activate_window(self) -> None:
        if self._user.IsIconic(self._window):
            self._user.ShowWindow(self._window, SW_RESTORE)
        elif self._app.state() == "withdrawn":
            self._app.deiconify()
        self._app.lift()
        self._user.SetForegroundWindow(self._window)
        self._app.focus_force()

    def unwatch(self) -> None:
        if self._app is not None and self._after_id is not None:
            from tkinter import TclError
            try:
                self._app.after_cancel(self._after_id)
            except TclError:
                pass
        if self._window:
            self._user.RemovePropW(self._window, self.window_property)
        self._app = self._after_id = self._window = None

    def close(self) -> None:
        self.unwatch()
        if self._mutex:
            if self.acquired:
                self._kernel.ReleaseMutex(self._mutex)
            self._kernel.CloseHandle(self._mutex)
            self._mutex = None
        if self._event:
            self._kernel.CloseHandle(self._event)
            self._event = None
        self.acquired = False

    def __enter__(self) -> SingleInstance:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
