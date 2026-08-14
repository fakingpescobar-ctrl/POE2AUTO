"""screen_capture.py — быстрый захват клиентской области окна PoE2 +
HSV-фильтрация для детекта активных UI кнопок (Activate в Map Device).

Адаптация grabScreen/sortByHSV из tachikoma 0_guest/utils.py:
  - grabScreen(region) — Win32 BitBlt, BGRA numpy массив
  - sortByHSV(img, h1,s1,v1, h2,s2,v2) — бинаризация по HSV диапазону

Используется в map_run_flow.py:
  activate_button_is_active = check_activate_button(map_device_region,
                                                       hsv=(yellow glow))

Не依赖于 dxcam — это per-region (+-1ms latency) 1 grab per check, нет потока.
"""
from __future__ import annotations

import logging
import win32api
import win32con
import win32gui
import win32ui
import numpy as np
import cv2

_log = logging.getLogger("poe2auto.scap")


def grab_client_region(hwnd: int, region: tuple[int, int, int, int] | None = None
                       ) -> np.ndarray:
    """Получить numpy-кадр клиентской области окна hwnd (BGR).

    region: (left, top, right, bottom) — относительно клиентской области окна.
    Если None — вся клиентская область.
    """
    if region is None:
        cr = win32gui.GetClientRect(hwnd)
        left, top = 0, 0
        right, bottom = cr[2], cr[3]
    else:
        left, top, right, bottom = region
    width = max(1, right - left + 1)
    height = max(1, bottom - top + 1)

    hwindc = win32gui.GetWindowDC(hwnd)
    srcdc = win32ui.CreateDCFromHandle(hwindc)
    memdc = srcdc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(srcdc, width, height)
    memdc.SelectObject(bmp)
    memdc.BitBlt((0, 0), (width, height), srcdc, (left, top), win32con.SRCCOPY)

    bits = bmp.GetBitmapBits(True)
    # bits — bytes BGRA; bytes -> uint8 -> reshape
    arr = np.frombuffer(bits, dtype=np.uint8)
    if arr.size != width * height * 4:
        # fallback: fromstring deprecated path
        arr = np.fromstring(bits, dtype=np.uint8)
    arr = arr.reshape(height, width, 4)
    img = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)

    srcdc.DeleteDC()
    memdc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwindc)
    win32gui.DeleteObject(bmp.GetHandle())
    return img


def grab_desktop(region: tuple[int, int, int, int] | None = None,
                 convert: str = "BGR") -> np.ndarray:
    """Захват рабочего стола. region — абсолютные экранные координаты.

    Альтернатива, если hwnd потерян или нужно внеигровое UI.
    """
    hwin = win32gui.GetDesktopWindow()
    if region is not None:
        left, top, x2, y2 = region
        width = x2 - left + 1
        height = y2 - top + 1
    else:
        width = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
        height = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
        left = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
        top = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)

    hwindc = win32gui.GetWindowDC(hwin)
    srcdc = win32ui.CreateDCFromHandle(hwindc)
    memdc = srcdc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(srcdc, width, height)
    memdc.SelectObject(bmp)
    memdc.BitBlt((0, 0), (width, height), srcdc, (left, top), win32con.SRCCOPY)

    bits = bmp.GetBitmapBits(True)
    arr = np.frombuffer(bits, dtype=np.uint8)
    if arr.size != width * height * 4:
        arr = np.fromstring(bits, dtype=np.uint8)
    arr = arr.reshape(height, width, 4)
    srcdc.DeleteDC()
    memdc.DeleteDC()
    win32gui.ReleaseDC(hwin, hwindc)
    win32gui.DeleteObject(bmp.GetHandle())

    if convert == "BGR":
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
    if convert == "RGB":
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2RGB)
    if convert == "GRAY":
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2GRAY)
    return arr


def sort_by_hsv(img_bgr: np.ndarray,
                h1: int, s1: int, v1: int,
                h2: int, s2: int, v2: int) -> np.ndarray:
    """Бинаризация по HSV диапазону.

    Возвращает grayscale где пиксели в диапазоне — 255, остальное 0.
    Полезно для детекта желтой подсветки активированной кнопки Activate
    на Map Device UI (например yellow color when lit).
    """
    blurred = cv2.medianBlur(img_bgr, 3)
    hsv = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)
    lower = np.array((h1, s1, v1), np.uint8)
    upper = np.array((h2, s2, v2), np.uint8)
    return cv2.inRange(hsv, lower, upper)


