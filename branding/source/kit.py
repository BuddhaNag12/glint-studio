"""Glint Studio logo kit: direction A (single loop), every size cut and colourway.

Usage: kit.py OUT_DIR PLEX_MEDIUM_TTF   (run with the venv python that has fontTools)
"""
import math, os, sys
from gen import sample, widths, outline, seg_range, star, circle_pts, NAVY, WHITE, BLUE, PAPER

# Size cuts: hairlines, star and crossbar grow as the icon shrinks so the twist and star survive.
CUTS = {
    "full": dict(thick=22, hair=3.2, tstem=15, cross=6, R=19, waist=0.13, box=0.80),
    "32":   dict(thick=27, hair=8.5, tstem=19, cross=11, R=27, waist=0.22, box=0.84),
    "16":   dict(thick=31, hair=13, tstem=22, cross=15, R=33, waist=0.28, box=0.86),
}


def build(c):
    hair, tst = c["hair"], c["tstem"]
    bowl = [
        {"p": ((121, 80), (114, 71), (104, 68), (93, 68))},
        {"p": ((93, 68), (75, 68), (62, 85), (62, 104))},
        {"p": ((62, 104), (62, 124), (75, 140), (92, 140))},
        {"p": ((92, 140), (110, 140), (122, 124), (123, 104))},
        {"p": ((123, 104), (124, 90), (125, 78), (126, 66))},
    ]
    tail = [
        {"p": ((126, 66), (126, 100), (126, 150), (126, 176))},
        {"p": ((126, 176), (126, 196), (121, 210), (110, 210))},
        {"p": ((110, 210), (99, 210), (93, 200), (94, 190))},
        {"p": ((94, 190), (95, 176), (118, 168), (142, 166))},
        {"p": ((142, 166), (160, 164), (177, 152), (178, 132)), "w": (hair, tst)},
        {"p": ((178, 132), (179, 104), (180, 70), (181, 34)), "w": float(tst)},
    ]
    cross = [{"p": ((163, 77), (174, 75), (188, 73), (202, 70)), "w": float(c["cross"])}]
    shapes = []
    smp = sample(bowl)
    w = widths(smp, bowl, taper=(12, 0), thick=c["thick"], hair=hair)
    shapes.append(("ink", outline(smp, w, 0, len(smp) - 1)))
    smp = sample(tail)
    w = widths(smp, tail, taper=(0, 22), thick=c["thick"], hair=hair)
    x0, y0 = smp[0]["pt"]
    shapes.append(("ink", circle_pts(x0, y0 + w[0] * 0.15, w[0] / 2)))
    for s0, s1 in ((0, 2), (3, 5)):          # split at the crossing so no outline self-intersects
        a, b = seg_range(smp, s0, s1)
        shapes.append(("ink", outline(smp, w, a, b)))
    tip, (tx, ty) = smp[-1]["pt"], smp[-1]["tan"]
    smp2 = sample(cross)
    w2 = widths(smp2, cross, taper=(7, 10), thick=c["thick"], hair=hair, smooth=1)
    shapes.append(("ink", outline(smp2, w2, 0, len(smp2) - 1)))
    R = c["R"]; d = R + 3
    shapes.append(("star", star(tip[0] + tx * d + 4, tip[1] + ty * d, R, R * 0.72, R * c["waist"])))
    return shapes


def rdp(pts, eps):
    """Ramer–Douglas–Peucker: drop points that sit within eps of the line through their neighbours."""
    if len(pts) < 3:
        return pts
    (ax, ay), (bx, by) = pts[0], pts[-1]
    L = math.hypot(bx - ax, by - ay) or 1e-9
    i, dmax = 0, -1.0
    for k in range(1, len(pts) - 1):
        d = abs((bx - ax) * (ay - pts[k][1]) - (ax - pts[k][0]) * (by - ay)) / L
        if d > dmax:
            i, dmax = k, d
    if dmax <= eps:
        return [pts[0], pts[-1]]
    return rdp(pts[:i + 1], eps)[:-1] + rdp(pts[i:], eps)


def path_d(shape, f, eps):
    if isinstance(shape[0][0], str):     # star commands
        return "".join(c[0] + " ".join(f"{f(p)[0]:.1f} {f(p)[1]:.1f}" for p in c[1:]) for c in shape) + "Z"
    pts = [f(p) for p in shape]
    half = len(pts) // 2                  # simplify each side of the stroke separately, keep the tips
    pts = rdp(pts[:half + 1], eps)[:-1] + rdp(pts[half:], eps)
    return "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in pts) + "Z"


