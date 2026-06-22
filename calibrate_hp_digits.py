"""Разовая калибровка шаблонов цифр HP для hp_ocr.py.

Снимает кадр игры, вырезает ROI строки «Здоровье», бинаризует и сегментирует
глифы. Ты смотришь сохранённую картинку _debug/hp_nums_cap.png и вписываешь, что
видишь (например 1640/1640). Глифы слева направо привязываются к введённым
символам и сохраняются как шаблоны в hp_digits/<0-9>.png и slash.png.

Чтобы собрать все цифры 0-9, сделай несколько снимков на РАЗНОМ HP (получи урон).
Тул показывает, каких цифр ещё не хватает. ~1 минута.

Запуск:  python calibrate_hp_digits.py
"""
import os
import time

import cv2

from config import load_config
from window import GameWindow
from screen import ScreenCapture
from virtual_xbox import VirtualXbox360
from dualsense_bridge import DualSenseBridge
from hp_ocr import (TPL_DIR, LABELS, roi_px, binarize, segment, norm_glyph,
                    load_templates, classify, HPNumReader)

DEFAULT_ROI = {"x1": 0.066, "y1": 0.700, "x2": 0.137, "y2": 0.735}
CAP_PNG = os.path.join(os.path.dirname(__file__), "_debug", "hp_nums_cap.png")


def collected():
    return {name for name in LABELS
            if os.path.exists(os.path.join(TPL_DIR, name + ".png"))}


def save_glyph(label, glyph_norm):
    os.makedirs(TPL_DIR, exist_ok=True)
    cv2.imwrite(os.path.join(TPL_DIR, label + ".png"), glyph_norm)


def grab(cap, region):
    for _ in range(8):
        f = cap.grab(region)
        if f is not None:
            return f
    return None


CAPS_DIR = os.path.join(os.path.dirname(__file__), "_debug", "digit_caps")


def capture_candidates(cap, region, roi, thr, min_h, match_min, missing_label,
                       seconds=300):
    """Копит на диск кадры-кандидаты с недостающей цифрой (надёжно, без угадывания).

    Пока ты играешь, тул читает HP имеющимися шаблонами. Кадры, где есть
    нераспознанный глиф ('?') — потенциально содержат недостающую цифру. Их ROI
    (цветной кроп) сохраняется в _debug/digit_caps/ с дедупом по прочитанной
    строке. Реген HP не мешает: момент ловится на лету. Сами шаблоны НЕ трогаем —
    настоящую цифру потом вытащит ассистент из этих кропов (с проверкой глазами).
    """
    tpls = load_templates()
    label = "/" if missing_label == "slash" else missing_label
    os.makedirs(CAPS_DIR, exist_ok=True)
    for f in os.listdir(CAPS_DIR):           # чистим старые кандидаты
        os.remove(os.path.join(CAPS_DIR, f))
    print(f"\n=== СБОР КАНДИДАТОВ на цифру '{label}' ({seconds}с) ===")
    print("Просто играй и получай урон в пачке — реген пусть работает.")
    print("Тул копит все разные значения HP. (Ctrl+C — закончить раньше)\n")
    end = time.time() + seconds
    seen = set()
    saved = 0
    cap_limit = 400
    last_print = 0.0
    try:
        while time.time() < end and saved < cap_limit:
            frame = grab(cap, region)
            if frame is None:
                continue
            fh, fw = frame.shape[:2]
            x1, y1, x2, y2 = roi_px(roi, fw, fh)
            crop = frame[y1:y2, x1:x2]
            binary = binarize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), thr)
            glyphs = segment(binary, min_h)
            s = "".join((classify(norm_glyph(g), tpls, match_min) or "?") for _x, g in glyphs)

            now = time.time()
            if now - last_print >= 1.0:
                rem = int(end - now)
                print(f"  вижу: {s or '(пусто)'}   [собрано разных: {saved} | осталось {rem}с]")
                last_print = now

            # копим КАЖДОЕ новое прочитанное значение (с '?' или без) — так не упустим
            # кадр с восьмёркой, даже если она криво совпала с похожим шаблоном
            if s and s not in seen:
                seen.add(s)
                saved += 1
                # '?' нельзя в имени файла Windows → заменяем на Q (это кадры с
                # незнакомым глифом = вероятная недостающая цифра, их и ловим!)
                safe = s.replace("/", "-").replace("?", "Q")
                cv2.imwrite(os.path.join(CAPS_DIR, f"{saved:03d}_{safe}.png"), crop)
    except KeyboardInterrupt:
        print("\n(прервано Ctrl+C)")
    print(f"\nСобрано разных значений: {saved} → {CAPS_DIR}")
    print("Закрывай калибратор и скажи ассистенту «готово» — он вытащит цифру.")
    return saved > 0


