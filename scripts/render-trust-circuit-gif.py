from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 960, 540
BG = "#06101d"
PANEL = "#0a192a"
LINE = "#183651"
CYAN = "#43d9ff"
MINT = "#6ef3ac"
CORAL = "#ff735f"
TEXT = "#d9f2ff"
MUTED = "#7896aa"


def font(size: int, bold: bool = False):
    name = "/System/Library/Fonts/SFNSMono.ttf"
    return ImageFont.truetype(name, size)


TITLE = font(26, True)
SMALL = font(12)
BODY = font(15)
BIG = font(28, True)

PACKETS = [
    ("NODE_A7", "NODE_F4", "VALID", "FRESH", "12s", True),
    ("NODE_Q6", "NODE_C2", "INVALID", "FRESH", "09s", False),
    ("NODE_M3", "NODE_K8", "VALID", "REPLAY", "07s", False),
    ("NODE_D9", "NODE_V1", "VALID", "FRESH", "11s", True),
    ("NODE_C2", "NODE_A7", "VALID", "FRESH", "EXPIRED", False),
    ("NODE_K8", "NODE_Q6", "VALID", "FRESH", "08s", True),
    ("NODE_F4", "NODE_M3", "INVALID", "REPLAY", "05s", False),
    ("NODE_V1", "NODE_D9", "VALID", "FRESH", "10s", True),
    ("NODE_A7", "NODE_K8", "VALID", "FRESH", "06s", True),
    ("NODE_Q6", "NODE_F4", "VALID", "REPLAY", "04s", False),
]


def text(draw, xy, value, fill=TEXT, font_obj=BODY, anchor=None):
    draw.text(xy, value, fill=fill, font=font_obj, anchor=anchor)


def frame(index: int):
    packet_index = min(len(PACKETS) - 1, index // 8)
    within = index % 8
    source, target, sig, nonce, ttl, valid = PACKETS[packet_index]
    decision = within >= 6
    color = MINT if valid else CORAL
    score = sum(100 + i * 20 for i, packet in enumerate(PACKETS[:packet_index]) if packet[-1])
    score += sum(100 for packet in PACKETS[:packet_index] if not packet[-1])

    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    for x in range(0, WIDTH, 40):
        draw.line((x, 0, x, HEIGHT), fill="#0b2032", width=1)
    for y in range(0, HEIGHT, 40):
        draw.line((0, y, WIDTH, y), fill="#0b2032", width=1)

    draw.rectangle((0, 0, WIDTH, 62), fill="#071522", outline=LINE)
    text(draw, (26, 30), "TC  TRUST CIRCUIT", CYAN, TITLE, "lm")
    text(draw, (934, 30), "REPRESENTATIVE GAMEPLAY · Q / R", MINT, SMALL, "rm")

    draw.rectangle((0, 62, 126, HEIGHT - 40), fill="#071522", outline=LINE)
    labels = [("TIME", f"2:{30 - packet_index * 2:02d}"), ("SCORE", str(score)), ("CHAIN", str(packet_index)), ("INTEGRITY", "◆◆◆"), ("PRESSURE", "P1")]
    y = 94
    for label, value in labels:
        text(draw, (18, y), label, MUTED, SMALL)
        text(draw, (18, y + 24), value, CORAL if label == "INTEGRITY" else TEXT, BIG)
        y += 78

    draw.line((160, 284, 810, 284), fill="#24536f", width=3)
    draw.ellipse((150, 264, 190, 304), outline=CYAN, width=3)
    draw.ellipse((790, 168, 838, 216), outline=MINT, width=3)
    draw.ellipse((790, 350, 838, 398), outline=CORAL, width=3)
    draw.line((500, 284, 812, 192), fill=MINT, width=3)
    draw.line((500, 284, 812, 374), fill=CORAL, width=3)

    travel = min(1, within / 5)
    px = 205 + int(250 * travel)
    draw.rounded_rectangle((px, 196, px + 350, 364), radius=10, fill=PANEL, outline=CYAN, width=2)
    text(draw, (px + 18, 216), f"INBOUND // {packet_index + 1:04d}", CYAN, SMALL)
    text(draw, (px + 332, 216), f"TTL {ttl}", CYAN if ttl != "EXPIRED" else CORAL, SMALL, "ra")
    text(draw, (px + 175, 255), f"{source}  →  {target}", TEXT, BODY, "mm")
    rows = [("SIGNATURE", sig), ("NONCE", nonce), ("PROOF TTL", ttl)]
    row_y = 286
    for label, value in rows:
        text(draw, (px + 20, row_y), label, MUTED, SMALL)
        passing = value in {"VALID", "FRESH"} or (label == "PROOF TTL" and value != "EXPIRED")
        text(draw, (px + 330, row_y), value, MINT if passing else CORAL, SMALL, "ra")
        row_y += 24

    draw.rounded_rectangle((240, 432, 465, 490), radius=5, fill="#081522", outline=CORAL, width=2)
    draw.rounded_rectangle((495, 432, 720, 490), radius=5, fill="#081522", outline=MINT, width=2)
    text(draw, (260, 461), "Q  QUARANTINE", CORAL, BODY, "lm")
    text(draw, (700, 461), "RELAY  R", MINT, BODY, "rm")
    if decision:
        chosen = "RELAY" if valid else "QUARANTINE"
        box = (495, 432, 720, 490) if valid else (240, 432, 465, 490)
        draw.rectangle(box, outline=color, width=6)
        text(draw, (480, 407), f"{chosen} · CORRECT · +{100 + packet_index * 20}", color, BODY, "mm")
    else:
        text(draw, (480, 407), "INSPECT SIGNATURE · NONCE · TTL", MUTED, SMALL, "mm")

    draw.rectangle((0, HEIGHT - 40, WIDTH, HEIGHT), fill="#071522", outline=LINE)
    text(draw, (24, HEIGHT - 20), "ROUTE PROOF. BUILD TRUST. BEAT THE CLOCK.", MUTED, SMALL, "lm")
    text(draw, (936, HEIGHT - 20), "NO LOGIN · NO BACKEND", MUTED, SMALL, "rm")
    return image


def main():
    frames = [frame(i) for i in range(80)]
    out = Path(__file__).resolve().parents[1] / "public" / "trust-circuit-gameplay.gif"
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=250, loop=0, optimize=True)
    print(f"wrote {out} ({out.stat().st_size} bytes, 20 seconds)")


if __name__ == "__main__":
    main()
