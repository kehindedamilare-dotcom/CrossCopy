"""PNG access to the Windows clipboard, using only ctypes + Pillow.

Reading:
  * the registered ``PNG`` format is preferred, so a PNG this app wrote comes
    back byte-for-byte (that is what keeps image echo suppression exact);
  * otherwise ``CF_DIB`` is converted with Pillow, which covers Paint, Snipping
    Tool, browsers and every other app that only publishes a bitmap.

Writing:
  * the ``PNG`` format is set for quality, plus ``CF_DIB`` for older apps that
    only understand bitmaps.
"""

from __future__ import annotations

import ctypes
import io
import logging
import sys
import time

logger = logging.getLogger("crosscopy.imageclip")

CF_DIB = 8
GMEM_MOVEABLE = 0x0042
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_user32 = None
_kernel32 = None


def supported() -> bool:
    """True when image clipboard access is possible on this platform."""
    if sys.platform != "win32":
        return False
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _win():
    global _user32, _kernel32
    if _user32 is not None:
        return _user32, _kernel32

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.CloseClipboard.restype = ctypes.c_int
    user32.EmptyClipboard.restype = ctypes.c_int
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
    user32.RegisterClipboardFormatW.restype = ctypes.c_uint
    user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    user32.IsClipboardFormatAvailable.restype = ctypes.c_int
    user32.GetClipboardSequenceNumber.argtypes = []
    user32.GetClipboardSequenceNumber.restype = ctypes.c_uint

    _user32, _kernel32 = user32, kernel32
    return _user32, _kernel32


def _png_format() -> int:
    user32, _ = _win()
    return user32.RegisterClipboardFormatW("PNG")


def sequence_number() -> int | None:
    """Windows-wide clipboard revision; changes on every clipboard update."""
    if not supported():
        return None
    try:
        user32, _ = _win()
        value = int(user32.GetClipboardSequenceNumber())
    except Exception:  # pragma: no cover - never fail on a cache hint
        return None
    return value or None


def _open_clipboard(user32, retries: int = 6) -> None:
    for attempt in range(retries):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.01 * (attempt + 1))
    raise OSError("the clipboard is locked by another application")


def _global_bytes(handle: int, kernel32) -> bytes:
    size = int(kernel32.GlobalSize(ctypes.c_void_p(handle)))
    pointer = kernel32.GlobalLock(ctypes.c_void_p(handle))
    if not pointer:
        return b""
    try:
        return ctypes.string_at(pointer, size)
    finally:
        kernel32.GlobalUnlock(ctypes.c_void_p(handle))


def _alloc_and_set(user32, kernel32, fmt: int, payload: bytes) -> None:
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        raise OSError("GlobalAlloc failed")
    pointer = kernel32.GlobalLock(ctypes.c_void_p(handle))
    if not pointer:
        kernel32.GlobalFree(ctypes.c_void_p(handle))
        raise OSError("GlobalLock failed")
    try:
        ctypes.memmove(pointer, payload, len(payload))
    finally:
        kernel32.GlobalUnlock(ctypes.c_void_p(handle))
    if not user32.SetClipboardData(fmt, ctypes.c_void_p(handle)):
        kernel32.GlobalFree(ctypes.c_void_p(handle))
        raise OSError("SetClipboardData failed")


def has_dib() -> bool:
    if not supported():
        return False
    try:
        user32, _ = _win()
        return bool(user32.IsClipboardFormatAvailable(CF_DIB))
    except Exception:  # pragma: no cover
        return False


def read_png() -> bytes | None:
    """Return the clipboard image as PNG bytes, or None when there is none."""
    if not supported():
        return None

    user32, kernel32 = _win()
    png_format = _png_format()

    try:
        png_available = bool(user32.IsClipboardFormatAvailable(png_format))
        dib_available = bool(user32.IsClipboardFormatAvailable(CF_DIB))
        if not png_available and not dib_available:
            return None

        if png_available:
            try:
                _open_clipboard(user32)
                try:
                    handle = user32.GetClipboardData(png_format)
                    if handle:
                        payload = _global_bytes(handle, kernel32)
                        if payload.startswith(PNG_SIGNATURE):
                            return payload
                finally:
                    user32.CloseClipboard()
            except OSError:
                logger.debug("could not read the PNG clipboard format", exc_info=True)

        if dib_available:
            from PIL import ImageGrab

            grabbed = ImageGrab.grabclipboard()
            if isinstance(grabbed, list):
                return None  # copied files, not an image
            if grabbed is None:
                return None
            buffer = io.BytesIO()
            grabbed.convert("RGB").save(buffer, "PNG")
            return buffer.getvalue()
    except Exception:
        logger.debug("could not read an image from the clipboard", exc_info=True)
    return None


def write_png(png: bytes) -> None:
    """Put PNG bytes on the clipboard (PNG + CF_DIB for compatibility)."""
    if not supported():
        raise OSError("image clipboard access is not available")

    user32, kernel32 = _win()
    _open_clipboard(user32)
    try:
        if not user32.EmptyClipboard():
            raise OSError("EmptyClipboard failed")
        _alloc_and_set(user32, kernel32, _png_format(), png)
        try:
            _alloc_and_set(user32, kernel32, CF_DIB, _png_to_dib(png))
        except Exception:
            # PNG alone is enough for modern apps.
            logger.debug("could not also set the bitmap clipboard format", exc_info=True)
    finally:
        user32.CloseClipboard()


def _png_to_dib(png: bytes) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "BMP")
    # BITMAPFILEHEADER is 14 bytes; CF_DIB starts at the info header.
    return buffer.getvalue()[14:]


def image_size(png: bytes) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            return image.size
    except Exception:  # pragma: no cover - only used for a nicer label
        return (0, 0)


def to_png(payload: bytes, mime: str = "image/png") -> bytes:
    """Normalise any decoded image bytes into PNG (used for non-PNG inputs)."""
    from PIL import Image

    with Image.open(io.BytesIO(payload)) as image:
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()
