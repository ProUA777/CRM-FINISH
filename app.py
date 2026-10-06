# -*- coding: utf-8 -*-
"""
CRM для металообробного підприємства (токарна, фрезерна та універсальна обробка) на власному парку верстатів з ЧПУ.
Монолітний застосунок на Flask + SQLite. Один файл, без зовнішньої БД.

Запуск:
    pip install -r requirements.txt
    python app.py
Відкрити: http://127.0.0.1:5000
Логін за замовчуванням: admin / admin123
"""

import os
import sqlite3
import functools
import datetime
import time
import sys

# Захист від UnicodeEncodeError на Windows, де консоль інколи використовує
# застарілу однобайтову кодировку без кирилиці (див. make_icon.py та
# launcher.py — там та сама проблема реально валила процес; тут вона лише
# псує читабельність попереджень типу warnings.warn нижче, але про всяк
# випадок захищаємось так само).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from werkzeug.security import generate_password_hash, check_password_hash
from flask import (
    Flask, request, session, redirect, url_for, g, render_template,
    flash, jsonify, abort
)
from jinja2 import DictLoader

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _default_data_dir():
    """Визначає, де зберігати crm.db, логи й бекапи.

    Критично для запуску як зібраного .exe (PyInstaller --onefile):
    BASE_DIR у такому разі вказує на ТИМЧАСОВУ теку розпакування
    (sys._MEIPASS), яка видаляється одразу після закриття програми —
    якщо зберігати базу даних туди, вона стиратиметься при кожному
    перезапуску. Тому для зібраного .exe свідомо використовуємо постійну
    домашню теку користувача, а не теку самої програми (це ж рішення
    заодно дозволяє встановлювати програму в Program Files без прав
    адміністратора на запис)."""
    if getattr(sys, "frozen", False):
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        else:
            base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        path = os.path.join(base, "OsnastkaMarket")
    else:
        path = BASE_DIR
    os.makedirs(path, exist_ok=True)
    return path


DATA_DIR = os.environ.get("CRM_DATA_DIR") or _default_data_dir()
DB_PATH = os.path.join(DATA_DIR, "crm.db")

def _get_or_create_secret_key():
    """Секретний ключ сесій: якщо адмін свідомо задав CRM_SECRET_KEY через
    змінну середовища - використовуємо його. Якщо ні (типовий випадок для
    звичайного запуску .exe без жодних налаштувань) - НЕ використовуємо
    вшитий у код типовий ключ (це була б дірка в безпеці, якщо програма
    стане доступна по мережі), а замість цього самі один раз генеруємо
    випадковий надійний ключ і зберігаємо його поруч із базою даних
    (DATA_DIR/secret_key.txt), щоб при наступних запусках сесії й логін
    не ламались. Це прибирає з користувача необхідність щось налаштовувати
    руками, зберігаючи той самий рівень безпеки."""
    env_key = os.environ.get("CRM_SECRET_KEY")
    if env_key:
        return env_key
    key_path = os.path.join(DATA_DIR, "secret_key.txt")
    if os.path.exists(key_path):
        try:
            with open(key_path, "r", encoding="utf-8") as f:
                saved = f.read().strip()
            if saved:
                return saved
        except Exception:
            pass
    import secrets as _secrets
    new_key = _secrets.token_urlsafe(48)
    try:
        with open(key_path, "w", encoding="utf-8") as f:
            f.write(new_key)
    except Exception:
        pass
    return new_key


# ---------------------------------------------------------------------------
# Шрифт для PDF (кирилиця)
# ---------------------------------------------------------------------------
# Стандартний Helvetica у reportlab не має кириличних гліфів: усі українські
# тексти в PDF виходили б рядками однакових "I". Тому реєструємо TTF-шрифт з
# кирилицею: DejaVu Sans з matplotlib (він іде в комплекті з .exe), інакше
# Arial з Windows чи DejaVu з Linux.

def _register_pdf_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    candidates = []
    try:
        import matplotlib
        base = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
        candidates.append((os.path.join(base, "DejaVuSans.ttf"), os.path.join(base, "DejaVuSans-Bold.ttf")))
    except Exception:
        pass
    windir = os.environ.get("WINDIR", r"C:\Windows")
    candidates.append((os.path.join(windir, "Fonts", "arial.ttf"), os.path.join(windir, "Fonts", "arialbd.ttf")))
    candidates.append(("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
    for regular, bold in candidates:
        if os.path.exists(regular) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("CRMSans", regular))
                pdfmetrics.registerFont(TTFont("CRMSans-Bold", bold))
                return "CRMSans", "CRMSans-Bold"
            except Exception:
                continue
    return "Helvetica", "Helvetica-Bold"


PDF_FONT, PDF_FONT_BOLD = _register_pdf_fonts()


app = Flask(__name__)
app.secret_key = _get_or_create_secret_key()
MAX_UPLOAD_SIZE_MB = 25

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("CRM_FORCE_HTTPS", "0") == "1",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12),
    MAX_CONTENT_LENGTH=MAX_UPLOAD_SIZE_MB * 1024 * 1024 + 1024 * 1024,  # +1МБ запасу на форму
)

# ---------------------------------------------------------------------------
# CSRF-захист (власна легка реалізація без зовнішніх залежностей)
# ---------------------------------------------------------------------------

def get_csrf_token():
    if "_csrf_token" not in session:
        import secrets as _secrets
        session["_csrf_token"] = _secrets.token_hex(24)
    return session["_csrf_token"]


app.jinja_env.globals["csrf_token"] = get_csrf_token


@app.before_request
def enforce_csrf():
    """Перевіряє прихований токен на кожному POST-запиті від браузера.
    API-ендпоінт телеметрії виключено — він автентифікується окремим
    API-ключем і викликається зовнішніми пристроями, а не браузером із
    сесією/кукою."""
    if request.method == "POST" and request.endpoint not in ("api_telemetry", "login"):
        sent = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
        expected = session.get("_csrf_token")
        if not expected or not sent or sent != expected:
            flash("Сесія застаріла або форма надіслана з іншого джерела. Спробуйте ще раз.", "error")
            return redirect(request.referrer or url_for("dashboard"))


# ---------------------------------------------------------------------------
# Захист від перебору паролів (login rate limiting)
# ---------------------------------------------------------------------------

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300


def _login_throttle_key():
    return request.remote_addr or "unknown"


def is_login_locked_out(key):
    """Зберігається в БД, а не в пам'яті процесу — важливо, бо в бою CRM
    зазвичай запущена через gunicorn з кількома worker-процесами, кожен
    зі своєю пам'яттю. In-memory лічильник в такому разі ефективно
    помножував би ліміт спроб на кількість воркерів."""
    db = get_db()
    cutoff = (datetime.datetime.now() - datetime.timedelta(seconds=LOGIN_LOCKOUT_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
    count = db.execute(
        "SELECT COUNT(*) c FROM login_attempts WHERE throttle_key=? AND created_at>=?", (key, cutoff)
    ).fetchone()["c"]
    return count >= LOGIN_MAX_ATTEMPTS


def register_failed_login(key):
    db = get_db()
    db.execute("INSERT INTO login_attempts (throttle_key, created_at) VALUES (?,?)", (key, now_iso()))
    db.commit()
    # Прибираємо старі записи, щоб таблиця не росла нескінченно.
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("DELETE FROM login_attempts WHERE created_at<?", (cutoff,))
    db.commit()

# ---------------------------------------------------------------------------
# Довідники предметної області (специфіка металообробного виробництва: токарна, фрезерна, універсальна обробка)
# ---------------------------------------------------------------------------

STAGES = [
    ("lead", "Новий лід", "#8a8f98"),
    ("qualify", "Кваліфікація потреби", "#7aa6c2"),
    ("spec", "Технічне завдання", "#4fa8e0"),
    ("proposal", "Комерційна пропозиція", "#4f8de0"),
    ("negotiation", "Переговори / умови", "#e0b34f"),
    ("contract", "Договір підписано", "#e08a4f"),
    ("prepay", "Передоплата отримана", "#e0704f"),
    ("production", "Виробництво", "#c2604f"),
    ("shipping", "Відвантаження", "#9a6fd1"),
    ("install", "Контроль якості (ВТК)", "#5fb27a"),
    ("won", "Угода закрита (виграно)", "#3fae5c"),
]
STAGE_KEYS = [s[0] for s in STAGES]
STAGE_LABEL = {s[0]: s[1] for s in STAGES}
STAGE_COLOR = {s[0]: s[2] for s in STAGES}
LOST_STAGE = "lost"
STAGE_LABEL[LOST_STAGE] = "Програно / відмова"
STAGE_COLOR[LOST_STAGE] = "#8a3f3f"

MACHINE_CATEGORIES = [
    "Токарний верстат з ЧПУ",
    "Фрезерний обробний центр",
    "Універсальний токарно-фрезерний верстат",
    "Свердлильно-фрезерний верстат",
    "Шліфувальний верстат з ЧПУ",
    "Лазерний різальний комплекс",
    "Плазмовий різальний комплекс",
    "Заготовки та оснащення",
]

PRIORITIES = [("low", "Низький"), ("medium", "Середній"), ("high", "Високий")]
CURRENCIES = ["UAH", "USD", "EUR", "PLN"]
LEAD_SOURCES = ["Виставка", "Сайт / заявка", "Холодний дзвінок", "Рекомендація",
                "Постійний клієнт", "Тендер", "Партнер / дилер"]
TASK_TYPES = [("call", "Дзвінок"), ("meeting", "Зустріч"), ("email", "Лист"),
              ("visit", "Виїзд на об'єкт"), ("other", "Інше")]
SERVICE_STATUSES = [("new", "Нова заявка"), ("diagnostics", "Діагностика"),
                     ("in_progress", "Виконується"), ("waiting_parts", "Очікування запчастин"),
                     ("done", "Виконано"), ("cancelled", "Скасовано")]
SERVICE_PRIORITIES = PRIORITIES

PAYMENT_KINDS = [("prepayment", "Передоплата"), ("balance", "Доплата"), ("final", "Остаточний розрахунок"),
                  ("service", "Оплата сервісу"), ("other", "Інше")]
PAYMENT_STATUSES = [("pending", "Очікується"), ("paid", "Оплачено"), ("overdue", "Прострочено")]

ROLES = [
    ("admin", "Адміністратор"),
    ("sales", "Менеджер продажів"),
    ("production", "Виробництво"),
    ("warehouse", "Склад"),
    ("service", "Сервіс"),
    ("accountant", "Бухгалтер"),
    ("viewer", "Перегляд"),
]
ROLE_LABEL = dict(ROLES)

PRODUCTION_ORDER_STATUSES = [("planned", "Заплановано"), ("in_progress", "У виробництві"),
                              ("done", "Завершено"), ("cancelled", "Скасовано")]
OPERATION_STATUSES = [("waiting", "Очікує"), ("in_progress", "Виконується"), ("done", "Виконано")]
WAREHOUSE_TX_TYPES = [("in", "Прихід"), ("out", "Видача / списання"), ("adjust", "Інвентаризація")]
WORK_DAY_HOURS = 8

# --- Вкладення файлів ---
# Білий список розширень: свідомо НЕ включаємо виконувані файли (.exe, .bat,
# .sh, .js, .html тощо) - завантаження довільних файлів є однією з
# найпоширеніших вразливостей веб-застосунків (виконання коду, stored XSS
# через SVG/HTML). Дозволяємо лише формати, реально потрібні цьому бізнесу:
# документи, зображення, креслення, архіви.
ALLOWED_UPLOAD_EXTENSIONS = {
    "pdf", "doc", "docx", "xls", "xlsx", "txt", "csv",
    "jpg", "jpeg", "png", "gif", "webp",
    "dxf", "step", "stp", "dwg", "iges", "igs",
    "zip", "rar", "7z",
}
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

CUSTOM_FIELD_TYPES = [("text", "Текст"), ("number", "Число"), ("date", "Дата"), ("select", "Список")]
SCRAP_STATUSES = [("available", "Доступний"), ("used", "Використаний"), ("scrapped", "Списаний")]

# Автоматичні задачі, що створюються при переході угоди на певну стадію.
# (заголовок задачі, тип задачі, зсув у днях від сьогодні)
AUTO_TASKS_ON_STAGE = {
    "spec": ("Підготувати технічне завдання та узгодити креслення з клієнтом", "other", 2),
    "proposal": ("Надіслати комерційну пропозицію клієнту", "email", 1),
    "contract": ("Підготувати договір та рахунок на передоплату", "other", 2),
    "prepay": ("Передати замовлення у виробництво", "other", 1),
    "production": ("Проконтролювати хід виготовлення замовлення", "call", 7),
    "shipping": ("Узгодити дату та спосіб відвантаження готових деталей", "call", 2),
    "install": ("Провести контроль якості готових деталей перед відвантаженням", "other", 3),
    "won": ("Архівувати технічну документацію по завершеному замовленню", "other", 1),
}

# ---------------------------------------------------------------------------
# База даних
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'manager',
    active INTEGER NOT NULL DEFAULT 1,
    work_center_id INTEGER
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    edrpou TEXT,
    industry TEXT,
    city TEXT,
    address TEXT,
    phone TEXT,
    email TEXT,
    website TEXT,
    source TEXT,
    manager_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    full_name TEXT NOT NULL,
    position TEXT,
    phone TEXT,
    email TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    model TEXT NOT NULL,
    description TEXT,
    spindle_power TEXT,
    work_area TEXT,
    accuracy TEXT,
    price REAL,
    currency TEXT DEFAULT 'USD',
    lead_time_days INTEGER,
    in_stock INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    client_id INTEGER NOT NULL,
    contact_id INTEGER,
    machine_id INTEGER,
    stage TEXT NOT NULL DEFAULT 'lead',
    amount REAL DEFAULT 0,
    currency TEXT DEFAULT 'USD',
    prepayment_percent INTEGER DEFAULT 30,
    probability INTEGER DEFAULT 20,
    manager_id INTEGER,
    expected_close TEXT,
    source TEXT,
    priority TEXT DEFAULT 'medium',
    competitor TEXT,
    tech_requirements TEXT,
    closed_reason TEXT,
    closed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(client_id) REFERENCES clients(id),
    FOREIGN KEY(machine_id) REFERENCES machines(id)
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER,
    client_id INTEGER,
    type TEXT NOT NULL,
    text TEXT NOT NULL,
    manager_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER,
    client_id INTEGER,
    title TEXT NOT NULL,
    description TEXT,
    type TEXT DEFAULT 'call',
    due_date TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    manager_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    throttle_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size_bytes INTEGER,
    uploaded_by INTEGER,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_field_defs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    field_key TEXT NOT NULL,
    label TEXT NOT NULL,
    field_type TEXT NOT NULL DEFAULT 'text',
    options TEXT,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS custom_field_values (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    value TEXT,
    UNIQUE(field_id, entity_id)
);

CREATE TABLE IF NOT EXISTS material_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    heat_number TEXT,
    certificate_number TEXT,
    supplier TEXT,
    qty REAL NOT NULL DEFAULT 0,
    received_date TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scrap_offcuts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_name TEXT NOT NULL,
    thickness_mm REAL,
    length_mm REAL,
    width_mm REAL,
    weight_kg REAL,
    location TEXT,
    source_order_id INTEGER,
    status TEXT NOT NULL DEFAULT 'available',
    created_at TEXT NOT NULL,
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS work_centers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT,
    capacity_hours_per_day REAL NOT NULL DEFAULT 8,
    mtc_host TEXT,
    mtc_port INTEGER DEFAULT 8082,
    mtc_enabled INTEGER DEFAULT 0
);

-- Змінний журнал виробництва: окремий, незалежний від виробничих замовлень
-- облік "скільки хто зробив за зміну" (на явний запит користувача - просто
-- й швидко заповнюється, не залежить від того, чи заведене відповідне
-- замовлення в CRM).
CREATE TABLE IF NOT EXISTS shift_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_date TEXT NOT NULL,
    shift TEXT NOT NULL DEFAULT 'day',
    work_center_id INTEGER,
    worker_user_id INTEGER,
    part_description TEXT NOT NULL,
    quantity_made INTEGER NOT NULL DEFAULT 0,
    quantity_scrap INTEGER NOT NULL DEFAULT 0,
    scrap_reason TEXT,
    shift_hours REAL,
    setup_hours REAL,
    notes TEXT,
    created_by_user_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    FOREIGN KEY(work_center_id) REFERENCES work_centers(id),
    FOREIGN KEY(worker_user_id) REFERENCES users(id),
    FOREIGN KEY(created_by_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS routing_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routing_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    routing_template_id INTEGER NOT NULL,
    step_order INTEGER NOT NULL,
    operation_name TEXT NOT NULL,
    work_center_id INTEGER,
    planned_hours REAL NOT NULL DEFAULT 4,
    material_item_id INTEGER,
    material_qty REAL DEFAULT 0,
    FOREIGN KEY(routing_template_id) REFERENCES routing_templates(id)
);

CREATE TABLE IF NOT EXISTS production_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER,
    machine_id INTEGER,
    routing_template_id INTEGER,
    quantity INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'planned',
    due_date TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS production_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    production_order_id INTEGER NOT NULL,
    step_order INTEGER NOT NULL,
    operation_name TEXT NOT NULL,
    work_center_id INTEGER,
    planned_hours REAL NOT NULL DEFAULT 4,
    status TEXT NOT NULL DEFAULT 'waiting',
    planned_start TEXT,
    planned_end TEXT,
    actual_start TEXT,
    actual_end TEXT,
    operator TEXT,
    FOREIGN KEY(production_order_id) REFERENCES production_orders(id)
);

CREATE TABLE IF NOT EXISTS warehouse_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT UNIQUE,
    name TEXT NOT NULL,
    category TEXT,
    unit TEXT DEFAULT 'шт',
    qty_on_hand REAL NOT NULL DEFAULT 0,
    min_qty REAL NOT NULL DEFAULT 0,
    unit_cost REAL DEFAULT 0,
    currency TEXT DEFAULT 'UAH',
    location TEXT
);

CREATE TABLE IF NOT EXISTS warehouse_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    qty_delta REAL NOT NULL,
    type TEXT NOT NULL,
    reference TEXT,
    user_id INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY(item_id) REFERENCES warehouse_items(id)
);

CREATE TABLE IF NOT EXISTS service_ticket_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    qty REAL NOT NULL DEFAULT 1,
    FOREIGN KEY(ticket_id) REFERENCES service_tickets(id),
    FOREIGN KEY(item_id) REFERENCES warehouse_items(id)
);

CREATE TABLE IF NOT EXISTS parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    drawing_no TEXT,
    client_id INTEGER,
    material TEXT,
    routing_text TEXT,
    last_price REAL,
    currency TEXT DEFAULT 'UAH',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS quality_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    production_order_id INTEGER,
    work_center_id INTEGER,
    inspector_user_id INTEGER,
    qty_checked INTEGER NOT NULL DEFAULT 0,
    qty_defect INTEGER NOT NULL DEFAULT 0,
    defect_reason TEXT,
    checklist TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    user_id INTEGER,
    username TEXT,
    method TEXT,
    endpoint TEXT,
    path TEXT,
    label TEXT,
    detail TEXT,
    result TEXT
);

CREATE TABLE IF NOT EXISTS rates_history (
    recorded_on TEXT PRIMARY KEY,
    usd REAL,
    eur REAL,
    pln REAL,
    source TEXT
);

CREATE TABLE IF NOT EXISTS machine_telemetry (
    work_center_id INTEGER PRIMARY KEY,
    status TEXT DEFAULT 'offline',
    spindle_load_pct REAL,
    cycle_count INTEGER,
    power_on_hours REAL,
    motion_hours REAL,
    part_count INTEGER,
    program_name TEXT,
    mode TEXT,
    source TEXT DEFAULT 'generic',
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS machine_telemetry_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_center_id INTEGER NOT NULL,
    status TEXT,
    spindle_load_pct REAL,
    motion_hours REAL,
    power_on_hours REAL,
    part_count INTEGER,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS client_equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    machine_id INTEGER,
    serial_number TEXT,
    install_date TEXT,
    warranty_until TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(client_id) REFERENCES clients(id)
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'prepayment',
    amount REAL NOT NULL DEFAULT 0,
    currency TEXT DEFAULT 'USD',
    status TEXT NOT NULL DEFAULT 'pending',
    due_date TEXT,
    paid_date TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(deal_id) REFERENCES deals(id)
);

CREATE TABLE IF NOT EXISTS service_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    deal_id INTEGER,
    machine_id INTEGER,
    issue TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    priority TEXT DEFAULT 'medium',
    engineer TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

-- Історія всіх розрахунків вартості (ручних і через ШІ) - кожен завершений
-- розрахунок у калькуляторі зберігається сюди автоматично, щоб потім можна
-- було переглянути, хто і коли рахував яку деталь та за якою ціною.
CREATE TABLE IF NOT EXISTS cost_calculations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    source TEXT NOT NULL DEFAULT 'manual',
    part_description TEXT,
    quantity INTEGER NOT NULL DEFAULT 1,
    weight_kg REAL,
    material_price_per_kg REAL,
    turning_hours REAL,
    turning_rate_per_hour REAL,
    milling_hours REAL,
    milling_rate_per_hour REAL,
    machine_hours REAL,
    machine_rate_per_hour REAL,
    setup_cost_total REAL,
    margin_pct REAL,
    cost_per_unit REAL,
    price_per_unit REAL,
    total_price REAL,
    operations_description TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
"""


def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        # WAL дозволяє одночасне читання під час запису (кілька менеджерів
        # одночасно в CRM не блокують одне одного); busy_timeout — якщо два
        # запити все ж зіткнуться на записі, другий почекає, а не впаде
        # одразу з "database is locked".
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
    return db


@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return datetime.date.today().strftime("%Y-%m-%d")


def save_calculation_history(db, user, source, part_description, result, raw_inputs=None, operations_description=None):
    """Зберігає завершений розрахунок вартості (ручний чи через ШІ) в
    історію - викликається одразу після того, як калькулятор порахував
    result (той самий dict, що йде в шаблон), щоб пізніше можна було
    переглянути весь список розрахунків: хто, коли, яка деталь і за якою
    ціною.

    raw_inputs: словник із вхідними параметрами (weight_kg,
    material_price_per_kg, turning_hours, ...) - result містить лише вже
    ПОРАХОВАНІ суми (material_cost тощо), а не самі вхідні числа, тож їх
    треба передати окремо (для ШІ-розрахунку це result["ai_inputs"], для
    ручного - локальні змінні з форми).

    Не кидає винятків назовні - збій запису історії не має ламати сам
    розрахунок користувачу."""
    raw_inputs = raw_inputs or {}
    try:
        db.execute(
            "INSERT INTO cost_calculations (user_id, source, part_description, quantity, weight_kg, "
            "material_price_per_kg, turning_hours, turning_rate_per_hour, milling_hours, milling_rate_per_hour, "
            "machine_hours, machine_rate_per_hour, setup_cost_total, margin_pct, cost_per_unit, price_per_unit, "
            "total_price, operations_description, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user["id"] if user else None, source, (part_description or "").strip()[:500],
                result.get("quantity") or 1,
                raw_inputs.get("weight_kg"), raw_inputs.get("material_price_per_kg"),
                raw_inputs.get("turning_hours"), raw_inputs.get("turning_rate_per_hour"),
                raw_inputs.get("milling_hours"), raw_inputs.get("milling_rate_per_hour"),
                raw_inputs.get("machine_hours"), raw_inputs.get("machine_rate_per_hour"),
                result.get("setup_cost_total"), result.get("margin_pct"),
                result.get("cost_per_unit"), result.get("price_per_unit"), result.get("total_price"),
                (operations_description or result.get("ai_operations_description") or "").strip()[:20000],
                now_iso(),
            ),
        )
        db.commit()
    except Exception:
        pass


def migrate_schema(db):
    """Безпечно додає нові колонки до вже існуючих таблиць у старій базі
    даних користувача. 'CREATE TABLE IF NOT EXISTS' створює нові таблиці,
    але НЕ додає нові колонки в таблиці, які вже існували на диску — тому
    без цієї міграції оновлення коду CRM ламало б роботу зі старою crm.db
    (SQLite повертав би 'no such column'). Виконується при кожному запуску,
    без втрати наявних даних."""
    expected_columns = {
        "deals": [("closed_at", "TEXT")],
        "machine_telemetry": [
            ("power_on_hours", "REAL"), ("motion_hours", "REAL"), ("part_count", "INTEGER"),
            ("program_name", "TEXT"), ("mode", "TEXT"), ("source", "TEXT DEFAULT 'generic'"),
        ],
        "users": [("work_center_id", "INTEGER")],
        "work_centers": [("mtc_host", "TEXT"), ("mtc_port", "INTEGER DEFAULT 8082"),
                         ("mtc_enabled", "INTEGER DEFAULT 0")],
    }
    for table, columns in expected_columns.items():
        table_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not table_exists:
            continue  # таблиці ще немає (стара база до появи цієї сутності) - CREATE TABLE IF NOT EXISTS вище вже її створив з усіма колонками одразу
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        for col_name, col_def in columns:
            if col_name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
        # стара база без злотого: додаємо стартовий курс (його можна оновити з НБУ)
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('rate_pln', '10.5')")
    db.commit()


def init_db():
    fresh = not os.path.exists(DB_PATH)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    migrate_schema(db)
    db.commit()
    if fresh:
        seed_data(db)
    db.close()


def seed_data(db):
    cur = db.cursor()
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
        ("admin", generate_password_hash("admin123"), "Адміністратор", "admin"),
    )
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
        ("oleh", generate_password_hash("manager123"), "Олег Ковальчук", "sales"),
    )
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
        ("maister", generate_password_hash("prod123"), "Тарас Майстренко", "production"),
    )
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role, work_center_id) VALUES (?,?,?,?,?)",
        ("tokar", generate_password_hash("tokar123"), "Василь Токарук", "production", 1),
    )
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role, work_center_id) VALUES (?,?,?,?,?)",
        ("frezer", generate_password_hash("frezer123"), "Андрій Фрезеренко", "production", 3),
    )
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
        ("sklad", generate_password_hash("sklad123"), "Ірина Комірник", "warehouse"),
    )
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
        ("servis", generate_password_hash("servis123"), "Микола Швець", "service"),
    )
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
        ("buh", generate_password_hash("buh12345"), "Олена Бухгалтер", "accountant"),
    )
    admin_id, oleh_id = 1, 2

    machines = [
        ("Токарний верстат з ЧПУ", "TurnMaster CNC-320", "Токарний верстат для важких деталей", "11 кВт", "Ø320x1000 мм", "0.01 мм", 42000, "USD", 45, 1),
        ("Фрезерний обробний центр", "MillPro VMC-850", "Вертикальний обробний центр, 3 осі", "15 кВт", "850x500x500 мм", "0.005 мм", 65000, "USD", 60, 0),
        ("Універсальний токарно-фрезерний верстат", "UniTech U-500", "Універсал для малих серій та ремонту", "7.5 кВт", "500x300 мм", "0.02 мм", 28000, "USD", 30, 1),
        ("Свердлильно-фрезерний верстат", "DrillFix DF-210", "Компактний верстат для цехів", "3 кВт", "210x210 мм", "0.03 мм", 9500, "USD", 20, 1),
        ("Шліфувальний верстат з ЧПУ", "GrindLine GL-400", "Кругло-шліфувальний верстат", "5.5 кВт", "Ø400x800 мм", "0.002 мм", 51000, "USD", 50, 0),
    ]
    cur.executemany(
        "INSERT INTO machines (category, model, description, spindle_power, work_area, accuracy, price, currency, lead_time_days, in_stock) VALUES (?,?,?,?,?,?,?,?,?,?)",
        machines,
    )

    clients = [
        ("ТОВ «Металпром»", "12345678", "Металообробка", "Харків", "вул. Індустріальна, 5", "+380 57 700 11 22", "info@metalprom.ua", "metalprom.ua", "Виставка", admin_id, "active", "Постійний клієнт, замовляють раз на рік"),
        ("ПрАТ «Дніпровагонбуд»", "23456789", "Машинобудування", "Дніпро", "вул. Заводська, 12", "+380 56 222 33 44", "sales@dvb.ua", "dvb.ua", "Тендер", oleh_id, "active", "Держзамовлення, довгий цикл узгодження"),
        ("ФОП Гриценко О.М.", None, "Ремонтна майстерня", "Львів", "вул. Городоцька, 88", "+380 67 111 22 33", "gritsenko@gmail.com", None, "Сайт / заявка", oleh_id, "active", None),
        ("ТОВ «АгроТехМаш»", "34567890", "Виробництво с/г техніки", "Вінниця", "вул. Хмельницьке шосе, 40", "+380 43 255 66 77", "office@agrotechmash.ua", "agrotechmash.ua", "Рекомендація", admin_id, "active", "Плановий апгрейд парку обладнання"),
        ("ТОВ «Стальконструкція»", "45678901", "Металоконструкції", "Кривий Ріг", "вул. Металургів, 3", "+380 56 400 55 11", "info@stalkon.ua", None, "Холодний дзвінок", oleh_id, "lost", "Обрали конкурента через ціну"),
        ("ТОВ «ПромЕлектро»", "56789012", "Електрообладнання", "Запоріжжя", "вул. South Core, 14", "+380 61 270 10 10", "sales@promelektro.ua", "promelektro.ua", "Виставка", oleh_id, "active", "Регулярні замовлення дрібних партій валів"),
        ("ФОП Сидоренко В.І.", None, "Ремонт сільгосптехніки", "Полтава", "вул. Європейська, 21", "+380 53 212 34 56", "sydorenko.vi@gmail.com", None, "Сайт / заявка", oleh_id, "active", "Сезонні замовлення перед посівною"),
        ("ТОВ «БудМаш»", "67890123", "Будівельна техніка", "Київ", "просп. Перемоги, 110", "+380 44 390 12 34", "office@budmash.ua", "budmash.ua", "Рекомендація", admin_id, "active", "Велике замовлення на капремонт парку"),
        ("ПрАТ «Вагонремонт»", "78901234", "Залізничне машинобудування", "Харків", "вул. Залізнична, 2", "+380 57 714 20 20", "info@vagonremont.ua", "vagonremont.ua", "Тендер", admin_id, "active", "Держпідприємство, суворі вимоги до якості"),
        ("ТОВ «ТехноЛінія»", "89012345", "Верстатобудування", "Одеса", "вул. Промислова, 9", "+380 48 711 45 67", "sales@technolinia.ua", "technolinia.ua", "Холодний дзвінок", oleh_id, "active", "Новий клієнт, перше замовлення на пробу"),
    ]
    cur.executemany(
        "INSERT INTO clients (name, edrpou, industry, city, address, phone, email, website, source, manager_id, status, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [c + (now_iso(),) for c in clients],
    )

    contacts = [
        (1, "Іван Петренко", "Головний технолог", "+380 50 111 22 33", "petrenko@metalprom.ua", 1),
        (1, "Марія Ковтун", "Постачання", "+380 50 111 22 34", "kovtun@metalprom.ua", 0),
        (2, "Сергій Литвин", "Начальник виробництва", "+380 67 222 33 44", "lytvyn@dvb.ua", 1),
        (3, "Олександр Гриценко", "Власник", "+380 67 111 22 33", "gritsenko@gmail.com", 1),
        (4, "Наталія Бондар", "Директор з виробництва", "+380 43 255 66 78", "bondar@agrotechmash.ua", 1),
        (5, "Роман Дацюк", "Закупівлі", "+380 56 400 55 12", "datsuk@stalkon.ua", 1),
        (6, "Андрій Петраш", "Головний інженер", "+380 61 270 10 11", "petrash@promelektro.ua", 1),
        (7, "Василь Сидоренко", "Власник", "+380 53 212 34 56", "sydorenko.vi@gmail.com", 1),
        (8, "Людмила Карпенко", "Керівник закупівель", "+380 44 390 12 35", "karpenko@budmash.ua", 1),
        (9, "Олег Шевцов", "Начальник цеху", "+380 57 714 20 21", "shevtsov@vagonremont.ua", 1),
        (10, "Ганна Мороз", "Технічний директор", "+380 48 711 45 68", "moroz@technolinia.ua", 1),
    ]
    cur.executemany(
        "INSERT INTO contacts (client_id, full_name, position, phone, email, is_primary) VALUES (?,?,?,?,?,?)",
        contacts,
    )

    d0 = datetime.date.today()

    def d(offset):
        return (d0 + datetime.timedelta(days=offset)).strftime("%Y-%m-%d")

    deals = [
        ("Виготовлення валів за кресленням (токарна обробка)", 1, 1, 1, "negotiation", 42000, "USD", 30, 60, admin_id, d(20), "Виставка", "high", "Konkurent CNC", "Потрібна точність 0.01 мм, робота з валами до 1000 мм", None),
        ("Виготовлення корпусних деталей (фрезерування)", 2, 3, 2, "spec", 65000, "USD", 40, 35, oleh_id, d(45), "Тендер", "high", None, "Тендерна документація, держзакупівля", None),
        ("Ремонтна партія деталей за кресленням клієнта", 3, 4, 3, "proposal", 28000, "USD", 30, 50, oleh_id, d(15), "Сайт / заявка", "medium", None, "Малосерійне виробництво, обмежений бюджет", None),
        ("Шліфування партії валів — цех клієнта", 4, 5, 5, "contract", 51000, "USD", 50, 80, admin_id, d(10), "Рекомендація", "high", None, "Потрібне пакування та доставка на об'єкт клієнта", None),
        ("Свердлильні роботи для цеху №2", 1, 2, 4, "prepay", 9500, "USD", 50, 90, admin_id, d(7), "Постійний клієнт", "medium", None, None, None),
        ("Переробка партії деталей за старим кресленням (універсал)", 2, 3, 3, "lead", 18000, "USD", 30, 15, oleh_id, d(60), "Виставка", "medium", "Konkurent CNC", "Мала серія, потрібна доробка під нове креслення замовника", None),
        ("Виготовлення партії кронштейнів (програно)", 5, 6, 1, "lost", 40000, "USD", 30, 0, oleh_id, d(-10), "Холодний дзвінок", "low", "Konkurent CNC", None, "Клієнт обрав дешевшого підрядника"),
        ("Виготовлення деталей для обробного центру клієнта", 4, 5, 2, "install", 65000, "USD", 40, 95, admin_id, d(3), "Рекомендація", "high", None, "Контроль якості заплановано на наступний тиждень", None),
        # Ці дві угоди додані навмисно, щоб на СВІЖІЙ демо-базі виробниче
        # замовлення охоплювало ВСІ 7 дільниць цеху з коробки (без них
        # Токарна дільниця №1 і Шліфувальна дільниця лишались порожніми -
        # на жодній не було жодного завдання, що й помітив користувач).
        ("Виготовлення валів партія №2 (токарна дільниця)", 1, 1, 1, "prepay", 15000, "USD", 30, 70, admin_id, d(12), "Постійний клієнт", "medium", None, "Партія валів для поточного замовлення, токарна дільниця", None),
        ("Шліфування посадкових втулок — друга партія", 4, 5, 5, "production", 22000, "USD", 40, 85, oleh_id, d(8), "Рекомендація", "medium", None, "Партія втулок на шліфування, контроль точності 0.002 мм", None),
        # --- Додаткові демо-угоди: більше клієнтів, стадій і верстатів для
        # наочнішої воронки продажів і завантаженого виробництва на демо ---
        ("Виготовлення партії валів для електродвигунів", 6, 7, 1, "negotiation", 19500, "USD", 30, 55, oleh_id, d(25), "Виставка", "medium", "Konkurent CNC", "Партія 40 шт, допуск 0.01 мм", None),
        ("Ремонт деталей кормозбирального комбайна", 7, 8, 3, "lead", 6200, "USD", 30, 20, oleh_id, d(35), "Сайт / заявка", "low", None, "Сезонне замовлення перед посівною", None),
        ("Капремонт парку будівельної техніки — партія 1", 8, 9, 2, "spec", 88000, "USD", 40, 40, admin_id, d(50), "Рекомендація", "high", "Konkurent CNC", "Велика партія, потрібна тендерна документація", None),
        ("Виготовлення осей для вагонних візків", 9, 10, 1, "proposal", 54000, "USD", 30, 45, admin_id, d(40), "Тендер", "high", None, "Держпідприємство, суворий контроль якості за ДСТУ", None),
        ("Пробна партія корпусних деталей", 10, 11, 3, "contract", 14300, "USD", 40, 75, oleh_id, d(18), "Холодний дзвінок", "medium", None, "Перше замовлення, важливо не підвести з термінами", None),
        ("Виготовлення валів для електродвигунів — партія 2", 6, 7, 1, "prepay", 21000, "USD", 30, 70, oleh_id, d(14), "Постійний клієнт", "medium", None, "Повторне замовлення, той самий техпроцес", None),
        ("Фрезерування кронштейнів кріплення", 8, 9, 2, "production", 31000, "USD", 40, 80, admin_id, d(9), "Рекомендація", "high", None, "Контроль площинності після фрезерування", None),
        ("Шліфування осей — партія для вагоноремонту", 9, 10, 5, "shipping", 47000, "USD", 30, 92, admin_id, d(4), "Тендер", "high", None, "Готово до відвантаження, очікує транспорт", None),
        ("Свердлильні роботи для цеху ПромЕлектро", 6, 7, 4, "install", 8700, "USD", 50, 97, oleh_id, d(2), "Постійний клієнт", "medium", None, "Монтаж на об'єкті клієнта, завершальний етап", None),
        ("Виготовлення пробної партії осей — виграно", 10, 11, 1, "won", 14300, "USD", 40, 100, oleh_id, d(-5), "Холодний дзвінок", "medium", None, None, "Клієнт задоволений якістю, плановий обсяг на наступний квартал"),
        ("Ремонтна партія для БудМаш — програно", 8, 9, 3, "lost", 26000, "USD", 30, 0, admin_id, d(-15), "Рекомендація", "low", "Konkurent CNC", None, "Клієнт обрав постачальника з коротшим терміном"),
    ]
    cur.executemany(
        """INSERT INTO deals (title, client_id, contact_id, machine_id, stage, amount, currency,
        prepayment_percent, probability, manager_id, expected_close, source, priority, competitor,
        tech_requirements, closed_reason, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [dl + (now_iso(), now_iso()) for dl in deals],
    )

    activities = [
        (1, 1, "call", "Дзвінок технологу — уточнили розміри деталей, надіслали типові рішення.", admin_id),
        (1, 1, "meeting", "Зустріч на виробництві клієнта, огляд креслень, уточнення допусків.", admin_id),
        (2, 2, "email", "Надіслано технічну специфікацію для тендерної документації.", oleh_id),
        (4, 4, "call", "Погоджено умови оплати 50/50, готуємо договір.", admin_id),
        (8, 4, "visit", "Виїзд інженера для приймання готової партії деталей клієнтом.", admin_id),
        (11, 6, "call", "Узгодили технічні вимоги до партії валів, готуємо КП.", oleh_id),
        (14, 9, "meeting", "Зустріч з представниками вагоноремонтного заводу, обговорили ДСТУ.", admin_id),
        (18, 9, "call", "Підтверджено готовність партії до відвантаження, узгоджено транспорт.", admin_id),
        (19, 6, "visit", "Виїзд на монтаж деталей у цеху клієнта.", oleh_id),
    ]
    cur.executemany(
        "INSERT INTO activities (deal_id, client_id, type, text, manager_id, created_at) VALUES (?,?,?,?,?,?)",
        [a + (now_iso(),) for a in activities],
    )

    tasks = [
        (1, 1, "Надіслати оновлене КП з розстрочкою", "Врахувати знижку 5% за обсяг", "email", d(2), 0, admin_id),
        (2, 2, "Подзвонити щодо статусу тендеру", None, "call", d(1), 0, oleh_id),
        (3, 3, "Зустріч з власником щодо бюджету", None, "meeting", d(3), 0, oleh_id),
        (4, 4, "Підготувати договір на підпис", None, "other", d(1), 0, admin_id),
        (8, 4, "Контроль якості партії перед відвантаженням", "Перевірити відповідність кресленню, підготувати протокол ВТК", "visit", d(5), 0, admin_id),
        (None, 1, "Привітати з річницею співпраці", None, "call", d(30), 0, admin_id),
        (13, 8, "Підготувати тендерну документацію на капремонт", "Велика партія, перевірити всі специфікації", "other", d(4), 0, admin_id),
        (15, 10, "Підписати договір з новим клієнтом", None, "other", d(2), 0, oleh_id),
        (18, 9, "Організувати транспорт для відвантаження", "Партія осей для вагоноремонту", "call", d(1), 0, admin_id),
        (20, 10, "Узгодити плановий обсяг на наступний квартал", None, "call", d(10), 0, oleh_id),
    ]
    cur.executemany(
        "INSERT INTO tasks (deal_id, client_id, title, description, type, due_date, done, manager_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [t + (now_iso(),) for t in tasks],
    )

    tickets = [
        (1, None, 1, "Шпиндель видає підвищений шум на високих обертах", "in_progress", "high", "Микола Швець", now_iso(), None),
        (4, 2, 2, "Планове ТО після 500 год напрацювання", "new", "medium", None, now_iso(), None),
        (3, None, 3, "Похибка позиціонування по осі X понад допуск", "diagnostics", "high", "Микола Швець", now_iso(), None),
    ]
    cur.executemany(
        "INSERT INTO service_tickets (client_id, deal_id, machine_id, issue, status, priority, engineer, created_at, resolved_at) VALUES (?,?,?,?,?,?,?,?,?)",
        tickets,
    )

    payments = [
        (4, "prepayment", 25500, "USD", "paid", d(-5), d(-6)),
        (4, "balance", 25500, "USD", "pending", d(10), None),
        (5, "prepayment", 4750, "USD", "paid", d(-3), d(-3)),
        (5, "balance", 4750, "USD", "pending", d(4), None),
        (1, "prepayment", 12600, "USD", "pending", d(5), None),
        (16, "prepayment", 6300, "USD", "paid", d(-2), d(-2)),
        (16, "balance", 14700, "USD", "pending", d(14), None),
        (18, "prepayment", 14100, "USD", "paid", d(-20), d(-20)),
        (18, "balance", 32900, "USD", "pending", d(4), None),
        (19, "prepayment", 4350, "USD", "paid", d(-10), d(-10)),
        (19, "balance", 4350, "USD", "paid", d(-1), d(-1)),
        (20, "prepayment", 5720, "USD", "paid", d(-30), d(-30)),
        (20, "balance", 8580, "USD", "paid", d(-6), d(-6)),
    ]
    cur.executemany(
        "INSERT INTO payments (deal_id, kind, amount, currency, status, due_date, paid_date, created_at) VALUES (?,?,?,?,?,?,?,?)",
        [p + (now_iso(),) for p in payments],
    )

    cur.execute("UPDATE deals SET closed_at=? WHERE stage IN ('won','lost')", (now_iso(),))

    # --- Виробництво: робочі центри та маршрутні карти ---
    work_centers = [
        ("Заготівельна дільниця", "Заготівля", 8),
        ("Токарна дільниця №1", "Токарна", 16),
        ("Фрезерна дільниця", "Фрезерна", 16),
        ("Шліфувальна дільниця", "Шліфувальна", 8),
        ("Складальна дільниця", "Збірка", 8),
        ("ВТК", "Контроль якості", 8),
        ("Пакування та відвантаження", "Логістика", 8),
    ]
    cur.executemany("INSERT INTO work_centers (name, type, capacity_hours_per_day) VALUES (?,?,?)", work_centers)

    routing_defs = {
        "Токарний верстат з ЧПУ": [
            ("Розкрій та підготовка заготовки", 1, 4),
            ("Чорнове точіння", 2, 10),
            ("Чистове точіння за програмою ЧПУ", 2, 8),
            ("Контроль розмірів (ВТК)", 6, 2),
            ("Упаковка та підготовка до відвантаження", 7, 2),
        ],
        "Фрезерний обробний центр": [
            ("Підготовка заготовки", 1, 4),
            ("Чорнова фрезерна обробка", 3, 14),
            ("Чистова фрезерна обробка", 3, 10),
            ("Контроль геометрії (ВТК)", 6, 3),
            ("Упаковка та відвантаження", 7, 2),
        ],
        "Універсальний токарно-фрезерний верстат": [
            ("Підготовка вузлів", 1, 3),
            ("Токарна обробка станини", 2, 8),
            ("Фрезерна обробка супорта", 3, 8),
            ("Складання та юстування", 5, 10),
            ("Контроль точності (ВТК)", 6, 3),
            ("Упаковка та відвантаження", 7, 2),
        ],
        "Свердлильно-фрезерний верстат": [
            ("Підготовка вузлів", 1, 2),
            ("Механообробка станини", 3, 6),
            ("Складання", 5, 6),
            ("Контроль (ВТК)", 6, 2),
            ("Упаковка", 7, 1),
        ],
        "Шліфувальний верстат з ЧПУ": [
            ("Підготовка заготовки", 1, 3),
            ("Попереднє шліфування", 4, 10),
            ("Чистове шліфування", 4, 8),
            ("Контроль точності (ВТК)", 6, 3),
            ("Упаковка та відвантаження", 7, 2),
        ],
    }
    for category, steps in routing_defs.items():
        cur.execute("INSERT INTO routing_templates (category, name) VALUES (?,?)",
                    (category, f"Типовий техпроцес: {category}"))
        rt_id = cur.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        for i, (op_name, wc_id, hours) in enumerate(steps, start=1):
            cur.execute(
                "INSERT INTO routing_steps (routing_template_id, step_order, operation_name, work_center_id, planned_hours) VALUES (?,?,?,?,?)",
                (rt_id, i, op_name, wc_id, hours),
            )

    # --- Склад: матеріали, запчастини, комплектуючі ---
    items = [
        ("MAT-STL-40X", "Сталь конструкційна 40Х, пруток", "Матеріал", "кг", 850, 200, 62, "UAH", "Стелаж А1"),
        ("MAT-AL-6061", "Алюмінієвий сплав 6061, лист", "Матеріал", "кг", 120, 100, 145, "UAH", "Стелаж А2"),
        ("CMP-BALLSCREW-25", "Кулько-гвинтова передача Ø25", "Комплектуючі", "шт", 6, 4, 8200, "UAH", "Стелаж B1"),
        ("CMP-SPINDLE-BRG", "Підшипник шпинделя високоточний", "Комплектуючі", "шт", 3, 5, 15600, "UAH", "Стелаж B2"),
        ("CMP-SERVO-DRV", "Сервопривід осі X/Y/Z", "Комплектуючі", "шт", 9, 3, 24500, "UAH", "Стелаж B3"),
        ("PRT-COOLANT-PUMP", "Насос системи охолодження", "Запчастина", "шт", 4, 3, 3800, "UAH", "Стелаж C1"),
        ("PRT-BELT-V12", "Ремінь приводний V-12", "Запчастина", "шт", 14, 6, 420, "UAH", "Стелаж C2"),
        ("PRT-CONTROL-FUSE", "Запобіжник блоку керування ЧПУ", "Запчастина", "шт", 22, 15, 95, "UAH", "Стелаж C3"),
        ("CON-LUBRICANT-5L", "Мастило для напрямних, 5л", "Витратні матеріали", "каністра", 7, 5, 890, "UAH", "Стелаж D1"),
        ("CON-COOLANT-20L", "ЗОР (охолоджувальна рідина), 20л", "Витратні матеріали", "каністра", 5, 4, 1650, "UAH", "Стелаж D2"),
    ]
    cur.executemany(
        "INSERT INTO warehouse_items (sku, name, category, unit, qty_on_hand, min_qty, unit_cost, currency, location) VALUES (?,?,?,?,?,?,?,?,?)",
        items,
    )
    for i in range(1, len(items) + 1):
        cur.execute(
            "INSERT INTO warehouse_transactions (item_id, qty_delta, type, reference, user_id, created_at) VALUES (?,?,?,?,?,?)",
            (i, items[i - 1][4], "in", "Початкове завантаження залишків", admin_id, now_iso()),
        )

    # Прив'язка норм витрат матеріалів (BOM) до окремих операцій техпроцесу —
    # при створенні виробничого замовлення ці матеріали автоматично спишуться зі складу.
    bom_links = [
        ("Чорнове точіння", "MAT-STL-40X", 15),
        ("Чорнова фрезерна обробка", "MAT-AL-6061", 10),
        ("Складання та юстування", "CMP-BALLSCREW-25", 1),
        ("Попереднє шліфування", "MAT-STL-40X", 5),
        ("Чистове точіння за програмою ЧПУ", "CMP-SPINDLE-BRG", 1),
    ]
    for op_name, sku, qty in bom_links:
        cur.execute(
            """UPDATE routing_steps SET material_item_id=(SELECT id FROM warehouse_items WHERE sku=?),
               material_qty=? WHERE operation_name=?""",
            (sku, qty, op_name),
        )

    # --- Виробничі замовлення для угод, що вже на відповідних стадіях ---
    # (у реальній роботі це створюється автоматично при переході на "Передоплата
    # отримана" через create_production_order_from_deal; в демо-даних угоди вже
    # мають ці стадії від початку, тому створюємо замовлення тут вручну тим самим
    # шляхом, щоб термінали цехів і виробнича аналітика не були порожні з коробки)
    db.commit()  # маршрутні карти й угоди мають бути закомічені перед створенням замовлень
    import random as _random
    # log_activity/consume_warehouse_stock всередині звертаються до session.get("user_id"),
    # а session існує лише в межах активного HTTP-запиту - під час сідінгу бази при
    # старті застосунку такого контексту немає, тому створюємо його штучно.
    with app.test_request_context():
        for deal_row in db.execute(
            "SELECT * FROM deals WHERE stage IN ('prepay','production','shipping','install','won')"
        ).fetchall():
            order_id = create_production_order_from_deal(db, deal_row)
            if not order_id:
                continue
            ops = db.execute(
                "SELECT * FROM production_operations WHERE production_order_id=? ORDER BY step_order", (order_id,)
            ).fetchall()
            # Позначаємо частину операцій як вже виконані/у роботі відповідно до
            # того, наскільки далеко угода просунулась стадіями - щоб і термінал
            # цеху, і Gantt одразу показували реалістичну картину, а не порожній
            # список "все очікує".
            stage_progress = {"prepay": 0.15, "production": 0.5, "shipping": 0.8, "install": 0.9, "won": 1.0}
            done_ratio = stage_progress.get(deal_row["stage"], 0)
            done_count = int(len(ops) * done_ratio)
            for i, op in enumerate(ops):
                if i < done_count:
                    db.execute(
                        "UPDATE production_operations SET status='done', actual_start=?, actual_end=? WHERE id=?",
                        (op["planned_start"], op["planned_end"], op["id"]),
                    )
                elif i == done_count and done_count < len(ops):
                    db.execute("UPDATE production_operations SET status='in_progress', actual_start=? WHERE id=?",
                               (now_iso(), op["id"]))
            if done_count >= len(ops):
                db.execute("UPDATE production_orders SET status='done' WHERE id=?", (order_id,))
            elif done_count > 0:
                db.execute("UPDATE production_orders SET status='in_progress' WHERE id=?", (order_id,))
    db.commit()

    # --- Демо-записи змінного журналу по всіх дільницях ---
    # (без цього новий розділ "Змінний журнал" був би порожнім одразу після
    # встановлення - додаємо кілька реалістичних записів за останні дні,
    # по одному-два на кожну дільницю, щоб було що показати й перевірити)
    maister_id = db.execute("SELECT id FROM users WHERE username='maister'").fetchone()["id"]
    tokar_id = db.execute("SELECT id FROM users WHERE username='tokar'").fetchone()["id"]
    shift_log_seed = [
        (d(0), "day", 1, maister_id, "Розкрій заготовок зі сталі 40Х", 24, 1, "задир на торці", 8, 0.5),
        (d(0), "day", 2, tokar_id, "Вал приводний Ф25 — чистове точіння", 18, 0, None, 8, 1.0),
        (d(-1), "day", 2, tokar_id, "Вал приводний Ф25 — чорнове точіння", 20, 2, "биття різця", 7.5, 1.5),
        (d(0), "day", 3, maister_id, "Корпус редуктора — фрезерування", 9, 1, "допуск по площині", 8, 2.0),
        (d(-1), "day", 4, tokar_id, "Втулка посадкова — шліфування", 30, 0, None, 8, 0.5),
        (d(0), "day", 5, maister_id, "Складання вузла супорта", 6, 0, None, 7, 0),
        (d(-2), "day", 6, maister_id, "Контроль партії валів (ВТК)", 20, 0, None, 4, 0),
        (d(0), "day", 7, maister_id, "Пакування партії для відвантаження", 15, 0, None, 3, 0),
    ]
    for work_date, shift, wc_id, worker_id, part_desc, made, scrap, scrap_reason, shift_hours, setup_hours in shift_log_seed:
        db.execute(
            """INSERT INTO shift_logs (work_date, shift, work_center_id, worker_user_id, part_description,
               quantity_made, quantity_scrap, scrap_reason, shift_hours, setup_hours, created_by_user_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (work_date, shift, wc_id, worker_id, part_desc, made, scrap, scrap_reason, shift_hours, setup_hours, worker_id, now_iso()),
        )
    db.commit()

    # --- Реалістична 14-денна історія телеметрії для аналітики верстатів ---
    # (без цього сторінки "Робочі центри" та "Термінал цеху" були б порожні
    # одразу після встановлення - незручно і для демонстрації, і для
    # перевірки, що функціонал взагалі працює)
    now_dt = datetime.datetime.now()
    for wc in db.execute("SELECT * FROM work_centers").fetchall():
        motion_hours = _random.uniform(600, 3000)
        power_on_hours = motion_hours * 1.6
        part_count = _random.randint(1200, 8000)
        for day_offset in range(13, -1, -1):
            day_dt = now_dt - datetime.timedelta(days=day_offset)
            is_workday = day_dt.weekday() < 5
            daily_hours = _random.uniform(3, 7.5) if is_workday else _random.uniform(0, 1.2)
            motion_hours += daily_hours
            power_on_hours += daily_hours * 1.3
            status = _random.choices(["running", "idle", "alarm"], weights=[70, 27, 3])[0]
            db.execute(
                """INSERT INTO machine_telemetry_log (work_center_id, status, spindle_load_pct,
                   motion_hours, power_on_hours, part_count, recorded_at) VALUES (?,?,?,?,?,?,?)""",
                (wc["id"], status, _random.randint(20, 90) if status == "running" else 0,
                 round(motion_hours, 1), round(power_on_hours, 1), part_count,
                 day_dt.strftime("%Y-%m-%d %H:%M:%S")),
            )
        last_status = "running" if wc["id"] % 3 != 0 else "idle"
        db.execute(
            """INSERT INTO machine_telemetry (work_center_id, status, spindle_load_pct, cycle_count,
               power_on_hours, motion_hours, part_count, program_name, mode, source, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (wc["id"], last_status, _random.randint(30, 85) if last_status == "running" else 0,
             _random.randint(500, 5000), round(power_on_hours, 1), round(motion_hours, 1), part_count,
             f"O{_random.randint(1000,9999)}", "MEM", "demo_seed", now_iso()),
        )

    # --- Партії матеріалу (плавки, сертифікати) для наочності на демо ---
    material_lots_demo = [
        (1, "HT-2026-DEMO-01", "CERT-QA-8891", "МеталТрейд ЛТД", 300, "2026-07-10", "Партія для замовлення Металпром"),
        (2, "HT-2026-DEMO-02", "CERT-QA-8756", "АлюмСервіс", 80, "2026-06-22", None),
    ]
    for item_id, heat, cert, supplier, qty, rdate, notes in material_lots_demo:
        cur.execute(
            """INSERT INTO material_lots (item_id, heat_number, certificate_number, supplier, qty,
               received_date, notes, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (item_id, heat, cert, supplier, qty, rdate, notes, now_iso()),
        )

    # --- Ділові відходи/обрізки для наочності на демо ---
    scrap_demo = [
        ("Сталь конструкційна 40Х", 5.0, 450.0, 300.0, 5.3, "Стелаж E1", "available"),
        ("Алюмінієвий сплав 6061", 3.0, 200.0, 150.0, 0.24, "Стелаж E2", "available"),
        ("Сталь конструкційна 40Х", 8.0, 120.0, 80.0, 0.6, "Стелаж E1", "used"),
    ]
    for material, thickness, length, width, weight, location, status in scrap_demo:
        used_at = now_iso() if status == "used" else None
        cur.execute(
            """INSERT INTO scrap_offcuts (material_name, thickness_mm, length_mm, width_mm, weight_kg,
               location, status, created_at, used_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (material, thickness, length, width, weight, location, status, now_iso(), used_at),
        )

    # --- Приклад кастомного поля клієнта для наочності на демо ---
    cur.execute(
        """INSERT INTO custom_field_defs (entity_type, field_key, label, field_type, options, sort_order)
           VALUES ('client', 'тип_клієнта', 'Тип клієнта', 'select', 'ВІП, Звичайний, Разовий', 1)"""
    )
    cf_id = cur.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    cur.execute(
        "INSERT INTO custom_field_values (field_id, entity_id, value) VALUES (?,?,?)",
        (cf_id, 1, "ВІП"),
    )

    # --- Приклади розрахунків вартості в історії калькулятора (наочність на
    # демо - без цього "Історія розрахунків" після встановлення порожня) ---
    calc_demo = [
        # source, опис, к-сть, вага, ціна_мат, токарна_год, токарна_ставка,
        # фрезерна_год, фрезерна_ставка, доп_год, доп_ставка, наладка, націнка,
        # техпроцес, собівартість/од, ціна/од, разом
        # (суми прораховані точно за тією ж формулою, що й сам калькулятор:
        # собівартість = матеріал + токарна + фрезерна + доп.обробка + наладка/к-сть;
        # ціна = собівартість × (1 + націнка/100); разом = ціна × к-сть)
        ("manual", "Вал ступінчастий Ø40х300, сталь 40Х", 20, 2.3, 62, 1.2, 800, 0.3, 900, 0, 0, 600, 25,
         "", 1402.60, 1753.25, 35065.0),
        ("ai", "Фланець приводний Ø120, сталь 45, партія під держзамовлення", 50, 1.8, 60, 0.8, 800, 1.1, 900, 0.2, 400,
         1200, 22,
         "Технологічний процес виготовлення приводного фланця Ø120 мм зі сталі 45:\n"
         "1. Розкрій заготовки з прутка Ø130 мм на токарно-відрізному верстаті, довжина заготовки 35 мм з припуском.\n"
         "2. Встановлення заготовки в трикулачний патрон токарного верстата з ЧПУ, вивірка биття не більше 0.05 мм.\n"
         "3. Чорнове точіння зовнішнього діаметра та торців з припуском 0.5 мм під чистову обробку.\n"
         "4. Свердління центрального отвору Ø30 мм наскрізь під подальше розточування.\n"
         "5. Розточування центрального отвору до Ø32H7 з дотриманням допуску та шорсткості Ra 1.6.\n"
         "6. Нарізання 6 кріпильних отворів М10 по колу Ø90 мм на фрезерному обробному центрі з ЧПУ.\n"
         "7. Чистове точіння зовнішнього діаметра Ø120h7 та торцевих поверхонь за програмою ЧПУ.\n"
         "8. Зняття фасок 1.5х45° по всіх гострих кромках для безпечного поводження та складання.\n"
         "9. Контроль геометричних розмірів на координатно-вимірювальній машині (ВТК), складання протоколу.\n"
         "10. Маркування партії відповідно до креслення, консервація антикорозійним мастилом.\n"
         "11. Упаковка в картонні короби з прокладками, підготовка супровідної документації до відвантаження.",
         1842.00, 2247.24, 112362.0),
        ("manual", "Втулка бронзова Ø25х40 для ремонтної партії", 8, 0.3, 450, 0.4, 750, 0, 900, 0, 0, 150, 30,
         "", 453.75, 589.875, 4719.0),
    ]
    for source, desc, qty, weight, mat_price, t_h, t_rate, m_h, m_rate, mc_h, mc_rate, setup, margin, ops_desc, cost_u, price_u, total in calc_demo:
        cur.execute(
            """INSERT INTO cost_calculations (user_id, source, part_description, quantity, weight_kg,
               material_price_per_kg, turning_hours, turning_rate_per_hour, milling_hours, milling_rate_per_hour,
               machine_hours, machine_rate_per_hour, setup_cost_total, margin_pct, cost_per_unit, price_per_unit,
               total_price, operations_description, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (oleh_id if source == "manual" else admin_id, source, desc, qty, weight, mat_price, t_h, t_rate,
             m_h, m_rate, mc_h, mc_rate, setup, margin, cost_u, price_u, total, ops_desc, now_iso()),
        )

    # --- Налаштування: API-ключ телеметрії, курси валют ---
    import secrets as _secrets
    cur.execute("INSERT INTO settings (key, value) VALUES (?,?)", ("api_key", _secrets.token_hex(16)))
    cur.execute("INSERT INTO settings (key, value) VALUES (?,?)", ("rate_usd", "41.5"))
    cur.execute("INSERT INTO settings (key, value) VALUES (?,?)", ("rate_eur", "45.0"))
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", ("rate_pln", "10.5"))
    cur.execute("INSERT INTO settings (key, value) VALUES (?,?)", ("webhook_url", ""))

    db.commit()


# ---------------------------------------------------------------------------
# Допоміжні функції
# ---------------------------------------------------------------------------

def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    user = get_current_user()
    reminders = get_reminders(get_db(), user) if user else []
    return dict(
        STAGES=STAGES,
        STAGE_LABEL=STAGE_LABEL,
        STAGE_COLOR=STAGE_COLOR,
        LOST_STAGE=LOST_STAGE,
        PRIORITIES=dict(PRIORITIES),
        current_user=user,
        today=today_str(),
        is_admin=is_admin(),
        PAYMENT_KINDS=PAYMENT_KINDS,
        PAYMENT_STATUSES=PAYMENT_STATUSES,
        reminders=reminders,
        foreman_wc_id=foreman_work_center_id(user),
    )


def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def get_managers():
    db = get_db()
    return db.execute("SELECT * FROM users WHERE active=1 AND role IN ('admin','sales') ORDER BY full_name").fetchall()


def get_production_workers():
    db = get_db()
    return db.execute("SELECT * FROM users WHERE active=1 AND role='production' ORDER BY full_name").fetchall()


def can_edit_shift_log(user, log_row):
    """Хто має право редагувати/видаляти ЧУЖИЙ запис змінного журналу
    (свій - завжди можна):
    - адмін - завжди;
    - виробничник БЕЗ закріпленої дільниці (work_center_id не задано) -
      це загальний майстер цеху за домовленістю, як і решта застосунку
      (foreman_work_center_id повертає None саме для такого користувача) -
      бачить і редагує все;
    - виробничник, закріплений за СВОЄЮ дільницею (майстер дільниці) -
      редагує записи, зроблені НА ЙОГО дільниці (незалежно від того, хто
      саме з робітників їх вносив), плюс завжди свої власні записи;
    - звичайний робітник без закріпленої дільниці, якщо такий колись
      з'явиться - за цією ж логікою отримає повний доступ нарівні з
      майстром цеху, тож для рядових операторів варто закріплювати
      дільницю (work_center_id) у картці користувача, щоб обмежити їх
      лише своїми записами й записами своєї дільниці."""
    if not user:
        return False
    if user["role"] == "admin":
        return True
    if user["role"] != "production":
        return False
    if log_row["worker_user_id"] == user["id"] or log_row["created_by_user_id"] == user["id"]:
        return True
    fw = foreman_work_center_id(user)
    if not fw:
        return True
    return log_row["work_center_id"] == fw


def fmt_money(amount, currency="USD"):
    if amount is None or not isinstance(amount, (int, float)):
        amount = 0
    symbols = {"USD": "$", "EUR": "€", "UAH": "₴", "PLN": "zł "}
    return "{}{:,.0f}".format(symbols.get(currency, ""), amount).replace(",", " ")


app.jinja_env.filters["money"] = fmt_money


def split_operations_text(text):
    """Розбиває текст технологічного опису (від ШІ чи збережений в історії)
    на окремі пункти, щоб його можна було показати як список етапів, а не
    суцільною стіною тексту. Повертає список словників:
        {"kind": "step", "num": "1", "title": "...", "body": "..."}  - нумерований етап
        {"kind": "note", "body": "..."}                              - примітка/припущення чи вільний рядок

    Працює і з новим форматом (кожен етап з нового рядка: «1. Заготовка — ...»),
    і зі старими записами, де все було одним абзацом: вбудовану нумерацію
    «1) ... 2) ...» або «1. ... 2. ...» розрізає по номерах, а якщо нумерації
    немає зовсім - по реченнях."""
    text = (text or "").strip()
    if not text:
        return []
    # Єдиний рядок з вбудованою нумерацією -> розкладаємо по рядках.
    if "\n" not in text:
        text = _re.sub(r"\s+(?=\d{1,2}[.)]\s+[^\d\s])", "\n", text)
    # Досі один рядок - ріжемо за реченнями (старі записи без структури).
    if "\n" not in text:
        text = _re.sub(r"(?<=[.!?])\s+(?=[А-ЯІЇЄҐA-Z])", "\n", text)

    items = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _re.match(r"^(\d{1,2})[.)]\s*(.+)$", line)
        if m:
            num, rest = m.group(1), m.group(2).strip()
            # Назва етапу - те, що до першого « — », « - » чи «:» (якщо воно коротке).
            title, body = rest, ""
            sm = _re.match(r"^(.{3,60}?)\s*(?:—|–|-|:)\s+(.+)$", rest)
            if sm:
                title, body = sm.group(1).strip().rstrip(":"), sm.group(2).strip()
            items.append({"kind": "step", "num": num, "title": title, "body": body})
        else:
            items.append({"kind": "note", "body": line})
    return items


def render_operations_html(text):
    """Jinja-фільтр: безпечно (з екрануванням HTML) малює технологічний опис
    як акуратний список етапів - номер, жирна назва, нижче пояснення."""
    from markupsafe import Markup, escape
    items = split_operations_text(text)
    if not items:
        return Markup("")
    parts = ['<div class="ops-list">']
    for it in items:
        if it["kind"] == "step":
            body_html = f'<div class="ops-body">{escape(it["body"])}</div>' if it["body"] else ""
            parts.append(
                '<div class="ops-step">'
                f'<div class="ops-num">{escape(it["num"])}</div>'
                f'<div class="ops-text"><div class="ops-title">{escape(it["title"])}</div>{body_html}</div>'
                '</div>'
            )
        else:
            parts.append(f'<div class="ops-note">{escape(it["body"])}</div>')
    parts.append("</div>")
    return Markup("".join(parts))


app.jinja_env.filters["ops_html"] = render_operations_html


def is_admin():
    u = get_current_user()
    return bool(u and u["role"] == "admin")


def manager_scope_sql(column="manager_id"):
    """Повертає (умова SQL, параметри) для обмеження видимості даних менеджера.
    Адмін бачить усе; менеджер бачить лише те, що призначено на нього."""
    if is_admin():
        return "", []
    uid = session.get("user_id")
    return f" AND {column}=?", [uid]


def foreman_work_center_id(user):
    """Якщо користувач - майстер, прив'язаний до конкретної дільниці
    (роль 'production' + заповнене work_center_id), повертає ID цієї
    дільниці. Інакше None (загальний виробничник/адмін бачить усе)."""
    if user and user["role"] == "production" and user["work_center_id"]:
        return user["work_center_id"]
    return None


def get_reminders(db, user):
    """Збирає нагадування, релевантні ролі поточного користувача:
    прострочені задачі, критично низькі залишки складу, аварійні сигнали
    обладнання, прострочені платежі. Показуємо лише те, що стосується
    ролі — виробничнику не потрібні нагадування про рахунки бухгалтерії."""
    if not user:
        return []
    role = user["role"]
    reminders = []
    today = today_str()

    if role in ("admin", "sales"):
        scope, params = manager_scope_sql("manager_id")
        overdue_tasks = db.execute(
            f"SELECT COUNT(*) c FROM tasks WHERE done=0 AND due_date IS NOT NULL AND due_date < ?{scope}",
            [today] + params,
        ).fetchone()["c"]
        if overdue_tasks:
            reminders.append({"text": f"Прострочених задач: {overdue_tasks}", "url": "/tasks", "level": "high"})

    if role in ("admin", "warehouse", "production"):
        low_stock = db.execute("SELECT COUNT(*) c FROM warehouse_items WHERE qty_on_hand <= min_qty").fetchone()["c"]
        if low_stock:
            reminders.append({"text": f"Критично низький залишок: {low_stock} позицій", "url": "/warehouse", "level": "high"})

    if role in ("admin", "production"):
        week_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        alarms = db.execute(
            "SELECT COUNT(DISTINCT work_center_id) c FROM machine_telemetry_log WHERE status='alarm' AND recorded_at>=?",
            (week_ago,),
        ).fetchone()["c"]
        if alarms:
            reminders.append({"text": f"Аварійні сигнали на {alarms} робочих центрах за тиждень", "url": "/work-centers", "level": "high"})

    if role in ("admin", "production", "sales"):
        overdue_orders = db.execute(
            "SELECT COUNT(*) c FROM production_orders WHERE status NOT IN ('done','cancelled') "
            "AND due_date IS NOT NULL AND due_date < ?",
            (today,),
        ).fetchone()["c"]
        if overdue_orders:
            reminders.append({"text": f"Прострочених виробничих замовлень: {overdue_orders}", "url": "/production", "level": "high"})

    if role in ("admin", "accountant"):
        overdue_payments = db.execute(
            "SELECT COUNT(*) c FROM payments WHERE status='pending' AND due_date IS NOT NULL AND due_date < ?",
            (today,),
        ).fetchone()["c"]
        if overdue_payments:
            reminders.append({"text": f"Прострочених платежів: {overdue_payments}", "url": "/finance", "level": "high"})

    if role in ("admin", "service"):
        open_tickets = db.execute(
            "SELECT COUNT(*) c FROM service_tickets WHERE status NOT IN ('done','cancelled') AND priority='high'"
        ).fetchone()["c"]
        if open_tickets:
            reminders.append({"text": f"Термінових рекламацій у роботі: {open_tickets}", "url": "/service", "level": "medium"})

    return reminders


def log_activity(db, deal_id, client_id, atype, text):
    db.execute(
        "INSERT INTO activities (deal_id, client_id, type, text, manager_id, created_at) VALUES (?,?,?,?,?,?)",
        (deal_id, client_id, atype, text, session.get("user_id"), now_iso()),
    )


def create_task(db, deal_id, client_id, title, ttype="other", days_offset=1, description=None):
    due = (datetime.date.today() + datetime.timedelta(days=days_offset)).strftime("%Y-%m-%d")
    db.execute(
        """INSERT INTO tasks (deal_id, client_id, title, description, type, due_date, done,
           manager_id, created_at) VALUES (?,?,?,?,?,?,0,?,?)""",
        (deal_id, client_id, title, description, ttype, due, session.get("user_id"), now_iso()),
    )


def cascade_delete_deal(db, deal_id):
    db.execute("DELETE FROM payments WHERE deal_id=?", (deal_id,))
    db.execute("DELETE FROM activities WHERE deal_id=?", (deal_id,))
    db.execute("DELETE FROM tasks WHERE deal_id=?", (deal_id,))
    db.execute("DELETE FROM deals WHERE id=?", (deal_id,))


def get_machine_runtime_stats(db, work_center_id, capacity_hours_per_day=8):
    """Рахує напрацювання робочого центру на основі накопичувального лічильника
    motion_hours (аналог Q301 у Haas): різниця між останнім і найранішим показником
    за період = скільки годин верстат реально був у русі за цей період.
    Це надійніше за підрахунок по знімках статусу, бо не залежить від частоти опитування.
    Плюс: денна розбивка за 14 днів для графіка, доступність (availability %),
    список останніх аварійних сигналів."""
    latest = db.execute(
        "SELECT * FROM machine_telemetry_log WHERE work_center_id=? ORDER BY recorded_at DESC LIMIT 1",
        (work_center_id,),
    ).fetchone()
    if not latest or latest["motion_hours"] is None:
        return None

    def motion_at_or_before(dt_str):
        row = db.execute(
            """SELECT motion_hours FROM machine_telemetry_log
               WHERE work_center_id=? AND recorded_at<=? AND motion_hours IS NOT NULL
               ORDER BY recorded_at DESC LIMIT 1""",
            (work_center_id, dt_str),
        ).fetchone()
        return row["motion_hours"] if row else None

    def delta_since(since_dt_str):
        earliest = db.execute(
            """SELECT motion_hours FROM machine_telemetry_log
               WHERE work_center_id=? AND recorded_at>=? AND motion_hours IS NOT NULL
               ORDER BY recorded_at ASC LIMIT 1""",
            (work_center_id, since_dt_str),
        ).fetchone()
        if not earliest:
            return 0.0
        return max(round(latest["motion_hours"] - earliest["motion_hours"], 1), 0.0)

    today_start = datetime.datetime.combine(datetime.date.today(), datetime.time.min).strftime("%Y-%m-%d %H:%M:%S")
    week_start = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    alarm_count_week = db.execute(
        "SELECT COUNT(*) c FROM machine_telemetry_log WHERE work_center_id=? AND status='alarm' AND recorded_at>=?",
        (work_center_id, week_start),
    ).fetchone()["c"]

    # Денна розбивка за 14 днів (для графіка) — приріст motion_hours щодня
    daily = []
    for i in range(13, -1, -1):
        day = datetime.date.today() - datetime.timedelta(days=i)
        day_start = datetime.datetime.combine(day, datetime.time.min).strftime("%Y-%m-%d %H:%M:%S")
        day_end = datetime.datetime.combine(day, datetime.time.max).strftime("%Y-%m-%d %H:%M:%S")
        start_val = motion_at_or_before(day_start)
        end_val = motion_at_or_before(day_end)
        if start_val is None and end_val is None:
            hours = 0.0
        elif start_val is None:
            hours = 0.0  # немає даних до цього дня — не вигадуємо приріст
        else:
            hours = max(round((end_val or start_val) - start_val, 1), 0.0)
        daily.append({"date": day.strftime("%d.%m"), "hours": hours})

    # Доступність (availability) за 14 днів: фактичне напрацювання / теоретична ємність
    total_14d = sum(d["hours"] for d in daily)
    capacity_14d = capacity_hours_per_day * 14
    availability_pct = round(min(total_14d / capacity_14d * 100, 100), 1) if capacity_14d else 0

    recent_alarms = db.execute(
        """SELECT recorded_at FROM machine_telemetry_log
           WHERE work_center_id=? AND status='alarm' ORDER BY recorded_at DESC LIMIT 10""",
        (work_center_id,),
    ).fetchall()

    return {
        "total_motion_hours": round(latest["motion_hours"], 1),
        "today_hours": delta_since(today_start),
        "week_hours": delta_since(week_start),
        "alarm_count_week": alarm_count_week,
        "daily": daily,
        "availability_pct": availability_pct,
        "recent_alarms": [r["recorded_at"] for r in recent_alarms],
    }


def cascade_delete_client(db, client_id):
    deal_ids = [r["id"] for r in db.execute("SELECT id FROM deals WHERE client_id=?", (client_id,)).fetchall()]
    for did in deal_ids:
        cascade_delete_deal(db, did)
    db.execute("DELETE FROM activities WHERE client_id=?", (client_id,))
    db.execute("DELETE FROM tasks WHERE client_id=?", (client_id,))
    db.execute("DELETE FROM service_tickets WHERE client_id=?", (client_id,))
    db.execute("DELETE FROM client_equipment WHERE client_id=?", (client_id,))
    db.execute("DELETE FROM contacts WHERE client_id=?", (client_id,))
    db.execute("DELETE FROM clients WHERE id=?", (client_id,))


# ---------------------------------------------------------------------------
# Налаштування, права доступу, вебхуки
# ---------------------------------------------------------------------------

def get_setting(db, key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_all_settings(db):
    return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}


def set_setting(db, key, value):
    db.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))


def send_webhook_notification(text):
    """Надсилає сповіщення на налаштований вебхук (Slack-сумісний формат).
    Помилки мережі не повинні ламати основний функціонал CRM - так само,
    як і помилки доступу до БД (наприклад, коли ця функція викликається
    зі штучного test_request_context під час сідингу демо-даних, де немає
    власного з'єднання з БД і get_db() міг би конфліктувати з відкритою
    транзакцією сідингу)."""
    try:
        db = get_db()
        url = get_setting(db, "webhook_url", "")
    except Exception as e:
        return False, str(e)
    if not url:
        return False, "URL вебхука не налаштовано"
    try:
        import urllib.request
        import json as _json
        payload = _json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=4)
        return True, "OK"
    except Exception as e:
        return False, str(e)


def send_telegram_notification(text):
    """Надсилає сповіщення в Telegram через офіційний Bot API.
    Потрібні два налаштування: токен бота (видає @BotFather) і chat_id
    (кому/куди слати) — саме chat_id, а не логін, бо Telegram з міркувань
    приватності не дозволяє боту писати користувачу лише за юзернеймом,
    поки той сам не написав боту хоч раз. Для каналів достатньо @назви
    каналу, якщо бот доданий туди адміністратором."""
    try:
        db = get_db()
        token = get_setting(db, "telegram_bot_token", "")
        chat_id = get_setting(db, "telegram_chat_id", "")
    except Exception as e:
        return False, str(e)
    if not token or not chat_id:
        return False, "Токен бота або chat_id не налаштовано"
    try:
        import urllib.request
        import urllib.parse
        import json as _json
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = _json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=6)
        result = _json.loads(resp.read().decode("utf-8"))
        if result.get("ok"):
            return True, "OK"
        return False, result.get("description", "Telegram повернув помилку")
    except Exception as e:
        return False, str(e)


def notify_event(text):
    """Сповіщення про події (нова угода, зміна етапу): вмикається в
    «Налаштуваннях». Збої каналів не ламають основну дію."""
    try:
        if get_setting(get_db(), "notify_events", "1") == "1":
            notify_all(text)
    except Exception:
        pass


def notify_all(text):
    """Надсилає сповіщення в усі налаштовані канали одразу (вебхук + Telegram).
    Використовується для критичних подій: аварії обладнання, низький залишок."""
    send_webhook_notification(text)
    send_telegram_notification(text)


# ---------------------------------------------------------------------------
# Розрахунок вартості через ШІ (опис деталі текстом -> параметри калькулятора)
# ---------------------------------------------------------------------------

AI_CALC_SYSTEM_PROMPT = """Ти - досвідчений технолог токарно-фрезерного механообробного цеху (токарна обробка з ЧПУ, фрезерування, свердління, шліфування, універсальні верстати для дрібних серій та доробки). У цеху НЕМАЄ лазерного різання, гину листового металу чи зварювального виробництва - заготовки це прутки, болванки, поковки чи вже частково оброблені деталі, які точаться й фрезеруються на верстатах з ЧПУ. Тобі дають опис деталі й операцій природною мовою (українською або російською), і/або зображення (фото деталі, скан або фото креслення). Твоя задача - і розрахувати параметри собівартості, і описати весь технологічний процес виготовлення максимально детально, так, ніби пишеш операційну карту для оператора верстата.

Парк обладнання цеху (обирай і називай КОНКРЕТНИЙ верстат для кожної операції, а не абстрактне "фрезерний верстат" чи "токарний верстат"):
- Haas UMC-750 - 5-осьовий універсальний обробний центр (mill-turn): складні деталі за один установ, деталі з піднутреннями, конічними/просторовими поверхнями, суміщення фрезерування й свердління під кутом без переустановки.
- Haas VF-2 - вертикальний обробний центр (3 осі), невеликий/середній робочий стіл: фрезерування, свердління, нарізання різьби мітчиком/фрезою на компактних і середніх деталях, дрібні та середні партії.
- Haas VF-4 - вертикальний обробний центр (3 осі), більший робочий стіл і хід: фрезерування габаритних деталей чи кількох деталей в оснастці за один цикл, великі партії фрезерних операцій.
- Токарний верстат з ЧПУ Haas (токарна група) - усі операції точіння: чорнове й чистове точіння прутка/болванки, розточування, нарізання різьби різцем, відрізання.

Правило вибору верстата: тіла обертання (вали, втулки, фланці, ступінчасті деталі) - токарний ЧПУ Haas (за потреби чорнове + чистове точіння окремо); прості призматичні деталі з пазами/отворами під один-два установи - VF-2 (менші) або VF-4 (більші/партійні); складна деталь з нахиленими/просторовими поверхнями, що вимагає одного установу - UMC-750. Комбінована деталь (спершу точіння, потім фрезерування паза чи площини) - явно вказуй обидва верстати в потрібній послідовності (напр. "токарний ЧПУ Haas → Haas VF-2").

Якщо додано зображення креслення чи деталі - уважно розглянь його: розміри та форму (тіла обертання - для токарної обробки; призматичні/пази/отвори - для фрезерної), матеріал і його стан (пруток, поковка, лита заготовка), допуски й шорсткість поверхонь, різьблення, отвори під свердління, термообробку чи покриття, якщо позначені. Використай усе, що видно на зображенні, для розрахунку - це важливіше за здогадки. Якщо текстовий опис і зображення суперечать одне одному, довірся зображенню й зауваж розбіжність у operations_description.

Якщо якийсь ДРУГОРЯДНИЙ параметр не можна визначити ні з тексту, ні із зображення (ціни, ставки за годину, вартість наладки, націнка) - постав розумне типове значення для механообробки в Україні (орієнтовно: сталь конструкційна ~55-65 грн/кг, алюміній ~150-200 грн/кг, нержавіюча сталь ~120-180 грн/кг; токарна обробка з ЧПУ ~600-1000 грн/машино-годину; фрезерна обробка з ЧПУ ~700-1200 грн/машино-годину; наладка верстата на партію ~300-1500 грн залежно від складності; націнка 15-25%) і обов'язково згадай це припущення у полі operations_description, щоб людина знала, що це орієнтовна оцінка, а не дані з документів. Про ДРУГОРЯДНІ параметри НІКОЛИ не питай - завжди оцінюй сам.

Але якщо опису мінімально НЕ вистачає для самого розрахунку - тобто ти не можеш визначити ні матеріал (з чого деталь - сталь, алюміній, нержавійка тощо), ні розмір/вагу заготовки (немає ні розмірів у тексті, ні зображення, з якого їх видно), ні які операції взагалі потрібні (просто "деталь" без жодного опису форми чи процесу) - НЕ вигадуй ці критичні дані з нуля і НЕ вважай типове значення прийнятним заміною. Замість цього виклич інструмент ask_clarifying_question і постав ОДНЕ конкретне, вузьке питання українською, яке справді закриє прогалину (напр. "З якого металу деталь і які її приблизні габарити (діаметр/довжина або довжина×ширина×висота)?"). Питай лише тоді, коли без відповіді оцінка була б беззмістовною - якщо є хоч якісь орієнтири (навіть неповні), користуйся ними та розумними припущеннями замість питання.

У полі operations_description дай РОЗГОРНУТИЙ, детальний опис технологічного процесу - це має читатись як справжня технологічна карта, а не коротка анотація. ФОРМАТ ВАЖЛИВИЙ: кожен етап пиши з НОВОГО РЯДКА (символ переносу рядка між етапами), у вигляді «N. Назва етапу — пояснення, верстат, орієнтовний час», без загального вступу і без суцільного абзацу. Приклад двох рядків:
1. Заготовка — пруток Ø60 мм, сталь 45, довжина 120 мм; обрано пруток, бо деталь — тіло обертання. Час підготовки ~10 хв.
2. Чорнове точіння — зовнішні поверхні з припуском 0,5 мм на токарному верстаті з ЧПУ; ~25 хв.
Якщо є припущення чи застереження (типові ціни, розбіжність між текстом і кресленням) - винеси їх в окремий останній рядок, що починається зі слова «Припущення:». Структуруй по етапах і для КОЖНОЇ операції механообробки явно назви верстат з парку вище:
1) заготовка - з чого і якого розміру (пруток Ø.. мм, поковка, лита заготовка) і чому саме така;
2) базування/закріплення заготовки (патрон, цанга, лещата, оснастка) на конкретному верстаті;
3) чорнове точіння (якщо є) - які поверхні, припуск, на якому верстаті;
4) чистове точіння (якщо є) - фінальні розміри, допуски, шорсткість;
5) фрезерні операції (пази, площини, контури) - послідовність переходів, на якому верстаті (VF-2/VF-4/UMC-750) і чому саме цей;
6) свердлильні операції та нарізання різьби - діаметри, глибина, інструмент;
7) термообробка чи покриття, якщо застосовне;
8) контроль ВТК - які розміри перевіряються і чим (штангенциркуль, мікрометр, КВД);
9) пакування і підготовка до відвантаження.
Для кожного етапу дай орієнтовний час у хвилинах чи годинах. Це має бути МІНІМУМ 10-15 речень розгорнутого тексту технологічною мовою - короткий опис у 3-4 речення вважається НЕДОСТАТНІМ і неприйнятним.

Параметри розрахунку завжди повертай через виклик інструмента cost_estimate (не текстом) - weight_kg (вага заготовки на ОДНУ деталь, кг), material_price_per_kg (грн/кг), turning_hours/turning_rate_per_hour (токарна обробка на ОДНУ деталь, 0 якщо немає), milling_hours/milling_rate_per_hour (фрезерування/свердління/різьба на ОДНУ деталь, 0 якщо немає), machine_hours/machine_rate_per_hour (додаткова обробка - шліфування/термообробка/покриття, 0 якщо немає), setup_cost_total (наладка на ВСЮ партію, одноразово), quantity (ціле число, за замовчуванням 1), margin_pct (рекомендована націнка, %), operations_description (РОЗГОРНУТИЙ технологічний маршрут за структурою етапів вище, МІНІМУМ 10-15 речень, з назвою конкретного верстата на кожній операції).

Якщо після першого розрахунку користувач у чаті просить щось виправити чи уточнити (напр. "вага не 2, а 3.2 кг", "забув свердління 4 отворів", "занизька націнка") - онови ЛИШЕ ті поля, яких стосується правка (і, якщо потрібно, відповідну частину operations_description), а решту полів лиши як у попередньому розрахунку, і знову виклич cost_estimate з ПОВНИМ оновленим набором усіх полів (не лише змінених)."""


class AICalcError(Exception):
    pass


def _extract_json_object(text):
    """Вирізає перший повний JSON-об'єкт {...} з довільного тексту - для
    випадків, коли модель відповідає не ЛИШЕ JSON-ом, а додає пояснювальний
    текст до чи після нього (попри пряму заборону в системному промпті).
    Рахує фігурні дужки вручну, а не regex-жадібністю, і коректно пропускає
    дужки всередині рядкових значень (у т.ч. екранованих лапок), щоб не
    зупинитись на "}" усередині, наприклад, operations_description. Повертає
    підрядок з першою збалансованою парою {...} або None, якщо такої немає."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def render_pdf_to_png_bytes(raw_bytes, max_pages=3, dpi=150):
    """Рендерить сторінки PDF-файлу (скан чи PDF-експорт креслення) у список
    PNG-картинок - ШІ Vision API вміє дивитись лише на растр, а не на PDF
    напряму. Через PyMuPDF (модуль fitz) - навмисно НЕ через pdf2image,
    бо той залежить від зовнішнього системного бінарника poppler
    (pdftoppm), якого нема на чистій Windows-машині користувача і який
    PyInstaller не запакує автоматично в один .exe; PyMuPDF - самодостатнє
    pip-колесо з вбудованим MuPDF, без зовнішніх системних залежностей.
    Повертає список байтів PNG (по одному на сторінку, макс. max_pages
    перших сторінок - креслення зазвичай на першій сторінці, але скан
    техпаспорта чи КД може мати кілька)."""
    import fitz  # PyMuPDF
    try:
        doc = fitz.open(stream=raw_bytes, filetype="pdf")
    except Exception as e:
        raise ValueError(f"файл не схожий на коректний PDF: {e}")
    try:
        if doc.page_count == 0:
            raise ValueError("PDF не містить жодної сторінки")
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pages = []
        for i in range(min(doc.page_count, max_pages)):
            pix = doc.load_page(i).get_pixmap(matrix=matrix)
            pages.append(pix.tobytes("png"))
        return pages
    finally:
        doc.close()


AI_IMAGE_MAX_BYTES = 5 * 1024 * 1024  # ліміт Anthropic API на одне зображення в base64


def detect_image_media_type(raw_bytes):
    """Визначає реальний тип зображення за магічними байтами файлу (не за
    заявленим mimetype, якому не можна довіряти). Навмисно не використовує
    стандартний модуль imghdr - його прибирають з Python 3.13, а нам
    потрібна лише невелика підмножина форматів (PNG/JPEG/GIF/WEBP),
    достатніх для Anthropic API. Повертає media_type-рядок або None,
    якщо це не один з підтримуваних форматів зображень."""
    if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw_bytes.startswith(b"RIFF") and raw_bytes[8:12] == b"WEBP":
        return "image/webp"
    return None


_AI_CALC_PENDING = {}
_AI_CALC_PENDING_TTL_SECONDS = 30 * 60  # 30 хвилин - достатньо, щоб встигнути відповісти на питання


def _ai_calc_pending_store(messages, description, known_fields=None):
    """Зберігає стан розмови з ШІ (messages, включно з попереднім
    tool_use ask_clarifying_question) сервер-side під одноразовим
    токеном, поки користувач відповідає на уточнююче питання - HTTP
    без стану, тож між двома POST-запитами розмову треба десь тримати,
    а передавати назад у прихованому полі форми (з base64-зображеннями
    всередині) було б і негарно, і могло б перевищити розумний розмір
    форми. Заразом прибирає протухлі записи старші за TTL.

    known_fields: поля, які користувач уже заповнив вручну в об'єднаній
    формі калькулятора (ПЕРЕД першим зверненням до ШІ) - треба донести їх
    і до другого кроку (після відповіді на уточнення), щоб фінальний
    результат так само поважав уже введені значення."""
    import uuid as _uuid
    now = time.time()
    for key in [k for k, v in _AI_CALC_PENDING.items() if now - v["created"] > _AI_CALC_PENDING_TTL_SECONDS]:
        _AI_CALC_PENDING.pop(key, None)
    token = _uuid.uuid4().hex
    _AI_CALC_PENDING[token] = {
        "messages": messages, "description": description,
        "known_fields": known_fields or {}, "created": now,
    }
    return token


def _ai_calc_pending_pop(token):
    """Дістає й одразу видаляє збережений стан розмови за токеном -
    одноразовий, щоб ту саму відповідь не можна було випадково
    надіслати двічі й отримати два різні продовження однієї розмови.
    Повертає None, якщо токен невідомий або протух (TTL вичерпано)."""
    entry = _AI_CALC_PENDING.pop(token, None)
    if entry is None:
        return None
    if time.time() - entry["created"] > _AI_CALC_PENDING_TTL_SECONDS:
        return None
    return entry


AI_CALC_KNOWN_FIELD_NAMES = ["weight_kg", "material_price_per_kg", "turning_hours", "turning_rate_per_hour",
                             "milling_hours", "milling_rate_per_hour", "machine_hours", "machine_rate_per_hour",
                             "setup_cost_total", "quantity", "margin_pct"]


def ai_calc_known_fields_from_form(form):
    """Витягує параметри, які користувач уже заповнив вручну в об'єднаній
    формі калькулятора (напр. кількість, матеріал), щоб ШІ довелось
    оцінювати лише те, чого реально бракує - ці значення пізніше
    ПРИМУСОВО підставляються в результат ШІ (calculator_ai_estimate),
    навіть якщо сама модель порахувала б інакше. Порожні чи нечислові
    поля просто пропускаються (не помилка - це все необов'язкові поля)."""
    known = {}
    for name in AI_CALC_KNOWN_FIELD_NAMES:
        raw = (form.get(name) or "").strip()
        if not raw:
            continue
        try:
            known[name] = float(raw)
        except ValueError:
            continue
    return known


def ai_calc_synthesize_description_from_known_fields(known_fields):
    """Коли текстове поле опису й файл порожні, але користувач уже заповнив
    частину полів вручну (напр. кількість і вагу), будує короткий опис із
    цих значень - щоб ШІ мав на чому написати operations_description,
    замість повної відмови через 'опис або фото обов'язкові'."""
    if not known_fields:
        return ""
    labels = {
        "weight_kg": "вага заготовки {} кг", "material_price_per_kg": "матеріал ~{} грн/кг",
        "turning_hours": "токарна обробка {} год/од", "turning_rate_per_hour": "ставка токарної {} грн/год",
        "milling_hours": "фрезерна обробка {} год/од", "milling_rate_per_hour": "ставка фрезерної {} грн/год",
        "machine_hours": "додаткова обробка {} год/од", "machine_rate_per_hour": "ставка додаткової {} грн/год",
        "setup_cost_total": "наладка на партію {} грн", "quantity": "партія {} шт.", "margin_pct": "націнка {}%",
    }
    parts = [labels[k].format(int(v) if k == "quantity" else v) for k, v in known_fields.items() if k in labels]
    return "Відомі параметри: " + ", ".join(parts) + ". Опиши технологічний процес для такої деталі."


AI_CALC_REQUIRED_PARAMS = ["weight_kg", "material_price_per_kg", "turning_hours", "turning_rate_per_hour",
                           "milling_hours", "milling_rate_per_hour", "machine_hours", "machine_rate_per_hour",
                           "quantity", "margin_pct", "operations_description"]

AI_CALC_COST_ESTIMATE_TOOL = {
    "name": "cost_estimate",
    "description": "Структуровані параметри для розрахунку собівартості механообробної деталі та опис техпроцесу",
    "input_schema": {
        "type": "object",
        "properties": {
            "weight_kg": {"type": "number", "description": "Маса однієї деталі, кг"},
            "material_price_per_kg": {"type": "number", "description": "Ціна матеріалу за кг, грн"},
            "turning_hours": {"type": "number", "description": "Час токарної обробки однієї деталі, год"},
            "turning_rate_per_hour": {"type": "number", "description": "Ставка токарного верстата, грн/год"},
            "milling_hours": {"type": "number", "description": "Час фрезерної обробки однієї деталі, год"},
            "milling_rate_per_hour": {"type": "number", "description": "Ставка фрезерного верстата, грн/год"},
            "machine_hours": {"type": "number", "description": "Час іншої мех. обробки (шліфування тощо), год"},
            "machine_rate_per_hour": {"type": "number", "description": "Ставка іншого верстата, грн/год"},
            "quantity": {"type": "number", "description": "Кількість деталей у партії"},
            "margin_pct": {"type": "number", "description": "Рекомендована націнка, %"},
            "operations_description": {
                "type": "string",
                "description": "РОЗГОРНУТИЙ технологічний маршрут українською: кожен етап з нового рядка у форматі «N. Назва етапу — пояснення, верстат, час», мінімум 10-15 речень загалом, з назвою конкретного верстата на кожній операції; припущення - окремим останнім рядком «Припущення: ...»",
            },
        },
        "required": AI_CALC_REQUIRED_PARAMS,
    },
}

AI_CALC_ASK_QUESTION_TOOL = {
    "name": "ask_clarifying_question",
    "description": (
        "Постав ОДНЕ конкретне уточнююче питання користувачу, коли даних критично "
        "не вистачає для розрахунку (невідомий матеріал, невідомі габарити/вага і "
        "немає зображення, або незрозуміло, які операції взагалі потрібні) і їх "
        "не можна розумно оцінити типовим значенням. НЕ використовуй для другорядних "
        "параметрів (ціни, ставки, націнка, наладка) - їх завжди оцінюй сам."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Одне конкретне, вузьке питання українською для користувача"},
        },
        "required": ["question"],
    },
}


def ai_estimate_cost_params(description, api_key, model="claude-sonnet-5", images=None,
                             continue_messages=None, answer=None):
    """Викликає Anthropic Messages API, щоб перетворити текстовий опис і/або
    зображення (фото деталі, скан креслення) на структуровані параметри
    калькулятора вартості разом з описом усього техпроцесу.

    images: список werkzeug FileStorage (0..3 файли) - фото/скани креслення.

    Якщо опису критично не вистачає (немає матеріалу/габаритів/незрозумілі
    операції), ШІ може замість результату попросити ОДНЕ уточнення - тоді
    функція повертає {"status": "question", "question": "...", "messages": [...]}.
    Покажи "question" користувачу, збережи "messages", і виклич функцію ЗНОВУ
    з continue_messages=<збережені messages> та answer=<відповідь користувача>
    (тоді description/images ігноруються - розмова продовжується). Коли ШІ
    готовий дати результат, повертається {"status": "ok", "params": {...}}.

    Кидає AICalcError із зрозумілим для користувача поясненням при будь-якій
    технічній проблемі (немає ключа, мережа, невалідна відповідь, погане
    зображення) - виклик коду має ловити цей виняток і показувати
    flash-повідомлення, а не падати з 500."""
    if not api_key:
        raise AICalcError("Спочатку введи API-ключ Anthropic на сторінці «Налаштування»")

    import requests as _requests
    import json as _json
    import re as _re_local
    import base64 as _base64

    if continue_messages is not None:
        # Продовження розмови після того, як ШІ попросив уточнення, а
        # користувач на нього відповів. Шукаємо id останнього виклику
        # ask_clarifying_question, щоб коректно прив'язати tool_result -
        # без цього Anthropic API відхилить запит як невалідний.
        if not answer or not answer.strip():
            raise AICalcError("Введи відповідь на уточнююче питання ШІ")
        tool_use_id = None
        for msg in reversed(continue_messages):
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == "ask_clarifying_question":
                    tool_use_id = block.get("id")
                    break
            if tool_use_id:
                break
        if not tool_use_id:
            raise AICalcError("Не вдалось продовжити розрахунок — почни його заново")

        messages = continue_messages + [{
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": answer.strip()}],
        }]
        # На другому колі ШІ вже отримав усе, що просив - більше питань не
        # приймаємо (щоб не зациклитись), примусово вимагаємо фінальний результат.
        tools = [AI_CALC_COST_ESTIMATE_TOOL]
        tool_choice = {"type": "tool", "name": "cost_estimate"}
    else:
        images = images or []
        if (not description or not description.strip()) and not images:
            raise AICalcError("Опиши деталь текстом або завантаж фото/скан креслення")
        messages, tools, tool_choice = _ai_calc_build_first_turn(description, images, _base64)

    return _ai_calc_call_api(messages, tools, tool_choice, api_key, model,
                              _requests, _json, _re_local)


def _ai_calc_build_first_turn(description, images, _base64):
    """Будує messages/tools/tool_choice для першого звернення до ШІ (опис
    і/або файли деталі, без попередньої розмови). Винесено окремо від
    _ai_calc_call_api, щоб цикл обробки файлів (зображення/PDF/DXF) не
    дублювався між першим і продовженим викликом."""
    content_blocks = []
    for f in images:
        raw = f.read()
        if not raw:
            continue
        filename = getattr(f, "filename", "") or "файл"
        if len(raw) > AI_IMAGE_MAX_BYTES:
            raise AICalcError(
                f"Файл «{filename}» завеликий ({len(raw)//1024//1024} МБ) — стисни його, максимум 5 МБ"
            )
        # Перевіряємо РЕАЛЬНИЙ вміст файлу за магічними байтами, а не заявлений
        # браузером mimetype - його легко підмінити (файл .txt, перейменований
        # на .png, прийшов би сюди з mimetype="image/png", тож mimetype сам
        # по собі нічого не доводить і навмисно тут ігнорується).
        media_type = detect_image_media_type(raw)
        if media_type:
            page_pngs = [raw]
        elif raw.lstrip(b"\x00\x20\t\r\n")[:5] == b"%PDF-":
            # PDF (скан чи експорт креслення) - рендеримо сторінки в PNG,
            # Vision API дивиться лише на растр, не на сам PDF.
            try:
                page_pngs = render_pdf_to_png_bytes(raw, max_pages=3)
            except Exception as e:
                raise AICalcError(
                    f"Файл «{filename}» — не вдалось розібрати як PDF ({e}). "
                    f"Завантаж фото, скан, DXF- або PDF-файл."
                )
            media_type = "image/png"
        else:
            # Не растрове зображення і не PDF - можливо, це векторне
            # DXF-креслення. Рендеримо DXF у PNG "на льоту" (в пам'яті).
            try:
                page_pngs = [render_dxf_to_png_bytes(raw)]
            except Exception as e:
                raise AICalcError(
                    f"Файл «{filename}» — не зображення (PNG/JPEG/GIF/WEBP), не PDF і не вдалось розібрати як "
                    f"DXF-креслення ({e}). Завантаж фото, скан, DXF- або PDF-файл."
                )
            media_type = "image/png"

        for page_bytes in page_pngs:
            if len(page_bytes) > AI_IMAGE_MAX_BYTES:
                raise AICalcError(
                    f"Рендер файлу «{filename}» вийшов завеликим — спрости креслення чи зменши роздільність PDF"
                )
            if len(content_blocks) >= 3:
                raise AICalcError(
                    "Максимум 3 зображення за один розрахунок (кожна сторінка PDF рахується як окреме зображення)"
                )
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": _base64.b64encode(page_bytes).decode("ascii")},
            })

    text = description.strip() if description and description.strip() else (
        "Проаналізуй зображення (креслення/фото деталі) і розрахуй параметри собівартості "
        "та опиши повний технологічний процес виготовлення."
    )
    content_blocks.append({"type": "text", "text": text})

    messages = [{"role": "user", "content": content_blocks}]
    # Дозволяємо ШІ обрати: одразу дати результат (cost_estimate), або,
    # якщо даних критично не вистачає, поставити ОДНЕ уточнююче питання
    # (ask_clarifying_question) - tool_choice "any" змушує викликати
    # ЯКИЙСЬ з інструментів (а не вільний текст), але не диктує який саме.
    tools = [AI_CALC_COST_ESTIMATE_TOOL, AI_CALC_ASK_QUESTION_TOOL]
    tool_choice = {"type": "any"}
    return messages, tools, tool_choice


def _ai_calc_call_api(messages, tools, tool_choice, api_key, model, _requests, _json, _re_local):
    """Виконує сам HTTP-виклик Anthropic Messages API з готовими
    messages/tools/tool_choice і розбирає відповідь. Спільна для першого
    звернення і для продовження після уточнюючого питання - розбір
    помилок (мережа, 401/429, невалідна відповідь) однаковий в обох
    випадках.

    ПРИМУСОВИЙ виклик інструмента (tool_choice) замість текстового префілу
    асистента - надійніший спосіб гарантувати структурований JSON-вивід:
    Anthropic API сам повертає вже РОЗІБРАНИЙ об'єкт у полі tool_use.input,
    тож не потрібен жоден текстовий парсинг/фолбек, і модель фізично не
    може відповісти вільним поясненням замість виклику інструмента.
    (Раніше тут був префіл {"role": "assistant", "content": "{"} - той
    прийом підтримують не всі моделі/режими, і на практиці API почав
    повертати помилку 400 "This model does not support assistant message
    prefill" - тому перейшли на tool_choice, який працює завжди.)"""
    try:
        resp = _requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 3072,
                "system": AI_CALC_SYSTEM_PROMPT,
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
            },
            timeout=45,
        )
    except _requests.exceptions.Timeout:
        raise AICalcError("Anthropic API не відповів за 45 секунд — спробуй ще раз")
    except _requests.exceptions.RequestException as e:
        raise AICalcError(f"Не вдалось з'єднатися з Anthropic API: {e}")

    if resp.status_code == 401:
        raise AICalcError("Невірний API-ключ Anthropic — перевір його на сторінці «Налаштування»")
    if resp.status_code == 429:
        raise AICalcError("Перевищено ліміт запитів до Anthropic API — спробуй за хвилину")
    if resp.status_code != 200:
        try:
            err_msg = resp.json().get("error", {}).get("message", resp.text[:200])
        except Exception:
            err_msg = resp.text[:200]
        raise AICalcError(f"Anthropic API повернув помилку ({resp.status_code}): {err_msg}")

    try:
        data = resp.json()
    except Exception:
        raise AICalcError("Не вдалось розібрати відповідь Anthropic API")

    tool_block = None
    for block in data.get("content", []):
        if block.get("type") == "tool_use":
            tool_block = block
            break

    if tool_block is not None and tool_block.get("name") == "ask_clarifying_question":
        question = (tool_block.get("input") or {}).get("question") or "Уточни, будь ласка, деталі для розрахунку"
        return {
            "status": "question",
            "question": question,
            "messages": messages + [{"role": "assistant", "content": data.get("content", [])}],
        }

    params = tool_block.get("input") if (tool_block is not None and tool_block.get("name") == "cost_estimate") else None

    if params is None:
        # Захисний фолбек на випадок, якщо модель (всупереч tool_choice) все ж
        # повернула звичайний текст - намагаємось витягти з нього JSON-об'єкт,
        # перш ніж здатись. Розмову в цьому випадку продовжити неможливо
        # (немає id виклику інструмента для коректного tool_result), тож
        # correction-чат буде недоступний для такого результату.
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        extracted = _extract_json_object(text)
        if extracted is None:
            raise AICalcError("ШІ повернув відповідь у неочікуваному форматі — спробуй переформулювати опис коротше й конкретніше")
        try:
            params = _json.loads(extracted)
        except _json.JSONDecodeError:
            raise AICalcError("ШІ повернув відповідь у неочікуваному форматі — спробуй переформулювати опис коротше й конкретніше")
        conversation_messages = None
    else:
        # Будуємо продовжувану розмову: сам tool_use ШІ + синтетичний
        # tool_result - Anthropic API вимагає, щоб КОЖЕН tool_use завершувався
        # відповідним tool_result, перш ніж у розмові з'явиться новий
        # user-хід. Це дозволяє потім (міні-чат "виправ результат") просто
        # дописати нове текстове повідомлення користувача й ще раз
        # примусово викликати cost_estimate - без повторення всього опису
        # й без повторного завантаження файлів.
        conversation_messages = messages + [
            {"role": "assistant", "content": data.get("content", [])},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_block.get("id", "toolu_unknown"), "content": "Прийнято, параметри розрахунку збережено."}
            ]},
        ]

    if not isinstance(params, dict):
        raise AICalcError("ШІ повернув відповідь у неочікуваному форматі — спробуй переформулювати опис коротше й конкретніше")

    missing = [k for k in AI_CALC_REQUIRED_PARAMS if k not in params]
    if missing:
        raise AICalcError(f"ШІ не заповнив усі потрібні поля ({', '.join(missing)}) — спробуй ще раз")

    return {"status": "ok", "params": params, "messages": conversation_messages}


def ai_calc_apply_correction(prior_messages, correction_text, api_key, model="claude-sonnet-5"):
    """Міні-чат із ШІ-калькулятором: продовжує вже завершену розмову
    (prior_messages - це "messages" з попереднього {"status": "ok", ...}
    результату, вже завершені синтетичним tool_result) новим повідомленням
    користувача - виправленням чи уточненням до вже порахованого
    результату (напр. "вага не 2, а 3.2 кг", "додай свердління 4 отворів").
    Не потребує повторного опису чи повторного завантаження файлів - лише
    сам текст правки. Примусово вимагає новий виклик cost_estimate (як і
    продовження після уточнюючого питання), тож завжди повертає
    {"status": "ok", ...} або кидає AICalcError."""
    if not api_key:
        raise AICalcError("Спочатку введи API-ключ Anthropic на сторінці «Налаштування»")
    if not correction_text or not correction_text.strip():
        raise AICalcError("Напиши, що саме виправити")
    if not prior_messages:
        raise AICalcError("Не вдалось продовжити розмову з ШІ — почни розрахунок заново")

    import requests as _requests
    import json as _json
    import re as _re_local

    messages = prior_messages + [{"role": "user", "content": correction_text.strip()}]
    return _ai_calc_call_api(messages, [AI_CALC_COST_ESTIMATE_TOOL], {"type": "tool", "name": "cost_estimate"},
                              api_key, model, _requests, _json, _re_local)


def telegram_detect_chats(token):
    """Опитує Telegram getUpdates і повертає список чатів, з яких боту вже
    писали — щоб адмін міг обрати потрібний chat_id одним кліком, а не
    шукати його вручну через API. Користувач має спочатку написати боту
    щось (напр. /start), інакше список буде порожній."""
    import urllib.request
    import json as _json
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    req = urllib.request.Request(url)
    resp = urllib.request.urlopen(req, timeout=6)
    data = _json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API повернув помилку"))
    seen = {}
    for update in data.get("result", []):
        msg = update.get("message") or update.get("channel_post")
        if not msg:
            continue
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        title = chat.get("title") or chat.get("username") or chat.get("first_name") or str(chat_id)
        seen[chat_id] = title
    return [{"chat_id": cid, "title": title} for cid, title in seen.items()]


def role_required(*roles):
    """Декоратор для маршрутів: доступ мають лише перелічені ролі (admin завжди має доступ)."""
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            u = get_current_user()
            if not u:
                return redirect(url_for("login"))
            if u["role"] != "admin" and u["role"] not in roles:
                flash("У вас немає прав для цього розділу", "error")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.before_request
def block_viewer_writes():
    """Роль 'Перегляд' має доступ лише на читання — блокуємо будь-які POST-запити,
    крім виходу з системи."""
    u = get_current_user()
    if u and u["role"] == "viewer" and request.method == "POST" and request.endpoint != "logout":
        flash("Ваша роль дозволяє лише перегляд даних", "error")
        return redirect(request.referrer or url_for("dashboard"))


RATE_CURRENCIES = ("usd", "eur", "pln")


def _positive_float(value):
    try:
        v = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def rate_for(db, currency, on_date=None):
    """Курс валюти до UAH. З on_date ('YYYY-MM-DD') повертає курс, що діяв на
    ту дату (з історії), щоб старі звіти не "пливли" після зміни курсу.
    None — якщо курсу немає взагалі."""
    cur = (currency or "UAH").lower()
    if cur == "uah":
        return 1.0
    if cur not in RATE_CURRENCIES:
        return None
    if on_date:
        row = db.execute(
            f"SELECT {cur} AS r FROM rates_history WHERE recorded_on<=? AND {cur} IS NOT NULL ORDER BY recorded_on DESC LIMIT 1",
            (str(on_date)[:10],),
        ).fetchone()
        if row is None:  # дата раніше за всю історію - беремо найстаріший запис
            row = db.execute(
                f"SELECT {cur} AS r FROM rates_history WHERE {cur} IS NOT NULL ORDER BY recorded_on LIMIT 1"
            ).fetchone()
        if row and row["r"]:
            return row["r"]
    return _positive_float(get_setting(db, f"rate_{cur}", ""))


def to_uah(db, amount, currency, on_date=None):
    if not amount:
        return 0
    if currency == "UAH" or not currency:
        return amount
    rate = rate_for(db, currency, on_date)
    if rate is None:
        rate = 1.0  # курсу немає: сума не втрачається, а на дашборді показується попередження (rates_warning)
    return amount * rate


def save_rates(db, usd=None, eur=None, pln=None, source="manual"):
    """Зберігає курси: оновлює поточні налаштування і додає запис в історію
    на сьогодні. Невалідні/невід'ємні значення ігноруються. Повертає список
    прийнятих валют."""
    new = {"usd": _positive_float(usd), "eur": _positive_float(eur), "pln": _positive_float(pln)}
    accepted = [k for k, v in new.items() if v]
    if not accepted:
        return []
    if db.execute("SELECT 1 FROM rates_history LIMIT 1").fetchone() is None:
        # перший запис в історії: зберігаємо ПОПЕРЕДНІ курси як базові, щоб
        # давніші операції лишились за старим курсом
        old = {k: _positive_float(get_setting(db, f"rate_{k}", "")) for k in RATE_CURRENCIES}
        if any(old.values()):
            db.execute("INSERT OR IGNORE INTO rates_history (recorded_on, usd, eur, pln, source) VALUES ('1900-01-01',?,?,?,'baseline')",
                       (old["usd"], old["eur"], old["pln"]))
    today = today_str()
    row = db.execute("SELECT usd, eur, pln FROM rates_history WHERE recorded_on=?", (today,)).fetchone()
    merged = {k: (new[k] or (row[k] if row else None) or _positive_float(get_setting(db, f"rate_{k}", ""))) for k in RATE_CURRENCIES}
    db.execute("INSERT OR REPLACE INTO rates_history (recorded_on, usd, eur, pln, source) VALUES (?,?,?,?,?)",
               (today, merged["usd"], merged["eur"], merged["pln"], source))
    for k in accepted:
        set_setting(db, f"rate_{k}", str(new[k]))
    set_setting(db, "rates_updated_on", today)
    set_setting(db, "rates_source", source)
    db.commit()
    return accepted


def fetch_nbu_rates(timeout=8):
    """Офіційні курси НБУ (відкритий API). Повертає {'usd','eur','pln'}."""
    import urllib.request
    import json as _json
    url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
    out = {}
    for row in data:
        cc = str(row.get("cc", "")).lower()
        if cc in RATE_CURRENCIES and row.get("rate"):
            out[cc] = float(row["rate"])
    if not out:
        raise ValueError("НБУ не повернув жодного курсу")
    return out


def update_rates_from_nbu(db):
    rates = fetch_nbu_rates()
    return save_rates(db, rates.get("usd"), rates.get("eur"), rates.get("pln"), source="НБУ")


def rates_warning():
    """Текст попередження про курси або None: немає курсу для валюти, або він
    давно не оновлювався."""
    try:
        db = get_db()
        missing = [c.upper() for c in RATE_CURRENCIES if _positive_float(get_setting(db, f"rate_{c}", "")) is None]
        if missing:
            return "Не задано курс для " + ", ".join(missing) + ": суми в цій валюті рахуються некоректно."
        updated = get_setting(db, "rates_updated_on", "")
        if updated:
            age = (datetime.date.today() - datetime.date.fromisoformat(updated)).days
            if age >= 7:
                return f"Курси валют не оновлювались {age} дн. Оновіть їх у «Налаштуваннях»."
    except Exception:
        return None
    return None


app.jinja_env.globals["rates_warning"] = rates_warning


# ---------------------------------------------------------------------------
# Вкладення файлів (безпечне завантаження)
# ---------------------------------------------------------------------------

def allowed_upload_file(filename):
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_UPLOAD_EXTENSIONS


def save_uploaded_file(file_storage, entity_type, entity_id):
    """Безпечно зберігає завантажений файл: перевірка розширення (білий
    список), санітизація імені, унікальне ім'я файлу на диску (щоб
    виключити перезапис чужих файлів чи обхід шляху), розмір під лімітом.
    Повертає (ok, error_message_or_None)."""
    from werkzeug.utils import secure_filename
    import uuid

    if not file_storage or not file_storage.filename:
        return False, "Файл не обрано"
    original_name = file_storage.filename
    if not allowed_upload_file(original_name):
        allowed = ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS))
        return False, f"Формат файлу не підтримується. Дозволені: {allowed}"

    safe_name = secure_filename(original_name) or "file"
    ext = safe_name.rsplit(".", 1)[1].lower() if "." in safe_name else ""
    stored_name = f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex

    entity_dir = os.path.join(UPLOAD_DIR, entity_type, str(entity_id))
    os.makedirs(entity_dir, exist_ok=True)
    dest_path = os.path.join(entity_dir, stored_name)

    file_storage.save(dest_path)
    size = os.path.getsize(dest_path)
    if size > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        os.remove(dest_path)
        return False, f"Файл завеликий (макс. {MAX_UPLOAD_SIZE_MB} МБ)"

    db = get_db()
    db.execute(
        """INSERT INTO attachments (entity_type, entity_id, original_name, stored_name,
           size_bytes, uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?,?)""",
        (entity_type, entity_id, original_name, stored_name, size, session.get("user_id"), now_iso()),
    )
    db.commit()
    return True, None


def get_attachments(entity_type, entity_id):
    db = get_db()
    return db.execute(
        """SELECT a.*, u.full_name as uploader_name FROM attachments a
           LEFT JOIN users u ON u.id=a.uploaded_by
           WHERE a.entity_type=? AND a.entity_id=? ORDER BY a.uploaded_at DESC""",
        (entity_type, entity_id),
    ).fetchall()


# ---------------------------------------------------------------------------
# Розбір DXF-креслень (довжина контуру, габарити заготовки для фрезерування)
# ---------------------------------------------------------------------------

def analyze_dxf(path):
    """Рахує сумарну довжину контуру (периметр усіх ліній) та габарити
    заготовки з DXF-файлу — для підстановки в калькулятор вартості плоских
    деталей під фрезерування (контур і час обробки по периметру, площа для
    розрахунку матеріалу на партію). Для токарних деталей (тіла обертання)
    ці значення зазвичай не потрібні — там визначальні параметри це діаметр
    і довжина заготовки, які зручніше ввести вручну.
    Підтримує найпоширеніші типи об'єктів у кресленнях: LINE, CIRCLE, ARC,
    LWPOLYLINE, POLYLINE, SPLINE (сплайн апроксимується ламаною). НЕ підтримує
    STEP-файли (потрібне повноцінне 3D CAD-ядро, це окрема задача) та складні
    вкладені blocks/xref-посилання всередині DXF."""
    import ezdxf
    import math

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    total_length_mm = 0.0
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")

    def track(x, y):
        nonlocal min_x, min_y, max_x, max_y
        min_x, max_x = min(min_x, x), max(max_x, x)
        min_y, max_y = min(min_y, y), max(max_y, y)

    def dist(p1, p2):
        return ((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2) ** 0.5

    entity_count = 0
    for e in msp:
        t = e.dxftype()
        try:
            if t == "LINE":
                p1, p2 = (e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)
                total_length_mm += dist(p1, p2)
                track(*p1); track(*p2)
                entity_count += 1
            elif t == "CIRCLE":
                r = e.dxf.radius
                total_length_mm += 2 * math.pi * r
                cx, cy = e.dxf.center.x, e.dxf.center.y
                track(cx - r, cy - r); track(cx + r, cy + r)
                entity_count += 1
            elif t == "ARC":
                r = e.dxf.radius
                a1, a2 = math.radians(e.dxf.start_angle), math.radians(e.dxf.end_angle)
                angle = (a2 - a1) % (2 * math.pi)
                total_length_mm += r * angle
                cx, cy = e.dxf.center.x, e.dxf.center.y
                track(cx - r, cy - r); track(cx + r, cy + r)
                entity_count += 1
            elif t in ("LWPOLYLINE", "POLYLINE"):
                pts = [(p[0], p[1]) for p in e.get_points("xy")] if t == "LWPOLYLINE" else \
                      [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
                for x, y in pts:
                    track(x, y)
                for i in range(len(pts) - 1):
                    total_length_mm += dist(pts[i], pts[i + 1])
                if getattr(e, "closed", False) and len(pts) > 1:
                    total_length_mm += dist(pts[0], pts[-1])
                entity_count += 1
            elif t == "SPLINE":
                pts = list(e.flattening(0.5))
                for i in range(len(pts) - 1):
                    total_length_mm += dist(pts[i], pts[i + 1])
                    track(pts[i][0], pts[i][1])
                entity_count += 1
        except Exception:
            continue  # пропускаємо об'єкти з нестандартними/пошкодженими даними

    width_mm = max_x - min_x if max_x > min_x else 0
    height_mm = max_y - min_y if max_y > min_y else 0
    return {
        "cut_length_m": round(float(total_length_mm) / 1000, 3),
        "width_mm": round(float(width_mm), 1),
        "height_mm": round(float(height_mm), 1),
        "area_m2": round(float(width_mm * height_mm) / 1_000_000, 4),
        "entity_count": entity_count,
    }


def render_dxf_to_png_bytes(raw_bytes):
    """Рендерить DXF-креслення у растрове зображення PNG (в пам'яті, без
    тимчасових файлів на диску), щоб його можна було показати ШІ-калькулятору
    через Vision API - Anthropic вміє дивитись лише на растрові картинки,
    а не на векторні DXF-координати напряму.

    Використовує офіційний addon ezdxf.addons.drawing з matplotlib-бекендом -
    той самий рушій, що й у CAD-переглядачах для швидкого прев'ю креслень.
    Кидає ValueError із зрозумілим поясненням, якщо файл не є коректним DXF.

    ВАЖЛИВО: DXF буває двох форматів - текстовий (ASCII) і бінарний. Спроба
    примусово декодувати бінарний DXF як UTF-8-текст ламає його вміст і дає
    незрозумілу помилку на кшталт "Invalid binary data near line: ...". Тому,
    так само як analyze_dxf() вище, читаємо через ezdxf.readfile() з реального
    файлу на диску - лише вона вміє сама розпізнати текстовий/бінарний DXF і
    правильне кодування, замість того щоб вгадувати наперед."""
    import io
    import ezdxf
    import os
    import tempfile
    from ezdxf.addons.drawing import RenderContext, Frontend
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from ezdxf.addons.drawing.config import Configuration, ColorPolicy, BackgroundPolicy
    import matplotlib
    matplotlib.use("Agg")  # без графічного дисплея (сервер / .exe без консолі)
    import matplotlib.pyplot as plt

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
            tmp.write(raw_bytes)
            tmp_path = tmp.name
        doc = ezdxf.readfile(tmp_path)
    except Exception as e:
        raise ValueError(f"файл не схожий на коректний DXF: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    msp = doc.modelspace()
    fig = plt.figure(figsize=(8, 8), dpi=150)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor("white")
    fig.patch.set_facecolor("white")
    ctx = RenderContext(doc)
    backend = MatplotlibBackend(ax)
    # Примусово малюємо чорними лініями на білому тлі незалежно від кольорів
    # шарів у самому кресленні - інакше типовий DXF-колір "7" (білий у
    # палітрі AutoCAD для темного фону САПР) стає невидимим на білому
    # аркуші, і ШІ отримав би порожню картинку замість креслення.
    render_config = Configuration(color_policy=ColorPolicy.BLACK, background_policy=BackgroundPolicy.WHITE)
    Frontend(ctx, backend, config=render_config).draw_layout(msp, finalize=True)
    ax.set_axis_off()

    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", facecolor="white", bbox_inches="tight", pad_inches=0.2)
    finally:
        plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Кастомні поля (гнучкість без правок коду)
# ---------------------------------------------------------------------------

def get_custom_field_defs(entity_type):
    db = get_db()
    return db.execute(
        "SELECT * FROM custom_field_defs WHERE entity_type=? ORDER BY sort_order, id",
        (entity_type,),
    ).fetchall()


def get_custom_field_values(entity_type, entity_id):
    """Повертає список {def: <рядок визначення поля>, value: <значення або None>}
    для конкретного запису — зручно для рендеру форми з уже заповненими
    значеннями."""
    db = get_db()
    defs = get_custom_field_defs(entity_type)
    values = {
        row["field_id"]: row["value"]
        for row in db.execute(
            """SELECT cfv.field_id, cfv.value FROM custom_field_values cfv
               JOIN custom_field_defs cfd ON cfd.id=cfv.field_id
               WHERE cfd.entity_type=? AND cfv.entity_id=?""",
            (entity_type, entity_id),
        ).fetchall()
    }
    return [{"def": d, "value": values.get(d["id"])} for d in defs]


def save_custom_field_values(entity_type, entity_id, form):
    db = get_db()
    defs = get_custom_field_defs(entity_type)
    for d in defs:
        field_name = f"custom_{d['id']}"
        if field_name in form:
            value = form.get(field_name, "")
            db.execute(
                """INSERT INTO custom_field_values (field_id, entity_id, value) VALUES (?,?,?)
                   ON CONFLICT(field_id, entity_id) DO UPDATE SET value=excluded.value""",
                (d["id"], entity_id, value),
            )
    db.commit()


# ---------------------------------------------------------------------------
# Серверна валідація вводу
# ---------------------------------------------------------------------------

import re as _re
_EMAIL_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ValidationError(Exception):
    pass


def parse_number(form, field, default=0.0, min_value=None, max_value=None, required=False, label=None):
    """Безпечно парсить число з форми. При некоректному значенні (текст замість
    числа, від'ємне там, де не можна) — кидає ValidationError із зрозумілим
    повідомленням користувачу замість падіння сервера з 500 помилкою."""
    raw = form.get(field, "")
    label = label or field
    if raw in (None, ""):
        if required:
            raise ValidationError(f"Поле «{label}» обов'язкове")
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"Поле «{label}» має містити число")
    if min_value is not None and value < min_value:
        raise ValidationError(f"Поле «{label}» не може бути меншим за {min_value}")
    if max_value is not None and value > max_value:
        raise ValidationError(f"Поле «{label}» не може бути більшим за {max_value}")
    return value


def validate_email_field(form, field="email"):
    val = (form.get(field) or "").strip()
    if val and not _EMAIL_RE.match(val):
        raise ValidationError("Некоректний формат email")
    return val


def validate_required(form, field, label=None):
    val = (form.get(field) or "").strip()
    if not val:
        raise ValidationError(f"Поле «{label or field}» обов'язкове")
    return val


def with_validation(redirect_endpoint_fn):
    """Декоратор для POST-маршрутів: перехоплює ValidationError, показує
    зрозумілу помилку користувачу (замість падіння з 500) і повертає його
    назад на попередню сторінку зі збереженими даними форми не втраченими
    (браузер зазвичай сам відновлює введені значення при поверненні назад)."""
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            try:
                return view(*args, **kwargs)
            except ValidationError as e:
                flash(str(e), "error")
                return redirect(redirect_endpoint_fn(*args, **kwargs))
        return wrapped
    return decorator


# ---------------------------------------------------------------------------
# Планування виробництва (скінченно-ємнісний прямий алгоритм розкладу)
# ---------------------------------------------------------------------------

def _next_work_start(dt):
    """Пропускає вихідні (сб/нд), повертає найближчий робочий момент."""
    while dt.weekday() >= 5:
        dt = datetime.datetime.combine(dt.date() + datetime.timedelta(days=1), datetime.time(8, 0))
    if dt.hour < 8:
        dt = dt.replace(hour=8, minute=0, second=0, microsecond=0)
    if dt.hour >= 17:
        dt = _next_work_start(datetime.datetime.combine(dt.date() + datetime.timedelta(days=1), datetime.time(8, 0)))
    return dt


def _add_work_hours(start_dt, hours):
    """Додає робочі години до дати-часу, враховуючи робочий день 8:00-17:00 (8 год + обід)
    та вихідні дні. Повертає момент завершення."""
    remaining = hours
    cur = _next_work_start(start_dt)
    while remaining > 0:
        end_of_day = cur.replace(hour=17, minute=0, second=0, microsecond=0)
        available_today = (end_of_day - cur).total_seconds() / 3600.0
        if available_today <= 0:
            cur = _next_work_start(datetime.datetime.combine(cur.date() + datetime.timedelta(days=1), datetime.time(8, 0)))
            continue
        if remaining <= available_today:
            cur = cur + datetime.timedelta(hours=remaining)
            remaining = 0
        else:
            remaining -= available_today
            cur = _next_work_start(datetime.datetime.combine(cur.date() + datetime.timedelta(days=1), datetime.time(8, 0)))
    return cur


def schedule_production_order(db, order_id):
    """Скінченно-ємнісне пряме планування: кожна операція розпочинається не раніше,
    ніж (а) завершиться попередня операція цього замовлення в маршруті та
    (б) звільниться робочий центр від інших операцій, запланованих раніше.
    Це спрощений аналог алгоритму завантаження робочих центрів (finite-capacity forward scheduling),
    що використовується в MES/APS-системах."""
    ops = db.execute(
        "SELECT * FROM production_operations WHERE production_order_id=? ORDER BY step_order",
        (order_id,),
    ).fetchall()
    order = db.execute("SELECT * FROM production_orders WHERE id=?", (order_id,)).fetchone()
    cursor_time = _next_work_start(datetime.datetime.now())
    for op in ops:
        if op["status"] == "done":
            cursor_time = datetime.datetime.strptime(op["actual_end"] or op["planned_end"], "%Y-%m-%d %H:%M:%S")
            continue
        wc_id = op["work_center_id"]
        earliest = cursor_time
        if wc_id:
            # Робочий центр вільний не раніше, ніж завершиться остання запланована на ньому операція
            busy_until_row = db.execute(
                """SELECT MAX(planned_end) as t FROM production_operations
                   WHERE work_center_id=? AND id!=? AND status!='done'
                   AND planned_end IS NOT NULL""",
                (wc_id, op["id"]),
            ).fetchone()
            if busy_until_row and busy_until_row["t"]:
                busy_dt = datetime.datetime.strptime(busy_until_row["t"], "%Y-%m-%d %H:%M:%S")
                if busy_dt > earliest:
                    earliest = busy_dt
        start = _next_work_start(earliest)
        end = _add_work_hours(start, op["planned_hours"] * max(order["quantity"], 1) if order else op["planned_hours"])
        db.execute(
            "UPDATE production_operations SET planned_start=?, planned_end=? WHERE id=?",
            (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), op["id"]),
        )
        cursor_time = end


def create_production_order_from_deal(db, deal):
    """Створює виробниче замовлення з операціями на основі типової маршрутної карти
    для категорії обраного верстата, списує основні матеріали зі складу за нормами
    та розраховує розклад по робочих центрах."""
    if not deal["machine_id"]:
        return None
    machine = db.execute("SELECT * FROM machines WHERE id=?", (deal["machine_id"],)).fetchone()
    if not machine:
        return None
    routing = db.execute("SELECT * FROM routing_templates WHERE category=?", (machine["category"],)).fetchone()
    existing = db.execute("SELECT id FROM production_orders WHERE deal_id=?", (deal["id"],)).fetchone()
    if existing:
        return existing["id"]
    db.execute(
        "INSERT INTO production_orders (deal_id, machine_id, routing_template_id, quantity, status, due_date, created_at) VALUES (?,?,?,?,?,?,?)",
        (deal["id"], machine["id"], routing["id"] if routing else None, 1, "planned", deal["expected_close"], now_iso()),
    )
    order_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    if routing:
        steps = db.execute("SELECT * FROM routing_steps WHERE routing_template_id=? ORDER BY step_order", (routing["id"],)).fetchall()
        for s in steps:
            db.execute(
                """INSERT INTO production_operations (production_order_id, step_order, operation_name,
                   work_center_id, planned_hours, status) VALUES (?,?,?,?,?, 'waiting')""",
                (order_id, s["step_order"], s["operation_name"], s["work_center_id"], s["planned_hours"]),
            )
            if s["material_item_id"] and s["material_qty"]:
                consume_warehouse_stock(db, s["material_item_id"], s["material_qty"],
                                         reference=f"Виробниче замовлення №{order_id}: {s['operation_name']}")
        schedule_production_order(db, order_id)
    log_activity(db, deal["id"], deal["client_id"], "note",
                 f"Створено виробниче замовлення №{order_id} за маршрутною картою «{machine['category']}»")
    return order_id


def consume_warehouse_stock(db, item_id, qty, reference=""):
    """Списує матеріал зі складу, логує рух і за потреби надсилає сповіщення
    про критично низький залишок."""
    item = db.execute("SELECT * FROM warehouse_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        return
    db.execute("UPDATE warehouse_items SET qty_on_hand = qty_on_hand - ? WHERE id=?", (qty, item_id))
    db.execute(
        "INSERT INTO warehouse_transactions (item_id, qty_delta, type, reference, user_id, created_at) VALUES (?,?,?,?,?,?)",
        (item_id, -qty, "out", reference, session.get("user_id"), now_iso()),
    )
    new_qty = item["qty_on_hand"] - qty
    if new_qty <= item["min_qty"]:
        notify_all(
            f"⚠️ Низький залишок на складі: «{item['name']}» ({item['sku']}) — {new_qty:.1f} {item['unit']} "
            f"(мінімум {item['min_qty']} {item['unit']})"
        )
        create_task(db, None, None, f"Поповнити склад: {item['name']}",
                    "other", 2, description=f"Залишок {new_qty:.1f} {item['unit']}, нижче мінімуму {item['min_qty']}")

# ---------------------------------------------------------------------------
# Шаблони (HTML) — все в одному файлі
# ---------------------------------------------------------------------------

BASE_HTML = """
<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}Дашборд{% endblock %} · Оснастка-Маркет</title>
{% if current_user %}<meta name="csrf-token" content="{{ csrf_token() }}">{% endif %}
<script>
  // Застосовуємо збережений масштаб інтерфейсу ДО рендеру сторінки, щоб
  // уникнути помітного "стрибка" розміру після завантаження. Використовуємо
  // CSS zoom (не font-size): весь наявний CSS написаний у px, а не rem,
  // тому саме zoom коректно масштабує все пропорційно без переписування
  // стилів.
  (function(){
    var scale = localStorage.getItem('crm_ui_scale') || '100';
    document.documentElement.style.zoom = (scale / 100);
  })();
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#15171b;
  --bg-soft:#1b1e23;
  --panel:#20232a;
  --panel-2:#262a32;
  --border:#343941;
  --metal:linear-gradient(160deg,#3a3f47 0%,#23262c 45%,#2e3238 55%,#1a1c20 100%);
  --metal-light:#c7ccd1;
  --text:#e7e9ec;
  --text-dim:#9aa0aa;
  --accent:#ff7a1a;
  --accent-2:#4fa8e0;
  --green:#3fae5c;
  --red:#e05a4f;
  --yellow:#e0b34f;
  --radius:10px;
  --shadow:0 6px 18px rgba(0,0,0,.35);
}
*{box-sizing:border-box;}
html{font-size:16px;}
body{
  margin:0; background:var(--bg); color:var(--text);
  font-family:'Inter',system-ui,sans-serif; font-size:14px;
}
h1,h2,h3,.brand,.stage-title,.kpi-value{font-family:'Rajdhani',sans-serif;}
a{color:inherit;text-decoration:none;}
.layout{display:flex; min-height:100vh;}
.sidebar{
  width:230px; background:var(--metal); border-right:1px solid var(--border);
  display:flex; flex-direction:column; position:sticky; top:0; height:100vh;
  box-shadow: inset -1px 0 0 rgba(255,255,255,.03);
}
.brand{
  padding:20px 18px 14px; font-weight:700; font-size:20px; letter-spacing:1px;
  color:var(--metal-light); border-bottom:1px solid rgba(255,255,255,.06);
  display:flex; align-items:center; gap:10px;
}
.brand .gear{color:var(--accent); font-size:22px;}
.nav{padding:14px 10px; flex:1;}
.nav a{
  display:flex; align-items:center; gap:10px; padding:10px 12px; margin-bottom:4px;
  border-radius:8px; color:var(--text-dim); font-weight:500; font-size:13.5px;
  border:1px solid transparent;
}
.nav a:hover{background:rgba(255,255,255,.04); color:var(--text);}
.nav a.active{background:rgba(255,122,26,.12); color:var(--accent); border-color:rgba(255,122,26,.3);}
.nav .icon{width:18px; text-align:center; opacity:.9;}
.sidebar-footer{padding:14px 16px; border-top:1px solid rgba(255,255,255,.06); font-size:12.5px; color:var(--text-dim);}
.sidebar-footer a{color:var(--accent-2);}
.main{flex:1; min-width:0;}
.topbar{
  display:flex; align-items:center; justify-content:space-between;
  padding:14px 26px; border-bottom:1px solid var(--border); background:var(--bg-soft);
  position:sticky; top:0; z-index:5;
}
.topbar h1{font-size:20px; margin:0; font-weight:700; letter-spacing:.3px;}
.topbar .sub{color:var(--text-dim); font-size:12.5px; margin-top:2px;}
.search-box input{
  background:var(--panel); border:1px solid var(--border); color:var(--text);
  padding:8px 12px; border-radius:8px; width:260px; font-size:13px;
}
.reminders-dropdown{
  display:none; position:absolute; top:36px; right:0; width:280px; max-height:340px;
  overflow-y:auto; background:var(--panel); border:1px solid var(--border); border-radius:10px;
  box-shadow:var(--shadow); z-index:50;
}
.reminders-dropdown.open{display:block;}
.reminders-dropdown a:hover{background:rgba(255,255,255,.04);}
.content{padding:24px 26px 60px;}
.btn{
  display:inline-flex; align-items:center; gap:6px; padding:9px 16px; border-radius:8px;
  border:1px solid var(--border); background:var(--panel-2); color:var(--text);
  cursor:pointer; font-size:13.5px; font-weight:500; font-family:inherit;
}
.btn:hover{border-color:var(--accent);}
.btn-primary{background:linear-gradient(160deg,#ff8c3a,#e2650a); border-color:#e2650a; color:#1a1000; font-weight:700;}
.btn-primary:hover{filter:brightness(1.08);}
.btn-ghost{background:transparent;}
.btn-danger{border-color:var(--red); color:var(--red); background:transparent;}
.btn-sm{padding:5px 10px; font-size:12px;}
.card{background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); box-shadow:var(--shadow);}
.card-pad{padding:18px 20px;}
.grid{display:grid; gap:18px;}
.grid-4{grid-template-columns:repeat(4,1fr);}
.grid-3{grid-template-columns:repeat(3,1fr);}
.grid-2{grid-template-columns:2fr 1fr;}
@media(max-width:1100px){.grid-4{grid-template-columns:repeat(2,1fr);} .grid-2{grid-template-columns:1fr;}}
.nav-toggle,.nav-overlay{display:none;}
/* Телефон/вузький екран: меню ховається в висувну шухляду (кнопка ☰), а контент
   займає всю ширину - замість 230px сайдбару, що з'їдав би пів екрана. */
@media(max-width:820px){
  .sidebar{position:fixed; left:0; top:0; height:100vh; width:260px; z-index:40;
    transform:translateX(-105%); transition:transform .2s ease; overflow-y:auto;}
  body.nav-open .sidebar{transform:none;}
  .nav-overlay{position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:35;}
  body.nav-open .nav-overlay{display:block;}
  .nav-toggle{display:inline-flex; align-items:center; justify-content:center; width:38px; height:38px;
    flex:0 0 38px; font-size:20px; background:var(--panel); border:1px solid var(--border);
    color:var(--text); border-radius:8px; cursor:pointer; margin-right:10px;}
  .nav a{padding:13px 12px; font-size:14.5px;}
  .topbar{padding:10px 12px; gap:8px; flex-wrap:wrap;}
  .topbar h1{font-size:17px;}
  .topbar .sub{display:none;}
  .topbar-right{flex:1 1 100%;}
  .search-box{flex:1; min-width:0;}
  .search-box input{width:100%; box-sizing:border-box;}
  .content{padding:14px 12px 60px;}
  .grid-4,.grid-3,.grid-2,.form-grid{grid-template-columns:1fr;}
  .grid[style*="repeat(5"]{grid-template-columns:repeat(2,1fr) !important;}
  .card-pad{padding:14px;}
  .filters,.filters form{width:100%; flex-wrap:wrap;}
  .filters select,.filters input{min-width:0 !important; flex:1 1 140px;}
  .toolbar .btn{flex:1 1 auto; justify-content:center;}
  .reminders-dropdown{position:fixed; top:56px; left:8px; right:8px; width:auto;}
  .kanban-col{min-width:78vw; max-width:78vw;}
  .content table{font-size:12.5px;}
}
.kpi{padding:18px 20px;}
.kpi .label{color:var(--text-dim); font-size:12.5px; text-transform:uppercase; letter-spacing:.5px;}
.kpi-value{font-size:28px; font-weight:700; margin-top:4px;}
.kpi .delta{font-size:12px; margin-top:6px;}
.delta.up{color:var(--green);}
.delta.down{color:var(--red);}
table{width:100%; border-collapse:collapse;}
th{
  text-align:left; font-size:11.5px; text-transform:uppercase; letter-spacing:.5px;
  color:var(--text-dim); padding:10px 14px; border-bottom:1px solid var(--border);
}
td{padding:12px 14px; border-bottom:1px solid rgba(255,255,255,.04); font-size:13.5px;}
tr:hover td{background:rgba(255,255,255,.02);}
.table-wrap{overflow-x:auto;}
.badge{
  display:inline-block; padding:3px 9px; border-radius:20px; font-size:11.5px; font-weight:600;
  border:1px solid rgba(255,255,255,.15);
}
.badge-low{background:rgba(63,174,92,.15); color:var(--green); border-color:rgba(63,174,92,.3);}
.badge-medium{background:rgba(224,179,79,.15); color:var(--yellow); border-color:rgba(224,179,79,.3);}
.badge-high{background:rgba(224,90,79,.15); color:var(--red); border-color:rgba(224,90,79,.3);}
.pill{padding:3px 10px; border-radius:20px; font-size:11.5px; font-weight:600; color:#0c0c0c;}
.form-grid{display:grid; grid-template-columns:1fr 1fr; gap:14px 18px;}
.form-grid.full,.form-row-full{grid-column:1/-1;}
label{display:block; font-size:12px; color:var(--text-dim); margin-bottom:5px; font-weight:600; text-transform:uppercase; letter-spacing:.3px;}
input[type=text],input[type=number],input[type=date],input[type=email],input[type=password],select,textarea{
  width:100%; background:var(--bg-soft); border:1px solid var(--border); color:var(--text);
  padding:9px 11px; border-radius:8px; font-size:13.5px; font-family:inherit;
}
textarea{resize:vertical; min-height:70px;}
input:focus,select:focus,textarea:focus{outline:none; border-color:var(--accent-2);}
.field{margin-bottom:2px;}
.flash{padding:10px 16px; border-radius:8px; margin-bottom:16px; font-size:13.5px;}
.flash-success{background:rgba(63,174,92,.12); border:1px solid rgba(63,174,92,.35); color:#8fe0a5;}
.flash-error{background:rgba(224,90,79,.12); border:1px solid rgba(224,90,79,.35); color:#f0a49c;}
.section-title{font-size:15px; font-weight:700; margin:26px 0 12px; display:flex; align-items:center; gap:8px;}
.section-title .accent-dot{width:8px; height:8px; border-radius:50%; background:var(--accent);}
.kanban{display:flex; gap:14px; overflow-x:auto; padding-bottom:12px;}
.kanban-col{min-width:250px; max-width:250px; background:var(--bg-soft); border:1px solid var(--border); border-radius:10px;}
.kanban-col-head{padding:10px 12px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center;}
.kanban-col-head .dot{width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:6px;}
.kanban-col-title{font-size:12.5px; font-weight:700;}
.kanban-col-sum{font-size:11px; color:var(--text-dim);}
.kanban-body{padding:8px; display:flex; flex-direction:column; gap:8px; min-height:60px;}
.deal-card{
  background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:10px 12px;
  cursor:grab; font-size:12.5px; transition:transform .1s;
}
.deal-card:hover{border-color:var(--accent-2); transform:translateY(-1px);}
.deal-card.dragging{opacity:.4;}
.deal-card .title{font-weight:600; margin-bottom:4px; font-size:13px;}
.deal-card .meta{color:var(--text-dim); font-size:11.5px; margin-bottom:6px;}
.deal-card .amount{font-weight:700; color:var(--accent-2);}
.kanban-col.dragover{outline:2px dashed var(--accent); outline-offset:-4px;}
.timeline{border-left:2px solid var(--border); margin-left:8px; padding-left:18px;}
.ops-list{display:flex; flex-direction:column; gap:10px;}
.ops-step{display:flex; gap:12px; align-items:flex-start; padding:10px 12px; border:1px solid var(--border); border-radius:10px; background:rgba(255,255,255,.02);}
.ops-num{flex:0 0 26px; height:26px; border-radius:50%; background:var(--accent, #e2650a); color:#fff; font-size:12px; font-weight:700; display:flex; align-items:center; justify-content:center;}
.ops-text{flex:1; min-width:0;}
.ops-title{font-weight:700; font-size:13px; line-height:1.35;}
.ops-body{font-size:12.5px; line-height:1.55; color:var(--text-dim); margin-top:3px;}
.ops-note{font-size:12px; line-height:1.5; padding:8px 12px; border-left:3px solid var(--accent-2); background:rgba(255,255,255,.03); border-radius:4px;}
.timeline-item{position:relative; padding-bottom:18px;}
.timeline-item::before{content:""; position:absolute; left:-24px; top:4px; width:10px; height:10px; border-radius:50%; background:var(--accent-2); border:2px solid var(--bg-soft);}
.timeline-item .when{font-size:11.5px; color:var(--text-dim);}
.tag{font-size:11px; padding:2px 8px; border-radius:6px; background:var(--panel-2); border:1px solid var(--border); color:var(--text-dim); margin-right:6px;}
.empty{color:var(--text-dim); font-size:13px; padding:20px; text-align:center;}
.progress-bar{height:6px; background:var(--panel-2); border-radius:4px; overflow:hidden; margin-top:6px;}
.progress-bar > div{height:100%; background:linear-gradient(90deg,var(--accent-2),var(--accent));}
.detail-header{display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:14px; margin-bottom:18px;}
.stat-row{display:flex; gap:22px; flex-wrap:wrap; margin-top:10px;}
.stat-row .stat{font-size:12.5px; color:var(--text-dim);}
.stat-row .stat b{color:var(--text); font-size:14px; display:block; font-weight:700;}
.checkline{display:flex; align-items:center; gap:8px;}
.task-row.done .task-title{text-decoration:line-through; color:var(--text-dim);}
.login-wrap{min-height:100vh; display:flex; align-items:center; justify-content:center; background:radial-gradient(circle at 50% 20%, #2a2e35, #101216);}
.login-card{width:340px; max-width:92vw; box-sizing:border-box; padding:34px 30px; background:var(--metal); border:1px solid var(--border); border-radius:14px; box-shadow:0 20px 50px rgba(0,0,0,.5);}
.login-card h1{text-align:center; margin-bottom:4px; letter-spacing:1px;}
.login-card .sub{text-align:center; color:var(--text-dim); font-size:12.5px; margin-bottom:22px;}
.hint{font-size:11.5px; color:var(--text-dim); margin-top:14px; text-align:center;}
form .actions{display:flex; gap:10px; margin-top:18px;}
.toolbar{display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; flex-wrap:wrap; gap:10px;}
.filters{display:flex; gap:8px; flex-wrap:wrap;}
.filters select,.filters input{width:auto; min-width:140px;}
::-webkit-scrollbar{height:8px; width:8px;}
::-webkit-scrollbar-thumb{background:#3a3f47; border-radius:4px;}
</style>
</head>
<body>
{% if current_user %}
<div class="layout">
  <div class="nav-overlay" onclick="document.body.classList.remove('nav-open')"></div>
  <aside class="sidebar">
    <div class="brand"><span class="gear">⚙</span> Оснастка-Маркет</div>
    <nav class="nav">
      <a href="{{ url_for('dashboard') }}" class="{{ 'active' if active=='dashboard' }}"><span class="icon">▣</span> Дашборд</a>
      {% if current_user['role'] in ('admin','sales','viewer') %}
      <a href="{{ url_for('deals_board') }}" class="{{ 'active' if active=='deals' }}"><span class="icon">◧</span> Угоди (воронка)</a>
      <a href="{{ url_for('cost_calculator') }}" class="{{ 'active' if active=='calculator' }}"><span class="icon">🧮</span> Калькулятор вартості</a>
      <a href="{{ url_for('clients_list') }}" class="{{ 'active' if active=='clients' }}"><span class="icon">◈</span> Клієнти</a>
      <a href="{{ url_for('tasks_list') }}" class="{{ 'active' if active=='tasks' }}"><span class="icon">☑</span> Задачі</a>
      <a href="{{ url_for('calendar_view') }}" class="{{ 'active' if active=='calendar' }}"><span class="icon">📅</span> Календар</a>
      {% endif %}
      {% if current_user['role'] in ('admin','sales') %}
      <a href="{{ url_for('clients_reorder') }}" class="{{ 'active' if active=='reorder' }}"><span class="icon">↻</span> Повторні замовлення{% if reorder_count() %} <span class="badge badge-high">{{ reorder_count() }}</span>{% endif %}</a>
      {% endif %}
      {% if current_user['role'] in ('admin','sales','production','viewer') %}
      <a href="{{ url_for('parts_list') }}" class="{{ 'active' if active=='parts' }}"><span class="icon">⬡</span> Каталог деталей</a>
      {% endif %}
      <a href="{{ url_for('machines_list') }}" class="{{ 'active' if active=='machines' }}"><span class="icon">⬢</span> Наше обладнання</a>
      {% if current_user['role'] in ('admin','production','sales','viewer') %}
      <a href="{{ url_for('production_list') }}" class="{{ 'active' if active=='production' }}"><span class="icon">⚙</span> {{ 'Моя дільниця' if foreman_wc_id else 'Виробництво' }}</a>
      {% endif %}
      {% if current_user['role'] in ('admin','production','sales','viewer') %}
      <a href="{{ url_for('production_board') }}" class="{{ 'active' if active=='board' }}"><span class="icon">▦</span> Завантаження верстатів</a>
      <a href="{{ url_for('quality_list') }}" class="{{ 'active' if active=='quality' }}"><span class="icon">✔</span> Контроль якості</a>
      {% endif %}
      {% if current_user['role'] in ('admin','accountant','sales') %}
      <a href="{{ url_for('production_costs') }}" class="{{ 'active' if active=='costs' }}"><span class="icon">⚖</span> План / факт</a>
      {% endif %}
      {% if current_user['role'] in ('admin','production') %}
      <a href="{{ url_for('shift_log_list') }}" class="{{ 'active' if active=='shift_log' }}"><span class="icon">📋</span> Змінний журнал</a>
      {% endif %}
      {% if current_user['role'] in ('admin','warehouse','production','viewer') %}
      <a href="{{ url_for('warehouse_list') }}" class="{{ 'active' if active=='warehouse' }}"><span class="icon">▤</span> Склад</a>
      <a href="{{ url_for('purchase_request') }}" class="{{ 'active' if active=='purchase' }}"><span class="icon">🛒</span> Заявка на закупівлю</a>
      {% endif %}
      {% if current_user['role'] in ('admin','service','viewer') %}
      <a href="{{ url_for('service_list') }}" class="{{ 'active' if active=='service' }}"><span class="icon">✚</span> Рекламації та доробки</a>
      {% endif %}
      {% if current_user['role'] in ('admin','accountant') %}
      <a href="{{ url_for('finance_view') }}" class="{{ 'active' if active=='finance' }}"><span class="icon">💰</span> Фінанси</a>
      <a href="{{ url_for('payments_overdue') }}" class="{{ 'active' if active=='overdue' }}"><span class="icon">⏰</span> Прострочені оплати{% if overdue_summary().count %} <span class="badge badge-high">{{ overdue_summary().count }}</span>{% endif %}</a>
      {% endif %}
      {% if current_user['role']=='admin' %}
      <a href="{{ url_for('audit_log_view') }}" class="{{ 'active' if active=='audit' }}"><span class="icon">☰</span> Журнал змін</a>
      <a href="{{ url_for('users_list') }}" class="{{ 'active' if active=='users' }}"><span class="icon">☺</span> Користувачі</a>
      <a href="{{ url_for('settings_page') }}" class="{{ 'active' if active=='settings' }}"><span class="icon">⚒</span> Налаштування</a>
      {% endif %}
    </nav>
    <div class="sidebar-footer">
      <a href="{{ url_for('my_profile') }}" style="color:var(--text);">{{ current_user['full_name'] }}</a><br>
      <span style="color:var(--accent-2); font-size:11px;">{{ ROLE_LABEL.get(current_user['role'], current_user['role']) }}</span><br>
      <a href="{{ url_for('my_profile') }}">Мій профіль</a> · <a href="{{ url_for('logout') }}">Вийти</a>
      <div style="margin-top:10px; display:flex; align-items:center; gap:6px;">
        <span style="font-size:11px; color:var(--text-dim);">Масштаб:</span>
        <select id="ui-scale-select" onchange="setUiScale(this.value)" style="width:auto; padding:2px 6px; font-size:11px;">
          <option value="90">90%</option>
          <option value="100">100%</option>
          <option value="110">110%</option>
          <option value="125">125%</option>
        </select>
      </div>
    </div>
  </aside>
  <script>
    (function(){
      var saved = localStorage.getItem('crm_ui_scale') || '100';
      var sel = document.getElementById('ui-scale-select');
      if (sel) sel.value = saved;
    })();
    function setUiScale(value) {
      localStorage.setItem('crm_ui_scale', value);
      document.documentElement.style.zoom = (value / 100);
    }
    document.addEventListener('click', function(e){
      // ВАЖЛИВО: перевіряємо належність до самої кнопки-дзвоника через
      // .closest('#reminders-toggle-btn'), а НЕ порівнянням e.target.textContent
      // з '🔔' - коли є непрочитані нагадування, всередині кнопки з'являється
      // бейдж-лічильник (<span>3</span>), і клік по самому бейджу чи по кнопці
      // з бейджем давав e.target.textContent на кшталт "🔔3", що НІКОЛИ не
      // дорівнювало '🔔' - через це цей самий обробник миттєво закривав щойно
      // відкритий дропдаун одразу після onclick-тогла, і дзвоник візуально
      // "не реагував" на натискання.
      var dd = document.getElementById('reminders-dropdown');
      if (dd && dd.classList.contains('open') && !e.target.closest('.reminders-dropdown') && !e.target.closest('#reminders-toggle-btn')) {
        dd.classList.remove('open');
      }
    });
    // Клавіша "/" фокусує глобальний пошук з будь-якого місця сторінки -
    // зручно, щоб не тягнутись мишкою до поля вгорі. Не перехоплюємо "/",
    // якщо людина вже друкує в якомусь полі/textarea (інакше неможливо
    // було б ввести сам символ "/" в адресу, нотатки тощо).
    document.addEventListener('keydown', function(e){
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
      var tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
      var box = document.getElementById('global-search-input');
      if (box) { e.preventDefault(); box.focus(); box.select(); }
    });
  </script>
  <div class="main">
    <div class="topbar">
      <div style="display:flex; align-items:center; min-width:0;">
        <button type="button" class="nav-toggle" id="nav-toggle" aria-label="Меню" onclick="document.body.classList.toggle('nav-open')">☰</button>
        <div>
          <h1>{% block heading %}{% endblock %}</h1>
          <div class="sub">{% block subheading %}{% endblock %}</div>
        </div>
      </div>
      <div class="topbar-right" style="display:flex; align-items:center; gap:14px;">
        <div style="position:relative;">
          <button type="button" id="reminders-toggle-btn" onclick="document.getElementById('reminders-dropdown').classList.toggle('open')"
                  style="background:none; border:none; cursor:pointer; font-size:20px; color:var(--text); position:relative; padding:4px;">
            🔔
            {% if reminders %}<span style="position:absolute; top:0; right:0; background:var(--red); color:white; border-radius:50%; width:16px; height:16px; font-size:10px; display:flex; align-items:center; justify-content:center;">{{ reminders|length }}</span>{% endif %}
          </button>
          <div id="reminders-dropdown" class="reminders-dropdown">
            <div style="padding:10px 14px; font-weight:700; font-size:12.5px; border-bottom:1px solid var(--border);">Нагадування</div>
            {% for r in reminders %}
            <a href="{{ r.url }}" style="display:block; padding:10px 14px; font-size:12.5px; border-bottom:1px solid rgba(255,255,255,.04); color:{{ 'var(--red)' if r.level=='high' else 'var(--yellow)' }};">{{ r.text }}</a>
            {% else %}
            <div style="padding:14px; font-size:12.5px; color:var(--text-dim);">Нагадувань немає</div>
            {% endfor %}
          </div>
        </div>
        <form class="search-box" method="get" action="{{ url_for('search') }}">
          <input type="text" id="global-search-input" name="q" placeholder="Пошук клієнтів, угод... (/)" value="{{ request.args.get('q','') }}">
        </form>
      </div>
    </div>
    <div class="content">
      {% if session.get('default_password') %}
      <div class="flash flash-error" id="default-password-warning">
        ⚠️ Ви користуєтесь типовим демо-паролем. Змініть його, поки CRM доступна з інших комп'ютерів:
        <a href="{{ url_for('my_profile') }}" style="text-decoration:underline; font-weight:700;">змінити пароль</a>
      </div>
      {% endif %}
      {% with messages = get_flashed_messages(with_categories=true) %}
        {% for category, msg in messages %}
          <div class="flash flash-{{ category }}">{{ msg }}</div>
        {% endfor %}
      {% endwith %}
      {% block content %}{% endblock %}
    </div>
  </div>
</div>
{% else %}
  {% block content_noauth %}{% endblock %}
{% endif %}
</body>
</html>
"""

LOGIN_HTML = """
{% extends "base.html" %}
{% block content_noauth %}
<div class="login-wrap">
  <div class="login-card">
    <h1>⚙ Оснастка-Маркет</h1>
    <div class="sub">CRM для металообробного виробництва на власному парку обладнання</div>
    <form method="post"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      {% with messages = get_flashed_messages(with_categories=true) %}
        {% for category, msg in messages %}
          <div class="flash flash-{{ category }}">{{ msg }}</div>
        {% endfor %}
      {% endwith %}
      <div class="field" style="margin-bottom:12px;">
        <label>Логін</label>
        <input type="text" name="username" required autofocus>
      </div>
      <div class="field">
        <label>Пароль</label>
        <input type="password" name="password" required>
      </div>
      <div class="actions">
        <button class="btn btn-primary" style="width:100%; justify-content:center;" type="submit">Увійти</button>
      </div>
    </form>
    <div class="hint">Демо-доступ: admin / admin123</div>
  </div>
</div>
{% endblock %}
"""

DASHBOARD_HTML = """
{% extends "base.html" %}
{% block title %}Дашборд{% endblock %}
{% block heading %}Дашборд{% endblock %}
{% block subheading %}Загальна картина по замовленнях на металообробку · зведені суми — в грн за курсом із «Налаштувань»{% endblock %}
{% block content %}
{% if current_user['role'] in ('admin','accountant') and rates_warning() %}
<div id="rates-warning" class="card card-pad" style="border-left:4px solid var(--yellow); margin-bottom:14px; font-size:13px;">💱 {{ rates_warning() }} <a href="{{ url_for('settings_page') }}">Налаштування →</a></div>
{% endif %}
{% if current_user['role'] in ('admin','accountant') and overdue_summary().count %}
<div id="overdue-card" class="card card-pad" style="border-left:4px solid var(--red); margin-bottom:14px; font-size:13px;">⏰ Прострочено оплат: <b>{{ overdue_summary().count }}</b> на {{ overdue_summary().total | money('UAH') }} <a href="{{ url_for('payments_overdue') }}">Переглянути →</a></div>
{% endif %}
{% if current_user['role'] in ('admin','sales') and reorder_count() %}
<div id="reorder-card" class="card card-pad" style="border-left:4px solid var(--accent); margin-bottom:14px; font-size:13px;">↻ Клієнтів, що давно не замовляли: <b>{{ reorder_count() }}</b> <a href="{{ url_for('clients_reorder') }}">Нагадати →</a></div>
{% endif %}
{% if current_user['role'] in ('admin','sales','production','warehouse','service') %}
<div class="toolbar" style="margin-bottom:18px;">
  <div class="filters" style="gap:8px; display:flex; flex-wrap:wrap;">
    {% if current_user['role'] in ('admin','sales') %}
    <a class="btn btn-primary btn-sm" href="{{ url_for('client_new') }}">+ Клієнт</a>
    <a class="btn btn-primary btn-sm" href="{{ url_for('deal_new') }}">+ Угода</a>
    <a class="btn btn-sm" href="{{ url_for('cost_calculator') }}">🧮 Розрахунок</a>
    <a class="btn btn-sm" href="{{ url_for('tasks_list') }}">+ Задача</a>
    {% endif %}
    {% if current_user['role'] in ('admin','production') %}
    <a class="btn btn-primary btn-sm" href="{{ url_for('shift_log_new') }}">+ Запис у змінний журнал</a>
    {% endif %}
    {% if current_user['role'] in ('admin','warehouse') %}
    <a class="btn btn-primary btn-sm" href="{{ url_for('warehouse_item_new') }}">+ Позиція складу</a>
    {% endif %}
    {% if current_user['role'] in ('admin','service') %}
    <a class="btn btn-primary btn-sm" href="{{ url_for('service_list') }}">+ Рекламація</a>
    {% endif %}
  </div>
</div>
{% endif %}
<div class="grid grid-4">
  <div class="card kpi">
    <div class="label">Відкриті угоди</div>
    <div class="kpi-value">{{ open_deals }}</div>
    <div class="delta up">на суму {{ open_amount | money('UAH') }}</div>
  </div>
  <div class="card kpi">
    <div class="label">Виграно (всього)</div>
    <div class="kpi-value">{{ won_deals }}</div>
    <div class="delta up">{{ won_amount | money('UAH') }}</div>
  </div>
  <div class="card kpi">
    <div class="label">Конверсія лід → угода</div>
    <div class="kpi-value">{{ conversion }}%</div>
    <div class="delta">виграно / (виграно+програно)</div>
  </div>
  <div class="card kpi">
    <div class="label">Прострочені задачі</div>
    <div class="kpi-value" style="color: {{ 'var(--red)' if overdue_tasks else 'var(--text)' }};">{{ overdue_tasks }}</div>
    <div class="delta {{ 'down' if overdue_tasks else '' }}">потребують уваги</div>
  </div>
</div>

<div class="section-title"><span class="accent-dot"></span> Воронка продажів</div>
<div class="card card-pad">
  <div style="display:flex; align-items:flex-end; gap:6px; height:150px;">
    {% for key,label,color in STAGES %}
    {% set cnt = funnel.get(key,0) %}
    <div style="flex:1; display:flex; flex-direction:column; align-items:center; justify-content:flex-end; height:100%;">
      <div style="font-size:11px; margin-bottom:4px;">{{ cnt }}</div>
      <div style="width:100%; background:{{ color }}; border-radius:4px 4px 0 0; height:{{ (cnt / max_funnel * 100) if max_funnel else 0 }}%; min-height:2px;"></div>
    </div>
    {% endfor %}
  </div>
  <div style="display:flex; gap:6px; margin-top:8px;">
    {% for key,label,color in STAGES %}
    <div style="flex:1; font-size:10px; color:var(--text-dim); text-align:center; line-height:1.2;">{{ label }}</div>
    {% endfor %}
  </div>
</div>

{% if shift_today is not none %}
<div class="section-title"><span class="accent-dot"></span> Виробництво сьогодні (змінний журнал)</div>
<div class="grid grid-2">
  <div class="card card-pad">
    <div class="grid grid-2" style="gap:10px;">
      <div class="card kpi" style="box-shadow:none;">
        <div class="label">Виготовлено, шт</div>
        <div class="kpi-value">{{ shift_today['made'] }}</div>
      </div>
      <div class="card kpi" style="box-shadow:none;">
        <div class="label">Брак, шт</div>
        <div class="kpi-value" style="color: {{ 'var(--red)' if shift_today['scrap'] else 'var(--text)' }};">{{ shift_today['scrap'] }}</div>
      </div>
    </div>
    <div style="margin-top:10px;"><a class="btn btn-sm" href="{{ url_for('shift_log_list') }}">📋 Відкрити змінний журнал</a></div>
  </div>
  <div class="card card-pad">
    <div class="section-title" style="margin-top:0;"><span class="accent-dot"></span> По дільницях сьогодні</div>
    {% if shift_by_wc_today %}
    <table>
      <tr><th>Дільниця</th><th>Виготовлено</th><th>Брак</th></tr>
      {% for r in shift_by_wc_today %}
      <tr>
        <td>{{ r['work_center_name'] or '—' }}</td>
        <td>{{ r['made'] or 0 }}</td>
        <td>{{ r['scrap'] or 0 }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">Записів за сьогодні ще немає</div>
    {% endif %}
  </div>
</div>
{% endif %}

<div class="grid grid-2" style="margin-top:22px;">
  <div class="card card-pad">
    <div class="section-title" style="margin-top:0;"><span class="accent-dot"></span> Найближчі задачі</div>
    {% if upcoming_tasks %}
    <table>
      <tr><th>Задача</th><th>Клієнт</th><th>Термін</th><th>Менеджер</th></tr>
      {% for t in upcoming_tasks %}
      <tr>
        <td><a href="{{ url_for('tasks_list') }}">{{ t['title'] }}</a></td>
        <td>{{ t['client_name'] or '—' }}</td>
        <td style="color:{{ 'var(--red)' if t['due_date'] and t['due_date'] < today else 'var(--text)' }}">{{ t['due_date'] or '—' }}</td>
        <td>{{ t['manager_name'] or '—' }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">Немає активних задач</div>
    {% endif %}
  </div>
  <div class="card card-pad">
    <div class="section-title" style="margin-top:0;"><span class="accent-dot"></span> Топ клієнтів за сумою угод</div>
    {% if top_clients %}
    <table>
      <tr><th>Клієнт</th><th>Сума</th></tr>
      {% for c in top_clients %}
      <tr>
        <td><a href="{{ url_for('client_detail', client_id=c['id']) }}">{{ c['name'] }}</a></td>
        <td>{{ c['total'] | money('UAH') }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">Даних поки немає</div>
    {% endif %}
  </div>
</div>

<div class="section-title"><span class="accent-dot"></span> Виручка по виграних угодах, останні 6 місяців</div>
<div class="card card-pad">
  <div style="display:flex; align-items:flex-end; gap:14px; height:120px;">
    {% for m in revenue_by_month %}
    <div style="flex:1; display:flex; flex-direction:column; align-items:center; justify-content:flex-end; height:100%;">
      <div style="font-size:11px; margin-bottom:4px;">{{ m.value | money('UAH') }}</div>
      <div style="width:60%; background:linear-gradient(180deg,var(--accent-2),var(--accent)); border-radius:4px 4px 0 0; height:{{ (m.value / max_revenue * 100) if max_revenue else 0 }}%; min-height:2px;"></div>
      <div style="font-size:10.5px; color:var(--text-dim); margin-top:6px;">{{ m.label }}</div>
    </div>
    {% endfor %}
  </div>
</div>

<div class="grid grid-2" style="margin-top:22px;">
  {% if leaderboard %}
  <div>
    <div class="section-title" style="margin-top:0;"><span class="accent-dot"></span> Рейтинг менеджерів</div>
    <div class="card table-wrap">
      <table>
        <tr><th>Менеджер</th><th>Виграно</th><th>Сума</th><th>Відкриті</th><th>Програно</th></tr>
        {% for l in leaderboard %}
        <tr>
          <td>{{ l['full_name'] }}</td>
          <td>{{ l['won_cnt'] }}</td>
          <td>{{ l['won_sum'] | money('UAH') }}</td>
          <td>{{ l['open_cnt'] }}</td>
          <td>{{ l['lost_cnt'] }}</td>
        </tr>
        {% endfor %}
      </table>
    </div>
  </div>
  {% endif %}
  <div>
    <div class="section-title" style="margin-top:0;"><span class="accent-dot"></span> Стрічка подій</div>
    <div class="card card-pad">
      <div class="timeline">
        {% for a in activity_feed %}
        <div class="timeline-item">
          <div class="when">{{ a['created_at'][:16] }} · {{ a['manager_name'] or '' }}</div>
          <div>{{ a['text'] }} {% if a['client_name'] %}<span class="tag">{{ a['client_name'] }}</span>{% endif %}</div>
        </div>
        {% else %}
        <div class="empty">Подій ще немає</div>
        {% endfor %}
      </div>
    </div>
  </div>
</div>

{% if recent_lost %}
<div class="section-title"><span class="accent-dot"></span> Останні програні угоди</div>
<div class="card table-wrap">
  <table>
    <tr><th>Угода</th><th>Клієнт</th><th>Сума</th><th>Причина</th></tr>
    {% for dl in recent_lost %}
    <tr onclick="window.location='{{ url_for('deal_detail', deal_id=dl['id']) }}'" style="cursor:pointer;">
      <td>{{ dl['title'] }}</td>
      <td>{{ dl['client_name'] }}</td>
      <td>{{ dl['amount'] | money(dl['currency']) }}</td>
      <td style="color:var(--text-dim);">{{ dl['closed_reason'] or '—' }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}
{% endblock %}
"""

CLIENTS_IMPORT_HTML = """
{% extends "base.html" %}
{% block title %}Імпорт клієнтів{% endblock %}
{% block heading %}Імпорт клієнтів з Excel{% endblock %}
{% block subheading %}Формат файлу - як у /export/clients.xlsx{% endblock %}
{% block content %}
{% if result %}
<div class="card card-pad" style="margin-bottom:18px;">
  <div class="stat-row">
    <div class="stat">Створено<b style="color:var(--green);">{{ result.created }}</b></div>
    <div class="stat">Оновлено<b style="color:var(--accent-2);">{{ result.updated }}</b></div>
    <div class="stat">Пропущено<b style="color:var(--text-dim);">{{ result.skipped }}</b></div>
  </div>
  {% if result.errors %}
  <div class="section-title" style="font-size:13px;">Зауваження</div>
  <ul style="font-size:12.5px; color:var(--yellow); margin:0; padding-left:18px;">
    {% for e in result.errors %}<li>{{ e }}</li>{% endfor %}
  </ul>
  {% endif %}
  <a class="btn btn-sm" href="{{ url_for('clients_list') }}" style="margin-top:12px;">До списку клієнтів</a>
</div>
{% endif %}
<div class="card card-pad">
  <p style="font-size:12.5px; color:var(--text-dim);">
    Перша колонка (назва компанії) — обов'язкова. Якщо клієнт з такою назвою
    вже є в CRM — його дані оновляться, якщо ні — створиться новий запис.
    Найпростіше — спочатку зроби «⬇ Excel» на сторінці клієнтів, відредагуй
    той файл і зав ре його назад сюди.
  </p>
  <form method="post" action="{{ url_for('clients_import') }}" enctype="multipart/form-data"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
    <input type="file" name="file" accept=".xlsx,.xls" required style="margin-bottom:14px;">
    <button class="btn btn-primary" type="submit">Імпортувати</button>
  </form>
</div>
{% endblock %}
"""

CLIENTS_LIST_HTML = """
{% extends "base.html" %}
{% block title %}Клієнти{% endblock %}
{% block heading %}Клієнти{% endblock %}
{% block subheading %}Всього: {{ total }}{% if total_pages > 1 %} · сторінка {{ page }} з {{ total_pages }}{% endif %}{% endblock %}
{% block content %}
<div class="toolbar">
  <div class="filters">
    <form method="get" style="display:flex; gap:8px;">
      <input type="text" name="q" placeholder="Пошук за назвою, містом, телефоном..." value="{{ request.args.get('q','') }}" style="min-width:240px;">
      <select name="status" onchange="this.form.submit()">
        <option value="">Всі статуси</option>
        <option value="active" {{ 'selected' if request.args.get('status')=='active' }}>Активні</option>
        <option value="lost" {{ 'selected' if request.args.get('status')=='lost' }}>Втрачені</option>
      </select>
      <button class="btn btn-sm" type="submit">Знайти</button>
      {% if request.args.get('q') or request.args.get('status') %}<a class="btn btn-sm btn-ghost" href="{{ url_for('clients_list') }}">Очистити</a>{% endif %}
    </form>
  </div>
  <a class="btn btn-primary" href="{{ url_for('client_new') }}">+ Новий клієнт</a>
  <a class="btn" href="{{ url_for('export_clients_xlsx') }}">⬇ Excel</a>
  <a class="btn" href="{{ url_for('clients_import') }}">⬆ Імпорт з Excel</a>
</div>
<div class="card table-wrap">
<table>
  <tr><th>Компанія</th><th>Галузь</th><th>Місто</th><th>Контакт</th><th>Менеджер</th><th>Джерело</th><th>Статус</th></tr>
  {% for c in clients %}
  <tr onclick="window.location='{{ url_for('client_detail', client_id=c['id']) }}'" style="cursor:pointer;">
    <td><b>{{ c['name'] }}</b></td>
    <td>{{ c['industry'] or '—' }}</td>
    <td>{{ c['city'] or '—' }}</td>
    <td>{{ c['phone'] or '—' }}</td>
    <td>{{ c['manager_name'] or '—' }}</td>
    <td>{{ c['source'] or '—' }}</td>
    <td>{% if c['status']=='active' %}<span class="badge badge-low">активний</span>{% else %}<span class="badge badge-high">втрачений</span>{% endif %}</td>
  </tr>
  {% else %}
  <tr><td colspan="7" class="empty">Клієнтів не знайдено</td></tr>
  {% endfor %}
</table>
</div>
{% if total_pages > 1 %}
<div style="display:flex; gap:8px; justify-content:center; margin-top:16px;">
  {% if page > 1 %}<a class="btn btn-sm" href="?page={{ page-1 }}{{ '&status='+request.args.get('status') if request.args.get('status') }}{{ '&q='+request.args.get('q') if request.args.get('q') }}">← Попередня</a>{% endif %}
  <span style="padding:6px 10px; color:var(--text-dim); font-size:12.5px;">Сторінка {{ page }} з {{ total_pages }}</span>
  {% if page < total_pages %}<a class="btn btn-sm" href="?page={{ page+1 }}{{ '&status='+request.args.get('status') if request.args.get('status') }}{{ '&q='+request.args.get('q') if request.args.get('q') }}">Наступна →</a>{% endif %}
</div>
{% endif %}
{% endblock %}
"""

CLIENT_FORM_HTML = """
{% extends "base.html" %}
{% block title %}{{ 'Редагувати клієнта' if client else 'Новий клієнт' }}{% endblock %}
{% block heading %}{{ 'Редагувати клієнта' if client else 'Новий клієнт' }}{% endblock %}
{% block content %}
<form method="post" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
  <div class="form-grid">
    <div class="field"><label>Назва компанії *</label><input type="text" name="name" required value="{{ client['name'] if client else '' }}"></div>
    <div class="field"><label>ЄДРПОУ</label><input type="text" name="edrpou" value="{{ client['edrpou'] if client else '' }}"></div>
    <div class="field"><label>Галузь</label><input type="text" name="industry" value="{{ client['industry'] if client else '' }}"></div>
    <div class="field"><label>Місто</label><input type="text" name="city" value="{{ client['city'] if client else '' }}"></div>
    <div class="field form-row-full"><label>Адреса</label><input type="text" name="address" value="{{ client['address'] if client else '' }}"></div>
    <div class="field"><label>Телефон</label><input type="text" name="phone" value="{{ client['phone'] if client else '' }}"></div>
    <div class="field"><label>Email</label><input type="email" name="email" value="{{ client['email'] if client else '' }}"></div>
    <div class="field"><label>Сайт</label><input type="text" name="website" value="{{ client['website'] if client else '' }}"></div>
    <div class="field">
      <label>Джерело</label>
      <select name="source">
        {% for s in LEAD_SOURCES %}<option {{ 'selected' if client and client['source']==s }}>{{ s }}</option>{% endfor %}
      </select>
    </div>
    <div class="field">
      <label>Менеджер</label>
      <select name="manager_id">
        {% for m in managers %}<option value="{{ m['id'] }}" {{ 'selected' if client and client['manager_id']==m['id'] }}>{{ m['full_name'] }}</option>{% endfor %}
      </select>
    </div>
    <div class="field">
      <label>Статус</label>
      <select name="status">
        <option value="active" {{ 'selected' if client and client['status']=='active' }}>Активний</option>
        <option value="lost" {{ 'selected' if client and client['status']=='lost' }}>Втрачений</option>
      </select>
    </div>
    <div class="field form-row-full"><label>Примітки</label><textarea name="notes">{{ client['notes'] if client else '' }}</textarea></div>
    {% for cf in custom_fields %}
    <div class="field">
      <label>{{ cf.def['label'] }}</label>
      {% if cf.def['field_type']=='select' %}
      <select name="custom_{{ cf.def['id'] }}">
        <option value="">—</option>
        {% for opt in (cf.def['options'] or '').split(',') %}
        <option value="{{ opt.strip() }}" {{ 'selected' if cf.value==opt.strip() }}>{{ opt.strip() }}</option>
        {% endfor %}
      </select>
      {% elif cf.def['field_type']=='date' %}
      <input type="date" name="custom_{{ cf.def['id'] }}" value="{{ cf.value or '' }}">
      {% elif cf.def['field_type']=='number' %}
      <input type="number" step="0.01" name="custom_{{ cf.def['id'] }}" value="{{ cf.value or '' }}">
      {% else %}
      <input type="text" name="custom_{{ cf.def['id'] }}" value="{{ cf.value or '' }}">
      {% endif %}
    </div>
    {% endfor %}
  </div>
  <div class="actions">
    <button class="btn btn-primary" type="submit">Зберегти</button>
    <a class="btn btn-ghost" href="{{ url_for('clients_list') }}">Скасувати</a>
  </div>
</form>
{% endblock %}
"""

CLIENT_DETAIL_HTML = """
{% extends "base.html" %}
{% block title %}{{ client['name'] }}{% endblock %}
{% block heading %}{{ client['name'] }}{% endblock %}
{% block subheading %}{{ client['industry'] or '' }} · {{ client['city'] or '' }}{% endblock %}
{% block content %}
<div class="detail-header">
  <div>
    <div class="stat-row">
      <div class="stat">Телефон<b>{{ client['phone'] or '—' }}</b></div>
      <div class="stat">Email<b>{{ client['email'] or '—' }}</b></div>
      <div class="stat">ЄДРПОУ<b>{{ client['edrpou'] or '—' }}</b></div>
      <div class="stat">Менеджер<b>{{ client['manager_name'] or '—' }}</b></div>
      <div class="stat">Джерело<b>{{ client['source'] or '—' }}</b></div>
      <div class="stat">Статус<b>{{ 'Активний' if client['status']=='active' else 'Втрачений' }}</b></div>
    </div>
  </div>
  <div style="display:flex; gap:8px;">
    <a class="btn" href="{{ url_for('client_edit', client_id=client['id']) }}">Редагувати</a>
    <a class="btn btn-primary" href="{{ url_for('deal_new') }}?client_id={{ client['id'] }}">+ Нова угода</a>
    <form method="post" action="{{ url_for('client_delete', client_id=client['id']) }}" onsubmit="return confirm('Видалити клієнта та всі пов'+String.fromCharCode(39)+'язані угоди, задачі й обладнання? Дію не можна скасувати.');"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <button class="btn btn-danger" type="submit">Видалити</button>
    </form>
  </div>
</div>

<div class="grid grid-2">
  <div>
    <div class="section-title"><span class="accent-dot"></span> Угоди клієнта</div>
    <div class="card table-wrap">
      <table>
        <tr><th>Угода</th><th>Стадія</th><th>Сума</th><th>Ймовірність</th></tr>
        {% for dl in deals %}
        <tr onclick="window.location='{{ url_for('deal_detail', deal_id=dl['id']) }}'" style="cursor:pointer;">
          <td>{{ dl['title'] }}</td>
          <td><span class="pill" style="background:{{ STAGE_COLOR[dl['stage']] }}">{{ STAGE_LABEL[dl['stage']] }}</span></td>
          <td>{{ dl['amount'] | money(dl['currency']) }}</td>
          <td>{{ dl['probability'] }}%</td>
        </tr>
        {% else %}
        <tr><td colspan="4" class="empty">Угод ще немає</td></tr>
        {% endfor %}
      </table>
    </div>

    <div class="section-title"><span class="accent-dot"></span> Сервісні заявки</div>
    <div class="card table-wrap">
      <table>
        <tr><th>Проблема</th><th>Статус</th><th>Дата</th></tr>
        {% for t in tickets %}
        <tr>
          <td>{{ t['issue'] }}</td>
          <td><span class="badge badge-{{ 'high' if t['status']!='done' else 'low' }}">{{ dict(SERVICE_STATUSES)[t['status']] }}</span></td>
          <td>{{ t['created_at'][:10] }}</td>
        </tr>
        {% else %}
        <tr><td colspan="3" class="empty">Заявок немає</td></tr>
        {% endfor %}
      </table>
    </div>
  </div>

  <div>
    <div class="section-title"><span class="accent-dot"></span> Контактні особи</div>
    <div class="card card-pad">
      {% for ct in contacts %}
      <div style="padding:8px 0; border-bottom:1px solid var(--border);">
        <b>{{ ct['full_name'] }}</b> {% if ct['is_primary'] %}<span class="tag">головний</span>{% endif %}<br>
        <span style="color:var(--text-dim); font-size:12.5px;">{{ ct['position'] or '' }}</span><br>
        <span style="font-size:12.5px;">{{ ct['phone'] or '' }} {{ ct['email'] or '' }}</span>
      </div>
      {% else %}
      <div class="empty">Контактів немає</div>
      {% endfor %}
      <form method="post" action="{{ url_for('contact_add', client_id=client['id']) }}" style="margin-top:14px;"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <div class="field" style="margin-bottom:8px;"><input type="text" name="full_name" placeholder="ПІБ" required></div>
        <div class="field" style="margin-bottom:8px;"><input type="text" name="position" placeholder="Посада"></div>
        <div class="field" style="margin-bottom:8px;"><input type="text" name="phone" placeholder="Телефон"></div>
        <button class="btn btn-sm" type="submit">+ Додати контакт</button>
      </form>
    </div>

    <div class="section-title"><span class="accent-dot"></span> Примітки</div>
    <div class="card card-pad">
      <p style="white-space:pre-wrap; margin:0; font-size:13px; color:var(--text-dim);">{{ client['notes'] or 'Без приміток' }}</p>
    </div>
  </div>
</div>

<div class="section-title"><span class="accent-dot"></span> Файли та документи (договори, специфікації)</div>
<div class="card card-pad">
  <div style="display:flex; flex-wrap:wrap; gap:10px; margin-bottom:14px;">
    {% for a in attachments %}
    <div style="border:1px solid var(--border); border-radius:8px; padding:10px; width:220px;">
      <div style="font-size:12.5px; word-break:break-all; margin-bottom:8px;">📎 {{ a['original_name'] }}</div>
      <div style="font-size:11px; color:var(--text-dim); margin-bottom:8px;">{{ (a['size_bytes']/1024)|round(1) }} КБ · {{ a['uploader_name'] or '' }}</div>
      <div style="display:flex; gap:6px;">
        <a class="btn btn-sm" href="{{ url_for('attachment_download', attachment_id=a['id']) }}">Завантажити</a>
        <form method="post" action="{{ url_for('attachment_delete', attachment_id=a['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm btn-danger" type="submit">✕</button></form>
      </div>
    </div>
    {% else %}
    <div class="empty">Файлів ще немає</div>
    {% endfor %}
  </div>
  <form method="post" action="{{ url_for('attachment_upload', entity_type='client', entity_id=client['id']) }}" enctype="multipart/form-data"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
    <input type="file" name="file" required>
    <button class="btn btn-sm btn-primary" type="submit">+ Додати файл</button>
  </form>
</div>

{% endblock %}
"""

DEALS_BOARD_HTML = """
{% extends "base.html" %}
{% block title %}Воронка угод{% endblock %}
{% block heading %}Воронка угод{% endblock %}
{% block subheading %}Перетягуйте картки між стадіями{% endblock %}
{% block content %}
<div class="toolbar">
  <div class="filters">
    <form method="get" style="display:flex; gap:8px;">
      <select name="manager_id" onchange="this.form.submit()">
        <option value="">Всі менеджери</option>
        {% for m in managers %}<option value="{{ m['id'] }}" {{ 'selected' if request.args.get('manager_id')|string == m['id']|string }}>{{ m['full_name'] }}</option>{% endfor %}
      </select>
    </form>
    <input type="text" id="deal-filter-input" placeholder="Швидкий пошук по назві чи клієнту..." oninput="filterDealCards(this.value)" style="min-width:260px;">
  </div>
  <a class="btn btn-primary" href="{{ url_for('deal_new') }}">+ Нова угода</a>
  <a class="btn" href="{{ url_for('export_deals_xlsx') }}">⬇ Excel</a>
</div>
<div class="kanban" id="kanban">
  {% for key,label,color in STAGES %}
  <div class="kanban-col" data-stage="{{ key }}">
    <div class="kanban-col-head">
      <div class="kanban-col-title"><span class="dot" style="background:{{ color }};"></span>{{ label }}</div>
    </div>
    <div class="kanban-col-sum" style="padding:0 12px 8px;">{{ (columns[key]|length) }} · {{ sums.get(key,0) | money }}</div>
    <div class="kanban-body" data-stage="{{ key }}">
      {% for dl in columns[key] %}
      <div class="deal-card" draggable="true" data-id="{{ dl['id'] }}" onclick="location.href='{{ url_for('deal_detail', deal_id=dl['id']) }}'">
        <div class="title">{{ dl['title'] }}</div>
        <div class="meta">{{ dl['client_name'] }}</div>
        <div class="meta">{{ dl['manager_name'] or '—' }} · {{ dl['expected_close'] or '' }}</div>
        <div class="amount">{{ dl['amount'] | money(dl['currency']) }}</div>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endfor %}
  <div class="kanban-col" data-stage="{{ LOST_STAGE }}" style="min-width:220px;">
    <div class="kanban-col-head">
      <div class="kanban-col-title"><span class="dot" style="background:{{ STAGE_COLOR[LOST_STAGE] }};"></span>{{ STAGE_LABEL[LOST_STAGE] }}</div>
    </div>
    <div class="kanban-col-sum" style="padding:0 12px 8px;">{{ (columns.get(LOST_STAGE,[])|length) }}</div>
    <div class="kanban-body" data-stage="{{ LOST_STAGE }}">
      {% for dl in columns.get(LOST_STAGE,[]) %}
      <div class="deal-card" draggable="true" data-id="{{ dl['id'] }}" onclick="location.href='{{ url_for('deal_detail', deal_id=dl['id']) }}'">
        <div class="title">{{ dl['title'] }}</div>
        <div class="meta">{{ dl['client_name'] }}</div>
        <div class="amount">{{ dl['amount'] | money(dl['currency']) }}</div>
      </div>
      {% endfor %}
    </div>
  </div>
</div>
<script>
function filterDealCards(text) {
  const needle = text.trim().toLowerCase();
  document.querySelectorAll('.deal-card').forEach(card => {
    const hay = card.textContent.toLowerCase();
    card.style.display = (!needle || hay.includes(needle)) ? '' : 'none';
  });
}
let dragged = null;
document.querySelectorAll('.deal-card').forEach(card => {
  card.addEventListener('dragstart', e => { dragged = card; card.classList.add('dragging'); e.stopPropagation(); });
  card.addEventListener('dragend', () => card.classList.remove('dragging'));
});
document.querySelectorAll('.kanban-body').forEach(col => {
  col.addEventListener('dragover', e => { e.preventDefault(); col.parentElement.classList.add('dragover'); });
  col.addEventListener('dragleave', () => col.parentElement.classList.remove('dragover'));
  col.addEventListener('drop', e => {
    e.preventDefault();
    col.parentElement.classList.remove('dragover');
    if (!dragged) return;
    const dealId = dragged.dataset.id;
    const stage = col.dataset.stage;
    col.appendChild(dragged);
    fetch(`/deals/${dealId}/move/${stage}`, {method:'POST', headers:{'X-Requested-With':'fetch', 'X-CSRF-Token':'{{ csrf_token() }}'}})
      .then(r=>r.json()).then(d=>{
        if (d.needs_machine && d.redirect) { window.location = d.redirect; return; }
        location.reload();
      })
      .catch(()=>location.reload());
  });
});
</script>
{% endblock %}
"""

CALCULATOR_HTML = """
{% extends "base.html" %}
{% block title %}Калькулятор вартості{% endblock %}
{% block heading %}Калькулятор вартості механообробки{% endblock %}
{% block subheading %}Заповни відомі поля вручну і/або додай опис чи фото/DXF/PDF — решту порахує ШІ{% endblock %}
{% block content %}

<div style="text-align:right; margin-bottom:10px;">
  <a class="btn btn-ghost btn-sm" href="{{ url_for('calculator_history') }}">📜 Історія розрахунків</a>
</div>

{% if ai_question %}
<div class="card card-pad" style="margin-bottom:20px; border-color:var(--accent);">
  <div style="font-weight:600; margin-bottom:8px;">🤖 ШІ просить уточнення:</div>
  <div style="margin-bottom:12px;">{{ ai_question }}</div>
  <form method="post" action="{{ url_for('calculator_ai_estimate') }}" style="display:flex; gap:10px; align-items:flex-end; flex-wrap:wrap;">
    <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
    <input type="hidden" name="ai_clarify_token" value="{{ ai_clarify_token }}">
    <div class="field" style="flex:1; min-width:240px;">
      <label>Твоя відповідь</label>
      <input type="text" name="ai_clarify_answer" placeholder="напр.: сталь 45, пруток Ø40, довжина 120 мм" required autofocus>
    </div>
    <button class="btn btn-primary" type="submit">Надіслати відповідь</button>
  </form>
</div>
{% endif %}

<div class="grid grid-2">
  <form method="post" action="{{ url_for('calculator_ai_estimate') }}" enctype="multipart/form-data" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
    <div class="section-title" style="margin-top:0;">🤖 Опис і/або файл (необов'язково)</div>
    <div class="field" style="margin-bottom:10px;">
      <label>Опиши деталь та операції звичайною мовою (необов'язково, якщо заповниш поля нижче або додаси фото)</label>
      <textarea name="ai_description" placeholder="напр.: Вал зі сталі 40Х, пруток Ø30мм, довжина 250мм, точіння + фрезерування шпонкового паза, партія 50 шт" style="min-height:60px;">{{ ai_description or '' }}</textarea>
    </div>
    <div class="field" style="margin-bottom:14px;">
      <label>Фото деталі, скан/фото креслення, DXF- або PDF-файл (до 3 файлів або сторінок, до 5 МБ кожне)</label>
      <input type="file" name="ai_images" accept="image/png,image/jpeg,image/webp,image/gif,.dxf,.pdf,application/pdf" multiple>
    </div>

    <div class="section-title">Матеріал заготовки</div>
    <div class="form-grid" style="margin-bottom:14px;">
      <div class="field"><label>Вага заготовки, кг</label><input type="number" step="0.01" name="weight_kg" value="{{ request.form.get('weight_kg','') }}" placeholder="заповни або лиши ШІ"></div>
      <div class="field"><label>Ціна металу, грн/кг</label><input type="number" step="0.01" name="material_price_per_kg" value="{{ request.form.get('material_price_per_kg','') }}" placeholder="заповни або лиши ШІ"></div>
    </div>
    <div class="section-title">Токарна обробка</div>
    <div class="form-grid" style="margin-bottom:14px;">
      <div class="field"><label>Час токарної обробки, год</label><input type="number" step="0.01" name="turning_hours" value="{{ request.form.get('turning_hours','') }}"></div>
      <div class="field"><label>Ставка, грн/год</label><input type="number" step="0.01" name="turning_rate_per_hour" value="{{ request.form.get('turning_rate_per_hour','') }}"></div>
    </div>
    <div class="section-title">Фрезерна обробка (фрезерування / свердління / різьба)</div>
    <div class="form-grid" style="margin-bottom:14px;">
      <div class="field"><label>Час фрезерної обробки, год</label><input type="number" step="0.01" name="milling_hours" value="{{ request.form.get('milling_hours','') }}"></div>
      <div class="field"><label>Ставка, грн/год</label><input type="number" step="0.01" name="milling_rate_per_hour" value="{{ request.form.get('milling_rate_per_hour','') }}"></div>
    </div>
    <div class="section-title">Додаткова обробка (шліфування / термообробка / покриття)</div>
    <div class="form-grid" style="margin-bottom:14px;">
      <div class="field"><label>Машино-години</label><input type="number" step="0.01" name="machine_hours" value="{{ request.form.get('machine_hours','') }}"></div>
      <div class="field"><label>Ставка, грн/год</label><input type="number" step="0.01" name="machine_rate_per_hour" value="{{ request.form.get('machine_rate_per_hour','') }}"></div>
    </div>
    <div class="section-title">Наладка та партія</div>
    <div class="form-grid" style="margin-bottom:14px;">
      <div class="field"><label>Наладка верстата на партію, грн</label><input type="number" step="0.01" name="setup_cost_total" value="{{ request.form.get('setup_cost_total','') }}"></div>
      <div class="field"><label>Кількість деталей</label><input type="number" step="1" name="quantity" value="{{ request.form.get('quantity','1') }}"></div>
      <div class="field"><label>Націнка, %</label><input type="number" step="1" name="margin_pct" value="{{ request.form.get('margin_pct','20') }}"></div>
    </div>
    <div class="section-title">Матеріал на партію (для плоских деталей під фрезерування)</div>
    <div class="form-grid" style="margin-bottom:14px;">
      <div class="field"><label>Площа деталі, м²</label><input type="number" step="0.001" name="part_area_m2" value="{{ request.form.get('part_area_m2') or (dxf_result.area_m2 if dxf_result else '') }}"></div>
      <div class="field"><label>Коефіцієнт розкрою, %</label><input type="number" step="1" name="nesting_pct" value="{{ request.form.get('nesting_pct','75') }}"></div>
      <div class="field"><label>Ширина заготовки, мм</label><input type="number" step="1" name="sheet_width_mm" value="{{ request.form.get('sheet_width_mm','1250') }}"></div>
      <div class="field"><label>Довжина заготовки, мм</label><input type="number" step="1" name="sheet_length_mm" value="{{ request.form.get('sheet_length_mm','2500') }}"></div>
    </div>
    <div class="field" style="margin-bottom:14px;">
      <label>Звірити з позицією на складі (необов'язково)</label>
      <select name="stock_item_id">
        <option value="">— не звіряти —</option>
        {% for wi in warehouse_items %}<option value="{{ wi['id'] }}" {{ 'selected' if request.form.get('stock_item_id')==wi['id']|string }}>{{ wi['name'] }} ({{ wi['qty_on_hand'] }} {{ wi['unit'] }} в наявності)</option>{% endfor %}
      </select>
    </div>
    <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top:4px;">
      <button class="btn btn-primary" type="submit" formaction="{{ url_for('cost_calculator') }}">🧮 Розрахувати вручну</button>
      <button class="btn btn-primary" type="submit" formaction="{{ url_for('calculator_ai_estimate') }}">🤖 Розрахувати через ШІ</button>
    </div>
    <p style="font-size:11px; color:var(--text-dim); margin:8px 0 0;">
      Можна поєднувати: заповни відомі поля вручну (напр. кількість і матеріал), а решту (час обробки,
      опис техпроцесу) хай порахує ШІ з опису і/або фото/DXF/PDF-файлу — заповнені тобою поля ШІ НІКОЛИ
      не змінює, навіть якщо сам порахував би інакше. «Розрахувати вручну» використовує лише поля вище
      (порожні = 0) і не звертається до ШІ та не потребує API-ключа. «Розрахувати через ШІ» потребує
      API-ключ Anthropic (див. «Налаштування») — опис може бути мінімальним чи взагалі порожнім, якщо
      заповнені поля чи фото/креслення дають достатньо контексту; якщо даних явно не вистачає, ШІ поставить
      одне уточнююче питання замість того, щоб вигадувати цифри з нуля. Це орієнтовна оцінка — обов'язково
      перевір цифри перед тим, як ставити їх у КП клієнту.
    </p>
  </form>

  <div>
    {% if result %}
    <div class="section-title" style="margin-top:0;"><span class="accent-dot"></span> Результат</div>
    {% if result.ai_operations_description %}
    <div class="card card-pad" style="margin-bottom:14px; border-color:var(--accent-2);">
      <div style="font-size:11px; color:var(--accent-2); font-weight:700; margin-bottom:6px;">🤖 ОЦІНКА ШІ — ПЕРЕВІР ПЕРЕД ВИКОРИСТАННЯМ</div>
      <div style="margin:0 0 12px;">{{ result.ai_operations_description | ops_html }}</div>
      <div style="font-size:11px; color:var(--text-dim);">
        Заготовка: {{ result.ai_inputs.weight_kg }} кг · Метал: {{ result.ai_inputs.material_price_per_kg }} грн/кг ·
        Токарна: {{ result.ai_inputs.turning_hours }} год · Фрезерна: {{ result.ai_inputs.milling_hours }} год ·
        Партія: {{ result.ai_inputs.quantity|int }} шт · Націнка: {{ result.ai_inputs.margin_pct }}%
      </div>
    </div>
    {% endif %}
    {% if result.ai_correction_token %}
    <div class="card card-pad" style="margin-bottom:14px;">
      <div style="font-weight:600; margin-bottom:8px; font-size:12.5px;">💬 Щось не так в оцінці ШІ? Напиши, що виправити:</div>
      <form method="post" action="{{ url_for('calculator_ai_estimate') }}" style="display:flex; gap:10px; align-items:flex-end; flex-wrap:wrap;">
        <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="ai_correction_token" value="{{ result.ai_correction_token }}">
        <div class="field" style="flex:1; min-width:240px;">
          <input type="text" name="ai_correction_message" placeholder="напр.: вага заготовки не 2, а 3.2 кг; додай свердління 4 отворів" required>
        </div>
        <button class="btn btn-ghost btn-sm" type="submit">Виправити</button>
      </form>
    </div>
    {% endif %}
    <div class="card card-pad">
      <table style="width:100%; font-size:13px;">
        <tr><td>Матеріал</td><td style="text-align:right;">{{ result.material_cost }} грн</td></tr>
        <tr><td>Токарна обробка</td><td style="text-align:right;">{{ result.turning_cost }} грн</td></tr>
        <tr><td>Фрезерна обробка</td><td style="text-align:right;">{{ result.milling_cost }} грн</td></tr>
        <tr><td>Додаткова обробка</td><td style="text-align:right;">{{ result.machine_cost }} грн</td></tr>
        {% if result.setup_cost_total %}<tr><td>Наладка (за од., {{ result.setup_cost_total }} грн на партію)</td><td style="text-align:right;">{{ result.setup_cost_per_unit }} грн</td></tr>{% endif %}
        <tr style="border-top:1px solid var(--border);"><td><b>Собівартість за од.</b></td><td style="text-align:right;"><b>{{ result.cost_per_unit }} грн</b></td></tr>
        <tr><td>Ціна за од. (+{{ result.margin_pct }}%)</td><td style="text-align:right; color:var(--accent-2);"><b>{{ result.price_per_unit }} грн</b></td></tr>
        <tr style="border-top:1px solid var(--border);"><td><b>Разом за {{ result.quantity }} шт.</b></td><td style="text-align:right; font-size:18px; color:var(--accent);"><b>{{ result.total_price }} грн</b></td></tr>
      </table>
      {% if result.material_calc %}
      <div class="section-title" style="font-size:13px;">Матеріал на партію</div>
      {% if result.material_calc.error %}
      <div style="color:var(--red); font-size:12.5px;">{{ result.material_calc.error }}</div>
      {% else %}
      <table style="width:100%; font-size:12.5px;">
        <tr><td>Площа листа</td><td style="text-align:right;">{{ result.material_calc.sheet_area_m2 }} м²</td></tr>
        <tr><td>Деталей на лист (з урахуванням розкрою)</td><td style="text-align:right;">{{ result.material_calc.parts_per_sheet }} шт.</td></tr>
        <tr><td><b>Листів потрібно на партію</b></td><td style="text-align:right;"><b>{{ result.material_calc.sheets_needed }} шт.</b></td></tr>
        <tr><td>Загальна площа матеріалу</td><td style="text-align:right;">{{ result.material_calc.total_material_area_m2 }} м²</td></tr>
        {% if result.material_calc.total_weight_kg %}<tr><td>Загальна вага партії</td><td style="text-align:right;">{{ result.material_calc.total_weight_kg }} кг</td></tr>{% endif %}
      </table>
      {% endif %}
      {% endif %}
      {% if result.stock_check %}
      <div style="margin-top:10px; padding:10px; border-radius:8px; background:var(--bg-soft); font-size:12.5px;">
        Склад «{{ result.stock_check.item_name }}»: наявно {{ result.stock_check.available }} {{ result.stock_check.unit }},
        потрібно {{ result.stock_check.needed }} {{ result.stock_check.unit }} —
        {% if result.stock_check.enough %}<span style="color:var(--green);">достатньо ✓</span>
        {% else %}<span style="color:var(--red);">НЕ ВИСТАЧАЄ, докупити {{ (result.stock_check.needed - result.stock_check.available) | round(1) }} {{ result.stock_check.unit }}</span>{% endif %}
      </div>
      {% endif %}
      <p style="font-size:11.5px; color:var(--text-dim); margin-bottom:8px; margin-top:14px;">Створити угоду з цим розрахунком — далі обереш клієнта на сторінці угоди.</p>
      <div style="display:flex; gap:8px; flex-wrap:wrap;">
        <a class="btn btn-primary btn-sm" href="{{ url_for('deal_new', prefill_title='Розрахунок з калькулятора', prefill_amount=result.total_price) }}">Створити угоду на основі розрахунку →</a>
        <form method="post" action="{{ url_for('calculator_quote_pdf') }}" target="_blank">
          <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
          <input type="hidden" name="weight_kg" value="{{ result.ai_inputs.weight_kg if result.ai_inputs else request.form.get('weight_kg', 0) }}">
          <input type="hidden" name="material_price_per_kg" value="{{ result.ai_inputs.material_price_per_kg if result.ai_inputs else request.form.get('material_price_per_kg', 0) }}">
          <input type="hidden" name="turning_hours" value="{{ result.ai_inputs.turning_hours if result.ai_inputs else request.form.get('turning_hours', 0) }}">
          <input type="hidden" name="turning_rate_per_hour" value="{{ result.ai_inputs.turning_rate_per_hour if result.ai_inputs else request.form.get('turning_rate_per_hour', 0) }}">
          <input type="hidden" name="milling_hours" value="{{ result.ai_inputs.milling_hours if result.ai_inputs else request.form.get('milling_hours', 0) }}">
          <input type="hidden" name="milling_rate_per_hour" value="{{ result.ai_inputs.milling_rate_per_hour if result.ai_inputs else request.form.get('milling_rate_per_hour', 0) }}">
          <input type="hidden" name="machine_hours" value="{{ result.ai_inputs.machine_hours if result.ai_inputs else request.form.get('machine_hours', 0) }}">
          <input type="hidden" name="machine_rate_per_hour" value="{{ result.ai_inputs.machine_rate_per_hour if result.ai_inputs else request.form.get('machine_rate_per_hour', 0) }}">
          <input type="hidden" name="setup_cost_total" value="{{ result.ai_inputs.setup_cost_total if result.ai_inputs else request.form.get('setup_cost_total', 0) }}">
          <input type="hidden" name="quantity" value="{{ result.ai_inputs.quantity if result.ai_inputs else request.form.get('quantity', 1) }}">
          <input type="hidden" name="margin_pct" value="{{ result.ai_inputs.margin_pct if result.ai_inputs else request.form.get('margin_pct', 20) }}">
          <input type="hidden" name="part_description" value="{{ ai_description or 'Розрахунок вартості механообробки (ручний ввід параметрів)' }}">
          <input type="hidden" name="operations_description" value="{{ result.ai_operations_description or '' }}">
          <button class="btn btn-ghost btn-sm" type="submit">📄 Завантажити як PDF</button>
        </form>
      </div>
    </div>
    {% else %}
    <div class="empty" style="padding:60px 20px;">Заповни параметри зліва і натисни «Розрахувати»</div>
    {% endif %}
  </div>
</div>
{% endblock %}
"""

SHIFT_LOG_LIST_HTML = """
{% extends "base.html" %}
{% block title %}Змінний журнал{% endblock %}
{% block heading %}Змінний журнал виробництва{% endblock %}
{% block subheading %}За період: виготовлено {{ totals['quantity_made'] }} шт · брак {{ totals['quantity_scrap'] }} шт{% endblock %}
{% block content %}
<div class="toolbar">
  <div class="filters">
    <form method="get" style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
      <label style="font-size:12px; color:var(--text-dim);">з <input type="date" name="date_from" value="{{ date_from }}" style="width:auto;"></label>
      <label style="font-size:12px; color:var(--text-dim);">по <input type="date" name="date_to" value="{{ date_to }}" style="width:auto;"></label>
      <select name="work_center_id" onchange="this.form.submit()" style="width:auto;">
        <option value="">Усі дільниці</option>
        {% for wc in work_centers %}<option value="{{ wc['id'] }}" {{ 'selected' if f_wc==wc['id'] }}>{{ wc['name'] }}</option>{% endfor %}
      </select>
      <select name="worker_user_id" onchange="this.form.submit()" style="width:auto;">
        <option value="">Усі робітники</option>
        {% for w in workers %}<option value="{{ w['id'] }}" {{ 'selected' if f_worker==w['id'] }}>{{ w['full_name'] }}</option>{% endfor %}
      </select>
      <button class="btn btn-sm" type="submit">Фільтр</button>
    </form>
  </div>
  <a class="btn btn-primary" href="{{ url_for('shift_log_new') }}">➕ Новий запис</a>
</div>
<div class="toolbar" style="margin-top:-6px;">
  <div class="filters">
    <form method="get" action="{{ url_for('shift_log_report_pdf') }}" target="_blank" style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
      <label style="font-size:12px; color:var(--text-dim);">Зведення за місяць <input type="month" name="month" value="{{ current_month }}" style="width:auto;"></label>
      <select name="worker_user_id" style="width:auto;">
        <option value="">Усі робітники</option>
        {% for w in workers %}<option value="{{ w['id'] }}" {{ 'selected' if f_worker==w['id'] }}>{{ w['full_name'] }}</option>{% endfor %}
      </select>
      <button class="btn btn-sm" type="submit">📄 Скачати PDF за місяць</button>
    </form>
  </div>
</div>
<div class="card table-wrap">
<table>
  <tr><th>Дата</th><th>Зміна</th><th>Дільниця</th><th>Робітник</th><th>Деталь</th><th>Виготовлено</th><th>Брак</th><th>Год. зміни</th><th>Налад., год</th><th>Дії</th></tr>
  {% for r in logs %}
  <tr>
    <td style="white-space:nowrap;">{{ r['work_date'] }}</td>
    <td>{{ {'day':'Денна','night':'Нічна'}.get(r['shift'], r['shift']) }}</td>
    <td>{{ r['work_center_name'] or '—' }}</td>
    <td>{{ r['worker_name'] or '—' }}</td>
    <td style="max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="{{ r['part_description'] }}">{{ r['part_description'] }}</td>
    <td>{{ r['quantity_made'] }}</td>
    <td>{% if r['quantity_scrap'] %}<span class="badge badge-high">{{ r['quantity_scrap'] }}</span>{% else %}0{% endif %}</td>
    <td>{{ r['shift_hours'] if r['shift_hours'] is not none else '—' }}</td>
    <td>{{ r['setup_hours'] if r['setup_hours'] is not none else '—' }}</td>
    <td style="white-space:nowrap;">
      {% if r['can_edit'] %}
      <a class="btn btn-ghost btn-sm" href="{{ url_for('shift_log_edit', log_id=r['id']) }}">✎</a>
      <form method="post" action="{{ url_for('shift_log_delete', log_id=r['id']) }}" style="display:inline;" onsubmit="return confirm('Видалити цей запис змінного журналу?');">
        <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <button class="btn btn-danger btn-sm" type="submit">🗑</button>
      </form>
      {% else %}
      <span style="color:var(--text-dim); font-size:12px;">—</span>
      {% endif %}
    </td>
  </tr>
  {% else %}
  <tr><td colspan="10" class="empty">Записів за обраний період немає</td></tr>
  {% endfor %}
</table>
</div>
{% endblock %}
"""

SHIFT_LOG_FORM_HTML = """
{% extends "base.html" %}
{% block title %}{{ 'Редагувати запис' if log else 'Новий запис змінного журналу' }}{% endblock %}
{% block heading %}{{ 'Редагувати запис' if log else 'Новий запис змінного журналу' }}{% endblock %}
{% block content %}
<form method="post" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
  <div class="form-grid">
    <div class="field"><label>Дата *</label><input type="date" name="work_date" required value="{{ log['work_date'] if log else today }}"></div>
    <div class="field">
      <label>Зміна</label>
      <select name="shift">
        <option value="day" {{ 'selected' if (log['shift'] if log else 'day')=='day' }}>Денна</option>
        <option value="night" {{ 'selected' if log and log['shift']=='night' }}>Нічна</option>
      </select>
    </div>
    <div class="field">
      <label>Дільниця{% if foreman_own_wc %} (ваша){% endif %}</label>
      <select name="work_center_id" {{ 'disabled' if foreman_own_wc }}>
        <option value="">— не обрано —</option>
        {% for wc in work_centers %}<option value="{{ wc['id'] }}" {{ 'selected' if (log and log['work_center_id']==wc['id']) or foreman_own_wc==wc['id'] }}>{{ wc['name'] }}</option>{% endfor %}
      </select>
      {% if foreman_own_wc %}<input type="hidden" name="work_center_id" value="{{ foreman_own_wc }}">{% endif %}
    </div>
    <div class="field">
      <label>Робітник</label>
      <select name="worker_user_id">
        {% for w in workers %}<option value="{{ w['id'] }}" {{ 'selected' if (log and log['worker_user_id']==w['id']) or (not log and w['id']==current_user['id']) }}>{{ w['full_name'] }}</option>{% endfor %}
      </select>
    </div>
    <div class="field form-row-full"><label>Деталь *</label><input type="text" name="part_description" required value="{{ log['part_description'] if log else '' }}"></div>
    <div class="field"><label>Виготовлено, шт *</label><input type="number" name="quantity_made" min="0" step="1" required value="{{ log['quantity_made'] if log else 0 }}"></div>
    <div class="field"><label>Брак, шт</label><input type="number" name="quantity_scrap" min="0" step="1" value="{{ log['quantity_scrap'] if log else 0 }}"></div>
    <div class="field form-row-full"><label>Причина браку</label><input type="text" name="scrap_reason" value="{{ log['scrap_reason'] if log else '' }}"></div>
    <div class="field"><label>Тривалість зміни, год</label><input type="number" step="0.1" min="0" name="shift_hours" value="{{ log['shift_hours'] if log and log['shift_hours'] is not none else '' }}"></div>
    <div class="field"><label>Години наладки</label><input type="number" step="0.1" min="0" name="setup_hours" value="{{ log['setup_hours'] if log and log['setup_hours'] is not none else '' }}"></div>
    <div class="field form-row-full"><label>Примітки</label><textarea name="notes">{{ log['notes'] if log else '' }}</textarea></div>
  </div>
  <div class="actions">
    <button class="btn btn-primary" type="submit">Зберегти</button>
    <a class="btn btn-ghost" href="{{ url_for('shift_log_list') }}">Скасувати</a>
  </div>
</form>
{% endblock %}
"""

CALCULATOR_HISTORY_HTML = """
{% extends "base.html" %}
{% block title %}Історія розрахунків{% endblock %}
{% block heading %}Історія розрахунків вартості{% endblock %}
{% block subheading %}Всього: {{ total }}{% if total_pages > 1 %} · сторінка {{ page }} з {{ total_pages }}{% endif %}{% endblock %}
{% block content %}
<div class="toolbar">
  <div class="filters">
    <form method="get" style="display:flex; gap:8px;">
      <input type="text" name="q" placeholder="Пошук за назвою деталі..." value="{{ search_q or '' }}" style="min-width:220px;">
      <select name="source" onchange="this.form.submit()">
        <option value="">Усі джерела</option>
        <option value="manual" {{ 'selected' if request.args.get('source')=='manual' }}>Ручний</option>
        <option value="ai" {{ 'selected' if request.args.get('source')=='ai' }}>ШІ</option>
      </select>
      <button class="btn btn-sm" type="submit">Знайти</button>
    </form>
  </div>
  <a class="btn" href="{{ url_for('export_calculations_xlsx', source=request.args.get('source',''), q=search_q or '') }}">⬇ Excel</a>
  <a class="btn btn-ghost" href="{{ url_for('cost_calculator') }}">← До калькулятора</a>
</div>
<div class="card table-wrap">
<table>
  <tr><th>Дата</th><th>Менеджер</th><th>Джерело</th><th>Деталь</th><th>К-сть</th><th>Собівартість/од</th><th>Ціна/од</th><th>Разом</th><th>Дії</th></tr>
  {% for r in rows %}
  <tr onclick="window.location='{{ url_for('calculator_history_detail', calc_id=r['id']) }}'" style="cursor:pointer;">
    <td style="white-space:nowrap;">{{ r['created_at'] }}</td>
    <td>{{ r['manager_name'] or '—' }}</td>
    <td>{% if r['source']=='ai' %}<span class="badge badge-low">🤖 ШІ</span>{% else %}<span class="badge">ручний</span>{% endif %}</td>
    <td style="max-width:360px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="{{ r['part_description'] or '' }}">{{ r['part_description'] or '—' }}</td>
    <td>{{ r['quantity'] }}</td>
    <td>{{ '%.2f'|format(r['cost_per_unit'] or 0) }}</td>
    <td>{{ '%.2f'|format(r['price_per_unit'] or 0) }}</td>
    <td><b>{{ '%.2f'|format(r['total_price'] or 0) }}</b></td>
    <td style="white-space:nowrap;" onclick="event.stopPropagation();">
      <a class="btn btn-ghost btn-sm" href="{{ url_for('calculator_history_detail', calc_id=r['id']) }}">👁 Деталі</a>
      <form method="post" action="{{ url_for('calculator_history_delete', calc_id=r['id']) }}" style="display:inline;" onsubmit="return confirm('Видалити цей розрахунок з історії? Дію не можна скасувати.');">
        <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="page" value="{{ page }}">
        <input type="hidden" name="source" value="{{ request.args.get('source','') }}">
        <input type="hidden" name="q" value="{{ search_q or '' }}">
        <button class="btn btn-danger btn-sm" type="submit">🗑</button>
      </form>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="9" class="empty">Розрахунків ще немає</td></tr>
  {% endfor %}
</table>
</div>
{% if total_pages > 1 %}
<div style="display:flex; gap:8px; justify-content:center; margin-top:16px;">
  {% set qs = ('&source='+request.args.get('source') if request.args.get('source') else '') + ('&q='+search_q if search_q else '') %}
  {% if page > 1 %}<a class="btn btn-sm" href="?page={{ page-1 }}{{ qs }}">← Попередня</a>{% endif %}
  <span style="padding:6px 10px; color:var(--text-dim); font-size:12.5px;">Сторінка {{ page }} з {{ total_pages }}</span>
  {% if page < total_pages %}<a class="btn btn-sm" href="?page={{ page+1 }}{{ qs }}">Наступна →</a>{% endif %}
</div>
{% endif %}
{% endblock %}
"""

CALCULATOR_HISTORY_DETAIL_HTML = """
{% extends "base.html" %}
{% block title %}Розрахунок №{{ r['id'] }}{% endblock %}
{% block heading %}Розрахунок №{{ r['id'] }}{% endblock %}
{% block subheading %}{{ r['created_at'] }} · {{ r['manager_name'] or '—' }} · {% if r['source']=='ai' %}🤖 через ШІ{% else %}ручний розрахунок{% endif %}{% endblock %}
{% block content %}
<div class="detail-header">
  <div style="max-width:720px;">
    <div style="font-weight:600; font-size:15px; margin-bottom:4px;">{{ r['part_description'] or '—' }}</div>
  </div>
  <div style="display:flex; gap:8px; flex-wrap:wrap;">
    <a class="btn btn-ghost" href="{{ url_for('calculator_history') }}">← До історії</a>
    <a class="btn" href="{{ url_for('calculator_history_quote_pdf', calc_id=r['id']) }}" target="_blank">📄 Скачати PDF</a>
    <a class="btn btn-primary" href="{{ url_for('deal_new', prefill_title=r['part_description'], prefill_amount=r['total_price']) }}">➕ Створити угоду</a>
    <form method="post" action="{{ url_for('calculator_history_delete', calc_id=r['id']) }}" onsubmit="return confirm('Видалити цей розрахунок з історії? Дію не можна скасувати.');">
      <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <button class="btn btn-danger" type="submit">🗑 Видалити</button>
    </form>
  </div>
</div>

<div class="grid grid-2">
  <div>
    <div class="section-title"><span class="accent-dot"></span> Вхідні параметри</div>
    <div class="card table-wrap">
      <table>
        <tr><th>Параметр</th><th>Значення</th></tr>
        <tr><td>Кількість у партії</td><td>{{ r['quantity'] }} шт</td></tr>
        <tr><td>Вага заготовки (на 1 деталь)</td><td>{{ r['weight_kg'] if r['weight_kg'] is not none else '—' }} кг</td></tr>
        <tr><td>Ціна матеріалу</td><td>{{ r['material_price_per_kg'] if r['material_price_per_kg'] is not none else '—' }} грн/кг</td></tr>
        <tr><td>Токарна обробка</td><td>{{ r['turning_hours'] if r['turning_hours'] is not none else '—' }} год × {{ r['turning_rate_per_hour'] if r['turning_rate_per_hour'] is not none else '—' }} грн/год</td></tr>
        <tr><td>Фрезерна/свердлильна обробка</td><td>{{ r['milling_hours'] if r['milling_hours'] is not none else '—' }} год × {{ r['milling_rate_per_hour'] if r['milling_rate_per_hour'] is not none else '—' }} грн/год</td></tr>
        <tr><td>Додаткова обробка (шліфування/ТО/покриття)</td><td>{{ r['machine_hours'] if r['machine_hours'] is not none else '—' }} год × {{ r['machine_rate_per_hour'] if r['machine_rate_per_hour'] is not none else '—' }} грн/год</td></tr>
        <tr><td>Наладка (на всю партію)</td><td>{{ '%.2f'|format(r['setup_cost_total'] or 0) }} грн</td></tr>
        <tr><td>Націнка</td><td>{{ r['margin_pct'] if r['margin_pct'] is not none else '—' }}%</td></tr>
      </table>
    </div>
  </div>
  <div>
    <div class="section-title"><span class="accent-dot"></span> Результат розрахунку</div>
    <div class="card table-wrap">
      <table>
        <tr><th>Показник</th><th>Значення</th></tr>
        <tr><td>Собівартість за одиницю</td><td><b>{{ '%.2f'|format(r['cost_per_unit'] or 0) }} грн</b></td></tr>
        <tr><td>Ціна за одиницю (з націнкою)</td><td><b>{{ '%.2f'|format(r['price_per_unit'] or 0) }} грн</b></td></tr>
        <tr><td>Разом за партію ({{ r['quantity'] }} шт)</td><td><b style="color:var(--accent-2); font-size:15px;">{{ '%.2f'|format(r['total_price'] or 0) }} грн</b></td></tr>
      </table>
    </div>
  </div>
</div>

<div class="section-title"><span class="accent-dot"></span> Технологічний опис / маршрут виготовлення</div>
<div class="card card-pad">
  {% if r['operations_description'] %}
  {{ r['operations_description'] | ops_html }}
  {% else %}
  <div class="empty">Опис техпроцесу не збережено для цього розрахунку</div>
  {% endif %}
</div>
{% endblock %}
"""

DEAL_FORM_HTML = """
{% extends "base.html" %}
{% block title %}{{ 'Редагувати угоду' if deal else 'Нова угода' }}{% endblock %}
{% block heading %}{{ 'Редагувати угоду' if deal else 'Нова угода' }}{% endblock %}
{% block content %}
<form method="post" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
  <div class="form-grid">
    <div class="field form-row-full"><label>Назва угоди *</label><input type="text" name="title" required value="{{ deal['title'] if deal else (prefill_title or '') }}"></div>
    <div class="field">
      <label>Клієнт *</label>
      <select name="client_id" required>
        {% for c in clients %}<option value="{{ c['id'] }}" {{ 'selected' if (deal and deal['client_id']==c['id']) or (preselect_client==c['id']) }}>{{ c['name'] }}</option>{% endfor %}
      </select>
    </div>
    <div class="field">
      <label>Обладнання / техпроцес</label>
      <select name="machine_id">
        <option value="">— не обрано —</option>
        {% for m in machines %}<option value="{{ m['id'] }}" {{ 'selected' if deal and deal['machine_id']==m['id'] }}>{{ m['category'] }} — {{ m['model'] }}</option>{% endfor %}
      </select>
    </div>
    <div class="field">
      <label>Стадія</label>
      <select name="stage">
        {% for key,label,color in STAGES %}<option value="{{ key }}" {{ 'selected' if deal and deal['stage']==key }}>{{ label }}</option>{% endfor %}
        <option value="{{ LOST_STAGE }}" {{ 'selected' if deal and deal['stage']==LOST_STAGE }}>{{ STAGE_LABEL[LOST_STAGE] }}</option>
      </select>
    </div>
    <div class="field"><label>Сума</label><input type="number" step="0.01" name="amount" value="{{ deal['amount'] if deal else (prefill_amount or 0) }}"></div>
    <div class="field">
      <label>Валюта</label>
      <select name="currency">{% for cur in CURRENCIES %}<option {{ 'selected' if deal and deal['currency']==cur }}>{{ cur }}</option>{% endfor %}</select>
    </div>
    <div class="field"><label>Передоплата, %</label><input type="number" name="prepayment_percent" value="{{ deal['prepayment_percent'] if deal else 30 }}"></div>
    <div class="field"><label>Ймовірність, %</label><input type="number" name="probability" min="0" max="100" value="{{ deal['probability'] if deal else 20 }}"></div>
    <div class="field">
      <label>Менеджер</label>
      <select name="manager_id">{% for m in managers %}<option value="{{ m['id'] }}" {{ 'selected' if deal and deal['manager_id']==m['id'] }}>{{ m['full_name'] }}</option>{% endfor %}</select>
    </div>
    <div class="field"><label>Очікуване закриття</label><input type="date" name="expected_close" value="{{ deal['expected_close'] if deal else '' }}"></div>
    <div class="field">
      <label>Пріоритет</label>
      <select name="priority">{% for k,v in PRIORITIES.items() %}<option value="{{ k }}" {{ 'selected' if deal and deal['priority']==k }}>{{ v }}</option>{% endfor %}</select>
    </div>
    <div class="field"><label>Конкурент</label><input type="text" name="competitor" value="{{ deal['competitor'] if deal else '' }}"></div>
    <div class="field form-row-full"><label>Технічні вимоги / специфікація</label><textarea name="tech_requirements">{{ deal['tech_requirements'] if deal else '' }}</textarea></div>
  </div>
  <div class="actions">
    <button class="btn btn-primary" type="submit">Зберегти</button>
    <a class="btn btn-ghost" href="{{ url_for('deals_board') }}">Скасувати</a>
  </div>
</form>
{% endblock %}
"""

DEAL_SELECT_MACHINE_HTML = """
{% extends "base.html" %}
{% block title %}Оберіть верстат{% endblock %}
{% block heading %}Оберіть верстат / техпроцес{% endblock %}
{% block subheading %}{{ deal['title'] }}{% endblock %}
{% block content %}
<div class="card card-pad" style="max-width:560px;">
  <p style="color:var(--text-dim); font-size:13px; margin-top:0;">
    Щоб завести виробниче замовлення й одразу розписати завдання по цехах,
    потрібно знати, за якою маршрутною картою (типом верстата) виготовляти
    деталь — ця угода ще без обраного верстата. Оберіть найближчий за
    технологією, і замовлення піде в цех автоматично.
  </p>
  <form method="post">
    <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
    <input type="hidden" name="next_stage" value="{{ next_stage }}">
    <div class="field">
      <label>Верстат / техпроцес *</label>
      <select name="machine_id" required>
        <option value="">— оберіть —</option>
        {% for m in machines %}<option value="{{ m['id'] }}">{{ m['category'] }} — {{ m['model'] }}</option>{% endfor %}
      </select>
    </div>
    <div class="actions">
      <button class="btn btn-primary" type="submit">Підтвердити і завести в цех</button>
      <a class="btn btn-ghost" href="{{ url_for('deal_detail', deal_id=deal['id']) }}">Скасувати</a>
    </div>
  </form>
</div>
{% endblock %}
"""

DEAL_DETAIL_HTML = """
{% extends "base.html" %}
{% block title %}{{ deal['title'] }}{% endblock %}
{% block heading %}{{ deal['title'] }}{% endblock %}
{% block subheading %}<a href="{{ url_for('client_detail', client_id=deal['client_id']) }}">{{ deal['client_name'] }}</a>{% endblock %}
{% block content %}
<div class="detail-header">
  <div>
    <span class="pill" style="background:{{ STAGE_COLOR[deal['stage']] }};">{{ STAGE_LABEL[deal['stage']] }}</span>
    <span class="badge badge-{{ deal['priority'] }}">{{ PRIORITIES[deal['priority']] }} пріоритет</span>
    <div class="stat-row">
      <div class="stat">Сума<b>{{ deal['amount'] | money(deal['currency']) }}</b></div>
      <div class="stat">Передоплата<b>{{ deal['prepayment_percent'] }}%</b></div>
      <div class="stat">Ймовірність<b>{{ deal['probability'] }}%</b></div>
      <div class="stat">Менеджер<b>{{ deal['manager_name'] or '—' }}</b></div>
      <div class="stat">Очікуване закриття<b>{{ deal['expected_close'] or '—' }}</b></div>
      <div class="stat">Обладнання<b>{{ deal['machine_name'] or '—' }}</b></div>
    </div>
    <div class="progress-bar"><div style="width:{{ deal['probability'] }}%;"></div></div>
  </div>
  <div style="display:flex; gap:8px;">
    <a class="btn" href="{{ url_for('deal_proposal_pdf', deal_id=deal['id']) }}" target="_blank">📄 КП (PDF)</a>
    <a class="btn" href="{{ url_for('deal_edit', deal_id=deal['id']) }}">Редагувати</a>
    <form method="post" action="{{ url_for('deal_delete', deal_id=deal['id']) }}" onsubmit="return confirm('Видалити угоду безповоротно?');"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <button class="btn btn-danger" type="submit">Видалити</button>
    </form>
  </div>
</div>

<div class="section-title"><span class="accent-dot"></span> Файли (креслення, договір, специфікація)</div>
<div class="card card-pad">
  <div style="display:flex; flex-wrap:wrap; gap:10px; margin-bottom:14px;">
    {% for a in attachments %}
    <div style="border:1px solid var(--border); border-radius:8px; padding:10px; width:220px;">
      <div style="font-size:12.5px; word-break:break-all; margin-bottom:8px;">📎 {{ a['original_name'] }}</div>
      <div style="font-size:11px; color:var(--text-dim); margin-bottom:8px;">{{ (a['size_bytes']/1024)|round(1) }} КБ · {{ a['uploader_name'] or '' }}</div>
      <div style="display:flex; gap:6px;">
        <a class="btn btn-sm" href="{{ url_for('attachment_download', attachment_id=a['id']) }}">Завантажити</a>
        <form method="post" action="{{ url_for('attachment_delete', attachment_id=a['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm btn-danger" type="submit">✕</button></form>
      </div>
    </div>
    {% else %}
    <div class="empty">Файлів ще немає</div>
    {% endfor %}
  </div>
  <form method="post" action="{{ url_for('attachment_upload', entity_type='deal', entity_id=deal['id']) }}" enctype="multipart/form-data"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
    <input type="file" name="file" required>
    <button class="btn btn-sm btn-primary" type="submit">+ Додати файл (DXF, STEP, договір, фото)</button>
  </form>
</div>

<div class="section-title"><span class="accent-dot"></span> Оплати</div>
<div class="card card-pad">
  <div class="stat-row" style="margin-bottom:14px;">
    <div class="stat">Сплачено<b style="color:var(--green);">{{ paid_total | money(deal['currency']) }}</b></div>
    <div class="stat">Залишок<b style="color:{{ 'var(--red)' if balance_due > 0 else 'var(--green)' }};">{{ balance_due | money(deal['currency']) }}</b></div>
  </div>
  <div class="table-wrap">
    <table>
      <tr><th>Тип</th><th>Сума</th><th>Термін</th><th>Статус</th><th></th></tr>
      {% for p in payments %}
      <tr>
        <td>{{ dict(PAYMENT_KINDS).get(p['kind'], p['kind']) }}</td>
        <td>{{ p['amount'] | money(p['currency']) }}</td>
        <td>{{ p['due_date'] or '—' }}</td>
        <td><span class="badge badge-{{ 'low' if p['status']=='paid' else 'medium' }}">{{ 'Оплачено' if p['status']=='paid' else 'Очікується' }}</span></td>
        <td style="display:flex; gap:6px;">
          <form method="post" action="{{ url_for('payment_toggle', payment_id=p['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm" type="submit">{{ 'Скасувати оплату' if p['status']=='paid' else 'Позначити оплаченим' }}</button></form>
          <form method="post" action="{{ url_for('payment_delete', payment_id=p['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm btn-danger" type="submit">✕</button></form>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="5" class="empty">Платежів ще немає</td></tr>
      {% endfor %}
    </table>
  </div>
  <form method="post" action="{{ url_for('payment_add', deal_id=deal['id']) }}" style="display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; align-items:flex-end;"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
    <div class="field"><label>Тип</label><select name="kind">{% for k,v in PAYMENT_KINDS %}<option value="{{ k }}">{{ v }}</option>{% endfor %}</select></div>
    <div class="field"><label>Сума</label><input type="number" step="0.01" name="amount" style="width:130px;" required></div>
    <div class="field"><label>Термін</label><input type="date" name="due_date" style="width:150px;"></div>
    <label class="checkline" style="margin-bottom:9px;"><input type="checkbox" name="mark_paid" value="1" style="width:auto;"> одразу оплачено</label>
    <button class="btn btn-sm btn-primary" type="submit">+ Додати платіж</button>
  </form>
</div>

<div class="section-title"><span class="accent-dot"></span> Змінити стадію</div>
<div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px;">
  {% for key,label,color in STAGES %}
  <form method="post" action="{{ url_for('deal_move', deal_id=deal['id'], stage=key) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
    <button class="btn btn-sm" type="submit" style="{{ 'border-color: ' + color + '; color:' + color if deal['stage']==key }}">{{ label }}</button>
  </form>
  {% endfor %}
  <form method="post" action="{{ url_for('deal_move', deal_id=deal['id'], stage=LOST_STAGE) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
    <button class="btn btn-sm btn-danger" type="submit">{{ STAGE_LABEL[LOST_STAGE] }}</button>
  </form>
</div>

{% if deal['tech_requirements'] %}
<div class="section-title"><span class="accent-dot"></span> Технічні вимоги</div>
<div class="card card-pad"><p style="margin:0; white-space:pre-wrap;">{{ deal['tech_requirements'] }}</p></div>
{% endif %}

<div class="grid grid-2" style="margin-top:20px;">
  <div>
    <div class="section-title"><span class="accent-dot"></span> Історія по угоді</div>
    <div class="card card-pad">
      <form method="post" action="{{ url_for('activity_add', deal_id=deal['id']) }}" style="margin-bottom:16px; display:flex; gap:8px;"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <select name="type" style="max-width:140px;">
          <option value="call">Дзвінок</option>
          <option value="meeting">Зустріч</option>
          <option value="email">Лист</option>
          <option value="visit">Виїзд</option>
          <option value="note">Нотатка</option>
        </select>
        <input type="text" name="text" placeholder="Додати запис в історію..." required>
        <button class="btn btn-sm btn-primary" type="submit">Додати</button>
      </form>
      <div class="timeline">
        {% for a in activities %}
        <div class="timeline-item">
          <div class="when">{{ a['created_at'] }} · {{ a['manager_name'] or '' }}</div>
          <div>{{ a['text'] }}</div>
        </div>
        {% else %}
        <div class="empty">Історія поки порожня</div>
        {% endfor %}
      </div>
    </div>
  </div>
  <div>
    <div class="section-title"><span class="accent-dot"></span> Задачі по угоді</div>
    <div class="card card-pad">
      <form method="post" action="{{ url_for('task_add') }}" style="margin-bottom:14px;"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="deal_id" value="{{ deal['id'] }}">
        <input type="hidden" name="client_id" value="{{ deal['client_id'] }}">
        <div class="field" style="margin-bottom:8px;"><input type="text" name="title" placeholder="Нова задача" required></div>
        <div style="display:flex; gap:8px;">
          <input type="date" name="due_date" style="max-width:150px;">
          <button class="btn btn-sm" type="submit">+ Додати</button>
        </div>
      </form>
      {% for t in tasks %}
      <div class="checkline task-row {{ 'done' if t['done'] }}" style="padding:6px 0; border-bottom:1px solid var(--border);">
        <form method="post" action="{{ url_for('task_toggle', task_id=t['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm" type="submit">{{ '↺' if t['done'] else '✓' }}</button></form>
        <div>
          <span class="task-title">{{ t['title'] }}</span>
          <div style="font-size:11.5px; color:var(--text-dim);">{{ t['due_date'] or '' }}</div>
        </div>
      </div>
      {% else %}
      <div class="empty">Задач немає</div>
      {% endfor %}
    </div>
  </div>
</div>
{% endblock %}
"""

CALENDAR_HTML = """
{% extends "base.html" %}
{% block title %}Календар{% endblock %}
{% block heading %}{{ month_name }} {{ year }}{% endblock %}
{% block subheading %}Календар задач{% endblock %}
{% block content %}
<div class="toolbar">
  <div style="display:flex; gap:8px;">
    <a class="btn btn-sm" href="{{ url_for('calendar_view', year=prev_year, month=prev_month) }}">← Попередній</a>
    <a class="btn btn-sm" href="{{ url_for('calendar_view') }}">Сьогодні</a>
    <a class="btn btn-sm" href="{{ url_for('calendar_view', year=next_year, month=next_month) }}">Наступний →</a>
  </div>
  <a class="btn" href="{{ url_for('tasks_list') }}">☰ Список задач</a>
</div>
<div class="card" style="overflow-x:auto;">
  <table style="width:100%; table-layout:fixed;">
    <tr>
      {% for d in ['Пн','Вт','Ср','Чт','Пт','Сб','Нд'] %}
      <th style="text-align:center; padding:10px; border-bottom:1px solid var(--border); font-size:11.5px;">{{ d }}</th>
      {% endfor %}
    </tr>
    {% for week in weeks %}
    <tr>
      {% for day in week %}
      <td style="vertical-align:top; height:110px; padding:6px; border:1px solid var(--border); {{ 'background:rgba(255,122,26,.08);' if day==today_day }}">
        {% if day %}
        <div style="font-size:12px; color:{{ 'var(--accent)' if day==today_day else 'var(--text-dim)' }}; font-weight:{{ '700' if day==today_day else '400' }}; margin-bottom:4px;">{{ day }}</div>
        {% for t in tasks_by_day.get(day, [])[:3] %}
        <div style="font-size:10.5px; padding:2px 5px; margin-bottom:2px; border-radius:4px; background:{{ 'rgba(63,174,92,.15)' if t['done'] else 'rgba(79,168,224,.15)' }}; color:{{ 'var(--green)' if t['done'] else 'var(--accent-2)' }}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="{{ t['title'] }}{{ ' — ' + t['client_name'] if t['client_name'] }}">
          {{ '✓ ' if t['done'] }}{{ t['title'] }}
        </div>
        {% endfor %}
        {% if tasks_by_day.get(day, [])|length > 3 %}
        <div style="font-size:10px; color:var(--text-dim);">+{{ tasks_by_day.get(day)|length - 3 }} ще</div>
        {% endif %}
        {% endif %}
      </td>
      {% endfor %}
    </tr>
    {% endfor %}
  </table>
</div>
{% endblock %}
"""

TASKS_HTML = """
{% extends "base.html" %}
{% block title %}Задачі{% endblock %}
{% block heading %}Задачі{% endblock %}
{% block subheading %}Активні: {{ tasks|selectattr('done','equalto',0)|list|length }}{% endblock %}
{% block content %}
<div class="grid grid-2">
  <div>
    <div class="section-title"><span class="accent-dot"></span> Список задач</div>
    <div class="card table-wrap">
      <table>
        <tr><th></th><th>Задача</th><th>Клієнт / Угода</th><th>Тип</th><th>Термін</th><th>Менеджер</th><th></th></tr>
        {% for t in tasks %}
        <tr class="{{ 'done' if t['done'] }}">
          <td><form method="post" action="{{ url_for('task_toggle', task_id=t['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm" type="submit">{{ '↺' if t['done'] else '✓' }}</button></form></td>
          <td class="task-title" style="{{ 'text-decoration:line-through;color:var(--text-dim);' if t['done'] }}">{{ t['title'] }}</td>
          <td>{% if t['deal_id'] %}<a href="{{ url_for('deal_detail', deal_id=t['deal_id']) }}">{{ t['deal_title'] }}</a>{% else %}{{ t['client_name'] or '—' }}{% endif %}</td>
          <td><span class="tag">{{ dict(TASK_TYPES).get(t['type'], t['type']) }}</span></td>
          <td style="color:{{ 'var(--red)' if t['due_date'] and t['due_date'] < today and not t['done'] else 'var(--text)' }}">{{ t['due_date'] or '—' }}</td>
          <td>{{ t['manager_name'] or '—' }}</td>
          <td><form method="post" action="{{ url_for('task_delete', task_id=t['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm btn-danger" type="submit">✕</button></form></td>
        </tr>
        {% else %}
        <tr><td colspan="7" class="empty">Задач немає</td></tr>
        {% endfor %}
      </table>
    </div>
  </div>
  <div>
    <div class="section-title"><span class="accent-dot"></span> Нова задача</div>
    <form method="post" action="{{ url_for('task_add') }}" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <div class="field" style="margin-bottom:10px;"><label>Назва *</label><input type="text" name="title" required></div>
      <div class="field" style="margin-bottom:10px;">
        <label>Клієнт</label>
        <select name="client_id">
          <option value="">—</option>
          {% for c in clients %}<option value="{{ c['id'] }}">{{ c['name'] }}</option>{% endfor %}
        </select>
      </div>
      <div class="field" style="margin-bottom:10px;">
        <label>Тип</label>
        <select name="type">{% for k,v in TASK_TYPES %}<option value="{{ k }}">{{ v }}</option>{% endfor %}</select>
      </div>
      <div class="field" style="margin-bottom:10px;"><label>Термін</label><input type="date" name="due_date"></div>
      <div class="field" style="margin-bottom:10px;"><label>Опис</label><textarea name="description"></textarea></div>
      <button class="btn btn-primary" type="submit">Додати задачу</button>
    </form>
  </div>
</div>
{% endblock %}
"""

MACHINES_HTML = """
{% extends "base.html" %}
{% block title %}Наше обладнання{% endblock %}
{% block heading %}Наше обладнання{% endblock %}
{% block subheading %}Технічні характеристики та можливості власного парку{% endblock %}
{% block content %}
<div class="toolbar">
  <div></div>
  <a class="btn btn-primary" href="{{ url_for('machine_new') }}">+ Додати обладнання</a>
</div>
<div class="grid grid-3">
  {% for m in machines %}
  <div class="card card-pad">
    <div class="tag">{{ m['category'] }}</div>
    <h3 style="margin:8px 0 4px;">{{ m['model'] }}</h3>
    <p style="color:var(--text-dim); font-size:12.5px; min-height:34px;">{{ m['description'] or '' }}</p>
    <div class="stat-row" style="gap:14px;">
      <div class="stat">Потужність<b>{{ m['spindle_power'] or '—' }}</b></div>
      <div class="stat">Робоча зона<b>{{ m['work_area'] or '—' }}</b></div>
      <div class="stat">Точність<b>{{ m['accuracy'] or '—' }}</b></div>
    </div>
    <div style="display:flex; justify-content:space-between; align-items:center; margin-top:14px;">
      <span class="badge badge-{{ 'low' if m['in_stock'] else 'medium' }}">{{ 'в роботі' if m['in_stock'] else 'на плановому ТО' }}</span>
    </div>
    <div style="margin-top:12px; display:flex; gap:8px;">
      <a class="btn btn-sm" href="{{ url_for('machine_edit', machine_id=m['id']) }}">Редагувати</a>
      <form method="post" action="{{ url_for('machine_delete', machine_id=m['id']) }}" onsubmit="return confirm('Видалити обладнання зі списку?');"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <button class="btn btn-sm btn-danger" type="submit">✕</button>
      </form>
    </div>
  </div>
  {% else %}
  <div class="empty">Каталог порожній</div>
  {% endfor %}
</div>
{% endblock %}
"""

MACHINE_FORM_HTML = """
{% extends "base.html" %}
{% block title %}{{ 'Редагувати обладнання' if machine else 'Нове обладнання' }}{% endblock %}
{% block heading %}{{ 'Редагувати обладнання' if machine else 'Нове обладнання' }}{% endblock %}
{% block content %}
<form method="post" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
  <div class="form-grid">
    <div class="field">
      <label>Категорія *</label>
      <select name="category" required>{% for c in MACHINE_CATEGORIES %}<option {{ 'selected' if machine and machine['category']==c }}>{{ c }}</option>{% endfor %}</select>
    </div>
    <div class="field"><label>Модель *</label><input type="text" name="model" required value="{{ machine['model'] if machine else '' }}"></div>
    <div class="field form-row-full"><label>Опис</label><textarea name="description">{{ machine['description'] if machine else '' }}</textarea></div>
    <div class="field"><label>Потужність шпинделя</label><input type="text" name="spindle_power" value="{{ machine['spindle_power'] if machine else '' }}"></div>
    <div class="field"><label>Робоча зона</label><input type="text" name="work_area" value="{{ machine['work_area'] if machine else '' }}"></div>
    <div class="field"><label>Точність</label><input type="text" name="accuracy" value="{{ machine['accuracy'] if machine else '' }}"></div>
    <div class="field"><label>Балансова вартість (довідково)</label><input type="number" step="0.01" name="price" value="{{ machine['price'] if machine else 0 }}"></div>
    <div class="field">
      <label>Валюта</label>
      <select name="currency">{% for cur in CURRENCIES %}<option {{ 'selected' if machine and machine['currency']==cur }}>{{ cur }}</option>{% endfor %}</select>
    </div>
    <div class="field"><label>Типовий термін виконання замовлення, дні</label><input type="number" name="lead_time_days" value="{{ machine['lead_time_days'] if machine else 30 }}"></div>
    <div class="field">
      <label>Статус</label>
      <select name="in_stock">
        <option value="1" {{ 'selected' if machine and machine['in_stock'] }}>В роботі</option>
        <option value="0" {{ 'selected' if machine and not machine['in_stock'] }}>На плановому ТО</option>
      </select>
    </div>
  </div>
  <div class="actions">
    <button class="btn btn-primary" type="submit">Зберегти</button>
    <a class="btn btn-ghost" href="{{ url_for('machines_list') }}">Скасувати</a>
  </div>
</form>
{% endblock %}
"""

SERVICE_HTML = """
{% extends "base.html" %}
{% block title %}Сервіс{% endblock %}
{% block heading %}Рекламації та доробки клієнтів{% endblock %}
{% block subheading %}Активні заявки: {{ tickets|rejectattr('status','equalto','done')|list|length }}{% endblock %}
{% block content %}
<div class="grid grid-2">
  <div>
    <div class="section-title"><span class="accent-dot"></span> Заявки</div>
    <div class="card table-wrap">
      <table>
        <tr><th>Клієнт</th><th>Проблема</th><th>Пріоритет</th><th>Статус</th><th>Інженер</th><th></th><th></th></tr>
        {% for t in tickets %}
        <tr>
          <td><a href="{{ url_for('client_detail', client_id=t['client_id']) }}">{{ t['client_name'] }}</a></td>
          <td><a href="{{ url_for('service_detail', ticket_id=t['id']) }}">{{ t['issue'] }}</a></td>
          <td><span class="badge badge-{{ t['priority'] }}">{{ PRIORITIES[t['priority']] }}</span></td>
          <td>
            <form method="post" action="{{ url_for('service_update_status', ticket_id=t['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
              <select name="status" onchange="this.form.submit()">
                {% for k,v in SERVICE_STATUSES %}<option value="{{ k }}" {{ 'selected' if t['status']==k }}>{{ v }}</option>{% endfor %}
              </select>
            </form>
          </td>
          <td>{{ t['engineer'] or '—' }}</td>
          <td><a class="btn btn-sm" href="{{ url_for('service_detail', ticket_id=t['id']) }}">Відкрити</a></td>
          <td><form method="post" action="{{ url_for('service_delete', ticket_id=t['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm btn-danger" type="submit">✕</button></form></td>
        </tr>
        {% else %}
        <tr><td colspan="7" class="empty">Заявок немає</td></tr>
        {% endfor %}
      </table>
    </div>
  </div>
  <div>
    <div class="section-title"><span class="accent-dot"></span> Нова заявка</div>
    <form method="post" action="{{ url_for('service_add') }}" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <div class="field" style="margin-bottom:10px;">
        <label>Клієнт *</label>
        <select name="client_id" required>{% for c in clients %}<option value="{{ c['id'] }}">{{ c['name'] }}</option>{% endfor %}</select>
      </div>
      <div class="field" style="margin-bottom:10px;">
        <label>Верстат</label>
        <select name="machine_id"><option value="">—</option>{% for m in machines %}<option value="{{ m['id'] }}">{{ m['model'] }}</option>{% endfor %}</select>
      </div>
      <div class="field" style="margin-bottom:10px;"><label>Опис проблеми *</label><textarea name="issue" required></textarea></div>
      <div class="field" style="margin-bottom:10px;">
        <label>Пріоритет</label>
        <select name="priority">{% for k,v in PRIORITIES.items() %}<option value="{{ k }}">{{ v }}</option>{% endfor %}</select>
      </div>
      <div class="field" style="margin-bottom:10px;"><label>Інженер</label><input type="text" name="engineer"></div>
      <button class="btn btn-primary" type="submit">Створити заявку</button>
    </form>
  </div>
</div>
{% endblock %}
"""

PRODUCTION_HTML = """
{% extends "base.html" %}
{% block title %}Виробництво{% endblock %}
{% block heading %}Виробничі замовлення{% endblock %}
{% block subheading %}Автоматично створюються при отриманні передоплати{% endblock %}
{% block content %}
<div class="toolbar">
  <div class="filters">
    <a class="btn btn-sm" href="{{ url_for('work_centers_list') }}">⚙ Робочі центри та завантаження</a>
  </div>
</div>
<div class="card table-wrap">
  <table>
    <tr><th>№</th><th>Угода / клієнт</th><th>Верстат</th><th>Статус</th><th>Прогрес операцій</th><th>Плановий фініш</th></tr>
    {% for o in orders %}
    <tr onclick="window.location='{{ url_for('production_detail', order_id=o['id']) }}'" style="cursor:pointer;">
      <td>#{{ o['id'] }}</td>
      <td>{{ o['client_name'] or '—' }}<br><span style="color:var(--text-dim); font-size:11.5px;">{{ o['deal_title'] or '' }}</span></td>
      <td>{{ o['machine_name'] or '—' }}</td>
      <td><span class="badge badge-{{ 'low' if o['status']=='done' else 'medium' }}">{{ dict(PRODUCTION_ORDER_STATUSES)[o['status']] }}</span></td>
      <td>
        <div class="progress-bar" style="width:140px;"><div style="width:{{ o['progress'] }}%;"></div></div>
        <span style="font-size:11px; color:var(--text-dim);">{{ o['done_ops'] }}/{{ o['total_ops'] }} операцій</span>
      </td>
      <td>{{ o['planned_end'] or '—' }}</td>
    </tr>
    {% else %}
    <tr><td colspan="6" class="empty">Виробничих замовлень ще немає — вони створюються автоматично при переході угоди на стадію «Передоплата отримана»</td></tr>
    {% endfor %}
  </table>
</div>
{% endblock %}
"""

PRODUCTION_DETAIL_HTML = """
{% extends "base.html" %}
{% block title %}Виробниче замовлення №{{ order['id'] }}{% endblock %}
{% block heading %}Виробниче замовлення №{{ order['id'] }}{% endblock %}
{% block subheading %}{{ order['client_name'] or '' }} · {{ order['machine_name'] or '' }}{% endblock %}
{% block content %}
<div class="detail-header">
  <div class="stat-row">
    <div class="stat">Статус<b>{{ dict(PRODUCTION_ORDER_STATUSES)[order['status']] }}</b></div>
    <div class="stat">Кількість<b>{{ order['quantity'] }}</b></div>
    <div class="stat">Плановий термін<b>{{ order['due_date'] or '—' }}</b></div>
  </div>
  <div style="display:flex; gap:8px;">
    {% if current_user['role'] in ('admin','sales','accountant') %}<a class="btn btn-sm" href="{{ url_for('production_costs') }}#order-{{ order['id'] }}">⚖ План/факт</a>{% endif %}
    <a class="btn btn-sm" href="{{ url_for('production_route_sheet_pdf', order_id=order['id']) }}" target="_blank">📋 Маршрутний лист (PDF)</a>
    <form method="post" action="{{ url_for('production_reschedule', order_id=order['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <button class="btn btn-sm" type="submit">↻ Перерахувати розклад</button>
    </form>
  </div>
</div>
<div class="section-title"><span class="accent-dot"></span> Маршрутна карта та розклад по робочих центрах</div>
<div class="card table-wrap">
  <table>
    <tr><th>#</th><th>Операція</th><th>Робочий центр</th><th>Норма, год</th><th>План початок</th><th>План кінець</th><th>Статус</th><th></th></tr>
    {% for op in operations %}
    <tr>
      <td>{{ op['step_order'] }}</td>
      <td>{{ op['operation_name'] }}</td>
      <td>{{ op['wc_name'] or '—' }}</td>
      <td>{{ op['planned_hours'] }}</td>
      <td>{{ op['planned_start'] or '—' }}</td>
      <td>{{ op['planned_end'] or '—' }}</td>
      <td><span class="badge badge-{{ 'low' if op['status']=='done' else ('medium' if op['status']=='in_progress' else 'high') }}">{{ dict(OPERATION_STATUSES)[op['status']] }}</span></td>
      <td>
        {% if op['status']!='done' %}
        <form method="post" action="{{ url_for('operation_advance', op_id=op['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
          <button class="btn btn-sm" type="submit">{{ 'Почати' if op['status']=='waiting' else 'Завершити' }}</button>
        </form>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </table>
</div>
<div class="section-title"><span class="accent-dot"></span> Gantt-схема (планові терміни операцій)</div>
<div class="card card-pad">
  {% for op in operations %}
  <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
    <div style="width:220px; font-size:12px;">{{ op['operation_name'] }}</div>
    <div style="flex:1; background:var(--bg-soft); border-radius:6px; height:18px; position:relative;">
      <div style="position:absolute; left:{{ op['offset_pct'] }}%; width:{{ op['width_pct'] }}%; min-width:2%; height:100%; border-radius:6px; background:{{ '#3fae5c' if op['status']=='done' else '#4fa8e0' }};"></div>
    </div>
    <div style="width:90px; font-size:11px; color:var(--text-dim);">{{ op['planned_hours'] }} год</div>
  </div>
  {% endfor %}
</div>
{% endblock %}
"""

TERMINAL_HTML = """
{% extends "base.html" %}
{% block title %}Термінал: {{ wc['name'] }}{% endblock %}
{% block heading %}{{ wc['name'] }}{% endblock %}
{% block subheading %}{{ 'Перегляд іншої дільниці — статус деталей' if not can_act else 'Термінал цеху — статус деталей на цій дільниці' }}{% endblock %}
{% block content %}
<div class="toolbar">
  <div>
    {% if not can_act %}<span class="badge badge-medium">👁 Лише перегляд — це не ваша дільниця</span>{% endif %}
  </div>
  <div style="display:flex; gap:8px;">
    {% if foreman_own_wc and not can_act %}<a class="btn btn-primary" href="{{ url_for('work_center_terminal', wc_id=foreman_own_wc) }}">← Моя дільниця</a>{% endif %}
    <a class="btn" href="{{ url_for('work_centers_list') }}">{{ 'Інші дільниці' if foreman_own_wc else '← Всі робочі центри' }}</a>
  </div>
</div>
<div style="display:flex; flex-direction:column; gap:14px;">
  {% for op in ops %}
  <div class="card card-pad" style="{{ 'opacity:0.55;' if not op.ready }}">
    <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:12px;">
      <div>
        <div style="font-size:11px; color:var(--text-dim);">{{ op.client_name or '—' }}</div>
        <div style="font-size:18px; font-weight:700; margin:2px 0;">{{ op.deal_title or ('Замовлення №' ~ op.production_order_id) }}</div>
        <div style="font-size:13px; color:var(--accent-2);">{{ op.operation_name }}</div>
        <div style="font-size:11.5px; color:var(--text-dim); margin-top:4px;">
          Крок {{ op.step_order }} · Кількість: {{ op.quantity }} шт. · Норма: {{ op.planned_hours }} год
          {% if op.planned_start %} · План: {{ op.planned_start[:16] }}{% endif %}
        </div>
        {% if not op.ready %}
        <div style="font-size:11.5px; color:var(--yellow); margin-top:6px;">⏳ Очікує завершення попереднього етапу в іншому цеху</div>
        {% endif %}
      </div>
      <div style="text-align:center;">
        <span class="badge badge-{{ 'low' if op.status=='in_progress' else 'medium' }}" style="margin-bottom:10px; display:inline-block;">
          {{ 'У РОБОТІ' if op.status=='in_progress' else 'ОЧІКУЄ' }}
        </span><br>
        {% if not can_act %}
        <span style="font-size:11.5px; color:var(--text-dim);">Дії доступні лише на своїй дільниці</span>
        {% elif op.ready %}
        <form method="post" action="{{ url_for('operation_advance', op_id=op.id) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
          <button class="btn btn-primary" type="submit" style="font-size:16px; padding:16px 28px;">
            {{ 'ЗАВЕРШИТИ' if op.status=='in_progress' else 'ПОЧАТИ' }}
          </button>
        </form>
        {% else %}
        <button class="btn" type="button" disabled style="font-size:16px; padding:16px 28px; opacity:0.5; cursor:not-allowed;">ОЧІКУЄ</button>
        {% endif %}
      </div>
    </div>
  </div>
  {% else %}
  <div class="empty" style="padding:60px 20px; font-size:15px;">На цій дільниці зараз немає активних завдань</div>
  {% endfor %}
</div>
<script>
  // Автооновлення екрана раз на хвилину - оператор лишає сторінку відкритою
  // весь день, нові завдання мають з'являтись самі, без ручного F5.
  setTimeout(function(){ location.reload(); }, 60000);
</script>
{% endblock %}
"""

WORK_CENTERS_HTML = """
{% extends "base.html" %}
{% block title %}Робочі центри{% endblock %}
{% block heading %}Робочі центри та завантаження{% endblock %}
{% block subheading %}Телеметрія з реального обладнання (Haas та інші) + % планового завантаження{% endblock %}
{% block content %}
<div class="toolbar">
  <div class="filters">
    {% if foreman_own_wc %}<span class="badge badge-low">👁 Перегляд усіх дільниць — дії доступні лише на вашій</span>{% endif %}
  </div>
  {% if foreman_own_wc %}<a class="btn btn-primary btn-sm" href="{{ url_for('work_center_terminal', wc_id=foreman_own_wc) }}">← Моя дільниця</a>{% else %}
  <a class="btn btn-sm" href="{{ url_for('settings_page') }}">⚒ Підключити обладнання (API / Haas)</a>
  {% endif %}
</div>
<div class="grid grid-3">
  {% for wc in work_centers %}
  {% set t = telemetry.get(wc['id']) %}
  {% set rs = runtime_stats.get(wc['id']) %}
  <div class="card card-pad" style="{{ 'outline:1px solid var(--accent-2);' if foreman_own_wc==wc['id'] }}">
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <h3 style="margin:0;">{{ wc['name'] }}{% if foreman_own_wc==wc['id'] %} <span class="badge badge-low" style="font-size:10px;">моя</span>{% endif %}</h3>
      {% if t and t['status']=='running' %}<span class="badge badge-low">▶ працює</span>
      {% elif t and t['status']=='alarm' %}<span class="badge badge-high">⛔ аварія</span>
      {% elif t and t['status']=='idle' %}<span class="badge badge-medium">⏸ простій</span>
      {% else %}<span class="badge" style="color:var(--text-dim);">офлайн</span>{% endif %}
    </div>
    <div style="font-size:12px; color:var(--text-dim); margin:6px 0;">{{ wc['type'] or '' }} · {{ wc['capacity_hours_per_day'] }} год/добу</div>

    {% if t %}
    {% if t['program_name'] %}<div style="font-size:11.5px;">Програма: <b>{{ t['program_name'] }}</b>{% if t['mode'] %} · {{ t['mode'] }}{% endif %}</div>{% endif %}
    {% if t['spindle_load_pct'] is not none %}
    <div style="font-size:11.5px; margin-top:6px;">Навантаження шпинделя: {{ t['spindle_load_pct'] or 0 }}%</div>
    <div class="progress-bar"><div style="width:{{ t['spindle_load_pct'] or 0 }}%;"></div></div>
    {% endif %}
    <div style="font-size:11px; color:var(--text-dim); margin-top:6px;">
      {% if t['part_count'] is not none %}Деталей: {{ t['part_count'] }} · {% endif %}
      джерело: {{ t['source'] or 'generic' }} · оновлено {{ t['last_seen'][:16] if t['last_seen'] else '' }}
    </div>
    {% endif %}

    {% if rs %}
    <div class="section-title" style="font-size:12px; margin:14px 0 6px;">Напрацювання (Motion Time)</div>
    <div class="stat-row" style="gap:14px;">
      <div class="stat">Сьогодні<b>{{ rs.today_hours }} год</b></div>
      <div class="stat">За 7 днів<b>{{ rs.week_hours }} год</b></div>
      <div class="stat">Всього<b>{{ rs.total_motion_hours }} год</b></div>
      <div class="stat">Доступність 14д<b style="color:{{ 'var(--green)' if rs.availability_pct>=70 else ('var(--yellow)' if rs.availability_pct>=40 else 'var(--red)') }};">{{ rs.availability_pct }}%</b></div>
    </div>
    {% if rs.alarm_count_week %}
    <details style="margin-top:6px;">
      <summary style="font-size:11px; color:var(--red); cursor:pointer;">⚠ {{ rs.alarm_count_week }} аварійних сигналів за 7 днів — показати</summary>
      <ul style="font-size:10.5px; color:var(--text-dim); margin:6px 0 0; padding-left:16px;">
        {% for a in rs.recent_alarms %}<li>{{ a }}</li>{% endfor %}
      </ul>
    </details>
    {% endif %}
    <div class="section-title" style="font-size:11px; margin:12px 0 4px; color:var(--text-dim);">Напрацювання по днях, 14 днів</div>
    <div style="display:flex; align-items:flex-end; gap:2px; height:44px;">
      {% set max_day = (rs.daily | map(attribute='hours') | max) or 1 %}
      {% for d in rs.daily %}
      <div style="flex:1; display:flex; flex-direction:column; align-items:center; justify-content:flex-end; height:100%;" title="{{ d.date }}: {{ d.hours }} год">
        <div style="width:100%; background:var(--accent-2); border-radius:2px 2px 0 0; height:{{ (d.hours / max_day * 100) if max_day else 0 }}%; min-height:{{ '2px' if d.hours > 0 else '0' }};"></div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div style="font-size:11px; color:var(--text-dim); margin-top:10px;">Немає даних напрацювання — підключіть телеметрію (див. «Налаштування»)</div>
    {% endif %}

    <div class="section-title" style="font-size:12px; margin:14px 0 6px;">Планове завантаження, %</div>
    <div class="progress-bar"><div style="width:{{ utilization.get(wc['id'], 0) }}%;"></div></div>
    <div style="font-size:11px; color:var(--text-dim); margin-top:4px;">{{ utilization.get(wc['id'], 0) }}% на найближчі 14 днів (за виробничим планом)</div>
    {% if can_connect %}
    <details class="mtc-connect" style="margin-top:12px;" {{ 'open' if not wc['mtc_enabled'] and not t }}>
      <summary style="cursor:pointer; font-size:12px; color:var(--accent-2);">🔌 {{ 'Підключено: ' ~ wc['mtc_host'] ~ ':' ~ (wc['mtc_port'] or 8082) if wc['mtc_enabled'] else 'Підключити верстат (MTConnect)' }}</summary>
      <form method="post" action="{{ url_for('work_center_connect', wc_id=wc['id']) }}" style="margin-top:8px;">
        <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <div class="field"><label>IP-адреса верстата</label>
          <input type="text" name="mtc_host" value="{{ wc['mtc_host'] or '' }}" placeholder="напр. 192.168.1.50" autocomplete="off"></div>
        <div class="field"><label>Порт (Haas — 8082)</label>
          <input type="text" name="mtc_port" value="{{ wc['mtc_port'] or 8082 }}"></div>
        <button class="btn btn-sm" type="submit">Зберегти й перевірити зв'язок</button>
        <div style="font-size:11px; color:var(--text-dim); margin-top:6px;">Порожня адреса — вимкнути. На Haas має бути увімкнено налаштування 143.</div>
      </form>
    </details>
    {% endif %}
    <a class="btn btn-primary btn-sm" href="{{ url_for('work_center_terminal', wc_id=wc['id']) }}" style="margin-top:12px; width:100%; justify-content:center;">🖥 Відкрити термінал цеху</a>
  </div>
  {% endfor %}
</div>
{% endblock %}
"""

WAREHOUSE_HTML = """
{% extends "base.html" %}
{% block title %}Склад{% endblock %}
{% block heading %}Склад: матеріали та запчастини{% endblock %}
{% block subheading %}Позицій нижче мінімального залишку: {{ low_stock_count }}{% endblock %}
{% block content %}
<div class="toolbar">
  <div class="filters">
    {% if low_stock_count %}<span class="badge badge-high">⚠ {{ low_stock_count }} позицій потребують поповнення</span>{% endif %}
  </div>
  <a class="btn" href="{{ url_for('scrap_list') }}">🔩 Обрізки та відходи</a>
  <a class="btn btn-primary" href="{{ url_for('warehouse_item_new') }}">+ Нова позиція</a>
</div>
<div class="card table-wrap">
  <table>
    <tr><th>SKU</th><th>Назва</th><th>Категорія</th><th>Залишок</th><th>Мін.</th><th>Ціна</th><th>Локація</th><th></th></tr>
    {% for i in items %}
    <tr class="{{ 'low' if i['qty_on_hand'] <= i['min_qty'] }}" onclick="window.location='{{ url_for('warehouse_item_detail', item_id=i['id']) }}'" style="cursor:pointer;">
      <td>{{ i['sku'] }}</td>
      <td>{{ i['name'] }}</td>
      <td><span class="tag">{{ i['category'] }}</span></td>
      <td style="color:{{ 'var(--red)' if i['qty_on_hand'] <= i['min_qty'] else 'var(--text)' }};"><b>{{ i['qty_on_hand'] }}</b> {{ i['unit'] }}</td>
      <td>{{ i['min_qty'] }} {{ i['unit'] }}</td>
      <td>{{ i['unit_cost'] | money(i['currency']) }}</td>
      <td>{{ i['location'] or '—' }}</td>
      <td>{% if i['qty_on_hand'] <= i['min_qty'] %}<span class="badge badge-high">поповнити</span>{% endif %}</td>
    </tr>
    {% else %}
    <tr><td colspan="8" class="empty">Склад порожній</td></tr>
    {% endfor %}
  </table>
</div>
{% endblock %}
"""

SCRAP_HTML = """
{% extends "base.html" %}
{% block title %}Обрізки та відходи{% endblock %}
{% block heading %}Ділові відходи та обрізки{% endblock %}
{% block subheading %}Облік залишків металу для повторного використання{% endblock %}
{% block content %}
<div class="toolbar">
  <div class="filters">
    <a class="btn btn-sm {{ 'btn-primary' if status_filter=='available' }}" href="?status=available">Доступні</a>
    <a class="btn btn-sm {{ 'btn-primary' if status_filter=='used' }}" href="?status=used">Використані</a>
    <a class="btn btn-sm {{ 'btn-primary' if status_filter=='' }}" href="?status=">Всі</a>
  </div>
  <a class="btn" href="{{ url_for('warehouse_list') }}">← До складу</a>
</div>
<div class="grid grid-2">
  <div class="card table-wrap">
    <table>
      <tr><th>Матеріал</th><th>Товщина</th><th>Розмір, мм</th><th>Вага, кг</th><th>Локація</th><th>Статус</th><th></th></tr>
      {% for o in offcuts %}
      <tr>
        <td>{{ o['material_name'] }}</td>
        <td>{{ o['thickness_mm'] or '—' }}</td>
        <td>{{ (o['length_mm']|string + ' × ' + o['width_mm']|string) if o['length_mm'] and o['width_mm'] else '—' }}</td>
        <td>{{ o['weight_kg'] or '—' }}</td>
        <td>{{ o['location'] or '—' }}</td>
        <td><span class="badge badge-{{ 'low' if o['status']=='available' else 'medium' }}">{{ dict(SCRAP_STATUSES)[o['status']] }}</span></td>
        <td style="display:flex; gap:6px;">
          <form method="post" action="{{ url_for('scrap_mark_used', offcut_id=o['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
            <button class="btn btn-sm" type="submit">{{ 'Позначити використаним' if o['status']=='available' else 'Повернути в наявність' }}</button>
          </form>
          <form method="post" action="{{ url_for('scrap_delete', offcut_id=o['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm btn-danger" type="submit">✕</button></form>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="7" class="empty">Записів немає</td></tr>
      {% endfor %}
    </table>
  </div>
  <form method="post" action="{{ url_for('scrap_add') }}" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
    <div class="field" style="margin-bottom:10px;"><label>Матеріал *</label><input type="text" name="material_name" placeholder="напр. Сталь 3, лист" required></div>
    <div class="form-grid" style="margin-bottom:10px;">
      <div class="field"><label>Товщина, мм</label><input type="number" step="0.1" name="thickness_mm"></div>
      <div class="field"><label>Вага, кг</label><input type="number" step="0.1" name="weight_kg"></div>
      <div class="field"><label>Довжина, мм</label><input type="number" step="1" name="length_mm"></div>
      <div class="field"><label>Ширина, мм</label><input type="number" step="1" name="width_mm"></div>
    </div>
    <div class="field" style="margin-bottom:10px;"><label>Локація</label><input type="text" name="location"></div>
    <button class="btn btn-primary" type="submit">+ Зареєструвати обрізок</button>
  </form>
</div>
{% endblock %}
"""

WAREHOUSE_ITEM_HTML = """
{% extends "base.html" %}
{% block title %}{{ item['name'] if item else 'Нова позиція' }}{% endblock %}
{% block heading %}{{ item['name'] if item else 'Нова складська позиція' }}{% endblock %}
{% block subheading %}{{ ('SKU ' + item['sku'] + ' · ' + (item['category'] or '')) if item else '' }}{% endblock %}
{% block content %}
{% if not item %}
<form method="post" action="{{ url_for('warehouse_item_new') }}" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
  <div class="form-grid">
    <div class="field"><label>SKU</label><input type="text" name="sku"></div>
    <div class="field"><label>Назва *</label><input type="text" name="name" required></div>
    <div class="field"><label>Категорія</label><input type="text" name="category" placeholder="Матеріал / Запчастина / Комплектуючі"></div>
    <div class="field"><label>Од. виміру</label><input type="text" name="unit" value="шт"></div>
    <div class="field"><label>Залишок</label><input type="number" step="0.01" name="qty_on_hand" value="0"></div>
    <div class="field"><label>Мінімальний залишок</label><input type="number" step="0.01" name="min_qty" value="0"></div>
    <div class="field"><label>Ціна за одиницю</label><input type="number" step="0.01" name="unit_cost" value="0"></div>
    <div class="field">
      <label>Валюта</label>
      <select name="currency">{% for cur in CURRENCIES + ['UAH'] %}<option {{ 'selected' if cur=='UAH' }}>{{ cur }}</option>{% endfor %}</select>
    </div>
    <div class="field"><label>Локація на складі</label><input type="text" name="location"></div>
  </div>
  <div class="actions">
    <button class="btn btn-primary" type="submit">Додати позицію</button>
    <a class="btn btn-ghost" href="{{ url_for('warehouse_list') }}">Скасувати</a>
  </div>
</form>
{% else %}
<div class="stat-row" style="margin-bottom:18px;">
  <div class="stat">Залишок<b>{{ item['qty_on_hand'] }} {{ item['unit'] }}</b></div>
  <div class="stat">Мінімум<b>{{ item['min_qty'] }} {{ item['unit'] }}</b></div>
  <div class="stat">Ціна за од.<b>{{ item['unit_cost'] | money(item['currency']) }}</b></div>
  <div class="stat">Локація<b>{{ item['location'] or '—' }}</b></div>
</div>
<div class="grid grid-2">
  <div>
    <div class="section-title"><span class="accent-dot"></span> Історія рухів</div>
    <div class="card table-wrap">
      <table>
        <tr><th>Дата</th><th>Тип</th><th>Кількість</th><th>Хто</th><th>Примітка</th></tr>
        {% for t in transactions %}
        <tr>
          <td>{{ t['created_at'][:16] }}</td>
          <td><span class="tag">{{ dict(WAREHOUSE_TX_TYPES).get(t['type'], t['type']) }}</span></td>
          <td style="color:{{ 'var(--green)' if t['qty_delta']>0 else 'var(--red)' }};">{{ '+' if t['qty_delta']>0 else '' }}{{ t['qty_delta'] }}</td>
          <td>{{ t['user_name'] or '—' }}</td>
          <td>{{ t['reference'] or '' }}</td>
        </tr>
        {% else %}
        <tr><td colspan="5" class="empty">Рухів ще не було</td></tr>
        {% endfor %}
      </table>
    </div>
  </div>
  <div>
    <div class="section-title"><span class="accent-dot"></span> {{ 'Списання матеріалу' if current_user['role']=='production' else 'Прихід / списання' }}</div>
    <form method="post" action="{{ url_for('warehouse_transaction_add', item_id=item['id']) }}" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      {% if current_user['role']=='production' %}
      <input type="hidden" name="type" value="out">
      <p style="font-size:11.5px; color:var(--text-dim); margin-top:0;">Спиши матеріал, який фактично витратив на деталь. Прихід нового матеріалу та коригування залишків проводить склад.</p>
      {% else %}
      <div class="field" style="margin-bottom:10px;">
        <label>Тип операції</label>
        <select name="type">{% for k,v in WAREHOUSE_TX_TYPES %}<option value="{{ k }}">{{ v }}</option>{% endfor %}</select>
      </div>
      {% endif %}
      <div class="field" style="margin-bottom:10px;"><label>Кількість</label><input type="number" step="0.01" name="qty" required></div>
      <div class="field" style="margin-bottom:10px;"><label>Примітка</label><input type="text" name="reference" placeholder="{{ 'напр.: списано на в.з. №12' if current_user['role']=='production' else '' }}"></div>
      <button class="btn btn-primary" type="submit">{{ 'Списати' if current_user['role']=='production' else 'Провести операцію' }}</button>
    </form>
  </div>
</div>

{% if current_user['role'] != 'production' %}
<div class="section-title"><span class="accent-dot"></span> Партії матеріалу (плавки, сертифікати якості)</div>
<div class="grid grid-2">
  <div class="card table-wrap">
    <table>
      <tr><th>№ плавки</th><th>№ сертифіката</th><th>Постачальник</th><th>К-сть</th><th>Отримано</th><th></th></tr>
      {% for lot in lots %}
      <tr>
        <td>{{ lot['heat_number'] or '—' }}</td>
        <td>{{ lot['certificate_number'] or '—' }}</td>
        <td>{{ lot['supplier'] or '—' }}</td>
        <td>{{ lot['qty'] }} {{ item['unit'] }}</td>
        <td>{{ lot['received_date'] or '—' }}</td>
        <td><form method="post" action="{{ url_for('material_lot_delete', lot_id=lot['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm btn-danger" type="submit">✕</button></form></td>
      </tr>
      {% else %}
      <tr><td colspan="6" class="empty">Партій ще не зареєстровано</td></tr>
      {% endfor %}
    </table>
  </div>
  <form method="post" action="{{ url_for('material_lot_add', item_id=item['id']) }}" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
    <p style="font-size:11.5px; color:var(--text-dim); margin-top:0;">Для простежуваності: до якої партії металу належить конкретний лист/пруток, з яким сертифікатом якості.</p>
    <div class="field" style="margin-bottom:10px;"><label>№ плавки (heat number)</label><input type="text" name="heat_number"></div>
    <div class="field" style="margin-bottom:10px;"><label>№ сертифіката якості</label><input type="text" name="certificate_number"></div>
    <div class="field" style="margin-bottom:10px;"><label>Постачальник</label><input type="text" name="supplier"></div>
    <div class="form-grid" style="margin-bottom:10px;">
      <div class="field"><label>Кількість</label><input type="number" step="0.01" name="qty" required></div>
      <div class="field"><label>Дата отримання</label><input type="date" name="received_date"></div>
    </div>
    <button class="btn btn-sm btn-primary" type="submit">+ Зареєструвати партію</button>
  </form>
</div>
{% endif %}
{% endif %}
{% endblock %}
"""

ERROR_HTML = """
<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Помилка {{ code }} · Оснастка-Маркет</title>
<style>
body{margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
  background:#15171b; color:#e7e9ec; font-family:system-ui,sans-serif;}
.box{text-align:center; padding:40px;}
.code{font-size:64px; font-weight:800; color:#ff7a1a; margin-bottom:6px;}
a{color:#4fa8e0; text-decoration:none;}
</style>
</head>
<body>
<div class="box">
  <div class="code">{{ code }}</div>
  <p>{{ message }}</p>
  <p><a href="/">← На дашборд</a></p>
</div>
</body>
</html>
"""

SERVICE_DETAIL_HTML = """
{% extends "base.html" %}
{% block title %}Заявка №{{ ticket['id'] }}{% endblock %}
{% block heading %}Сервісна заявка №{{ ticket['id'] }}{% endblock %}
{% block subheading %}{{ ticket['client_name'] }} · {{ ticket['machine_name'] or '' }}{% endblock %}
{% block content %}
<div class="stat-row" style="margin-bottom:18px;">
  <div class="stat">Пріоритет<b>{{ PRIORITIES[ticket['priority']] }}</b></div>
  <div class="stat">Статус<b>{{ dict(SERVICE_STATUSES)[ticket['status']] }}</b></div>
  <div class="stat">Інженер<b>{{ ticket['engineer'] or '—' }}</b></div>
  <div class="stat">Створено<b>{{ ticket['created_at'][:16] }}</b></div>
</div>
<div class="section-title"><span class="accent-dot"></span> Опис проблеми</div>
<div class="card card-pad"><p style="margin:0; white-space:pre-wrap;">{{ ticket['issue'] }}</p></div>

<div class="section-title"><span class="accent-dot"></span> Файли та фото (акти, фото дефектів, креслення)</div>
<div class="card card-pad">
  <div style="display:flex; flex-wrap:wrap; gap:10px; margin-bottom:14px;">
    {% for a in attachments %}
    <div style="border:1px solid var(--border); border-radius:8px; padding:10px; width:220px;">
      <div style="font-size:12.5px; word-break:break-all; margin-bottom:8px;">📎 {{ a['original_name'] }}</div>
      <div style="font-size:11px; color:var(--text-dim); margin-bottom:8px;">{{ (a['size_bytes']/1024)|round(1) }} КБ · {{ a['uploader_name'] or '' }}</div>
      <div style="display:flex; gap:6px;">
        <a class="btn btn-sm" href="{{ url_for('attachment_download', attachment_id=a['id']) }}">Завантажити</a>
        <form method="post" action="{{ url_for('attachment_delete', attachment_id=a['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm btn-danger" type="submit">✕</button></form>
      </div>
    </div>
    {% else %}
    <div class="empty">Файлів ще немає</div>
    {% endfor %}
  </div>
  <form method="post" action="{{ url_for('attachment_upload', entity_type='service_ticket', entity_id=ticket['id']) }}" enctype="multipart/form-data"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
    <input type="file" name="file" required>
    <button class="btn btn-sm btn-primary" type="submit">+ Додати файл</button>
  </form>
</div>
{% endblock %}
"""

FINANCE_HTML = """
{% extends "base.html" %}
{% block title %}Фінанси{% endblock %}
{% block heading %}Фінанси: оплати по угодах{% endblock %}
{% block subheading %}{{ 'Прострочено: ' + overdue_count|string if overdue_count else 'Прострочених немає' }}{% endblock %}
{% block content %}
<div class="grid grid-4" style="margin-bottom:20px;">
  <div class="card kpi">
    <div class="label">Сплачено всього</div>
    <div class="kpi-value" style="color:var(--green);">{{ totals.paid_total | money('UAH') }}</div>
  </div>
  <div class="card kpi">
    <div class="label">Очікується</div>
    <div class="kpi-value" style="color:var(--accent-2);">{{ totals.pending_total | money('UAH') }}</div>
  </div>
  <div class="card kpi">
    <div class="label">Прострочено платежів</div>
    <div class="kpi-value" style="color:{{ 'var(--red)' if overdue_count else 'var(--text)' }};">{{ overdue_count }}</div>
  </div>
</div>
<div class="toolbar">
  <div class="filters">
    <a class="btn btn-sm {{ 'btn-primary' if status_filter=='' }}" href="?status=">Всі</a>
    <a class="btn btn-sm {{ 'btn-primary' if status_filter=='pending' }}" href="?status=pending">Очікуються</a>
    <a class="btn btn-sm {{ 'btn-primary' if status_filter=='paid' }}" href="?status=paid">Оплачені</a>
  </div>
</div>
<div class="card table-wrap">
  <table>
    <tr><th>Угода</th><th>Клієнт</th><th>Тип</th><th>Сума</th><th>Термін</th><th>Статус</th></tr>
    {% for p in payments %}
    <tr>
      <td>{{ p['deal_title'] or '—' }}</td>
      <td>{{ p['client_name'] or '—' }}</td>
      <td>{{ dict(PAYMENT_KINDS).get(p['kind'], p['kind']) }}</td>
      <td>{{ p['amount'] | money(p['currency']) }}</td>
      <td style="color:{{ 'var(--red)' if p['status']=='pending' and p['due_date'] and p['due_date'] < today else 'var(--text)' }};">{{ p['due_date'] or '—' }}</td>
      <td><span class="badge badge-{{ 'low' if p['status']=='paid' else 'medium' }}">{{ 'Оплачено' if p['status']=='paid' else 'Очікується' }}</span></td>
    </tr>
    {% else %}
    <tr><td colspan="6" class="empty">Платежів немає</td></tr>
    {% endfor %}
  </table>
</div>
{% endblock %}
"""

SEARCH_HTML = """
{% extends "base.html" %}
{% block title %}Пошук{% endblock %}
{% block heading %}Результати пошуку: «{{ q }}»{% endblock %}
{% block content %}
<div class="section-title"><span class="accent-dot"></span> Клієнти</div>
<div class="card table-wrap">
<table>
  {% for c in clients %}
  <tr onclick="window.location='{{ url_for('client_detail', client_id=c['id']) }}'" style="cursor:pointer;"><td>{{ c['name'] }}</td><td>{{ c['city'] or '' }}</td></tr>
  {% else %}<tr><td class="empty">Нічого не знайдено</td></tr>{% endfor %}
</table>
</div>
<div class="section-title"><span class="accent-dot"></span> Угоди</div>
<div class="card table-wrap">
<table>
  {% for dl in deals %}
  <tr onclick="window.location='{{ url_for('deal_detail', deal_id=dl['id']) }}'" style="cursor:pointer;"><td>{{ dl['title'] }}</td><td>{{ dl['amount'] | money(dl['currency']) }}</td></tr>
  {% else %}<tr><td class="empty">Нічого не знайдено</td></tr>{% endfor %}
</table>
</div>
{% if show_calculations %}
<div class="section-title"><span class="accent-dot"></span> Розрахунки вартості</div>
<div class="card table-wrap">
<table>
  {% for calc in calculations %}
  <tr onclick="window.location='{{ url_for('calculator_history_detail', calc_id=calc['id']) }}'" style="cursor:pointer;"><td>{{ calc['part_description'] or '—' }}</td><td>{{ '%.2f'|format(calc['total_price'] or 0) }} грн</td></tr>
  {% else %}<tr><td class="empty">Нічого не знайдено</td></tr>{% endfor %}
</table>
</div>
{% endif %}
{% endblock %}
"""

MY_PROFILE_HTML = """
{% extends "base.html" %}
{% block title %}Мій профіль{% endblock %}
{% block heading %}Мій профіль{% endblock %}
{% block subheading %}{{ current_user['full_name'] }} · {{ ROLE_LABEL.get(current_user['role'], current_user['role']) }}{% endblock %}
{% block content %}
<div class="grid grid-2">
  <form method="post" class="card card-pad">
    <div class="section-title" style="margin-top:0;"><span class="accent-dot"></span> Змінити пароль</div>
    <div class="field" style="margin-bottom:12px;"><label>Поточний пароль</label><input type="password" name="current_password" required></div>
    <div class="field" style="margin-bottom:12px;"><label>Новий пароль</label><input type="password" name="new_password" required minlength="6"></div>
    <div class="field" style="margin-bottom:12px;"><label>Підтвердіть новий пароль</label><input type="password" name="confirm_password" required minlength="6"></div>
    <button class="btn btn-primary" type="submit">Змінити пароль</button>
  </form>
  <div class="card card-pad">
    <div class="section-title" style="margin-top:0;"><span class="accent-dot"></span> Дані облікового запису</div>
    <div class="stat-row">
      <div class="stat">ПІБ<b>{{ current_user['full_name'] }}</b></div>
      <div class="stat">Логін<b>{{ current_user['username'] }}</b></div>
      <div class="stat">Роль<b>{{ ROLE_LABEL.get(current_user['role'], current_user['role']) }}</b></div>
    </div>
  </div>
</div>
{% endblock %}
"""

USERS_HTML = """
{% extends "base.html" %}
{% block title %}Користувачі{% endblock %}
{% block heading %}Користувачі та права доступу{% endblock %}
{% block subheading %}Тільки для адміністратора{% endblock %}
{% block content %}
<div class="grid grid-2">
  <div>
    <div class="section-title"><span class="accent-dot"></span> Список користувачів</div>
    <div class="card table-wrap">
      <table>
        <tr><th>ПІБ</th><th>Логін</th><th>Роль</th><th>Дільниця</th><th>Статус</th><th></th></tr>
        {% for u in users %}
        <tr>
          <td>{{ u['full_name'] }}</td>
          <td>{{ u['username'] }}</td>
          <td><span class="tag">{{ ROLE_LABEL.get(u['role'], u['role']) }}</span></td>
          <td>{{ u['work_center_name'] or ('всі' if u['role']=='production' else '—') }}</td>
          <td>{% if u['active'] %}<span class="badge badge-low">активний</span>{% else %}<span class="badge badge-high">вимкнено</span>{% endif %}</td>
          <td>
            <form method="post" action="{{ url_for('user_toggle', user_id=u['id']) }}" onsubmit="return confirm('Змінити статус користувача?');"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
              <button class="btn btn-sm" type="submit">{{ 'Вимкнути' if u['active'] else 'Увімкнути' }}</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </table>
    </div>
  </div>
  <div>
    <div class="section-title"><span class="accent-dot"></span> Новий користувач</div>
    <form method="post" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <div class="field" style="margin-bottom:10px;"><label>ПІБ *</label><input type="text" name="full_name" required></div>
      <div class="field" style="margin-bottom:10px;"><label>Логін *</label><input type="text" name="username" required></div>
      <div class="field" style="margin-bottom:10px;"><label>Пароль *</label><input type="password" name="password" required></div>
      <div class="field" style="margin-bottom:10px;">
        <label>Роль</label>
        <select name="role" id="new-user-role" onchange="document.getElementById('wc-field').style.display = this.value==='production' ? 'block' : 'none';">
          {% for k,v in ROLES %}<option value="{{ k }}">{{ v }}</option>{% endfor %}
        </select>
      </div>
      <div class="field" id="wc-field" style="margin-bottom:10px; display:none;">
        <label>Дільниця (майстер конкретного цеху)</label>
        <select name="work_center_id">
          <option value="">— всі дільниці (загальний виробничник) —</option>
          {% for wc in work_centers %}<option value="{{ wc['id'] }}">{{ wc['name'] }}</option>{% endfor %}
        </select>
      </div>
      <button class="btn btn-primary" type="submit">Додати користувача</button>
    </form>
    <div class="section-title"><span class="accent-dot"></span> Опис ролей</div>
    <div class="card card-pad" style="font-size:12.5px; color:var(--text-dim);">
      <p><b style="color:var(--text);">Адміністратор</b> — повний доступ до всіх модулів, налаштувань та інтеграцій.</p>
      <p><b style="color:var(--text);">Менеджер продажів</b> — клієнти, угоди, задачі, КП. Бачить лише свої угоди.</p>
      <p><b style="color:var(--text);">Виробництво</b> — якщо прив'язаний до дільниці, бачить тільки термінал своєї
      дільниці; без прив'язки — все виробництво (планування, всі дільниці).</p>
      <p><b style="color:var(--text);">Склад</b> — залишки, прихід/видача, списання матеріалів.</p>
      <p><b style="color:var(--text);">Сервіс</b> — рекламації клієнтів, доробки та повторні звернення.</p>
      <p><b style="color:var(--text);">Перегляд</b> — тільки перегляд даних, без редагування.</p>
    </div>
  </div>
</div>
{% endblock %}
"""

CUSTOM_FIELDS_HTML = """
{% extends "base.html" %}
{% block title %}Кастомні поля{% endblock %}
{% block heading %}Додаткові поля{% endblock %}
{% block subheading %}Додавай власні поля без правок коду{% endblock %}
{% block content %}
<div class="grid grid-2">
  <div>
    {% for entity_type, fields in fields_by_entity.items() %}
    <div class="section-title" style="margin-top:0;"><span class="accent-dot"></span> {{ 'Клієнти' if entity_type=='client' else 'Угоди' }}</div>
    <div class="card table-wrap" style="margin-bottom:20px;">
      <table>
        <tr><th>Назва</th><th>Тип</th><th></th></tr>
        {% for fd in fields %}
        <tr>
          <td>{{ fd['label'] }}</td>
          <td><span class="tag">{{ dict(CUSTOM_FIELD_TYPES).get(fd['field_type'], fd['field_type']) }}</span></td>
          <td>
            <form method="post" action="{{ url_for('custom_field_delete', field_id=fd['id']) }}" onsubmit="return confirm('Видалити поле? Усі введені значення теж зникнуть.');"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
              <button class="btn btn-sm btn-danger" type="submit">✕</button>
            </form>
          </td>
        </tr>
        {% else %}
        <tr><td colspan="3" class="empty">Полів ще немає</td></tr>
        {% endfor %}
      </table>
    </div>
    {% endfor %}
  </div>
  <div>
    <div class="section-title" style="margin-top:0;"><span class="accent-dot"></span> Нове поле</div>
    <form method="post" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <div class="field" style="margin-bottom:10px;">
        <label>Для чого</label>
        <select name="entity_type">
          <option value="client">Клієнти</option>
          <option value="deal">Угоди</option>
        </select>
      </div>
      <div class="field" style="margin-bottom:10px;"><label>Назва поля</label><input type="text" name="label" placeholder="напр. Джерело контакту" required></div>
      <div class="field" style="margin-bottom:10px;">
        <label>Тип</label>
        <select name="field_type">{% for k,v in CUSTOM_FIELD_TYPES %}<option value="{{ k }}">{{ v }}</option>{% endfor %}</select>
      </div>
      <div class="field" style="margin-bottom:10px;"><label>Варіанти для списку (через кому, якщо тип «Список»)</label><input type="text" name="options" placeholder="Варіант 1, Варіант 2"></div>
      <button class="btn btn-primary" type="submit">Додати поле</button>
    </form>
  </div>
</div>
{% endblock %}
"""

SETTINGS_HTML = """
{% extends "base.html" %}
{% block title %}Налаштування{% endblock %}
{% block heading %}Налаштування та інтеграції{% endblock %}
{% block subheading %}Сповіщення, обладнання, курси валют, резервні копії{% endblock %}
{% block content %}

<div class="grid" style="grid-template-columns:repeat(5,1fr); margin-bottom:22px;">
  <div class="card kpi">
    <div class="label">Telegram</div>
    <div class="kpi-value" style="font-size:16px; color:{{ 'var(--green)' if settings.get('telegram_bot_token') and settings.get('telegram_chat_id') else 'var(--text-dim)' }};">
      {{ 'Підключено ✓' if settings.get('telegram_bot_token') and settings.get('telegram_chat_id') else 'Не налаштовано' }}</div>
  </div>
  <div class="card kpi">
    <div class="label">Вебхук (Slack)</div>
    <div class="kpi-value" style="font-size:16px; color:{{ 'var(--green)' if settings.get('webhook_url') else 'var(--text-dim)' }};">
      {{ 'Підключено ✓' if settings.get('webhook_url') else 'Не налаштовано' }}</div>
  </div>
  <div class="card kpi">
    <div class="label">ШІ-калькулятор</div>
    <div class="kpi-value" style="font-size:16px; color:{{ 'var(--green)' if settings.get('anthropic_api_key') else 'var(--text-dim)' }};">
      {{ 'Підключено ✓' if settings.get('anthropic_api_key') else 'Не налаштовано' }}</div>
  </div>
  <div class="card kpi">
    <div class="label">API обладнання</div>
    <div class="kpi-value" style="font-size:16px; color:var(--green);">Активний ✓</div>
  </div>
  <div class="card kpi">
    <div class="label">Курси валют</div>
    <div class="kpi-value" style="font-size:16px;">1$ = {{ settings.get('rate_usd','41.5') }}₴</div>
  </div>
</div>

<div class="section-title"><span class="accent-dot"></span> 🌐 Доступ з інших комп'ютерів у мережі</div>
<div class="card card-pad" style="margin-bottom:22px;">
  {% if network_url %}
  <p style="font-size:12.5px; color:var(--text-dim); margin-top:0;">
    На ІНШИХ комп'ютерах у тій же Wi-Fi/локальній мережі відкрий цю адресу в браузері —
    нічого встановлювати на них не треба, дані спільні й оновлюються одразу:
  </p>
  <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
    <code style="font-size:16px; padding:8px 14px; background:var(--bg-2, rgba(255,255,255,.04)); border-radius:8px;" id="network-url-text">{{ network_url }}</code>
    <button type="button" class="btn btn-sm" onclick="navigator.clipboard.writeText(document.getElementById('network-url-text').textContent).then(()=>{this.textContent='Скопійовано ✓'; setTimeout(()=>this.textContent='Копіювати',1500);})">Копіювати</button>
  </div>
  {% if network_qr_svg %}
  <div style="display:flex; gap:14px; align-items:center; margin-top:14px; flex-wrap:wrap;">
    <div id="network-qr" style="width:132px; height:132px; background:#fff; border-radius:8px; padding:4px;">{{ network_qr_svg }}</div>
    <div style="font-size:12px; color:var(--text-dim); max-width:360px;">
      📱 Навіть простіше з телефону: наведи камеру на QR-код (телефон має бути в тій самій Wi-Fi мережі) і CRM відкриється одразу.
    </div>
  </div>
  {% endif %}
  {% if network_host_url %}
  <p style="font-size:12px; color:var(--text-dim); margin:14px 0 0;">
    Те саме за назвою комп'ютера (не змінюється після перезавантаження роутера, зручно для закладок):
    <code id="network-host-url">{{ network_host_url }}</code>
    <br><span style="font-size:11px;">У Windows-мережі зазвичай працює; на деяких телефонах — ні, тоді користуйся IP-адресою вище.</span>
  </p>
  {% endif %}
  <p style="font-size:11.5px; color:var(--text-dim); margin-bottom:0;">
    Ця ж адреса є в системному треї (біля годинника Windows) → «Адреса для інших комп'ютерів».
    Цей комп'ютер має лишатись увімкненим, щоб інші мали доступ.
  </p>
  <details style="margin-top:12px;">
    <summary style="cursor:pointer; font-size:12.5px; color:var(--accent-2);">Потрібен доступ з іншого Wi-Fi, з дому чи з мобільного інтернету?</summary>
    <ol style="font-size:12px; color:var(--text-dim); margin:8px 0 0; padding-left:18px;">
      <li>Встанови безкоштовну програму <b style="color:var(--text);">Tailscale</b> (tailscale.com) на цей комп'ютер і на пристрій, з якого хочеш зайти, та увійди в обох в один акаунт.</li>
      <li>У Tailscale подивись адресу цього комп'ютера (вигляду <code>100.x.y.z</code>).</li>
      <li>З будь-якої мережі відкрий у браузері <code>http://100.x.y.z:{{ network_url.rsplit(':',1)[-1] }}</code>. Дані ті самі, трафік шифрується.</li>
    </ol>
    <p style="font-size:11.5px; color:var(--text-dim); margin:8px 0 0;">Перед цим обов'язково заміни типові паролі користувачів на свої («Мій профіль»).</p>
  </details>
  {% else %}
  <p style="font-size:12.5px; color:var(--text-dim); margin-top:0; margin-bottom:0;">
    Не вдалося визначити адресу цього комп'ютера в мережі (можливо, немає підключення до
    Wi-Fi/мережі прямо зараз). Перевір з'єднання і відкрий цю сторінку знову.
  </p>
  {% endif %}
</div>

<div class="grid grid-2">
  <div>
    <div class="section-title" style="margin-top:0;"><span class="accent-dot"></span> 📱 Сповіщення в Telegram</div>
    <div class="card card-pad">
      <p style="font-size:12.5px; color:var(--text-dim); margin-top:0;">
        Сюди прийдуть сповіщення: аварія обладнання, критично низький залишок на складі.
      </p>
      <details style="margin-bottom:14px;">
        <summary style="cursor:pointer; font-size:12.5px; color:var(--accent-2);">Як створити бота (одна хвилина, покроково)</summary>
        <ol style="font-size:12px; color:var(--text-dim); margin:8px 0 0; padding-left:18px;">
          <li>У Telegram знайди <b style="color:var(--text);">@BotFather</b>, напиши йому <code>/newbot</code></li>
          <li>Дай боту назву й унікальне ім'я (має закінчуватись на "bot")</li>
          <li>BotFather дасть токен виду <code>123456789:AAExAmPl3-Token</code> — встав його в поле нижче</li>
          <li>Якщо хочеш отримувати сповіщення особисто — знайди свого щойно створеного бота в Telegram і напиши йому будь-що (напр. <code>/start</code>)</li>
          <li>Якщо хочеш сповіщення в канал/групу — додай бота туди адміністратором</li>
          <li>Натисни «Зберегти» нижче, потім «Знайти chat_id автоматично»</li>
        </ol>
      </details>
      <form method="post" action="{{ url_for('settings_save') }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="form" value="telegram">
        <div class="field" style="margin-bottom:10px;">
          <label>Токен бота (від @BotFather)</label>
          <input type="text" name="telegram_bot_token" value="{{ settings.get('telegram_bot_token','') }}" placeholder="123456789:AAExAmPl3-Token">
        </div>
        <div class="field" style="margin-bottom:10px;">
          <label>Chat ID (кому слати) або @назва_каналу</label>
          <input type="text" name="telegram_chat_id" id="telegram_chat_id_input" value="{{ settings.get('telegram_chat_id','') }}" placeholder="напр. 123456789 або @moy_kanal">
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
          <button class="btn btn-primary btn-sm" type="submit">Зберегти</button>
          <button class="btn btn-sm" type="submit" name="test" value="1">Надіслати тест</button>
        </div>
      </form>
      <form method="post" action="{{ url_for('settings_telegram_detect_chats') }}" style="margin-top:10px;" onsubmit="var t=document.querySelector('[name=telegram_bot_token]'); if(t){var h=document.createElement('input'); h.type='hidden'; h.name='telegram_bot_token'; h.value=t.value; this.appendChild(h);}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <button class="btn btn-sm" type="submit">🔍 Знайти chat_id автоматично</button>
      </form>
      {% if detected_chats is not none %}
      <div style="margin-top:12px; padding:10px; background:var(--bg-soft); border-radius:8px;">
        {% if detected_chats %}
        <div style="font-size:11.5px; color:var(--text-dim); margin-bottom:6px;">Знайдено — клікни, щоб підставити:</div>
        {% for ch in detected_chats %}
        <button type="button" class="btn btn-sm" style="margin:2px;" onclick="document.getElementById('telegram_chat_id_input').value='{{ ch.chat_id }}'">{{ ch.title }} ({{ ch.chat_id }})</button>
        {% endfor %}
        {% endif %}
      </div>
      {% endif %}
    </div>

    <div class="section-title"><span class="accent-dot"></span> 💬 Вебхук (Slack / Zapier / Make)</div>
    <div class="card card-pad">
      <p style="font-size:12px; color:var(--text-dim); margin-top:0;">
        Альтернатива Telegram — для тих, хто вже користується Slack чи автоматизаціями Zapier/Make.
        Формат сумісний зі Slack Incoming Webhooks.
      </p>
      <form method="post" action="{{ url_for('settings_save') }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="form" value="webhook">
        <div class="field" style="margin-bottom:10px;">
          <label>URL вебхука</label>
          <input type="text" name="webhook_url" value="{{ settings.get('webhook_url','') }}" placeholder="https://hooks.slack.com/services/...">
        </div>
        <div style="display:flex; gap:8px;">
          <button class="btn btn-sm btn-primary" type="submit">Зберегти</button>
          <button class="btn btn-sm" type="submit" name="test" value="1">Надіслати тест</button>
        </div>
      </form>
    </div>

    <div class="section-title"><span class="accent-dot"></span> 🤖 ШІ-розрахунок вартості</div>
    <div class="card card-pad">
      <p style="font-size:12.5px; color:var(--text-dim); margin-top:0;">
        Дозволяє в калькуляторі описати деталь звичайним текстом (напр. «вал зі сталі 40Х, Ø30х250мм,
        точіння + фрезерування паза, партія 50 шт») або додати фото/скан креслення — ШІ сам оцінить параметри й порахує вартість.
        Потрібен власний API-ключ Anthropic (<a href="https://console.anthropic.com/settings/keys" target="_blank" style="color:var(--accent-2);">console.anthropic.com</a>).
      </p>
      <form method="post" action="{{ url_for('settings_save') }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="form" value="anthropic">
        <div class="field" style="margin-bottom:10px;">
          <label>API-ключ Anthropic</label>
          <input type="password" name="anthropic_api_key" value="{{ settings.get('anthropic_api_key','') }}" placeholder="sk-ant-...">
        </div>
        <button class="btn btn-sm btn-primary" type="submit">Зберегти</button>
      </form>
      <p style="font-size:11px; color:var(--text-dim); margin-top:10px; margin-bottom:0;">
        ⚠ Розрахунок від ШІ — орієнтовна оцінка. Завжди перевіряй цифри перед тим, як ставити їх у КП клієнту.
      </p>
    </div>

    <div class="section-title"><span class="accent-dot"></span> 🧩 Додаткові поля</div>
    <div class="card card-pad">
      <p style="font-size:12.5px; color:var(--text-dim);">Додай власні поля до карток клієнтів та угод без правок коду.</p>
      <a class="btn btn-sm" href="{{ url_for('custom_fields_manage') }}">Керувати полями</a>
    </div>

    <div class="section-title"><span class="accent-dot"></span> 💱 Курси валют (до UAH)</div>
    <form method="post" action="{{ url_for('settings_save') }}" class="card card-pad"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <input type="hidden" name="form" value="rates">
      <div class="form-grid">
        <div class="field"><label>USD → UAH</label><input type="number" step="0.01" name="rate_usd" value="{{ settings.get('rate_usd','41.5') }}"></div>
        <div class="field"><label>EUR → UAH</label><input type="number" step="0.01" name="rate_eur" value="{{ settings.get('rate_eur','45.0') }}"></div>
        <div class="field"><label>PLN → UAH</label><input type="number" step="0.01" name="rate_pln" value="{{ settings.get('rate_pln','10.5') }}"></div>
      </div>
      <p style="font-size:12px; color:var(--text-dim);">Використовується для зведеної аналітики у гривні. Історія курсів зберігається, тому минулі місяці не змінюються при новому курсі.
        Оновлено: {{ settings.get('rates_updated_on') or 'ще не оновлювались' }}{% if settings.get('rates_source') %} ({{ settings.get('rates_source') }}){% endif %}.</p>
      <label style="font-size:12.5px; display:flex; gap:8px; align-items:center; margin-bottom:10px;"><input type="checkbox" name="rates_auto" value="1" {{ 'checked' if settings.get('rates_auto')=='1' }}> Оновлювати курс з НБУ щодня автоматично</label>
      <button class="btn btn-primary btn-sm" type="submit">Зберегти курси</button>
    </form>
    <form method="post" action="{{ url_for('settings_rates_nbu') }}" style="margin-top:8px;"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <button class="btn btn-sm" type="submit">Оновити зараз з НБУ</button>
    </form>

    <div class="section-title"><span class="accent-dot"></span> 🔔 Сповіщення про події</div>
    <form method="post" action="{{ url_for('settings_save') }}" class="card card-pad" id="events-form"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <input type="hidden" name="form" value="events">
      <label style="font-size:12.5px; display:flex; gap:8px; align-items:center; margin-bottom:8px;"><input type="checkbox" name="notify_events" value="1" {{ 'checked' if settings.get('notify_events','1')=='1' }}> Повідомляти в Telegram/вебхук про нові угоди та зміну етапу</label>
      <label style="font-size:12.5px; display:flex; gap:8px; align-items:center; margin-bottom:8px;"><input type="checkbox" name="notify_overdue" value="1" {{ 'checked' if settings.get('notify_overdue','1')=='1' }}> Щодня нагадувати про прострочені оплати</label>
      <div class="field"><label>Повторні замовлення: нагадувати, якщо клієнт мовчить, днів</label>
        <input type="number" min="7" name="reorder_days" value="{{ settings.get('reorder_days','60') }}"></div>
      <p style="font-size:12px; color:var(--text-dim);">Аварії верстатів і низькі залишки надсилаються завжди (якщо налаштовано Telegram чи вебхук).</p>
      <button class="btn btn-sm" type="submit">Зберегти</button>
    </form>

    <div class="section-title"><span class="accent-dot"></span> 💾 Резервне копіювання</div>
    <div class="card card-pad">
      <p style="font-size:12.5px; color:var(--text-dim);">
        Копія створюється безпечно через вбудований механізм SQLite, у файл
        <code>backups/crm_backup_ДАТА.db</code>. Зберігаються останні 30 копій.
      </p>
      <form method="post" action="{{ url_for('settings_backup_now') }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <button class="btn btn-sm btn-primary" type="submit">Створити резервну копію зараз</button>
      </form>
      <form method="post" action="{{ url_for('settings_save') }}" id="backup-auto-form" style="margin-top:14px;"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="form" value="backup">
        <label style="font-size:12.5px; display:flex; gap:8px; align-items:center; margin-bottom:10px;"><input type="checkbox" name="backup_auto" value="1" {{ 'checked' if settings.get('backup_auto','1')=='1' }}> Робити копію автоматично раз на день</label>
        <div class="field"><label>Додаткова тека для копій (флешка чи мережевий диск, необов'язково)</label>
          <input type="text" name="backup_extra_dir" value="{{ settings.get('backup_extra_dir','') }}" placeholder="напр. E:/Backups"></div>
        <p style="font-size:12px; color:var(--text-dim);">Остання автоматична копія: {{ settings.get('backup_last_on') or 'ще не було' }}.
          {% if settings.get('backup_last_error') %}<span style="color:var(--red);">Помилка додаткової теки: {{ settings.get('backup_last_error') }}</span>{% endif %}</p>
        <button class="btn btn-sm" type="submit">Зберегти</button>
      </form>
    </div>

    <div class="section-title"><span class="accent-dot" style="background:var(--red, #c23b2f);"></span> ⚠️ Скинути до демо-даних</div>
    <div class="card card-pad" style="border-color:#c23b2f33;">
      <p style="font-size:12.5px; color:var(--text-dim);">
        Повністю очищує базу і наповнює її заново актуальним демо-набором (клієнти, угоди, виробництво, склад тощо) —
        тим самим, що й при першому встановленні. Використовуйте після оновлення версії, щоб побачити нові демо-дані.
        <b>Усі наявні записи буде видалено</b> (перед цим автоматично створюється резервна копія в <code>backups/</code>).
      </p>
      <form method="post" action="{{ url_for('settings_reset_demo_data') }}" onsubmit="return confirm('Точно скинути базу і втратити всі поточні дані? Резервна копія буде створена автоматично.');">
        <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <div class="field" style="max-width:280px; margin-bottom:10px;">
          <label>Введіть «СКИНУТИ» для підтвердження</label>
          <input type="text" name="confirm" placeholder="СКИНУТИ" autocomplete="off">
        </div>
        <button class="btn btn-sm" style="background:#c23b2f; color:#fff;" type="submit">Скинути й наповнити демо-даними</button>
      </form>
    </div>
  </div>

  <div>
    <div class="section-title" style="margin-top:0;"><span class="accent-dot"></span> 🖥 Інтеграція з обладнанням (телеметрія)</div>
    <div class="card card-pad">
      <p style="font-size:12.5px; color:var(--text-dim);">
        Будь-який контролер верстата чи цеховий шлюз може напряму слати статус роботи в CRM
        через простий HTTP API — без додаткового ПЗ на боці CRM.
      </p>
      <div class="field" style="margin-bottom:10px;">
        <label>API-ключ</label>
        <input type="text" readonly value="{{ settings.get('api_key','') }}" onclick="this.select();">
      </div>
      <form method="post" action="{{ url_for('settings_regenerate_key') }}" onsubmit="return confirm('Старий ключ перестане працювати. Продовжити?');"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <button class="btn btn-sm" type="submit">Згенерувати новий ключ</button>
      </form>
      <details style="margin-top:12px;">
        <summary style="cursor:pointer; font-size:12px; color:var(--accent-2);">Приклад запиту (будь-який контролер)</summary>
        <pre style="background:var(--bg-soft); padding:12px; border-radius:8px; font-size:11px; overflow-x:auto; white-space:pre-wrap; margin-top:8px;">curl -X POST {{ request.url_root }}api/v1/telemetry/1 \\
  -H "X-API-Key: {{ settings.get('api_key','') }}" \\
  -H "Content-Type: application/json" \\
  -d '{"status":"running","spindle_load_pct":72,"cycle_count":184}'</pre>
      </details>
      <form method="post" action="{{ url_for('telemetry_simulate') }}" style="margin-top:10px;"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        <button class="btn btn-sm" type="submit">Згенерувати тестову телеметрію та історію напрацювання</button>
      </form>
    </div>

    <div class="section-title"><span class="accent-dot"></span> ⚙ Підключення верстатів Haas (реальне обладнання)</div>
    <div class="card card-pad" style="font-size:12.5px; color:var(--text-dim);">
      <p>
        Верстати Haas із контролером <b style="color:var(--text);">NGC (з 2017–2018 р.)</b> вже вміють
        віддавати дані по мережі без додаткового заліза.
      </p>
      <details>
        <summary style="cursor:pointer; color:var(--accent-2);">Показати технічні деталі підключення</summary>
        <p style="margin-top:8px;"><b style="color:var(--text);">Спосіб 1 — Ethernet Q Commands (рекомендовано).</b>
        На пульті верстата: <span class="tag">SETTING → 143 → порт 5051</span>. Читає:</p>
        <ul style="margin:6px 0 10px 18px; padding:0;">
          <li><code>Q301</code> — сумарний час у русі (напрацювання)</li>
          <li><code>Q300</code> — час увімкненого стану</li>
          <li><code>Q500</code>/<code>Q402</code> — програма, статус, лічильник деталей</li>
        </ul>
        <p><b style="color:var(--text);">Спосіб 2 — MTConnect.</b> Той самий Setting 143 вмикає агент на порту 8082.</p>
        <p>Готові скрипти — у теці <code>integrations/</code> (<code>haas_q_adapter.py</code>,
        <code>haas_mtconnect_adapter.py</code>).</p>
        <p style="color:var(--yellow);">⚠ Перед бойовим використанням запустіть адаптер з <code>--debug</code>
        і звірте формат відповіді з вашою версією ПЗ контролера.</p>
      </details>
    </div>
  </div>
</div>
{% endblock %}
"""

app.jinja_loader = DictLoader({
    "base.html": BASE_HTML,
    "login.html": LOGIN_HTML,
    "dashboard.html": DASHBOARD_HTML,
    "clients_list.html": CLIENTS_LIST_HTML,
    "clients_import.html": CLIENTS_IMPORT_HTML,
    "client_form.html": CLIENT_FORM_HTML,
    "client_detail.html": CLIENT_DETAIL_HTML,
    "deals_board.html": DEALS_BOARD_HTML,
    "deal_form.html": DEAL_FORM_HTML,
    "calculator.html": CALCULATOR_HTML,
    "shift_log_list.html": SHIFT_LOG_LIST_HTML,
    "shift_log_form.html": SHIFT_LOG_FORM_HTML,
    "calculator_history.html": CALCULATOR_HISTORY_HTML,
    "calculator_history_detail.html": CALCULATOR_HISTORY_DETAIL_HTML,
    "deal_detail.html": DEAL_DETAIL_HTML,
    "deal_select_machine.html": DEAL_SELECT_MACHINE_HTML,
    "tasks.html": TASKS_HTML,
    "calendar.html": CALENDAR_HTML,
    "machines.html": MACHINES_HTML,
    "machine_form.html": MACHINE_FORM_HTML,
    "service.html": SERVICE_HTML,
    "service_detail.html": SERVICE_DETAIL_HTML,
    "search.html": SEARCH_HTML,
    "finance.html": FINANCE_HTML,
    "error.html": ERROR_HTML,
    "users.html": USERS_HTML,
    "my_profile.html": MY_PROFILE_HTML,
    "settings.html": SETTINGS_HTML,
    "custom_fields.html": CUSTOM_FIELDS_HTML,
    "production.html": PRODUCTION_HTML,
    "production_detail.html": PRODUCTION_DETAIL_HTML,
    "warehouse.html": WAREHOUSE_HTML,
    "scrap.html": SCRAP_HTML,
    "warehouse_item.html": WAREHOUSE_ITEM_HTML,
    "work_centers.html": WORK_CENTERS_HTML,
    "terminal.html": TERMINAL_HTML,
})

app.jinja_env.globals["dict"] = dict
app.jinja_env.globals["TASK_TYPES"] = TASK_TYPES
app.jinja_env.globals["SERVICE_STATUSES"] = SERVICE_STATUSES
app.jinja_env.globals["MACHINE_CATEGORIES"] = MACHINE_CATEGORIES
app.jinja_env.globals["LEAD_SOURCES"] = LEAD_SOURCES
app.jinja_env.globals["CURRENCIES"] = CURRENCIES
app.jinja_env.globals["ROLES"] = ROLES
app.jinja_env.globals["ROLE_LABEL"] = ROLE_LABEL
app.jinja_env.globals["PRODUCTION_ORDER_STATUSES"] = PRODUCTION_ORDER_STATUSES
app.jinja_env.globals["OPERATION_STATUSES"] = OPERATION_STATUSES
app.jinja_env.globals["WAREHOUSE_TX_TYPES"] = WAREHOUSE_TX_TYPES
app.jinja_env.globals["CUSTOM_FIELD_TYPES"] = CUSTOM_FIELD_TYPES
app.jinja_env.globals["SCRAP_STATUSES"] = SCRAP_STATUSES

# ---------------------------------------------------------------------------
# Маршрути: авторизація
# ---------------------------------------------------------------------------

# Типові паролі демо-користувачів із seed_data() - лише для нагадування змінити їх.
DEFAULT_PASSWORDS = {
    "admin": "admin123", "oleh": "manager123", "maister": "prod123", "tokar": "tokar123", "frezer": "frezer123",
    "sklad": "sklad123", "servis": "servis123", "buh": "buh12345",
}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        throttle_key = _login_throttle_key()
        if is_login_locked_out(throttle_key):
            flash(f"Забагато невдалих спроб входу. Спробуйте знову через {LOGIN_LOCKOUT_SECONDS // 60} хв.", "error")
            return render_template("login.html")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username=? AND active=1",
                           (request.form.get("username", "").strip(),)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            # Якщо людина зайшла з типовим демо-паролем - показуємо на всіх
            # сторінках помітне нагадування змінити його (особливо важливо,
            # коли CRM доступна з інших комп'ютерів чи через VPN). Прапорець
            # виставляється тут, де відомий відкритий пароль, - так не треба
            # щоразу перераховувати хеші на кожному запиті.
            if DEFAULT_PASSWORDS.get(user["username"]) == request.form.get("password", ""):
                session["default_password"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        register_failed_login(throttle_key)
        flash("Невірний логін або пароль", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/finance")
@login_required
@role_required("accountant")
def finance_view():
    """Окрема сторінка для ролі 'Бухгалтер': усі платежі по всіх угодах,
    без доступу до решти CRM (клієнтські відносини, виробництво, склад).
    Бухгалтеру потрібні гроші й статуси оплат, а не воронка продажів."""
    db = get_db()
    status_filter = request.args.get("status", "")
    q = """SELECT p.*, d.title as deal_title, d.id as deal_id, c.name as client_name
           FROM payments p LEFT JOIN deals d ON d.id=p.deal_id
           LEFT JOIN clients c ON c.id=d.client_id WHERE 1=1"""
    params = []
    if status_filter:
        q += " AND p.status=?"
        params.append(status_filter)
    q += " ORDER BY p.due_date IS NULL, p.due_date"
    payments = db.execute(q, params).fetchall()

    totals = db.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN status='paid' THEN amount END),0) as paid_total,
             COALESCE(SUM(CASE WHEN status='pending' THEN amount END),0) as pending_total
           FROM payments"""
    ).fetchone()

    overdue_count = db.execute(
        "SELECT COUNT(*) c FROM payments WHERE status='pending' AND due_date IS NOT NULL AND due_date < ?",
        (today_str(),),
    ).fetchone()["c"]

    return render_template("finance.html", active="finance", payments=payments, totals=totals,
                            status_filter=status_filter, overdue_count=overdue_count)


@app.route("/")
@login_required
def dashboard():
    db = get_db()
    scope, scope_params = manager_scope_sql("manager_id")

    def amount_uah_rows(rows):
        """Конвертує суму кожної угоди в гривню за курсом з налаштувань перед
        підсумовуванням — раніше тут просто складались 'сирі' суми в різних
        валютах (USD/EUR/UAH), що давало математично некоректний підсумок."""
        return sum(to_uah(db, r["amount"], r["currency"]) for r in rows)

    open_deal_rows = db.execute(
        f"SELECT amount, currency FROM deals WHERE stage NOT IN ('won','lost'){scope}", scope_params
    ).fetchall()
    won_deal_rows = db.execute(
        f"SELECT amount, currency FROM deals WHERE stage='won'{scope}", scope_params
    ).fetchall()
    open_deals, open_amount = len(open_deal_rows), amount_uah_rows(open_deal_rows)
    won_deals, won_amount = len(won_deal_rows), amount_uah_rows(won_deal_rows)
    lost_count = db.execute(
        f"SELECT COUNT(*) c FROM deals WHERE stage='lost'{scope}", scope_params
    ).fetchone()["c"]

    funnel = {}
    for row in db.execute(f"SELECT stage, COUNT(*) c FROM deals WHERE 1=1{scope} GROUP BY stage", scope_params):
        funnel[row["stage"]] = row["c"]
    max_funnel = max([funnel.get(k, 0) for k, _, _ in STAGES] + [1])

    conversion = 0
    denom = won_deals + lost_count
    if denom:
        conversion = round(won_deals / denom * 100)

    overdue_scope, overdue_params = manager_scope_sql("manager_id")
    overdue = db.execute(
        f"SELECT COUNT(*) c FROM tasks WHERE done=0 AND due_date IS NOT NULL AND due_date < ?{overdue_scope}",
        [today_str()] + overdue_params,
    ).fetchone()["c"]

    upcoming_tasks = db.execute(
        f"""SELECT t.*, c.name as client_name, u.full_name as manager_name
           FROM tasks t LEFT JOIN clients c ON c.id=t.client_id
           LEFT JOIN users u ON u.id=t.manager_id
           WHERE t.done=0 {overdue_scope.replace('manager_id','t.manager_id')}
           ORDER BY (t.due_date IS NULL), t.due_date LIMIT 6""",
        overdue_params,
    ).fetchall()

    client_deal_rows = db.execute(
        f"""SELECT c.id, c.name, d.amount, d.currency FROM clients c JOIN deals d ON d.client_id=c.id
           WHERE 1=1 {scope.replace('manager_id','d.manager_id')}""",
        scope_params,
    ).fetchall()
    client_totals = {}
    for r in client_deal_rows:
        client_totals.setdefault(r["id"], {"name": r["name"], "total": 0.0})
        client_totals[r["id"]]["total"] += to_uah(db, r["amount"], r["currency"])
    top_clients = sorted(
        [{"id": cid, "name": v["name"], "total": v["total"]} for cid, v in client_totals.items()],
        key=lambda x: x["total"], reverse=True,
    )[:5]

    # Виручка по місяцях (останні 6 місяців) за виграними угодами, у гривні
    revenue_rows = db.execute(
        f"""SELECT strftime('%Y-%m', closed_at) as ym, amount, currency, closed_at
            FROM deals WHERE stage='won' AND closed_at IS NOT NULL {scope}""",
        scope_params,
    ).fetchall()
    rev_map = {}
    for r in revenue_rows:
        rev_map[r["ym"]] = rev_map.get(r["ym"], 0.0) + to_uah(db, r["amount"], r["currency"], r["closed_at"])
    cursor_dt = datetime.date.today().replace(day=1)
    tmp = []
    for i in range(6):
        tmp.append(cursor_dt)
        prev_month = cursor_dt.month - 1 or 12
        prev_year = cursor_dt.year - 1 if cursor_dt.month == 1 else cursor_dt.year
        cursor_dt = cursor_dt.replace(year=prev_year, month=prev_month)
    tmp.reverse()
    revenue_by_month = [{"label": dtm.strftime("%m.%Y"), "value": rev_map.get(dtm.strftime("%Y-%m"), 0)} for dtm in tmp]
    max_revenue = max([m["value"] for m in revenue_by_month] + [1])

    # Рейтинг менеджерів (видно тільки адміну), суми — у гривні
    leaderboard = []
    if is_admin():
        mgr_rows = db.execute(
            """SELECT u.id, u.full_name,
                      d.stage, d.amount, d.currency, d.closed_at
               FROM users u LEFT JOIN deals d ON d.manager_id=u.id
               WHERE u.active=1"""
        ).fetchall()
        mgr_stats = {}
        for r in mgr_rows:
            mgr_stats.setdefault(r["id"], {"full_name": r["full_name"], "won_cnt": 0, "won_sum": 0.0,
                                            "open_cnt": 0, "lost_cnt": 0})
            st = mgr_stats[r["id"]]
            if r["stage"] == "won":
                st["won_cnt"] += 1
                st["won_sum"] += to_uah(db, r["amount"], r["currency"], r["closed_at"])
            elif r["stage"] == "lost":
                st["lost_cnt"] += 1
            elif r["stage"] is not None:
                st["open_cnt"] += 1
        leaderboard = sorted(mgr_stats.values(), key=lambda x: x["won_sum"], reverse=True)

    recent_lost = db.execute(
        f"""SELECT d.*, c.name as client_name FROM deals d LEFT JOIN clients c ON c.id=d.client_id
            WHERE d.stage='lost' {scope.replace('manager_id','d.manager_id')} ORDER BY d.updated_at DESC LIMIT 5""",
        scope_params,
    ).fetchall()

    activity_feed = db.execute(
        """SELECT a.*, u.full_name as manager_name, c.name as client_name, d.title as deal_title
           FROM activities a LEFT JOIN users u ON u.id=a.manager_id
           LEFT JOIN clients c ON c.id=a.client_id LEFT JOIN deals d ON d.id=a.deal_id
           ORDER BY a.created_at DESC LIMIT 8"""
    ).fetchall()

    # Виробничий блок "Змінний журнал за сьогодні" - додається ПОРУЧ із
    # воронкою продажів (не замінює її), на явний запит користувача:
    # видно адміну й ролі "Виробництво", бо саме вони вносять ці записи.
    shift_today = None
    shift_by_wc_today = []
    cur_user = get_current_user()
    if cur_user and cur_user["role"] in ("admin", "production"):
        shift_today = db.execute(
            """SELECT COALESCE(SUM(quantity_made),0) made, COALESCE(SUM(quantity_scrap),0) scrap,
                      COUNT(*) entries FROM shift_logs WHERE work_date=?""",
            (today_str(),),
        ).fetchone()
        shift_by_wc_today = db.execute(
            """SELECT wc.name AS work_center_name, SUM(sl.quantity_made) made, SUM(sl.quantity_scrap) scrap
               FROM shift_logs sl LEFT JOIN work_centers wc ON wc.id=sl.work_center_id
               WHERE sl.work_date=? GROUP BY sl.work_center_id ORDER BY made DESC""",
            (today_str(),),
        ).fetchall()

    return render_template(
        "dashboard.html", active="dashboard",
        open_deals=open_deals, open_amount=open_amount,
        won_deals=won_deals, won_amount=won_amount,
        conversion=conversion, overdue_tasks=overdue,
        funnel=funnel, max_funnel=max_funnel,
        upcoming_tasks=upcoming_tasks, top_clients=top_clients,
        revenue_by_month=revenue_by_month, max_revenue=max_revenue,
        leaderboard=leaderboard, recent_lost=recent_lost,
        activity_feed=activity_feed,
        shift_today=shift_today, shift_by_wc_today=shift_by_wc_today,
    )


# ---------------------------------------------------------------------------
# Клієнти
# ---------------------------------------------------------------------------

@app.route("/clients")
@login_required
@role_required('sales')
def clients_list():
    db = get_db()
    status = request.args.get("status", "")
    search_q = request.args.get("q", "").strip()
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = 50
    q = "SELECT c.*, u.full_name as manager_name FROM clients c LEFT JOIN users u ON u.id=c.manager_id WHERE 1=1"
    params = []
    if status:
        q += " AND c.status=?"
        params.append(status)
    if search_q:
        like = f"%{search_q}%"
        q += " AND (c.name LIKE ? OR c.city LIKE ? OR c.phone LIKE ? OR c.email LIKE ?)"
        params += [like, like, like, like]
    scope, scope_params = manager_scope_sql("c.manager_id")
    q += scope
    params += scope_params
    count_row = db.execute(q.replace("SELECT c.*, u.full_name as manager_name", "SELECT COUNT(*) as cnt"), params).fetchone()
    total = count_row["cnt"]
    total_pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    q += " ORDER BY c.name LIMIT ? OFFSET ?"
    clients = db.execute(q, params + [per_page, (page - 1) * per_page]).fetchall()
    return render_template("clients_list.html", active="clients", clients=clients,
                            page=page, total_pages=total_pages, total=total)


@app.route("/clients/new", methods=["GET", "POST"])
@login_required
@role_required('sales')
def client_new():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            name = validate_required(f, "name", "Назва компанії")
            email = validate_email_field(f)
        except ValidationError as e:
            flash(str(e), "error")
            return render_template("client_form.html", active="clients", client=None, managers=get_managers(),
                                    custom_fields=[{"def": d, "value": None} for d in get_custom_field_defs("client")])
        db.execute(
            """INSERT INTO clients (name, edrpou, industry, city, address, phone, email, website,
               source, manager_id, status, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, f.get("edrpou"), f.get("industry"), f.get("city"), f.get("address"),
             f.get("phone"), email, f.get("website"), f.get("source"),
             f.get("manager_id") or None, f.get("status", "active"), f.get("notes"), now_iso()),
        )
        new_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        save_custom_field_values("client", new_id, f)
        db.commit()
        flash("Клієнта створено", "success")
        return redirect(url_for("clients_list"))
    custom_fields = [{"def": d, "value": None} for d in get_custom_field_defs("client")]
    return render_template("client_form.html", active="clients", client=None, managers=get_managers(),
                            custom_fields=custom_fields)


@app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
@role_required('sales')
def client_edit(client_id):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            name = validate_required(f, "name", "Назва компанії")
            email = validate_email_field(f)
        except ValidationError as e:
            flash(str(e), "error")
            return redirect(url_for("client_edit", client_id=client_id))
        db.execute(
            """UPDATE clients SET name=?, edrpou=?, industry=?, city=?, address=?, phone=?, email=?,
               website=?, source=?, manager_id=?, status=?, notes=? WHERE id=?""",
            (name, f.get("edrpou"), f.get("industry"), f.get("city"), f.get("address"),
             f.get("phone"), email, f.get("website"), f.get("source"),
             f.get("manager_id") or None, f.get("status", "active"), f.get("notes"), client_id),
        )
        save_custom_field_values("client", client_id, f)
        db.commit()
        flash("Дані клієнта оновлено", "success")
        return redirect(url_for("client_detail", client_id=client_id))
    custom_fields = get_custom_field_values("client", client_id)
    return render_template("client_form.html", active="clients", client=client, managers=get_managers(),
                            custom_fields=custom_fields)


@app.route("/clients/<int:client_id>")
@login_required
@role_required('sales')
def client_detail(client_id):
    db = get_db()
    client = db.execute(
        """SELECT c.*, u.full_name as manager_name FROM clients c
           LEFT JOIN users u ON u.id=c.manager_id WHERE c.id=?""", (client_id,)
    ).fetchone()
    if not client:
        abort(404)
    contacts = db.execute("SELECT * FROM contacts WHERE client_id=? ORDER BY is_primary DESC", (client_id,)).fetchall()
    deals = db.execute("SELECT * FROM deals WHERE client_id=? ORDER BY created_at DESC", (client_id,)).fetchall()
    tickets = db.execute("SELECT * FROM service_tickets WHERE client_id=? ORDER BY created_at DESC", (client_id,)).fetchall()
    attachments = get_attachments("client", client_id)
    custom_fields = get_custom_field_values("client", client_id)
    return render_template("client_detail.html", active="clients", client=client,
                            contacts=contacts, deals=deals, tickets=tickets,
                            attachments=attachments, custom_fields=custom_fields)


@app.route("/clients/<int:client_id>/delete", methods=["POST"])
@login_required
@role_required('sales')
def client_delete(client_id):
    db = get_db()
    cascade_delete_client(db, client_id)
    db.commit()
    flash("Клієнта та всі пов'язані дані видалено", "success")
    return redirect(url_for("clients_list"))


@app.route("/clients/<int:client_id>/contacts", methods=["POST"])
@login_required
@role_required('sales')
def contact_add(client_id):
    db = get_db()
    f = request.form
    try:
        full_name = validate_required(f, "full_name", "ПІБ")
    except ValidationError as e:
        flash(str(e), "error")
        return redirect(url_for("client_detail", client_id=client_id))
    db.execute(
        "INSERT INTO contacts (client_id, full_name, position, phone, email, is_primary) VALUES (?,?,?,?,?,0)",
        (client_id, full_name, f.get("position"), f.get("phone"), f.get("email")),
    )
    db.commit()
    flash("Контакт додано", "success")
    return redirect(url_for("client_detail", client_id=client_id))


# ---------------------------------------------------------------------------
# Угоди
# ---------------------------------------------------------------------------

def deal_row_to_dict(db, deal_id):
    return db.execute(
        """SELECT d.*, c.name as client_name, u.full_name as manager_name,
                  m.model as machine_name
           FROM deals d
           LEFT JOIN clients c ON c.id=d.client_id
           LEFT JOIN users u ON u.id=d.manager_id
           LEFT JOIN machines m ON m.id=d.machine_id
           WHERE d.id=?""", (deal_id,)
    ).fetchone()


@app.route("/deals")
@login_required
@role_required('sales')
def deals_board():
    db = get_db()
    manager_id = request.args.get("manager_id", "")
    q = """SELECT d.*, c.name as client_name, u.full_name as manager_name
           FROM deals d LEFT JOIN clients c ON c.id=d.client_id
           LEFT JOIN users u ON u.id=d.manager_id WHERE 1=1"""
    params = []
    if manager_id and is_admin():
        q += " AND d.manager_id=?"
        params.append(manager_id)
    scope, scope_params = manager_scope_sql("d.manager_id")
    q += scope
    params += scope_params
    q += " ORDER BY d.updated_at DESC"
    deals = db.execute(q, params).fetchall()
    columns = {k: [] for k, _, _ in STAGES}
    columns[LOST_STAGE] = []
    sums = {}
    for dl in deals:
        columns.setdefault(dl["stage"], []).append(dl)
        sums[dl["stage"]] = sums.get(dl["stage"], 0) + (dl["amount"] or 0)
    return render_template("deals_board.html", active="deals", columns=columns, sums=sums,
                            managers=get_managers())


@app.route("/calculator", methods=["GET", "POST"])
@login_required
@role_required("sales")
def cost_calculator():
    """Параметричний калькулятор вартості токарно-фрезерної механообробки:
    матеріал заготовки, токарна обробка, фрезерна обробка (фрезерування/
    свердління/нарізання різьби), додаткова обробка (шліфування, покриття),
    наладка верстата на партію.

    Контур деталі та габарити для фрезерування плоских деталей можна ввести
    вручну АБО отримати автоматично, завантаживши DXF-креслення (analyze_dxf).
    STEP свідомо не підтримується — потрібне повноцінне 3D CAD-ядро, це
    окрема інженерна задача.

    Додатково (для деталей з плоского/листового прокату, що йдуть під
    фрезерування) рахує, скільки заготовок матеріалу потрібно на партію
    деталей (площа деталі + розмір стандартної заготовки + коефіцієнт
    розкрою) і звіряє з залишком на складі."""
    result = None
    if request.method == "POST":
        f = request.form
        try:
            weight_kg = parse_number(f, "weight_kg", default=0, min_value=0, label="Вага заготовки")
            material_price = parse_number(f, "material_price_per_kg", default=0, min_value=0, label="Ціна металу за кг")
            turning_hours = parse_number(f, "turning_hours", default=0, min_value=0, label="Час токарної обробки")
            turning_rate = parse_number(f, "turning_rate_per_hour", default=0, min_value=0, label="Ставка токарної обробки за годину")
            milling_hours = parse_number(f, "milling_hours", default=0, min_value=0, label="Час фрезерної обробки")
            milling_rate = parse_number(f, "milling_rate_per_hour", default=0, min_value=0, label="Ставка фрезерної обробки за годину")
            machine_hours = parse_number(f, "machine_hours", default=0, min_value=0, label="Машино-години додаткової обробки")
            machine_rate = parse_number(f, "machine_rate_per_hour", default=0, min_value=0, label="Ставка за машино-годину додаткової обробки")
            setup_cost_total = parse_number(f, "setup_cost_total", default=0, min_value=0, label="Вартість наладки на партію")
            quantity = parse_number(f, "quantity", default=1, min_value=1, label="Кількість деталей")
            margin_pct = parse_number(f, "margin_pct", default=20, min_value=0, label="Націнка, %")
            part_area_m2 = parse_number(f, "part_area_m2", default=0, min_value=0, label="Площа деталі")
            sheet_width_mm = parse_number(f, "sheet_width_mm", default=0, min_value=0, label="Ширина заготовки")
            sheet_length_mm = parse_number(f, "sheet_length_mm", default=0, min_value=0, label="Довжина заготовки")
            nesting_pct = parse_number(f, "nesting_pct", default=75, min_value=1, max_value=100, label="Коефіцієнт розкрою")
        except ValidationError as e:
            flash(str(e), "error")
            return render_template("calculator.html", active="calculator", result=None, dxf_result=None, warehouse_items=[])

        material_cost = weight_kg * material_price
        turning_cost = turning_hours * turning_rate
        milling_cost = milling_hours * milling_rate
        machine_cost = machine_hours * machine_rate
        setup_cost_per_unit = (setup_cost_total / quantity) if quantity else 0
        cost_per_unit = material_cost + turning_cost + milling_cost + machine_cost + setup_cost_per_unit
        price_per_unit = cost_per_unit * (1 + margin_pct / 100)
        total_price = price_per_unit * quantity

        result = {
            "material_cost": round(material_cost, 2),
            "turning_cost": round(turning_cost, 2),
            "milling_cost": round(milling_cost, 2),
            "machine_cost": round(machine_cost, 2),
            "setup_cost_total": round(setup_cost_total, 2),
            "setup_cost_per_unit": round(setup_cost_per_unit, 2),
            "cost_per_unit": round(cost_per_unit, 2),
            "price_per_unit": round(price_per_unit, 2),
            "quantity": quantity,
            "total_price": round(total_price, 2),
            "margin_pct": margin_pct,
        }

        # Розрахунок кількості заготовок матеріалу на партію (якщо вказано розмір заготовки й площу деталі)
        material_calc = None
        if part_area_m2 > 0 and sheet_width_mm > 0 and sheet_length_mm > 0:
            sheet_area_m2 = (sheet_width_mm * sheet_length_mm) / 1_000_000
            usable_area_m2 = sheet_area_m2 * (nesting_pct / 100)
            parts_per_sheet = int(usable_area_m2 // part_area_m2) if part_area_m2 else 0
            if parts_per_sheet > 0:
                sheets_needed = -(-int(quantity) // parts_per_sheet)  # округлення вгору без math.ceil
                total_material_area_m2 = round(sheets_needed * sheet_area_m2, 3)
                total_weight_kg = round(weight_kg * quantity, 2) if weight_kg else None
                material_calc = {
                    "sheet_area_m2": round(sheet_area_m2, 3),
                    "parts_per_sheet": parts_per_sheet,
                    "sheets_needed": sheets_needed,
                    "total_material_area_m2": total_material_area_m2,
                    "total_weight_kg": total_weight_kg,
                }
            else:
                material_calc = {"error": "Деталь не вміщується на лист із заданим коефіцієнтом розкрою"}
        result["material_calc"] = material_calc

        # Звірка з залишками матеріалу на складі (за назвою позиції, якщо вказано)
        stock_check = None
        stock_item_id = f.get("stock_item_id")
        if stock_item_id and material_calc and material_calc.get("total_weight_kg"):
            db = get_db()
            item = db.execute("SELECT * FROM warehouse_items WHERE id=?", (stock_item_id,)).fetchone()
            if item:
                needed = material_calc["total_weight_kg"]
                stock_check = {
                    "item_name": item["name"], "unit": item["unit"],
                    "available": item["qty_on_hand"], "needed": needed,
                    "enough": item["qty_on_hand"] >= needed,
                }
        result["stock_check"] = stock_check
        save_calculation_history(
            get_db(), get_current_user(), "manual",
            f.get("part_description") or "Ручний розрахунок (без опису деталі)", result,
            raw_inputs={
                "weight_kg": weight_kg, "material_price_per_kg": material_price,
                "turning_hours": turning_hours, "turning_rate_per_hour": turning_rate,
                "milling_hours": milling_hours, "milling_rate_per_hour": milling_rate,
                "machine_hours": machine_hours, "machine_rate_per_hour": machine_rate,
            },
        )

    db = get_db()
    warehouse_items = db.execute(
        "SELECT * FROM warehouse_items WHERE category LIKE '%атеріал%' ORDER BY name"
    ).fetchall()
    return render_template("calculator.html", active="calculator", result=result, dxf_result=None,
                            warehouse_items=warehouse_items)


def _calculator_history_filters():
    """Будує WHERE-умову й параметри для списку історії розрахунків із
    query-параметрів ?source=&q= - спільне для сторінки /calculator/history
    і експорту /export/calculations.xlsx, щоб експорт завжди вивантажував
    РІВНО те, що бачить користувач на екрані (з тим самим фільтром), а не
    щось інше."""
    source = request.args.get("source", "").strip()
    q = request.args.get("q", "").strip()
    clauses = []
    params = []
    if source in ("manual", "ai"):
        clauses.append("cc.source = ?")
        params.append(source)
    if q:
        clauses.append("cc.part_description LIKE ?")
        params.append(f"%{q}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params, source, q


@app.route("/calculator/history")
@login_required
@role_required("sales")
def calculator_history():
    """Список усіх раніше завершених розрахунків вартості (ручних і через
    ШІ) - кожен розрахунок з cost_calculator/calculator_ai_estimate
    автоматично потрапляє сюди через save_calculation_history. Видно всім
    з роллю sales (не лише свої) - це спільний інструмент відділу продажів,
    як і сам калькулятор."""
    db = get_db()
    where, params, source, q = _calculator_history_filters()
    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = 50

    total = db.execute(f"SELECT COUNT(*) c FROM cost_calculations cc {where}", params).fetchone()["c"]
    total_pages = max(1, -(-total // per_page))
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    rows = db.execute(
        f"SELECT cc.*, u.full_name AS manager_name FROM cost_calculations cc "
        f"LEFT JOIN users u ON u.id = cc.user_id {where} "
        f"ORDER BY cc.created_at DESC, cc.id DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    return render_template("calculator_history.html", active="calculator", rows=rows,
                            total=total, page=page, total_pages=total_pages, search_q=q)


@app.route("/calculator/history/<int:calc_id>")
@login_required
@role_required("sales")
def calculator_history_detail(calc_id):
    """Детальна картка одного збереженого розрахунку - повний опис
    техпроцесу (без обрізання, на відміну від рядка в таблиці) і всі вхідні
    параметри та результат, щоб менеджер міг перевірити розрахунок перед
    тим, як показати його клієнту чи керівнику."""
    db = get_db()
    r = db.execute(
        "SELECT cc.*, u.full_name AS manager_name FROM cost_calculations cc "
        "LEFT JOIN users u ON u.id = cc.user_id WHERE cc.id=?",
        (calc_id,),
    ).fetchone()
    if not r:
        abort(404)
    return render_template("calculator_history_detail.html", active="calculator", r=r)


@app.route("/calculator/history/<int:calc_id>/delete", methods=["POST"])
@login_required
@role_required("sales")
def calculator_history_delete(calc_id):
    """Видаляє один розрахунок з історії - остаточно (без кошика), тому
    підтвердження береться на формі (confirm()) перед відправкою. Після
    видалення повертає туди, звідки прийшли (зі сторінки списку - з тим
    самим фільтром/сторінкою; з картки деталей - на список)."""
    db = get_db()
    r = db.execute("SELECT id FROM cost_calculations WHERE id=?", (calc_id,)).fetchone()
    if not r:
        abort(404)
    db.execute("DELETE FROM cost_calculations WHERE id=?", (calc_id,))
    db.commit()
    flash("Розрахунок видалено з історії", "success")

    page = request.form.get("page", "").strip()
    source = request.form.get("source", "").strip()
    q = request.form.get("q", "").strip()
    if page:
        qs = []
        if page and page != "1":
            qs.append(f"page={page}")
        if source:
            qs.append(f"source={source}")
        if q:
            qs.append(f"q={q}")
        url = url_for("calculator_history")
        if qs:
            url += "?" + "&".join(qs)
        return redirect(url)
    return redirect(url_for("calculator_history"))


@app.route("/export/calculations.xlsx")
@login_required
@role_required("sales")
def export_calculations_xlsx():
    """Вивантажує історію розрахунків у Excel - з тим самим фільтром
    (джерело/пошук), що й на екрані /calculator/history, щоб можна було
    передати бухгалтерії чи керівнику повний список розрахунків за період
    без ручного переписування з екрана."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from flask import send_file

    db = get_db()
    where, params, source, q = _calculator_history_filters()
    rows = db.execute(
        f"SELECT cc.*, u.full_name AS manager_name FROM cost_calculations cc "
        f"LEFT JOIN users u ON u.id = cc.user_id {where} "
        f"ORDER BY cc.created_at DESC, cc.id DESC",
        params,
    ).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Розрахунки"
    headers = ["Дата", "Менеджер", "Джерело", "Деталь", "К-сть", "Вага, кг", "Матеріал, грн/кг",
               "Токарна, год", "Фрезерна, год", "Собівартість/од", "Ціна/од", "Націнка, %", "Разом, грн"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2A2E35")
    for r in rows:
        ws.append([
            r["created_at"], r["manager_name"], "ШІ" if r["source"] == "ai" else "ручний",
            r["part_description"], r["quantity"], r["weight_kg"], r["material_price_per_kg"],
            r["turning_hours"], r["milling_hours"], r["cost_per_unit"], r["price_per_unit"],
            r["margin_pct"], r["total_price"],
        ])
    for i, w in enumerate([18, 18, 10, 40, 8, 10, 14, 12, 12, 14, 12, 10, 14], start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="rozrahunky_vartosti.xlsx",
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/calculator/ai-estimate", methods=["POST"])
@login_required
@role_required("sales")
def calculator_ai_estimate():
    """Приймає довільний текстовий опис деталі й операцій та/або фото/скан
    креслення, викликає ШІ для оцінки параметрів калькулятора (з візуальним
    аналізом зображення, якщо воно є), одразу рахує собівартість тими ж
    формулами, що й ручний розрахунок (жодної окремої "ШІ-математики" -
    цифри для розрахунку однакові, різниться лише спосіб їх отримання).

    Опис може бути мінімальним: якщо ШІ не вистачає критичних даних
    (матеріал, габарити/вага), він замість вигаданих цифр ставить ОДНЕ
    уточнююче питання (див. ai_estimate_cost_params) - тоді ця функція
    зберігає стан розмови сервер-side (_AI_CALC_PENDING) під одноразовим
    токеном і показує форму з питанням; другий POST на цей же маршрут з
    ai_clarify_token/ai_clarify_answer продовжує ту саму розмову.

    Калькулятор об'єднаний в одну форму з ручним розрахунком: якщо
    користувач уже заповнив частину полів вручну (напр. кількість,
    матеріал), ці значення НІКОЛИ не перезаписуються відповіддю ШІ -
    ШІ лише заповнює те, що лишилось порожнім, і завжди пише
    operations_description.

    Після успішного розрахунку доступний міні-чат правок (ai_correction_token
    у result): якщо щось в оцінці ШІ виглядає не так, можна написати
    виправлення звичайною мовою, не повторюючи весь опис і не
    перезавантажуючи файл - ai_calc_apply_correction() продовжує ТУ САМУ
    розмову з ШІ."""
    db = get_db()
    api_key = get_setting(db, "anthropic_api_key", "")
    warehouse_items = db.execute(
        "SELECT * FROM warehouse_items WHERE category LIKE '%атеріал%' ORDER BY name"
    ).fetchall()

    clarify_token = request.form.get("ai_clarify_token", "").strip()
    correction_token = request.form.get("ai_correction_token", "").strip()
    if clarify_token:
        pending = _ai_calc_pending_pop(clarify_token)
        description = (pending or {}).get("description", "")
        known_fields = (pending or {}).get("known_fields", {})
        if pending is None:
            flash("Уточнення застаріло або вже використане — почни розрахунок заново", "error")
            return render_template("calculator.html", active="calculator", result=None, dxf_result=None,
                                    warehouse_items=warehouse_items, ai_description=description)
        answer = request.form.get("ai_clarify_answer", "")
        try:
            calc_result = ai_estimate_cost_params(
                None, api_key, continue_messages=pending["messages"], answer=answer
            )
        except AICalcError as e:
            flash(str(e), "error")
            return render_template("calculator.html", active="calculator", result=None, dxf_result=None,
                                    warehouse_items=warehouse_items, ai_description=description)
    elif correction_token:
        pending = _ai_calc_pending_pop(correction_token)
        description = (pending or {}).get("description", "")
        known_fields = (pending or {}).get("known_fields", {})
        if pending is None:
            flash("Сесія правок застаріла або вже використана — почни розрахунок заново", "error")
            return render_template("calculator.html", active="calculator", result=None, dxf_result=None,
                                    warehouse_items=warehouse_items, ai_description=description)
        correction_text = request.form.get("ai_correction_message", "")
        try:
            calc_result = ai_calc_apply_correction(pending["messages"], correction_text, api_key)
        except AICalcError as e:
            flash(str(e), "error")
            return render_template("calculator.html", active="calculator", result=None, dxf_result=None,
                                    warehouse_items=warehouse_items, ai_description=description)
    else:
        description = request.form.get("ai_description", "")
        images = [f for f in request.files.getlist("ai_images") if f and f.filename]
        known_fields = ai_calc_known_fields_from_form(request.form)
        if not description.strip() and not images and known_fields:
            description = ai_calc_synthesize_description_from_known_fields(known_fields)
        try:
            calc_result = ai_estimate_cost_params(description, api_key, images=images)
        except AICalcError as e:
            flash(str(e), "error")
            return render_template("calculator.html", active="calculator", result=None, dxf_result=None,
                                    warehouse_items=warehouse_items, ai_description=description)

    if calc_result["status"] == "question":
        token = _ai_calc_pending_store(calc_result["messages"], description, known_fields)
        return render_template("calculator.html", active="calculator", result=None, dxf_result=None,
                                warehouse_items=warehouse_items, ai_description=description,
                                ai_question=calc_result["question"], ai_clarify_token=token)

    params = calc_result["params"]
    # Поля, які користувач уже заповнив вручну ДО звернення до ШІ, мають
    # пріоритет над тим, що запропонував ШІ - це стосується лише першого
    # розрахунку й відповіді на уточнення. У правці з мінічату (correction_token)
    # це свідомо НЕ застосовуємо - сама суть правки в тому, щоб людина могла
    # природною мовою змінити будь-яке значення, в т.ч. раніше введене вручну,
    # і ШІ має це почути, а не бачити його миттєво відкоченим назад.
    if not correction_token:
        for _k, _v in known_fields.items():
            params[_k] = _v
    weight_kg = float(params.get("weight_kg") or 0)
    material_price = float(params.get("material_price_per_kg") or 0)
    turning_hours = float(params.get("turning_hours") or 0)
    turning_rate = float(params.get("turning_rate_per_hour") or 0)
    milling_hours = float(params.get("milling_hours") or 0)
    milling_rate = float(params.get("milling_rate_per_hour") or 0)
    machine_hours = float(params.get("machine_hours") or 0)
    machine_rate = float(params.get("machine_rate_per_hour") or 0)
    setup_cost_total = float(params.get("setup_cost_total") or 0)
    quantity = max(int(params.get("quantity") or 1), 1)
    margin_pct = float(params.get("margin_pct") or 20)

    material_cost = weight_kg * material_price
    turning_cost = turning_hours * turning_rate
    milling_cost = milling_hours * milling_rate
    machine_cost = machine_hours * machine_rate
    setup_cost_per_unit = (setup_cost_total / quantity) if quantity else 0
    cost_per_unit = material_cost + turning_cost + milling_cost + machine_cost + setup_cost_per_unit
    price_per_unit = cost_per_unit * (1 + margin_pct / 100)
    total_price = price_per_unit * quantity

    result = {
        "material_cost": round(material_cost, 2), "turning_cost": round(turning_cost, 2),
        "milling_cost": round(milling_cost, 2), "machine_cost": round(machine_cost, 2),
        "setup_cost_total": round(setup_cost_total, 2), "setup_cost_per_unit": round(setup_cost_per_unit, 2),
        "cost_per_unit": round(cost_per_unit, 2), "price_per_unit": round(price_per_unit, 2),
        "quantity": quantity, "total_price": round(total_price, 2), "margin_pct": margin_pct,
        "material_calc": None, "stock_check": None,
        "ai_operations_description": params.get("operations_description", ""),
        "ai_inputs": {
            "weight_kg": weight_kg, "material_price_per_kg": material_price,
            "turning_hours": turning_hours, "turning_rate_per_hour": turning_rate,
            "milling_hours": milling_hours, "milling_rate_per_hour": milling_rate,
            "machine_hours": machine_hours, "machine_rate_per_hour": machine_rate,
            "setup_cost_total": setup_cost_total,
            "quantity": quantity, "margin_pct": margin_pct,
        },
    }
    # Якщо розмову можна продовжити (є валідний tool_use/tool_result для
    # cost_estimate - немає лише у рідкісному текстовому фолбеку), даємо
    # мінічат правок: новий одноразовий токен, під яким чекає ТА САМА
    # розмова, готова прийняти наступне виправлення без повторення опису.
    if calc_result.get("messages"):
        # Після першої правки known_fields більше не "заморожуємо" - з цього
        # моменту результат повністю в руках діалогу з ШІ (див. коментар вище).
        stored_known_fields = {} if correction_token else known_fields
        result["ai_correction_token"] = _ai_calc_pending_store(calc_result["messages"], description, stored_known_fields)
    save_calculation_history(db, get_current_user(), "ai", description, result, raw_inputs=result["ai_inputs"])
    flash("Розрахунок від ШІ готовий — обов'язково перевір цифри перед використанням у КП", "success")
    return render_template("calculator.html", active="calculator", result=result, dxf_result=None,
                            warehouse_items=warehouse_items, ai_description=description)


def _render_quote_pdf(part_description, operations_description, weight_kg, material_price,
                       turning_hours, turning_rate, milling_hours, milling_rate,
                       machine_hours, machine_rate, setup_cost_total, quantity, margin_pct):
    """Будує сам PDF-документ розрахунку вартості (використовується і для
    щойно порахованого результату на сторінці калькулятора, і для вже
    збереженого запису з історії - код генерації PDF спільний, щоб не
    дублювати 150+ рядків reportlab-верстки і щоб обидва джерела завжди
    давали однаковий за виглядом документ). Повертає BytesIO, готовий до
    читання (seek(0) вже виконано)."""
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas as pdf_canvas

    material_cost = weight_kg * material_price
    turning_cost = turning_hours * turning_rate
    milling_cost = milling_hours * milling_rate
    machine_cost = machine_hours * machine_rate
    setup_cost_per_unit = (setup_cost_total / quantity) if quantity else 0
    cost_per_unit = material_cost + turning_cost + milling_cost + machine_cost + setup_cost_per_unit
    price_per_unit = cost_per_unit * (1 + margin_pct / 100)
    total_price = price_per_unit * quantity

    part_description = (part_description or "").strip() or "Розрахунок вартості механообробки"
    operations_description = (operations_description or "").strip()

    def wrap_text(text_obj, text, max_chars):
        for line in text.splitlines() or [""]:
            words = line.split(" ")
            cur_line = ""
            for w in words:
                if len(cur_line) + len(w) > max_chars:
                    text_obj.textLine(cur_line)
                    cur_line = w + " "
                else:
                    cur_line += w + " "
            text_obj.textLine(cur_line)

    buf = BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    steel = colors.HexColor("#2a2e35")
    accent = colors.HexColor("#e2650a")
    text_dim = colors.HexColor("#555555")

    def draw_header():
        c.setFillColor(steel)
        c.rect(0, height - 28 * mm, width, 28 * mm, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont(PDF_FONT_BOLD, 18)
        c.drawString(20 * mm, height - 15 * mm, "Оснастка-Маркет")
        c.setFont(PDF_FONT, 10)
        c.drawString(20 * mm, height - 22 * mm, "Розрахунок вартості токарно-фрезерної механообробки")
        c.setFillColor(colors.black)

    draw_header()
    y = height - 40 * mm
    c.setFont(PDF_FONT_BOLD, 13)
    c.drawString(20 * mm, y, f"Розрахунок від {datetime.date.today().strftime('%d.%m.%Y')}")
    y -= 10 * mm

    c.setFont(PDF_FONT_BOLD, 11)
    c.drawString(20 * mm, y, "Деталь / замовлення:")
    y -= 6 * mm
    c.setFont(PDF_FONT, 10)
    text_obj = c.beginText(20 * mm, y)
    text_obj.setLeading(4.5 * mm)
    wrap_text(text_obj, part_description, 95)
    c.drawText(text_obj)
    y = text_obj.getY() - 8 * mm

    if operations_description:
        c.setFont(PDF_FONT_BOLD, 11)
        c.drawString(20 * mm, y, "Технологічний процес:")
        y -= 6 * mm
        c.setFont(PDF_FONT, 9)
        text_obj = c.beginText(20 * mm, y)
        text_obj.setLeading(4.2 * mm)
        # Той самий розбір на етапи, що й на екрані: заголовок етапу, під ним
        # пояснення з відступом, між етапами порожній рядок - а не суцільний текст.
        pdf_lines = []
        for it in split_operations_text(operations_description):
            if it["kind"] == "step":
                pdf_lines.append(f'{it["num"]}. {it["title"]}')
                if it["body"]:
                    pdf_lines.append("    " + it["body"])
                pdf_lines.append("")
            else:
                pdf_lines.append(it["body"])
        wrap_text(text_obj, "\n".join(pdf_lines), 100)
        c.drawText(text_obj)
        y = text_obj.getY() - 8 * mm

    # Розгорнутий технологічний опис (тепер, за замовчуванням, це 10-15
    # речень від ШІ) легко не влазить на одну сторінку разом з таблицею
    # вартості - переходимо на нову, а не обрізаємо чи накладаємо текст.
    if y < 85 * mm:
        c.showPage()
        draw_header()
        y = height - 40 * mm

    rows = [
        ("Матеріал", material_cost),
        ("Токарна обробка", turning_cost),
        ("Фрезерна обробка", milling_cost),
        ("Додаткова обробка", machine_cost),
    ]
    if setup_cost_total:
        rows.append((f"Наладка (за од., {setup_cost_total:.0f} грн на партію)", setup_cost_per_unit))

    table_height = (len(rows) + 5) * 6 * mm + 20 * mm
    if y - table_height < 15 * mm:
        c.showPage()
        draw_header()
        y = height - 40 * mm

    c.setFont(PDF_FONT_BOLD, 11)
    c.drawString(20 * mm, y, "Розрахунок вартості:")
    y -= 8 * mm
    c.setFont(PDF_FONT, 10)
    for label, value in rows:
        c.drawString(24 * mm, y, label)
        c.drawRightString(width - 20 * mm, y, "{:,.2f} грн".format(value).replace(",", " "))
        y -= 6 * mm

    c.setStrokeColor(colors.HexColor("#dddddd"))
    c.line(20 * mm, y, width - 20 * mm, y)
    y -= 7 * mm
    c.setFont(PDF_FONT_BOLD, 10)
    c.drawString(24 * mm, y, "Собівартість за од.")
    c.drawRightString(width - 20 * mm, y, "{:,.2f} грн".format(cost_per_unit).replace(",", " "))
    y -= 6 * mm
    c.setFillColor(accent)
    c.drawString(24 * mm, y, "Ціна за од. (+{:.0f}%)".format(margin_pct))
    c.drawRightString(width - 20 * mm, y, "{:,.2f} грн".format(price_per_unit).replace(",", " "))
    c.setFillColor(colors.black)
    y -= 10 * mm

    c.setFillColor(steel)
    c.rect(20 * mm, y - 12 * mm, width - 40 * mm, 12 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont(PDF_FONT_BOLD, 13)
    c.drawString(24 * mm, y - 8.5 * mm, "Разом за {} шт.:".format(int(quantity)))
    c.drawRightString(width - 24 * mm, y - 8.5 * mm, "{:,.2f} грн".format(total_price).replace(",", " "))

    c.setFillColor(text_dim)
    c.setFont(PDF_FONT, 8)
    c.drawString(20 * mm, 15 * mm, "Документ згенеровано автоматично в Оснастка-Маркет. Ціни орієнтовні та можуть уточнюватись за результатами технічного огляду.")
    c.showPage()
    c.save()
    buf.seek(0)
    return buf


@app.route("/calculator/quote.pdf", methods=["POST"])
@login_required
@role_required("sales")
def calculator_quote_pdf():
    """Генерує PDF одразу з результату калькулятора (ручного чи ШІ-розрахунку),
    без потреби спершу створювати угоду - зручно, щоб відразу відправити
    клієнту розрахунок і повний технологічний процес. Суми НАВМИСНО рахуються
    заново на сервері з переданих параметрів (а не беруться готовими з
    прихованих полів форми), щоб підсумок у PDF не залежав від значень, які
    можна підмінити в інструментах розробника браузера - той самий принцип
    "довіряй, але перевіряй", що й у решті калькулятора."""
    from flask import Response

    f = request.form
    try:
        weight_kg = parse_number(f, "weight_kg", default=0, min_value=0, label="Вага заготовки")
        material_price = parse_number(f, "material_price_per_kg", default=0, min_value=0, label="Ціна металу за кг")
        turning_hours = parse_number(f, "turning_hours", default=0, min_value=0, label="Час токарної обробки")
        turning_rate = parse_number(f, "turning_rate_per_hour", default=0, min_value=0, label="Ставка токарної обробки за годину")
        milling_hours = parse_number(f, "milling_hours", default=0, min_value=0, label="Час фрезерної обробки")
        milling_rate = parse_number(f, "milling_rate_per_hour", default=0, min_value=0, label="Ставка фрезерної обробки за годину")
        machine_hours = parse_number(f, "machine_hours", default=0, min_value=0, label="Машино-години додаткової обробки")
        machine_rate = parse_number(f, "machine_rate_per_hour", default=0, min_value=0, label="Ставка за машино-годину додаткової обробки")
        setup_cost_total = parse_number(f, "setup_cost_total", default=0, min_value=0, label="Вартість наладки на партію")
        quantity = parse_number(f, "quantity", default=1, min_value=1, label="Кількість деталей")
        margin_pct = parse_number(f, "margin_pct", default=20, min_value=0, label="Націнка, %")
    except ValidationError as e:
        flash(str(e), "error")
        return redirect(url_for("cost_calculator"))

    part_description = (f.get("part_description") or "").strip()
    operations_description = (f.get("operations_description") or "").strip()

    buf = _render_quote_pdf(part_description, operations_description, weight_kg, material_price,
                             turning_hours, turning_rate, milling_hours, milling_rate,
                             machine_hours, machine_rate, setup_cost_total, quantity, margin_pct)

    resp = Response(buf.read(), mimetype="application/pdf")
    resp.headers["Content-Disposition"] = "inline; filename=rozrahunok_vartosti.pdf"
    return resp


@app.route("/calculator/history/<int:calc_id>/quote.pdf")
@login_required
@role_required("sales")
def calculator_history_quote_pdf(calc_id):
    """Той самий PDF-документ розрахунку, але з уже ЗБЕРЕЖЕНОГО запису в
    історії (а не з форми калькулятора) - щоб можна було відкрити старий
    розрахунок через кілька днів і одразу скачати готовий PDF для клієнта,
    не вводячи всі параметри заново вручну."""
    from flask import Response

    db = get_db()
    r = db.execute("SELECT * FROM cost_calculations WHERE id=?", (calc_id,)).fetchone()
    if not r:
        abort(404)

    buf = _render_quote_pdf(
        r["part_description"] or "", r["operations_description"] or "",
        r["weight_kg"] or 0, r["material_price_per_kg"] or 0,
        r["turning_hours"] or 0, r["turning_rate_per_hour"] or 0,
        r["milling_hours"] or 0, r["milling_rate_per_hour"] or 0,
        r["machine_hours"] or 0, r["machine_rate_per_hour"] or 0,
        r["setup_cost_total"] or 0, r["quantity"] or 1, r["margin_pct"] or 0,
    )

    resp = Response(buf.read(), mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f"inline; filename=rozrahunok_{calc_id}.pdf"
    return resp


@app.route("/calculator/analyze-dxf", methods=["POST"])
@login_required
@role_required("sales")
def calculator_analyze_dxf():
    file_storage = request.files.get("dxf_file")
    if not file_storage or not file_storage.filename.lower().endswith(".dxf"):
        flash("Обери файл із розширенням .dxf", "error")
        return render_template("calculator.html", active="calculator", result=None, dxf_result=None, warehouse_items=[])

    import tempfile
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
            file_storage.save(tmp.name)
            tmp_path = tmp.name
        dxf_result = analyze_dxf(tmp_path)
    except Exception as e:
        flash(f"Не вдалось розібрати DXF-файл: {e}", "error")
        return render_template("calculator.html", active="calculator", result=None, dxf_result=None, warehouse_items=[])
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    # Опційна оцінка ваги, якщо вказана товщина і густина матеріалу
    thickness_mm = parse_number(request.form, "thickness_mm", default=0, min_value=0, label="Товщина")
    density = parse_number(request.form, "density_kg_m3", default=7850, min_value=0, label="Густина")
    if thickness_mm > 0:
        dxf_result["estimated_weight_kg"] = round(dxf_result["area_m2"] * (thickness_mm / 1000) * density, 3)

    flash(f"Креслення розібрано: {dxf_result['entity_count']} об'єктів, "
          f"периметр різу {dxf_result['cut_length_m']} м", "success")
    return render_template("calculator.html", active="calculator", result=None, dxf_result=dxf_result, warehouse_items=[])


@app.route("/deals/new", methods=["GET", "POST"])
@login_required
@role_required('sales')
def deal_new():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            title = validate_required(f, "title", "Назва угоди")
            client_id = validate_required(f, "client_id", "Клієнт")
            amount = parse_number(f, "amount", default=0, min_value=0, label="Сума")
            prepayment = parse_number(f, "prepayment_percent", default=30, min_value=0, max_value=100, label="Передоплата, %")
            probability = parse_number(f, "probability", default=20, min_value=0, max_value=100, label="Ймовірність, %")
        except ValidationError as e:
            flash(str(e), "error")
            clients = db.execute("SELECT * FROM clients ORDER BY name").fetchall()
            machines = db.execute("SELECT * FROM machines ORDER BY category, model").fetchall()
            return render_template("deal_form.html", active="deals", deal=None, clients=clients,
                                    machines=machines, managers=get_managers(), CURRENCIES=CURRENCIES,
                                    preselect_client=request.args.get("client_id", type=int))
        db.execute(
            """INSERT INTO deals (title, client_id, contact_id, machine_id, stage, amount, currency,
               prepayment_percent, probability, manager_id, expected_close, source, priority,
               competitor, tech_requirements, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (title, client_id, f.get("contact_id") or None, f.get("machine_id") or None,
             f.get("stage", "lead"), amount, f.get("currency", "USD"),
             prepayment, probability, f.get("manager_id") or None,
             f.get("expected_close") or None, f.get("source"), f.get("priority", "medium"),
             f.get("competitor"), f.get("tech_requirements"), now_iso(), now_iso()),
        )
        new_deal_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        log_activity(db, new_deal_id, client_id, "note", f"Угоду «{title}» створено")
        db.commit()
        notify_event(f"🆕 Нова угода: «{title}» на {amount:,.0f} {f.get('currency', 'USD')}".replace(",", " "))
        flash("Угоду створено", "success")
        return redirect(url_for("deal_detail", deal_id=new_deal_id))
    clients = db.execute("SELECT * FROM clients ORDER BY name").fetchall()
    machines = db.execute("SELECT * FROM machines ORDER BY category, model").fetchall()
    preselect_client = request.args.get("client_id", type=int)
    return render_template("deal_form.html", active="deals", deal=None, clients=clients,
                            machines=machines, managers=get_managers(),
                            CURRENCIES=CURRENCIES, preselect_client=preselect_client,
                            prefill_title=request.args.get("prefill_title"),
                            prefill_amount=request.args.get("prefill_amount"))


@app.route("/deals/<int:deal_id>/edit", methods=["GET", "POST"])
@login_required
@role_required('sales')
def deal_edit(deal_id):
    db = get_db()
    deal = db.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not deal:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            title = validate_required(f, "title", "Назва угоди")
            client_id = validate_required(f, "client_id", "Клієнт")
            amount = parse_number(f, "amount", default=0, min_value=0, label="Сума")
            prepayment = parse_number(f, "prepayment_percent", default=30, min_value=0, max_value=100, label="Передоплата, %")
            probability = parse_number(f, "probability", default=20, min_value=0, max_value=100, label="Ймовірність, %")
        except ValidationError as e:
            flash(str(e), "error")
            return redirect(url_for("deal_edit", deal_id=deal_id))
        db.execute(
            """UPDATE deals SET title=?, client_id=?, machine_id=?, stage=?, amount=?, currency=?,
               prepayment_percent=?, probability=?, manager_id=?, expected_close=?, priority=?,
               competitor=?, tech_requirements=?, updated_at=? WHERE id=?""",
            (title, client_id, f.get("machine_id") or None, f.get("stage", "lead"),
             amount, f.get("currency", "USD"), prepayment,
             probability, f.get("manager_id") or None, f.get("expected_close") or None,
             f.get("priority", "medium"), f.get("competitor"), f.get("tech_requirements"),
             now_iso(), deal_id),
        )
        db.commit()
        flash("Угоду оновлено", "success")
        return redirect(url_for("deal_detail", deal_id=deal_id))
    clients = db.execute("SELECT * FROM clients ORDER BY name").fetchall()
    machines = db.execute("SELECT * FROM machines ORDER BY category, model").fetchall()
    return render_template("deal_form.html", active="deals", deal=deal, clients=clients,
                            machines=machines, managers=get_managers(),
                            CURRENCIES=CURRENCIES, preselect_client=None)


@app.route("/deals/<int:deal_id>")
@login_required
@role_required('sales')
def deal_detail(deal_id):
    db = get_db()
    deal = deal_row_to_dict(db, deal_id)
    if not deal:
        abort(404)
    activities = db.execute(
        """SELECT a.*, u.full_name as manager_name FROM activities a
           LEFT JOIN users u ON u.id=a.manager_id WHERE a.deal_id=? ORDER BY a.created_at DESC""",
        (deal_id,),
    ).fetchall()
    tasks = db.execute("SELECT * FROM tasks WHERE deal_id=? ORDER BY done, due_date", (deal_id,)).fetchall()
    payments = db.execute("SELECT * FROM payments WHERE deal_id=? ORDER BY created_at", (deal_id,)).fetchall()
    paid_total = sum(p["amount"] for p in payments if p["status"] == "paid")
    balance_due = (deal["amount"] or 0) - paid_total
    attachments = get_attachments("deal", deal_id)
    return render_template("deal_detail.html", active="deals", deal=deal,
                            activities=activities, tasks=tasks, payments=payments,
                            paid_total=paid_total, balance_due=balance_due,
                            PAYMENT_KINDS=PAYMENT_KINDS, attachments=attachments)


@app.route("/deals/<int:deal_id>/proposal.pdf")
@login_required
@role_required('sales')
def deal_proposal_pdf(deal_id):
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas as pdf_canvas

    db = get_db()
    deal = deal_row_to_dict(db, deal_id)
    if not deal:
        abort(404)
    client = db.execute("SELECT * FROM clients WHERE id=?", (deal["client_id"],)).fetchone()
    machine = None
    if deal["machine_id"]:
        machine = db.execute("SELECT * FROM machines WHERE id=?", (deal["machine_id"],)).fetchone()

    buf = BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    steel = colors.HexColor("#2a2e35")
    accent = colors.HexColor("#e2650a")
    text_dim = colors.HexColor("#555555")

    c.setFillColor(steel)
    c.rect(0, height - 28 * mm, width, 28 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont(PDF_FONT_BOLD, 18)
    c.drawString(20 * mm, height - 15 * mm, "Оснастка-Маркет")
    c.setFont(PDF_FONT, 10)
    c.drawString(20 * mm, height - 22 * mm, "Комерційна пропозиція на виготовлення деталей / металоконструкцій")

    y = height - 40 * mm
    c.setFillColor(colors.black)
    c.setFont(PDF_FONT_BOLD, 13)
    c.drawString(20 * mm, y, f"КП № {deal['id']:04d}  від  {datetime.date.today().strftime('%d.%m.%Y')}")
    y -= 10 * mm

    c.setFont(PDF_FONT_BOLD, 11)
    c.drawString(20 * mm, y, "Клієнт:")
    c.setFont(PDF_FONT, 11)
    c.drawString(45 * mm, y, client["name"] if client else "—")
    y -= 6 * mm
    if client and client["edrpou"]:
        c.setFont(PDF_FONT, 9)
        c.setFillColor(text_dim)
        c.drawString(45 * mm, y, f"ЄДРПОУ: {client['edrpou']}   Місто: {client['city'] or '—'}")
        c.setFillColor(colors.black)
        y -= 8 * mm
    else:
        y -= 4 * mm

    c.setFont(PDF_FONT_BOLD, 11)
    c.drawString(20 * mm, y, "Об'єкт пропозиції:")
    y -= 7 * mm
    c.setFont(PDF_FONT, 11)
    c.drawString(20 * mm, y, deal["title"])
    y -= 10 * mm

    if machine:
        c.setFillColor(steel)
        c.rect(20 * mm, y - 42 * mm, width - 40 * mm, 40 * mm, fill=0, stroke=1)
        c.setFont(PDF_FONT_BOLD, 12)
        c.setFillColor(colors.black)
        c.drawString(24 * mm, y - 8 * mm, f"Обробка виконується на: {machine['category']}")
        c.setFont(PDF_FONT_BOLD, 14)
        c.setFillColor(accent)
        c.drawString(24 * mm, y - 16 * mm, machine["model"])
        c.setFillColor(colors.black)
        c.setFont(PDF_FONT, 10)
        specs = [
            f"Потужність обладнання: {machine['spindle_power'] or '—'}",
            f"Робоча зона: {machine['work_area'] or '—'}",
            f"Точність обробки: {machine['accuracy'] or '—'}",
            f"Орієнтовний термін виконання замовлення: {machine['lead_time_days'] or '—'} днів",
        ]
        sy = y - 24 * mm
        for line in specs:
            c.drawString(24 * mm, sy, line)
            sy -= 5 * mm
        y = y - 42 * mm - 10 * mm
    else:
        y -= 4 * mm

    if deal["tech_requirements"]:
        c.setFont(PDF_FONT_BOLD, 11)
        c.drawString(20 * mm, y, "Технічні вимоги:")
        y -= 6 * mm
        c.setFont(PDF_FONT, 9)
        text_obj = c.beginText(20 * mm, y)
        text_obj.setLeading(4.5 * mm)
        for line in str(deal["tech_requirements"]).splitlines() or [""]:
            words = line.split(" ")
            cur_line = ""
            for w in words:
                if len(cur_line) + len(w) > 95:
                    text_obj.textLine(cur_line)
                    cur_line = w + " "
                else:
                    cur_line += w + " "
            text_obj.textLine(cur_line)
        c.drawText(text_obj)
        y = text_obj.getY() - 8 * mm

    c.setFillColor(steel)
    c.rect(20 * mm, y - 30 * mm, width - 40 * mm, 28 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont(PDF_FONT_BOLD, 11)
    c.drawString(24 * mm, y - 10 * mm, "Вартість:")
    c.setFont(PDF_FONT_BOLD, 16)
    c.drawString(24 * mm, y - 18 * mm, fmt_money(deal["amount"], deal["currency"]))
    c.setFont(PDF_FONT, 10)
    c.drawString(90 * mm, y - 10 * mm, f"Умови передоплати: {deal['prepayment_percent']}%")
    c.drawString(90 * mm, y - 16 * mm, f"Термін дії пропозиції: 14 днів")
    if deal["expected_close"]:
        c.drawString(90 * mm, y - 22 * mm, f"Орієнтовний термін закриття: {deal['expected_close']}")

    c.setFillColor(text_dim)
    c.setFont(PDF_FONT, 8)
    c.drawString(20 * mm, 15 * mm, "Документ згенеровано автоматично в Оснастка-Маркет. Ціни можуть уточнюватись за результатами технічного огляду.")
    c.showPage()
    c.save()
    buf.seek(0)

    from flask import Response
    resp = Response(buf.read(), mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f"inline; filename=proposal_{deal_id}.pdf"
    return resp


@app.route("/deals/<int:deal_id>/move/<stage>", methods=["POST"])
@login_required
@role_required('sales')
def deal_move(deal_id, stage):
    if stage not in STAGE_KEYS and stage != LOST_STAGE:
        return jsonify(ok=False), 400
    db = get_db()
    deal = db.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not deal:
        abort(404)
    if stage == "prepay" and not deal["machine_id"]:
        # Виробниче замовлення заводиться по маршрутній карті, прив'язаній до
        # верстата (create_production_order_from_deal мовчки нічого не робить
        # без machine_id) - типово для угод, створених одним кліком з історії
        # калькулятора (там верстат не обирається). Раніше угода просто
        # переходила на "Передоплата" без жодного сліду в цехах - тепер
        # питаємо верстат і одразу заводимо завдання.
        redirect_url = url_for("deal_select_machine", deal_id=deal_id, next_stage=stage)
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify(ok=False, needs_machine=True, redirect=redirect_url)
        return redirect(redirect_url)
    _apply_deal_stage_move(db, deal, stage)
    db.commit()
    # Kanban drag-and-drop calls this via fetch() with X-Requested-With: fetch and wants JSON back.
    # The buttons on the deal detail page submit a normal HTML form and expect a redirect back.
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    flash("Стадію угоди оновлено", "success")
    return redirect(url_for("deal_detail", deal_id=deal_id))


def _apply_deal_stage_move(db, deal, stage):
    """Спільна логіка переходу угоди на нову стадію - винесена окремо, щоб
    нею міг скористатись і звичайний deal_move, і deal_select_machine
    (після того, як верстат щойно обрано і треба довершити той самий
    перехід стадії). Повертає id щойно заведеного виробничого замовлення,
    якщо воно було створене (стадія 'prepay' і верстат вже відомий)."""
    closed_at = now_iso() if stage in ("won", LOST_STAGE) else deal["closed_at"]
    db.execute("UPDATE deals SET stage=?, updated_at=?, closed_at=? WHERE id=?",
               (stage, now_iso(), closed_at, deal["id"]))
    label = STAGE_LABEL.get(stage, stage)
    log_activity(db, deal["id"], deal["client_id"], "stage", f"Стадію угоди змінено на «{label}»")
    notify_event(f"📌 Угода «{deal['title']}»: тепер «{label}»")
    auto = AUTO_TASKS_ON_STAGE.get(stage)
    if auto:
        title, ttype, offset = auto
        create_task(db, deal["id"], deal["client_id"], title, ttype, offset)
    order_id = None
    if stage == "prepay":
        order_id = create_production_order_from_deal(db, deal)
    return order_id


@app.route("/deals/<int:deal_id>/select-machine", methods=["GET", "POST"])
@login_required
@role_required('sales')
def deal_select_machine(deal_id):
    """Проміжний крок для угод без обраного верстата (типово - угоди,
    створені одним кліком з історії калькулятора вартості): перш ніж
    перевести угоду на 'Передоплата отримана' і завести виробниче
    замовлення, треба знати, за якою маршрутною картою його вести -
    просимо вибрати верстат і одразу довершуємо перехід стадії."""
    db = get_db()
    deal = db.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not deal:
        abort(404)
    next_stage = request.values.get("next_stage", "prepay")
    if next_stage not in STAGE_KEYS and next_stage != LOST_STAGE:
        next_stage = "prepay"
    machines = db.execute("SELECT * FROM machines ORDER BY category, model").fetchall()
    if request.method == "POST":
        try:
            machine_id = validate_required(request.form, "machine_id", "Верстат / техпроцес")
        except ValidationError as e:
            flash(str(e), "error")
            return render_template("deal_select_machine.html", active="deals", deal=deal,
                                    machines=machines, next_stage=next_stage)
        db.execute("UPDATE deals SET machine_id=?, updated_at=? WHERE id=?", (machine_id, now_iso(), deal_id))
        deal = db.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        order_id = _apply_deal_stage_move(db, deal, next_stage)
        db.commit()
        if order_id:
            flash(f"Верстат обрано, угоду переведено на «{STAGE_LABEL.get(next_stage, next_stage)}», "
                  f"заведено виробниче замовлення №{order_id} у цех", "success")
        else:
            flash("Верстат обрано, угоду переведено на нову стадію", "success")
        return redirect(url_for("deal_detail", deal_id=deal_id))
    return render_template("deal_select_machine.html", active="deals", deal=deal,
                            machines=machines, next_stage=next_stage)


@app.route("/deals/<int:deal_id>/delete", methods=["POST"])
@login_required
@role_required('sales')
def deal_delete(deal_id):
    db = get_db()
    cascade_delete_deal(db, deal_id)
    db.commit()
    flash("Угоду видалено", "success")
    return redirect(url_for("deals_board"))


@app.route("/deals/<int:deal_id>/payments", methods=["POST"])
@login_required
@role_required('sales')
def payment_add(deal_id):
    db = get_db()
    f = request.form
    try:
        amount = parse_number(f, "amount", required=True, min_value=0.01, label="Сума платежу")
    except ValidationError as e:
        flash(str(e), "error")
        return redirect(url_for("deal_detail", deal_id=deal_id))
    status = "paid" if f.get("mark_paid") else "pending"
    paid_date = now_iso() if status == "paid" else None
    db.execute(
        """INSERT INTO payments (deal_id, kind, amount, currency, status, due_date, paid_date, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (deal_id, f.get("kind", "prepayment"), amount, f.get("currency", "USD"),
         status, f.get("due_date") or None, paid_date, now_iso()),
    )
    deal = db.execute("SELECT client_id FROM deals WHERE id=?", (deal_id,)).fetchone()
    log_activity(db, deal_id, deal["client_id"] if deal else None, "note",
                 f"Додано платіж: {dict(PAYMENT_KINDS).get(f.get('kind'), f.get('kind'))} — {amount} {f.get('currency', 'USD')}")
    db.commit()
    flash("Платіж додано", "success")
    return redirect(url_for("deal_detail", deal_id=deal_id))


@app.route("/payments/<int:payment_id>/toggle", methods=["POST"])
@login_required
@role_required('sales')
def payment_toggle(payment_id):
    db = get_db()
    p = db.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    if not p:
        abort(404)
    new_status = "pending" if p["status"] == "paid" else "paid"
    paid_date = now_iso() if new_status == "paid" else None
    db.execute("UPDATE payments SET status=?, paid_date=? WHERE id=?", (new_status, paid_date, payment_id))
    db.commit()
    return redirect(url_for("deal_detail", deal_id=p["deal_id"]))


@app.route("/payments/<int:payment_id>/delete", methods=["POST"])
@login_required
@role_required('sales')
def payment_delete(payment_id):
    db = get_db()
    p = db.execute("SELECT deal_id FROM payments WHERE id=?", (payment_id,)).fetchone()
    db.execute("DELETE FROM payments WHERE id=?", (payment_id,))
    db.commit()
    if p:
        return redirect(url_for("deal_detail", deal_id=p["deal_id"]))
    return redirect(url_for("deals_board"))


@app.route("/activities/<int:deal_id>", methods=["POST"])
@login_required
@role_required('sales')
def activity_add(deal_id):
    db = get_db()
    deal = db.execute("SELECT client_id FROM deals WHERE id=?", (deal_id,)).fetchone()
    f = request.form
    try:
        text = validate_required(f, "text", "Текст")
    except ValidationError as e:
        flash(str(e), "error")
        return redirect(url_for("deal_detail", deal_id=deal_id))
    db.execute(
        "INSERT INTO activities (deal_id, client_id, type, text, manager_id, created_at) VALUES (?,?,?,?,?,?)",
        (deal_id, deal["client_id"] if deal else None, f.get("type", "note"), text,
         session.get("user_id"), now_iso()),
    )
    db.commit()
    return redirect(url_for("deal_detail", deal_id=deal_id))


# ---------------------------------------------------------------------------
# Задачі
# ---------------------------------------------------------------------------

@app.route("/tasks")
@login_required
def tasks_list():
    db = get_db()
    tasks = db.execute(
        """SELECT t.*, c.name as client_name, u.full_name as manager_name, d.title as deal_title
           FROM tasks t
           LEFT JOIN clients c ON c.id=t.client_id
           LEFT JOIN users u ON u.id=t.manager_id
           LEFT JOIN deals d ON d.id=t.deal_id
           ORDER BY t.done, (t.due_date IS NULL), t.due_date"""
    ).fetchall()
    clients = db.execute("SELECT * FROM clients ORDER BY name").fetchall()
    return render_template("tasks.html", active="tasks", tasks=tasks, clients=clients)


@app.route("/calendar")
@login_required
def calendar_view():
    db = get_db()
    year = request.args.get("year", type=int)
    if year is None:
        year = datetime.date.today().year
    month = request.args.get("month", type=int)
    if month is None:
        month = datetime.date.today().month
    if month < 1:
        month, year = 12, year - 1
    elif month > 12:
        month, year = 1, year + 1

    first_day = datetime.date(year, month, 1)
    days_in_month = (datetime.date(year + (month == 12), (month % 12) + 1, 1) - datetime.timedelta(days=1)).day
    start_weekday = first_day.weekday()  # 0 = понеділок

    tasks = db.execute(
        """SELECT t.*, c.name as client_name FROM tasks t
           LEFT JOIN clients c ON c.id=t.client_id
           WHERE t.due_date IS NOT NULL AND t.due_date >= ? AND t.due_date <= ?
           ORDER BY t.due_date""",
        (first_day.strftime("%Y-%m-%d"), datetime.date(year, month, days_in_month).strftime("%Y-%m-%d")),
    ).fetchall()
    tasks_by_day = {}
    for t in tasks:
        day = int(t["due_date"][8:10])
        tasks_by_day.setdefault(day, []).append(t)

    weeks = []
    week = [None] * start_weekday
    for day in range(1, days_in_month + 1):
        week.append(day)
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        week += [None] * (7 - len(week))
        weeks.append(week)

    month_names = ["Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
                   "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"]
    prev_month, prev_year = (12, year - 1) if month == 1 else (month - 1, year)
    next_month, next_year = (1, year + 1) if month == 12 else (month + 1, year)

    return render_template(
        "calendar.html", active="tasks", weeks=weeks, tasks_by_day=tasks_by_day,
        month_name=month_names[month - 1], year=year, month=month,
        prev_month=prev_month, prev_year=prev_year, next_month=next_month, next_year=next_year,
        today_day=datetime.date.today().day if (datetime.date.today().year == year and datetime.date.today().month == month) else None,
    )


@app.route("/tasks/add", methods=["POST"])
@login_required
def task_add():
    db = get_db()
    f = request.form
    try:
        title = validate_required(f, "title", "Назва задачі")
    except ValidationError as e:
        flash(str(e), "error")
        if f.get("deal_id"):
            return redirect(url_for("deal_detail", deal_id=f.get("deal_id")))
        return redirect(url_for("tasks_list"))
    db.execute(
        """INSERT INTO tasks (deal_id, client_id, title, description, type, due_date, done,
           manager_id, created_at) VALUES (?,?,?,?,?,?,0,?,?)""",
        (f.get("deal_id") or None, f.get("client_id") or None, title, f.get("description"),
         f.get("type", "call"), f.get("due_date") or None, session.get("user_id"), now_iso()),
    )
    db.commit()
    flash("Задачу додано", "success")
    if f.get("deal_id"):
        return redirect(url_for("deal_detail", deal_id=f.get("deal_id")))
    return redirect(url_for("tasks_list"))


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
@login_required
def task_delete(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    db.commit()
    if task and task["deal_id"]:
        return redirect(url_for("deal_detail", deal_id=task["deal_id"]))
    return redirect(url_for("tasks_list"))


@app.route("/tasks/<int:task_id>/toggle", methods=["POST"])
@login_required
def task_toggle(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        abort(404)
    db.execute("UPDATE tasks SET done=? WHERE id=?", (0 if task["done"] else 1, task_id))
    db.commit()
    if task["deal_id"]:
        return redirect(url_for("deal_detail", deal_id=task["deal_id"]))
    return redirect(url_for("tasks_list"))


# ---------------------------------------------------------------------------
# Наше обладнання
# ---------------------------------------------------------------------------

@app.route("/machines")
@login_required
def machines_list():
    db = get_db()
    machines = db.execute("SELECT * FROM machines ORDER BY category, model").fetchall()
    return render_template("machines.html", active="machines", machines=machines)


@app.route("/machines/new", methods=["GET", "POST"])
@login_required
@role_required('sales', 'production')
def machine_new():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            category = validate_required(f, "category", "Категорія")
            model = validate_required(f, "model", "Модель")
            price = parse_number(f, "price", default=0, min_value=0, label="Ціна")
            lead_time = parse_number(f, "lead_time_days", default=30, min_value=0, label="Термін виготовлення")
        except ValidationError as e:
            flash(str(e), "error")
            return render_template("machine_form.html", active="machines", machine=None, CURRENCIES=CURRENCIES)
        db.execute(
            """INSERT INTO machines (category, model, description, spindle_power, work_area,
               accuracy, price, currency, lead_time_days, in_stock) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (category, model, f.get("description"), f.get("spindle_power"),
             f.get("work_area"), f.get("accuracy"), price, f.get("currency", "USD"),
             lead_time, int(f.get("in_stock", 0))),
        )
        db.commit()
        flash("Верстат додано в каталог", "success")
        return redirect(url_for("machines_list"))
    return render_template("machine_form.html", active="machines", machine=None, CURRENCIES=CURRENCIES)


@app.route("/machines/<int:machine_id>/edit", methods=["GET", "POST"])
@login_required
@role_required('sales', 'production')
def machine_edit(machine_id):
    db = get_db()
    machine = db.execute("SELECT * FROM machines WHERE id=?", (machine_id,)).fetchone()
    if not machine:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            category = validate_required(f, "category", "Категорія")
            model = validate_required(f, "model", "Модель")
            price = parse_number(f, "price", default=0, min_value=0, label="Ціна")
            lead_time = parse_number(f, "lead_time_days", default=30, min_value=0, label="Термін виготовлення")
        except ValidationError as e:
            flash(str(e), "error")
            return redirect(url_for("machine_edit", machine_id=machine_id))
        db.execute(
            """UPDATE machines SET category=?, model=?, description=?, spindle_power=?, work_area=?,
               accuracy=?, price=?, currency=?, lead_time_days=?, in_stock=? WHERE id=?""",
            (category, model, f.get("description"), f.get("spindle_power"),
             f.get("work_area"), f.get("accuracy"), price, f.get("currency", "USD"),
             lead_time, int(f.get("in_stock", 0)), machine_id),
        )
        db.commit()
        flash("Дані верстата оновлено", "success")
        return redirect(url_for("machines_list"))
    return render_template("machine_form.html", active="machines", machine=machine, CURRENCIES=CURRENCIES)


@app.route("/machines/<int:machine_id>/delete", methods=["POST"])
@login_required
@role_required('sales', 'production')
def machine_delete(machine_id):
    db = get_db()
    # Раніше тут перевірялось "є хоч одна угода з цим verstat'ом взагалі" —
    # через це верстат лишався незнищенним НАЗАВЖДИ, щойно хоч раз
    # використовувався навіть у вже закритій (виграна/програна) угоді, хоча
    # повідомлення явно обіцяло блокувати лише "активні" угоди. Тепер
    # перевіряємо саме активність: незакриті угоди й незавершене виробниче
    # замовлення на цьому верстаті — те саме визначення "активна угода", що
    # й у решті системи (stage NOT IN ('won','lost')).
    active_deals = db.execute(
        "SELECT COUNT(*) c FROM deals WHERE machine_id=? AND stage NOT IN ('won','lost')",
        (machine_id,),
    ).fetchone()["c"]
    active_production = db.execute(
        "SELECT COUNT(*) c FROM production_orders WHERE machine_id=? AND status NOT IN ('done','cancelled')",
        (machine_id,),
    ).fetchone()["c"]
    if active_deals or active_production:
        flash("Неможливо видалити: обладнання використовується в активних угодах або виробництві", "error")
        return redirect(url_for("machines_list"))
    # Жодних активних прив'язок немає — відв'язуємо верстат від уже закритих
    # (виграних/програних) угод та іншої історичної інформації, щоб не
    # лишати "висячих" посилань на видалений id, і видаляємо сам верстат.
    db.execute("UPDATE deals SET machine_id=NULL WHERE machine_id=?", (machine_id,))
    db.execute("UPDATE production_orders SET machine_id=NULL WHERE machine_id=?", (machine_id,))
    db.execute("UPDATE client_equipment SET machine_id=NULL WHERE machine_id=?", (machine_id,))
    db.execute("UPDATE service_tickets SET machine_id=NULL WHERE machine_id=?", (machine_id,))
    db.execute("DELETE FROM machines WHERE id=?", (machine_id,))
    db.commit()
    flash("Верстат видалено з каталогу", "success")
    return redirect(url_for("machines_list"))


# ---------------------------------------------------------------------------
# Рекламації та доробки
# ---------------------------------------------------------------------------

@app.route("/service")
@login_required
@role_required('service')
def service_list():
    db = get_db()
    tickets = db.execute(
        """SELECT s.*, c.name as client_name FROM service_tickets s
           LEFT JOIN clients c ON c.id=s.client_id ORDER BY s.created_at DESC"""
    ).fetchall()
    clients = db.execute("SELECT * FROM clients ORDER BY name").fetchall()
    machines = db.execute("SELECT * FROM machines ORDER BY model").fetchall()
    return render_template("service.html", active="service", tickets=tickets, clients=clients, machines=machines)


@app.route("/service/<int:ticket_id>")
@login_required
@role_required("service")
def service_detail(ticket_id):
    db = get_db()
    ticket = db.execute(
        """SELECT s.*, c.name as client_name, m.model as machine_name
           FROM service_tickets s LEFT JOIN clients c ON c.id=s.client_id
           LEFT JOIN machines m ON m.id=s.machine_id WHERE s.id=?""",
        (ticket_id,),
    ).fetchone()
    if not ticket:
        abort(404)
    attachments = get_attachments("service_ticket", ticket_id)
    return render_template("service_detail.html", active="service", ticket=ticket, attachments=attachments)


@app.route("/service/add", methods=["POST"])
@login_required
@role_required('service')
def service_add():
    db = get_db()
    f = request.form
    try:
        client_id = validate_required(f, "client_id", "Клієнт")
        issue = validate_required(f, "issue", "Опис проблеми")
    except ValidationError as e:
        flash(str(e), "error")
        return redirect(url_for("service_list"))
    db.execute(
        """INSERT INTO service_tickets (client_id, machine_id, issue, status, priority, engineer,
           created_at) VALUES (?,?,?,?,?,?,?)""",
        (client_id, f.get("machine_id") or None, issue, "new", f.get("priority", "medium"),
         f.get("engineer"), now_iso()),
    )
    db.commit()
    flash("Сервісну заявку створено", "success")
    return redirect(url_for("service_list"))


@app.route("/service/<int:ticket_id>/status", methods=["POST"])
@login_required
@role_required('service')
def service_update_status(ticket_id):
    db = get_db()
    try:
        status = validate_required(request.form, "status", "Статус")
    except ValidationError as e:
        flash(str(e), "error")
        return redirect(url_for("service_list"))
    resolved_at = now_iso() if status == "done" else None
    db.execute("UPDATE service_tickets SET status=?, resolved_at=? WHERE id=?",
               (status, resolved_at, ticket_id))
    db.commit()
    return redirect(url_for("service_list"))


# ---------------------------------------------------------------------------
# Пошук
# ---------------------------------------------------------------------------

@app.route("/service/<int:ticket_id>/delete", methods=["POST"])
@login_required
@role_required('service')
def service_delete(ticket_id):
    db = get_db()
    db.execute("DELETE FROM service_tickets WHERE id=?", (ticket_id,))
    db.commit()
    flash("Заявку видалено", "success")
    return redirect(url_for("service_list"))


# ---------------------------------------------------------------------------
# Імпорт клієнтів з Excel
# ---------------------------------------------------------------------------

@app.route("/clients/import", methods=["GET", "POST"])
@login_required
@role_required("sales")
def clients_import():
    if request.method == "GET":
        return render_template("clients_import.html", active="clients", result=None)

    file_storage = request.files.get("file")
    if not file_storage or not file_storage.filename.lower().endswith((".xlsx", ".xls")):
        flash("Обери файл Excel (.xlsx)", "error")
        return redirect(url_for("clients_import"))

    from openpyxl import load_workbook
    try:
        wb = load_workbook(file_storage, data_only=True)
    except Exception:
        flash("Не вдалось прочитати файл — переконайся, що це справжній .xlsx", "error")
        return redirect(url_for("clients_import"))

    ws = wb.active
    db = get_db()
    created, updated, skipped, errors = 0, 0, 0, []

    # Очікуваний порядок колонок відповідає файлу, який видає /export/clients.xlsx:
    # Компанія, ЄДРПОУ, Галузь, Місто, Адреса, Телефон, Email, Сайт, Джерело,
    # Менеджер (ігнорується при імпорті — призначається вручну), Статус, Примітки
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    for i, row in enumerate(rows, start=2):
        if not row or not row[0]:
            skipped += 1
            continue
        name = str(row[0]).strip()
        try:
            email = str(row[6]).strip() if len(row) > 6 and row[6] else ""
            if email and not _EMAIL_RE.match(email):
                errors.append(f"Рядок {i}: некоректний email «{email}» — імпортовано без email")
                email = ""
            existing = db.execute("SELECT id FROM clients WHERE name=?", (name,)).fetchone()
            values = (
                name,
                str(row[1]).strip() if len(row) > 1 and row[1] else None,
                str(row[2]).strip() if len(row) > 2 and row[2] else None,
                str(row[3]).strip() if len(row) > 3 and row[3] else None,
                str(row[4]).strip() if len(row) > 4 and row[4] else None,
                str(row[5]).strip() if len(row) > 5 and row[5] else None,
                email or None,
                str(row[7]).strip() if len(row) > 7 and row[7] else None,
                str(row[8]).strip() if len(row) > 8 and row[8] else None,
                str(row[11]).strip() if len(row) > 11 and row[11] else None,
            )
            if existing:
                db.execute(
                    """UPDATE clients SET edrpou=?, industry=?, city=?, address=?, phone=?, email=?,
                       website=?, source=?, notes=? WHERE id=?""",
                    values[1:] + (existing["id"],),
                )
                updated += 1
            else:
                db.execute(
                    """INSERT INTO clients (name, edrpou, industry, city, address, phone, email,
                       website, source, status, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)""",
                    values + (now_iso(),),
                )
                created += 1
        except Exception as e:
            errors.append(f"Рядок {i}: {e}")
            skipped += 1
    db.commit()
    result = {"created": created, "updated": updated, "skipped": skipped, "errors": errors[:20]}
    return render_template("clients_import.html", active="clients", result=result)


# ---------------------------------------------------------------------------
# Експорт в Excel
# ---------------------------------------------------------------------------

@app.route("/export/clients.xlsx")
@login_required
@role_required('sales')
def export_clients_xlsx():
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    db = get_db()
    scope, params = manager_scope_sql("c.manager_id")
    clients = db.execute(
        f"""SELECT c.*, u.full_name as manager_name FROM clients c
            LEFT JOIN users u ON u.id=c.manager_id WHERE 1=1 {scope} ORDER BY c.name""",
        params,
    ).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Клієнти"
    headers = ["Компанія", "ЄДРПОУ", "Галузь", "Місто", "Адреса", "Телефон", "Email",
               "Сайт", "Джерело", "Менеджер", "Статус", "Примітки"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2A2E35")
    for c in clients:
        ws.append([c["name"], c["edrpou"], c["industry"], c["city"], c["address"], c["phone"],
                   c["email"], c["website"], c["source"], c["manager_name"],
                   "Активний" if c["status"] == "active" else "Втрачений", c["notes"]])
    for i, w in enumerate([28, 14, 20, 14, 26, 16, 24, 20, 16, 18, 12, 30], start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    from flask import send_file
    return send_file(buf, as_attachment=True, download_name="clients.xlsx",
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/export/deals.xlsx")
@login_required
@role_required('sales')
def export_deals_xlsx():
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    db = get_db()
    scope, params = manager_scope_sql("d.manager_id")
    deals = db.execute(
        f"""SELECT d.*, c.name as client_name, u.full_name as manager_name, m.model as machine_name
            FROM deals d LEFT JOIN clients c ON c.id=d.client_id
            LEFT JOIN users u ON u.id=d.manager_id LEFT JOIN machines m ON m.id=d.machine_id
            WHERE 1=1 {scope} ORDER BY d.created_at DESC""",
        params,
    ).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Угоди"
    headers = ["Назва угоди", "Клієнт", "Верстат", "Стадія", "Сума", "Валюта", "Передоплата %",
               "Ймовірність %", "Менеджер", "Очікуване закриття", "Пріоритет", "Створено"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2A2E35")
    for d in deals:
        ws.append([d["title"], d["client_name"], d["machine_name"], STAGE_LABEL.get(d["stage"], d["stage"]),
                   d["amount"], d["currency"], d["prepayment_percent"], d["probability"],
                   d["manager_name"], d["expected_close"], dict(PRIORITIES).get(d["priority"], d["priority"]),
                   d["created_at"][:10] if d["created_at"] else ""])
    for i, w in enumerate([30, 26, 22, 24, 12, 8, 12, 12, 18, 16, 12, 14], start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    from flask import send_file
    return send_file(buf, as_attachment=True, download_name="deals.xlsx",
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------------------
# Керування користувачами (тільки адмін)
# ---------------------------------------------------------------------------

@app.route("/my-profile", methods=["GET", "POST"])
@login_required
def my_profile():
    """Кожен користувач (незалежно від ролі) може змінити власний пароль.
    Раніше паролі міг створювати лише адміністратор при заведенні
    користувача, а змінити його потім було неможливо без прямого
    доступу до бази даних — реальна прогалина для 'чесно робочої' CRM."""
    user = get_current_user()
    if request.method == "POST":
        f = request.form
        current_password = f.get("current_password", "")
        new_password = f.get("new_password", "")
        confirm_password = f.get("confirm_password", "")
        if not check_password_hash(user["password_hash"], current_password):
            flash("Поточний пароль введено невірно", "error")
        elif len(new_password) < 6:
            flash("Новий пароль має містити щонайменше 6 символів", "error")
        elif new_password != confirm_password:
            flash("Новий пароль і підтвердження не збігаються", "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (generate_password_hash(new_password), user["id"]))
            db.commit()
            session.pop("default_password", None)
            flash("Пароль успішно змінено", "success")
            return redirect(url_for("my_profile"))
    return render_template("my_profile.html", active="profile")


@app.route("/users", methods=["GET", "POST"])
@login_required
def users_list():
    if not is_admin():
        flash("Доступно лише адміністратору", "error")
        return redirect(url_for("dashboard"))
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            full_name = validate_required(f, "full_name", "ПІБ")
            username = validate_required(f, "username", "Логін").strip()
            password = f.get("password", "")
            if len(password) < 6:
                raise ValidationError("Пароль має містити щонайменше 6 символів")
        except ValidationError as e:
            flash(str(e), "error")
            return redirect(url_for("users_list"))
        existing = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            flash("Такий логін вже існує", "error")
        else:
            wc_id = f.get("work_center_id") or None
            if f.get("role") != "production":
                wc_id = None  # дільниця має сенс лише для ролі "Виробництво"
            db.execute(
                "INSERT INTO users (username, password_hash, full_name, role, work_center_id) VALUES (?,?,?,?,?)",
                (username, generate_password_hash(password), full_name, f.get("role", "manager"), wc_id),
            )
            db.commit()
            flash("Користувача додано", "success")
        return redirect(url_for("users_list"))
    users = db.execute(
        """SELECT u.*, wc.name as work_center_name FROM users u
           LEFT JOIN work_centers wc ON wc.id=u.work_center_id
           ORDER BY u.active DESC, u.full_name"""
    ).fetchall()
    work_centers = db.execute("SELECT * FROM work_centers ORDER BY name").fetchall()
    return render_template("users.html", active="users", users=users, work_centers=work_centers)


@app.route("/users/<int:user_id>/toggle", methods=["POST"])
@login_required
def user_toggle(user_id):
    if not is_admin():
        abort(403)
    db = get_db()
    if user_id == session.get("user_id"):
        flash("Не можна деактивувати власний обліковий запис", "error")
        return redirect(url_for("users_list"))
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    db.execute("UPDATE users SET active=? WHERE id=?", (0 if user["active"] else 1, user_id))
    db.commit()
    return redirect(url_for("users_list"))


# ---------------------------------------------------------------------------
# Виробництво
# ---------------------------------------------------------------------------

@app.route("/production")
@login_required
@role_required("production", "sales")
def production_list():
    fw = foreman_work_center_id(get_current_user())
    if fw:
        return redirect(url_for("work_center_terminal", wc_id=fw))
    db = get_db()
    rows = db.execute(
        """SELECT o.*, c.name as client_name, d.title as deal_title, m.model as machine_name
           FROM production_orders o
           LEFT JOIN deals d ON d.id=o.deal_id
           LEFT JOIN clients c ON c.id=d.client_id
           LEFT JOIN machines m ON m.id=o.machine_id
           ORDER BY o.created_at DESC"""
    ).fetchall()
    orders = []
    for o in rows:
        stats = db.execute(
            "SELECT COUNT(*) total, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done, MAX(planned_end) pe FROM production_operations WHERE production_order_id=?",
            (o["id"],),
        ).fetchone()
        total, done = stats["total"] or 0, stats["done"] or 0
        d = dict(o)
        d["total_ops"] = total
        d["done_ops"] = done
        d["progress"] = round(done / total * 100) if total else 0
        d["planned_end"] = stats["pe"][:16] if stats["pe"] else None
        orders.append(d)
    return render_template("production.html", active="production", orders=orders)


@app.route("/production/<int:order_id>")
@login_required
@role_required("production", "sales")
def production_detail(order_id):
    fw = foreman_work_center_id(get_current_user())
    if fw:
        return redirect(url_for("work_center_terminal", wc_id=fw))
    db = get_db()
    order = db.execute(
        """SELECT o.*, c.name as client_name, m.model as machine_name
           FROM production_orders o LEFT JOIN deals d ON d.id=o.deal_id
           LEFT JOIN clients c ON c.id=d.client_id LEFT JOIN machines m ON m.id=o.machine_id
           WHERE o.id=?""", (order_id,)
    ).fetchone()
    if not order:
        abort(404)
    ops_rows = db.execute(
        """SELECT op.*, wc.name as wc_name FROM production_operations op
           LEFT JOIN work_centers wc ON wc.id=op.work_center_id
           WHERE op.production_order_id=? ORDER BY op.step_order""",
        (order_id,),
    ).fetchall()
    all_times = [o["planned_start"] for o in ops_rows if o["planned_start"]] + [o["planned_end"] for o in ops_rows if o["planned_end"]]
    operations = []
    if all_times:
        tmin = min(datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S") for t in all_times)
        tmax = max(datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S") for t in all_times)
        span = max((tmax - tmin).total_seconds(), 3600)
        for op in ops_rows:
            d = dict(op)
            if op["planned_start"] and op["planned_end"]:
                s = datetime.datetime.strptime(op["planned_start"], "%Y-%m-%d %H:%M:%S")
                e = datetime.datetime.strptime(op["planned_end"], "%Y-%m-%d %H:%M:%S")
                d["offset_pct"] = round((s - tmin).total_seconds() / span * 100, 1)
                d["width_pct"] = round(max((e - s).total_seconds() / span * 100, 1.5), 1)
            else:
                d["offset_pct"], d["width_pct"] = 0, 5
            operations.append(d)
    else:
        operations = [dict(op, offset_pct=0, width_pct=5) for op in ops_rows]
    return render_template("production_detail.html", active="production", order=order, operations=operations)


@app.route("/production/<int:order_id>/route-sheet.pdf")
@login_required
@role_required("production", "sales")
def production_route_sheet_pdf(order_id):
    """Маршрутний лист (traveler card) — фізичний документ, що супроводжує
    партію деталей від заготовки до готової продукції через усі цехи/дільниці.
    На кожному етапі — місце для підпису й дати, щоб відстежувати, хто і
    коли прийняв деталь у роботу та передав далі."""
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas as pdf_canvas

    db = get_db()
    order = db.execute(
        """SELECT o.*, c.name as client_name, d.title as deal_title, m.model as machine_name, m.category as machine_category
           FROM production_orders o LEFT JOIN deals d ON d.id=o.deal_id
           LEFT JOIN clients c ON c.id=d.client_id LEFT JOIN machines m ON m.id=o.machine_id
           WHERE o.id=?""", (order_id,)
    ).fetchone()
    if not order:
        abort(404)
    ops = db.execute(
        """SELECT op.*, wc.name as wc_name, wc.type as wc_type FROM production_operations op
           LEFT JOIN work_centers wc ON wc.id=op.work_center_id
           WHERE op.production_order_id=? ORDER BY op.step_order""",
        (order_id,),
    ).fetchall()

    buf = BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    steel = colors.HexColor("#2a2e35")

    c.setFillColor(steel)
    c.rect(0, height - 26 * mm, width, 26 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont(PDF_FONT_BOLD, 16)
    c.drawString(20 * mm, height - 12 * mm, "МАРШРУТНИЙ ЛИСТ")
    c.setFont(PDF_FONT, 10)
    c.drawString(20 * mm, height - 19 * mm, f"Виробниче замовлення №{order['id']:04d} · {order['client_name'] or '—'}")

    y = height - 34 * mm
    c.setFillColor(colors.black)
    c.setFont(PDF_FONT_BOLD, 11)
    c.drawString(20 * mm, y, f"Виріб: {order['deal_title'] or '—'}")
    y -= 6 * mm
    c.setFont(PDF_FONT, 10)
    c.drawString(20 * mm, y, f"Обладнання/техпроцес: {order['machine_category'] or '—'} — {order['machine_name'] or '—'}")
    y -= 6 * mm
    c.drawString(20 * mm, y, f"Кількість: {order['quantity']} шт.  ·  Дата видачі листа: {datetime.date.today().strftime('%d.%m.%Y')}")
    y -= 12 * mm

    col_x = [20, 32, 75, 110, 130, 150, 175]
    headers = ["№", "Операція", "Цех/дільниця", "План, год", "Прийняв", "Дата", "Підпис"]
    c.setFillColor(steel)
    c.rect(20 * mm, y - 6 * mm, width - 40 * mm, 6 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont(PDF_FONT_BOLD, 8)
    for x, h in zip(col_x, headers):
        c.drawString(x * mm, y - 4.5 * mm, h)
    y -= 6 * mm

    c.setFillColor(colors.black)
    c.setFont(PDF_FONT, 8)
    row_h = 11 * mm
    for op in ops:
        if y - row_h < 25 * mm:
            c.showPage()
            y = height - 20 * mm
        c.rect(20 * mm, y - row_h, width - 40 * mm, row_h, fill=0, stroke=1)
        c.drawString(col_x[0] * mm, y - 4 * mm, str(op["step_order"]))
        c.drawString(col_x[1] * mm, y - 4 * mm, (op["operation_name"] or "")[:24])
        c.drawString(col_x[2] * mm, y - 4 * mm, (op["wc_type"] or op["wc_name"] or "—")[:16])
        c.drawString(col_x[3] * mm, y - 4 * mm, str(op["planned_hours"]))
        y -= row_h

    y -= 8 * mm
    c.setFont(PDF_FONT, 8)
    c.setFillColor(colors.HexColor("#555555"))
    c.drawString(20 * mm, y, "Цей документ супроводжує партію фізично через усі цехи. Кожен майстер підписує при прийманні у роботу.")

    c.save()
    buf.seek(0)
    from flask import Response
    resp = Response(buf.read(), mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f"inline; filename=route_sheet_{order_id}.pdf"
    return resp


@app.route("/production/<int:order_id>/reschedule", methods=["POST"])
@login_required
@role_required("production")
def production_reschedule(order_id):
    db = get_db()
    schedule_production_order(db, order_id)
    db.commit()
    flash("Розклад перераховано", "success")
    return redirect(url_for("production_detail", order_id=order_id))


@app.route("/production/operations/<int:op_id>/advance", methods=["POST"])
@login_required
@role_required("production")
def operation_advance(op_id):
    db = get_db()
    op = db.execute("SELECT * FROM production_operations WHERE id=?", (op_id,)).fetchone()
    if not op:
        abort(404)
    # Майстер, закріплений за своєю дільницею, тепер може ВІДКРИВАТИ термінали
    # інших дільниць для перегляду (work_center_terminal), але ставити статус
    # операції він і далі може лише на своїй - кнопки дій на чужому терміналі
    # приховані в шаблоні, а ця перевірка захищає від прямого POST в обхід UI.
    fw = foreman_work_center_id(get_current_user())
    if fw and op["work_center_id"] != fw:
        flash("Ви можете позначати статус операцій лише на своїй дільниці", "error")
        if request.referrer and "/terminal" in request.referrer:
            return redirect(request.referrer)
        return redirect(url_for("work_center_terminal", wc_id=fw))
    if op["status"] == "waiting":
        prev = db.execute(
            "SELECT status FROM production_operations WHERE production_order_id=? AND step_order=?",
            (op["production_order_id"], op["step_order"] - 1),
        ).fetchone()
        if prev is not None and prev["status"] != "done":
            flash("Неможливо почати: попередній етап маршруту ще не завершено", "error")
            if request.referrer and "/terminal" in request.referrer:
                return redirect(request.referrer)
            return redirect(url_for("production_detail", order_id=op["production_order_id"]))
        db.execute("UPDATE production_operations SET status='in_progress', actual_start=? WHERE id=?", (now_iso(), op_id))
    elif op["status"] == "in_progress":
        db.execute("UPDATE production_operations SET status='done', actual_end=? WHERE id=?", (now_iso(), op_id))
        remaining = db.execute(
            "SELECT COUNT(*) c FROM production_operations WHERE production_order_id=? AND status!='done'",
            (op["production_order_id"],),
        ).fetchone()["c"]
        if remaining == 0:
            db.execute("UPDATE production_orders SET status='done' WHERE id=?", (op["production_order_id"],))
        else:
            db.execute("UPDATE production_orders SET status='in_progress' WHERE id=? AND status='planned'",
                       (op["production_order_id"],))
    db.commit()
    # Повертаємо оператора туди, звідки він прийшов — на екран свого цеху
    # (термінал) або на деталі замовлення, якщо статус міняли звідти.
    if request.referrer and "/terminal" in request.referrer:
        return redirect(request.referrer)
    return redirect(url_for("production_detail", order_id=op["production_order_id"]))


@app.route("/work-centers/<int:wc_id>/terminal")
@login_required
@role_required("production")
def work_center_terminal(wc_id):
    """Екран для комп'ютера безпосередньо в цеху/дільниці: оператор бачить
    свої поточні завдання (на цьому конкретному робочому центрі, по всіх
    виробничих замовленнях одразу) і великими кнопками сам ставить статус
    деталі/заготовки — без потреби відкривати повний інтерфейс CRM.

    Майстер, закріплений за своєю дільницею (work_center_id), може також
    ВІДКРИВАТИ термінали ІНШИХ дільниць - щоб побачити, на якій стадії
    зараз конкретна деталь по всьому цеху, а не лише на своїй ланці. Але
    керувати (ставити "Почати"/"Завершити") він може лише на СВОЇЙ дільниці
    - на чужій термінал відкривається в режимі лише перегляду, кнопки дій
    приховані (і сервер однаково відхилить спробу натиснути їх напряму -
    див. operation_advance)."""
    fw = foreman_work_center_id(get_current_user())
    can_act = (not fw) or (fw == wc_id)
    db = get_db()
    wc = db.execute("SELECT * FROM work_centers WHERE id=?", (wc_id,)).fetchone()
    if not wc:
        abort(404)
    ops = db.execute(
        """SELECT op.*, po.quantity, d.title as deal_title, c.name as client_name
           FROM production_operations op
           JOIN production_orders po ON po.id=op.production_order_id
           LEFT JOIN deals d ON d.id=po.deal_id
           LEFT JOIN clients c ON c.id=d.client_id
           WHERE op.work_center_id=? AND op.status IN ('waiting','in_progress')
           ORDER BY (op.status='in_progress') DESC, op.planned_start""",
        (wc_id,),
    ).fetchall()
    ops_enriched = []
    for op in ops:
        prev = db.execute(
            "SELECT status FROM production_operations WHERE production_order_id=? AND step_order=?",
            (op["production_order_id"], op["step_order"] - 1),
        ).fetchone()
        d = dict(op)
        d["ready"] = (prev is None) or (prev["status"] == "done")
        ops_enriched.append(d)
    return render_template("terminal.html", active="production", wc=wc, ops=ops_enriched,
                            can_act=can_act, foreman_own_wc=fw)


@app.route("/work-centers")
@login_required
@role_required("production", "sales")
def work_centers_list():
    db = get_db()
    work_centers = db.execute("SELECT * FROM work_centers ORDER BY id").fetchall()
    telemetry = {r["work_center_id"]: r for r in db.execute("SELECT * FROM machine_telemetry").fetchall()}
    runtime_stats = {wc["id"]: get_machine_runtime_stats(db, wc["id"], wc["capacity_hours_per_day"]) for wc in work_centers}
    horizon_end = (datetime.date.today() + datetime.timedelta(days=14)).strftime("%Y-%m-%d")
    utilization = {}
    for wc in work_centers:
        hours = db.execute(
            """SELECT COALESCE(SUM(planned_hours),0) h FROM production_operations
               WHERE work_center_id=? AND status!='done' AND planned_start IS NOT NULL
               AND planned_start <= ?""",
            (wc["id"], horizon_end),
        ).fetchone()["h"]
        capacity = wc["capacity_hours_per_day"] * 14
        utilization[wc["id"]] = min(round(hours / capacity * 100) if capacity else 0, 100)
    return render_template("work_centers.html", active="production", work_centers=work_centers,
                            telemetry=telemetry, utilization=utilization, runtime_stats=runtime_stats,
                            can_connect=is_admin(),
                            foreman_own_wc=foreman_work_center_id(get_current_user()))


# ---------------------------------------------------------------------------
# Змінний журнал виробництва (shift log)
#
# Свідомо НЕ прив'язаний до production_orders/production_operations -
# "незалежний журнал" на явний вибір користувача: швидкий і простий облік
# "скільки хто зробив за зміну", який заповнюється незалежно від того, чи
# заведене відповідне замовлення в CRM.
# ---------------------------------------------------------------------------
@app.route("/shift-log")
@login_required
@role_required("production")
def shift_log_list():
    db = get_db()
    user = get_current_user()
    fw = foreman_work_center_id(user)
    work_centers = db.execute("SELECT * FROM work_centers ORDER BY id").fetchall()
    workers = get_production_workers()

    date_from = request.args.get("date_from") or (datetime.date.today() - datetime.timedelta(days=13)).strftime("%Y-%m-%d")
    date_to = request.args.get("date_to") or today_str()
    f_wc = request.args.get("work_center_id", type=int)
    f_worker = request.args.get("worker_user_id", type=int)

    sql = """SELECT sl.*, wc.name AS work_center_name, w.full_name AS worker_name,
                     cb.full_name AS created_by_name
              FROM shift_logs sl
              LEFT JOIN work_centers wc ON wc.id = sl.work_center_id
              LEFT JOIN users w ON w.id = sl.worker_user_id
              LEFT JOIN users cb ON cb.id = sl.created_by_user_id
              WHERE sl.work_date >= ? AND sl.work_date <= ?"""
    params = [date_from, date_to]
    if f_wc:
        sql += " AND sl.work_center_id=?"
        params.append(f_wc)
    if f_worker:
        sql += " AND sl.worker_user_id=?"
        params.append(f_worker)
    sql += " ORDER BY sl.work_date DESC, sl.id DESC"
    rows = db.execute(sql, params).fetchall()

    logs = []
    totals = {"quantity_made": 0, "quantity_scrap": 0}
    for r in rows:
        d = dict(r)
        d["can_edit"] = can_edit_shift_log(user, r)
        logs.append(d)
        totals["quantity_made"] += r["quantity_made"] or 0
        totals["quantity_scrap"] += r["quantity_scrap"] or 0

    return render_template("shift_log_list.html", active="shift_log", logs=logs, work_centers=work_centers,
                            workers=workers, date_from=date_from, date_to=date_to,
                            f_wc=f_wc, f_worker=f_worker, totals=totals, foreman_own_wc=fw,
                            current_month=today_str()[:7])


def _shift_log_form_context(db, user, log=None):
    return dict(
        work_centers=db.execute("SELECT * FROM work_centers ORDER BY id").fetchall(),
        workers=get_production_workers(),
        log=log,
        foreman_own_wc=foreman_work_center_id(user),
    )


@app.route("/shift-log/new", methods=["GET", "POST"])
@login_required
@role_required("production")
def shift_log_new():
    db = get_db()
    user = get_current_user()
    fw = foreman_work_center_id(user)
    if request.method == "POST":
        f = request.form
        try:
            part_description = validate_required(f, "part_description", "Деталь")
            work_date = f.get("work_date") or today_str()
            quantity_made = int(parse_number(f, "quantity_made", default=0, min_value=0, label="Кількість виготовлено"))
            quantity_scrap = int(parse_number(f, "quantity_scrap", default=0, min_value=0, label="Брак, шт"))
            shift_hours = parse_number(f, "shift_hours", default=None, min_value=0, label="Тривалість зміни, год") if f.get("shift_hours") else None
            setup_hours = parse_number(f, "setup_hours", default=None, min_value=0, label="Години наладки") if f.get("setup_hours") else None
            worker_user_id = f.get("worker_user_id", type=int) or user["id"]
        except ValidationError as e:
            flash(str(e), "error")
            ctx = _shift_log_form_context(db, user)
            return render_template("shift_log_form.html", active="shift_log", **ctx)
        if fw and worker_user_id != user["id"]:
            # Майстер дільниці вносить запис за іншого робітника лише своєї дільниці -
            # work_center_id нижче в будь-якому разі перевизначається на fw для майстра,
            # тож тут достатньо просто дозволити вибір будь-якого працівника.
            pass
        work_center_id = f.get("work_center_id", type=int)
        if fw:
            # Майстер дільниці завжди пише запис на СВОЮ дільницю - так само, як
            # термінал дозволяє діяти лише на своїй дільниці (operation_advance).
            work_center_id = fw
        db.execute(
            """INSERT INTO shift_logs (work_date, shift, work_center_id, worker_user_id, part_description,
               quantity_made, quantity_scrap, scrap_reason, shift_hours, setup_hours, notes,
               created_by_user_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (work_date, f.get("shift", "day"), work_center_id, worker_user_id, part_description,
             quantity_made, quantity_scrap, f.get("scrap_reason") or None, shift_hours, setup_hours,
             f.get("notes") or None, user["id"], now_iso()),
        )
        db.commit()
        flash("Запис змінного журналу додано", "success")
        return redirect(url_for("shift_log_list"))
    ctx = _shift_log_form_context(db, user)
    return render_template("shift_log_form.html", active="shift_log", **ctx)


@app.route("/shift-log/<int:log_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("production")
def shift_log_edit(log_id):
    db = get_db()
    user = get_current_user()
    log = db.execute("SELECT * FROM shift_logs WHERE id=?", (log_id,)).fetchone()
    if not log:
        abort(404)
    if not can_edit_shift_log(user, log):
        flash("Ви можете редагувати лише свої записи або записи своєї дільниці", "error")
        return redirect(url_for("shift_log_list"))
    fw = foreman_work_center_id(user)
    if request.method == "POST":
        f = request.form
        try:
            part_description = validate_required(f, "part_description", "Деталь")
            work_date = f.get("work_date") or log["work_date"]
            quantity_made = int(parse_number(f, "quantity_made", default=0, min_value=0, label="Кількість виготовлено"))
            quantity_scrap = int(parse_number(f, "quantity_scrap", default=0, min_value=0, label="Брак, шт"))
            shift_hours = parse_number(f, "shift_hours", default=None, min_value=0, label="Тривалість зміни, год") if f.get("shift_hours") else None
            setup_hours = parse_number(f, "setup_hours", default=None, min_value=0, label="Години наладки") if f.get("setup_hours") else None
            worker_user_id = f.get("worker_user_id", type=int) or log["worker_user_id"]
        except ValidationError as e:
            flash(str(e), "error")
            ctx = _shift_log_form_context(db, user, log=log)
            return render_template("shift_log_form.html", active="shift_log", **ctx)
        work_center_id = f.get("work_center_id", type=int)
        if fw:
            work_center_id = fw
        db.execute(
            """UPDATE shift_logs SET work_date=?, shift=?, work_center_id=?, worker_user_id=?,
               part_description=?, quantity_made=?, quantity_scrap=?, scrap_reason=?, shift_hours=?,
               setup_hours=?, notes=?, updated_at=? WHERE id=?""",
            (work_date, f.get("shift", "day"), work_center_id, worker_user_id, part_description,
             quantity_made, quantity_scrap, f.get("scrap_reason") or None, shift_hours, setup_hours,
             f.get("notes") or None, now_iso(), log_id),
        )
        db.commit()
        flash("Запис змінного журналу оновлено", "success")
        return redirect(url_for("shift_log_list"))
    ctx = _shift_log_form_context(db, user, log=log)
    return render_template("shift_log_form.html", active="shift_log", **ctx)


@app.route("/shift-log/<int:log_id>/delete", methods=["POST"])
@login_required
@role_required("production")
def shift_log_delete(log_id):
    db = get_db()
    user = get_current_user()
    log = db.execute("SELECT * FROM shift_logs WHERE id=?", (log_id,)).fetchone()
    if not log:
        abort(404)
    if not can_edit_shift_log(user, log):
        flash("Ви можете видаляти лише свої записи або записи своєї дільниці", "error")
        return redirect(url_for("shift_log_list"))
    db.execute("DELETE FROM shift_logs WHERE id=?", (log_id,))
    db.commit()
    flash("Запис видалено", "success")
    return redirect(url_for("shift_log_list"))


@app.route("/shift-log/report.pdf")
@login_required
@role_required("production")
def shift_log_report_pdf():
    """PDF-зведення змінного журналу за місяць - окремий розділ на кожного
    робітника (токаря, фрезерувальника тощо): щоденні записи + підсумок
    виготовлено/брак/години. Зручно роздрукувати або додати до нарахування
    зарплати за виробітком. За замовчуванням - поточний місяць і всі
    робітники; можна звузити до одного робітника через worker_user_id."""
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas as pdf_canvas

    db = get_db()
    month = request.args.get("month") or today_str()[:7]
    if not _re.match(r"^\d{4}-\d{2}$", month):
        month = today_str()[:7]
    worker_filter = request.args.get("worker_user_id", type=int)

    sql = """SELECT sl.*, wc.name AS work_center_name, w.full_name AS worker_name
              FROM shift_logs sl
              LEFT JOIN work_centers wc ON wc.id=sl.work_center_id
              LEFT JOIN users w ON w.id=sl.worker_user_id
              WHERE sl.work_date LIKE ?"""
    params = [f"{month}%"]
    if worker_filter:
        sql += " AND sl.worker_user_id=?"
        params.append(worker_filter)
    sql += " ORDER BY w.full_name, sl.work_date"
    rows = db.execute(sql, params).fetchall()

    by_worker = {}
    for r in rows:
        key = r["worker_user_id"] or 0
        g = by_worker.setdefault(key, {"name": r["worker_name"] or "— без робітника —", "rows": [],
                                        "made": 0, "scrap": 0, "hours": 0.0})
        g["rows"].append(r)
        g["made"] += r["quantity_made"] or 0
        g["scrap"] += r["quantity_scrap"] or 0
        g["hours"] += r["shift_hours"] or 0

    buf = BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    steel = colors.HexColor("#2a2e35")

    def draw_header(worker_name, totals):
        c.setFillColor(steel)
        c.rect(0, height - 26 * mm, width, 26 * mm, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont(PDF_FONT_BOLD, 16)
        c.drawString(20 * mm, height - 12 * mm, f"Змінний журнал — {worker_name}")
        c.setFont(PDF_FONT, 10)
        c.drawString(20 * mm, height - 19 * mm,
                     f"Місяць: {month}   Виготовлено: {totals['made']} шт   "
                     f"Брак: {totals['scrap']} шт   Годин: {totals['hours']:.1f}")

    def draw_table_header(y):
        col_x = [20, 35, 58, 128, 148, 168]
        headers = ["Дата", "Дільниця", "Деталь", "Зроблено", "Брак", "Год."]
        c.setFillColor(steel)
        c.rect(20 * mm, y - 6 * mm, width - 40 * mm, 6 * mm, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont(PDF_FONT_BOLD, 8)
        for x, h in zip(col_x, headers):
            c.drawString(x * mm, y - 4.5 * mm, h)
        c.setFillColor(colors.black)
        c.setFont(PDF_FONT, 8)
        return col_x, y - 6 * mm

    if not by_worker:
        c.setFont(PDF_FONT_BOLD, 14)
        c.drawString(20 * mm, height - 30 * mm, f"Змінний журнал за {month} — записів немає")
        c.showPage()
    else:
        for key in sorted(by_worker, key=lambda k: by_worker[k]["name"]):
            g = by_worker[key]
            draw_header(g["name"], g)
            y = height - 34 * mm
            col_x, y = draw_table_header(y)
            row_h = 7 * mm
            for r in g["rows"]:
                if y - row_h < 25 * mm:
                    c.showPage()
                    y = height - 20 * mm
                    col_x, y = draw_table_header(y)
                c.rect(20 * mm, y - row_h, width - 40 * mm, row_h, fill=0, stroke=1)
                c.drawString(col_x[0] * mm, y - 5 * mm, r["work_date"])
                c.drawString(col_x[1] * mm, y - 5 * mm, (r["work_center_name"] or "—")[:16])
                c.drawString(col_x[2] * mm, y - 5 * mm, (r["part_description"] or "")[:32])
                c.drawString(col_x[3] * mm, y - 5 * mm, str(r["quantity_made"] or 0))
                c.drawString(col_x[4] * mm, y - 5 * mm, str(r["quantity_scrap"] or 0))
                c.drawString(col_x[5] * mm, y - 5 * mm, f"{r['shift_hours']:.1f}" if r["shift_hours"] is not None else "—")
                y -= row_h
            c.showPage()

    c.save()
    buf.seek(0)
    from flask import Response
    resp = Response(buf.read(), mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f"inline; filename=shift_log_{month}.pdf"
    return resp


# ---------------------------------------------------------------------------
# Склад
# ---------------------------------------------------------------------------

@app.route("/warehouse")
@login_required
@role_required("warehouse", "production")
def warehouse_list():
    db = get_db()
    items = db.execute("SELECT * FROM warehouse_items ORDER BY name").fetchall()
    low_stock_count = sum(1 for i in items if i["qty_on_hand"] <= i["min_qty"])
    return render_template("warehouse.html", active="warehouse", items=items, low_stock_count=low_stock_count)


@app.route("/warehouse/scrap")
@login_required
@role_required("warehouse", "production")
def scrap_list():
    db = get_db()
    status_filter = request.args.get("status", "available")
    q = "SELECT * FROM scrap_offcuts WHERE 1=1"
    params = []
    if status_filter:
        q += " AND status=?"
        params.append(status_filter)
    q += " ORDER BY created_at DESC"
    offcuts = db.execute(q, params).fetchall()
    return render_template("scrap.html", active="warehouse", offcuts=offcuts, status_filter=status_filter)


@app.route("/warehouse/scrap/add", methods=["POST"])
@login_required
@role_required("warehouse", "production")
def scrap_add():
    db = get_db()
    f = request.form
    try:
        name = validate_required(f, "material_name", "Матеріал")
    except ValidationError as e:
        flash(str(e), "error")
        return redirect(url_for("scrap_list"))
    db.execute(
        """INSERT INTO scrap_offcuts (material_name, thickness_mm, length_mm, width_mm, weight_kg,
           location, status, created_at) VALUES (?,?,?,?,?,?, 'available', ?)""",
        (name,
         parse_number(f, "thickness_mm", default=None, min_value=0, label="Товщина"),
         parse_number(f, "length_mm", default=None, min_value=0, label="Довжина"),
         parse_number(f, "width_mm", default=None, min_value=0, label="Ширина"),
         parse_number(f, "weight_kg", default=None, min_value=0, label="Вага"),
         f.get("location"), now_iso()),
    )
    db.commit()
    flash("Обрізок зареєстровано", "success")
    return redirect(url_for("scrap_list"))


@app.route("/warehouse/scrap/<int:offcut_id>/use", methods=["POST"])
@login_required
@role_required("warehouse", "production")
def scrap_mark_used(offcut_id):
    db = get_db()
    offcut = db.execute("SELECT * FROM scrap_offcuts WHERE id=?", (offcut_id,)).fetchone()
    if not offcut:
        abort(404)
    new_status = "used" if offcut["status"] == "available" else "available"
    used_at = now_iso() if new_status == "used" else None
    db.execute("UPDATE scrap_offcuts SET status=?, used_at=? WHERE id=?", (new_status, used_at, offcut_id))
    db.commit()
    return redirect(url_for("scrap_list"))


@app.route("/warehouse/scrap/<int:offcut_id>/delete", methods=["POST"])
@login_required
@role_required("warehouse")
def scrap_delete(offcut_id):
    db = get_db()
    db.execute("DELETE FROM scrap_offcuts WHERE id=?", (offcut_id,))
    db.commit()
    flash("Запис видалено", "success")
    return redirect(url_for("scrap_list"))


@app.route("/warehouse/new", methods=["GET", "POST"])
@login_required
@role_required("warehouse")
def warehouse_item_new():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            name = validate_required(f, "name", "Назва")
            qty_on_hand = parse_number(f, "qty_on_hand", default=0, min_value=0, label="Залишок")
            min_qty = parse_number(f, "min_qty", default=0, min_value=0, label="Мінімальний залишок")
            unit_cost = parse_number(f, "unit_cost", default=0, min_value=0, label="Ціна за одиницю")
        except ValidationError as e:
            flash(str(e), "error")
            return render_template("warehouse_item.html", active="warehouse", item=None, transactions=[])
        db.execute(
            """INSERT INTO warehouse_items (sku, name, category, unit, qty_on_hand, min_qty, unit_cost, currency, location)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (f.get("sku"), name, f.get("category"), f.get("unit", "шт"), qty_on_hand,
             min_qty, unit_cost, f.get("currency", "UAH"), f.get("location")),
        )
        db.commit()
        flash("Позицію додано на склад", "success")
        return redirect(url_for("warehouse_list"))
    return render_template("warehouse_item.html", active="warehouse", item=None, transactions=[])


@app.route("/warehouse/<int:item_id>")
@login_required
@role_required("warehouse", "production")
def warehouse_item_detail(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM warehouse_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        abort(404)
    transactions = db.execute(
        """SELECT t.*, u.full_name AS user_name FROM warehouse_transactions t
           LEFT JOIN users u ON u.id = t.user_id
           WHERE t.item_id=? ORDER BY t.created_at DESC""",
        (item_id,),
    ).fetchall()
    lots = db.execute(
        "SELECT * FROM material_lots WHERE item_id=? ORDER BY received_date DESC", (item_id,)
    ).fetchall()
    return render_template("warehouse_item.html", active="warehouse", item=item,
                            transactions=transactions, lots=lots)


@app.route("/warehouse/<int:item_id>/lots", methods=["POST"])
@login_required
@role_required("warehouse")
def material_lot_add(item_id):
    db = get_db()
    f = request.form
    try:
        qty = parse_number(f, "qty", required=True, min_value=0.01, label="Кількість")
    except ValidationError as e:
        flash(str(e), "error")
        return redirect(url_for("warehouse_item_detail", item_id=item_id))
    db.execute(
        """INSERT INTO material_lots (item_id, heat_number, certificate_number, supplier, qty,
           received_date, notes, created_at) VALUES (?,?,?,?,?,?,?,?)""",
        (item_id, f.get("heat_number"), f.get("certificate_number"), f.get("supplier"), qty,
         f.get("received_date") or today_str(), f.get("notes"), now_iso()),
    )
    db.commit()
    flash("Партію матеріалу зареєстровано", "success")
    return redirect(url_for("warehouse_item_detail", item_id=item_id))


@app.route("/warehouse/lots/<int:lot_id>/delete", methods=["POST"])
@login_required
@role_required("warehouse")
def material_lot_delete(lot_id):
    db = get_db()
    lot = db.execute("SELECT item_id FROM material_lots WHERE id=?", (lot_id,)).fetchone()
    if not lot:
        abort(404)
    db.execute("DELETE FROM material_lots WHERE id=?", (lot_id,))
    db.commit()
    return redirect(url_for("warehouse_item_detail", item_id=lot["item_id"]))


@app.route("/warehouse/<int:item_id>/transactions", methods=["POST"])
@login_required
@role_required("warehouse", "production")
def warehouse_transaction_add(item_id):
    """Проводить складську операцію (прихід/списання/інвентаризація).
    Роль 'Склад' може все - прихід нового матеріалу, списання,
    інвентаризацію. Роль 'Виробництво' бачить склад саме для того, щоб
    списати матеріал, який сама витратила на деталь (type='out') - але НЕ
    може оформити прихід чи коригування (type='in'/'adjust'), бо приймання
    постачання й облік залишків - відповідальність комірника, а не цеху."""
    db = get_db()
    f = request.form
    user = get_current_user()
    ttype = f.get("type", "in")
    if user and user["role"] == "production" and ttype != "out":
        flash("Виробництво може лише списувати використаний матеріал — прихід і коригування залишків проводить склад", "error")
        return redirect(url_for("warehouse_item_detail", item_id=item_id))
    try:
        qty = parse_number(f, "qty", required=True, min_value=0.0001, label="Кількість")
    except ValidationError as e:
        flash(str(e), "error")
        return redirect(url_for("warehouse_item_detail", item_id=item_id))
    delta = qty if ttype == "in" else -qty
    item_check = db.execute("SELECT qty_on_hand, name FROM warehouse_items WHERE id=?", (item_id,)).fetchone()
    if not item_check:
        abort(404)
    if ttype != "in" and item_check["qty_on_hand"] + delta < 0:
        flash(f"Неможливо списати {qty} — на залишку лише {item_check['qty_on_hand']}", "error")
        return redirect(url_for("warehouse_item_detail", item_id=item_id))
    db.execute("UPDATE warehouse_items SET qty_on_hand = qty_on_hand + ? WHERE id=?", (delta, item_id))
    db.execute(
        "INSERT INTO warehouse_transactions (item_id, qty_delta, type, reference, user_id, created_at) VALUES (?,?,?,?,?,?)",
        (item_id, delta, ttype, f.get("reference"), session.get("user_id"), now_iso()),
    )
    item = db.execute("SELECT * FROM warehouse_items WHERE id=?", (item_id,)).fetchone()
    if item["qty_on_hand"] <= item["min_qty"]:
        notify_all(f"⚠️ Низький залишок: «{item['name']}» — {item['qty_on_hand']} {item['unit']}")
    db.commit()
    flash("Складську операцію проведено", "success")
    return redirect(url_for("warehouse_item_detail", item_id=item_id))


# ---------------------------------------------------------------------------
# Налаштування та інтеграції
# ---------------------------------------------------------------------------

def make_qr_svg(url):
    """QR-код адреси як inline-SVG (Markup) - щоб відкрити CRM з телефону,
    просто навівши камеру. Бібліотека segno чисто-пітонівська; якщо її
    раптом немає - просто повертаємо None, і сторінка працює без QR."""
    if not url:
        return None
    try:
        import segno
        from markupsafe import Markup
        from io import BytesIO
        buf = BytesIO()
        segno.make(url, error="m").save(buf, kind="svg", scale=4, border=2, dark="#000000", light="#ffffff", xmldecl=False)
        return Markup(buf.getvalue().decode("utf-8"))
    except Exception:
        return None


def get_lan_ip():
    """Локальна IP-адреса цього комп'ютера в мережі - показуємо її в
    «Налаштуваннях», щоб адмін міг назвати/скопіювати адресу колегам,
    не заходячи окремо в системний трей. Той самий трюк з UDP-сокетом,
    що і в launcher.py (навмисно продубльований, а не імпортований -
    launcher.py може бути відсутній, якщо запущено напряму python app.py)."""
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


@app.route("/settings")
@login_required
@role_required()
def settings_page():
    if not is_admin():
        flash("Доступно лише адміністратору", "error")
        return redirect(url_for("dashboard"))
    db = get_db()
    lan_ip = get_lan_ip()
    network_port = os.environ.get("CRM_PORT", "5000")
    network_url = f"http://{lan_ip}:{network_port}" if lan_ip else None
    # Адреса за ім'ям комп'ютера не "пливе" після перезавантаження роутера
    # (на відміну від IP) - зручніша для закладок у браузері колег.
    network_host_url = None
    try:
        import socket as _socket
        hostname = _socket.gethostname()
        if hostname:
            network_host_url = f"http://{hostname}:{network_port}"
    except Exception:
        pass
    return render_template("settings.html", active="settings", settings=get_all_settings(db), detected_chats=None,
                            network_url=network_url, network_host_url=network_host_url,
                            network_qr_svg=make_qr_svg(network_url))


@app.route("/settings/custom-fields", methods=["GET", "POST"])
@login_required
def custom_fields_manage():
    if not is_admin():
        flash("Доступно лише адміністратору", "error")
        return redirect(url_for("dashboard"))
    db = get_db()
    if request.method == "POST":
        f = request.form
        label = (f.get("label") or "").strip()
        if not label:
            flash("Назва поля обов'язкова", "error")
        else:
            field_key = _re.sub(r"[^\w]", "_", label.lower().replace(" ", "_"), flags=_re.UNICODE)
            max_order = db.execute(
                "SELECT COALESCE(MAX(sort_order),0) m FROM custom_field_defs WHERE entity_type=?",
                (f.get("entity_type", "client"),),
            ).fetchone()["m"]
            db.execute(
                """INSERT INTO custom_field_defs (entity_type, field_key, label, field_type, options, sort_order)
                   VALUES (?,?,?,?,?,?)""",
                (f.get("entity_type", "client"), field_key, label, f.get("field_type", "text"),
                 f.get("options"), max_order + 1),
            )
            db.commit()
            flash(f"Поле «{label}» додано", "success")
        return redirect(url_for("custom_fields_manage"))
    fields_by_entity = {
        "client": get_custom_field_defs("client"),
        "deal": get_custom_field_defs("deal"),
    }
    return render_template("custom_fields.html", active="settings", fields_by_entity=fields_by_entity)


@app.route("/settings/custom-fields/<int:field_id>/delete", methods=["POST"])
@login_required
def custom_field_delete(field_id):
    if not is_admin():
        abort(403)
    db = get_db()
    db.execute("DELETE FROM custom_field_values WHERE field_id=?", (field_id,))
    db.execute("DELETE FROM custom_field_defs WHERE id=?", (field_id,))
    db.commit()
    flash("Поле видалено", "success")
    return redirect(url_for("custom_fields_manage"))


@app.route("/settings/save", methods=["POST"])
@login_required
def settings_save():
    if not is_admin():
        abort(403)
    db = get_db()
    f = request.form
    if f.get("form") == "webhook":
        set_setting(db, "webhook_url", f.get("webhook_url", "").strip())
        db.commit()
        if f.get("test"):
            ok, msg = send_webhook_notification("🔧 Тестове повідомлення з Оснастка-Маркет")
            flash("Тестове сповіщення надіслано" if ok else f"Помилка надсилання: {msg}", "success" if ok else "error")
        else:
            flash("Налаштування збережено", "success")
    elif f.get("form") == "telegram":
        set_setting(db, "telegram_bot_token", f.get("telegram_bot_token", "").strip())
        set_setting(db, "telegram_chat_id", f.get("telegram_chat_id", "").strip())
        db.commit()
        if f.get("test"):
            ok, msg = send_telegram_notification("🔧 Тестове повідомлення з Оснастка-Маркет")
            flash("Тестове повідомлення надіслано в Telegram" if ok else f"Помилка Telegram: {msg}", "success" if ok else "error")
        else:
            flash("Налаштування Telegram збережено", "success")
    elif f.get("form") == "rates":
        accepted = save_rates(db, f.get("rate_usd"), f.get("rate_eur"), f.get("rate_pln"), source="вручну")
        if accepted:
            set_setting(db, "rates_auto", "1" if f.get("rates_auto") else "0")
            db.commit()
            flash("Курси валют оновлено", "success")
        else:
            flash("Курс має бути додатним числом", "error")
    elif f.get("form") == "backup":
        set_setting(db, "backup_auto", "1" if f.get("backup_auto") else "0")
        extra = f.get("backup_extra_dir", "").strip()
        if extra and not os.path.isdir(extra):
            try:
                os.makedirs(extra, exist_ok=True)
            except OSError as e:
                flash(f"Теку «{extra}» неможливо створити: {e}", "error")
                return redirect(url_for("settings_page"))
        set_setting(db, "backup_extra_dir", extra)
        db.commit()
        flash("Налаштування копіювання збережено", "success")
    elif f.get("form") == "events":
        set_setting(db, "notify_events", "1" if f.get("notify_events") else "0")
        set_setting(db, "notify_overdue", "1" if f.get("notify_overdue") else "0")
        try:
            days = int(f.get("reorder_days") or 60)
        except ValueError:
            days = 0
        if days < 7:
            flash("Кількість днів має бути не менше 7", "error")
            return redirect(url_for("settings_page"))
        set_setting(db, "reorder_days", str(days))
        db.commit()
        flash("Налаштування сповіщень збережено", "success")
    elif f.get("form") == "anthropic":
        set_setting(db, "anthropic_api_key", f.get("anthropic_api_key", "").strip())
        db.commit()
        flash("API-ключ Anthropic збережено", "success")
    return redirect(url_for("settings_page"))


@app.route("/settings/rates/nbu", methods=["POST"])
@login_required
def settings_rates_nbu():
    if not is_admin():
        abort(403)
    db = get_db()
    try:
        accepted = update_rates_from_nbu(db)
        flash("Курси оновлено з НБУ: " + ", ".join(c.upper() for c in accepted), "success")
    except Exception as e:
        flash(f"Не вдалось отримати курси НБУ: {e}", "error")
    return redirect(url_for("settings_page"))


@app.route("/settings/telegram/detect-chats", methods=["POST"])
@login_required
def settings_telegram_detect_chats():
    if not is_admin():
        abort(403)
    db = get_db()
    f = request.form
    token = f.get("telegram_bot_token", "").strip()
    if not token:
        flash("Спочатку введи токен бота нижче і збережи його", "error")
        return redirect(url_for("settings_page"))
    set_setting(db, "telegram_bot_token", token)
    db.commit()
    try:
        chats = telegram_detect_chats(token)
    except Exception as e:
        flash(f"Не вдалось звʼязатись з Telegram: {e}", "error")
        return redirect(url_for("settings_page"))
    if not chats:
        flash("Поки що нікого не знайдено. Напишіть боту в Telegram будь-яке повідомлення "
              "(або /start) і натисніть цю кнопку ще раз.", "error")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", active="settings", settings=get_all_settings(db), detected_chats=chats)


@app.route("/settings/regenerate-key", methods=["POST"])
@login_required
def settings_regenerate_key():
    if not is_admin():
        abort(403)
    import secrets as _secrets
    db = get_db()
    set_setting(db, "api_key", _secrets.token_hex(16))
    db.commit()
    flash("Новий API-ключ згенеровано", "success")
    return redirect(url_for("settings_page"))


@app.route("/telemetry/simulate", methods=["POST"])
@login_required
def telemetry_simulate():
    """Демо-режим без реального обладнання: генерує 14-денну історію
    напрацювання (наростаючий Motion Time, як у Haas Q301), щоб показати
    роботу аналітики. Реальні дані замінять цю історію після підключення
    адаптера — дивись /mnt/user-data/outputs/integrations/."""
    import random
    db = get_db()
    now = datetime.datetime.now()
    for wc in db.execute("SELECT id FROM work_centers").fetchall():
        wc_id = wc["id"]
        existing = db.execute(
            "SELECT motion_hours, power_on_hours FROM machine_telemetry WHERE work_center_id=?", (wc_id,)
        ).fetchone()
        motion_hours = existing["motion_hours"] if existing and existing["motion_hours"] else random.uniform(800, 4000)
        power_on_hours = existing["power_on_hours"] if existing and existing["power_on_hours"] else motion_hours * 1.6
        part_count = random.randint(1500, 9000)
        # Заднім числом заповнюємо журнал за останні 14 днів з наростаючим naprацюванням,
        # щоб на сторінці "Робочі центри" одразу було видно "сьогодні / за тиждень / всього".
        db.execute("DELETE FROM machine_telemetry_log WHERE work_center_id=?", (wc_id,))
        for day_offset in range(14, -1, -1):
            day_dt = now - datetime.timedelta(days=day_offset)
            daily_hours = random.uniform(3, 7.5) if day_dt.weekday() < 5 else random.uniform(0, 1.5)
            motion_hours += daily_hours
            power_on_hours += daily_hours * 1.3
            status = "running" if day_offset == 0 else random.choice(["running", "idle"])
            db.execute(
                """INSERT INTO machine_telemetry_log (work_center_id, status, spindle_load_pct,
                   motion_hours, power_on_hours, part_count, recorded_at) VALUES (?,?,?,?,?,?,?)""",
                (wc_id, status, random.randint(20, 90), round(motion_hours, 1), round(power_on_hours, 1),
                 part_count, day_dt.strftime("%Y-%m-%d %H:%M:%S")),
            )
        status = random.choice(["running", "running", "running", "idle", "alarm"])
        db.execute(
            """INSERT INTO machine_telemetry (work_center_id, status, spindle_load_pct, cycle_count,
               power_on_hours, motion_hours, part_count, program_name, mode, source, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(work_center_id) DO UPDATE SET status=excluded.status,
               spindle_load_pct=excluded.spindle_load_pct, cycle_count=excluded.cycle_count,
               power_on_hours=excluded.power_on_hours, motion_hours=excluded.motion_hours,
               part_count=excluded.part_count, program_name=excluded.program_name,
               mode=excluded.mode, source=excluded.source, last_seen=excluded.last_seen""",
            (wc_id, status, random.randint(20, 95) if status == "running" else 0, random.randint(100, 9000),
             round(power_on_hours, 1), round(motion_hours, 1), part_count,
             f"O{random.randint(1000,9999)}", "MEM", "demo_simulation", now_iso()),
        )
    db.commit()
    flash("Тестові дані телеметрії та 14-денна історія напрацювання згенеровані", "success")
    return redirect(url_for("work_centers_list"))


@app.route("/api/v1/telemetry/<int:work_center_id>", methods=["POST"])
def api_telemetry(work_center_id):
    """Публічний API-ендпоінт для контролерів верстатів / OPC-UA-шлюзів / адаптерів Haas.
    Автентифікація за заголовком X-API-Key.

    Приймає JSON. Мінімум: {"status": "running"}.
    Для Haas-адаптера (Ethernet Q Commands) додатково очікуються:
      motion_hours   — сумарний час у русі (з Q301), головна метрика напрацювання
      power_on_hours — сумарний час увімкненого стану (з Q300)
      part_count     — лічильник деталей (з Q402/Q500)
      program_name   — активна програма (з Q500)
      mode           — режим контролера (з Q104)
      source         — довільний рядок, напр. "haas_q_commands" або "haas_mtconnect"
    """
    db = get_db()
    api_key = get_setting(db, "api_key", "")
    if not api_key or request.headers.get("X-API-Key") != api_key:
        return jsonify(error="unauthorized"), 401
    data = request.get_json(silent=True) or {}
    record_telemetry(db, work_center_id, data)
    return jsonify(ok=True)


def record_telemetry(db, work_center_id, data):
    """Записує один знімок телеметрії (спільна логіка для HTTP API та
    вбудованого опитування MTConnect)."""
    status = data.get("status", "offline")
    spindle_load = data.get("spindle_load_pct")
    cycle_count = data.get("cycle_count")
    motion_hours = data.get("motion_hours")
    power_on_hours = data.get("power_on_hours")
    part_count = data.get("part_count")
    program_name = data.get("program_name")
    mode = data.get("mode")
    source = data.get("source", "generic")
    db.execute(
        """INSERT INTO machine_telemetry (work_center_id, status, spindle_load_pct, cycle_count,
           power_on_hours, motion_hours, part_count, program_name, mode, source, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(work_center_id) DO UPDATE SET status=excluded.status,
           spindle_load_pct=excluded.spindle_load_pct, cycle_count=excluded.cycle_count,
           power_on_hours=excluded.power_on_hours, motion_hours=excluded.motion_hours,
           part_count=excluded.part_count, program_name=excluded.program_name,
           mode=excluded.mode, source=excluded.source, last_seen=excluded.last_seen""",
        (work_center_id, status, spindle_load, cycle_count, power_on_hours, motion_hours,
         part_count, program_name, mode, source, now_iso()),
    )
    db.execute(
        """INSERT INTO machine_telemetry_log (work_center_id, status, spindle_load_pct,
           motion_hours, power_on_hours, part_count, recorded_at) VALUES (?,?,?,?,?,?,?)""",
        (work_center_id, status, spindle_load, motion_hours, power_on_hours, part_count, now_iso()),
    )
    if status == "alarm":
        notify_all(f"🚨 Аварійний сигнал з робочого центру #{work_center_id} (джерело: {source})!")
    db.commit()


# ---------------------------------------------------------------------------
# Вбудоване опитування верстатів по MTConnect (без окремих скриптів)
# ---------------------------------------------------------------------------

MTC_HOST_RE = _re.compile(r"^[A-Za-z0-9._-]{1,253}$")


def _xml_local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def parse_mtconnect_current(xml_text):
    """Розбирає відповідь MTConnect /current незалежно від версії/namespace.
    Повертає словник для record_telemetry (без source)."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    execution = mode = program = None
    part_count = spindle_load = None
    fault = False
    found = 0
    for el in root.iter():
        tag = _xml_local(el.tag)
        text = (el.text or "").strip()
        if text.upper() in ("UNAVAILABLE", ""):
            text_val = None
        else:
            text_val = text
        ident = ((el.get("name") or "") + " " + (el.get("dataItemId") or "")).lower()
        if tag == "Execution":
            execution = text_val or execution
            found += 1
        elif tag == "ControllerMode":
            mode = text_val or mode
            found += 1
        elif tag == "Program":
            program = text_val or program
            found += 1
        elif tag in ("PartCount", "PartCountAct"):
            if text_val is not None:
                try:
                    part_count = int(float(text_val))
                except ValueError:
                    pass
            found += 1
        elif tag == "Load" and "spindle" in ident or (tag == "Load" and ident.strip().startswith("sload")):
            if text_val is not None:
                try:
                    spindle_load = float(text_val)
                except ValueError:
                    pass
        elif tag == "Fault":
            fault = True
            found += 1
    if not found:
        raise ValueError("У відповіді немає жодного відомого MTConnect-елемента (Execution, Program, PartCount)")
    exe = (execution or "").upper()
    if fault:
        status = "alarm"
    elif exe == "ACTIVE":
        status = "running"
    elif exe:
        status = "idle"
    else:
        status = "offline"
    return {"status": status, "program_name": program, "mode": mode,
            "part_count": part_count, "spindle_load_pct": spindle_load}


def fetch_mtconnect(host, port, timeout=4):
    if not MTC_HOST_RE.match(host or ""):
        raise ValueError("Некоректна адреса верстата")
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("Некоректний порт")
    import urllib.request
    with urllib.request.urlopen(f"http://{host}:{port}/current", timeout=timeout) as resp:
        body = resp.read(3_000_000)
    return parse_mtconnect_current(body.decode("utf-8", errors="replace").encode("utf-8"))


def poll_machines_once():
    """Опитує всі робочі центри з увімкненим MTConnect. Повертає {wc_id: помилка|None}."""
    results = {}
    with app.app_context():
        db = get_db()
        rows = db.execute("SELECT id, mtc_host, mtc_port FROM work_centers "
                          "WHERE mtc_enabled=1 AND mtc_host IS NOT NULL AND mtc_host!=''").fetchall()
        for wc in rows:
            try:
                data = fetch_mtconnect(wc["mtc_host"], wc["mtc_port"] or 8082)
                data["source"] = "mtconnect"
                record_telemetry(db, wc["id"], data)
                results[wc["id"]] = None
            except Exception as e:
                results[wc["id"]] = str(e)
                prev = db.execute("SELECT status FROM machine_telemetry WHERE work_center_id=?", (wc["id"],)).fetchone()
                if prev and prev["status"] != "offline":
                    record_telemetry(db, wc["id"], {"status": "offline", "source": "mtconnect"})
    return results


import threading as _threading
_poller_stop = _threading.Event()
_poller_started = False


def start_machine_poller(interval=None):
    """Запускає фоновий потік опитування (викликається з launcher.py)."""
    global _poller_started
    if _poller_started:
        return False
    _poller_started = True
    import threading
    secs = interval or int(os.environ.get("CRM_MTC_INTERVAL", "15"))
    _poller_stop.clear()

    def loop():
        while not _poller_stop.is_set():
            try:
                poll_machines_once()
            except Exception:
                pass
            try:
                run_daily_tasks()
            except Exception:
                pass
            _poller_stop.wait(secs)

    threading.Thread(target=loop, daemon=True, name="mtconnect-poller").start()
    return True


def stop_machine_poller():
    _poller_stop.set()


@app.route("/work-centers/<int:wc_id>/connect", methods=["POST"])
@login_required
def work_center_connect(wc_id):
    if not is_admin():
        abort(403)
    db = get_db()
    wc = db.execute("SELECT id, name FROM work_centers WHERE id=?", (wc_id,)).fetchone()
    if not wc:
        abort(404)
    host = request.form.get("mtc_host", "").strip()
    if not host:
        db.execute("UPDATE work_centers SET mtc_host=NULL, mtc_enabled=0 WHERE id=?", (wc_id,))
        db.commit()
        flash(f"Підключення «{wc['name']}» вимкнено", "success")
        return redirect(url_for("work_centers_list"))
    try:
        port = int(request.form.get("mtc_port") or 8082)
    except ValueError:
        port = 0
    if not MTC_HOST_RE.match(host) or not 1 <= port <= 65535:
        flash("Перевірте адресу (лише IP або ім'я без http://) і порт", "error")
        return redirect(url_for("work_centers_list"))
    db.execute("UPDATE work_centers SET mtc_host=?, mtc_port=?, mtc_enabled=1 WHERE id=?", (host, port, wc_id))
    db.commit()
    try:
        data = fetch_mtconnect(host, port)
        data["source"] = "mtconnect"
        record_telemetry(db, wc_id, data)
        flash(f"Зв'язок є: «{wc['name']}» — статус {data['status']}, програма {data.get('program_name') or '—'}. "
              "Далі дані оновлюються автоматично.", "success")
    except Exception as e:
        flash(f"Адресу збережено, але верстат не відповів: {e}. Перевірте IP, мережу та налаштування 143 на Haas. "
              "CRM пробуватиме знову автоматично.", "error")
    return redirect(url_for("work_centers_list"))


# ---------------------------------------------------------------------------
# Вкладення файлів
# ---------------------------------------------------------------------------

ATTACHMENT_ENTITY_TABLES = {
    "client": "clients",
    "deal": "deals",
    "service_ticket": "service_tickets",
}


def _attachment_entity_exists(entity_type, entity_id):
    table = ATTACHMENT_ENTITY_TABLES.get(entity_type)
    if not table:
        return False
    db = get_db()
    return db.execute(f"SELECT 1 FROM {table} WHERE id=?", (entity_id,)).fetchone() is not None


@app.route("/attachments/<entity_type>/<int:entity_id>/upload", methods=["POST"])
@login_required
def attachment_upload(entity_type, entity_id):
    if not _attachment_entity_exists(entity_type, entity_id):
        abort(404)
    file_storage = request.files.get("file")
    ok, error = save_uploaded_file(file_storage, entity_type, entity_id)
    if ok:
        flash("Файл завантажено", "success")
    else:
        flash(error, "error")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/attachments/<int:attachment_id>/download")
@login_required
def attachment_download(attachment_id):
    db = get_db()
    att = db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    if not att:
        abort(404)
    directory = os.path.join(UPLOAD_DIR, att["entity_type"], str(att["entity_id"]))
    from flask import send_from_directory
    # as_attachment=True — примусово завантажує файл, а не показує в браузері.
    # Це важливо саме з міркувань безпеки: якщо колись хтось завантажить
    # файл із розширенням, що браузер вміє відкрити напряму (напр. .txt чи
    # зображення з підробленим вмістом), примусове завантаження виключає
    # виконання вмісту в контексті сторінки CRM.
    return send_from_directory(directory, att["stored_name"], as_attachment=True,
                                download_name=att["original_name"])


@app.route("/attachments/<int:attachment_id>/delete", methods=["POST"])
@login_required
def attachment_delete(attachment_id):
    db = get_db()
    att = db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    if not att:
        abort(404)
    path = os.path.join(UPLOAD_DIR, att["entity_type"], str(att["entity_id"]), att["stored_name"])
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    db.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
    db.commit()
    flash("Файл видалено", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/search")
@login_required
def search():
    q = request.args.get("q", "").strip()
    db = get_db()
    clients, deals, calculations = [], [], []
    user = get_current_user()
    # Розрахунки вартості показуємо лише тим, хто й так має доступ до
    # калькулятора/історії (сторінка /calculator/history теж захищена
    # role_required("sales")) - інакше пошук розкривав би ціни ролям,
    # яким цей розділ узагалі не показаний у меню.
    show_calculations = bool(user) and user["role"] in ("admin", "sales", "viewer")
    if q:
        like = f"%{q}%"
        clients = db.execute("SELECT * FROM clients WHERE name LIKE ? ORDER BY name", (like,)).fetchall()
        deals = db.execute(
            """SELECT d.* FROM deals d WHERE d.title LIKE ?
               OR d.id IN (SELECT id FROM deals WHERE client_id IN
                 (SELECT id FROM clients WHERE name LIKE ?))""",
            (like, like),
        ).fetchall()
        if show_calculations:
            calculations = db.execute(
                "SELECT * FROM cost_calculations WHERE part_description LIKE ? "
                "ORDER BY created_at DESC LIMIT 50",
                (like,),
            ).fetchall()
    return render_template("search.html", active="", q=q, clients=clients, deals=deals,
                            calculations=calculations, show_calculations=show_calculations)


# ---------------------------------------------------------------------------
# Точка входу
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Логування, обробники помилок, резервне копіювання
# ---------------------------------------------------------------------------

LOG_DIR = os.path.join(DATA_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def setup_logging():
    import logging
    from logging.handlers import RotatingFileHandler
    handler = RotatingFileHandler(os.path.join(LOG_DIR, "crm.log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.setLevel(logging.WARNING)
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.WARNING)


setup_logging()


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Сторінку не знайдено"), 404


@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message="Доступ заборонено"), 403


@app.errorhandler(500)
def server_error(e):
    # Клієнту показуємо загальне повідомлення (ніколи не показуємо стектрейс
    # чи деталі винятку — це може розкрити структуру бази чи коду), деталі
    # пишемо тільки в лог-файл на сервері.
    app.logger.exception("Внутрішня помилка сервера: %s", e)
    return render_template("error.html", code=500,
                            message="Сталася внутрішня помилка. Її вже записано в лог для адміністратора."), 500


def backup_database():
    """Створює консистентну копію бази даних через офіційний backup API SQLite
    (безпечно навіть якщо в цей момент триває запис — на відміну від простого
    копіювання файлу, яке може дати пошкоджену копію)."""
    backup_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(backup_dir, f"crm_backup_{ts}.db")
    source = sqlite3.connect(DB_PATH)
    dest = sqlite3.connect(dest_path)
    with dest:
        source.backup(dest)
    source.close()
    dest.close()
    # Лишаємо тільки останні 30 копій, щоб диск не переповнювався при
    # регулярному (напр. щоденному) запуску через cron/планувальник.
    backups = sorted(
        [f for f in os.listdir(backup_dir) if f.startswith("crm_backup_") and f.endswith(".db")]
    )
    for old in backups[:-30]:
        try:
            os.remove(os.path.join(backup_dir, old))
        except OSError:
            pass
    return dest_path


def run_scheduled_backup(db=None):
    """Раз на день робить копію бази (+ копію в додаткову теку, якщо задана).
    Повертає шлях до копії або None, якщо сьогодні вже робили/вимкнено."""
    own = db is None
    if own:
        db = sqlite3.connect(DB_PATH, timeout=10)
        db.row_factory = sqlite3.Row
    try:
        if get_setting(db, "backup_auto", "1") != "1" or get_setting(db, "backup_last_on", "") == today_str():
            return None
        path = backup_database()
        error = ""
        extra = get_setting(db, "backup_extra_dir", "").strip()
        if extra:
            try:
                import shutil
                os.makedirs(extra, exist_ok=True)
                shutil.copy2(path, os.path.join(extra, os.path.basename(path)))
                names = sorted(f for f in os.listdir(extra) if f.startswith("crm_backup_") and f.endswith(".db"))
                for old in names[:-30]:
                    try:
                        os.remove(os.path.join(extra, old))
                    except OSError:
                        pass
            except Exception as e:
                error = str(e)
        set_setting(db, "backup_last_on", today_str())
        set_setting(db, "backup_last_error", error)
        db.commit()
        return path
    finally:
        if own:
            db.close()


def run_daily_tasks():
    """Щоденні фонові завдання: курс НБУ (якщо ввімкнено) і резервна копія."""
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        if get_setting(db, "rates_auto", "0") == "1" and get_setting(db, "rates_updated_on", "") != today_str():
            last_try = float(get_setting(db, "rates_last_try", "0") or 0)
            if time.time() - last_try > 3600:  # при збої НБУ повторюємо не частіше разу на годину
                set_setting(db, "rates_last_try", str(time.time()))
                db.commit()
                try:
                    update_rates_from_nbu(db)
                except Exception:
                    pass
        run_scheduled_backup(db)
    finally:
        db.close()
    try:
        send_overdue_reminder()
    except Exception:
        pass


@app.route("/settings/backup", methods=["POST"])
@login_required
def settings_backup_now():
    if not is_admin():
        abort(403)
    path = backup_database()
    flash(f"Резервну копію створено: {os.path.basename(path)}", "success")
    return redirect(url_for("settings_page"))


def reset_to_demo_data(db):
    """Повністю очищує базу і наповнює її актуальними демо-даними — та сама
    логіка, що виконується автоматично при першому запуску (fresh install),
    але викликана вручну на вже існуючій базі. Навмисно НЕ видаляє файл
    crm.db з диска (на відміну від видалення файлу вручну): просто очищує
    всі таблиці через SQL, щоб однаково надійно працювати і в зібраному
    .exe, і без нього, без ризику лишити файл заблокованим на Windows."""
    tables = [
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    db.execute("PRAGMA foreign_keys = OFF")
    for table in tables:
        db.execute(f"DELETE FROM {table}")
    # Скидаємо лічильники AUTOINCREMENT, щоб нові id знову починались з 1 —
    # seed_data() посилається на конкретні числові id (client_id=6 тощо),
    # тож без цього кроку демо-дані прив'язались би не до тих рядків.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        db.execute("DELETE FROM sqlite_sequence")
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")
    seed_data(db)
    db.commit()


@app.route("/settings/reset-demo-data", methods=["POST"])
@login_required
def settings_reset_demo_data():
    if not is_admin():
        abort(403)
    confirm = (request.form.get("confirm") or "").strip()
    if confirm != "СКИНУТИ":
        flash('Для підтвердження введіть слово "СКИНУТИ" точно як написано.', "error")
        return redirect(url_for("settings_page"))
    # Завжди робимо резервну копію перед повним скиданням — щоб реальні дані
    # (якщо це не свіжа тестова машина) можна було відновити з backups/.
    try:
        backup_database()
    except Exception as e:
        flash(f"Не вдалося створити резервну копію перед скиданням, скасовано: {e}", "error")
        return redirect(url_for("settings_page"))
    db = get_db()
    reset_to_demo_data(db)
    flash("Базу скинуто й наповнено актуальними демо-даними. Резервну копію попереднього стану збережено в backups/.", "success")
    return redirect(url_for("settings_page"))


send_overdue_reminder = lambda: False
import features_ext  # noqa: E402  (додаткові розділи: деталі, якість, план/факт, дошка, аудит)
features_ext.register(sys.modules[__name__])

init_db()  # ІНІЦІАЛІЗАЦІЯ БАЗИ ДАНИХ (виконується один раз при старті процесу)

if __name__ == "__main__":
    # БЕЗПЕКА: debug=True разом з host="0.0.0.0" — критична вразливість
    # (інтерактивний веб-дебагер Werkzeug дозволяє віддалене виконання коду
    # будь-кому в мережі). Тому за замовчуванням: debug вимкнено, слухаємо
    # лише localhost. Для доступу з інших пристроїв у цеховій мережі —
    # свідомо став host="0.0.0.0" МИНУВШИ debug=True, і став окремий
    # надійний CRM_SECRET_KEY через змінну середовища.
    debug_mode = os.environ.get("CRM_DEBUG", "0") == "1"
    host = os.environ.get("CRM_HOST", "127.0.0.1")
    start_machine_poller()
    app.run(debug=debug_mode, host=host, port=int(os.environ.get("CRM_PORT", "5000")))
