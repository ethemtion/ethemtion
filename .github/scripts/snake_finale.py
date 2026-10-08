#!/usr/bin/env python3
"""Platane/snk'nin ürettiği yılan SVG'sini son işlemden geçirir.

1. Alttaki "yenenler" çubuğunu her an renk sırasına göre (az katkıdan çok
   katkıya) dizer. snk kareleri yenme sırasına göre dizdiği için tonlar karışıyordu.
2. Yılan bütün kareleri yedikten sonra topladığı kareleri harcayarak ızgaraya
   ETHEMTION yazar (çubuk harcandıkça kısalır). Kare yetmezse ETO yazar,
   o da yetmezse sadece çubuğu düzeltir.

Kullanım:  python3 snake_finale.py yilan.svg [yilan-dark.svg ...]

Bir dosyada beklenmedik bir şey olursa o dosyaya dokunmaz; o zaman snk'nin
orijinal çıktısı yayınlanır. Sadece Python standart kütüphanesini kullanır.
"""
import heapq
import os
import re
import sys

STEP_MS = 100        # snk'nin adım süresi (action'da sabit)
WRITE_STEP_MS = 80   # yılan yazı yazarken adım süresi
EXIT_STEP_MS = 60    # yazı bitince sahneden çıkarken adım süresi
HOLD_MS = 4000       # yazının tek başına ekranda kalma süresi
FADE_MS = 800        # döngü başa dönerken yazıdan ızgaraya geçiş
EPS_MS = 4           # "anlık" renk/boy değişimlerinin süresi
SLACK = 2            # bir harfi en fazla kaç adım uzatarak sonrakine yaklaşabilir
MAX_POPS = 400_000   # SLACK'li arama bu kadar durumu aşarsa SLACK=0 ile tekrar dene
MARK = "snake-finale"

TEXTS = ("ETHEMTION", "ETO")

