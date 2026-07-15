"""Детект полосы HP моба (рара/уника) вверху по центру экрана — для авто-атаки.

Когда наводишься/в бою с монстром, PoE2 рисует вверху по центру его HP-бар:
красная полоса на тёмной подложке + имя. Ловим её как красную связную компоненту
ФОРМЫ ПОЛОСЫ — тонкий широкий сплошной прямоугольник (w ≫ h, высокий fill).
Красные пятна сцены (камень/лава/факелы в красно-оранжевой локации) — это почти
квадратные бесформенные блобы (aspect ≈ 1, большая высота) и отсекаются формой.
Бар виден (даже частично) = моб жив → бот держит R1. Бар пропал = цели нет → отпускает.

Когда HP моба почти на нуле, от полосы остаётся крохотный огрызок красного у
левого края рамки (бар тает СПРАВА налево) — он уже не проходит фильтры формы
(слишком узкий/квадратный, легко спутать с декором). Чтобы не отпускать R1
прямо перед добиванием, детектор «залипает»: как только рамка бара была
уверенно опознана по форме, её координаты (x, y, w, h) запоминаются, и пока
внутри НИХ остаётся хоть немного красного — считаем бар всё ещё живым, даже
если остаток сам по себе форму полосы не держит.

Разбор боя с Shiverdrool (2026-07-16): если моб появляется в поле зрения УЖЕ
подраненным (бар с первого кадра узкий), строгий матч не проходит НИ РАЗУ —
лока для «добивания» выше никогда не появляется, и бот не атакует вообще, пока
бар не пропадёт целиком. Пробовали отличать такой узкий огрызок от шума/декора
по цвету соседней области (плоский тёмный фон бара справа) — не подтвердилось:
у этого стиля бара имя моба подходит вплотную почти без зазора, а тёмный
десатурированный декор сцены неотличим по HSV. Вместо цвета используем ВРЕМЯ:
узкий кандидат подтверждается, только если держит примерно одну и ту же рамку
несколько кадров подряд — случайный шум/блик так не делает, а настоящий (пусть
и подраненный) бар моба — держит.

ОБРАТНАЯ проблема, найдена в том же сеансе сразу после фикса: «добивание»
(low_hp_lock) держало R1 20+ секунд НА ПУСТОЙ ЗЕМЛЕ — в локации оказалась
рыжая листва/подстилка, которая почти всегда даёт ≥low_hp_min_px(6) красных
пикселей где-то внутри старой рамки бара. У low_hp_lock_timeout_s окно
СКОЛЬЗЯЩЕЕ (сбрасывается на каждом попадании шума), поэтому оно продлевало
само себя бесконечно, хотя моб давно пропал. Фикс: держим ОТДЕЛЬНУЮ метку
времени последнего РЕАЛЬНОГО (strict/bootstrap) подтверждения — low_hp_lock
теперь может продлеваться только пока не прошло больше low_hp_hard_cap_s с
последнего такого подтверждения, а не от самого себя.

БУТСТРАП ОТКЛЮЧЁН ПО УМОЛЧАНИЮ (2026-07-16, сразу после введения): в той же
живой сессии, в лавовой/скальной локации, поймал ДВА разных декора (плитка
пола, скала на закате) с aspect 1.62 и 1.94 — а у настоящего Shiverdrool
aspect был тоже 1.94. Устойчивость во времени фильтрует только МЕЛЬКАЮЩИЙ
шум (VFX-вспышки), а не статичный декор сцены — а такой декор, оказывается,
не отличить от бара по одной лишь форме (aspect/fill/ширина): они физически
совпадают в этой локации. Держать включённым — значит регулярно ложно
атаковать пустоту в скальных/лавовых зонах. Код рабочий, но
auto_attack.bootstrap_enabled по умолчанию false, пока не найдётся более
надёжный признак (например, проверка на текст названия моба рядом с
полосой — у декора такого текста не бывает).

«NARROW»-уровень (2026-07-16, толпа мобов): при куче мобов рядом бар каждого
отдельного моба узкий (не один большой бар цели, а много мелких по толпе) —
все ушли ниже min_run_frac. НО их форма очень вытянута (aspect 8.8-11.0 в
живых замерах) — далеко от квадратистого декора (aspect 1.6-1.9, тот же
разбор, что и у бутстрапа выше). Разрыв большой, поэтому в отличие от
бутстрапа этот уровень включён по умолчанию и без задержки на устойчивость —
чистая проверка по форме, как строгий уровень, только либеральнее по ширине
и строже по aspect. Пользователь на момент ввода подтвердил, что FPS/HW-
оверлей (потенциальный источник путаницы, ради которого раньше подняли
min_run_frac до 256px) сейчас выключен — если он у тебя обычно включён и он
широкий вытянутый плашкой (не квадратный бейдж), последи за kind=narrow.
"""
import time

