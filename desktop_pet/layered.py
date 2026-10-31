"""Per-pixel transparent, always-on-top Win32 window for the pet.

Tkinter cannot make a truly transparent window on Windows, so the character is
drawn into a 32-bit premultiplied DIB and presented with
``UpdateLayeredWindow``.  Windows hit-tests layered windows by alpha, and an
explicit ``WM_NCHITTEST`` handler makes fully transparent pixels click-through.

The Win32 message loop runs in its own thread.  Tkinter owns the control panel
in the main thread and communicates through a queue.
"""
from __future__ import annotations

import ctypes
import logging
import os
import queue
import threading
import time
from ctypes import wintypes
from typing import Callable

import numpy as np
from PIL import Image

log = logging.getLogger("othvm.pet.layered")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# ------------------------------------------------------------------ constants
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000
CS_DBLCLKS = 0x0008

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_NCHITTEST = 0x0084
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_MOUSEWHEEL = 0x020A
WM_HOTKEY = 0x0312
WM_APP = 0x8000
WM_APP_SHOW = WM_APP + 1
WM_APP_MOVE = WM_APP + 2
WM_APP_TOPMOST = WM_APP + 3

HTTRANSPARENT = -1
HTCLIENT = 1
SPI_GETWORKAREA = 0x0030
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
VK_SPACE = 0x20
VK_O = 0x4F
VK_H = 0x48
IDC_HAND = 32649

# ------------------------------------------------------------------ types
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


# ------------------------------------------------------------------ prototypes
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetMessagePos.argtypes = []
user32.GetMessagePos.restype = wintypes.DWORD
user32.SetCapture.argtypes = [wintypes.HWND]
user32.ReleaseCapture.argtypes = []
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                  wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT,
                            wintypes.LPVOID]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
    ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                         wintypes.LPVOID, wintypes.UINT]
user32.SetProcessDPIAware.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
user32.LoadCursorW.restype = wintypes.HANDLE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(wintypes.LPVOID), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


def enable_dpi_awareness() -> None:
    """Opt into per-monitor DPI awareness.

    Must run before Tkinter creates its first window, otherwise Windows may
    scale the control panel and the layered pet differently.
    """
    try:
        # PER_MONITOR_AWARE_V2 = -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


# Backwards-compatible private alias.
_dpi_aware = enable_dpi_awareness


