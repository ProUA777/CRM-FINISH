# -*- coding: utf-8 -*-
"""
Лаунчер для зібраного .exe Оснастка-Маркет.

Що робить при запуску:
  1. Піднімає застосунок через waitress (продакшн WSGI-сервер, працює
     на Windows, на відміну від gunicorn) на http://127.0.0.1:5000.
  2. Автоматично відкриває цю адресу в браузері за замовчуванням.
  3. Показує іконку в системному треї з пунктами "Відкрити CRM",
     "Тека з даними" (бекапи/логи/crm.db) і "Вийти".
     Якщо pystray/Pillow з якоїсь причини недоступні у зібраному .exe —
     чемно падає назад у режим звичайного консольного вікна.

Це не веб-фреймворк і не частина самого app.py — окремий, тонкий
"обгортковий" скрипт саме для десктопної зручності (щоб не пояснювати
колезі, що таке pip і командний рядок).
"""

import os
import sys
import time
import threading
import webbrowser
import logging
from logging.handlers import RotatingFileHandler

HOST = "127.0.0.1"
PORT = int(os.environ.get("CRM_PORT", "5000"))
URL = f"http://{HOST}:{PORT}"


def _is_port_free(host, port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _find_free_port(host, start_port, max_tries=20):
    """На комп'ютері користувача може вже працювати щось інше на порту 5000
    (інший локальний проєкт, інший сервіс) — сліпе прив'язування до
    зайнятого порту раніше валило наш сервер з OSError, а браузер при
    цьому все одно відкривався і показував ЧУЖУ програму, що займає порт,
    без жодного пояснення користувачу. Тепер шукаємо перший вільний порт
    поруч із бажаним."""
    port = start_port
    for _ in range(max_tries):
        if _is_port_free(host, port):
            return port
        port += 1
    return start_port


def _data_dir():
    """Та сама логіка, що і в app.py (навмисно продубльована тут, а не
    імпортована з app.py, щоб налаштування логування не залежало від
    успішного імпорту всього застосунку)."""
    if getattr(sys, "frozen", False):
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        else:
            base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        path = os.path.join(base, "OsnastkaMarket")
    else:
        path = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(path, exist_ok=True)
    return path


DATA_DIR = os.environ.get("CRM_DATA_DIR") or _data_dir()


def _setup_logging():
    """КРИТИЧНО для зібраного .exe: він збирається з прапорцем --noconsole,
    тобто в ньому НЕМАЄ консольного вікна — sys.stdout там дорівнює None
    (не просто перенаправлений, а буквально відсутній). Будь-яка спроба
    print() чи запису в консоль у такому режимі одразу валить програму
    з AttributeError, причому користувач не побачить НІЯКОГО повідомлення
    про причину — вікно просто не з'явиться.

    Тому лог пишеться в файл ЗАВЖДИ (це працює незалежно від наявності
    консолі), а в консоль дублюється лише тоді, коли вона справді є
    (запуск напряму через `python launcher.py` під час розробки)."""
    logger = logging.getLogger("osnastka_market_launcher")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "launcher.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(fmt)
        logger.addHandler(console_handler)

    return logger


log = _setup_logging()


def start_server():
    """Запускає CRM через waitress у поточному потоці (викликається з окремого
    daemon-потоку в main(), тож завершення головного потоку автоматично
    зупиняє й сервер)."""
    import app as crm_app  # імпорт тут, а не на верху файлу: тільки в цей момент
                            # DATA_DIR/DB_PATH з app.py вже коректно визначені
                            # для frozen-режиму (див. app.py: _default_data_dir)
    from waitress import serve
    log.info("Оснастка-Маркет: дані зберігаються в %s", crm_app.DATA_DIR)
    serve(crm_app.app, host=HOST, port=PORT, _quiet=True)


def open_data_dir():
    import app as crm_app
    path = crm_app.DATA_DIR
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: os.startfile існує лише на Windows
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}"')
    except Exception as e:
        log.warning("Не вдалось відкрити теку даних: %s", e)


def make_tray_icon_image():
    """Малює просту іконку програмно (шестерня на темному колі) — без
    потреби тягнути окремий файл .ico як ресурс."""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, size - 2, size - 2], fill=(26, 29, 35, 255))
    draw.ellipse([16, 16, size - 16, size - 16], fill=(255, 122, 26, 255))
    # кілька "зубців" шестерні
    import math
    cx, cy, r1, r2 = size / 2, size / 2, 26, 31
    for i in range(8):
        angle = i * (2 * math.pi / 8)
        x1, y1 = cx + r1 * math.cos(angle), cy + r1 * math.sin(angle)
        x2, y2 = cx + r2 * math.cos(angle), cy + r2 * math.sin(angle)
        draw.line([x1, y1, x2, y2], fill=(26, 29, 35, 255), width=6)
    return img


def run_with_tray():
    import pystray
    from pystray import MenuItem as Item

    def on_open(icon, item):
        webbrowser.open(URL)

    def on_data_dir(icon, item):
        open_data_dir()

    def on_exit(icon, item):
        icon.stop()
        os._exit(0)  # daemon-потік сервера теж завершиться разом з процесом

    icon = pystray.Icon(
        "OsnastkaMarket",
        make_tray_icon_image(),
        "Оснастка-Маркет",
        menu=pystray.Menu(
            Item("Відкрити CRM", on_open, default=True),
            Item("Тека з даними", on_data_dir),
            Item("Вийти", on_exit),
        ),
    )
    icon.run()


def run_console_fallback():
    log.info("Оснастка-Маркет запущено: %s", URL)
    log.info("Це вікно можна згорнути. Щоб зупинити сервер — закрийте вікно або натисніть Ctrl+C.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Зупинка Оснастка-Маркет...")


def main():
    global PORT, URL
    chosen_port = _find_free_port(HOST, PORT)
    if chosen_port != PORT:
        log.warning("Порт %s зайнятий іншою програмою на цьому комп'ютері — використовую порт %s замість нього",
                    PORT, chosen_port)
    PORT = chosen_port
    URL = f"http://{HOST}:{PORT}"

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # Даємо серверу секунду піднятись перед тим, як відкривати браузер,
    # інакше перший запит може прийти на порт, що ще не слухає.
    time.sleep(1.2)
    try:
        webbrowser.open(URL)
    except Exception as e:
        log.warning("Не вдалось автоматично відкрити браузер: %s", e)

    try:
        run_with_tray()
    except ImportError:
        log.info("pystray/Pillow недоступні — працюю в консольному режимі")
        run_console_fallback()
    except Exception as e:
        log.warning("Іконка в треї не запустилась (%s) — консольний режим", e)
        run_console_fallback()


if __name__ == "__main__":
    main()
