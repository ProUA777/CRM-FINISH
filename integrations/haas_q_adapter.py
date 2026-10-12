# -*- coding: utf-8 -*-
"""
Адаптер Haas Ethernet Q Commands -> Оснастка-Маркет.

Що робить: підключається по TCP до контролера Haas (NGC та новіші SGC з
увімкненим Machine Data Collect), періодично опитує стан верстата і
пересилає дані в Оснастка-Маркет через /api/v1/telemetry/<work_center_id>.

ПЕРЕДУМОВИ НА ВЕРСТАТІ (робиться один раз на пульті керування):
  1. Верстат підключений до тієї ж мережі, що й ПК, на якому запускається
     цей скрипт (Ethernet, статична IP-адреса верстата бажана).
  2. Натиснути SETTING на пульті, перейти до Setting 143
     "Machine Data Collect" і встановити порт 5051, увімкнути (ON).
  3. Записати IP-адресу верстата: SETTING -> Network -> Wired Connection.

ЩО ОПИТУЄМО (документовані Ethernet Q Commands контролера Haas):
  ?Q100          - серійний номер (використовується як "рукостискання")
  ?Q300          - Power-On Time, сумарний час увімкненого стану
  ?Q301          - Motion Time, сумарний час у русі -- це і є
                   "скільки пропрацював верстат" в сенсі різання металу
  ?Q402          - лічильник деталей M30 (Parts Counter #1)
  ?Q500          - "три в одному": активна програма, статус, лічильник деталей

ВАЖЛИВО ПРО ТОЧНІСТЬ ДАНИХ:
  Формат відповіді Haas задокументований (CSV-рядок, що починається з ">"),
  але конкретні текстові значення поля STATUS у Q500 (наприклад ACTIVE/READY/
  ALARM тощо) та одиниці виміру часу в Q300/Q301 (години чи хвилини) можуть
  відрізнятися між версіями ПЗ контролера. Перед бойовим використанням
  ОБОВ'ЯЗКОВО запустіть скрипт з прапорцем --debug і звірте сирі відповіді
  зі своїм верстатом, після чого за потреби скоригуйте функції
  parse_q500() / normalize_status() / hours-конвертацію нижче.

ЗАПУСК:
    pip install requests
    python haas_q_adapter.py \\
        --haas-ip 192.168.1.50 \\
        --work-center-id 2 \\
        --crm-url http://192.168.1.10:5000 \\
        --api-key ВАШ_КЛЮЧ_З_НАЛАШТУВАНЬ_CRM \\
        --interval 30

Запускати окремим процесом на кожен верстат (один --work-center-id на
верстат — ідентифікатор робочого центру з розділу "Виробництво" в CRM).
Можна також запустити як systemd-сервіс для автозапуску при старті ПК.
"""

import argparse
import socket
import time
import sys
import logging

try:
    import requests
except ImportError:
    print("Потрібна бібліотека requests: pip install requests")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("haas_q_adapter")


class HaasQClient:
    """Клієнт для Ethernet Q Commands контролера Haas (порт за замовчуванням 5051)."""

    def __init__(self, host, port=5051, timeout=5):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        # За документацією Haas початкове підключення інколи вимагає ?Q100 двічі.
        self.query("Q100")
        time.sleep(0.2)
        self.query("Q100")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def query(self, code):
        """Надсилає ?Qxxx і повертає сирий текст відповіді (без обробки)."""
        if not self.sock:
            self.connect()
        cmd = f"?{code}\r\n".encode("ascii")
        self.sock.sendall(cmd)
        time.sleep(0.15)
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            return ""
        return data.decode("ascii", errors="ignore").strip()


def parse_csv_response(raw):
    """Відповідь Haas має вигляд '>NAME,VALUE1,VALUE2,...'. Прибираємо '>' і
    розбиваємо по комі. Повертає список рядків (без обрізки пробілів для
    діагностики, обрізку робимо в місцях використання)."""
    raw = raw.strip()
    if raw.startswith(">"):
        raw = raw[1:]
    return [p.strip() for p in raw.split(",")]