def _work_area() -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _premultiplied_bgra(image: Image.Image) -> bytes:
    arr = np.asarray(image.convert("RGBA"), dtype=np.uint16)
    alpha = arr[:, :, 3:4]
    rgb = (arr[:, :, :3] * alpha // 255).astype(np.uint8)
    bgra = np.dstack([
        rgb[:, :, 2], rgb[:, :, 1], rgb[:, :, 0],
        arr[:, :, 3].astype(np.uint8),
    ])
    return np.ascontiguousarray(bgra).tobytes()


class LayeredPet:
    """Owns one layered window and its Win32 message loop."""

    def __init__(self, renderer: Callable[[], Image.Image], width: int, height: int,
                 x: int | None = None, y: int | None = None,
                 always_on_top: bool = True,
                 event_queue: queue.Queue | None = None,
                 frame_ms: int = 40):
        self._renderer = renderer
        self.width = int(width)
        self.height = int(height)
        self.frame_ms = max(16, min(int(frame_ms), 200))
        self.always_on_top = bool(always_on_top)
        self.events = event_queue or queue.Queue()
        left, top, right, bottom = _work_area()
        default_x = right - self.width - 24
        default_y = bottom - self.height - 24
        self.x = int(x if x is not None else default_x)
        self.y = int(y if y is not None else default_y)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd: int | None = None
        self._class_name = f"OTHVM_Pet_{os.getpid()}_{id(self)}"
        self._wndproc_ref = WNDPROC(self._wndproc)
        self._visible = True
        self._dragging = False
        self._drag_start = (0, 0)
        self._drag_origin = (self.x, self.y)
        self._alpha: np.ndarray | None = None
        self._screen_dc = None
        self._mem_dc = None
        self._bitmap = None
        self._bits = None
        self._error: Exception | None = None

    # ------------------------------------------------------------------ public
    @property
    def hwnd(self) -> int | None:
        return self._hwnd

    @property
    def position(self) -> tuple[int, int]:
        with self._lock:
            return self.x, self.y

    def start(self, timeout: float = 8.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="othvm-pet-window",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"pet window did not start: {self._error}")
        if self._error:
            raise self._error

    def stop(self) -> None:
        hwnd = self._hwnd
        if hwnd:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(timeout=3)

    def set_visible(self, visible: bool) -> None:
        self._visible = bool(visible)
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_APP_SHOW,
                                1 if visible else 0, 0)

    def move(self, x: int, y: int) -> None:
        with self._lock:
            self.x, self.y = int(x), int(y)
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_APP_MOVE, 0, 0)

    def set_always_on_top(self, enabled: bool) -> None:
        self.always_on_top = bool(enabled)
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_APP_TOPMOST,
                                1 if enabled else 0, 0)

    def render_now(self) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_TIMER, 1, 0)

    # ------------------------------------------------------------------ window
    def _run(self) -> None:
        try:
            _dpi_aware()
            hinstance = kernel32.GetModuleHandleW(None)
            cursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))
            wc = WNDCLASSW()
            wc.style = CS_DBLCLKS
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinstance
            wc.hCursor = cursor
            wc.lpszClassName = self._class_name
            atom = user32.RegisterClassW(ctypes.byref(wc))
            if not atom:
                err = kernel32.GetLastError()
                if err != 1410:  # class already exists
                    raise ctypes.WinError(err)
            ex_style = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            if self.always_on_top:
                ex_style |= WS_EX_TOPMOST
            hwnd = user32.CreateWindowExW(
                ex_style, self._class_name, "OTHVM Desktop Pet", WS_POPUP,
                self.x, self.y, self.width, self.height,
                None, None, hinstance, None)
            if not hwnd:
                raise ctypes.WinError(kernel32.GetLastError())
            self._hwnd = hwnd
            if self._visible:
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            user32.SetWindowPos(
                hwnd, HWND_TOPMOST if self.always_on_top else HWND_NOTOPMOST,
                self.x, self.y, self.width, self.height,
                SWP_NOACTIVATE | (SWP_SHOWWINDOW if self._visible else 0))
            for hotkey_id, vk, label in (
                (1, VK_SPACE, "Ctrl+Alt+Space"),
                (2, VK_O, "Ctrl+Alt+O"),
                (3, VK_H, "Ctrl+Alt+H"),
            ):
                if not user32.RegisterHotKey(hwnd, hotkey_id,
                                             MOD_CONTROL | MOD_ALT, vk):
                    log.warning("hotkey %s could not be registered", label)
            user32.SetTimer(hwnd, 1, self.frame_ms, None)
            self._create_dib()
            self._ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # noqa: BLE001
            self._error = exc
            log.exception("layered window failed")
            self._ready.set()
        finally:
            self._cleanup_dib()
            self._hwnd = None

    def _create_dib(self) -> None:
        self._screen_dc = user32.GetDC(None)
        self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.width
        bmi.bmiHeader.biHeight = -self.height  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        bits = wintypes.LPVOID()
        self._bitmap = gdi32.CreateDIBSection(
            self._mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not self._bitmap:
            raise ctypes.WinError(kernel32.GetLastError())
        gdi32.SelectObject(self._mem_dc, self._bitmap)
        self._bits = bits

    def _cleanup_dib(self) -> None:
        try:
            if self._mem_dc:
                gdi32.DeleteDC(self._mem_dc)
            if self._screen_dc:
                user32.ReleaseDC(None, self._screen_dc)
            if self._bitmap:
                gdi32.DeleteObject(self._bitmap)
        except Exception:
            pass
        self._mem_dc = None
        self._screen_dc = None
        self._bitmap = None
        self._bits = None

    def _paint(self) -> None:
        if not self._hwnd or not self._bits:
            return
        try:
            image = self._renderer()
        except Exception:
            log.exception("renderer failed")
            return
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height), Image.BILINEAR)
        alpha = np.asarray(image.convert("RGBA"))[:, :, 3].copy()
        self._alpha = alpha
        data = _premultiplied_bgra(image)
        target = getattr(self._bits, "value", self._bits)
        ctypes.memmove(target, data, len(data))
        with self._lock:
            x, y = self.x, self.y
        pt_dst = wintypes.POINT(x, y)
        pt_src = wintypes.POINT(0, 0)
        size = wintypes.SIZE(self.width, self.height)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(
            self._hwnd, self._screen_dc, ctypes.byref(pt_dst),
            ctypes.byref(size), self._mem_dc, ctypes.byref(pt_src),
            0, ctypes.byref(blend), ULW_ALPHA)

    # ------------------------------------------------------------------ wndproc
    def _wndproc(self, hwnd, msg, wparam, lparam):  # noqa: ANN001
        try:
            if msg == WM_NCHITTEST:
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                cx, cy = x - rect.left, y - rect.top
                alpha = self._alpha
                if alpha is not None and 0 <= cy < alpha.shape[0] and 0 <= cx < alpha.shape[1]:
                    if int(alpha[cy, cx]) < 8:
                        return HTTRANSPARENT
                return HTCLIENT
            if msg == WM_TIMER:
                if self._visible:
                    self._paint()
                return 0
            if msg == WM_LBUTTONDOWN:
                self._dragging = True
                # Use the position associated with the message, not the
                # current pointer: the queue may lag behind a moving mouse.
                self._drag_start = self._message_pos()
                with self._lock:
                    self._drag_origin = (self.x, self.y)
                log.debug("drag start at %s window=%s", self._drag_start,
                          self._drag_origin)
                user32.SetCapture(hwnd)
                return 0
            if msg == WM_MOUSEMOVE and self._dragging:
                cx, cy = self._message_pos()
                dx = cx - self._drag_start[0]
                dy = cy - self._drag_start[1]
                with self._lock:
                    self.x = self._drag_origin[0] + dx
                    self.y = self._drag_origin[1] + dy
                    x, y = self.x, self.y
                user32.SetWindowPos(hwnd, None, x, y, 0, 0,
                                    SWP_NOSIZE | SWP_NOACTIVATE)
                return 0
            if msg == WM_LBUTTONUP:
                if self._dragging:
                    cx, cy = self._message_pos()
                    moved = abs(cx - self._drag_start[0]) + abs(cy - self._drag_start[1])
                    self._dragging = False
                    user32.ReleaseCapture()
                    if moved < 5:
                        self.events.put(("click", None))
                    else:
                        with self._lock:
                            position = (self.x, self.y)
                        log.debug("drag end at %s window=%s delta=%s",
                                  (cx, cy), position,
                                  (position[0] - self._drag_origin[0],
                                   position[1] - self._drag_origin[1]))
                        self.events.put(("moved", position))
                return 0
            if msg == WM_LBUTTONDBLCLK:
                self._dragging = False
                self.events.put(("double_click", None))
                return 0
            if msg == WM_RBUTTONUP:
                self.events.put(("right_click", self._message_pos()))
                return 0
            if msg == WM_MOUSEWHEEL:
                delta = ctypes.c_short((wparam >> 16) & 0xFFFF).value
                self.events.put(("wheel", delta))
                return 0
            if msg == WM_HOTKEY:
                self.events.put(("hotkey", int(wparam)))
                return 0
            if msg == WM_APP_SHOW:
                self._visible = bool(wparam)
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE if self._visible else SW_HIDE)
                if self._visible:
                    self._paint()
                return 0
            if msg == WM_APP_MOVE:
                with self._lock:
                    x, y = self.x, self.y
                user32.SetWindowPos(hwnd, None, x, y, 0, 0,
                                    SWP_NOSIZE | SWP_NOACTIVATE)
                return 0
            if msg == WM_APP_TOPMOST:
                flag = HWND_TOPMOST if wparam else HWND_NOTOPMOST
                user32.SetWindowPos(hwnd, flag, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.KillTimer(hwnd, 1)
                for hotkey in (1, 2, 3):
                    user32.UnregisterHotKey(hwnd, hotkey)
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("wndproc error")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    @staticmethod
    def _cursor() -> tuple[int, int]:
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    @staticmethod
    def _message_pos() -> tuple[int, int]:
        """Screen position associated with the message being processed."""
        value = int(user32.GetMessagePos())
        x = ctypes.c_short(value & 0xFFFF).value
        y = ctypes.c_short((value >> 16) & 0xFFFF).value
        return x, y