import cv2
import numpy as np


class MobBarDetector:
    def __init__(self, cfg):
        self.roi = cfg.get("roi", {"x1": 0.36, "y1": 0.02, "x2": 0.64, "y2": 0.12})
        self.sat_min = cfg.get("red_sat_min", 150)
        self.val_min = cfg.get("red_val_min", 90)
        # признаки ПОЛОСЫ (а не пятна сцены): длинная, тонкая, сплошная
        self.min_w_frac = cfg.get("min_run_frac", 0.018)   # мин. ширина (доля кадра)
        self.max_h_frac = cfg.get("max_h_frac", 0.030)     # макс. высота (доля кадра)
        self.min_aspect = cfg.get("min_aspect", 3.0)       # w/h — полоса вытянута
        self.min_fill = cfg.get("min_fill", 0.45)          # area/(w*h) — полоса сплошная
        self.edge_margin_px = cfg.get("edge_margin_px", 2)  # компонента у самого края ROI = обрезанный артефакт декора, не бар
        # «добивание» почти мёртвого моба: рамку последнего уверенного матча
        # держим ещё столько секунд без подтверждений, ищем в ней хотя бы
        # столько красных пикселей
        self.low_hp_lock_timeout_s = cfg.get("low_hp_lock_timeout_s", 2.5)
        self.low_hp_min_px = cfg.get("low_hp_min_px", 12)
        # жёсткий потолок с последнего РЕАЛЬНОГО (strict/bootstrap) подтверждения —
        # low_hp_lock не может продлевать себя от собственных шумовых попаданий
        # бесконечно (см. docstring модуля, инцидент с рыжей листвой)
        self.low_hp_hard_cap_s = cfg.get("low_hp_hard_cap_s", 6.0)

        # «бутстрап» под моба, который бот впервые видит УЖЕ подраненным — та же
        # форма (тонкая, сплошная), но ширина/aspect слабее строгих порогов.
        # Подтверждается не с первого кадра, а только если рамка держится на
        # месте bootstrap_confirm_s секунд подряд (см. docstring модуля).
        self.bootstrap_enabled = cfg.get("bootstrap_enabled", False)
        self.bootstrap_min_w_frac = cfg.get("bootstrap_min_w_frac", 0.02)
        self.bootstrap_min_aspect = cfg.get("bootstrap_min_aspect", 1.8)
        self.bootstrap_confirm_s = cfg.get("bootstrap_confirm_s", 0.25)
        self.bootstrap_move_tol_px = cfg.get("bootstrap_move_tol_px", 6)

        # «narrow»: узкие бары толпы мобов — та же ширина, что у бутстрапа, но
        # БЕЗ задержки на устойчивость (форма сама по себе надёжно отличима от
        # декора — см. docstring модуля)
        self.narrow_enabled = cfg.get("narrow_enabled", True)
        self.narrow_min_w_frac = cfg.get("narrow_min_w_frac", 0.02)
        self.narrow_min_aspect = cfg.get("narrow_min_aspect", 6.0)

        # диагностика последнего вызова present() — для дампа улик при ложном срабатывании
        self.last_roi_px = None    # (x1, y1, x2, y2) в координатах кадра
        self.last_mask = None      # маска красного внутри ROI
        self.last_match = None     # dict с bbox/aspect/fill компоненты, что дала True

        self._lock = None      # (x, y, w, h) рамки последнего уверенного матча, координаты ROI-local
        self._lock_ts = 0.0    # когда рамка последний раз подтверждалась (строго, бутстрапом или добиванием)
        self._confirmed_ts = 0.0  # когда рамка последний раз подтверждалась РЕАЛЬНО (strict/bootstrap)

        self._boot_box = None   # (x, y, w, h) текущего бутстрап-кандидата
        self._boot_since = 0.0  # когда кандидат впервые появился на этом месте

    def present(self, frame_bgr):
        """True, если в ROI есть красная компонента ФОРМЫ полосы HP (моб жив)."""
        fh, fw = frame_bgr.shape[:2]
        x1, y1 = int(self.roi["x1"] * fw), int(self.roi["y1"] * fh)
        x2, y2 = int(self.roi["x2"] * fw), int(self.roi["y2"] * fh)
        self.last_roi_px = (x1, y1, x2, y2)
        sub = frame_bgr[y1:y2, x1:x2]
        if sub.size == 0:
            self.last_mask, self.last_match = None, None
            return False
        hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
        mask = cv2.bitwise_or(
            cv2.inRange(hsv, (0, self.sat_min, self.val_min), (10, 255, 255)),
            cv2.inRange(hsv, (170, self.sat_min, self.val_min), (179, 255, 255)))
        self.last_mask = mask
        min_w = max(8, int(self.min_w_frac * fw))
        min_w_boot = max(6, int(self.bootstrap_min_w_frac * fw))
        min_w_narrow = max(6, int(self.narrow_min_w_frac * fw))
        max_h = max(4, int(self.max_h_frac * fh))
        roi_w = sub.shape[1]
        margin = self.edge_margin_px
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        now = time.perf_counter()
        boot_candidate = None
        for i in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[i])
            # компонента, упирающаяся в левый/правый край ROI — обрезанный кусок
            # декора сцены (факел/жаровня), а не настоящий бар (тот весь помещается
            # в ROI, т.к. рисуется по центру над мобом)
            if x <= margin or (x + w) >= (roi_w - margin):
                continue
            aspect = w / max(1, h)
            fill = area / max(1, w * h)
            if (w >= min_w and h <= max_h
                    and aspect >= self.min_aspect
                    and fill >= self.min_fill):
                self.last_match = {"x": x, "y": y, "w": w, "h": h, "area": area,
                                    "aspect": aspect, "fill": fill}
                self._lock = (x, y, w, h)
                self._lock_ts = now
                self._confirmed_ts = now
                self._boot_box = None
                return True
            if (self.narrow_enabled and w >= min_w_narrow and h <= max_h
                    and aspect >= self.narrow_min_aspect
                    and fill >= self.min_fill):
                self.last_match = {"x": x, "y": y, "w": w, "h": h, "area": area,
                                    "aspect": aspect, "fill": fill, "narrow": True}
                self._lock = (x, y, w, h)
                self._lock_ts = now
                self._confirmed_ts = now
                self._boot_box = None
                return True
            if (self.bootstrap_enabled and w >= min_w_boot and h <= max_h
                    and aspect >= self.bootstrap_min_aspect
                    and fill >= self.min_fill):
                boot_candidate = (x, y, w, h, area, aspect, fill)

        # строгая форма не нашлась — проверяем «добивание» по запомненной рамке:
        # бар тает справа налево, остаток красного всегда у ЛЕВОГО края рамки,
        # значит он лежит внутри уже подтверждённого (x, y, w, h)
        if (self._lock is not None and now - self._lock_ts <= self.low_hp_lock_timeout_s
                and now - self._confirmed_ts <= self.low_hp_hard_cap_s):
            lx, ly, lw, lh = self._lock
            band = mask[max(0, ly - 2):ly + lh + 2, max(0, lx):lx + lw]
            red_px = int(cv2.countNonZero(band)) if band.size else 0
            if red_px >= self.low_hp_min_px:
                self.last_match = {"x": lx, "y": ly, "w": lw, "h": lh, "area": red_px,
                                    "aspect": lw / max(1, lh), "fill": red_px / max(1, lw * lh),
                                    "low_hp_lock": True}
                self._lock_ts = now
                return True

        # бутстрап: узкий кандидат подтверждаем только по устойчивости во времени
        # (см. docstring модуля — по цвету надёжно не отличить от декора сцены)
        if boot_candidate is not None:
            x, y, w, h, area, aspect, fill = boot_candidate
            if (self._boot_box is not None
                    and abs(x - self._boot_box[0]) <= self.bootstrap_move_tol_px
                    and abs(y - self._boot_box[1]) <= self.bootstrap_move_tol_px):
                held_s = now - self._boot_since
            else:
                self._boot_box = (x, y, w, h)
                self._boot_since = now
                held_s = 0.0
            if held_s >= self.bootstrap_confirm_s:
                self.last_match = {"x": x, "y": y, "w": w, "h": h, "area": area,
                                    "aspect": aspect, "fill": fill, "bootstrap": True}
                self._lock = (x, y, w, h)
                self._lock_ts = now
                self._confirmed_ts = now
                return True
        else:
            self._boot_box = None

        self.last_match = None
        return False
