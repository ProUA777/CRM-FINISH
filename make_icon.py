# -*- coding: utf-8 -*-
"""
Генерує icon.ico для PyInstaller та інсталятора Inno Setup — щоб не
тримати в репозиторії бінарний файл-картинку, іконка малюється кодом.

Запуск (один раз перед збіркою):
    pip install pillow
    python make_icon.py
Створює icon.ico у поточній теці.
"""

import math
import sys
from PIL import Image, ImageDraw

# На Windows консоль інколи використовує застарілу однобайтову кодировку
# (cp1252 і подібні), яка фізично не має символів кирилиці — спроба
# надрукувати туди щось україномовне валить програму з UnicodeEncodeError.
# Явно перемикаємо стандартний вивід на UTF-8, де це можливо.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def build_icon(size=256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = int(size * 0.03)
    draw.ellipse([pad, pad, size - pad, size - pad], fill=(26, 29, 35, 255))
    inner = size * 0.28
    cx = cy = size / 2
    draw.ellipse([cx - inner, cy - inner, cx + inner, cy + inner], fill=(255, 122, 26, 255))
    r1, r2 = size * 0.40, size * 0.47
    width = max(int(size * 0.09), 2)
    for i in range(8):
        angle = i * (2 * math.pi / 8)
        x1, y1 = cx + r1 * math.cos(angle), cy + r1 * math.sin(angle)
        x2, y2 = cx + r2 * math.cos(angle), cy + r2 * math.sin(angle)
        draw.line([x1, y1, x2, y2], fill=(26, 29, 35, 255), width=width)
    return img


if __name__ == "__main__":
    icon = build_icon(256)
    icon.save("icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icon.ico created")
