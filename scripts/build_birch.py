#!/usr/bin/env python3
"""Рисует берёзу beresta: крону, ствол-плитку, корни и линию земли.

Только стандартная библиотека. Результат детерминирован (seed 7), все числа округлены до десятых.
Пишет app/static/birch-trunk.svg, app/static/soil-line.svg и вставляет крону и корни в app/static/index.html
между метками <!--birch:crown--> ... <!--/birch:crown--> и <!--birch:roots--> ... <!--/birch:roots-->.

Запуск из корня репозитория: python3 scripts/build_birch.py
Устройство и координаты описаны в docs/DESIGN.md, раздел «Берёза».
"""
import math
import random
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

W = 400          # ширина всех частей берёзы в единицах
T = 1200         # высота плитки ствола
CROWN_H = 560    # высота кроны
ROOTS_H = 260    # высота корней
GROUND = 64      # линия земли в корнях, отсчёт от верха корней
XC = 296         # ось ствола на стыках
HALF = 34        # полуширина ствола на стыках

INK = "#2a2d2b"      # графит коры
BARK = "#1f2221"     # самые тёмные пятна
PAPER = "#fffefa"    # ствол
SHADE = "#e8e2d2"    # плоская тень на правой стороне ствола
ROOT = "#3a3e3b"
GREENS = ["#7db84a", "#9bcb6b", "#5e9a34", "#bddb94"]  # один оттенок, четыре тона


def f(v):
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def pt(p):
    return f"{f(p[0])} {f(p[1])}"