def manual_calibrate(cap, region, roi, thr, min_h):
    """Ручной режим: снимок → ты вписываешь что видишь → шаблоны сохраняются."""
    print("\n=== Ручная калибровка цифр HP ===")
    print("После снимка ОТКРОЙ", CAP_PNG, "и впиши что видишь.")
    print("Для всех цифр 0-9 сделай снимки на разном HP (получи урон).\n")
    while True:
        frame = grab(cap, region)
        if frame is None:
            print("Нет кадра, повтор (игра запущена?)…")
            continue
        fh, fw = frame.shape[:2]
        x1, y1, x2, y2 = roi_px(roi, fw, fh)
        binary = binarize(cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY), thr)
        glyphs = segment(binary, min_h)
        cv2.imwrite(CAP_PNG, cv2.resize(binary, None, fx=4, fy=4,
                                        interpolation=cv2.INTER_NEAREST))
        have = collected()
        missing = [("/" if l == "slash" else l) for l in LABELS if l not in have]
        print(f"\nГлифов в кадре: {len(glyphs)} | собрано: "
              f"{sorted('/' if h=='slash' else h for h in have)} | не хватает: {missing}")
        s = input("Что видишь (напр 1640/1640) | Enter=переснять | q=выход: ").strip()
        if s.lower() == "q":
            break
        if s == "":
            continue
        chars = [c for c in s if c.isdigit() or c == "/"]
        if len(chars) != len(glyphs):
            print(f"  ! Ты ввёл {len(chars)} символов, а глифов найдено {len(glyphs)}. "
                  f"Кадр кривой (цифры касаются/шум/перекрытие) — переснять на другом HP.")
            continue
        for (_x, g), ch in zip(glyphs, chars):
            save_glyph("slash" if ch == "/" else ch, norm_glyph(g))
        print("  ✓ сохранил:", "".join(chars))
        if set(LABELS) <= collected():
            print("\n*** ВСЕ глифы 0-9 и '/' собраны! Калибровка завершена. ***")
            break


def main():
    cfg = load_config()
    num_cfg = cfg.get("hp_num", {})
    roi = num_cfg.get("roi", DEFAULT_ROI)
    thr = num_cfg.get("bin_thr", 130)
    min_h = num_cfg.get("min_glyph_h", 10)

    win = GameWindow(cfg["game"]["window_title"])
    if win.find() and win.get_region():
        region = win.get_region()
        print("Окно PoE2:", region)
    else:
        region = GameWindow.primary_region()
        print("Окно не найдено — захват монитора:", region)
    cap = ScreenCapture(cfg["capture"]["backend"])
    os.makedirs(os.path.dirname(CAP_PNG), exist_ok=True)

    # геймпад: держим виртуальный Xbox + мост DualSense, чтобы джойстик работал
    # и тебя не кикало из игры, пока калибруешь. ВАЖНО: run_bot.bat должен быть
    # ЗАКРЫТ (иначе два геймпада и два захвата экрана конфликтуют).
    vx = VirtualXbox360()
    if not vx.start():
        print("! Не удалось создать виртуальный Xbox 360 (ViGEmBus/vgamepad?). "
              "Закрой run_bot.bat и запусти калибратор от администратора.")
        return
    bridge = DualSenseBridge(vx)
    if not bridge.start():
        print("! DualSense не найден — управление с джойстика не пойдёт "
              "(но калибровать всё равно можно).")
    print("Геймпад активен — можешь играть. (run_bot.bat должен быть закрыт!)")
    print("ROI строки 'Здоровье':", roi)

    max_hp = num_cfg.get("max_hp", 0)
    try:
        missing = [l for l in LABELS if l not in collected()]
        # авто-режим: если не хватает РОВНО одной цифры и известен max_hp — ловим
        # её автоматически (реген не мешает). Иначе обычный ручной режим.
        if len(missing) == 1 and missing[0] != "slash":
            capture_candidates(cap, region, roi, thr, min_h,
                               num_cfg.get("match_min", 0.55), missing[0])
        else:
            if missing and max_hp == 0:
                print("(max_hp в config не задан — авто-ловля недоступна, ручной режим)")
            manual_calibrate(cap, region, roi, thr, min_h)

        # самопроверка: что читается сейчас
        reader = HPNumReader({**num_cfg, "roi": roi, "bin_thr": thr, "min_glyph_h": min_h})
        if reader.ready:
            frame = grab(cap, region)
            if frame is not None:
                print("\nПроверка чтения текущего кадра:")
                print("  строка :", reader.read_string(frame))
                r = reader.read(frame)
                print("  разбор :", f"{r[0]}/{r[1]} = {r[2]*100:.0f}%" if r else "не распозналось")
        print("\nГотово. Шаблоны в:", TPL_DIR)
        print("Дальше: перезапусти run_bot.bat — цифровое чтение включится само.")
    finally:
        bridge.stop()
        vx.stop()


if __name__ == "__main__":
    main()
