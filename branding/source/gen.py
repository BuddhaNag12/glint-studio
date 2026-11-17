"""Glint Studio monogram generator: a brush-pen 'g' flowing into a 't'.

Each stroke is a centreline of cubic Béziers in a 256 design grid (y down, upright).
The centreline is slanted, sampled, given a brush width (thick going down, hairline going up,
with per-segment overrides), then outlined into filled polygons. Output: flat filled paths only.
"""
import math, os, sys

OUT = os.path.dirname(os.path.abspath(__file__))
NAVY, WHITE, BLUE, DEEP, PAPER = "#0f172a", "#ffffff", "#3b82f6", "#1d4ed8", "#f8fafc"

SLANT = math.tan(math.radians(10))   # lean right
BASE_Y = 140                          # slant pivot (baseline)
THICK, HAIR = 22.0, 3.2


def bez(p, t):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = p
    u = 1 - t
    return (u**3*x0 + 3*u*u*t*x1 + 3*u*t*t*x2 + t**3*x3,
            u**3*y0 + 3*u*u*t*y1 + 3*u*t*t*y2 + t**3*y3)


def slant(pt):
    x, y = pt
    return (x + (BASE_Y - y) * SLANT, y)


def smooth01(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def sample(segs, step=0.7):
    """segs: list of dicts {p:(p0,c1,c2,p3), w: None|num|(a,b)}. Returns list of samples."""
    out = []
    for i, s in enumerate(segs):
        p = s["p"]
        approx = sum(math.dist(p[k], p[k + 1]) for k in range(3))
        n = max(6, int(approx / step))
        for k in range(0 if i == 0 else 1, n + 1):
            t = k / n
            out.append({"pt": slant(bez(p, t)), "seg": i, "t": t})
    # tangents + arc length
    L = 0.0
    for j, s in enumerate(out):
        a = out[max(0, j - 1)]["pt"]; b = out[min(len(out) - 1, j + 1)]["pt"]
        dx, dy = b[0] - a[0], b[1] - a[1]
        d = math.hypot(dx, dy) or 1
        s["tan"] = (dx / d, dy / d)
        if j:
            L += math.dist(out[j - 1]["pt"], s["pt"])
        s["s"] = L
    return out


def widths(samples, segs, taper=(0, 0), pinches=(), thick=THICK, hair=HAIR, smooth=6.0):
    total = samples[-1]["s"]
    w = []
    for s in samples:
        spec = segs[s["seg"]].get("w")
        if spec is None:
            down = max(0.0, s["tan"][1])
            v = hair + (thick - hair) * down ** 1.6
        elif isinstance(spec, tuple):
            v = spec[0] + (spec[1] - spec[0]) * smooth01(s["t"])
        else:
            v = spec
        w.append(v)
    # moving average over arc length so segment joins never jump
    sm = []
    for j, s in enumerate(samples):
        acc = cnt = 0.0
        k = j
        while k >= 0 and s["s"] - samples[k]["s"] <= smooth:
            acc += w[k]; cnt += 1; k -= 1
        k = j + 1
        while k < len(samples) and samples[k]["s"] - s["s"] <= smooth:
            acc += w[k]; cnt += 1; k += 1
        sm.append(acc / cnt)
    ts, te = taper
    for j, s in enumerate(samples):
        f = 1.0
        if ts: f *= max(0.06, min(1.0, s["s"] / ts)) ** 0.55
        if te: f *= max(0.03, min(1.0, (total - s["s"]) / te)) ** 0.55
        for (seg_boundary, radius, floor) in pinches:
            # pinch centred on the start of segment `seg_boundary`
            c = next(x["s"] for x in samples if x["seg"] == seg_boundary)
            f *= floor + (1 - floor) * smooth01(abs(s["s"] - c) / radius)
        sm[j] *= f
    return sm


def outline(samples, w, a, b):
    """Polygon for samples[a:b+1]."""
    left, right = [], []
    for j in range(a, b + 1):
        (x, y), (tx, ty), h = samples[j]["pt"], samples[j]["tan"], w[j] / 2
        left.append((x - ty * h, y + tx * h))
        right.append((x + ty * h, y - tx * h))
    return left + right[::-1]


def seg_range(samples, s0, s1, pad=2):
    idx = [j for j, s in enumerate(samples) if s0 <= s["seg"] <= s1]
    return max(0, idx[0] - pad), min(len(samples) - 1, idx[-1] + pad)


def star(cx, cy, R, r, k):
    return [("M", (cx, cy - R)), ("Q", (cx + k, cy - k), (cx + r, cy)), ("Q", (cx + k, cy + k), (cx, cy + R)),
            ("Q", (cx - k, cy + k), (cx - r, cy)), ("Q", (cx - k, cy - k), (cx, cy - R))]


# ---------------------------------------------------------------- the letters

def bowl_segs():
    return [
        {"p": ((121, 80), (114, 71), (104, 68), (93, 68))},
        {"p": ((93, 68), (75, 68), (62, 85), (62, 104))},
        {"p": ((62, 104), (62, 124), (75, 140), (92, 140))},
        {"p": ((92, 140), (110, 140), (122, 124), (123, 104))},
        {"p": ((123, 104), (124, 90), (125, 78), (126, 66))},
    ]


STEM = {"p": ((126, 66), (126, 100), (126, 150), (126, 176))}
TSTEM = {"p": ((178, 132), (179, 104), (180, 70), (181, 34)), "w": 15.0}
CROSSBAR = [{"p": ((163, 77), (174, 75), (188, 73), (202, 70)), "w": 6.0}]
HOOK = {"p": ((126, 176), (126, 196), (121, 210), (110, 210))}
LOOP_UP = {"p": ((110, 210), (99, 210), (93, 200), (94, 190))}


def climb(p0, c1, c2):
    return {"p": (p0, c1, c2, TSTEM["p"][0]), "w": (HAIR, 15.0)}


def variant(name):
    """Returns (segs, chunks[(seg0, seg1, colour_key)], pinches)."""
    if name == "loop":          # A: cursive descender loop, crosses the stem once
        segs = [STEM, HOOK, LOOP_UP,
                {"p": ((94, 190), (95, 176), (118, 168), (142, 166))},
                climb((142, 166), (160, 164), (177, 152)), TSTEM]
        return segs, [(0, 2, "ink"), (3, 5, "ink")], []
    if name == "eight":         # B: loop under the g, a second loop under the t (an 8 on its side)
        segs = [STEM, HOOK, LOOP_UP,
                {"p": ((94, 190), (95, 176), (124, 170), (148, 176))},
                {"p": ((148, 176), (168, 181), (178, 200), (166, 210))},
                {"p": ((166, 210), (156, 218), (142, 210), (148, 196))},
                {"p": ((148, 196), (154, 180), (172, 166), (178, 150)), "w": (HAIR, 13.0)},
                {"p": ((178, 150), (178, 145), (178, 138), (178, 132)), "w": (13.0, 15.0)}, TSTEM]
        return segs, [(0, 2, "ink"), (3, 4, "ink"), (5, 8, "ink")], []
    if name == "ribbon":        # C: same path as A, the loop shows the ribbon's back
        segs = [STEM, HOOK,
                dict(LOOP_UP, w=(9.0, 12.0)),
                {"p": ((94, 190), (95, 176), (118, 168), (142, 166)), "w": (12.0, 9.0)},
                climb((142, 166), (160, 164), (177, 152)), TSTEM]
        return segs, [(0, 1, "ink"), (2, 3, "back"), (4, 5, "ink")], [(2, 6, 0.03), (4, 6, 0.03)]
    if name == "minimal":       # D: a slim teardrop beside the descender, barely a twist
        segs = [STEM,
                {"p": ((126, 176), (126, 198), (124, 214), (117, 214))},
                {"p": ((117, 214), (110, 214), (108, 204), (111, 192))},
                {"p": ((111, 192), (114, 180), (124, 170), (142, 166))},
                climb((142, 166), (160, 164), (177, 152)), TSTEM]
        return segs, [(0, 2, "ink"), (3, 5, "ink")], []
    raise SystemExit(f"unknown variant {name}")


# ---------------------------------------------------------------- build + emit

def build(name, small=False):
    thick, hair = (THICK + 4, 7.0) if small else (THICK, HAIR)
    shapes = []  # (colour_key, polygon points | star cmds)

    bs = bowl_segs()
    smp = sample(bs)
    w = widths(smp, bs, taper=(12, 0), thick=thick, hair=hair)
    shapes.append(("ink", outline(smp, w, 0, len(smp) - 1)))

    segs, chunks, pinches = variant(name)
    if small:
        segs = [dict(s, w=(max(s["w"][0], 8), s["w"][1] + 3)) if isinstance(s.get("w"), tuple) else s for s in segs]
        pinches = [(b, r, max(f, 0.45)) for b, r, f in pinches]
    smp = sample(segs)
    w = widths(smp, segs, taper=(0, 22), pinches=pinches, thick=thick, hair=hair)
    # round top of the stem where the bowl's hairline meets it
    x0, y0 = smp[0]["pt"]
    shapes.append(("ink", circle_pts(x0, y0 + w[0] * 0.15, w[0] / 2)))
    order = sorted(range(len(chunks)), key=lambda i: chunks[i][2] == "back")  # back side drawn on top
    for i in order:
        a, b = seg_range(smp, chunks[i][0], chunks[i][1], pad=0 if pinches else 2)
        shapes.append((chunks[i][2], outline(smp, w, a, b)))

    cb = CROSSBAR
    if small:
        cb = [dict(cb[0], w=11.0)]
    smp2 = sample(cb)
    w2 = widths(smp2, cb, taper=(7, 10), thick=thick, hair=hair, smooth=1)
    shapes.append(("ink", outline(smp2, w2, 0, len(smp2) - 1)))

    # star: flicked off the tip, along the stroke's direction
    tip = smp[-1]["pt"]; tx, ty = smp[-1]["tan"]
    R = 24 if small else 19
    d = R + 3
    cx, cy = tip[0] + tx * d + 4, tip[1] + ty * d
    shapes.append(("star", star(cx, cy, R, R * 0.72, R * (0.2 if small else 0.13))))
    return shapes


def circle_pts(cx, cy, r, n=48):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def bbox(shapes):
    xs, ys = [], []
    for _, s in shapes:
        pts = s if not isinstance(s[0][0], str) else [p for c in s for p in c[1:]]
        xs += [p[0] for p in pts]; ys += [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def to_d(s, f):
    if isinstance(s[0][0], str):
        out = []
        for c in s:
            out.append(c[0] + " ".join(f"{f(p)[0]:.1f} {f(p)[1]:.1f}" for p in c[1:]))
        return "".join(out) + "Z"
    pts = [f(p) for p in s]
    return "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in pts) + "Z"


def svg(shapes, size, box, colours, bg=None, radius=0, title="Glint Studio"):
    x0, y0, x1, y1 = bbox(shapes)
    scale = box / max(x1 - x0, y1 - y0)
    ox = (size - (x1 - x0) * scale) / 2 - x0 * scale
    oy = (size - (y1 - y0) * scale) / 2 - y0 * scale - size * 0.01   # optical: a hair above centre
    f = lambda p: (p[0] * scale + ox, p[1] * scale + oy)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img">',
             f"<title>{title}</title>"]
    if bg:
        parts.append(f'<rect width="{size}" height="{size}" rx="{radius}" fill="{bg}"/>')
    for key, s in shapes:
        parts.append(f'<path fill="{colours[key]}" d="{to_d(s, f)}"/>')
    parts.append("</svg>")
    return "\n".join(parts)


if __name__ == "__main__":
    names = {"a": "loop", "b": "eight", "c": "ribbon", "d": "minimal"}
    ver = sys.argv[1] if len(sys.argv) > 1 else "v1"
    os.makedirs(os.path.join(OUT, ver), exist_ok=True)
    for letter, n in names.items():
        sh = build(n)
        black = {"ink": "#000", "back": "#000", "star": "#000"}
        tile = {"ink": WHITE, "back": DEEP, "star": BLUE}
        base = os.path.join(OUT, ver, f"{letter}-{n}")
        open(base + "-mark.svg", "w").write(svg(sh, 256, 220, black))
        open(base + "-tile.svg", "w").write(svg(sh, 1024, 820, tile, bg=NAVY, radius=184))
        sh_small = build(n, small=True)
        open(base + "-tile-small.svg", "w").write(svg(sh_small, 1024, 840, tile, bg=NAVY, radius=184))
    print("ok", ver)
