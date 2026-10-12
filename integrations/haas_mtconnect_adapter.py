# -*- coding: utf-8 -*-
"""
Адаптер Haas MTConnect -> Оснастка-Маркет.

Альтернатива haas_q_adapter.py: використовує вбудований MTConnect-агент
контролера Haas NGC (галузевий відкритий стандарт обміну даними верстатів)
замість "сирих" Q-команд. Має сенс, якщо у вас в цеху вже є інфраструктура
під MTConnect або кілька верстатів різних виробників, які теж його підтримують.

ПЕРЕДУМОВИ НА ВЕРСТАТІ:
  1. Контролер Haas NGC (виготовлений з 2017-2018 і пізніше), бажано
     версія ПЗ 100.20.000.1200 або новіша.
  2. SETTING -> 143 "Machine Data Collect" — увімкнути (те саме налаштування,
     що вмикає і Q Commands, і MTConnect-агент).
  3. Перевірити доступність агента з ПК, на якому запускається цей скрипт:
         http://IP_верстата:8082/probe   -- має повернути XML з переліком DataItems
         http://IP_верстата:8082/current -- поточні значення

ЩО ЗЧИТУЄМО З ВІДПОВІДІ /current (типові DataItem для верстатів з ЧПУ,
включно з Haas):
  Execution     -- стан виконання програми (ACTIVE / READY / STOPPED / ...)
  PartCount     -- лічильник деталей
  Program       -- ім'я активної програми
  Alarm / Fault -- ознаки аварії (за наявності на конкретній моделі)

ВАЖЛИВО: точний набір і назви DataItem залежать від XML-опису пристрою
(Devices.xml), який публікує агент — перед бойовим використанням відкрийте
http://IP_верстата:8082/probe і звірте фактичні id/type елементів із кодом
нижче (функція extract_metrics). Стандарт MTConnect не гарантує, що напряму
буде елемент виду "напрацювання в годинах" — для цього показника
надійніше використовувати haas_q_adapter.py (Q301 Motion Time).

ЗАПУСК:
    pip install requests
    python haas_mtconnect_adapter.py \\
        --haas-ip 192.168.1.50 \\
        --work-center-id 2 \\
        --crm-url http://192.168.1.10:5000 \\
        --api-key ВАШ_КЛЮЧ_З_НАЛАШТУВАНЬ_CRM \\
        --interval 15
"""

import argparse
import sys
import time
import logging
import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:
    print("Потрібна бібліотека requests: pip install requests")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("haas_mtconnect_adapter")

# MTConnect XML використовує простір імен, який відрізняється залежно від
# версії агента. Тому елементи шукаємо за локальним іменем тега (без
# урахування namespace), а не через жорстко прописаний namespace-рядок.


def local_tag(elem):
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


def extract_metrics(xml_text):
    """Проходить по всіх елементах відповіді /current і забирає ті типи,
    які нам потрібні, за атрибутом type/name — незалежно від namespace."""
    root = ET.fromstring(xml_text)
    result = {"execution": None, "part_count": None, "program": None, "alarm": False}
    for elem in root.iter():
        tag = local_tag(elem)
        item_type = (elem.get("type") or elem.get("name") or "").upper()
        text = (elem.text or "").strip()
        if tag == "Execution" or item_type == "EXECUTION":
            result["execution"] = text or result["execution"]
        elif tag == "PartCount" or item_type == "PART_COUNT":
            result["part_count"] = text or result["part_count"]
        elif tag == "Program" or item_type == "PROGRAM":
            result["program"] = text or result["program"]
        elif tag in ("Alarm", "Fault") or item_type in ("ALARM", "FAULT"):
            if text and text.upper() not in ("UNAVAILABLE", "NORMAL", ""):
                result["alarm"] = True
    return result


def normalize_status(metrics):
    if metrics["alarm"]:
        return "alarm"
    exe = (metrics["execution"] or "").upper()
    if exe in ("ACTIVE",):
        return "running"
    if exe in ("READY", "STOPPED", "INTERRUPTED", "FEED_HOLD"):
        return "idle"
    if not exe:
        return "offline"
    return "idle"


def to_int_safe(s):
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def send_to_crm(crm_url, api_key, work_center_id, payload, timeout=5):
    url = f"{crm_url.rstrip('/')}/api/v1/telemetry/{work_center_id}"
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def main():
    ap = argparse.ArgumentParser(description="Адаптер Haas MTConnect -> Оснастка-Маркет")
    ap.add_argument("--haas-ip", required=True, help="IP-адреса контролера Haas у цеховій мережі")
    ap.add_argument("--mtconnect-port", type=int, default=8082, help="Порт MTConnect-агента (типово 8082)")
    ap.add_argument("--work-center-id", type=int, required=True, help="ID робочого центру в Оснастка-Маркет")
    ap.add_argument("--crm-url", required=True, help="Базова адреса CRM, напр. http://192.168.1.10:5000")
    ap.add_argument("--api-key", required=True, help="API-ключ зі сторінки Налаштування в CRM")
    ap.add_argument("--interval", type=int, default=15, help="Інтервал опитування, секунд")
    ap.add_argument("--debug", action="store_true", help="Виводити сиру XML-відповідь для калібрування")
    args = ap.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    current_url = f"http://{args.haas_ip}:{args.mtconnect_port}/current"
    log.info("Запуск адаптера Haas MTConnect: %s -> %s (work_center_id=%s)",
             current_url, args.crm_url, args.work_center_id)

    while True:
        try:
            resp = requests.get(current_url, timeout=5)
            resp.raise_for_status()
            if args.debug:
                log.debug("RAW XML: %s", resp.text[:2000])
            metrics = extract_metrics(resp.text)
            payload = {
                "source": "haas_mtconnect",
                "status": normalize_status(metrics),
                "program_name": metrics["program"],
                "part_count": to_int_safe(metrics["part_count"]),
            }
            log.info("Телеметрія: статус=%s, програма=%s, деталей=%s",
                      payload["status"], payload["program_name"], payload["part_count"])
            send_to_crm(args.crm_url, args.api_key, args.work_center_id, payload)

        except requests.RequestException as e:
            log.warning("Проблема зі зв'язком (верстат або CRM): %s", e)
        except ET.ParseError as e:
            log.warning("Не вдалось розібрати XML-відповідь MTConnect: %s", e)
        except KeyboardInterrupt:
            log.info("Зупинка адаптера")
            sys.exit(0)
        except Exception as e:
            log.exception("Неочікувана помилка: %s", e)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