def to_float_safe(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def to_int_safe(s, default=None):
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def normalize_status(raw_status):
    """Мапить сирий текстовий статус Haas у словник CRM (running/idle/alarm/offline).
    ПЕРЕВІРТЕ реальні значення вашого контролера через --debug і за потреби
    розширте цей словник — тут покрито найпоширеніші варіанти з практики."""
    if not raw_status:
        return "offline"
    s = raw_status.strip().upper()
    if "ALARM" in s:
        return "alarm"
    if any(k in s for k in ("ACTIVE", "RUN", "CYCLE START", "FEED HOLD" + "")) and "HOLD" not in s:
        return "running"
    if any(k in s for k in ("READY", "STOP", "IDLE", "HOLD", "RESET")):
        return "idle"
    return "idle"


def poll_once(client, work_center_id):
    """Одна ітерація опитування верстата. Повертає dict, готовий для відправки в CRM,
    або None, якщо зв'язок не вдався (тоді буде спроба перепідключення)."""
    payload = {"source": "haas_q_commands"}

    q500 = parse_csv_response(client.query("Q500"))
    # Очікуваний формат (за документацією Haas): PROGRAM, Oxxxxx, STATUS, xxx, PARTS, xxxxx
    # Реальне розташування полів варто звірити через --debug на вашому ПЗ контролера.
    if len(q500) >= 2:
        payload["program_name"] = q500[1] if len(q500) > 1 else None
    raw_status = None
    for token in q500:
        tu = token.strip().upper()
        if tu in ("ACTIVE", "READY", "ALARM", "STOPPED", "FEED HOLD", "RESET"):
            raw_status = token
            break
    payload["mode"] = None
    payload["status"] = normalize_status(raw_status)
    # Лічильник деталей — пробуємо витягнути останнє числове поле відповіді Q500,
    # інакше підстрахуємось окремим запитом Q402.
    numeric_tail = [to_int_safe(x) for x in q500 if to_int_safe(x) is not None]
    if numeric_tail:
        payload["part_count"] = numeric_tail[-1]
    else:
        q402 = parse_csv_response(client.query("Q402"))
        nums = [to_int_safe(x) for x in q402 if to_int_safe(x) is not None]
        payload["part_count"] = nums[-1] if nums else None

    q301 = parse_csv_response(client.query("Q301"))
    nums = [to_float_safe(x) for x in q301 if to_float_safe(x) is not None]
    payload["motion_hours"] = nums[-1] if nums else None

    q300 = parse_csv_response(client.query("Q300"))
    nums = [to_float_safe(x) for x in q300 if to_float_safe(x) is not None]
    payload["power_on_hours"] = nums[-1] if nums else None

    return payload


def send_to_crm(crm_url, api_key, work_center_id, payload, timeout=5):
    url = f"{crm_url.rstrip('/')}/api/v1/telemetry/{work_center_id}"
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def main():
    ap = argparse.ArgumentParser(description="Адаптер Haas Ethernet Q Commands -> Оснастка-Маркет")
    ap.add_argument("--haas-ip", required=True, help="IP-адреса контролера Haas у цеховій мережі")
    ap.add_argument("--haas-port", type=int, default=5051, help="Порт Machine Data Collect (Setting 143), типово 5051")
    ap.add_argument("--work-center-id", type=int, required=True, help="ID робочого центру в Оснастка-Маркет")
    ap.add_argument("--crm-url", required=True, help="Базова адреса CRM, напр. http://192.168.1.10:5000")
    ap.add_argument("--api-key", required=True, help="API-ключ зі сторінки Налаштування в CRM")
    ap.add_argument("--interval", type=int, default=30, help="Інтервал опитування, секунд (типово 30)")
    ap.add_argument("--debug", action="store_true", help="Виводити сирі відповіді верстата для калібрування парсера")
    args = ap.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    client = HaasQClient(args.haas_ip, args.haas_port)
    log.info("Запуск адаптера Haas Q Commands: %s:%s -> %s (work_center_id=%s)",
             args.haas_ip, args.haas_port, args.crm_url, args.work_center_id)

    while True:
        try:
            if not client.sock:
                client.connect()
                log.info("Підключено до контролера Haas %s:%s", args.haas_ip, args.haas_port)

            if args.debug:
                for code in ("Q100", "Q300", "Q301", "Q402", "Q500"):
                    raw = client.query(code)
                    log.debug("RAW ?%s -> %r", code, raw)

            payload = poll_once(client, args.work_center_id)
            log.info("Телеметрія: статус=%s, motion_hours=%s, power_on_hours=%s, parts=%s, програма=%s",
                      payload.get("status"), payload.get("motion_hours"),
                      payload.get("power_on_hours"), payload.get("part_count"),
                      payload.get("program_name"))
            send_to_crm(args.crm_url, args.api_key, args.work_center_id, payload)

        except (socket.error, ConnectionError, OSError) as e:
            log.warning("Проблема зі зв'язком з верстатом (%s) — перепідключення через %ss", e, args.interval)
            client.close()
        except requests.RequestException as e:
            log.warning("Проблема з відправкою в CRM: %s", e)
        except KeyboardInterrupt:
            log.info("Зупинка адаптера")
            client.close()
            sys.exit(0)
        except Exception as e:
            log.exception("Неочікувана помилка: %s", e)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