# 5x7 piksel harfler (HD44780 LCD fontu; I daha dar).
GLYPHS = {
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "I": ("###", ".#.", ".#.", ".#.", ".#.", ".#.", "###"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "N": ("#...#", "#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"),
}

DIRS = ((1, 0), (0, -1), (-1, 0), (0, 1))  # sağ, yukarı, sol, aşağı (eşitlikte bu sıra)


class SnkFormatError(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise SnkFormatError(msg)


# ---------------------------------------------------------------------------
# snk çıktısını okuma
# ---------------------------------------------------------------------------
def css_blocks(css):
    """Üst seviyedeki `başlık{gövde}` bloklarını sırayla döndürür."""
    out, depth, start, head, body_start = [], 0, 0, None, 0
    for i, ch in enumerate(css):
        if ch == "{":
            if depth == 0:
                head, body_start = css[start:i].strip(), i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            need(depth >= 0, "css: fazladan }")
            if depth == 0:
                out.append((head, css[body_start:i]))
                start = i + 1
    need(depth == 0 and not css[start:].strip(), "css: kapanmamış blok")
    return out


def to_int(v):
    r = round(v)
    need(abs(v - r) < 1e-6, f"tam sayı bekleniyordu: {v}")
    return r


def parse(svg):
    m = re.match(r'<svg viewBox="([^"]+)" width="([\d.]+)" height="([\d.]+)" '
                 r'xmlns="http://www.w3.org/2000/svg">', svg)
    need(m, "svg başlığı tanınmadı")
    vb = [float(v) for v in m.group(1).split()]
    need(len(vb) == 4 and vb[0] < 0 and vb[1] == 2 * vb[0], "viewBox beklenmedik")
    cell = -vb[0]

    sm = re.search(r"<style>(.*?)</style>", svg, re.S)
    need(sm, "style yok")
    keyframes, rules, root = {}, {}, []
    for head, body in css_blocks(sm.group(1)):
        if head.startswith("@keyframes "):
            frames = []
            for sel, decl in css_blocks(body):
                for p in sel.split(","):
                    need(p.strip().endswith("%"), "keyframe yüzdesi yok")
                    frames.append((float(p.strip()[:-1]), decl.strip()))
            keyframes[head.split()[1]] = frames
        elif head == ":root" or head.startswith("@media"):
            root.append(f"{head}{{{body}}}")
        else:
            rules[head] = body
    for base in (".c", ".u", ".s"):
        need(base in rules, f"{base} kuralı yok")

    dm = re.search(r"(\d+)ms", rules[".c"])
    need(dm, "süre yok")
    duration = int(dm.group(1))
    need(duration % STEP_MS == 0, "süre adımın katı değil")
    steps = duration // STEP_MS
    need(1 <= steps <= 4000, "adım sayısı beklenmedik")
    dm = re.search(r"(?<![\w-])width:([\d.]+)px", rules[".c"])  # stroke-width değil
    need(dm, "hücre boyu yok")
    dot = float(dm.group(1))
    margin = (cell - dot) / 2

    body = svg[sm.end():svg.rindex("</svg>")]
    rects = [dict(re.findall(r'([\w-]+)="([^"]*)"', r)) for r in re.findall(r"<rect ([^>]*)/>", body)]
    need(len(rects) == body.count("<"), "rect dışında eleman var")

    cells, snake_rects, bar_y = [], [], None
    for r in rects:
        cls = r.get("class", "").split()
        if cls[:1] == ["c"]:
            c = {"col": to_int((float(r["x"]) - margin) / cell),
                 "row": to_int((float(r["y"]) - margin) / cell),
                 "rx": r.get("rx", "0"), "color": 0, "step": None}
            if len(cls) == 2:
                cid = cls[1]
                cm = re.search(r"fill:var\(--c(\d+)\)", rules.get(f".c.{cid}", ""))
                need(cm, f"{cid} rengi yok")
                c["color"] = int(cm.group(1))
                frames = keyframes[cid]
                on = [p for p, d in frames if d == f"fill:var(--c{c['color']})"]
                off = [p for p, d in frames if d == "fill:var(--ce)"]
                need(on and off, f"{cid} yenme anı yok")
                t = (max(on) + min(off)) / 2 / 100 * steps
                need(abs(t - round(t)) < 0.25, f"{cid} yenme anı adıma oturmuyor")
                c["step"] = round(t)
            cells.append(c)
        elif cls[:1] == ["s"]:
            snake_rects.append(r)
        elif cls[:1] == ["u"]:
            bar_y = float(r["y"])
        else:
            raise SnkFormatError(f"tanınmayan rect: {cls}")
    need(cells and snake_rects, "hücre ya da yılan yok")

    parts = []
    for r in snake_rects:
        sid = r["class"].split()[1]
        pts = {}
        for p, decl in keyframes[sid]:
            tm = re.fullmatch(r"transform:translate\((-?[\d.]+)px,(-?[\d.]+)px\)", decl)
            need(tm, "yılan konumu okunamadı")
            j = p / 100 * steps
            need(abs(j - round(j)) < 0.25, "yılan karesi adıma oturmuyor")
            if round(j) < steps:
                pts[round(j)] = (float(tm.group(1)) / cell, float(tm.group(2)) / cell)
        keys = sorted(pts)
        need(keys[0] == 0 and keys[-1] == steps - 1, "yılan zaman çizelgesi eksik")
        pos = []
        for a, b in zip(keys, keys[1:]):
            (ax, ay), (bx, by) = pts[a], pts[b]
            for j in range(a, b):
                k = (j - a) / (b - a)
                pos.append((to_int(ax + (bx - ax) * k), to_int(ay + (by - ay) * k)))
        pos.append((to_int(pts[keys[-1]][0]), to_int(pts[keys[-1]][1])))
        parts.append(pos)

    states = [tuple(part[j] for part in parts) for j in range(steps)]
    check_moves(states)
    for c in cells:
        if c["step"] is not None:
            need(states[c["step"]][0] == (c["col"], c["row"]), "yenme anında baş hücrede değil")

    width = max(c["col"] for c in cells) + 1
    height = max(c["row"] for c in cells) + 1
    return {
        "header": m.group(0), "viewbox": vb, "cell": cell, "dot": dot,
        "root": root, "rules": rules, "cells": cells, "snake_rects": snake_rects,
        "states": states, "width": width, "height": height,
        "bar_y": bar_y if bar_y is not None else (height + 2) * cell,
    }


def check_moves(states):
    """Her adımda baş bir kare ilerler, geri dönmez; gövde başı izler."""
    for j in range(1, len(states)):
        (hx, hy), prev = states[j][0], states[j - 1]
        need(abs(hx - prev[0][0]) + abs(hy - prev[0][1]) == 1, f"adım {j}: baş sıçradı")
        need(len(prev) < 2 or states[j][0] != prev[1], f"adım {j}: yılan geri döndü")
        need(states[j][1:] == prev[:-1], f"adım {j}: gövde başı izlemiyor")


# ---------------------------------------------------------------------------
# Yazı: yerleşim ve yılanın rotası
# ---------------------------------------------------------------------------
def place_text(cells, width, height, available):
    """Kare sayısı yeten ve ızgaraya sığan ilk yazıyı ortalanmış olarak yerleştirir."""
    if height != 7:
        return None
    exists = {(c["col"], c["row"]) for c in cells}
    for text in TEXTS:
        glyphs = [GLYPHS[ch] for ch in text]
        text_w = sum(len(g[0]) for g in glyphs) + len(glyphs) - 1
        ink_n = sum(row.count("#") for g in glyphs for row in g)
        if ink_n > available or text_w > width:
            continue
        center = (width - text_w) // 2
        for off in sorted(range(width - text_w + 1), key=lambda o: (abs(o - center), o)):
            letters, x = [], off
            for g in glyphs:
                letters.append([(x + i, r) for r, row in enumerate(g)
                                for i, ch in enumerate(row) if ch == "#"])
                x += len(g[0]) + 1
            if all(p in exists for ink in letters for p in ink):
                return text, letters
    return None


def manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def write_letter(pos, d, ink, inside, goal_score, slack=SLACK, max_pops=MAX_POPS):
    """Baştan itibaren harfin bütün piksellerinden geçen en kısa yolu bulur (A*).

    Yılan geri dönemez; eşit uzunluktakiler arasında daha az dönüşlü olan seçilir.
    En kısa yoldan en çok `slack` adım uzun olanlar da değerlendirilir ve
    goal_score (sonraki harfe/çıkışa uzaklık) ile toplamı en küçük olan alınır.
    Arama max_pops durumu aşarsa None döner.
    """
    index = {p: i for i, p in enumerate(ink)}
    full = (1 << len(ink)) - 1
    cache = {}

    def h(p, mask):  # kalan piksel sayısı + en yakın kalan piksele (uzaklık - 1)
        key = (p, mask)
        if key not in cache:
            rest = [q for i, q in enumerate(ink) if not mask >> i & 1]
            cache[key] = len(rest) + max(0, min(manhattan(p, q) for q in rest) - 1) if rest else 0
        return cache[key]

    start = (pos, d, 0)
    best = {start: (0, 0)}
    parent = {start: None}
    heap = [(h(pos, 0), 0, 0, pos, d, 0)]
    goals, limit, pops = [], None, 0
    while heap:
        pops += 1
        if pops > max_pops:
            return None
        f, g, turns, p, pd, mask = heapq.heappop(heap)
        if limit is not None and f > limit:
            break
        if best[(p, pd, mask)] != (g, turns):
            continue
        if mask == full:
            goals.append((g, turns, p, pd))
            if limit is None:
                limit = g + slack
            continue
        for nd in DIRS:
            if pd is not None and nd == (-pd[0], -pd[1]):
                continue
            np = (p[0] + nd[0], p[1] + nd[1])
            if not inside(np):
                continue
            nmask = mask | (1 << index[np]) if np in index else mask
            key = (np, nd, nmask)
            cost = (g + 1, turns + (nd != pd))
            if key not in best or cost < best[key]:
                best[key] = cost
                parent[key] = (p, pd, mask)
                heapq.heappush(heap, (cost[0] + h(np, nmask), *cost, np, nd, nmask))
    need(goals, "harf için yol bulunamadı")
    g, turns, p, pd = min(goals, key=lambda s: (s[0] + goal_score(s[2]), s[1], s[0], s[2]))
    path, key = [], (p, pd, full)
    while parent[key] is not None:
        path.append(key[0])
        key = parent[key]
    return path[::-1], pd


def exit_route(pos, d, width, height):
    """Yazı bitince yılanı en kısa yoldan görünür alanın dışına çıkarır."""
    def gate(p, nd):
        return ((nd == (1, 0) and p[0] == width + 1) or (nd == (0, -1) and p[1] == -3)
                or (nd == (-1, 0) and p[0] == -2))

    start = (pos, d)
    parent = {start: None}
    queue, end = [start], None
    for p, pd in queue:  # BFS (liste ilerledikçe büyür)
        for nd in DIRS:
            if pd is not None and nd == (-pd[0], -pd[1]):
                continue
            np = (p[0] + nd[0], p[1] + nd[1])
            if not (-2 <= np[0] <= width + 1 and -3 <= np[1] <= height) or (np, nd) in parent:
                continue
            parent[(np, nd)] = (p, pd)
            if gate(np, nd):
                end = (np, nd)
                break
            queue.append((np, nd))
        if end:
            break
    need(end, "çıkış yolu bulunamadı")
    path, key = [], end
    while parent[key] is not None:
        path.append(key[0])
        key = parent[key]
    path.reverse()
    (x, y), (dx, dy) = end
    path += [(x + dx * i, y + dy * i) for i in (1, 2, 3)]  # kuyruk da çıksın
    return path


# ---------------------------------------------------------------------------
# Yeni zaman çizelgesi
# ---------------------------------------------------------------------------
_timelines = {}


def build_timeline(model):
    """Açık ve koyu dosya aynı rotayı paylaşır; rota bir kez hesaplanır."""
    key = (tuple(model["states"]), model["width"], model["height"],
           tuple((c["col"], c["row"], c["step"]) for c in model["cells"]))
    if key not in _timelines:
        _timelines[key] = _build_timeline(model)
    return _timelines[key]


def _build_timeline(model):
    cells, states0 = model["cells"], model["states"]
    eaten = [c for c in cells if c["step"] is not None]
    placed = place_text(cells, model["width"], model["height"], len(eaten)) if eaten else None
    if placed is None:
        # Yazı yok: snk'nin rotası aynen kalır, sadece çubuk sıralanır.
        times = [j * STEP_MS for j in range(len(states0))]
        return {"text": None, "states": states0, "times": times,
                "duration": len(states0) * STEP_MS, "paint": {}}

    text, letters = placed
    last = max(c["step"] for c in eaten)
    states = list(states0[:last + 1])
    times = [j * STEP_MS for j in range(last + 1)]
    (hx, hy), (nx, ny) = states[-1][0], states[-1][1]
    d = (hx - nx, hy - ny)
    w, hgt = model["width"], model["height"]

    def inside(p):
        return -1 <= p[0] <= w and -1 <= p[1] <= hgt

    def step_to(p, ms):
        states.append((p,) + states[-1][:-1])
        times.append(times[-1] + ms)

    paint = {}
    for i, ink in enumerate(letters):
        if i + 1 < len(letters):
            nxt = letters[i + 1]
            score = lambda p, nxt=nxt: min(manhattan(p, q) for q in nxt)  # noqa: E731
        else:
            score = lambda p: min(w + 1 - p[0], p[1] + 3, p[0] + 2)  # noqa: E731
        found = (write_letter(states[-1][0], d, ink, inside, score)
                 or write_letter(states[-1][0], d, ink, inside, score, slack=0, max_pops=10 * MAX_POPS))
        need(found, "harf rotası çok uzun sürdü")
        path, d = found
        todo = set(ink)
        for p in path:
            step_to(p, WRITE_STEP_MS)
            if p in todo:
                todo.discard(p)
                paint[p] = len(states) - 1
    for p in exit_route(states[-1][0], d, w, hgt):
        step_to(p, EXIT_STEP_MS)

    duration = times[-1] + HOLD_MS + FADE_MS
    return {"text": text, "states": states, "times": times,
            "duration": duration, "paint": paint, "letters": letters}


def verify(model, tl):
    check_moves(tl["states"])
    times = tl["times"]
    need(all(b > a for a, b in zip(times, times[1:])), "zaman geri gidiyor")
    if tl["text"]:
        ink = {p for letter in tl["letters"] for p in letter}
        need(set(tl["paint"]) == ink, "yazının her pikseli boyanmadı")
        last_eat = max(c["step"] for c in model["cells"] if c["step"] is not None)
        need(min(tl["paint"].values()) > last_eat, "yazı yemek bitmeden başladı")
        vb = model["viewbox"]
        cell = model["cell"]
        for x, y in tl["states"][-1]:
            visible = (vb[0] <= x * cell < vb[0] + vb[2]) and (vb[1] <= y * cell < vb[1] + vb[3])
            need(not visible, "yılan sahneden çıkmadı")


# ---------------------------------------------------------------------------
# SVG yazma
# ---------------------------------------------------------------------------
def pct(ms, total):
    return f"{ms * 100 / total:.4f}".rstrip("0").rstrip(".") + "%"


def keyframes_css(name, frames, total, fmt, lerp=None):
    """frames: [(ms, değer)]. Arada kalan gereksiz kareleri atar, aynı stilleri birleştirir.

    lerp verilirse, komşu iki kareden doğrusal olarak çıkan kareler de atılır
    (yılan düz giderken her adımı yazmaya gerek yok; tarayıcı arayı doldurur).
    """
    for (a, _), (b, _) in zip(frames, frames[1:]):
        need(b > a, f"{name}: kareler sıralı değil")
    keep = [frames[0]]
    for i in range(1, len(frames) - 1):
        (ta, va), (t, v), (tb, vb) = keep[-1], frames[i], frames[i + 1]
        if v == va == vb:
            continue
        if lerp and all(abs(a - b) < 1e-9 for a, b in zip(lerp(va, vb, (t - ta) / (tb - ta)), v)):
            continue
        keep.append(frames[i])
    keep.append(frames[-1])
    groups = {}
    for t, v in keep:
        groups.setdefault(fmt(v), []).append(pct(t, total))
    body = "".join(",".join(ps) + "{" + style + "}" for style, ps in groups.items())
    return f"@keyframes {name}{{{body}}}"


def b36(n):
    s = ""
    while True:
        n, r = divmod(n, 36)
        s = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + s
        if not n:
            return s


def render(model, tl):
    cell, total = model["cell"], tl["duration"]
    times, states, paint = tl["times"], tl["states"], tl["paint"]
    finale = tl["text"] is not None
    base = {k: re.sub(r"\d+ms", f"{total}ms", model["rules"][k]) for k in (".c", ".u", ".s")}
    css = list(model["root"]) + [f".c{{{base['.c']}}}"]
    svg = []

    # --- ızgara ---
    eaten = [c for c in model["cells"] if c["step"] is not None]
    # yazı her zaman paletin en koyu/parlak tonuyla (--c4) yazılır
    top = max(int(n) for n in re.findall(r"--c(\d+):", "".join(model["root"])) or [1])
    margin = (cell - model["dot"]) / 2
    n = 0
    for c in model["cells"]:
        start = f"var(--c{c['color']})" if c["color"] else "var(--ce)"
        events = []
        if c["step"] is not None:
            events.append((times[c["step"]], "var(--ce)"))
        if (c["col"], c["row"]) in paint:
            events.append((times[paint[(c["col"], c["row"])]], f"var(--c{top})"))
        attrs = (f'x="{c["col"] * cell + margin:g}" y="{c["row"] * cell + margin:g}" '
                 f'rx="{c["rx"]}" ry="{c["rx"]}"')
        if not events:
            svg.append(f'<rect class="c" {attrs}/>')
            continue
        cid = "c" + b36(n)
        n += 1
        frames, cur = [(0, start)], start
        for t, v in events:
            frames += [(t - EPS_MS, cur), (t + EPS_MS, v)]
            cur = v
        if finale:
            frames += [(total - FADE_MS, cur), (total, start)]
        else:
            frames.append((total, cur))
        css.append(keyframes_css(cid, frames, total, fmt=lambda v: f"fill:{v}"))
        css.append(f".c.{cid}{{fill:{start};animation-name:{cid}}}")
        svg.append(f'<rect class="c {cid}" {attrs}/>')

    # --- çubuk: her renk tek parça, her an az katkıdan çok katkıya sıralı ---
    css.append(f".u{{{base['.u']}}}")
    if eaten:
        colors = sorted({c["color"] for c in eaten})
        totals = {k: sum(1 for c in eaten if c["color"] == k) for k in colors}
        unit = model["width"] * cell / len(eaten)
        events = sorted([(times[c["step"]], c["color"]) for c in eaten]
                        + [(times[s], None) for s in paint.values()])
        count = dict.fromkeys(colors, 0)

        def layout():
            out, x = {}, 0.0
            for k in colors:
                w = count[k] * unit
                scale = (w + 0.6) / (totals[k] * unit + 0.6) if count[k] else 0.0
                out[k] = (round(x, 1), round(scale, 4))
                x += w
            return out

        frames = {k: [(0, (0.0, 0.0))] for k in colors}
        cur = layout()
        for t, color in events:
            if color is None:  # yazıya harcanan kare: en koyu uçtan
                color = max(k for k in colors if count[k])
                count[color] -= 1
            else:
                count[color] += 1
            new = layout()
            for k in colors:
                if new[k] != cur[k]:
                    frames[k] += [(t - EPS_MS, cur[k]), (t + EPS_MS, new[k])]
            cur = new
        need(all(v >= 0 for v in count.values()), "çubuk eksiye düştü")
        for k in colors:
            # geçişte bütün parçalar x=0'a doğru aynı oranda küçülür; çubuk bölünmez
            end = [(total - FADE_MS, cur[k]), (total, (0.0, 0.0))] if finale else [(total, cur[k])]
            css.append(keyframes_css(f"u{k}", frames[k] + end, total,
                                     fmt=lambda v: f"transform:translate({v[0]:g}px,0) scale({v[1]:g},1)"))
            css.append(f".u.u{k}{{fill:var(--c{k});animation-name:u{k}}}")
            svg.append(f'<rect class="u u{k}" height="{model["dot"]:g}" '
                       f'width="{totals[k] * unit + 0.6:.1f}" x="0" y="{model["bar_y"]:g}"/>')

    # --- yılan ---
    css.append(f".s{{{base['.s']}}}")

    def lerp(a, b, k):
        return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)

    def where(v):
        return f"transform:translate({v[0] * cell:g}px,{v[1] * cell:g}px)"

    for i, r in enumerate(model["snake_rects"]):
        sid = f"s{i}"
        frames = [(t, s[i]) for t, s in zip(times, states)] + [(total, states[-1][i])]
        css.append(keyframes_css(sid, frames, total, fmt=where, lerp=lerp))
        x0, y0 = states[0][i]
        css.append(f".s.{sid}{{transform:translate({x0 * cell:g}px,{y0 * cell:g}px);animation-name:{sid}}}")
        a = {k: v for k, v in r.items() if k != "class"}
        svg.append(f'<rect class="s {sid}" ' + " ".join(f'{k}="{v}"' for k, v in a.items()) + "/>")

    note = f" ({MARK}: {tl['text']})" if finale else f" ({MARK})"
    return (model["header"] + f"<desc>Generated with https://github.com/Platane/snk{note}</desc>"
            + "<style>" + "".join(css) + "</style>" + "".join(svg) + "</svg>")


def say(msg):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # UTF-8 olmayan konsol
        print(msg.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def process(path):
    """Tek dosyayı işler; hiçbir durumda istisna fırlatmaz, dosyayı yarım bırakmaz."""
    tmp = path + ".tmp"
    try:
        with open(path, encoding="utf-8") as f:
            svg = f.read()
        if MARK in svg:
            say(f"{path}: zaten işlenmiş, atlandı")
            return True
        model = parse(svg)
        if not any(c["step"] is not None for c in model["cells"]):
            say(f"{path}: hiç dolu kare yok, dokunulmadı")
            return True
        tl = build_timeline(model)
        verify(model, tl)
        out = render(model, tl)
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(out)
        os.replace(tmp, path)  # ya hep ya hiç: yarım yazılmış dosya yayınlanmaz
    except Exception as e:  # ne olursa olsun orijinal yılan yayında kalsın
        try:
            os.remove(tmp)
        except OSError:
            pass
        say(f"::warning::{path}: {e!r} — dosyaya dokunulmadı (orijinal yılan kalıyor)")
        return False
    eaten = sum(1 for c in model["cells"] if c["step"] is not None)
    yazi = tl["text"] or "yok (kare yetmedi)"
    say(f"{path}: {eaten} kare, yazı: {yazi}, döngü {tl['duration'] / 1000:.1f} sn")
    return True


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    for p in sys.argv[1:]:
        process(p)
