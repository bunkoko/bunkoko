"""アプリのアイコン（E3 改「右肩上がりの記録」）を SVG で描く。

  python3 ios/Design/app_icon.py            # ios/Design/ に AppIcon*.svg を書く
  node ios/Design/render_app_icon.mjs       # SVG を 1024px の PNG にして、アセットカタログに入れる

解体新書の人体図のような人と、方眼紙の上の右肩上がりの記録（折れ線と、だんだん細くなるばらつきの帯）。
地は夜の青。人の肌・朱色の心臓・後ろの淡い光で、人のぬくもりを残す。
iPhone の表示に合わせて、ふつう・ダーク・色付き（白黒）の3枚を作る。
"""
import math
from pathlib import Path

OUT = Path(__file__).resolve().parent

PALETTES = {
    "AppIcon": dict(
        bg=("#2C4266", "#1C2C48", "#121D33"), grain=(0.85, 0.9, 1.0, 0.07), line="#EADFCB", grid_op=0.10,
        figure_ink="#3A2A22", skin="#F2D3B5", heart="#E2563F", sun="#F2B544", moon="#A9CBF2",
        band="#8DB5E2", band_op=0.24, shadow="#050A14", glow="#F2B544",
        ramp=[(0.0, "#9CC0EA"), (0.45, "#86C6B4"), (0.8, "#F2B544"), (1.0, "#F59A4A")]),
    # ダーク表示: 地をさらに暗くする
    "AppIcon-Dark": dict(
        bg=("#1B2A44", "#111C30", "#0A1222"), grain=(0.85, 0.9, 1.0, 0.06), line="#EADFCB", grid_op=0.09,
        figure_ink="#3A2A22", skin="#F2D3B5", heart="#E2563F", sun="#F2B544", moon="#A9CBF2",
        band="#8DB5E2", band_op=0.22, shadow="#000000", glow="#F2B544",
        ramp=[(0.0, "#9CC0EA"), (0.45, "#86C6B4"), (0.8, "#F2B544"), (1.0, "#F59A4A")]),
    # 色付き表示: iPhone が好きな色をのせるので、白黒で描く（明るいところほど色が濃く出る）
    "AppIcon-Tinted": dict(
        bg=("#141414", "#0A0A0A", "#000000"), grain=(1.0, 1.0, 1.0, 0.0), line="#D8D8D8", grid_op=0.10,
        figure_ink="#1E1E1E", skin="#CFCFCF", heart="#FFFFFF", sun="#FFFFFF", moon="#BDBDBD",
        band="#FFFFFF", band_op=0.16, shadow="#000000", glow="#FFFFFF",
        ramp=[(0.0, "#8A8A8A"), (0.5, "#BDBDBD"), (1.0, "#FFFFFF")]),
}


def smooth(points, closed=True):
    """点を通る、なめらかな線（Catmull-Rom を3次ベジェにする）"""
    n = len(points)
    d = [f"M{points[0][0]:.1f} {points[0][1]:.1f}"]
    for i in range(n) if closed else range(n - 1):
        p0 = points[(i - 1) % n] if closed or i > 0 else points[i]
        p1, p2 = points[i], points[(i + 1) % n]
        p3 = points[(i + 2) % n] if closed or i + 2 < n else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append(f"C{c1[0]:.1f} {c1[1]:.1f} {c2[0]:.1f} {c2[1]:.1f} {p2[0]:.1f} {p2[1]:.1f}")
    return " ".join(d) + (" Z" if closed else "")


def color_at(ramp, t):
    for (t0, c0), (t1, c1) in zip(ramp, ramp[1:]):
        if t <= t1:
            k = (t - t0) / (t1 - t0)
            a = [int(c0[i:i + 2], 16) for i in (1, 3, 5)]
            b = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
            return "#" + "".join(f"{round(x + (y - x) * k):02X}" for x, y in zip(a, b))
    return ramp[-1][1]


# 立ち姿の人（正面）。左半分の輪郭の点を決めて、左右対称にする
LEFT = [(486, 286), (484, 322), (452, 338), (412, 350), (392, 378), (382, 440), (366, 520), (350, 600), (340, 650),
        (332, 690), (346, 714), (362, 700), (368, 650), (382, 595), (398, 520), (414, 450), (428, 412), (438, 470),
        (452, 560), (440, 625), (446, 700), (458, 770), (456, 830), (468, 882), (450, 900), (458, 912), (500, 912),
        (500, 884), (494, 830), (494, 770), (500, 700)]
BODY = LEFT + [(512, 662)] + [(1024 - x, y) for (x, y) in reversed(LEFT)]


def figure(p):
    ink = p["figure_ink"]
    detail = f'stroke="{ink}" stroke-width="7" fill="none" stroke-linecap="round" opacity="0.55"'
    heart = "M0 130 C -120 50 -165 -20 -128 -76 C -94 -126 -28 -122 0 -70 C 28 -122 94 -126 128 -76 C 165 -20 120 50 0 130 Z"
    return f'''<g filter="url(#soft)">
    <path d="{smooth(BODY)}" fill="{p["skin"]}" stroke="{ink}" stroke-width="15" stroke-linejoin="round"/>
    <ellipse cx="512" cy="230" rx="52" ry="63" fill="{p["skin"]}" stroke="{ink}" stroke-width="15"/>
  </g>
  <line x1="512" y1="300" x2="512" y2="650" stroke="{ink}" stroke-width="5" stroke-dasharray="14 12" opacity="0.35"/>
  <path d="M488 338 Q 456 344 424 356 M536 338 Q 568 344 600 356" {detail}/>
  <path d="{smooth([(492, 380), (462, 392), (448, 450), (458, 512), (494, 520)], closed=False)}" {detail}/>
  <path d="{smooth([(532, 380), (562, 392), (576, 450), (566, 512), (530, 520)], closed=False)}" {detail}/>
  <path d="{heart}" transform="translate(528 452) scale(0.236)" fill="{p["heart"]}"/>'''


