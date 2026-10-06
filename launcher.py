# -*- coding: utf-8 -*-
"""
Лаунчер для зібраного .exe Оснастка-Маркет.

Що робить при запуску:
  1. Піднімає застосунок через waitress (продакшн WSGI-сервер, працює
     на Windows, на відміну від gunicorn) на порту 5000, слухаючи ВСІ
     мережеві інтерфейси (0.0.0.0) - це дає доступ з інших комп'ютерів
     у тій же Wi-Fi/локальній мережі без жодних додаткових налаштувань:
     просто вводять IP-адресу цього комп'ютера в браузері. На самому
     цьому комп'ютері все одно відкривається звичний http://127.0.0.1.
  2. Автоматично відкриває цю адресу в браузері за замовчуванням.
  3. Показує іконку в системному треї з пунктами "Відкрити CRM",
     "Адреса для інших комп'ютерів" (копіює в буфер і показує адресу),
     "Тека з даними" (бекапи/логи/crm.db) і "Вийти".
     Якщо pystray/Pillow з якоїсь причини недоступні у зібраному .exe —
     чемно падає назад у режим звичайного консольного вікна.

  Перший запуск після прив'язки до 0.0.0.0 - Windows Defender Firewall
  зазвичай сам питає одноразовим вікном "Дозволити доступ?" (достатньо
  натиснути "Дозволити"), це стандартна поведінка Windows для будь-якої
  програми, що починає слухати мережу, і коду для цього не потрібно.

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

# BIND_HOST - на чому слухає сервер (0.0.0.0 = приймає з'єднання з усієї
# мережі, не лише з цього комп'ютера). LOCAL_URL - адреса, яку відкриваємо
# в браузері САМЕ на цьому комп'ютері (навмисно 127.0.0.1, а не 0.0.0.0-
# останній не всі браузери коректно відкривають як "цей комп'ютер").
# За потреби можна явно повернутись до старої поведінки "лише цей
# комп'ютер" через CRM_HOST=127.0.0.1.
BIND_HOST = os.environ.get("CRM_HOST", "0.0.0.0")
PORT = int(os.environ.get("CRM_PORT", "5000"))
LOCAL_URL = f"http://127.0.0.1:{PORT}"
URL = LOCAL_URL


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
    # Якщо порт 5000 був зайнятий і main() підібрав інший - кладемо реальний
    # порт у змінну середовища ДО імпорту app.py, щоб сторінка "Налаштування"
    # у самій CRM показувала правильну адресу для інших комп'ютерів, а не
    # завжди 5000.
    os.environ["CRM_PORT"] = str(PORT)
    import app as crm_app  # імпорт тут, а не на верху файлу: тільки в цей момент
                            # DATA_DIR/DB_PATH з app.py вже коректно визначені
                            # для frozen-режиму (див. app.py: _default_data_dir)
    from waitress import serve
    log.info("Оснастка-Маркет: дані зберігаються в %s", crm_app.DATA_DIR)
    try:
        crm_app.start_machine_poller()  # опитування верстатів по MTConnect (якщо задані в CRM)
    except Exception:
        log.exception("Не вдалось запустити опитування верстатів")
    serve(crm_app.app, host=BIND_HOST, port=PORT, _quiet=True)


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


def get_lan_ip():
    """Визначає локальну IP-адресу цього комп'ютера в мережі - саме її
    треба вводити в браузері на ІНШИХ комп'ютерах, щоб відкрити CRM тут.
    Трюк з UDP-сокетом: питаємо ОС, яким мережевим інтерфейсом пішов би
    пакет до 8.8.8.8, без фактичної відправки будь-яких даних назовні
    (жодного реального з'єднання з інтернетом не відбувається)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "OsnastkaMarket"


def autostart_supported():
    """Автозапуск має сенс лише для зібраного .exe на Windows (для
    `python launcher.py` у розробці sys.executable - це Python, а не наша програма)."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def is_autostart_enabled():
    if not autostart_supported():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
            return bool(value)
    except Exception:
        return False


def set_autostart(enabled):
    """Додає/прибирає програму з автозапуску Windows для ПОТОЧНОГО користувача
    (гілка HKEY_CURRENT_USER - прав адміністратора не потрібно). Після
    перезавантаження головного комп'ютера CRM сама піднімається, і колегам
    не треба нікого просити її запускати. Повертає True, якщо вдалось."""
    if not autostart_supported():
        return False
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, f'"{sys.executable}"')
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        log.warning("Не вдалось змінити автозапуск: %s", e)
        return False


def show_network_address():
    """Пункт трея "Адреса для інших комп'ютерів": копіює адресу в буфер
    обміну і показує її у віконці, щоб людина могла або просто вставити
    (Ctrl+V) в браузер на іншому комп'ютері, або прочитати й ввести вручну.
    Tkinter навмисно обраний для цього, а не окрема бібліотека - він уже
    входить у стандартну поставку Python, тож не додає нових залежностей
    до збірки .exe."""
    lan_ip = get_lan_ip()
    if not lan_ip:
        message = ("Не вдалося автоматично визначити адресу цього комп'ютера "
                    "в мережі. Перевір, що Wi-Fi/мережевий кабель підключені, "
                    "і повтори спробу ще раз через меню трея.")
    else:
        lan_url = f"http://{lan_ip}:{PORT}"
        message = (f"Адресу скопійовано в буфер обміну:\n\n{lan_url}\n\n"
                    "Відкрий цю адресу в браузері на ІНШОМУ комп'ютері в тій "
                    "же Wi-Fi мережі, щоб зайти в CRM з нього. На цьому "
                    "комп'ютері нічого встановлювати на інших машинах не треба.")
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        if lan_ip:
            root.clipboard_clear()
            root.clipboard_append(f"http://{lan_ip}:{PORT}")
            root.update()
        messagebox.showinfo("Оснастка-Маркет — адреса для мережі", message)
        root.destroy()
    except Exception as e:
        log.warning("Не вдалось показати вікно з адресою (%s). Адреса: %s", e, message)


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

    def on_network_address(icon, item):
        show_network_address()

    def on_toggle_autostart(icon, item):
        set_autostart(not is_autostart_enabled())

    def on_exit(icon, item):
        icon.stop()
        os._exit(0)  # daemon-потік сервера теж завершиться разом з процесом

    menu_items = [
        Item("Відкрити CRM", on_open, default=True),
        Item("Адреса для інших комп'ютерів", on_network_address),
    ]
    if autostart_supported():
        menu_items.append(Item("Запускати разом з Windows", on_toggle_autostart,
                                checked=lambda item: is_autostart_enabled()))
    menu_items += [
        Item("Тека з даними", on_data_dir),
        Item("Вийти", on_exit),
    ]
    icon = pystray.Icon(
        "OsnastkaMarket",
        make_tray_icon_image(),
        "Оснастка-Маркет",
        menu=pystray.Menu(*menu_items),
    )
    icon.run()


def run_console_fallback():
    log.info("Оснастка-Маркет запущено: %s", URL)
    lan_ip = get_lan_ip()
    if lan_ip and BIND_HOST != "127.0.0.1":
        log.info("Доступ з інших комп'ютерів у цій мережі: http://%s:%s", lan_ip, PORT)
    log.info("Це вікно можна згорнути. Щоб зупинити сервер — закрийте вікно або натисніть Ctrl+C.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Зупинка Оснастка-Маркет...")


def main():
    global PORT, URL
    chosen_port = _find_free_port(BIND_HOST, PORT)
    if chosen_port != PORT:
        log.warning("Порт %s зайнятий іншою програмою на цьому комп'ютері — використовую порт %s замість нього",
                    PORT, chosen_port)
    PORT = chosen_port
    URL = f"http://127.0.0.1:{PORT}"

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