def bounds(shapes):
    xs, ys = [], []
    for _, s in shapes:
        pts = [p for c in s for p in c[1:]] if isinstance(s[0][0], str) else s
        xs += [p[0] for p in pts]; ys += [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def fit(shapes, W, H, box_w, box_h, dy=0.0):
    x0, y0, x1, y1 = bounds(shapes)
    s = min(box_w / (x1 - x0), box_h / (y1 - y0))
    ox = (W - (x1 - x0) * s) / 2 - x0 * s
    oy = (H - (y1 - y0) * s) / 2 - y0 * s + dy
    return lambda p: (p[0] * s + ox, p[1] * s + oy)


def write_svg(path, W, H, body, title="Glint Studio"):
    with open(path, "w") as fh:
        fh.write(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" width="{W:.0f}" height="{H:.0f}" role="img">\n'
                 f"<title>{title}</title>\n" + "\n".join(body) + "\n</svg>\n")


def paths(shapes, f, colours, eps):
    return [f'<path fill="{colours[k]}" d="{path_d(s, f, eps)}"/>' for k, s in shapes]


def icon(out, name, cut, tile, ink, size=1024, inset=0):
    """Rounded-square tile; inset>0 draws the tile smaller inside the canvas (macOS icon grid)."""
    c = CUTS[cut]; sh = build(c)
    t = size - 2 * inset
    f0 = fit(sh, t, t, t * c["box"], t * c["box"], dy=-t * 0.01)   # optical: a hair above centre
    f = lambda p: (f0(p)[0] + inset, f0(p)[1] + inset)
    body = [f'<rect x="{inset}" y="{inset}" width="{t}" height="{t}" rx="{t * 0.18:.0f}" fill="{tile}"/>']
    body += paths(sh, f, {"ink": ink, "star": BLUE}, size / 4000)
    write_svg(os.path.join(out, name), size, size, body)


def icon_doc(path):
    """Icon Composer document (.icon): navy fill plus letter and star layers, so macOS 26 can draw Dark, Clear and Tinted."""
    import json
    c = CUTS["full"]; sh = build(c)
    f = fit(sh, 1024, 1024, 1024 * c["box"], 1024 * c["box"], dy=-1024 * 0.01)   # same placement as icon()
    os.makedirs(os.path.join(path, "Assets"), exist_ok=True)
    for key, col in (("ink", WHITE), ("star", BLUE)):
        write_svg(os.path.join(path, "Assets", f"{key}.svg"), 1024, 1024, paths([s for s in sh if s[0] == key], f, {key: col}, 1024 / 4000))
    rgb = ",".join(f"{int(NAVY[i:i + 2], 16) / 255:.5f}" for i in (1, 3, 5))
    doc = {
        "fill": {"solid": f"srgb:{rgb},1.00000"},
        "groups": [{
            "layers": [{"image-name": "star.svg", "name": "star"}, {"image-name": "ink.svg", "name": "letters"}],
            "shadow": {"kind": "neutral", "opacity": 0.5},
            "translucency": {"enabled": True, "value": 0.5},
        }],
        "supported-platforms": {"squares": ["macOS"]},
    }
    with open(os.path.join(path, "icon.json"), "w") as fh:
        json.dump(doc, fh, indent=2)


def mark(out, name, cut, ink, star_col, size=256, pad=0.04):
    c = CUTS[cut]; sh = build(c)
    f = fit(sh, size, size, size * (1 - 2 * pad), size * (1 - 2 * pad))
    write_svg(os.path.join(out, name), size, size, paths(sh, f, {"ink": ink, "star": star_col}, size / 4000))


def wordmark_paths(ttf, text):
    from fontTools.ttLib import TTFont
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    font = TTFont(ttf)
    gs, cmap, hmtx = font.getGlyphSet(), font.getBestCmap(), font["hmtx"]
    upm, cap = font["head"].unitsPerEm, font["OS/2"].sCapHeight
    x, out = 0, []
    for ch in text:   # shortcut: no GPOS kerning applied; fine for this short name, revisit if the name changes
        g = cmap[ord(ch)]
        pen = SVGPathPen(gs)
        gs[g].draw(TransformPen(pen, (1, 0, 0, -1, x, 0)))   # flip y: font y-up -> SVG y-down
        if pen.getCommands():
            out.append(pen.getCommands())
        x += hmtx[g][0]
    return out, x, upm, cap


def lockup(out, name, ttf, ink, star_col):
    c = CUTS["full"]; sh = build(c)
    x0, y0, x1, y1 = bounds(sh)
    word, adv, upm, cap = wordmark_paths(ttf, "Glint Studio")
    # cap height of the wordmark = the g's x-height (68 -> 140); baselines shared at y = 140
    k = (140 - 68) * 0.70 / cap
    gap = 26
    wx = x1 + gap
    total_w = (wx + adv * k) - x0
    H = 256; s = (H * 0.86) / (y1 - y0)
    W = total_w * s + H * 0.14
    ox, oy = H * 0.07 - x0 * s, H * 0.07 - y0 * s
    f = lambda p: (p[0] * s + ox, p[1] * s + oy)
    body = paths(sh, f, {"ink": ink, "star": star_col}, 0.06)
    tx, ty, ks = wx * s + ox, 140 * s + oy, k * s
    body.append(f'<g id="wordmark" fill="{ink}" transform="matrix({ks:.5f} 0 0 {ks:.5f} {tx:.2f} {ty:.2f})">'
                + "".join(f'<path d="{d}"/>' for d in word) + "</g>")
    write_svg(os.path.join(out, name), W, H, body)


if __name__ == "__main__":
    out, ttf = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    for cut, suffix in (("full", ""), ("32", "-32"), ("16", "-16")):
        icon(out, f"glint-icon-dark{suffix}.svg", cut, NAVY, WHITE)
        icon(out, f"glint-icon-light{suffix}.svg", cut, PAPER, NAVY)
    icon(out, "glint-icon-dark-macos.svg", "full", NAVY, WHITE, inset=100)
    icon_doc(os.path.join(out, "..", "glint.icon"))
    mark(out, "glint-mark-white.svg", "full", WHITE, BLUE)
    mark(out, "glint-mark-navy.svg", "full", NAVY, BLUE)
    mark(out, "glint-mark-black.svg", "full", "#000000", "#000000")
    mark(out, "glint-menubar-template.svg", "16", "#000000", "#000000", pad=0.02)
    lockup(out, "glint-lockup-white.svg", ttf, WHITE, BLUE)
    lockup(out, "glint-lockup-navy.svg", ttf, NAVY, BLUE)
    lockup(out, "glint-lockup-black.svg", ttf, "#000000", "#000000")
    print("kit ->", out)