def sun(p, cx, cy, r):
    rays = "".join(
        f'<line x1="{cx + (r + 14) * math.cos(a):.1f}" y1="{cy + (r + 14) * math.sin(a):.1f}" '
        f'x2="{cx + (r + 32) * math.cos(a):.1f}" y2="{cy + (r + 32) * math.sin(a):.1f}" '
        f'stroke="{p["sun"]}" stroke-width="12" stroke-linecap="round"/>'
        for a in [i * math.pi / 4 for i in range(8)])
    return f'{rays}<circle cx="{cx}" cy="{cy}" r="{r}" fill="{p["sun"]}"/>'


def moon(p, cx, cy, r):
    return (f'<mask id="moon-cut"><rect width="1024" height="1024" fill="#fff"/>'
            f'<circle cx="{cx + r * 0.45:.1f}" cy="{cy - r * 0.3:.1f}" r="{r * 0.85:.1f}" fill="#000"/></mask>'
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{p["moon"]}" mask="url(#moon-cut)"/>')


def icon(p):
    r, g, b, a = p["grain"]
    grid = "".join(f'<line x1="{x}" y1="0" x2="{x}" y2="1024" stroke="{p["line"]}" stroke-width="3" opacity="{p["grid_op"]}"/>' for x in range(64, 1024, 64))
    grid += "".join(f'<line x1="0" y1="{y}" x2="1024" y2="{y}" stroke="{p["line"]}" stroke-width="3" opacity="{p["grid_op"]}"/>' for y in range(64, 1024, 64))
    # 記録の推移: 右肩上がり（少し上下しながら）。ばらつきの帯は、右へ行くほど細い
    pts = [(190 + 72 * i, 800 - 50 * i + 26 * math.sin(i * 1.7)) for i in range(10)]
    half = [82 - 6 * i for i in range(10)]
    upper = [(x, y - h) for (x, y), h in zip(pts, half)]
    lower = [(x, y + h) for (x, y), h in zip(pts, half)]
    band = smooth(upper, closed=False) + " " + smooth(list(reversed(lower)), closed=False).replace("M", "L", 1) + " Z"
    dots = "".join(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="12" fill="{color_at(p["ramp"], i / 9)}"/>' for i, (x, y) in enumerate(pts[:-1]))
    lx, ly = pts[-1]
    end = (f'<circle cx="{lx:.0f}" cy="{ly:.0f}" r="44" fill="{p["glow"]}" opacity="0.25"/>'
           f'<circle cx="{lx:.0f}" cy="{ly:.0f}" r="20" fill="{color_at(p["ramp"], 1)}"/>'
           f'<line x1="{lx + 10:.0f}" y1="{ly - 30:.0f}" x2="866" y2="262" stroke="{p["sun"]}" stroke-width="8" stroke-dasharray="2 16" stroke-linecap="round"/>')
    axis = f'<path d="M150 150 L150 880 L900 880" fill="none" stroke="{p["line"]}" stroke-width="9" stroke-linecap="round" stroke-linejoin="round" opacity="0.7"/>'
    axis += "".join(f'<line x1="150" y1="{y}" x2="172" y2="{y}" stroke="{p["line"]}" stroke-width="7" opacity="0.7"/>' for y in range(240, 880, 128))
    axis += "".join(f'<line x1="{x}" y1="858" x2="{x}" y2="880" stroke="{p["line"]}" stroke-width="7" opacity="0.7"/>' for x in range(278, 900, 128))
    stops = "".join(f'<stop offset="{t}" stop-color="{c}"/>' for t, c in p["ramp"])
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <defs>
    <radialGradient id="paper" cx="0.5" cy="0.42" r="0.78">
      <stop offset="0" stop-color="{p["bg"][0]}"/><stop offset="0.68" stop-color="{p["bg"][1]}"/><stop offset="1" stop-color="{p["bg"][2]}"/>
    </radialGradient>
    <filter id="grain" x="0" y="0" width="100%" height="100%">
      <feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="2" seed="7"/>
      <feColorMatrix type="matrix" values="0 0 0 0 {r}  0 0 0 0 {g}  0 0 0 0 {b}  0 0 0 {a} 0"/>
    </filter>
    <filter id="soft" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="10" stdDeviation="14" flood-color="{p["shadow"]}" flood-opacity="0.25"/>
    </filter>
    <radialGradient id="vital" cx="512" cy="470" r="340" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{p["glow"]}" stop-opacity="0.26"/><stop offset="1" stop-color="{p["glow"]}" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="rise" x1="190" y1="0" x2="838" y2="0" gradientUnits="userSpaceOnUse">{stops}</linearGradient>
  </defs>
  <rect width="1024" height="1024" fill="url(#paper)"/>
  <rect width="1024" height="1024" filter="url(#grain)"/>
  {grid}
  <circle cx="512" cy="470" r="340" fill="url(#vital)"/>
  <path d="{band}" fill="{p["band"]}" opacity="{p["band_op"]}"/>
  <path d="{smooth(pts, closed=False)}" fill="none" stroke="url(#rise)" stroke-width="14" stroke-linecap="round"/>
  {dots}{end}
  {axis}
  {sun(p, 872, 206, 38)}
  {moon(p, 262, 222, 40)}
  {figure(p)}
</svg>
'''


if __name__ == "__main__":
    for name, palette in PALETTES.items():
        (OUT / f"{name}.svg").write_text(icon(palette))
        print(f"wrote {OUT / (name + '.svg')}")