def count_pixels_in_hsv(img_bgr: np.ndarray,
                         h1: int, s1: int, v1: int,
                         h2: int, s2: int, v2: int,
                         threshold: int = 30) -> tuple[bool, int]:
    """True если в img_bgr больше `threshold` пикселей в HSV-диапазоне."""
    mask = sort_by_hsv(img_bgr, h1, s1, v1, h2, s2, v2)
    cnt = int(np.count_nonzero(mask))
    return cnt > threshold, cnt


# Activate button на Map Device UI — яркое жёлтое свечение когда валиден
ACTIVATE_BTN_LIT_HSV = (0, 234, 0, 255, 255, 73)


def is_activate_button_lit(button_region_screen: tuple[int, int, int, int],
                            hwnd: int | None = None) -> bool:
    """Проверить, подсвечен ли Activate button (yellow glow).

    button_region_screen: (left, top, right, bottom) — абсолютные экранные coords.
    Если hwnd=None — grab_desktop (если hwnd есть — grab_client_region + screen offset).
    """
    if hwnd is None:
        img = grab_desktop(region=button_region_screen)
    else:
        # translate screen -> client — fallback через desktop grab (hwnd-branch TODO)
        img = grab_desktop(region=button_region_screen)
    lit, n = count_pixels_in_hsv(img, *ACTIVATE_BTN_LIT_HSV, threshold=30)
    _log.debug("activate_btn_lit=%s pixels=%d", lit, n)
    return lit


# Голубые точки Atlas (Map Device overview) — доступные непройденные локации.
# HSV диапазон (OpenCV: H 0..179) — калибруется; по умолчанию сине-голубые.
ATLAS_BLUE_HSV = (90, 70, 60, 135, 255, 255)


def atlas_blue_finder(hwnd: int | None = None,
                      region: tuple[int, int, int, int] | None = None,
                      hsv_range: tuple[int, int, int, int, int, int] = ATLAS_BLUE_HSV,
                      min_area: int = 8,
                      max_components: int = 12,
                      ) -> list[tuple[int, int, int]]:
    """Найти голубые точки на Atlas overview (доступные непройденные узлы).

    Возвращает [(cx, cy, area), ...] — центры кластеров и площадь (px), отсортировано
    по area по убыванию. Экрано-координаты абсолютные (через grab_desktop),
    при hwnd и region=(L,T,R,B) относительно client-области.

    Используется в map_run_flow.step4: по центрам голубых точек геймпад-курсор
    наводится (left stick) на ближайшую, tap(A) открывает info panel с TRAVERSE.
    """
    if hwnd is not None and region is not None:
        off_x = win32gui.ClientToScreen(hwnd, (0, 0))[0]
        off_y = win32gui.ClientToScreen(hwnd, (0, 0))[1]
        img = grab_client_region(hwnd, region)
        region_screen = (region[0] + off_x, region[1] + off_y,
                         region[2] + off_x, region[3] + off_y)
    else:
        img = grab_desktop(region=region)
        region_screen = region if region is not None else (
            0, 0,
            win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN),
            win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN))

    mask = sort_by_hsv(img, *hsv_range)
    num, labels, stats, _cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out: list[tuple[int, int, int]] = []
    for i in range(1, num):
        x, y, w, h, area = stats[i]
        if area < min_area:
            continue
        if w < 2 or h < 2:
            continue
        cx = region_screen[0] + x + w // 2
        cy = region_screen[1] + y + h // 2
        out.append((cx, cy, int(area)))
    out.sort(key=lambda t: -t[2])
    if len(out) > max_components:
        out = out[:max_components]
    _log.debug("atlas_blue_finder: blobs=%d", len(out))
    return out


if __name__ == "__main__":
    # sanity check: grab whole desktop center, show shape
    img = grab_desktop()
    print("desktop shape:", img.shape, "dtype:", img.dtype)
    mask = sort_by_hsv(img, 0, 234, 0, 255, 255, 73)
    print("yellow pixels in HSV(0,234,0..255,255,73):", int(np.count_nonzero(mask)))
    blue = atlas_blue_finder()
    print("atlas blue blobs:", len(blue))
    for cx, cy, area in blue[:10]:
        print("  blob center=(%d,%d) area=%d" % (cx, cy, area))