def smooth(points, tension=0.5):
    """Catmull-Rom через точки, как путь из кубических кривых."""
    if len(points) < 3:
        return "M" + " L".join(pt(p) for p in points)
    d = f"M{pt(points[0])}"
    for i in range(len(points) - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1, p2 = points[i], points[i + 1]
        p3 = points[i + 2] if i + 2 < len(points) else points[i + 1]
        c1 = (p1[0] + (p2[0] - p0[0]) * tension / 3 * 2 / 2, p1[1] + (p2[1] - p0[1]) * tension / 3 * 2 / 2)
        c2 = (p2[0] - (p3[0] - p1[0]) * tension / 3 * 2 / 2, p2[1] - (p3[1] - p1[1]) * tension / 3 * 2 / 2)
        d += f"C{pt(c1)} {pt(c2)} {pt(p2)}"
    return d


class Noise:
    """Сумма синусов с целым числом периодов на высоте T: на концах плитки значение и наклон совпадают."""

    def __init__(self, rnd, amps=(4.2, 2.4, 1.1), ks=(3, 7, 15)):
        self.terms = [(a, k, rnd.uniform(0, math.tau)) for a, k in zip(amps, ks)]

    def __call__(self, y):
        return sum(a * math.sin(math.tau * k * y / T + p) for a, k, p in self.terms)


def ease(u):
    u = max(0.0, min(1.0, u))
    return u * u * (3 - 2 * u)


class Birch:
    def __init__(self):
        self.rnd = random.Random(7)
        self.nl = Noise(self.rnd)
        self.nr = Noise(self.rnd)

    # ---------- края ствола ----------
    def edges_tile(self, y):
        return XC - HALF + self.nl(y), XC + HALF + self.nr(y)

    def edges_crown(self, y):
        u = ease((CROWN_H - 150 - y) / (CROWN_H - 150 - 70))
        half = HALF - 26 * u
        xc = XC - 24 * u
        scale = 1 - 0.7 * u
        return xc - half + self.nl(y - CROWN_H) * scale, xc + half + self.nr(y - CROWN_H) * scale

    def edges_roots(self, y):
        flare = ease((y - 6) / (GROUND - 6)) * 16 + (ease((y - GROUND) / 40) * 10 if y > GROUND else 0)
        left = XC - HALF + self.nl(y) - flare
        right = XC + HALF + self.nr(y) + flare * 0.9
        return left, right

    # ---------- общие куски рисунка ----------
    def trunk_body(self, edges, y0, y1, step=14):
        ys = [y0 + i * step for i in range(int((y1 - y0) / step) + 1)]
        if ys[-1] < y1:
            ys.append(y1)
        left = [(edges(y)[0], y) for y in ys]
        right = [(edges(y)[1], y) for y in ys]
        return ys, left, right

    def fill_and_shade(self, ys, left, right, yoff=0):
        out = []
        # Заливка: левый край сверху вниз, правый снизу вверх.
        out.append(f'<path fill="{PAPER}" d="{smooth(left)}L{pt(right[-1])}{smooth(list(reversed(right)))[len("M" + pt(right[-1])):]}Z"/>')
        # Плоская тень вдоль правого края.
        band = []
        for (x, y), (xl, _) in zip(right, left):
            yy = y - yoff
            width = 11 + 5 * math.sin(math.tau * 5 * yy / T + 1.3) + 3 * math.sin(math.tau * 17 * yy / T)
            band.append((x - width, y))
        d = smooth(band) + "L" + pt(right[-1]) + smooth(list(reversed(right)))[len("M" + pt(right[-1])):] + "Z"
        out.append(f'<path fill="{SHADE}" d="{d}"/>')
        return out

    def edge_strokes(self, side, ys, left, right, draw=False, y_span=None):
        """Края ствола отрывистыми штрихами: каждый штрих начинается чуть в стороне и заходит на предыдущий."""
        pts = left if side == "l" else right
        out = []
        i = 0
        n = len(pts)
        k = 0
        while i < n - 1:
            run = self.rnd.randint(3, 6)
            j = min(n - 1, i + run)
            seg = pts[i:j + 1]
            jitter = [(x + self.rnd.uniform(-0.9, 0.9), y) for x, y in seg]
            cls = f' class="draw d{(k % 6) + 1}" pathLength="1"' if draw else ""
            w = self.rnd.choice((1.8, 2.0, 2.3, 2.6))
            out.append(f'<path{cls} fill="none" stroke="{INK}" stroke-width="{w}" stroke-linecap="round" d="{smooth(jitter)}"/>')
            i = max(i + 1, j - self.rnd.randint(0, 1))
            k += 1
        return out

    def lenticels(self, edges, y0, y1, count, big_at=None, margin=8):
        """Тёмные поперечные штрихи и пятна."""
        out = []
        y = y0 + margin
        while y < y1 - margin:
            xl, xr = edges(y)
            inner = xr - xl - 8
            kind = self.rnd.random()
            if kind < 0.2 and inner > 30:
                # Пятно: неровный тёмный клок.
                w, h = self.rnd.uniform(16, min(46, inner * 0.85)), self.rnd.uniform(8, 20)
                x = self.rnd.uniform(xl + 5, xr - 5 - w)
                p = [(x, y), (x + w * 0.3, y - h * 0.55), (x + w * 0.7, y - h * 0.4), (x + w, y + h * 0.05),
                     (x + w * 0.75, y + h * 0.5), (x + w * 0.25, y + h * 0.6)]
                out.append(f'<path fill="{BARK}" d="{smooth(p + [p[0]])}Z"/>')
                y += h + self.rnd.uniform(14, 30)
                continue
            w = self.rnd.uniform(8, min(44, max(10, inner * 0.8)))
            x0 = self.rnd.uniform(xl + 4, max(xl + 5, xr - 4 - w))
            h = self.rnd.uniform(1.0, 2.6)
            tilt = self.rnd.uniform(-1.5, 1.5)
            d = (f"M{f(x0)} {f(y)}Q{f(x0 + w / 2)} {f(y - h * 1.6)} {f(x0 + w)} {f(y + tilt)}"
                 f"Q{f(x0 + w / 2)} {f(y + h * 1.1)} {f(x0)} {f(y)}Z")
            out.append(f'<path fill="{BARK}" d="{d}"/>')
            if self.rnd.random() < 0.35:
                # Короткий соседний штрих рядом.
                w2 = w * self.rnd.uniform(0.3, 0.6)
                x2 = x0 + self.rnd.uniform(-6, w * 0.8)
                y2 = y + self.rnd.uniform(4, 8)
                if xl + 3 < x2 and x2 + w2 < xr - 3:
                    out.append(f'<path fill="{BARK}" d="M{f(x2)} {f(y2)}Q{f(x2 + w2 / 2)} {f(y2 - 1.6)} {f(x2 + w2)} {f(y2)}Q{f(x2 + w2 / 2)} {f(y2 + 1.2)} {f(x2)} {f(y2)}Z"/>')
            y += self.rnd.uniform(8, 22)
        return out

    def bark_peel(self, edges, y0, y1, count):
        out = []
        for _ in range(count):
            y = self.rnd.uniform(y0 + 10, y1 - 30)
            xl, xr = edges(y)
            x = xl + self.rnd.uniform(5, 12)
            length = self.rnd.uniform(8, 22)
            out.append(f'<path fill="none" stroke="#bdb6a3" stroke-width="1" stroke-linecap="round" d="M{f(x)} {f(y)}q{f(self.rnd.uniform(-1.5, 1.5))} {f(length / 2)} {f(self.rnd.uniform(-1, 1))} {f(length)}"/>')
        return out

    # ---------- плитка ствола ----------
    def trunk_tile(self):
        ys, left, right = self.trunk_body(self.edges_tile, 0, T, 16)
        parts = self.fill_and_shade(ys, left, right)
        parts += self.edge_strokes("l", ys, left, right)
        parts += self.edge_strokes("r", ys, left, right)
        parts += self.bark_peel(self.edges_tile, 0, T, 14)
        parts += self.lenticels(self.edges_tile, 0, T, 0)
        body = "".join(parts)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {T}" width="{W}" height="{T}" preserveAspectRatio="none">{body}</svg>\n')

    # ---------- линия земли ----------
    def soil_line(self):
        rnd = random.Random(11)
        pts = [(0, 6)]
        x = 0
        while x < 600:
            x += rnd.uniform(40, 90)
            pts.append((min(x, 600), 6 + rnd.uniform(-2.2, 2.2)))
        pts[-1] = (600, 6)
        d = smooth(pts)
        tint = d + "L600 14L0 14Z"
        return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 14" width="600" height="14">'
                f'<path fill="#ebe8dc" d="{tint}"/><path fill="none" stroke="#8c8878" stroke-width="1.3" stroke-linecap="round" d="{d}"/></svg>\n')

    # ---------- листья ----------
    def leaf(self, x, y, angle, size, color, flutter=None):
        tr = f"translate({round(x)} {round(y)}) rotate({round(angle)})" + (f" scale({size:.1f})" if abs(size - 1) > 0.05 else "")
        if flutter:
            return f'<g transform="{tr}"><use href="#lf" class="flutter {flutter} c{color}"/></g>'
        return f'<use href="#lf" class="c{color}" transform="{tr}"/>'

    def branch(self, x0, y0, side, angle_deg, length, bend, widths=(3.0, 2.0, 1.3)):
        """Ветка дугой вверх и вниз. Возвращает точки, пути и места для веточек."""
        sgn = -1 if side == "l" else 1
        theta = math.radians(angle_deg)
        step = 7.0
        n = int(length / step)
        pts = [(x0, y0)]
        x, y = x0, y0
        for k in range(1, n + 1):
            t = k / n
            th = theta - bend * t ** 1.5
            x += sgn * math.cos(th) * step
            y -= math.sin(th) * step
            pts.append((x, y + self.rnd.uniform(-0.6, 0.6)))
        return pts

    def twig(self, x, y, length, side):
        """Тонкая свисающая веточка."""
        pts = [(x, y)]
        dx = self.rnd.uniform(-8, 8) + (-4 if side == "l" else 4)
        for k in range(1, 6):
            t = k / 5
            pts.append((x + dx * t + self.rnd.uniform(-2.5, 2.5) * t, y + length * t))
        return pts

    def crown(self):
        rnd = self.rnd
        out = []
        defs = ('<defs><g id="lf"><path transform="translate(1.3 1)" d="M0 0C5 2.5 7.2 7.6 0 15C-7 7.6-5 2.5 0 0Z"/>'
                '<path fill="none" stroke="#2a2d2b" stroke-width=".9" stroke-linejoin="round" d="M0 0C5 2.5 7.2 7.6 0 15C-7 7.6-5 2.5 0 0ZM0 1.5V11"/></g></defs>')
        # Тело ствола.
        ys, left, right = self.trunk_body(self.edges_crown, 80, CROWN_H + 16, 16)
        out.append('<g class="trunk">')
        out += self.fill_and_shade(ys, left, right, CROWN_H)
        out.append("</g>")
        # Лентицели: гуще внизу кроны.
        out.append('<g class="marks">')
        out += self.lenticels(self.edges_crown, 90, CROWN_H - 8, 0)
        out += self.bark_peel(self.edges_crown, 300, CROWN_H, 8)
        out.append("</g>")
        # Края ствола, рисуются при загрузке.
        out.append('<g class="edges">')
        out += self.edge_strokes("l", ys, left, right, draw=True)
        out += self.edge_strokes("r", ys, left, right, draw=True)
        out.append("</g>")
        # Ветки: сторона, y на стволе, угол, длина, изгиб вниз.
        plan = [
            ("l", 118, 52, 120, 1.1), ("l", 190, 40, 175, 1.15), ("l", 280, 30, 210, 1.1), ("l", 372, 22, 160, 1.0),
            ("r", 150, 46, 58, 1.0), ("r", 245, 34, 60, 1.05), ("r", 338, 24, 52, 1.0),
        ]
        flutter_names = iter(["f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12"])
        order = 0
        for side, ty, ang, length, bend in plan:
            xl, xr = self.edges_crown(ty)
            x0 = xl + 1 if side == "l" else xr - 1
            pts = self.branch(x0, ty, side, ang, length, bend)
            order += 1
            out.append(f'<g class="sw {"sw-l" if side == "l" else "sw-r"} s{order}">')
            # Ветка тремя участками разной толщины.
            third = max(2, len(pts) // 3)
            for idx, (a, b, w) in enumerate(((0, third, 3.4), (third - 1, 2 * third, 2.3), (2 * third - 1, len(pts) - 1, 1.5))):
                seg = pts[a:b + 1]
                if len(seg) >= 2:
                    out.append(f'<path class="draw d{(order + idx) % 6 + 1}" pathLength="1" fill="none" stroke="{INK}" stroke-width="{w}" stroke-linecap="round" d="{smooth(seg)}"/>')
            # Тёмный клин под веткой, как у настоящей берёзы.
            out.append(f'<path fill="{BARK}" d="M{f(x0)} {f(ty + 2)}l{f(-6 if side == "l" else 6)} {f(7)}l{f(1 if side == "l" else -1)} {f(-9)}Z"/>')
            # Веточки со свисающими листьями.
            count = max(3, int(length / 27))
            for k in range(count):
                idx = int((0.3 + 0.7 * (k + rnd.random() * 0.5) / count) * (len(pts) - 1))
                idx = min(idx, len(pts) - 1)
                bx, by = pts[idx]
                tl = rnd.uniform(30, 92) * (0.85 if side == "r" else 1)
                tw = self.twig(bx, by, tl, side)
                out.append(f'<path class="draw d{rnd.randint(1, 6)}" pathLength="1" fill="none" stroke="{INK}" stroke-width="1" stroke-linecap="round" d="{smooth(tw)}"/>')
                for j in range(1, len(tw)):
                    if j < len(tw) - 1 and rnd.random() < 0.3:
                        continue
                    lx, ly = tw[j]
                    ang_leaf = rnd.uniform(-35, 35) + (rnd.choice((-1, 1)) * rnd.uniform(15, 40))
                    size = rnd.uniform(0.75, 1.25)
                    color = rnd.choices(range(4), weights=(5, 3, 3, 2))[0]
                    name = next(flutter_names, None) if rnd.random() < 0.2 and j >= 3 else None
                    out.append(self.leaf(lx, ly, ang_leaf, size, color, name))
                lx, ly = tw[-1]
                out.append(self.leaf(lx, ly + 1, rnd.uniform(-12, 12), rnd.uniform(0.95, 1.35), rnd.randint(0, 3)))
            # Листья прямо на ветке у самого конца.
            for _ in range(3):
                bx, by = pts[-rnd.randint(1, 3)]
                out.append(self.leaf(bx, by, rnd.uniform(-70, 70), rnd.uniform(0.8, 1.2), rnd.randint(0, 3)))
            out.append("</g>")
        # Вершина ствола: тонкий лидер с развилкой.
        xl, xr = self.edges_crown(72)
        xm = (xl + xr) / 2
        lead = [(xm, 76), (xm - 6, 52), (xm - 16, 30), (xm - 30, 14)]
        order += 1
        out.append(f'<g class="sw sw-l s{order}">')
        out.append(f'<path class="draw d2" pathLength="1" fill="none" stroke="{INK}" stroke-width="2.2" stroke-linecap="round" d="{smooth(lead)}"/>')
        fork = [(xm - 5, 50), (xm + 10, 34), (xm + 20, 22)]
        out.append(f'<path class="draw d4" pathLength="1" fill="none" stroke="{INK}" stroke-width="1.4" stroke-linecap="round" d="{smooth(fork)}"/>')
        for lx, ly, a in ((xm - 30, 16, 20), (xm - 22, 22, -40), (xm - 12, 36, 60), (xm + 20, 24, 30), (xm + 12, 36, -50), (xm - 38, 18, 80)):
            out.append(self.leaf(lx, ly, a, rnd.uniform(0.9, 1.25), rnd.randint(0, 3)))
        out.append("</g>")
        # Падающие листья: отдельные маленькие svg позиционируются в CSS, здесь только их рисунок.
        body = "".join(out)
        return (f'<svg class="birch-svg" viewBox="0 0 {W} {CROWN_H}" width="{W}" height="{CROWN_H}" preserveAspectRatio="xMaxYMin meet" '
                f'focusable="false" aria-hidden="true">{defs}{body}</svg>')

    def falling(self):
        names = []
        for i, color in enumerate((0, 1, 3, 2, 0)):
            names.append(f'<svg class="fall fall-{i + 1}" viewBox="-9 -1 18 18" width="14" height="14" focusable="false" aria-hidden="true">'
                         f'<use href="#lf" class="c{color}"/></svg>')
        return "".join(names)

    # ---------- корни ----------
    def taper(self, pts, w0, w1):
        """Заливной корень: центральная линия и плавно сужающаяся ширина."""
        left, right = [], []
        n = len(pts)
        for i, (x, y) in enumerate(pts):
            ax, ay = pts[max(0, i - 1)]
            bx, by = pts[min(n - 1, i + 1)]
            dx, dy = bx - ax, by - ay
            length = math.hypot(dx, dy) or 1
            nx, ny = -dy / length, dx / length
            w = (w0 + (w1 - w0) * (i / (n - 1)) ** 0.8) / 2
            left.append((x + nx * w, y + ny * w))
            right.append((x - nx * w, y - ny * w))
        back = smooth(list(reversed(right)))
        return f"{smooth(left)}L{pt(right[-1])}{back[len('M' + pt(right[-1])):]}Z"

    def roots(self):
        rnd = self.rnd
        out = []
        ys, left, right = self.trunk_body(self.edges_roots, 0, GROUND, 16)
        out.append('<g class="trunk">')
        out += self.fill_and_shade(ys, left, right)
        out.append("</g>")
        out.append('<g class="marks">')
        out += self.lenticels(self.edges_roots, 14, GROUND - 6, 0, margin=4)
        out.append("</g>")
        out.append('<g class="edges">')
        out += self.edge_strokes("l", ys, left, right, draw=True)
        out += self.edge_strokes("r", ys, left, right, draw=True)
        out.append("</g>")
        xl, xr = self.edges_roots(GROUND)
        # Земля: штрихи слева и справа от ствола.
        out.append(f'<path class="draw d5" pathLength="1" fill="none" stroke="#8c8878" stroke-width="1.3" stroke-linecap="round" d="{smooth([(0, GROUND + 1), (60, GROUND - 2), (130, GROUND + 2), (200, GROUND - 1), (xl + 4, GROUND)])}"/>')
        out.append(f'<path class="draw d5" pathLength="1" fill="none" stroke="#8c8878" stroke-width="1.3" stroke-linecap="round" d="{smooth([(xr - 4, GROUND), (xr + 30, GROUND - 2), (400, GROUND)])}"/>')
        # Корни: от края вздутия вниз и в стороны.
        specs = [
            ((xl + 4, GROUND - 4), [(-26, 14), (-70, 26), (-118, 52), (-160, 94), (-190, 150)], 22, 3),
            ((xl + 16, GROUND), [(-8, 30), (-38, 64), (-70, 108), (-96, 160), (-110, 196)], 20, 3),
            ((xl + 30, GROUND + 2), [(0, 36), (-10, 80), (-26, 126), (-30, 172), (-44, 214)], 16, 2),
            (((xl + xr) / 2 + 10, GROUND + 2), [(8, 38), (14, 86), (6, 134), (14, 180), (6, 226)], 15, 2),
            ((xr - 28, GROUND + 2), [(10, 34), (28, 72), (24, 118), (40, 164), (36, 204)], 16, 2),
            ((xr - 14, GROUND), [(20, 26), (46, 54), (62, 96), (86, 134), (96, 168)], 19, 3),
            ((xr - 2, GROUND - 4), [(22, 10), (52, 22), (82, 40), (102, 70)], 17, 3),
        ]
        out.append('<g class="rootset">')
        for order, (start, deltas, w0, w1) in enumerate(specs, 1):
            pts = [start] + [(start[0] + dx + rnd.uniform(-3, 3), start[1] + dy + rnd.uniform(-3, 3)) for dx, dy in deltas]
            out.append(f'<path fill="{ROOT}" d="{self.taper(pts, w0 * 0.55, w1)}"/>')
            # Корешки от середины и конца.
            for idx in range(2, len(pts)):
                if rnd.random() < 0.85:
                    px, py = pts[idx]
                    sgn = -1 if px < start[0] else 1
                    tip = [(px, py), (px + sgn * rnd.uniform(8, 16), py + rnd.uniform(8, 16)), (px + sgn * rnd.uniform(18, 36), py + rnd.uniform(14, 34))]
                    out.append(f'<path fill="none" stroke="{ROOT}" stroke-width="{rnd.choice((1.2, 1.5, 1.8))}" stroke-linecap="round" d="{smooth(tip)}"/>')
                    if rnd.random() < 0.5:
                        x2, y2 = tip[1]
                        out.append(f'<path fill="none" stroke="{ROOT}" stroke-width=".9" stroke-linecap="round" d="M{pt(tip[1])}l{f(sgn * 6)} {f(rnd.uniform(10, 18))}"/>')
        out.append("</g>")
        blades = []
        for bx, bh, lean in ((xl - 18, 13, -3), (xl - 12, 9, 2), (xr + 12, 11, 3), (xr + 18, 7, -2), (xl - 60, 10, -4), (xl - 130, 9, 3), (xl - 200, 12, -3), (xr + 70, 8, 2)):
            blades.append(f'<path class="blade" fill="none" stroke="{GREENS[0]}" stroke-width="2" stroke-linecap="round" d="M{f(bx)} {GROUND}q{f(lean * 0.4)} {f(-bh * 0.6)} {f(lean)} {f(-bh)}"/>')
        out += blades
        body = "".join(out)
        return (f'<svg class="birch-svg" viewBox="0 0 {W} {ROOTS_H}" width="{W}" height="{ROOTS_H}" preserveAspectRatio="xMaxYMax meet" '
                f'focusable="false" aria-hidden="true">{body}</svg>')


def inject(html, name, fragment):
    pattern = re.compile(rf"(<!--birch:{name}-->)(.*?)(<!--/birch:{name}-->)", re.S)
    if not pattern.search(html):
        raise SystemExit(f"В index.html нет меток birch:{name}")
    return pattern.sub(lambda m: m.group(1) + "\n" + fragment + "\n" + m.group(3), html, count=1)


def main():
    birch = Birch()
    tile = birch.trunk_tile()
    (STATIC / "birch-trunk.svg").write_text(tile, encoding="utf-8")
    (STATIC / "soil-line.svg").write_text(birch.soil_line(), encoding="utf-8")
    crown = birch.crown() + birch.falling()
    roots = birch.roots()
    index = STATIC / "index.html"
    html = index.read_text(encoding="utf-8")
    html = inject(html, "crown", crown)
    html = inject(html, "roots", roots)
    index.write_text(html, encoding="utf-8")
    print(f"ствол {len(tile)} Б, крона {len(crown)} Б, корни {len(roots)} Б")


if __name__ == "__main__":
    main()
