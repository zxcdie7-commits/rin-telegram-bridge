"""Слои аватара из листа персонажа владельца: лицо на каждую эмоцию + отдельно открытый рот и закрытые глаза.

Запуск: venv/bin/python avatar_art.py [путь к листу]  (по умолчанию art/_sheet_source.png)
Фон вырезает нейросеть isnet-anime (rembg). Головы на листе выровнены одинаково, поэтому рот из «удивления»
и закрытые глаза из «радости» ложатся на любое лицо.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
from rembg import new_session, remove

BASE = Path(__file__).resolve().parent
OUT = BASE / "art"
SHEET = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT / "_sheet_source.png"
# нижний ряд листа: центры голов по x (в пикселях оригинала 2752×1536) и общая полоса по y
FACES = {"idle": 282, "laugh": 722, "sad": 1162, "angry": 1609, "surprised": 2049, "smile": 2489}
HALF_W, TOP, BOTTOM = 230, 1005, 1478
MOUTH = (200, 279, 260, 329)   # рот внутри вырезанной головы
EYES = (128, 192, 332, 278)    # глаза с ресницами и нижним веком


def feather(size, box, blur):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse(box, fill=255)
    return mask.filter(ImageFilter.GaussianBlur(blur))


def overlay(src, box, blur):
    """Кусок src в мягком овале, остальное прозрачное — ложится поверх лица."""
    mask = feather(src.size, box, blur)
    out = src.copy()
    alpha = Image.composite(src.getchannel("A"), Image.new("L", src.size, 0), mask)
    out.putalpha(alpha)
    return out


def main():
    OUT.mkdir(exist_ok=True)
    sheet = Image.open(SHEET).convert("RGB")
    session = new_session("isnet-anime")
    faces = {}
    for name, cx in FACES.items():
        crop = sheet.crop((cx - HALF_W, TOP, cx + HALF_W, BOTTOM))
        faces[name] = remove(crop, session=session, post_process_mask=True).convert("RGBA")
        faces[name].save(OUT / f"base_{name}.png")
    mouth = overlay(faces["surprised"], MOUTH, 6)
    blink = overlay(faces["laugh"], EYES, 5)
    for name in FACES:
        if name not in ("laugh", "surprised"):      # у смеха и удивления рот уже открыт
            mouth.save(OUT / f"mouth_{name}.png")
        if name != "laugh":                          # у смеха глаза уже закрыты
            blink.save(OUT / f"blink_{name}.png")
    print("ok:", ", ".join(FACES))


if __name__ == "__main__":
    main()
