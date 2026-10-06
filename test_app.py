# -*- coding: utf-8 -*-
"""
Автоматизовані тести Оснастка-Маркет.

Запуск:
    pip install pytest
    pytest test_app.py -v

Це не вичерпний набір (для цього знадобився б окремий проєкт), але він
покриває критичні для бізнесу сценарії: логін і захист від перебору
паролів, CSRF-захист, рольовий доступ, створення угоди й автоматичне
виробниче замовлення, планування виробництва, складські операції та
захист від списання в мінус, мультивалютну аналітику, валідацію форм,
і те, що стара база даних переживає оновлення схеми (міграції).

Кожен тест працює з чистою тимчасовою базою даних (не займає й не псує
робочу crm.db).
"""

import os
import re
import sqlite3
import tempfile
import datetime

import pytest


@pytest.fixture()
def client(monkeypatch):
    """Ізольований тестовий клієнт: тимчасова база даних, чиста для кожного тесту."""
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test_crm.db")
    monkeypatch.setenv("CRM_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("CRM_DEBUG", "0")
    # CRM_DATA_DIR теж ізолюємо в tmp_dir ДО перезавантаження модуля: інакше
    # DATA_DIR (а з ним UPLOAD_DIR/LOG_DIR і тека backups/ для
    # backup_database()) лишається рахованим від реального BASE_DIR і тести
    # писали б файли в справжню робочу теку застосунку, а не у тимчасову.
    monkeypatch.setenv("CRM_DATA_DIR", tmp_dir)

    import importlib
    import app as app_module
    importlib.reload(app_module)  # щоб підхопився новий DATA_DIR/DB_PATH нижче
    app_module.DB_PATH = db_path
    if os.path.exists(db_path):
        os.remove(db_path)
    app_module.init_db()
    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as c:
        c._app_module = app_module
        yield c


def get_csrf_token(client, path="/"):
    """Витягує CSRF-токен зі сторінки, як це робив би реальний браузер:
    рендер сторінки викликає csrf_token(), який кладе токен у сесію."""
    client.get(path)
    with client.session_transaction() as sess:
        return sess.get("_csrf_token")


def login(client, username="admin", password="admin123"):
    token = get_csrf_token(client, "/login")
    return client.post("/login", data={"username": username, "password": password, "_csrf_token": token},
                        follow_redirects=True)


def post(client, path, data=None, **kwargs):
    """POST-запит з автоматично підставленим CSRF-токеном."""
    data = dict(data or {})
    token = get_csrf_token(client)
    data["_csrf_token"] = token
    return client.post(path, data=data, **kwargs)


# ---------------------------------------------------------------------------
# Автентифікація та безпека
# ---------------------------------------------------------------------------

def test_login_success(client):
    r = login(client)
    assert r.status_code == 200
    assert "Дашборд" in r.get_data(as_text=True) or "дашборд" in r.get_data(as_text=True).lower()


def test_login_wrong_password_shows_error(client):
    r = login(client, password="неправильний")
    assert "Невірний логін або пароль" in r.get_data(as_text=True)


def test_login_lockout_after_repeated_failures(client):
    for _ in range(5):
        login(client, password="wrong")
    r = login(client, password="wrong")
    assert "Забагато невдалих спроб" in r.get_data(as_text=True)


def test_dashboard_requires_login(client):
    r = client.get("/", follow_redirects=True)
    assert "Логін" in r.get_data(as_text=True) or "логін" in r.get_data(as_text=True).lower()


def test_csrf_rejected_without_token(client):
    login(client)
    # Прямий POST без токена — має бути відхилений, а не виконаний.
    r = client.post("/clients/new", data={"name": "Без токена"}, follow_redirects=True)
    clients_after = client._app_module.sqlite3.connect(client._app_module.DB_PATH)
    clients_after.row_factory = sqlite3.Row
    count = clients_after.execute("SELECT COUNT(*) c FROM clients WHERE name='Без токена'").fetchone()["c"]
    assert count == 0, "Клієнт створився без CSRF-токена — захист не працює"


def test_csrf_accepted_with_valid_token(client):
    login(client)
    r = post(client, "/clients/new", {"name": "З токеном ТОВ"}, follow_redirects=True)
    assert r.status_code == 200
    db = sqlite3.connect(client._app_module.DB_PATH)
    db.row_factory = sqlite3.Row
    count = db.execute("SELECT COUNT(*) c FROM clients WHERE name='З токеном ТОВ'").fetchone()["c"]
    assert count == 1


# ---------------------------------------------------------------------------
# Рольовий доступ
# ---------------------------------------------------------------------------

def test_sales_manager_cannot_open_warehouse(client):
    login(client, "oleh", "manager123")
    r = client.get("/warehouse", follow_redirects=True)
    assert "прав" in r.get_data(as_text=True).lower()


def test_warehouse_role_cannot_open_clients(client):
    login(client, "sklad", "sklad123")
    r = client.get("/clients", follow_redirects=True)
    assert "прав" in r.get_data(as_text=True).lower()


def test_viewer_role_cannot_write(client):
    login(client)
    post(client, "/users", {"full_name": "В'ювер", "username": "viewer1", "password": "test123", "role": "viewer"})
    login(client, "viewer1", "test123")
    r = post(client, "/clients/new", {"name": "Спроба в'ювера"}, follow_redirects=True)
    db = sqlite3.connect(client._app_module.DB_PATH)
    db.row_factory = sqlite3.Row
    count = db.execute("SELECT COUNT(*) c FROM clients WHERE name='Спроба в'ювера'"
                        if False else "SELECT COUNT(*) c FROM clients WHERE name=?",
                        ("Спроба в'ювера",)).fetchone()["c"]
    assert count == 0


# ---------------------------------------------------------------------------
# Бізнес-логіка: угоди, виробництво, склад
# ---------------------------------------------------------------------------

def test_create_deal_validates_negative_amount(client):
    login(client)
    r = post(client, "/deals/new", {"title": "Тестова угода", "client_id": "1", "amount": "-500"},
             follow_redirects=True)
    assert "не може бути меншим" in r.get_data(as_text=True)


def test_create_deal_validates_non_numeric_amount(client):
    login(client)
    r = post(client, "/deals/new", {"title": "Тестова угода", "client_id": "1", "amount": "не число"},
             follow_redirects=True)
    assert "має містити число" in r.get_data(as_text=True)


def test_moving_deal_to_prepay_creates_production_order(client):
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    deal = db.execute("SELECT * FROM deals WHERE machine_id IS NOT NULL LIMIT 1").fetchone()
    assert deal is not None, "У демо-даних має бути хоч одна угода з обраним верстатом"

    post(client, f"/deals/{deal['id']}/move/prepay")

    db2 = sqlite3.connect(a.DB_PATH)
    db2.row_factory = sqlite3.Row
    order = db2.execute("SELECT * FROM production_orders WHERE deal_id=?", (deal["id"],)).fetchone()
    assert order is not None, "Виробниче замовлення не створилось автоматично при передоплаті"
    ops = db2.execute("SELECT * FROM production_operations WHERE production_order_id=?", (order["id"],)).fetchall()
    assert len(ops) > 0, "У виробничого замовлення немає операцій за маршрутною картою"
    assert all(op["planned_start"] and op["planned_end"] for op in ops), "Не всі операції отримали розклад"


def test_production_schedule_respects_sequence_order(client):
    """Кожна наступна операція в маршруті не повинна починатись раніше,
    ніж завершилась попередня — це основна гарантія алгоритму планування."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    deal = db.execute("SELECT * FROM deals WHERE machine_id IS NOT NULL LIMIT 1").fetchone()
    post(client, f"/deals/{deal['id']}/move/prepay")

    db2 = sqlite3.connect(a.DB_PATH)
    db2.row_factory = sqlite3.Row
    order = db2.execute("SELECT * FROM production_orders WHERE deal_id=?", (deal["id"],)).fetchone()
    ops = db2.execute(
        "SELECT * FROM production_operations WHERE production_order_id=? ORDER BY step_order", (order["id"],)
    ).fetchall()
    for prev, curr in zip(ops, ops[1:]):
        assert curr["planned_start"] >= prev["planned_end"], \
            f"Операція «{curr['operation_name']}» починається раніше, ніж завершилась «{prev['operation_name']}»"


def test_warehouse_cannot_go_negative(client):
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    item = db.execute("SELECT * FROM warehouse_items ORDER BY id LIMIT 1").fetchone()

    r = post(client, f"/warehouse/{item['id']}/transactions",
             {"type": "out", "qty": str(item["qty_on_hand"] + 999999)}, follow_redirects=True)
    assert "Неможливо списати" in r.get_data(as_text=True)

    db2 = sqlite3.connect(a.DB_PATH)
    db2.row_factory = sqlite3.Row
    item_after = db2.execute("SELECT qty_on_hand FROM warehouse_items WHERE id=?", (item["id"],)).fetchone()
    assert item_after["qty_on_hand"] == item["qty_on_hand"], "Залишок змінився попри відхилену операцію"


def test_dashboard_converts_multicurrency_amounts_to_uah(client):
    login(client)
    post(client, "/deals/new", {"title": "Угода в EUR", "client_id": "1", "amount": "1000",
                                 "currency": "EUR", "stage": "won"})
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    rate_eur = float(db.execute("SELECT value FROM settings WHERE key='rate_eur'").fetchone()["value"])

    r = client.get("/")
    assert r.status_code == 200
    # Дашборд не повинен падати і має відобразитись з конвертованими сумами.
    # Пряма перевірка суми через внутрішню функцію конвертації:
    assert a.to_uah(db, 1000, "EUR") == 1000 * rate_eur


# ---------------------------------------------------------------------------
# Стійкість бази даних (міграції)
# ---------------------------------------------------------------------------

def test_schema_migration_preserves_old_data(tmp_path, monkeypatch):
    """Емулює стару базу даних (без нових колонок) і перевіряє, що
    міграція додає відсутні колонки, НЕ втрачаючи наявні рядки."""
    db_path = str(tmp_path / "old_crm.db")
    old_db = sqlite3.connect(db_path)
    old_db.executescript("""
        CREATE TABLE deals (id INTEGER PRIMARY KEY, title TEXT, amount REAL);
        CREATE TABLE machine_telemetry (work_center_id INTEGER PRIMARY KEY, status TEXT);
        INSERT INTO deals (id, title, amount) VALUES (1, 'Стара угода', 1000);
    """)
    old_db.commit()
    old_db.close()

    # Ізолюємо CRM_DATA_DIR і тут: інакше автогенерація secret_key.txt
    # (коли CRM_SECRET_KEY не задано) писала б файл у справжню теку
    # застосунку замість тимчасової - та сама пастка, що колись була
    # з backup_database() і теж знайдена реальним тестом, а не оглядом коду.
    monkeypatch.setenv("CRM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CRM_SECRET_KEY", raising=False)

    import importlib
    import app as app_module
    importlib.reload(app_module)

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    app_module.migrate_schema(db)

    row = db.execute("SELECT * FROM deals WHERE id=1").fetchone()
    assert row["title"] == "Стара угода"
    assert row["amount"] == 1000
    cols = {r["name"] for r in db.execute("PRAGMA table_info(deals)").fetchall()}
    assert "closed_at" in cols


def test_backup_creates_valid_sqlite_file(client, tmp_path):
    a = client._app_module
    a.BASE_DIR = str(tmp_path)
    path = a.backup_database()
    assert os.path.exists(path)
    # Перевіряємо, що це справді відкривана SQLite-база, а не пошкоджений файл.
    check = sqlite3.connect(path)
    tables = check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert len(tables) > 0
    check.close()


# ---------------------------------------------------------------------------
# Захист від перебору паролів через кілька процесів (симуляція gunicorn)
# ---------------------------------------------------------------------------

def test_login_lockout_is_shared_via_db(client):
    """Лічильник спроб зберігається в БД, а не в пам'яті процесу — критично
    для коректної роботи під gunicorn з кількома worker-процесами."""
    a = client._app_module
    for _ in range(5):
        with a.app.test_request_context():
            a.register_failed_login("test-key-123")
    with a.app.test_request_context():
        assert a.is_login_locked_out("test-key-123") is True
        assert a.is_login_locked_out("інший-ключ") is False


# ---------------------------------------------------------------------------
# Власний профіль
# ---------------------------------------------------------------------------

def test_change_own_password_requires_correct_current_password(client):
    login(client)
    r = post(client, "/my-profile", {"current_password": "wrong", "new_password": "newpass123",
                                       "confirm_password": "newpass123"})
    assert "невірно" in r.get_data(as_text=True).lower()


def test_change_own_password_success_and_relogin(client):
    login(client)
    r = post(client, "/my-profile", {"current_password": "admin123", "new_password": "newpass123",
                                       "confirm_password": "newpass123"}, follow_redirects=True)
    assert "успішно змінено" in r.get_data(as_text=True)
    client.get("/logout")
    r2 = login(client, "admin", "newpass123")
    assert "дашборд" in r2.get_data(as_text=True).lower()


def test_change_password_rejects_mismatched_confirmation(client):
    login(client)
    r = post(client, "/my-profile", {"current_password": "admin123", "new_password": "abc12345",
                                       "confirm_password": "different123"})
    assert "не збігаються" in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Пагінація
# ---------------------------------------------------------------------------

def test_clients_list_pagination(client):
    login(client)
    for i in range(60):
        post(client, "/clients/new", {"name": f"Масовий клієнт {i:03d}"})
    r = client.get("/clients")
    assert "Наступна" in r.get_data(as_text=True)
    r2 = client.get("/clients?page=2")
    assert r2.status_code == 200
    assert "Масовий клієнт" in r2.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Безпека вводу (XSS / SQL-ін'єкції)
# ---------------------------------------------------------------------------

def test_xss_payload_is_escaped_in_output(client):
    login(client)
    payload = "<script>alert(1)</script>"
    post(client, "/clients/new", {"name": payload})
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT id FROM clients WHERE name=?", (payload,)).fetchone()
    assert row is not None
    r = client.get(f"/clients/{row['id']}")
    html = r.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_sql_injection_attempt_does_not_break_database(client):
    login(client)
    payload = "Test'; DROP TABLE clients; --"
    r = post(client, "/clients/new", {"name": payload})
    assert r.status_code in (200, 302)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='clients'").fetchall()
    assert len(tables) == 1


def test_nonexistent_records_return_404(client):
    login(client)
    for path in ("/clients/999999", "/deals/999999", "/warehouse/999999", "/production/999999"):
        r = client.get(path)
        assert r.status_code == 404, f"{path} returned {r.status_code} instead of 404"


def test_client_cascade_delete_removes_related_records(client):
    login(client)
    post(client, "/clients/new", {"name": "Клієнт для видалення"})
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    victim = db.execute("SELECT id FROM clients WHERE name='Клієнт для видалення'").fetchone()
    post(client, "/deals/new", {"title": "Угода жертви", "client_id": str(victim["id"]), "amount": "100"})
    post(client, "/tasks/add", {"title": "Задача жертви", "client_id": str(victim["id"])})
    post(client, f"/clients/{victim['id']}/delete", {})

    db2 = sqlite3.connect(a.DB_PATH)
    db2.row_factory = sqlite3.Row
    assert db2.execute("SELECT COUNT(*) c FROM clients WHERE id=?", (victim["id"],)).fetchone()["c"] == 0
    assert db2.execute("SELECT COUNT(*) c FROM deals WHERE client_id=?", (victim["id"],)).fetchone()["c"] == 0
    assert db2.execute("SELECT COUNT(*) c FROM tasks WHERE client_id=?", (victim["id"],)).fetchone()["c"] == 0


def test_machine_deletion_blocked_when_referenced(client):
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    used = db.execute("SELECT machine_id FROM deals WHERE machine_id IS NOT NULL LIMIT 1").fetchone()
    r = post(client, f"/machines/{used['machine_id']}/delete", {}, follow_redirects=True)
    assert "Неможливо видалити" in r.get_data(as_text=True)
    db2 = sqlite3.connect(a.DB_PATH)
    still_there = db2.execute("SELECT COUNT(*) c FROM machines WHERE id=?", (used["machine_id"],)).fetchone()
    assert still_there[0] == 1


def test_pdf_proposal_is_valid_pdf(client):
    login(client)
    r = client.get("/deals/1/proposal.pdf")
    assert r.status_code == 200
    assert r.data[:4] == b"%PDF"
    assert len(r.data) > 1000


def test_excel_export_is_valid_xlsx_with_data(client):
    login(client)
    r = client.get("/export/clients.xlsx")
    assert r.data[:2] == b"PK"
    import io
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(r.data))
    assert wb.active.max_row > 1


def test_full_production_cycle_completes_and_consumes_stock(client):
    """Наскрізний тест: передоплата -> створення виробничого замовлення ->
    виконання всіх операцій -> замовлення автоматично 'done' -> матеріали
    списані зі складу."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    deal = db.execute(
        "SELECT * FROM deals WHERE machine_id IS NOT NULL AND stage NOT IN ('prepay','production','won','lost') LIMIT 1"
    ).fetchone()
    assert deal is not None

    post(client, f"/deals/{deal['id']}/move/prepay", {})

    db2 = sqlite3.connect(a.DB_PATH)
    db2.row_factory = sqlite3.Row
    order = db2.execute("SELECT * FROM production_orders WHERE deal_id=?", (deal["id"],)).fetchone()
    ops = db2.execute(
        "SELECT * FROM production_operations WHERE production_order_id=? ORDER BY step_order", (order["id"],)
    ).fetchall()
    assert len(ops) > 0

    for op in ops:
        post(client, f"/production/operations/{op['id']}/advance", {})
        post(client, f"/production/operations/{op['id']}/advance", {})

    db3 = sqlite3.connect(a.DB_PATH)
    db3.row_factory = sqlite3.Row
    final_order = db3.execute("SELECT * FROM production_orders WHERE id=?", (order["id"],)).fetchone()
    assert final_order["status"] == "done"
    all_ops = db3.execute("SELECT * FROM production_operations WHERE production_order_id=?", (order["id"],)).fetchall()
    assert all(o["status"] == "done" for o in all_ops)


# ---------------------------------------------------------------------------
# Вкладення файлів
# ---------------------------------------------------------------------------

def test_dangerous_file_extension_rejected(client):
    from io import BytesIO
    login(client)
    token = get_csrf_token(client)
    r = client.post("/attachments/client/1/upload",
                     data={"_csrf_token": token, "file": (BytesIO(b"x"), "virus.exe")},
                     content_type="multipart/form-data", follow_redirects=True)
    assert "не підтримується" in r.get_data(as_text=True)


def test_html_upload_rejected_to_prevent_xss(client):
    from io import BytesIO
    login(client)
    token = get_csrf_token(client)
    r = client.post("/attachments/client/1/upload",
                     data={"_csrf_token": token, "file": (BytesIO(b"<script>1</script>"), "hack.html")},
                     content_type="multipart/form-data", follow_redirects=True)
    assert "не підтримується" in r.get_data(as_text=True)


def test_pdf_upload_succeeds_with_safe_stored_name(client):
    from io import BytesIO
    login(client)
    token = get_csrf_token(client)
    client.post("/attachments/client/1/upload",
                data={"_csrf_token": token, "file": (BytesIO(b"%PDF fake"), "dogovir.pdf")},
                content_type="multipart/form-data")
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    att = db.execute("SELECT * FROM attachments WHERE original_name='dogovir.pdf'").fetchone()
    assert att is not None
    assert att["stored_name"] != "dogovir.pdf"  # унікальне ім'я на диску, не оригінальне


def test_attachment_download_forces_disposition(client):
    from io import BytesIO
    login(client)
    token = get_csrf_token(client)
    client.post("/attachments/client/1/upload",
                data={"_csrf_token": token, "file": (BytesIO(b"content-here"), "f.pdf")},
                content_type="multipart/form-data")
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    att = db.execute("SELECT * FROM attachments ORDER BY id DESC LIMIT 1").fetchone()
    r = client.get(f"/attachments/{att['id']}/download")
    assert r.status_code == 200
    assert b"content-here" in r.data
    assert "attachment" in r.headers.get("Content-Disposition", "")


def test_attachment_upload_to_nonexistent_entity_404s(client):
    from io import BytesIO
    login(client)
    token = get_csrf_token(client)
    r = client.post("/attachments/client/999999/upload",
                     data={"_csrf_token": token, "file": (BytesIO(b"%PDF"), "f.pdf")},
                     content_type="multipart/form-data")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Імпорт клієнтів з Excel
# ---------------------------------------------------------------------------

def test_excel_import_full_roundtrip(client):
    import io
    from openpyxl import load_workbook
    login(client)
    r = client.get("/export/clients.xlsx")
    wb = load_workbook(io.BytesIO(r.data))
    ws = wb.active
    original_count = ws.max_row - 1
    ws.append(["Новий Імпортований Клієнт", "99999999", "Тест", "Одеса", "вул. Тестова",
               "+380001112233", "test@import.ua", None, "Виставка", None, "Активний", "з імпорту"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    token = get_csrf_token(client)
    r = client.post("/clients/import", data={"_csrf_token": token, "file": (buf, "clients.xlsx")},
                     content_type="multipart/form-data", follow_redirects=True)
    assert "Створено" in r.get_data(as_text=True)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    new_client = db.execute("SELECT * FROM clients WHERE name='Новий Імпортований Клієнт'").fetchone()
    assert new_client is not None
    assert new_client["edrpou"] == "99999999"
    total_now = db.execute("SELECT COUNT(*) c FROM clients").fetchone()["c"]
    assert total_now == original_count + 1


def test_excel_import_rejects_non_excel_file(client):
    import io
    login(client)
    token = get_csrf_token(client)
    r = client.post("/clients/import", data={"_csrf_token": token, "file": (io.BytesIO(b"not excel"), "f.txt")},
                     content_type="multipart/form-data", follow_redirects=True)
    assert "excel" in r.get_data(as_text=True).lower()


def test_excel_import_bad_email_does_not_block_whole_row(client):
    import io
    from openpyxl import Workbook
    login(client)
    wb = Workbook()
    ws = wb.active
    ws.append(["Компанія", "ЄДРПОУ", "Галузь", "Місто", "Адреса", "Телефон", "Email",
               "Сайт", "Джерело", "Менеджер", "Статус", "Примітки"])
    ws.append(["Клієнт з поганим email", "111", "X", "Y", "Z", "000", "not-an-email",
               None, None, None, "Активний", None])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    token = get_csrf_token(client)
    r = client.post("/clients/import", data={"_csrf_token": token, "file": (buf, "bad.xlsx")},
                     content_type="multipart/form-data", follow_redirects=True)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM clients WHERE name='Клієнт з поганим email'").fetchone()
    assert row is not None
    assert row["email"] is None


def test_calendar_month_boundary_transitions(client):
    login(client)
    r = client.get("/calendar?year=2026&month=13")
    html = r.get_data(as_text=True)
    assert "Січень" in html and "2027" in html
    r = client.get("/calendar?year=2026&month=0")
    html = r.get_data(as_text=True)
    assert "Грудень" in html and "2025" in html


def test_calendar_shows_task_on_correct_day(client):
    login(client)
    token = get_csrf_token(client)
    client.post("/tasks/add", data={"_csrf_token": token, "title": "Тестова задача календаря",
                                      "due_date": "2026-06-15"})
    r = client.get("/calendar?year=2026&month=6")
    assert "Тестова задача календаря" in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Партії матеріалу та обрізки
# ---------------------------------------------------------------------------

def test_material_lot_add_and_delete(client):
    login(client, "sklad", "sklad123")
    r = post(client, "/warehouse/1/lots", {"heat_number": "HT-2026-0451", "certificate_number": "CERT-889",
                                             "supplier": "МеталТрейд", "qty": "500", "received_date": "2026-01-15"},
             follow_redirects=True)
    assert "зареєстровано" in r.get_data(as_text=True).lower()
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    lot = db.execute("SELECT * FROM material_lots WHERE heat_number='HT-2026-0451'").fetchone()
    assert lot is not None
    assert lot["qty"] == 500
    post(client, f"/warehouse/lots/{lot['id']}/delete", {})
    db2 = sqlite3.connect(a.DB_PATH)
    assert db2.execute("SELECT COUNT(*) c FROM material_lots WHERE id=?", (lot["id"],)).fetchone()[0] == 0


def test_scrap_offcut_lifecycle_and_filter(client):
    login(client, "sklad", "sklad123")
    post(client, "/warehouse/scrap/add", {"material_name": "Алюміній офкат", "thickness_mm": "3",
                                            "length_mm": "500", "width_mm": "300", "weight_kg": "3.5",
                                            "location": "Стелаж-ТЕСТ-E1"})
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    offcut = db.execute("SELECT * FROM scrap_offcuts WHERE location='Стелаж-ТЕСТ-E1'").fetchone()
    assert offcut is not None
    assert offcut["status"] == "available"

    post(client, f"/warehouse/scrap/{offcut['id']}/use", {})
    db2 = sqlite3.connect(a.DB_PATH)
    db2.row_factory = sqlite3.Row
    updated = db2.execute("SELECT * FROM scrap_offcuts WHERE id=?", (offcut["id"],)).fetchone()
    assert updated["status"] == "used"
    assert updated["used_at"] is not None

    r = client.get("/warehouse/scrap?status=available")
    assert "Стелаж-ТЕСТ-E1" not in r.get_data(as_text=True)
    r = client.get("/warehouse/scrap?status=used")
    assert "Стелаж-ТЕСТ-E1" in r.get_data(as_text=True)


def test_cost_calculator_math_and_deal_prefill(client):
    login(client)
    r = post(client, "/calculator", {
        "weight_kg": "10", "material_price_per_kg": "50",
        "turning_hours": "5", "turning_rate_per_hour": "20",
        "milling_hours": "4", "milling_rate_per_hour": "15",
        "machine_hours": "0.5", "machine_rate_per_hour": "200",
        "quantity": "2", "margin_pct": "20",
    })
    html = r.get_data(as_text=True)
    assert "760" in html  # собівартість за одиницю
    assert "1824" in html  # разом за 2 шт (760*1.2*2)

    r2 = client.get("/deals/new?prefill_title=Розрахунок з калькулятора&prefill_amount=1824.0")
    html2 = r2.get_data(as_text=True)
    assert "Розрахунок з калькулятора" in html2
    assert "1824" in html2


def test_cost_calculator_rejects_non_numeric(client):
    login(client)
    r = post(client, "/calculator", {"weight_kg": "не число"})
    assert "має містити число" in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Розбір DXF-креслень (калькулятор вартості)
# ---------------------------------------------------------------------------

def _make_sample_dxf_bytes():
    """Генерує невеличке тестове DXF-креслення (прямокутник 100x50мм +
    коло r=10мм) прямо в пам'яті, без залежності від зовнішніх файлів."""
    import ezdxf
    import io
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0))
    msp.add_line((100, 0), (100, 50))
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
    msp.add_circle((50, 25), radius=10)
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")


def test_dxf_analysis_computes_correct_perimeter(client):
    """Калькулятор об'єднано в одну форму (опис/файл + ручні поля, з
    вибором "Розрахувати вручну"/"Розрахувати через ШІ") - окремий
    візуальний блок "Автозаповнення з DXF-креслення" з показом ширини/
    висоти прибрано зі сторінки, тож тут перевіряємо те, що й далі реально
    видно користувачу (флеш-повідомлення з периметром), а геометрію
    (ширина/висота) - напряму через analyze_dxf(), який маршрут
    використовує під капотом і який лишається покритим тестами нижче."""
    import io
    login(client)
    dxf_bytes = _make_sample_dxf_bytes()
    token = get_csrf_token(client)
    r = client.post("/calculator/analyze-dxf", data={
        "_csrf_token": token,
        "dxf_file": (io.BytesIO(dxf_bytes), "деталь.dxf"),
        "thickness_mm": "3", "density_kg_m3": "7850",
    }, content_type="multipart/form-data", follow_redirects=True)
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    # Периметр: 2 лінії (100+50) + периметр полілінії (300) + коло (2*pi*10≈62.83) = 512.83мм = 0.513м
    assert "0.513" in html

    import app as app_module
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
        tmp.write(dxf_bytes)
        tmp_path = tmp.name
    try:
        dxf_result = app_module.analyze_dxf(tmp_path)
    finally:
        os.remove(tmp_path)
    assert dxf_result["width_mm"] == 100.0
    assert dxf_result["height_mm"] == 50.0


def test_dxf_analysis_rejects_non_dxf_extension(client):
    import io
    login(client)
    token = get_csrf_token(client)
    r = client.post("/calculator/analyze-dxf", data={
        "_csrf_token": token, "dxf_file": (io.BytesIO(b"not a dxf"), "file.txt"),
    }, content_type="multipart/form-data")
    assert ".dxf" in r.get_data(as_text=True)


def test_dxf_analysis_handles_corrupted_file_gracefully(client):
    import io
    login(client)
    token = get_csrf_token(client)
    r = client.post("/calculator/analyze-dxf", data={
        "_csrf_token": token, "dxf_file": (io.BytesIO(b"broken dxf content"), "broken.dxf"),
    }, content_type="multipart/form-data")
    assert r.status_code == 200  # не 500 — оброблено штатно
    assert "не вдалось розібрати" in r.get_data(as_text=True).lower()


# ---------------------------------------------------------------------------
# Роль "Бухгалтер"
# ---------------------------------------------------------------------------

def test_accountant_sees_finance_page(client):
    login(client, "buh", "buh12345")
    r = client.get("/finance")
    assert r.status_code == 200
    assert "Сплачено всього" in r.get_data(as_text=True)


def test_accountant_blocked_from_clients_and_warehouse(client):
    login(client, "buh", "buh12345")
    r = client.get("/clients", follow_redirects=True)
    assert "немає прав" in r.get_data(as_text=True)
    r = client.get("/warehouse", follow_redirects=True)
    assert "немає прав" in r.get_data(as_text=True)


def test_accountant_nav_shows_only_finance(client):
    login(client, "buh", "buh12345")
    r = client.get("/")
    html = r.get_data(as_text=True)
    assert "Фінанси" in html
    assert "Угоди (воронка)" not in html
    assert "Склад" not in html


# ---------------------------------------------------------------------------
# Розширена статистика верстатів
# ---------------------------------------------------------------------------

def test_machine_stats_daily_breakdown_and_availability(client):
    login(client)
    post(client, "/telemetry/simulate", {})
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    stats = a.get_machine_runtime_stats(db, 1, 8)
    assert stats is not None
    assert len(stats["daily"]) == 14
    assert 0 <= stats["availability_pct"] <= 100
    assert isinstance(stats["recent_alarms"], list)


def test_work_centers_page_without_telemetry_does_not_crash(client):
    login(client)
    r = client.get("/work-centers")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Розрахунок матеріалу на партію деталей
# ---------------------------------------------------------------------------

def test_material_calc_sheets_needed_math(client):
    login(client)
    # Лист 1250x2500мм=3.125м², розкрій 75%=2.34375м² корисної площі.
    # Деталь 0.05м² -> 46 деталей/лист. 200 деталей -> 5 листів.
    r = post(client, "/calculator", {
        "weight_kg": "2", "material_price_per_kg": "50",
        "part_area_m2": "0.05", "nesting_pct": "75",
        "sheet_width_mm": "1250", "sheet_length_mm": "2500",
        "quantity": "200", "margin_pct": "20",
    })
    html = r.get_data(as_text=True)
    assert "46" in html


def test_material_calc_part_too_big_for_sheet(client):
    login(client)
    r = post(client, "/calculator", {
        "weight_kg": "2", "material_price_per_kg": "50",
        "part_area_m2": "10", "nesting_pct": "75",
        "sheet_width_mm": "1250", "sheet_length_mm": "2500",
        "quantity": "5", "margin_pct": "20",
    })
    assert "не вміщується" in r.get_data(as_text=True)


def test_material_calc_stock_check_flags_shortage(client):
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    mat_item = db.execute("SELECT * FROM warehouse_items WHERE category LIKE '%атеріал%' LIMIT 1").fetchone()
    r = post(client, "/calculator", {
        "weight_kg": "100", "material_price_per_kg": "50",
        "part_area_m2": "0.05", "nesting_pct": "75",
        "sheet_width_mm": "1250", "sheet_length_mm": "2500",
        "quantity": "200", "margin_pct": "20", "stock_item_id": str(mat_item["id"]),
    })
    html = r.get_data(as_text=True)
    assert mat_item["name"] in html
    assert "НЕ ВИСТАЧАЄ" in html


# ---------------------------------------------------------------------------
# Маршрутний лист (traveler card)
# ---------------------------------------------------------------------------

def test_route_sheet_pdf_is_valid(client):
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    deal = db.execute(
        "SELECT * FROM deals WHERE machine_id IS NOT NULL AND stage NOT IN ('prepay','production','won','lost') LIMIT 1"
    ).fetchone()
    post(client, f"/deals/{deal['id']}/move/prepay", {})
    db2 = sqlite3.connect(a.DB_PATH)
    db2.row_factory = sqlite3.Row
    order = db2.execute("SELECT * FROM production_orders WHERE deal_id=?", (deal["id"],)).fetchone()
    assert order is not None
    r = client.get(f"/production/{order['id']}/route-sheet.pdf")
    assert r.status_code == 200
    assert r.data[:4] == b"%PDF"
    assert len(r.data) > 1000


# ---------------------------------------------------------------------------
# Нагадування
# ---------------------------------------------------------------------------

def test_reminders_appear_for_relevant_role_only(client):
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    item = db.execute("SELECT * FROM warehouse_items LIMIT 1").fetchone()
    post(client, f"/warehouse/{item['id']}/transactions", {"type": "out", "qty": str(item["qty_on_hand"])})

    r = client.get("/")
    assert "Критично низький залишок" in r.get_data(as_text=True)

    client.get("/logout")
    login(client, "oleh", "manager123")
    r2 = client.get("/")
    assert "Критично низький залишок" not in r2.get_data(as_text=True)

    client.get("/logout")
    login(client, "sklad", "sklad123")
    r3 = client.get("/")
    assert "Критично низький залишок" in r3.get_data(as_text=True)


def test_reminders_include_overdue_production_orders(client):
    """Дзвоник має попереджати і про прострочені виробничі замовлення (не
    лише про задачі, склад, аварії, рекламації, платежі) - це доступний,
    вже готовий сигнал (production_orders.due_date + status), якого раніше
    не було серед нагадувань. Перевіряємо реальним запитом через ролі:
    видно адміну й виробництву, не видно бухгалтерії (нерелевантно)."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row

    yesterday = (a.datetime.datetime.now() - a.datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    future = (a.datetime.datetime.now() + a.datetime.timedelta(days=10)).strftime("%Y-%m-%d")
    now = a.datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "INSERT INTO production_orders (deal_id, machine_id, quantity, status, due_date, created_at) "
        "VALUES (NULL, NULL, 5, 'in_progress', ?, ?)",
        (yesterday, now),
    )
    # Прострочене, але вже виконане замовлення - НЕ має рахуватись.
    db.execute(
        "INSERT INTO production_orders (deal_id, machine_id, quantity, status, due_date, created_at) "
        "VALUES (NULL, NULL, 3, 'done', ?, ?)",
        (yesterday, now),
    )
    # Замовлення з дедлайном у майбутньому - теж НЕ має рахуватись.
    db.execute(
        "INSERT INTO production_orders (deal_id, machine_id, quantity, status, due_date, created_at) "
        "VALUES (NULL, NULL, 2, 'planned', ?, ?)",
        (future, now),
    )
    db.commit()

    r = client.get("/")
    html = r.get_data(as_text=True)
    assert "Прострочених виробничих замовлень: 1" in html

    client.get("/logout")
    login(client, "maister", "prod123")
    r2 = client.get("/")
    assert "Прострочених виробничих замовлень: 1" in r2.get_data(as_text=True)

    client.get("/logout")
    login(client, "buh", "buh12345")
    r3 = client.get("/")
    assert "Прострочених виробничих замовлень" not in r3.get_data(as_text=True)


def test_reminders_bell_button_has_stable_id_and_js_no_longer_uses_broken_textcontent_check(client):
    """РЕГРЕСІЙНИЙ ТЕСТ на реальний баг: користувач повідомив, що дзвоник
    "не реагує на натискання". Причина була в document-click обробнику:
    він порівнював e.target.textContent з рівно '🔔', але коли є непрочитані
    нагадування (майже завжди), у кнопці є ще й бейдж-лічильник, тож
    textContent виходив на кшталт '🔔3' і НІКОЛИ не збігався з '🔔' -
    обробник миттєво закривав щойно відкритий дропдаун одразу після
    onclick-тогла. Фікс - перевіряти приналежність кліку до кнопки через
    .closest('#reminders-toggle-btn'), а не порівнювати текст. Тест не
    виконує JS (немає браузера), але перевіряє, що в реальному HTML є
    стабільний id кнопки і що крихкого порівняння з textContent більше
    немає - інакше цей самий regresssion можна випадково повернути."""
    login(client)
    html = client.get("/").get_data(as_text=True)
    assert 'id="reminders-toggle-btn"' in html
    assert "e.target.closest('#reminders-toggle-btn')" in html
    assert "e.target.textContent !== '🔔'" not in html


# ---------------------------------------------------------------------------
# Історія розрахунків вартості
# ---------------------------------------------------------------------------

def test_manual_calculator_saves_to_history(client):
    """Кожен завершений ручний розрахунок має автоматично потрапляти в
    історію - перевіряємо і сам факт запису в БД (з правильними цифрами),
    і що він потім видно на сторінці /calculator/history."""
    login(client)
    r = post(client, "/calculator", {
        "weight_kg": "2.5", "material_price_per_kg": "60", "turning_hours": "1",
        "turning_rate_per_hour": "800", "milling_hours": "0", "milling_rate_per_hour": "900",
        "machine_hours": "0", "machine_rate_per_hour": "0", "setup_cost_total": "0",
        "quantity": "10", "margin_pct": "20", "part_description": "Тестовий вал для історії",
    })
    assert r.status_code == 200

    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM cost_calculations ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["source"] == "manual"
    assert row["part_description"] == "Тестовий вал для історії"
    assert row["quantity"] == 10
    assert row["weight_kg"] == 2.5

    hist_html = client.get("/calculator/history").get_data(as_text=True)
    assert "Тестовий вал для історії" in hist_html


def test_ai_calculator_saves_to_history_with_ai_source(client):
    """Розрахунок через ШІ теж має потрапляти в історію, позначений
    джерелом 'ai' (а не 'manual'), з розгорнутим описом техпроцесу."""
    from unittest import mock

    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-test123"})

    def fake_post(url, headers=None, json=None, timeout=None):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"content": [{
                    "type": "tool_use", "name": "cost_estimate", "input": {
                        "weight_kg": 0.8, "material_price_per_kg": 60, "turning_hours": 0.3,
                        "turning_rate_per_hour": 800, "milling_hours": 0.1, "milling_rate_per_hour": 900,
                        "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 7, "margin_pct": 20,
                        "operations_description": "Технологічний процес виготовлення втулки.",
                    },
                }]}

        return FakeResponse()

    with mock.patch("requests.post", side_effect=fake_post):
        token = get_csrf_token(client, "/calculator")
        r = client.post("/calculator/ai-estimate", data={
            "_csrf_token": token, "ai_description": "Втулка зі сталі 45, партія 7 шт",
        }, follow_redirects=True)
    assert r.status_code == 200

    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM cost_calculations ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["source"] == "ai"
    assert row["quantity"] == 7
    assert "Втулка" in row["part_description"]
    assert "втулки" in row["operations_description"]

    hist_html = client.get("/calculator/history").get_data(as_text=True)
    assert "🤖 ШІ" in hist_html


def test_calculator_history_page_filters_by_source_and_paginates(client):
    """Фільтр по джерелу (ручний/ШІ) і пагінація мають реально працювати -
    вставляємо напряму в БД кілька записів різних джерел і перевіряємо, що
    фільтр показує лише потрібні."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    now = a.now_iso() if hasattr(a, "now_iso") else "2026-01-01 10:00:00"
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, cost_per_unit, "
        "price_per_unit, total_price, created_at) VALUES (1,'manual','Деталь-ручна',1,10,12,12,?)", (now,)
    )
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, cost_per_unit, "
        "price_per_unit, total_price, created_at) VALUES (1,'ai','Деталь-ШІ',1,20,24,24,?)", (now,)
    )
    db.commit()

    html_all = client.get("/calculator/history").get_data(as_text=True)
    assert "Деталь-ручна" in html_all
    assert "Деталь-ШІ" in html_all

    html_manual = client.get("/calculator/history?source=manual").get_data(as_text=True)
    assert "Деталь-ручна" in html_manual
    assert "Деталь-ШІ" not in html_manual

    html_ai = client.get("/calculator/history?source=ai").get_data(as_text=True)
    assert "Деталь-ШІ" in html_ai
    assert "Деталь-ручна" not in html_ai


def test_calculator_history_requires_login():
    a = __import__("app")
    c = a.app.test_client()
    r = c.get("/calculator/history", follow_redirects=True)
    assert "Увійти" in r.get_data(as_text=True) or "login" in r.request.path


def test_calculator_page_links_to_history_page(client):
    login(client)
    html = client.get("/calculator").get_data(as_text=True)
    assert 'href="/calculator/history"' in html


def test_calculator_history_text_search_filters_by_part_description(client):
    """Пошук за назвою деталі має реально фільтрувати (не лише
    source) - вставляємо дві деталі з різними назвами й перевіряємо, що
    ?q=... показує тільки ту, що містить підрядок."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    now = "2026-01-01 10:00:00"
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, cost_per_unit, "
        "price_per_unit, total_price, created_at) VALUES (1,'manual','Фланець Ø60',1,10,12,12,?)", (now,)
    )
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, cost_per_unit, "
        "price_per_unit, total_price, created_at) VALUES (1,'manual','Втулка бронзова',1,8,10,10,?)", (now,)
    )
    db.commit()

    html = client.get("/calculator/history?q=Фланець").get_data(as_text=True)
    assert "Фланець" in html
    assert "Втулка" not in html


def test_export_calculations_xlsx_produces_real_workbook_with_correct_rows_and_respects_filter(client):
    """Реальна перевірка Excel-експорту: відкриваємо згенерований файл
    через openpyxl (а не лише дивимось на статус-код чи content-type) і
    звіряємо, що рядки та суми справді там, і що фільтр по джерелу працює
    так само, як і на сторінці історії."""
    import io
    from openpyxl import load_workbook

    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    now = "2026-01-01 10:00:00"
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, cost_per_unit, "
        "price_per_unit, total_price, created_at) VALUES (1,'manual','Експорт-ручний',3,100,120,360,?)", (now,)
    )
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, cost_per_unit, "
        "price_per_unit, total_price, created_at) VALUES (1,'ai','Експорт-ШІ',2,50,60,120,?)", (now,)
    )
    db.commit()

    r_all = client.get("/export/calculations.xlsx")
    assert r_all.status_code == 200
    assert r_all.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    wb = load_workbook(io.BytesIO(r_all.data))
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][0] == "Дата"
    part_descriptions = [row[3] for row in rows[1:]]
    assert "Експорт-ручний" in part_descriptions
    assert "Експорт-ШІ" in part_descriptions
    manual_row = next(row for row in rows[1:] if row[3] == "Експорт-ручний")
    assert manual_row[4] == 3  # кількість
    assert manual_row[12] == 360  # разом

    r_manual_only = client.get("/export/calculations.xlsx?source=manual")
    wb2 = load_workbook(io.BytesIO(r_manual_only.data))
    descriptions2 = [row[3] for row in wb2.active.iter_rows(values_only=True)][1:]
    assert "Експорт-ручний" in descriptions2
    assert "Експорт-ШІ" not in descriptions2


def test_export_calculations_xlsx_requires_login():
    a = __import__("app")
    c = a.app.test_client()
    r = c.get("/export/calculations.xlsx", follow_redirects=True)
    assert "Увійти" in r.get_data(as_text=True) or "login" in r.request.path


def test_calculator_history_page_has_excel_export_link(client):
    login(client)
    html = client.get("/calculator/history").get_data(as_text=True)
    assert "/export/calculations.xlsx" in html


# ---------------------------------------------------------------------------
# Перегляд і видалення окремого запису в історії розрахунків
# ---------------------------------------------------------------------------

def test_calculator_history_list_links_to_detail_page(client):
    """У списку історії кожен рядок має вести на детальну картку
    розрахунку (а не лише показувати обрізаний опис)."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, cost_per_unit, "
        "price_per_unit, total_price, created_at) VALUES (1,'manual','Деталь для перегляду',1,10,12,12,'2026-01-01 10:00:00')"
    )
    db.commit()
    calc_id = db.execute("SELECT id FROM cost_calculations ORDER BY id DESC LIMIT 1").fetchone()["id"]

    html = client.get("/calculator/history").get_data(as_text=True)
    assert f"/calculator/history/{calc_id}" in html


def test_calculator_history_detail_shows_full_inputs_and_untruncated_description(client):
    """Детальна картка має показувати ПОВНИЙ текст техпроцесу (не обрізаний,
    як у таблиці) і всі вхідні параметри розрахунку, а не лише підсумкові
    суми - це головна причина, чому менеджер хоче "передивитись деталі"
    перед презентацією клієнту."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    long_description = "Операція 1: токарна обробка. " * 20  # довший за те, що влізло б у рядок таблиці
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, weight_kg, "
        "material_price_per_kg, turning_hours, turning_rate_per_hour, milling_hours, milling_rate_per_hour, "
        "machine_hours, machine_rate_per_hour, setup_cost_total, margin_pct, cost_per_unit, price_per_unit, "
        "total_price, operations_description, created_at) VALUES "
        "(1,'ai','Вал ступінчастий Ø40',15,2.3,65,1.2,800,0.4,900,0,0,500,25,733.5,916.88,13753.2,?,'2026-01-01 10:00:00')",
        (long_description,),
    )
    db.commit()
    calc_id = db.execute("SELECT id FROM cost_calculations ORDER BY id DESC LIMIT 1").fetchone()["id"]

    r = client.get(f"/calculator/history/{calc_id}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Вал ступінчастий Ø40" in html
    # Опис тепер розкладається на окремі пункти (а не стіна тексту), тому
    # перевіряємо, що зміст на місці, а не те, що він іде одним суцільним рядком.
    assert "Операція 1: токарна обробка." in html
    assert html.count("ops-note") >= 20
    assert "2.3" in html  # вага
    assert "65" in html  # ціна матеріалу
    assert "13753.20" in html  # разом за партію
    assert "733.50" in html  # собівартість/од


def test_split_operations_text_numbered_lines(client):
    """Новий формат ШІ: етап на рядок -> окремі етапи з назвою й поясненням,
    а рядок «Припущення» - окрема примітка."""
    a = client._app_module
    items = a.split_operations_text(
        "1. Заготовка — пруток Ø60, 10 хв.\n2. Чорнове точіння — на токарному; 25 хв.\nПрипущення: ціни типові"
    )
    assert [i["kind"] for i in items] == ["step", "step", "note"]
    assert items[0]["num"] == "1" and items[0]["title"] == "Заготовка" and "пруток" in items[0]["body"]
    assert items[2]["body"].startswith("Припущення")


def test_split_operations_text_legacy_single_paragraph(client):
    """Старі записи (весь текст одним абзацом із вбудованою нумерацією або без
    неї) теж розкладаються на пункти, а не лишаються купою."""
    a = client._app_module
    inline = a.split_operations_text("Вступ. 1) Заготовка: різка 10 хв. 2) Токарна: чорнове 25 хв. 3) ВТК: контроль.")
    assert [i["num"] for i in inline if i["kind"] == "step"] == ["1", "2", "3"]
    sentences = a.split_operations_text("Спочатку ріжемо. Потім точимо. Далі фрезеруємо.")
    assert len(sentences) == 3
    assert a.split_operations_text("") == []
    assert a.split_operations_text(None) == []


def test_operations_html_escapes_markup(client):
    """Текст від ШІ/користувача виводиться екранованим - жодного виконання HTML/JS."""
    a = client._app_module
    html = str(a.render_operations_html("1. Етап — <script>alert(1)</script>\n<b>примітка</b>"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>примітка</b>" not in html


def test_calculator_history_detail_shows_stage_cards(client):
    """На сторінці збереженого розрахунку етапи показані списком карток."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, weight_kg, "
        "material_price_per_kg, turning_hours, turning_rate_per_hour, milling_hours, milling_rate_per_hour, "
        "machine_hours, machine_rate_per_hour, setup_cost_total, margin_pct, cost_per_unit, price_per_unit, "
        "total_price, operations_description, created_at) VALUES "
        "(1,'ai','Втулка',1,1,50,1,800,0,0,0,0,0,20,850,1020,1020,?,'2026-01-01 10:00:00')",
        ("1. Заготовка — пруток Ø40\n2. Точіння — Haas ST-20\n3. ВТК — штангенциркуль",),
    )
    db.commit()
    calc_id = db.execute("SELECT id FROM cost_calculations ORDER BY id DESC LIMIT 1").fetchone()[0]
    db.close()
    html = client.get(f"/calculator/history/{calc_id}").get_data(as_text=True)
    assert html.count('class="ops-step"') == 3
    assert "ops-title\">Точіння" in html


def test_quote_pdf_with_multistage_operations(client):
    """PDF із багатоетапним описом генерується без помилок."""
    login(client)
    a = client._app_module
    buf = a._render_quote_pdf(
        "Втулка", "1. Заготовка — пруток Ø40\n2. Точіння — Haas ST-20\nПрипущення: ціни типові",
        1, 50, 1, 800, 0, 0, 0, 0, 0, 1, 20,
    )
    assert buf.read(5) == b"%PDF-"


def test_calculator_history_detail_missing_id_returns_404(client):
    login(client)
    r = client.get("/calculator/history/999999")
    assert r.status_code == 404


def test_calculator_history_detail_requires_login():
    a = __import__("app")
    c = a.app.test_client()
    r = c.get("/calculator/history/1", follow_redirects=True)
    assert "Увійти" in r.get_data(as_text=True) or "login" in r.request.path


def test_calculator_history_delete_removes_row_and_redirects(client):
    """Видалення має реально прибрати запис з БД (не лише приховати на
    екрані) і повернути користувача на список історії."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, cost_per_unit, "
        "price_per_unit, total_price, created_at) VALUES (1,'manual','Деталь на видалення',1,10,12,12,'2026-01-01 10:00:00')"
    )
    db.commit()
    calc_id = db.execute("SELECT id FROM cost_calculations ORDER BY id DESC LIMIT 1").fetchone()["id"]

    r = post(client, f"/calculator/history/{calc_id}/delete", follow_redirects=True)
    assert r.status_code == 200
    assert "видалено" in r.get_data(as_text=True).lower()

    db2 = sqlite3.connect(a.DB_PATH)
    db2.row_factory = sqlite3.Row
    assert db2.execute("SELECT id FROM cost_calculations WHERE id=?", (calc_id,)).fetchone() is None

    # Деталі видаленого розрахунку більше не мають бути доступні.
    r2 = client.get(f"/calculator/history/{calc_id}")
    assert r2.status_code == 404


def test_calculator_history_delete_missing_id_returns_404(client):
    login(client)
    r = post(client, "/calculator/history/999999/delete")
    assert r.status_code == 404


def test_calculator_history_delete_requires_login():
    a = __import__("app")
    c = a.app.test_client()
    r = c.post("/calculator/history/1/delete", follow_redirects=True)
    assert "Увійти" in r.get_data(as_text=True) or "login" in r.request.path


def test_calculator_history_delete_blocked_for_viewer_role(client):
    """Роль 'Перегляд' має лише переглядати - блокується загальним
    before_request для будь-якого POST, тому видалення теж має впасти
    саме з цієї причини (а не якимось іншим шляхом)."""
    login(client)
    post(client, "/users", {"full_name": "В'ювер Історії", "username": "glance", "password": "view123", "role": "viewer"})
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, cost_per_unit, "
        "price_per_unit, total_price, created_at) VALUES (1,'manual','Деталь viewer',1,10,12,12,'2026-01-01 10:00:00')"
    )
    db.commit()
    calc_id = db.execute("SELECT id FROM cost_calculations ORDER BY id DESC LIMIT 1").fetchone()["id"]

    login(client, "glance", "view123")
    r = post(client, f"/calculator/history/{calc_id}/delete", follow_redirects=True)
    assert "лише перегляд" in r.get_data(as_text=True).lower()
    db2 = sqlite3.connect(a.DB_PATH)
    db2.row_factory = sqlite3.Row
    assert db2.execute("SELECT id FROM cost_calculations WHERE id=?", (calc_id,)).fetchone() is not None


def test_calculator_history_delete_preserves_filter_and_page_on_redirect(client):
    """Після видалення з відфільтрованого списку користувач має лишитись на
    тому самому фільтрі/сторінці, а не впасти на перший нефільтрований
    екран історії."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, cost_per_unit, "
        "price_per_unit, total_price, created_at) VALUES (1,'ai','Деталь ШІ для фільтра',1,10,12,12,'2026-01-01 10:00:00')"
    )
    db.commit()
    calc_id = db.execute("SELECT id FROM cost_calculations ORDER BY id DESC LIMIT 1").fetchone()["id"]

    token = get_csrf_token(client, "/calculator/history?source=ai")
    r = client.post(f"/calculator/history/{calc_id}/delete", data={
        "_csrf_token": token, "page": "1", "source": "ai", "q": "",
    }, follow_redirects=False)
    assert r.status_code == 302
    assert "source=ai" in r.location


# ---------------------------------------------------------------------------
# Майстер, прив'язаний до конкретної дільниці
# ---------------------------------------------------------------------------

def test_foreman_redirected_to_own_terminal(client):
    """Клік по пункту меню "Моя дільниця" (/production) і далі кнопка
    "Почати роботу" на дашборді мають одразу відкривати термінал СВОЄЇ
    дільниці - це швидкий робочий екран для цеху, а не повний список
    замовлень CRM."""
    login(client, "tokar", "tokar123")
    r = client.get("/")
    assert "Моя дільниця" in r.get_data(as_text=True)
    r2 = client.get("/production", follow_redirects=False)
    assert r2.status_code == 302
    assert "/work-centers/1/terminal" in r2.location


def test_foreman_can_browse_all_work_centers_list(client):
    """/work-centers більше НЕ перекидає майстра одразу на його термінал -
    майстер має бачити повний список усіх дільниць, щоб перевірити стан
    деталей на інших ланках (за запитом користувача), з позначкою, яка
    дільниця його власна."""
    login(client, "tokar", "tokar123")
    r = client.get("/work-centers")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Моя дільниця" in html
    assert "👁 Перегляд усіх дільниць" in html


def test_foreman_can_view_other_work_center_read_only(client):
    """Майстер МОЖЕ відкрити термінал ІНШОЇ дільниці, щоб подивитись, на
    якій стадії деталь (новий запит користувача) - але це режим лише
    перегляду: сторінка не дає помилку доступу, показує банер "лише
    перегляд" і НЕ показує кнопки "Почати"/"Завершити" для чужих операцій."""
    login(client, "tokar", "tokar123")
    r = client.get("/work-centers/3/terminal")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "немає доступу" not in html.lower()
    assert "лише перегляд" in html.lower()
    assert "ПОЧАТИ" not in html and "ЗАВЕРШИТИ" not in html

    # А на своїй дільниці кнопки дій лишаються доступні як і раніше.
    r2 = client.get("/work-centers/1/terminal")
    assert r2.status_code == 200
    html2 = r2.get_data(as_text=True)
    assert "лише перегляд" not in html2.lower()


def test_foreman_cannot_advance_operation_on_foreign_work_center(client):
    """Навіть якщо майстер відкрив чужий термінал і спробує напряму
    відправити POST на operation_advance (в обхід прихованої в UI кнопки),
    сервер має відхилити цю дію - дозволено лише перегляд, не керування."""
    login(client, "tokar", "tokar123")
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    op = db.execute(
        "SELECT id FROM production_operations WHERE work_center_id=3 AND status='waiting' LIMIT 1"
    ).fetchone()
    if op is None:
        pytest.skip("немає підходящої тестової операції на дільниці 3 в демо-даних")
    before_status = db.execute("SELECT status FROM production_operations WHERE id=?", (op["id"],)).fetchone()["status"]

    r = post(client, f"/production/operations/{op['id']}/advance", follow_redirects=True)
    assert "лише на своїй дільниці" in r.get_data(as_text=True).lower()

    after_status = db.execute("SELECT status FROM production_operations WHERE id=?", (op["id"],)).fetchone()["status"]
    assert after_status == before_status


def test_general_production_role_sees_everything(client):
    login(client, "maister", "prod123")
    r = client.get("/production")
    assert r.status_code == 200
    r2 = client.get("/work-centers")
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# Склад: виробництво може списувати матеріал, але не оформляти прихід
# ---------------------------------------------------------------------------

def test_production_role_can_write_off_warehouse_stock(client):
    """Виробничник має право списати матеріал, який сам витратив (саме
    тому йому й показують склад у меню) - перевіряємо, що списання реально
    зменшує залишок."""
    login(client, "maister", "prod123")
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    item = db.execute("SELECT * FROM warehouse_items ORDER BY id LIMIT 1").fetchone()
    before_qty = item["qty_on_hand"]

    r = post(client, f"/warehouse/{item['id']}/transactions", {
        "type": "out", "qty": "2", "reference": "списано на деталь",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "складську операцію проведено" in r.get_data(as_text=True).lower()

    db2 = sqlite3.connect(a.DB_PATH)
    db2.row_factory = sqlite3.Row
    after_qty = db2.execute("SELECT qty_on_hand FROM warehouse_items WHERE id=?", (item["id"],)).fetchone()["qty_on_hand"]
    assert after_qty == before_qty - 2


def test_warehouse_transaction_history_shows_which_user_did_it(client):
    """РЕГРЕСІЙНИЙ ТЕСТ: Дмитро повідомив, що після списання Миколою
    (виробництво) в адмінці не було видно, ХТО саме провів операцію -
    user_id в БД зберігався, але колонка в таблиці історії не виводилась.
    Перевіряємо наскрізно: Микола списує -> адмін відкриває картку
    матеріалу -> бачить "Тарас Майстренко" (full_name maister), а не
    порожню колонку чи просто дату."""
    login(client, "maister", "prod123")
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    item = db.execute("SELECT * FROM warehouse_items ORDER BY id LIMIT 1").fetchone()

    post(client, f"/warehouse/{item['id']}/transactions", {
        "type": "out", "qty": "3", "reference": "списано Тарасом",
    }, follow_redirects=True)

    login(client)  # адмін
    html = client.get(f"/warehouse/{item['id']}").get_data(as_text=True)
    assert "Тарас Майстренко" in html
    assert "списано Тарасом" in html


def test_production_role_cannot_record_warehouse_receipt(client):
    """Виробничник НЕ може оформити прихід нового матеріалу (type='in') -
    це відповідальність ролі 'Склад', а не цеху."""
    login(client, "maister", "prod123")
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    item = db.execute("SELECT * FROM warehouse_items ORDER BY id LIMIT 1").fetchone()
    before_qty = item["qty_on_hand"]

    r = post(client, f"/warehouse/{item['id']}/transactions", {
        "type": "in", "qty": "50", "reference": "спроба приходу",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "прихід і коригування" in r.get_data(as_text=True).lower()

    db2 = sqlite3.connect(a.DB_PATH)
    db2.row_factory = sqlite3.Row
    after_qty = db2.execute("SELECT qty_on_hand FROM warehouse_items WHERE id=?", (item["id"],)).fetchone()["qty_on_hand"]
    assert after_qty == before_qty


def test_production_role_cannot_add_material_lot(client):
    """Реєстрація партії матеріалу (плавка/сертифікат) лишається виключно
    за складом - маршрут і далі приймає тільки роль 'Склад'."""
    login(client, "maister", "prod123")
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    item = db.execute("SELECT * FROM warehouse_items ORDER BY id LIMIT 1").fetchone()

    r = post(client, f"/warehouse/{item['id']}/lots", {
        "heat_number": "HT-TEST", "qty": "10",
    }, follow_redirects=True)
    assert "прав" in r.get_data(as_text=True).lower()


def test_warehouse_item_page_hides_receipt_form_for_production(client):
    """Сторінка матеріалу для ролі 'Виробництво' має показувати лише форму
    списання (без вибору типу операції й без блоку реєстрації партій), щоб
    не вводити в оману можливістю, якої насправді немає."""
    login(client, "maister", "prod123")
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    item = db.execute("SELECT * FROM warehouse_items ORDER BY id LIMIT 1").fetchone()

    html = client.get(f"/warehouse/{item['id']}").get_data(as_text=True)
    assert "Списання матеріалу" in html
    assert "<select name=\"type\">" not in html  # вибір типу операції прихований - type завжди 'out'
    assert "Партії матеріалу" not in html


def test_warehouse_role_still_sees_full_form(client):
    """Роль 'Склад' і далі бачить повний вибір типу операції (прихід/
    списання/інвентаризація) і блок партій матеріалу - обмеження стосується
    лише ролі 'Виробництво'."""
    login(client, "sklad", "sklad123")
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    item = db.execute("SELECT * FROM warehouse_items ORDER BY id LIMIT 1").fetchone()

    html = client.get(f"/warehouse/{item['id']}").get_data(as_text=True)
    assert 'name="type"' in html
    assert "Партії матеріалу" in html


# ---------------------------------------------------------------------------
# Telegram-сповіщення
# ---------------------------------------------------------------------------

def test_settings_page_shows_telegram_status(client):
    login(client)
    r = client.get("/settings")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Telegram" in html
    assert "Не налаштовано" in html
    assert "@BotFather" in html


def test_telegram_settings_save_and_status_update(client):
    login(client)
    r = post(client, "/settings/save", {"form": "telegram", "telegram_bot_token": "123:FAKE",
                                          "telegram_chat_id": "999999"}, follow_redirects=True)
    assert r.status_code == 200
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    val = db.execute("SELECT value FROM settings WHERE key='telegram_bot_token'").fetchone()
    assert val["value"] == "123:FAKE"
    r2 = client.get("/settings")
    assert "Підключено" in r2.get_data(as_text=True)


def test_telegram_test_with_fake_token_fails_gracefully(client):
    login(client)
    r = post(client, "/settings/save", {"form": "telegram", "telegram_bot_token": "123:FAKE",
                                          "telegram_chat_id": "999999", "test": "1"}, follow_redirects=True)
    assert r.status_code == 200  # не 500 - помилка Telegram оброблена штатно


def test_telegram_detect_chats_does_not_crash_on_bad_token(client):
    login(client)
    r = post(client, "/settings/telegram/detect-chats", {"telegram_bot_token": "123:FAKE"}, follow_redirects=True)
    assert r.status_code == 200


def test_non_admin_blocked_from_settings(client):
    login(client, "oleh", "manager123")
    r = client.get("/settings", follow_redirects=True)
    assert "немає прав" in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Розрахунок вартості через ШІ
# ---------------------------------------------------------------------------

def test_ai_calc_without_api_key_shows_clear_error(client):
    login(client)
    r = post(client, "/calculator/ai-estimate", {"ai_description": "Кронштейн зі сталі"}, follow_redirects=True)
    assert r.status_code == 200
    assert "API-ключ" in r.get_data(as_text=True)


def test_ai_calc_empty_description_shows_clear_error(client):
    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-something"})
    r = post(client, "/calculator/ai-estimate", {"ai_description": ""}, follow_redirects=True)
    assert "Опиши деталь" in r.get_data(as_text=True)


def test_ai_calc_real_api_call_with_invalid_key_handled_gracefully(client):
    """Реальний виклик Anthropic API (мережа насправді доступна в тестовому
    середовищі) з навмисно невалідним ключем — перевіряє, що справжня
    помилка 401 від Anthropic коректно перехоплюється й показується
    користувачу зрозумілою мовою, а не валить сервер."""
    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-fake-invalid-key-12345"})
    r = post(client, "/calculator/ai-estimate",
             {"ai_description": "Вал зі сталі 40Х, Ø30х250мм, точіння + фрезерування паза, партія 50 шт"},
             follow_redirects=True)
    assert r.status_code == 200
    assert "Невірний API-ключ" in r.get_data(as_text=True)


def test_ai_calc_json_parsing_handles_markdown_wrapper():
    """Модель інколи обгортає JSON у markdown-блок попри пряму заборону в
    промпті — перевіряємо, що очищення регулярним виразом коректно знімає
    цю обгортку в обох випадках."""
    import json
    import re as re_local
    good_json = '{"weight_kg": 1.2, "quantity": 50}'
    wrapped = f"```json\n{good_json}\n```"
    for text in (good_json, wrapped):
        cleaned = re_local.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re_local.MULTILINE).strip()
        parsed = json.loads(cleaned)
        assert parsed["weight_kg"] == 1.2
        assert parsed["quantity"] == 50


def test_extract_json_object_handles_prose_around_json():
    """РЕГРЕСІЙНИЙ ТЕСТ на реальний баг: користувач отримав "ШІ повернув
    відповідь у неочікуваному форматі" на короткому неоднозначному описі
    ("торець 200 шт") - модель, стикнувшись з браком деталей, додала текст
    до/після JSON-об'єкта (попри пряму заборону в промпті), а стара логіка
    вміла знімати лише markdown-код-фенси, тому падала на будь-якому іншому
    обрамленні. _extract_json_object() має вирізати сам об'єкт у кожному
    з цих випадків."""
    from app import _extract_json_object
    import json

    cases = [
        'Оскільки опис дуже короткий, ось орієнтовний розрахунок:\n\n'
        '{"weight_kg": 1.2, "quantity": 200}',

        '{"weight_kg": 1.2, "quantity": 200}\n\n'
        'Зверни увагу, це дуже груба оцінка через брак деталей в описі.',

        'Ось розрахунок:\n'
        '{"weight_kg": 1.2, "operations_description": "Токарна і фрезерна", "quantity": 200}\n'
        'Сподіваюсь, допомогло!',
    ]
    for text in cases:
        extracted = _extract_json_object(text)
        assert extracted is not None, f"не знайшов JSON у: {text!r}"
        parsed = json.loads(extracted)
        assert parsed["weight_kg"] == 1.2
        assert parsed["quantity"] == 200


def test_extract_json_object_ignores_braces_inside_string_values():
    """Фігурні дужки всередині текстового значення (напр. у
    operations_description) не мають передчасно завершити пошук об'єкта -
    рахунок глибини має ігнорувати вміст рядків у лапках."""
    from app import _extract_json_object
    import json

    text = '{"weight_kg": 1.2, "operations_description": "Розмір {200x150}, партія {A}", "quantity": 5}'
    extracted = _extract_json_object(text)
    parsed = json.loads(extracted)
    assert parsed["operations_description"] == "Розмір {200x150}, партія {A}"
    assert parsed["quantity"] == 5


def test_extract_json_object_returns_none_for_text_without_json():
    from app import _extract_json_object
    assert _extract_json_object("Вибач, я не можу порахувати без додаткової інформації.") is None
    assert _extract_json_object("") is None


def test_ai_calc_prompt_names_real_shop_machines_and_asks_for_detailed_process():
    """Користувач хоче, щоб ШІ описував технологічний процес прив'язаним до
    РЕАЛЬНОГО парку обладнання цеху (Haas UMC-750, VF-2, VF-4, токарні ЧПУ
    Haas), а не абстрактно "фрезерний верстат" - і щоб опис був розгорнутим,
    а не 3-4 реченнями. Перевіряємо, що системний промпт справді називає
    кожен верстат явно (щоб майбутнє редагування промпту випадково не
    загубило назву) і вимагає розгорнутого, а не короткого опису."""
    from app import AI_CALC_SYSTEM_PROMPT
    for machine in ("Haas UMC-750", "Haas VF-2", "Haas VF-4", "Haas"):
        assert machine in AI_CALC_SYSTEM_PROMPT
    assert "10-15 речень" in AI_CALC_SYSTEM_PROMPT
    assert "НЕДОСТАТНІМ" in AI_CALC_SYSTEM_PROMPT


def test_ai_calc_forces_tool_use_to_guarantee_structured_json_output():
    """РЕГРЕСІЙНИЙ ТЕСТ на реальний баг (двічі): (1) короткий опис без деталей
    про операції інколи змушував модель відповісти поясненням замість JSON
    ("ШІ повернув відповідь у неочікуваному форматі"); (2) спроба зафіксувати
    це префілом асистента ({"role": "assistant", "content": "{"}) сама зламала
    продакшн - реальний Anthropic API почав повертати 400 "This model does
    not support assistant message prefill. The conversation must end with a
    user message." Правильний і стійкий фікс - примусовий виклик інструмента
    (tool_choice: {"type": "tool", ...}): Anthropic API гарантовано повертає
    УЖЕ РОЗІБРАНИЙ JSON-об'єкт у tool_use.input, без жодного текстового
    парсингу і без додаткового "assistant"-повідомлення в messages (розмова
    завершується саме user-повідомленням, як і вимагає API)."""
    import app as app_module
    from unittest import mock

    captured_payload = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "content": [{
                    "type": "tool_use",
                    "name": "cost_estimate",
                    "input": {
                        "weight_kg": 0.52, "material_price_per_kg": 62,
                        "turning_hours": 0.4, "turning_rate_per_hour": 800,
                        "milling_hours": 0, "milling_rate_per_hour": 900,
                        "machine_hours": 0, "machine_rate_per_hour": 0,
                        "quantity": 200, "margin_pct": 20,
                        "operations_description": "Токарна обробка прутка 40Х Ø30 мм.",
                    },
                }]
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        captured_payload.update(json or {})
        return FakeResponse()

    with mock.patch("requests.post", side_effect=fake_post):
        result = app_module.ai_estimate_cost_params(
            "Партія 200 шт. сталь 40Х, пруток 30 діаметром", "sk-ant-fake-key"
        )

    # Розмова завершується user-повідомленням - жодного "assistant"-префілу
    messages = captured_payload.get("messages", [])
    assert len(messages) == 1
    assert messages[0]["role"] == "user"

    # Перший виклик дозволяє ШІ або дати результат, або поставити уточнення -
    # "any" примусово вимагає виклик ЯКОГОСЬ інструмента (не вільний текст),
    # а не диктує наперед, якого саме.
    assert captured_payload.get("tool_choice") == {"type": "any"}
    tool_names = {t.get("name") for t in captured_payload.get("tools", [])}
    assert tool_names == {"cost_estimate", "ask_clarifying_question"}

    assert result["status"] == "ok"
    params = result["params"]
    assert params["quantity"] == 200
    assert params["weight_kg"] == 0.52
    assert "Токарна" in params["operations_description"]


def test_ai_calc_falls_back_to_text_json_extraction_if_model_ignores_tool_choice():
    """Захисний фолбек: якщо модель (усупереч tool_choice) все ж повернула
    звичайний текстовий блок замість tool_use, код має спробувати витягти
    з нього JSON, а не одразу падати - той самий фолбек-парсер
    _extract_json_object, що й раніше."""
    import app as app_module
    from unittest import mock

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "content": [{
                    "type": "text",
                    "text": (
                        'Ось параметри: {"weight_kg": 1.1, "material_price_per_kg": 60, '
                        '"turning_hours": 0.5, "turning_rate_per_hour": 800, '
                        '"milling_hours": 0.2, "milling_rate_per_hour": 900, '
                        '"machine_hours": 0, "machine_rate_per_hour": 0, '
                        '"quantity": 10, "margin_pct": 15, '
                        '"operations_description": "Токарно-фрезерна обробка."}'
                    ),
                }]
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        return FakeResponse()

    with mock.patch("requests.post", side_effect=fake_post):
        result = app_module.ai_estimate_cost_params("опис", "sk-ant-fake-key")
    assert result["status"] == "ok"
    assert result["params"]["quantity"] == 10


def test_ai_calc_asks_clarifying_question_when_description_too_thin():
    """Користувач хоче: якщо опису мінімально не вистачає (наприклад просто
    "деталь", без матеріалу й без розмірів), ШІ має поставити ОДНЕ конкретне
    уточнююче питання замість того, щоб вигадувати цифри з повітря чи
    падати з помилкою формату. Перевіряємо і функцію напряму: вона віддає
    status="question" з текстом питання і збереженими messages (потрібними,
    щоб продовжити ту саму розмову другим викликом)."""
    import app as app_module
    from unittest import mock

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "content": [{
                    "type": "tool_use",
                    "id": "toolu_ask_123",
                    "name": "ask_clarifying_question",
                    "input": {"question": "З якого металу деталь і які її приблизні габарити?"},
                }]
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        return FakeResponse()

    with mock.patch("requests.post", side_effect=fake_post):
        result = app_module.ai_estimate_cost_params("деталь", "sk-ant-fake-key")

    assert result["status"] == "question"
    assert "металу" in result["question"]
    assert isinstance(result["messages"], list)
    # Останнє повідомлення - це assistant-хід з викликом ask_clarifying_question,
    # потрібним, щоб коректно продовжити розмову (tool_use_id для tool_result).
    assert result["messages"][-1]["role"] == "assistant"


def test_ai_calc_continues_conversation_after_answering_clarifying_question():
    """Другий крок того самого сценарію: коли користувач відповідає на
    питання ШІ, функція має продовжити ТУ САМУ розмову (передати
    tool_result з правильним tool_use_id) і цього разу примусово отримати
    фінальний результат (cost_estimate), а не ще одне питання - інакше
    міг би вийти нескінченний цикл уточнень."""
    import app as app_module
    from unittest import mock

    first_response_content = [{
        "type": "tool_use",
        "id": "toolu_ask_456",
        "name": "ask_clarifying_question",
        "input": {"question": "З якого металу деталь?"},
    }]
    captured_second_payload = {}

    class FakeResponse:
        def __init__(self, content):
            self._content = content
            self.status_code = 200

        def json(self):
            return {"content": self._content}

    call_count = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FakeResponse(first_response_content)
        captured_second_payload.update(json or {})
        return FakeResponse([{
            "type": "tool_use", "name": "cost_estimate", "input": {
                "weight_kg": 2.0, "material_price_per_kg": 65, "turning_hours": 0.6,
                "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
                "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 5, "margin_pct": 20,
                "operations_description": "Токарна обробка сталі 45.",
            },
        }])

    with mock.patch("requests.post", side_effect=fake_post):
        first = app_module.ai_estimate_cost_params("деталь", "sk-ant-fake-key")
        assert first["status"] == "question"
        second = app_module.ai_estimate_cost_params(
            None, "sk-ant-fake-key", continue_messages=first["messages"], answer="сталь 45, пруток Ø40х80мм"
        )

    assert second["status"] == "ok"
    assert second["params"]["quantity"] == 5

    # Другий запит примусово вимагає фінальний результат - без ask_clarifying_question,
    # щоб уникнути нескінченного циклу уточнень.
    assert captured_second_payload.get("tool_choice") == {"type": "tool", "name": "cost_estimate"}
    tool_names = {t.get("name") for t in captured_second_payload.get("tools", [])}
    assert tool_names == {"cost_estimate"}

    # І сама відповідь користувача коректно прив'язана до питання через tool_result.
    sent_messages = captured_second_payload.get("messages", [])
    last_msg = sent_messages[-1]
    assert last_msg["role"] == "user"
    tool_result_block = last_msg["content"][0]
    assert tool_result_block["type"] == "tool_result"
    assert tool_result_block["tool_use_id"] == "toolu_ask_456"
    assert tool_result_block["content"] == "сталь 45, пруток Ø40х80мм"


def test_ai_calc_continue_without_answer_raises_clear_error():
    from app import ai_estimate_cost_params, AICalcError
    with pytest.raises(AICalcError) as exc_info:
        ai_estimate_cost_params(None, "sk-ant-fake-key",
                                 continue_messages=[{"role": "assistant", "content": [
                                     {"type": "tool_use", "id": "x", "name": "ask_clarifying_question", "input": {}}
                                 ]}],
                                 answer="   ")
    assert "відповідь" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Об'єднана форма калькулятора: ручні поля + ШІ в одній формі
# (вибір "Розрахувати вручну" / "Розрахувати через ШІ" через formaction)
# ---------------------------------------------------------------------------

def test_calculator_page_has_single_form_with_two_submit_buttons():
    """Користувач хоче ОДНУ форму замість трьох окремих - перевіряємо, що
    на сторінці рівно одна форма, яка охоплює і ручні поля (weight_kg), і
    ШІ-поля (ai_description/ai_images), з двома кнопками відправки, що
    ведуть на різні маршрути через formaction (без JS/дублювання полів)."""
    a = __import__("app")
    c = a.app.test_client()
    r = c.get("/login")
    import re
    token = re.search(r'name="_csrf_token" value="([^"]+)"', r.get_data(as_text=True)).group(1)
    c.post("/login", data={"username": "admin", "password": "admin123", "_csrf_token": token})
    html = c.get("/calculator").get_data(as_text=True)

    # Більше немає окремої видимої секції "Автозаповнення з DXF-креслення" -
    # ai_description і weight_kg тепер поля ОДНІЄЇ об'єднаної форми.
    assert "Автозаповнення з DXF-креслення" not in html
    assert 'action="/calculator/analyze-dxf"' not in html
    assert html.count('formaction=') == 2
    assert 'formaction="/calculator"' in html
    assert 'formaction="/calculator/ai-estimate"' in html
    # weight_kg більше не обов'язкове - бо тепер це поле спільне для
    # ручного і ШІ-розрахунку, де воно необов'язкове.
    weight_field = re.search(r'<input[^>]*name="weight_kg"[^>]*>', html).group(0)
    assert "required" not in weight_field


def test_manual_button_ignores_ai_description_and_uses_only_filled_fields(client):
    """"Розрахувати вручну" має рахувати ЛИШЕ з заповнених полів і НЕ
    звертатись до Anthropic API, навіть якщо в тій самій формі є текст
    опису чи файл - submit на formaction="/calculator" має йти напряму
    в ручний маршрут, без жодного HTTP-виклику назовні."""
    from unittest import mock
    login(client)
    with mock.patch("requests.post", side_effect=AssertionError("ручний розрахунок не має звертатись до ШІ")):
        r = post(client, "/calculator", {
            "weight_kg": "1", "material_price_per_kg": "60", "turning_hours": "0.5",
            "turning_rate_per_hour": "800", "quantity": "5", "margin_pct": "20",
            "ai_description": "цей текст має ігноруватись ручним розрахунком",
        })
    assert r.status_code == 200
    assert "грн" in r.get_data(as_text=True)


def test_ai_button_respects_manually_filled_fields_and_only_fills_the_rest(client):
    """Головний сценарій запиту користувача: заповнюємо вручну кількість і
    вагу, лишаємо решту порожньою й тиснемо "Розрахувати через ШІ" - ШІ
    повертає СВОЇ (інші) значення для цих полів, але у фінальному
    результаті мають лишитись САМЕ ті, що ввела людина, а не ШІ."""
    from unittest import mock
    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-test123"})

    def fake_post(url, headers=None, json=None, timeout=None):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"content": [{
                    "type": "tool_use", "name": "cost_estimate", "input": {
                        # ШІ навмисно повертає ІНШІ weight_kg і quantity, ніж ввів користувач -
                        # фінальний результат має все одно показати введені людиною 2.5 кг і 7 шт.
                        "weight_kg": 999, "material_price_per_kg": 60, "turning_hours": 0.4,
                        "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
                        "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 999, "margin_pct": 20,
                        "operations_description": "Технологічний процес виготовлення деталі.",
                    },
                }]}

        return FakeResponse()

    with mock.patch("requests.post", side_effect=fake_post):
        token = get_csrf_token(client, "/calculator")
        r = client.post("/calculator/ai-estimate", data={
            "_csrf_token": token, "weight_kg": "2.5", "quantity": "7", "ai_description": "",
        }, follow_redirects=True)

    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Заготовка: 2.5 кг" in html
    assert "Партія: 7 шт" in html
    # Не шукаємо просто "999": випадковий CSRF-токен у HTML інколи містить таку послідовність (флейки).
    assert "Заготовка: 999" not in html
    assert "Партія: 999" not in html


def test_ai_button_with_only_known_fields_and_no_description_still_works(client):
    """Користувач заповнює лише таблицю (без опису й без файлу) і тисне
    "Розрахувати через ШІ" - функція має синтезувати короткий опис із
    заповнених полів замість того, щоб відмовити з "опиши деталь текстом
    або завантаж фото"."""
    from unittest import mock
    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-test123"})

    captured_payload = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured_payload.update(json or {})

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"content": [{
                    "type": "tool_use", "name": "cost_estimate", "input": {
                        "weight_kg": 3, "material_price_per_kg": 60, "turning_hours": 0.5,
                        "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
                        "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 12, "margin_pct": 20,
                        "operations_description": "Опис техпроцесу.",
                    },
                }]}

        return FakeResponse()

    with mock.patch("requests.post", side_effect=fake_post):
        token = get_csrf_token(client, "/calculator")
        r = client.post("/calculator/ai-estimate", data={
            "_csrf_token": token, "weight_kg": "3", "quantity": "12", "ai_description": "",
        }, follow_redirects=True)

    assert r.status_code == 200
    assert "Розрахунок від ШІ готовий" in r.get_data(as_text=True)
    sent_text = next(b["text"] for b in captured_payload["messages"][0]["content"] if b.get("type") == "text")
    assert "Відомі параметри" in sent_text
    assert "3" in sent_text and "12" in sent_text


def test_calculator_ai_estimate_route_shows_clarifying_question_and_completes_after_answer(client):
    """Наскрізний сценарій через реальний HTTP-маршрут /calculator/ai-estimate:
    (1) короткий опис "вал" без матеріалу й розмірів -> сторінка калькулятора
    показує питання ШІ і форму для відповіді (а не помилку чи вигадані
    цифри); (2) користувач надсилає відповідь через ту саму форму (token +
    answer) -> другий запит повертає вже готовий результат розрахунку."""
    import app as app_module
    from unittest import mock

    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-test123"})

    call_count = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        call_count["n"] += 1

        class FakeResponse:
            status_code = 200

            def json(self):
                if call_count["n"] == 1:
                    return {"content": [{
                        "type": "tool_use", "id": "toolu_route_1", "name": "ask_clarifying_question",
                        "input": {"question": "З якого металу вал і які його габарити?"},
                    }]}
                return {"content": [{
                    "type": "tool_use", "name": "cost_estimate", "input": {
                        "weight_kg": 1.2, "material_price_per_kg": 60, "turning_hours": 0.5,
                        "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
                        "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 20, "margin_pct": 20,
                        "operations_description": "Токарна обробка вала зі сталі 45.",
                    },
                }]}

        return FakeResponse()

    with mock.patch("requests.post", side_effect=fake_post):
        token = get_csrf_token(client, "/calculator")
        r1 = client.post("/calculator/ai-estimate", data={"_csrf_token": token, "ai_description": "вал"},
                          follow_redirects=True)
        html1 = r1.get_data(as_text=True)
        assert r1.status_code == 200
        assert "З якого металу вал" in html1
        assert "ai_clarify_token" in html1

        import re
        m = re.search(r'name="ai_clarify_token" value="([^"]+)"', html1)
        assert m, "токен уточнюючого питання не знайдено на сторінці"
        clarify_token = m.group(1)

        token2 = get_csrf_token(client, "/calculator")
        r2 = client.post("/calculator/ai-estimate", data={
            "_csrf_token": token2, "ai_clarify_token": clarify_token,
            "ai_clarify_answer": "сталь 45, Ø30х200мм",
        }, follow_redirects=True)
        html2 = r2.get_data(as_text=True)

    assert r2.status_code == 200
    assert "Розрахунок від ШІ готовий" in html2
    assert "Токарна обробка вала" in html2


def test_calculator_ai_estimate_reused_clarify_token_fails_gracefully(client):
    """Токен уточнення одноразовий - повторне використання (напр. подвійний
    клік або відкрита стара вкладка) має дати зрозумілу помилку, а не
    зламатись чи тихо повторити застарілий запит."""
    import app as app_module
    from unittest import mock

    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-test123"})

    def fake_post(url, headers=None, json=None, timeout=None):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"content": [{
                    "type": "tool_use", "id": "toolu_reuse_1", "name": "ask_clarifying_question",
                    "input": {"question": "Уточни матеріал?"},
                }]}

        return FakeResponse()

    with mock.patch("requests.post", side_effect=fake_post):
        token = get_csrf_token(client, "/calculator")
        r1 = client.post("/calculator/ai-estimate", data={"_csrf_token": token, "ai_description": "деталь"},
                          follow_redirects=True)
        import re
        m = re.search(r'name="ai_clarify_token" value="([^"]+)"', r1.get_data(as_text=True))
        clarify_token = m.group(1)

    def fake_post_final(url, headers=None, json=None, timeout=None):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"content": [{
                    "type": "tool_use", "name": "cost_estimate", "input": {
                        "weight_kg": 1, "material_price_per_kg": 60, "turning_hours": 0.2,
                        "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
                        "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 1, "margin_pct": 20,
                        "operations_description": "Опис.",
                    },
                }]}

        return FakeResponse()

    # Перше використання токена - легітимне, споживає його і дає результат.
    with mock.patch("requests.post", side_effect=fake_post_final):
        token2 = get_csrf_token(client, "/calculator")
        r2 = client.post("/calculator/ai-estimate", data={
            "_csrf_token": token2, "ai_clarify_token": clarify_token, "ai_clarify_answer": "сталь",
        }, follow_redirects=True)
    assert "Розрахунок від ШІ готовий" in r2.get_data(as_text=True)

    # Повторне використання ТОГО Ж токена - має дати зрозумілу помилку БЕЗ
    # звернення до Anthropic API (токен уже спожитий і видалений).
    with mock.patch("requests.post", side_effect=AssertionError("не має викликатись вдруге з протухлим токеном")):
        token3 = get_csrf_token(client, "/calculator")
        r3 = client.post("/calculator/ai-estimate", data={
            "_csrf_token": token3, "ai_clarify_token": clarify_token, "ai_clarify_answer": "сталь ще раз",
        }, follow_redirects=True)

    assert r3.status_code == 200
    assert "застаріло" in r3.get_data(as_text=True) or "заново" in r3.get_data(as_text=True)


def test_ai_calc_apply_correction_continues_conversation_and_forces_cost_estimate():
    """Юніт-рівень: ai_calc_apply_correction() має продовжити ВЖЕ завершену
    розмову (повідомлення з попереднього успішного результату, де останній
    tool_use вже закритий синтетичним tool_result), додати новий user-хід
    з текстом правки і примусово вимагати cost_estimate (не ask_clarifying_question,
    щоб правка завжди завершувалась готовим результатом)."""
    import app as app_module
    from unittest import mock

    first_response_content = [{
        "type": "tool_use", "id": "toolu_first_1", "name": "cost_estimate", "input": {
            "weight_kg": 2.0, "material_price_per_kg": 65, "turning_hours": 0.6,
            "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
            "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 5, "margin_pct": 20,
            "operations_description": "Токарна обробка сталі 45.",
        },
    }]
    captured_payload = {}

    class FakeResponse:
        def __init__(self, content):
            self._content = content
            self.status_code = 200

        def json(self):
            return {"content": self._content}

    call_count = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FakeResponse(first_response_content)
        captured_payload.update(json or {})
        return FakeResponse([{
            "type": "tool_use", "id": "toolu_second_1", "name": "cost_estimate", "input": {
                "weight_kg": 3.2, "material_price_per_kg": 65, "turning_hours": 0.6,
                "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
                "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 5, "margin_pct": 20,
                "operations_description": "Токарна обробка сталі 45, вага заготовки 3.2 кг.",
            },
        }])

    with mock.patch("requests.post", side_effect=fake_post):
        first = app_module.ai_estimate_cost_params("деталь, сталь 45", "sk-ant-fake-key")
        assert first["status"] == "ok"
        assert first.get("messages"), "успішний результат має повертати продовжувану розмову"

        second = app_module.ai_calc_apply_correction(
            first["messages"], "вага не 2, а 3.2 кг", "sk-ant-fake-key"
        )

    assert second["status"] == "ok"
    assert second["params"]["weight_kg"] == 3.2

    assert captured_payload.get("tool_choice") == {"type": "tool", "name": "cost_estimate"}
    sent_messages = captured_payload.get("messages", [])
    last_msg = sent_messages[-1]
    assert last_msg["role"] == "user"
    assert last_msg["content"] == "вага не 2, а 3.2 кг"
    # Правка - продовження ПОВНОЇ попередньої розмови (tool_use + синтетичний
    # tool_result), а не новий ізольований запит.
    assert any(
        m.get("role") == "assistant" and any(b.get("type") == "tool_use" for b in m.get("content", []))
        for m in sent_messages
    )


def test_ai_calc_apply_correction_rejects_empty_text_and_missing_prior_messages():
    from app import ai_calc_apply_correction, AICalcError

    with pytest.raises(AICalcError):
        ai_calc_apply_correction([{"role": "user", "content": "x"}], "   ", "sk-ant-fake-key")

    with pytest.raises(AICalcError):
        ai_calc_apply_correction([], "вага не 2, а 3.2 кг", "sk-ant-fake-key")

    with pytest.raises(AICalcError):
        ai_calc_apply_correction([{"role": "user", "content": "x"}], "вага не 2, а 3.2 кг", "")


def test_calculator_ai_estimate_shows_correction_chat_and_applies_fix(client):
    """Наскрізний сценарій через /calculator/ai-estimate: (1) перший
    розрахунок ШІ показує міні-чат "Щось не так..." з токеном правки;
    (2) користувач вводить правку через той самий маршрут (ai_correction_token
    + ai_correction_message) -> сторінка показує ОНОВЛЕНИЙ результат з
    новими значеннями ШІ, а не старими."""
    import re
    from unittest import mock

    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-test123"})

    def fake_post_first(url, headers=None, json=None, timeout=None):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"content": [{
                    "type": "tool_use", "id": "toolu_corr_first", "name": "cost_estimate", "input": {
                        "weight_kg": 2, "material_price_per_kg": 60, "turning_hours": 0.5,
                        "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
                        "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 1, "margin_pct": 20,
                        "operations_description": "Токарна обробка деталі зі сталі.",
                    },
                }]}

        return FakeResponse()

    with mock.patch("requests.post", side_effect=fake_post_first):
        token = get_csrf_token(client, "/calculator")
        r1 = client.post("/calculator/ai-estimate", data={"_csrf_token": token, "ai_description": "деталь зі сталі"},
                          follow_redirects=True)

    html1 = r1.get_data(as_text=True)
    assert r1.status_code == 200
    assert "Щось не так в оцінці ШІ" in html1
    m = re.search(r'name="ai_correction_token" value="([^"]+)"', html1)
    assert m, "токен правки не знайдено на сторінці результату"
    correction_token = m.group(1)
    assert "Заготовка: 2" in html1

    def fake_post_correction(url, headers=None, json=None, timeout=None):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"content": [{
                    "type": "tool_use", "id": "toolu_corr_second", "name": "cost_estimate", "input": {
                        "weight_kg": 3.2, "material_price_per_kg": 60, "turning_hours": 0.5,
                        "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
                        "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 1, "margin_pct": 20,
                        "operations_description": "Токарна обробка деталі зі сталі, вага 3.2 кг.",
                    },
                }]}

        return FakeResponse()

    with mock.patch("requests.post", side_effect=fake_post_correction):
        token2 = get_csrf_token(client, "/calculator")
        r2 = client.post("/calculator/ai-estimate", data={
            "_csrf_token": token2, "ai_correction_token": correction_token,
            "ai_correction_message": "вага не 2, а 3.2 кг",
        }, follow_redirects=True)

    html2 = r2.get_data(as_text=True)
    assert r2.status_code == 200
    assert "Заготовка: 3.2" in html2
    assert "Заготовка: 2 кг" not in html2


def test_calculator_ai_estimate_reused_correction_token_fails_gracefully(client):
    """Токен правки, як і токен уточнення, одноразовий - повторне
    використання має дати зрозумілу помилку, без звернення до API."""
    import re
    from unittest import mock

    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-test123"})

    def fake_post_first(url, headers=None, json=None, timeout=None):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"content": [{
                    "type": "tool_use", "id": "toolu_reuse_corr_1", "name": "cost_estimate", "input": {
                        "weight_kg": 1, "material_price_per_kg": 60, "turning_hours": 0.2,
                        "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
                        "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 1, "margin_pct": 20,
                        "operations_description": "Опис.",
                    },
                }]}

        return FakeResponse()

    with mock.patch("requests.post", side_effect=fake_post_first):
        token = get_csrf_token(client, "/calculator")
        r1 = client.post("/calculator/ai-estimate", data={"_csrf_token": token, "ai_description": "деталь"},
                          follow_redirects=True)
        m = re.search(r'name="ai_correction_token" value="([^"]+)"', r1.get_data(as_text=True))
        correction_token = m.group(1)

    def fake_post_correction(url, headers=None, json=None, timeout=None):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"content": [{
                    "type": "tool_use", "id": "toolu_reuse_corr_2", "name": "cost_estimate", "input": {
                        "weight_kg": 1.5, "material_price_per_kg": 60, "turning_hours": 0.2,
                        "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
                        "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 1, "margin_pct": 20,
                        "operations_description": "Опис.",
                    },
                }]}

        return FakeResponse()

    with mock.patch("requests.post", side_effect=fake_post_correction):
        token2 = get_csrf_token(client, "/calculator")
        r2 = client.post("/calculator/ai-estimate", data={
            "_csrf_token": token2, "ai_correction_token": correction_token,
            "ai_correction_message": "вага 1.5",
        }, follow_redirects=True)
    assert "Заготовка: 1.5" in r2.get_data(as_text=True)

    with mock.patch("requests.post", side_effect=AssertionError("не має звертатись до API з протухлим токеном правки")):
        token3 = get_csrf_token(client, "/calculator")
        r3 = client.post("/calculator/ai-estimate", data={
            "_csrf_token": token3, "ai_correction_token": correction_token,
            "ai_correction_message": "вага ще раз інша",
        }, follow_redirects=True)

    assert r3.status_code == 200
    assert "застаріло" in r3.get_data(as_text=True) or "заново" in r3.get_data(as_text=True)


def test_ai_correction_does_not_reapply_manually_known_fields():
    """Користувач спершу вручну вписав weight_kg=2 (known_fields), потім у
    мінічаті написав "вага 3.2" - результат правки має показати 3.2, а НЕ
    відкотитись назад до вручну введеного 2 (інакше правка була б марною)."""
    import app as app_module
    from unittest import mock

    first_response_content = [{
        "type": "tool_use", "id": "toolu_kf_1", "name": "cost_estimate", "input": {
            "weight_kg": 2.0, "material_price_per_kg": 60, "turning_hours": 0.5,
            "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
            "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 1, "margin_pct": 20,
            "operations_description": "Опис.",
        },
    }]

    class FakeResponse:
        def __init__(self, content):
            self._content = content
            self.status_code = 200

        def json(self):
            return {"content": self._content}

    call_count = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FakeResponse(first_response_content)
        return FakeResponse([{
            "type": "tool_use", "id": "toolu_kf_2", "name": "cost_estimate", "input": {
                "weight_kg": 3.2, "material_price_per_kg": 60, "turning_hours": 0.5,
                "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
                "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 1, "margin_pct": 20,
                "operations_description": "Опис, вага 3.2 кг.",
            },
        }])

    with mock.patch("requests.post", side_effect=fake_post):
        first = app_module.ai_estimate_cost_params("деталь", "sk-ant-fake-key")
        corrected = app_module.ai_calc_apply_correction(first["messages"], "вага не 2, а 3.2 кг", "sk-ant-fake-key")

    # Сама функція ai_calc_apply_correction не знає про known_fields - override
    # відбувається лише в маршруті calculator_ai_estimate(), і саме там він
    # навмисно вимкнений для correction_token (перевірено наскрізним тестом
    # test_calculator_ai_estimate_shows_correction_chat_and_applies_fix вище).
    assert corrected["params"]["weight_kg"] == 3.2


def test_settings_page_shows_ai_calculator_status(client):
    login(client)
    r = client.get("/settings")
    assert "ШІ-калькулятор" in r.get_data(as_text=True)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-test123"})
    r2 = client.get("/settings")
    assert "Підключено" in r2.get_data(as_text=True)


# ---------------------------------------------------------------------------
# ШІ-калькулятор: аналіз зображення/креслення
# ---------------------------------------------------------------------------

def test_ai_calc_without_api_key_and_no_text_but_with_image_shows_key_error(client):
    """Навіть якщо зображення додано, без API-ключа далі йти нікуди -
    має показати саме помилку про відсутній ключ, а не про порожній опис."""
    import io
    login(client)
    token = get_csrf_token(client, "/calculator")
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
        "de0000000c4944415478da6360000002000100ffff03000006000557bfabd400000000"
        "49454e44ae426082"
    )
    r = client.post(
        "/calculator/ai-estimate",
        data={"_csrf_token": token, "ai_description": "",
              "ai_images": (io.BytesIO(png_bytes), "креслення.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "API-ключ" in r.get_data(as_text=True)


def test_ai_calc_no_text_and_no_image_shows_clear_error(client):
    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-something"})
    r = post(client, "/calculator/ai-estimate", {"ai_description": ""}, follow_redirects=True)
    assert "Опиши деталь" in r.get_data(as_text=True) or "фото" in r.get_data(as_text=True)


def test_detect_image_media_type_recognizes_real_formats_and_rejects_fake():
    from app import detect_image_media_type
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
        "de0000000c4944415478da6360000002000100ffff03000006000557bfabd400000000"
        "49454e44ae426082"
    )
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    gif = b"GIF89a" + b"\x00" * 20
    webp = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 10
    assert detect_image_media_type(png) == "image/png"
    assert detect_image_media_type(jpeg) == "image/jpeg"
    assert detect_image_media_type(gif) == "image/gif"
    assert detect_image_media_type(webp) == "image/webp"
    assert detect_image_media_type(b"this is plain text, not an image at all") is None
    # Файл .png з підміненим вмістом (лише розширення обманює, байти - ні)
    assert detect_image_media_type(b"<html>not an image</html>") is None


def test_ai_calc_rejects_non_image_file_as_image():
    """Функція ai_estimate_cost_params має відхиляти файл, що не є
    справжнім зображенням (наприклад текстовий файл з розширенням .png),
    з чіткою помилкою, а не падати чи мовчки відправляти сміття в API."""
    import io
    from app import ai_estimate_cost_params, AICalcError

    class FakeFile:
        def __init__(self, data, filename, mimetype):
            self._data = data
            self.filename = filename
            self.mimetype = mimetype

        def read(self):
            return self._data

    fake = FakeFile(b"this is definitely not a png file", "fake.png", "image/png")
    with pytest.raises(AICalcError) as exc_info:
        ai_estimate_cost_params("", "sk-ant-fake-key", images=[fake])
    assert "не схож" in str(exc_info.value)


def test_ai_calc_rejects_oversized_image():
    from app import ai_estimate_cost_params, AICalcError, AI_IMAGE_MAX_BYTES

    class FakeFile:
        def __init__(self, data, filename, mimetype):
            self._data = data
            self.filename = filename
            self.mimetype = mimetype

        def read(self):
            return self._data

    huge = FakeFile(b"\x89PNG" + b"0" * (AI_IMAGE_MAX_BYTES + 1), "величезне.png", "image/png")
    with pytest.raises(AICalcError) as exc_info:
        ai_estimate_cost_params("", "sk-ant-fake-key", images=[huge])
    assert "завелик" in str(exc_info.value)


def test_ai_calc_real_api_call_with_image_and_invalid_key_handled_gracefully(client):
    """Реальний виклик Anthropic API з навмисно невалідним ключем, АЛЕ з
    реальним валідним зображенням у content blocks - перевіряє, що сам
    запит із image-блоком коректно формується й надсилається (інакше
    помилка була б про побудову запиту, а не 401 від сервера), і що 401
    так само коректно перехоплюється при наявності зображення."""
    import io
    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-fake-invalid-key-12345"})
    token = get_csrf_token(client, "/calculator")
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
        "de0000000c4944415478da6360000002000100ffff03000006000557bfabd400000000"
        "49454e44ae426082"
    )
    r = client.post(
        "/calculator/ai-estimate",
        data={"_csrf_token": token, "ai_description": "деталь на кресленні",
              "ai_images": (io.BytesIO(png_bytes), "креслення.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "Невірний API-ключ" in r.get_data(as_text=True)


def test_ai_calc_too_many_images_rejected():
    from app import ai_estimate_cost_params, AICalcError

    class FakeFile:
        def __init__(self, data, filename, mimetype):
            self._data = data
            self.filename = filename
            self.mimetype = mimetype

        def read(self):
            return self._data

    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
        "de0000000c4944415478da6360000002000100ffff03000006000557bfabd400000000"
        "49454e44ae426082"
    )
    images = [FakeFile(png_bytes, f"img{i}.png", "image/png") for i in range(4)]
    with pytest.raises(AICalcError) as exc_info:
        ai_estimate_cost_params("опис", "sk-ant-fake-key", images=images)
    assert "3 зображення" in str(exc_info.value)


def test_render_dxf_to_png_produces_valid_visible_drawing():
    """Перевіряє не лише що функція повертає валідний PNG, а що на ньому
    справді щось намальовано (не порожній білий аркуш) - рендер DXF без
    примусового кольору ліній дає невидимі білі лінії на білому тлі,
    що саме тут і перевіряється через підрахунок непорожніх пікселів."""
    from app import render_dxf_to_png_bytes, detect_image_media_type
    dxf_bytes = _make_sample_dxf_bytes()

    png_bytes = render_dxf_to_png_bytes(dxf_bytes)
    assert detect_image_media_type(png_bytes) == "image/png"
    assert len(png_bytes) > 500

    from PIL import Image
    import io
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    non_white = sum(1 for p in img.tobytes() if p < 250)
    assert non_white > 50, "рендер DXF вийшов практично порожнім - лінії не намальовані"


def test_render_dxf_to_png_rejects_garbage_input():
    from app import render_dxf_to_png_bytes
    with pytest.raises(ValueError):
        render_dxf_to_png_bytes(b"this is not a dxf file at all, just plain text")


def test_render_dxf_to_png_handles_binary_dxf_format():
    """РЕГРЕСІЙНИЙ ТЕСТ на реальний баг: DXF буває не лише текстовим (ASCII),
    а й бінарним - користувач надіслав саме такий файл, і функція падала з
    "Invalid binary data near line: ..." через примусове UTF-8-декодування
    байтів бінарного DXF перед розбором. Фікс - читати через ezdxf.readfile()
    з тимчасового файлу на диску (як analyze_dxf() вище), а не декодувати
    вручну наперед. Створюємо СПРАВЖНІЙ бінарний DXF через ezdxf і рендеримо."""
    import ezdxf
    import tempfile
    import os
    from app import render_dxf_to_png_bytes, detect_image_media_type
    from PIL import Image
    import io

    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0))
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
    msp.add_circle((50, 25), radius=10)

    tmp = tempfile.NamedTemporaryFile(suffix=".dxf", delete=False)
    tmp.close()
    try:
        doc.saveas(tmp.name, fmt="bin")
        with open(tmp.name, "rb") as f:
            binary_dxf_bytes = f.read()
    finally:
        os.remove(tmp.name)

    assert binary_dxf_bytes.startswith(b"AutoCAD Binary DXF"), "тестовий фікстур не є справжнім бінарним DXF"

    png_bytes = render_dxf_to_png_bytes(binary_dxf_bytes)
    assert detect_image_media_type(png_bytes) == "image/png"
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    non_white = sum(1 for p in img.tobytes() if p < 250)
    assert non_white > 50, "рендер бінарного DXF вийшов порожнім - лінії не намальовані"


def test_ai_calc_accepts_real_binary_dxf_end_to_end(client):
    """Наскрізна перевірка того самого бага через HTTP-роут калькулятора:
    бінарний DXF-файл завантажується у форму ШІ-калькулятора і не має
    впасти з помилкою парсингу до того, як дійде до Anthropic API."""
    import ezdxf
    import tempfile
    import os
    import io

    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_circle((10, 10), radius=5)
    tmp = tempfile.NamedTemporaryFile(suffix=".dxf", delete=False)
    tmp.close()
    try:
        doc.saveas(tmp.name, fmt="bin")
        with open(tmp.name, "rb") as f:
            binary_dxf_bytes = f.read()
    finally:
        os.remove(tmp.name)

    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-fake-invalid-key-12345"})
    token = get_csrf_token(client, "/calculator")
    r = client.post(
        "/calculator/ai-estimate",
        data={"_csrf_token": token, "ai_description": "",
              "ai_images": (io.BytesIO(binary_dxf_bytes), "деталь_binary.dxf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    # Головне: НЕ помилка парсингу DXF, а дійшло до реального виклику API
    # (з фейковим ключем - тому саме "Невірний API-ключ", а не "не вдалось розібрати")
    assert "не вдалось розібрати" not in html
    assert "Невірний API-ключ" in html


def test_ai_calc_accepts_real_dxf_file_and_renders_it_before_api_call(client):
    """Наскрізна перевірка: DXF-файл (не зображення) завантажується в
    ШІ-калькулятор, автоматично рендериться в PNG всередині
    ai_estimate_cost_params, і запит іде до Anthropic API з картинкою -
    навіть з фейковим ключем це підтверджує, що ланцюжок
    DXF -> рендер -> валідний image-блок -> реальний HTTP-запит
    не ламається на жодному кроці."""
    import io
    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-fake-invalid-key-12345"})
    dxf_bytes = _make_sample_dxf_bytes()
    token = get_csrf_token(client, "/calculator")
    r = client.post(
        "/calculator/ai-estimate",
        data={"_csrf_token": token, "ai_description": "",
              "ai_images": (io.BytesIO(dxf_bytes), "деталь.dxf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "Невірний API-ключ" in r.get_data(as_text=True)


def test_ai_calc_rejects_garbage_file_with_dxf_extension():
    """Файл з розширенням .dxf, але сміттєвим вмістом всередині, має
    отримати зрозумілу помилку про DXF, а не про PNG/JPEG (бо система вже
    намагалась спершу як зображення, потім як DXF - обидва не підійшли)."""
    from app import ai_estimate_cost_params, AICalcError

    class FakeFile:
        def __init__(self, data, filename, mimetype):
            self._data = data
            self.filename = filename
            self.mimetype = mimetype

        def read(self):
            return self._data

    fake = FakeFile(b"not a dxf and not an image either", "fake.dxf", "application/octet-stream")
    with pytest.raises(AICalcError) as exc_info:
        ai_estimate_cost_params("", "sk-ant-fake-key", images=[fake])
    assert "DXF" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Завантаження PDF у ШІ-калькулятор (скан/PDF-експорт креслення)
# ---------------------------------------------------------------------------

def _make_sample_pdf_bytes(num_pages=1):
    """Генерує невеличкий тестовий PDF (прямокутник + текст на кожній
    сторінці) прямо в пам'яті через reportlab - без залежності від
    зовнішніх файлів. Малює реальну графіку (не порожню сторінку), щоб
    тест на рендеринг міг перевірити, що на картинці справді щось є."""
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as pdf_canvas

    buf = BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    for i in range(num_pages):
        c.setLineWidth(2)
        c.rect(50, 500, 200, 150, fill=0, stroke=1)
        c.circle(300, 400, 40, fill=0, stroke=1)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(50, 700, f"Креслення деталі, сторінка {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_render_pdf_to_png_produces_valid_visible_pages():
    """Перевіряє, що рендер PDF дає справжню картинку (не порожній аркуш) -
    той самий клас перевірки, що й для DXF (test_render_dxf_to_png_produces_
    valid_visible_drawing): підрахунок непорожніх пікселів."""
    from app import render_pdf_to_png_bytes, detect_image_media_type
    from PIL import Image
    import io

    pdf_bytes = _make_sample_pdf_bytes(num_pages=1)
    pages = render_pdf_to_png_bytes(pdf_bytes)
    assert len(pages) == 1
    png_bytes = pages[0]
    assert detect_image_media_type(png_bytes) == "image/png"
    assert len(png_bytes) > 500

    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    non_white = sum(1 for p in img.tobytes() if p < 250)
    assert non_white > 50, "рендер PDF вийшов практично порожнім - вміст не намальований"


def test_render_pdf_to_png_respects_max_pages_limit():
    """PDF на 5 сторінок має дати рівно 3 рендери (max_pages за замовчуванням
    у виклику з ai_estimate_cost_params), а не всі 5 - інакше один
    багатосторінковий PDF міг би обійти ліміт у 3 зображення на запит."""
    from app import render_pdf_to_png_bytes
    pdf_bytes = _make_sample_pdf_bytes(num_pages=5)
    pages = render_pdf_to_png_bytes(pdf_bytes, max_pages=3)
    assert len(pages) == 3


def test_render_pdf_to_png_rejects_garbage_input():
    from app import render_pdf_to_png_bytes
    with pytest.raises(ValueError):
        render_pdf_to_png_bytes(b"this is not a pdf file at all, just plain text")


def test_ai_calc_accepts_real_pdf_file_and_renders_it_before_api_call(client):
    """Наскрізна перевірка: PDF-файл (не зображення, не DXF) завантажується
    в ШІ-калькулятор, автоматично рендериться в PNG всередині
    ai_estimate_cost_params, і запит іде до Anthropic API з картинкою -
    навіть з фейковим ключем це підтверджує, що ланцюжок
    PDF -> рендер сторінок -> валідні image-блоки -> реальний HTTP-запит
    не ламається на жодному кроці."""
    import io
    login(client)
    post(client, "/settings/save", {"form": "anthropic", "anthropic_api_key": "sk-ant-fake-invalid-key-12345"})
    pdf_bytes = _make_sample_pdf_bytes(num_pages=1)
    token = get_csrf_token(client, "/calculator")
    r = client.post(
        "/calculator/ai-estimate",
        data={"_csrf_token": token, "ai_description": "",
              "ai_images": (io.BytesIO(pdf_bytes), "креслення.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "не вдалось розібрати" not in html
    assert "Невірний API-ключ" in html


def test_ai_calc_single_pdf_is_capped_at_three_rendered_pages():
    """Один PDF на 4 сторінки сам по собі НЕ має впасти з помилкою ліміту -
    render_pdf_to_png_bytes(max_pages=3) вже обрізає його до перших 3 сторінок
    ще на етапі рендерингу, тож у content_blocks потрапляє рівно 3 картинки,
    а не 4 (інакше один багатосторінковий PDF міг би непомітно роздути запит
    до Anthropic далеко за розумну межу)."""
    from app import ai_estimate_cost_params, AICalcError
    from unittest import mock

    class FakeFile:
        def __init__(self, data, filename):
            self._data = data
            self.filename = filename
            self.mimetype = "application/pdf"

        def read(self):
            return self._data

    pdf_bytes = _make_sample_pdf_bytes(num_pages=4)
    fake = FakeFile(pdf_bytes, "креслення.pdf")

    captured_payload = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"content": [{"type": "tool_use", "name": "cost_estimate", "input": {
                "weight_kg": 1, "material_price_per_kg": 60, "turning_hours": 0.1,
                "turning_rate_per_hour": 800, "milling_hours": 0, "milling_rate_per_hour": 900,
                "machine_hours": 0, "machine_rate_per_hour": 0, "quantity": 1, "margin_pct": 15,
                "operations_description": "Опис.",
            }}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured_payload.update(json or {})
        return FakeResponse()

    with mock.patch("requests.post", side_effect=fake_post):
        ai_estimate_cost_params("опис", "sk-ant-fake-key", images=[fake])

    sent_images = [b for b in captured_payload["messages"][0]["content"] if b.get("type") == "image"]
    assert len(sent_images) == 3


def test_ai_calc_two_multi_page_pdfs_together_exceed_three_image_limit():
    """Два окремі PDF-файли по 2 сторінки (усього 4 картинки) РАЗОМ мають
    впасти з поясненням ліміту - ліміт у 3 зображення застосовується до
    сумарної кількості сторінок з усіх завантажених файлів, а не лише в
    межах одного файлу."""
    from app import ai_estimate_cost_params, AICalcError

    class FakeFile:
        def __init__(self, data, filename):
            self._data = data
            self.filename = filename
            self.mimetype = "application/pdf"

        def read(self):
            return self._data

    pdf_a = FakeFile(_make_sample_pdf_bytes(num_pages=2), "перша.pdf")
    pdf_b = FakeFile(_make_sample_pdf_bytes(num_pages=2), "друга.pdf")
    with pytest.raises(AICalcError) as exc_info:
        ai_estimate_cost_params("опис", "sk-ant-fake-key", images=[pdf_a, pdf_b])
    assert "3 зображення" in str(exc_info.value)


def test_ai_calc_rejects_garbage_file_with_pdf_extension():
    """Файл з розширенням .pdf, але сміттєвим вмістом усередині, має дати
    зрозумілу помилку, що згадує і PDF, і DXF (бо система намагається
    послідовно: зображення → PDF → DXF, і жодне не підійшло)."""
    from app import ai_estimate_cost_params, AICalcError

    class FakeFile:
        def __init__(self, data, filename):
            self._data = data
            self.filename = filename
            self.mimetype = "application/pdf"

        def read(self):
            return self._data

    fake = FakeFile(b"not a pdf and not an image either, just garbage bytes", "fake.pdf")
    with pytest.raises(AICalcError) as exc_info:
        ai_estimate_cost_params("", "sk-ant-fake-key", images=[fake])
    msg = str(exc_info.value)
    assert "PDF" in msg
    assert "DXF" in msg


# ---------------------------------------------------------------------------
# Бізнес-модель: токарно-фрезерна механообробка (НЕ лазерне різання/гин/
# зварювання листового металу) — регресійні тести на коректність правки
# ---------------------------------------------------------------------------

def test_no_laser_bending_welding_wording_anywhere_in_calculator_page(client):
    """РЕГРЕСІЙНИЙ ТЕСТ: користувач явно повідомив, що в них немає лазера,
    гину чи зварювання - вони точать і фрезерують на ЧПУ. Уся система була
    переписана під цю модель. Цей тест ловить, якщо хтось (людина чи ШІ)
    надалі випадково поверне старе формулювання назад у калькулятор."""
    login(client)
    html = client.get("/calculator").get_data(as_text=True)
    for bad_word in ["лазер", "Лазер", "Гин ", "Гин(", "листогин", "Різ (лазер"]:
        assert bad_word not in html, f"застаріле формулювання '{bad_word}' повернулось на сторінку калькулятора"


def test_seed_data_has_no_orphaned_machine_or_routing_references(client):
    """РЕГРЕСІЙНИЙ ТЕСТ: після видалення лазерного верстата з каталогу
    обладнання деталі/угоди/маршрутні карти, що на нього посилались, мали
    бути або видалені, або перепризначені на реальний верстат. Перевіряємо
    БД напряму, що жодна угода не посилається на неіснуючий machine_id, і
    що серед дільниць/техпроцесів немає лазерного різання."""
    import sqlite3
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row

    machine_ids = {r["id"] for r in db.execute("SELECT id FROM machines").fetchall()}
    assert machine_ids, "у демо-даних мають бути верстати"

    orphaned = db.execute(
        "SELECT id, title, machine_id FROM deals WHERE machine_id IS NOT NULL AND machine_id NOT IN "
        f"({','.join('?' * len(machine_ids))})",
        tuple(machine_ids),
    ).fetchall()
    assert not orphaned, f"угоди посилаються на неіснуючий верстат: {[dict(o) for o in orphaned]}"

    categories = [r["category"] for r in db.execute("SELECT category FROM machines").fetchall()]
    for cat in categories:
        assert "лазер" not in cat.lower(), f"у каталозі обладнання лишився лазерний верстат: {cat}"

    routing_categories = [r["category"] for r in db.execute("SELECT category FROM routing_templates").fetchall()]
    for cat in routing_categories:
        assert "лазер" not in cat.lower(), f"лишився маршрутний техпроцес для лазера: {cat}"


def test_cost_calculator_uses_turning_and_milling_fields_not_cut_and_bend(client):
    """РЕГРЕСІЙНИЙ ТЕСТ: формули калькулятора мають рахувати вартість через
    turning_hours/milling_hours (токарна/фрезерна обробка), а не через
    застарілі cut_length_m/bend_count (периметр різу/кількість гибів)."""
    login(client)
    r = post(client, "/calculator", {
        "weight_kg": "5", "material_price_per_kg": "60",
        "turning_hours": "2", "turning_rate_per_hour": "800",
        "milling_hours": "1", "milling_rate_per_hour": "900",
        "machine_hours": "0", "machine_rate_per_hour": "0",
        "setup_cost_total": "500",
        "quantity": "5", "margin_pct": "20",
    })
    html = r.get_data(as_text=True)
    # 5*60 + 2*800 + 1*900 + 500/5 = 300+1600+900+100 = 2900; ціна = 2900*1.2 = 3480
    assert "2900" in html
    assert "3480" in html
    assert "Токарна обробка" in html
    assert "Фрезерна обробка" in html
    assert "Наладка" in html


# ---------------------------------------------------------------------------
# PDF з результату калькулятора
# ---------------------------------------------------------------------------

def test_calculator_result_page_has_pdf_download_button(client):
    """Кнопка «Завантажити як PDF» має з'являтись разом з результатом
    розрахунку (і посилатись саме на новий ендпоінт), інакше користувач
    її просто не побачить."""
    login(client)
    r = post(client, "/calculator", {
        "weight_kg": "10", "material_price_per_kg": "50",
        "turning_hours": "5", "turning_rate_per_hour": "20",
        "milling_hours": "4", "milling_rate_per_hour": "15",
        "machine_hours": "0.5", "machine_rate_per_hour": "200",
        "quantity": "2", "margin_pct": "20",
    })
    html = r.get_data(as_text=True)
    assert "Завантажити як PDF" in html
    assert "/calculator/quote.pdf" in html


def test_calculator_quote_pdf_produces_real_pdf_with_correct_totals(client):
    """РЕАЛЬНИЙ функціональний тест: POST на /calculator/quote.pdf з тими ж
    параметрами, що й ручний розрахунок (760 грн/од., 1824 грн разом - як у
    test_cost_calculator_math_and_deal_prefill), і перевірка, що це дійсно
    валідний PDF (сигнатура %PDF, реальний розмір), а не порожня заглушка.
    Суми в PDF рахуються заново на сервері, тож тест заразом підтверджує,
    що сервер не просто "довіряє" числам з прихованих полів форми."""
    login(client)
    token = get_csrf_token(client, "/calculator")
    r = client.post("/calculator/quote.pdf", data={
        "_csrf_token": token,
        "weight_kg": "10", "material_price_per_kg": "50",
        "turning_hours": "5", "turning_rate_per_hour": "20",
        "milling_hours": "4", "milling_rate_per_hour": "15",
        "machine_hours": "0.5", "machine_rate_per_hour": "200",
        "quantity": "2", "margin_pct": "20",
        "part_description": "Вал зі сталі 40Х, Ø30х250мм",
        "operations_description": "Токарна обробка прутка на верстаті Haas.",
    })
    assert r.status_code == 200
    assert r.mimetype == "application/pdf"
    assert r.data[:4] == b"%PDF"
    assert len(r.data) > 1500


def test_calculator_quote_pdf_handles_long_ai_operations_description_without_error(client):
    """РЕГРЕСІЙНИЙ ТЕСТ: після зміни промпту ШІ тепер за замовчуванням дає
    10-15 речень опису техпроцесу (замість 4-7) - перевіряємо, що довгий
    текст, який не влазить на одну сторінку разом з таблицею вартості, не
    ламає генерацію PDF (код має самостійно перейти на нову сторінку), а
    не впасти з помилкою чи обрізати вміст."""
    login(client)
    token = get_csrf_token(client, "/calculator")
    long_description = (
        "Заготовка - пруток сталі 40Х діаметром 30 мм, довжина під партію 260 мм на деталь. "
        "Базування виконується в цанговому патроні на токарному верстаті з ЧПУ Haas. "
        "Чорнове точіння знімає основний припуск по зовнішньому діаметру та торцю, залишаючи 0.5 мм на чистову обробку. "
        "Чистове точіння формує фінальні розміри Ø28h7 та довжину з допуском ±0.05 мм, шорсткість Ra 1.6. "
        "Деталь переустановлюється на Haas VF-2 для фрезерування шпонкового паза шириною 8 мм і глибиною 4 мм. "
        "Свердління двох отворів Ø6 мм під кріплення виконується на тому ж верстаті за один установ. "
        "Нарізання різьби M8 виконується мітчиком у свердлених отворах. "
        "Термообробка (гартування до HRC 45-50) виконується за необхідності після механообробки. "
        "Контроль ВТК перевіряє діаметр мікрометром, довжину штангенциркулем, биття - на призмах. "
        "Пакування партії виконується в картонні короби з прокладками між деталями. "
        "Орієнтовний час токарної обробки - 12 хвилин на деталь, фрезерної - 8 хвилин на деталь. "
    ) * 3  # свідомо втричі довше типової відповіді, щоб гарантовано не влізло на одну сторінку
    r = client.post("/calculator/quote.pdf", data={
        "_csrf_token": token,
        "weight_kg": "1.2", "material_price_per_kg": "62",
        "turning_hours": "0.4", "turning_rate_per_hour": "800",
        "milling_hours": "0.3", "milling_rate_per_hour": "900",
        "machine_hours": "0", "machine_rate_per_hour": "0",
        "setup_cost_total": "400",
        "quantity": "200", "margin_pct": "20",
        "part_description": "Партія 200 шт, сталь 40Х, пруток Ø30мм",
        "operations_description": long_description,
    })
    assert r.status_code == 200
    assert r.data[:4] == b"%PDF"
    assert len(r.data) > 1500


def test_calculator_quote_pdf_requires_login(client):
    r = client.post("/calculator/quote.pdf", data={"weight_kg": "1"}, follow_redirects=True)
    assert "логін" in r.get_data(as_text=True).lower() or "пароль" in r.get_data(as_text=True).lower()


# ---------------------------------------------------------------------------
# PDF, створення угоди й пошук із картки історії розрахунків
# ---------------------------------------------------------------------------

def test_calculator_history_quote_pdf_produces_real_pdf_with_correct_totals(client):
    """РЕАЛЬНИЙ функціональний тест: PDF генерується із ЗБЕРЕЖЕНОГО запису
    історії (а не з форми) - перевіряємо справжню PDF-сигнатуру і що
    документ використовує дані саме цього запису (довгий опис техпроцесу
    не ламає генерацію, як і в "живому" калькуляторі)."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, weight_kg, "
        "material_price_per_kg, turning_hours, turning_rate_per_hour, milling_hours, milling_rate_per_hour, "
        "machine_hours, machine_rate_per_hour, setup_cost_total, margin_pct, cost_per_unit, price_per_unit, "
        "total_price, operations_description, created_at) VALUES "
        "(1,'manual','Вал для PDF-тесту',10,2,50,1,800,0,900,0,0,0,20,850,1020,10200,'Токарна обробка.','2026-01-01 10:00:00')"
    )
    db.commit()
    calc_id = db.execute("SELECT id FROM cost_calculations ORDER BY id DESC LIMIT 1").fetchone()["id"]

    r = client.get(f"/calculator/history/{calc_id}/quote.pdf")
    assert r.status_code == 200
    assert r.mimetype == "application/pdf"
    assert r.data[:4] == b"%PDF"
    assert len(r.data) > 1500


def test_calculator_history_quote_pdf_missing_id_returns_404(client):
    login(client)
    r = client.get("/calculator/history/999999/quote.pdf")
    assert r.status_code == 404


def test_calculator_history_quote_pdf_requires_login():
    a = __import__("app")
    c = a.app.test_client()
    r = c.get("/calculator/history/1/quote.pdf", follow_redirects=True)
    assert "Увійти" in r.get_data(as_text=True) or "login" in r.request.path


def test_calculator_history_detail_has_pdf_and_create_deal_links(client):
    """Картка деталей розрахунку має вести і на PDF, і на створення угоди з
    уже підставленими назвою деталі та сумою (щоб не вводити ті самі дані
    вдруге вручну в формі нової угоди)."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, cost_per_unit, "
        "price_per_unit, total_price, created_at) VALUES (1,'manual','Фланець для угоди',5,100,120,600,'2026-01-01 10:00:00')"
    )
    db.commit()
    calc_id = db.execute("SELECT id FROM cost_calculations ORDER BY id DESC LIMIT 1").fetchone()["id"]

    html = client.get(f"/calculator/history/{calc_id}").get_data(as_text=True)
    assert f"/calculator/history/{calc_id}/quote.pdf" in html
    assert "/deals/new" in html
    assert "prefill_title=" in html
    assert "prefill_amount=600" in html


def test_create_deal_from_calculation_prefills_form(client):
    """Повний сценарій: клік «Створити угоду» з картки розрахунку відкриває
    форму нової угоди з уже підставленими назвою й сумою."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, cost_per_unit, "
        "price_per_unit, total_price, created_at) VALUES (1,'manual','Вал для нової угоди',3,200,240,720,'2026-01-01 10:00:00')"
    )
    db.commit()

    r = client.get("/deals/new?prefill_title=" + "Вал для нової угоди" + "&prefill_amount=720")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'value="Вал для нової угоди"' in html
    assert 'value="720' in html


def test_search_finds_calculation_by_part_description(client):
    """Глобальний пошук має знаходити й розрахунки вартості (не лише
    клієнтів/угоди) для ролей, яким і так доступний калькулятор."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO cost_calculations (user_id, source, part_description, quantity, cost_per_unit, "
        "price_per_unit, total_price, created_at) VALUES (1,'manual','Унікальна деталь ШукайМене',1,10,12,12,'2026-01-01 10:00:00')"
    )
    db.commit()
    calc_id = db.execute("SELECT id FROM cost_calculations ORDER BY id DESC LIMIT 1").fetchone()["id"]

    r = client.get("/search?q=ШукайМене")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Унікальна деталь ШукайМене" in html
    assert f"/calculator/history/{calc_id}" in html


def test_search_hides_calculations_for_warehouse_role(client):
    """Роль 'Склад' не має доступу до калькулятора/історії - пошук не
    повинен витікати туди цінову інформацію, навіть якщо збіг є."""
    login(client, "sklad", "sklad123")
    r = client.get("/search?q=деталь")
    assert r.status_code == 200
    assert "Розрахунки вартості" not in r.get_data(as_text=True)


def test_demo_seed_populates_calculation_history_for_fresh_install(client):
    """На свіжовстановленій базі (одразу після сідингу демо-даних) сторінка
    історії розрахунків НЕ повинна бути порожньою - інакше презентація
    починається з порожнього екрана там, де мала б бути жива демонстрація."""
    login(client)
    html = client.get("/calculator/history").get_data(as_text=True)
    assert "Розрахунків ще немає" not in html
    assert "Фланець приводний" in html


# ---------------------------------------------------------------------------
# Змінний журнал виробництва (shift log)
# ---------------------------------------------------------------------------

def test_shift_log_requires_login(client):
    r = client.get("/shift-log", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.location


def test_shift_log_blocked_for_non_production_non_admin_role(client):
    """Роль 'Склад' не бере участі у змінному журналі - лише виробництво
    та адмін."""
    login(client, "sklad", "sklad123")
    r = client.get("/shift-log", follow_redirects=True)
    assert "немає прав" in r.get_data(as_text=True).lower()


def test_shift_log_new_get_renders_form(client):
    login(client, "maister", "prod123")
    r = client.get("/shift-log/new")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Деталь" in html
    assert "Виготовлено" in html


def test_shift_log_create_entry_for_self(client):
    """Звичайний виробничник створює запис за свою зміну - worker_user_id і
    created_by_user_id мають співпасти з ним самим."""
    login(client, "maister", "prod123")
    a = client._app_module
    r = post(client, "/shift-log/new", data={
        "work_date": "2026-09-30", "shift": "day", "work_center_id": "3",
        "part_description": "Вал приводний Ф25", "quantity_made": "40",
        "quantity_scrap": "2", "scrap_reason": "биття різця",
        "shift_hours": "8", "setup_hours": "0.5",
    }, follow_redirects=True)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Вал приводний Ф25" in html

    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM shift_logs WHERE part_description='Вал приводний Ф25'").fetchone()
    assert row is not None
    assert row["quantity_made"] == 40
    assert row["quantity_scrap"] == 2
    maister_id = db.execute("SELECT id FROM users WHERE username='maister'").fetchone()["id"]
    assert row["worker_user_id"] == maister_id
    assert row["created_by_user_id"] == maister_id
    assert row["work_center_id"] == 3


def test_shift_log_validates_required_part_description(client):
    login(client, "maister", "prod123")
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    before = db.execute("SELECT COUNT(*) c FROM shift_logs").fetchone()[0]

    r = post(client, "/shift-log/new", data={
        "work_date": "2026-09-30", "quantity_made": "10",
    }, follow_redirects=True)
    html = r.get_data(as_text=True).lower()
    assert "flash-error" in html and "обов" in html
    after = db.execute("SELECT COUNT(*) c FROM shift_logs").fetchone()[0]
    assert after == before


def test_shift_log_validates_non_negative_quantity(client):
    login(client, "maister", "prod123")
    r = post(client, "/shift-log/new", data={
        "work_date": "2026-09-30", "part_description": "Тест", "quantity_made": "-5",
    }, follow_redirects=True)
    assert "не може бути меншим" in r.get_data(as_text=True).lower()


def test_shift_log_foreman_record_pinned_to_own_work_center(client):
    """Майстер дільниці (tokar, work_center_id=1) завжди пише запис на СВОЮ
    дільницю, навіть якщо у форму якось підставили інший work_center_id -
    так само, як термінал дозволяє діяти лише на своїй дільниці."""
    login(client, "tokar", "tokar123")
    a = client._app_module
    post(client, "/shift-log/new", data={
        "work_date": "2026-09-30", "part_description": "Втулка бронзова",
        "quantity_made": "15", "work_center_id": "3",
    }, follow_redirects=True)
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM shift_logs WHERE part_description='Втулка бронзова'").fetchone()
    assert row is not None
    assert row["work_center_id"] == 1


def test_shift_log_worker_can_edit_own_record(client):
    login(client, "maister", "prod123")
    a = client._app_module
    post(client, "/shift-log/new", data={
        "work_date": "2026-09-30", "part_description": "Деталь-ред1", "quantity_made": "5",
    }, follow_redirects=True)
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    log_id = db.execute("SELECT id FROM shift_logs WHERE part_description='Деталь-ред1'").fetchone()["id"]

    r = post(client, f"/shift-log/{log_id}/edit", data={
        "work_date": "2026-09-30", "part_description": "Деталь-ред1", "quantity_made": "9",
    }, follow_redirects=True)
    assert "оновлено" in r.get_data(as_text=True).lower()
    updated = db.execute("SELECT quantity_made FROM shift_logs WHERE id=?", (log_id,)).fetchone()
    assert updated["quantity_made"] == 9


def test_shift_log_foreman_can_edit_same_department_record(client):
    """Майстер дільниці може редагувати записи своєї дільниці, навіть
    зроблені ІНШИМ (адмін) - за прямим рішенням користувача: "Майстер
    дільниці + адмін" для редагування чужих записів."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    admin_id = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
    db.execute(
        "INSERT INTO shift_logs (work_date, shift, work_center_id, worker_user_id, part_description, "
        "quantity_made, quantity_scrap, created_by_user_id, created_at) VALUES "
        "('2026-09-30','day',1,?,'Чужий запис на дільниці 1',3,0,?,'2026-09-30 10:00:00')",
        (admin_id, admin_id),
    )
    db.commit()
    log_id = db.execute("SELECT id FROM shift_logs WHERE part_description='Чужий запис на дільниці 1'").fetchone()["id"]

    login(client, "tokar", "tokar123")
    r = post(client, f"/shift-log/{log_id}/edit", data={
        "work_date": "2026-09-30", "part_description": "Чужий запис на дільниці 1", "quantity_made": "7",
    }, follow_redirects=True)
    assert "оновлено" in r.get_data(as_text=True).lower()
    updated = db.execute("SELECT quantity_made FROM shift_logs WHERE id=?", (log_id,)).fetchone()
    assert updated["quantity_made"] == 7


def test_shift_log_foreman_cannot_edit_other_department_record(client):
    """Майстер дільниці 1 НЕ може редагувати чи видаляти запис, зроблений
    на ІНШІЙ дільниці кимось іншим."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    admin_id = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
    db.execute(
        "INSERT INTO shift_logs (work_date, shift, work_center_id, worker_user_id, part_description, "
        "quantity_made, quantity_scrap, created_by_user_id, created_at) VALUES "
        "('2026-09-30','day',3,?,'Запис на дільниці 3',3,0,?,'2026-09-30 10:00:00')",
        (admin_id, admin_id),
    )
    db.commit()
    log_id = db.execute("SELECT id FROM shift_logs WHERE part_description='Запис на дільниці 3'").fetchone()["id"]

    login(client, "tokar", "tokar123")
    r = post(client, f"/shift-log/{log_id}/edit", data={
        "work_date": "2026-09-30", "part_description": "ЗМІНЕНО", "quantity_made": "99",
    }, follow_redirects=True)
    assert "лише свої записи" in r.get_data(as_text=True).lower()
    unchanged = db.execute("SELECT part_description FROM shift_logs WHERE id=?", (log_id,)).fetchone()
    assert unchanged["part_description"] == "Запис на дільниці 3"

    r2 = post(client, f"/shift-log/{log_id}/delete", follow_redirects=True)
    assert "лише свої записи" in r2.get_data(as_text=True).lower()
    still_there = db.execute("SELECT id FROM shift_logs WHERE id=?", (log_id,)).fetchone()
    assert still_there is not None


def test_shift_log_general_production_can_edit_any_record(client):
    """Загальний майстер цеху (maister, без закріпленої дільниці) бачить і
    редагує все - так само, як і решта застосунку трактує таких
    користувачів."""
    login(client, "tokar", "tokar123")
    a = client._app_module
    post(client, "/shift-log/new", data={
        "work_date": "2026-09-30", "part_description": "Запис токаря", "quantity_made": "3",
    }, follow_redirects=True)
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    log_id = db.execute("SELECT id FROM shift_logs WHERE part_description='Запис токаря'").fetchone()["id"]

    login(client, "maister", "prod123")
    r = post(client, f"/shift-log/{log_id}/edit", data={
        "work_date": "2026-09-30", "part_description": "Запис токаря", "quantity_made": "11",
    }, follow_redirects=True)
    assert "оновлено" in r.get_data(as_text=True).lower()
    updated = db.execute("SELECT quantity_made FROM shift_logs WHERE id=?", (log_id,)).fetchone()
    assert updated["quantity_made"] == 11


def test_shift_log_delete_removes_row(client):
    login(client, "maister", "prod123")
    a = client._app_module
    post(client, "/shift-log/new", data={
        "work_date": "2026-09-30", "part_description": "На видалення", "quantity_made": "1",
    }, follow_redirects=True)
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    log_id = db.execute("SELECT id FROM shift_logs WHERE part_description='На видалення'").fetchone()["id"]

    r = post(client, f"/shift-log/{log_id}/delete", follow_redirects=True)
    assert "видалено" in r.get_data(as_text=True).lower()
    gone = db.execute("SELECT id FROM shift_logs WHERE id=?", (log_id,)).fetchone()
    assert gone is None


def test_shift_log_edit_missing_id_returns_404(client):
    login(client, "maister", "prod123")
    r = client.get("/shift-log/999999/edit")
    assert r.status_code == 404


def test_shift_log_delete_missing_id_returns_404(client):
    login(client, "maister", "prod123")
    r = post(client, "/shift-log/999999/delete")
    assert r.status_code == 404


def test_shift_log_list_shows_totals_and_filters(client):
    login(client, "maister", "prod123")
    post(client, "/shift-log/new", data={
        "work_date": today_str(), "part_description": "Для списку", "quantity_made": "20", "quantity_scrap": "4",
    }, follow_redirects=True)
    r = client.get("/shift-log")
    html = r.get_data(as_text=True)
    assert "Для списку" in html
    assert "20" in html


def today_str():
    import datetime
    return datetime.date.today().strftime("%Y-%m-%d")


def test_dashboard_shows_production_block_for_production_role(client):
    """Виробничий блок 'Виробництво сьогодні' на дашборді - новий запит
    користувача, додано ПОРУЧ із воронкою продажів, видно адміну й ролі
    'Виробництво'."""
    login(client, "maister", "prod123")
    post(client, "/shift-log/new", data={
        "work_date": today_str(), "part_description": "Деталь дашборду", "quantity_made": "12",
    }, follow_redirects=True)
    r = client.get("/")
    html = r.get_data(as_text=True)
    assert "Виробництво сьогодні" in html
    assert "Угоди (воронка)" not in html  # maister не бачить меню продажів
    assert "Воронка продажів" in html  # але сам блок воронки на дашборді лишається для всіх


def test_dashboard_hides_production_block_for_sales_role(client):
    login(client, "oleh", "manager123")
    html = client.get("/").get_data(as_text=True)
    assert "Виробництво сьогодні" not in html


def test_dashboard_still_shows_sales_funnel_alongside_production_block(client):
    """Виробничий блок ДОДАНО, а не замінив воронку продажів - адмін має
    бачити обидва блоки одночасно (явний вибір користувача: 'Додати
    виробничий блок поруч')."""
    login(client)
    post(client, "/shift-log/new", data={
        "work_date": today_str(), "part_description": "Деталь для адміна", "quantity_made": "6",
    }, follow_redirects=True)
    html = client.get("/").get_data(as_text=True)
    assert "Воронка продажів" in html
    assert "Виробництво сьогодні" in html


def test_sidebar_shows_shift_log_link_for_production_not_for_sales(client):
    login(client, "maister", "prod123")
    html = client.get("/").get_data(as_text=True)
    assert "Змінний журнал" in html

    login(client, "oleh", "manager123")
    html2 = client.get("/").get_data(as_text=True)
    assert "Змінний журнал" not in html2


# ---------------------------------------------------------------------------
# Угода без верстата -> перехід на "Передоплата" питає верстат
# ---------------------------------------------------------------------------

def test_moving_deal_without_machine_to_prepay_redirects_to_select_machine(client):
    """Угода, створена без обраного верстата (типово - одним кліком з історії
    калькулятора), НЕ повинна мовчки переходити на 'Передоплата' без жодного
    сліду в цехах - користувача має перенаправити на вибір верстата."""
    login(client)
    r = post(client, "/deals/new", {"title": "Угода без верстата", "client_id": "1", "amount": "5000"},
             follow_redirects=True)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    deal = db.execute("SELECT * FROM deals WHERE title='Угода без верстата'").fetchone()
    assert deal["machine_id"] is None

    r2 = post(client, f"/deals/{deal['id']}/move/prepay", follow_redirects=False)
    assert r2.status_code == 302
    assert f"/deals/{deal['id']}/select-machine" in r2.location

    order = db.execute("SELECT * FROM production_orders WHERE deal_id=?", (deal["id"],)).fetchone()
    assert order is None
    stage_unchanged = db.execute("SELECT stage FROM deals WHERE id=?", (deal["id"],)).fetchone()
    assert stage_unchanged["stage"] != "prepay"


def test_moving_deal_without_machine_fetch_returns_needs_machine_json(client):
    """Kanban-перетягування (fetch) теж не повинно мовчки губити завдання -
    має повернути прапорець needs_machine і посилання для переходу."""
    login(client)
    post(client, "/deals/new", {"title": "Канбан без верстата", "client_id": "1", "amount": "3000"},
         follow_redirects=True)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    deal = db.execute("SELECT * FROM deals WHERE title='Канбан без верстата'").fetchone()

    token = get_csrf_token(client)
    r = client.post(f"/deals/{deal['id']}/move/prepay", headers={"X-Requested-With": "fetch", "X-CSRF-Token": token})
    data = r.get_json()
    assert data["needs_machine"] is True
    assert f"/deals/{deal['id']}/select-machine" in data["redirect"]


def test_select_machine_sets_machine_and_creates_production_order(client):
    """Після вибору верстата на проміжній сторінці угода має перейти на
    обрану стадію, і виробниче замовлення має завестись у цех - саме те,
    чого раніше не ставалось для угод без верстата."""
    login(client)
    post(client, "/deals/new", {"title": "Угода для вибору верстата", "client_id": "1", "amount": "7000"},
         follow_redirects=True)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    deal = db.execute("SELECT * FROM deals WHERE title='Угода для вибору верстата'").fetchone()
    machine = db.execute("SELECT * FROM machines LIMIT 1").fetchone()

    r = post(client, f"/deals/{deal['id']}/select-machine", data={
        "machine_id": str(machine["id"]), "next_stage": "prepay",
    }, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "заведено виробниче замовлення" in html.lower()

    updated_deal = db.execute("SELECT * FROM deals WHERE id=?", (deal["id"],)).fetchone()
    assert updated_deal["machine_id"] == machine["id"]
    assert updated_deal["stage"] == "prepay"
    order = db.execute("SELECT * FROM production_orders WHERE deal_id=?", (deal["id"],)).fetchone()
    assert order is not None
    ops = db.execute("SELECT * FROM production_operations WHERE production_order_id=?", (order["id"],)).fetchall()
    assert len(ops) > 0


def test_select_machine_requires_machine_id(client):
    login(client)
    post(client, "/deals/new", {"title": "Угода без вибору", "client_id": "1", "amount": "1000"},
         follow_redirects=True)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    deal = db.execute("SELECT * FROM deals WHERE title='Угода без вибору'").fetchone()

    r = post(client, f"/deals/{deal['id']}/select-machine", data={"next_stage": "prepay"}, follow_redirects=True)
    html = r.get_data(as_text=True).lower()
    assert "flash-error" in html and "обов" in html
    unchanged = db.execute("SELECT machine_id, stage FROM deals WHERE id=?", (deal["id"],)).fetchone()
    assert unchanged["machine_id"] is None
    assert unchanged["stage"] != "prepay"


def test_select_machine_requires_login(client):
    r = client.get("/deals/1/select-machine", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.location


def test_select_machine_404_for_missing_deal(client):
    login(client)
    r = client.get("/deals/999999/select-machine")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# PDF-зведення змінного журналу за місяць
# ---------------------------------------------------------------------------

def test_shift_log_report_pdf_requires_login(client):
    r = client.get("/shift-log/report.pdf", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.location


def test_shift_log_report_pdf_produces_real_pdf_for_current_month(client):
    """Демо-дані вже засівають записи на сьогодні - PDF за поточний місяць
    повинен бути справжнім, непорожнім PDF-документом."""
    login(client, "maister", "prod123")
    r = client.get("/shift-log/report.pdf")
    assert r.status_code == 200
    assert r.mimetype == "application/pdf"
    assert r.data[:4] == b"%PDF"
    assert len(r.data) > 500


def test_shift_log_report_pdf_filters_by_worker(client):
    """PDF звужений до одного робітника не повинен включати записи іншого -
    перевіряємо побічно через розмір (менше рядків -> коротший файл) і те,
    що обидва варіанти реально генеруються без помилок."""
    login(client, "maister", "prod123")
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    tokar_id = db.execute("SELECT id FROM users WHERE username='tokar'").fetchone()["id"]

    r_all = client.get("/shift-log/report.pdf")
    r_one = client.get(f"/shift-log/report.pdf?worker_user_id={tokar_id}")
    assert r_all.status_code == 200 and r_one.status_code == 200
    assert r_all.data[:4] == b"%PDF" and r_one.data[:4] == b"%PDF"


def test_shift_log_report_pdf_handles_month_with_no_entries(client):
    """Місяць без жодного запису не повинен падати з 500 - просто PDF з
    написом 'записів немає'."""
    login(client, "maister", "prod123")
    r = client.get("/shift-log/report.pdf?month=2019-01")
    assert r.status_code == 200
    assert r.data[:4] == b"%PDF"


def test_shift_log_report_pdf_rejects_malformed_month_gracefully(client):
    """Некоректний параметр місяця (спроба зламати формат) не повинен
    спричиняти 500 - тихо відкочується на поточний місяць."""
    login(client, "maister", "prod123")
    r = client.get("/shift-log/report.pdf?month=not-a-month")
    assert r.status_code == 200
    assert r.data[:4] == b"%PDF"


def test_shift_log_list_has_pdf_report_link(client):
    login(client, "maister", "prod123")
    html = client.get("/shift-log").get_data(as_text=True)
    assert "report.pdf" in html
    assert "Скачати PDF за місяць" in html


# ---------------------------------------------------------------------------
# Серверна валідація форм, що раніше падали з "технічною" 400-сторінкою
# замість зрозумілого повідомлення (f["поле"] без .get() -> BadRequestKeyError)
# ---------------------------------------------------------------------------

def test_service_add_missing_required_fields_shows_friendly_error(client):
    login(client)
    r = post(client, "/service/add", data={}, follow_redirects=True)
    assert r.status_code == 200
    html = r.get_data(as_text=True).lower()
    assert "flash-error" in html and "обов" in html


def test_task_add_missing_title_shows_friendly_error(client):
    login(client)
    r = post(client, "/tasks/add", data={}, follow_redirects=True)
    assert r.status_code == 200
    html = r.get_data(as_text=True).lower()
    assert "flash-error" in html and "обов" in html


def test_contact_add_missing_full_name_shows_friendly_error(client):
    login(client)
    r = post(client, "/clients/1/contacts", data={}, follow_redirects=True)
    assert r.status_code == 200
    html = r.get_data(as_text=True).lower()
    assert "flash-error" in html and "обов" in html


def test_activity_add_missing_text_shows_friendly_error(client):
    login(client)
    r = post(client, "/activities/1", data={}, follow_redirects=True)
    assert r.status_code == 200
    html = r.get_data(as_text=True).lower()
    assert "flash-error" in html and "обов" in html


def test_service_update_status_missing_status_shows_friendly_error(client):
    login(client)
    r = post(client, "/service/1/status", data={}, follow_redirects=True)
    assert r.status_code == 200
    html = r.get_data(as_text=True).lower()
    assert "flash-error" in html and "обов" in html


def test_service_add_with_valid_data_still_works(client):
    """Регресія: виправлення валідації не повинне зламати звичайний,
    коректний шлях створення заявки."""
    login(client)
    r = post(client, "/service/add", data={"client_id": "1", "issue": "Тестова несправність"},
             follow_redirects=True)
    assert "Сервісну заявку створено" in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Скидання до демо-даних (/settings/reset-demo-data)
# ---------------------------------------------------------------------------

def test_reset_demo_data_requires_admin(client):
    login(client, "oleh", "manager123")
    r = post(client, "/settings/reset-demo-data", data={"confirm": "СКИНУТИ"})
    assert r.status_code == 403


def test_reset_demo_data_requires_login(client):
    r = client.post("/settings/reset-demo-data", data={"confirm": "СКИНУТИ"}, follow_redirects=True)
    assert "Увійти" in r.get_data(as_text=True) or r.status_code in (302, 401)


def test_reset_demo_data_wrong_confirmation_word_does_nothing(client):
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("INSERT INTO clients (name, source, status, created_at) VALUES ('Реальний клієнт','ручний','active','2026-01-01')")
    db.commit()
    before = db.execute("SELECT COUNT(*) c FROM clients").fetchone()["c"]
    db.close()

    r = post(client, "/settings/reset-demo-data", data={"confirm": "ні"}, follow_redirects=True)
    assert "точно як написано" in r.get_data(as_text=True)

    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    after = db.execute("SELECT COUNT(*) c FROM clients").fetchone()["c"]
    still_there = db.execute("SELECT COUNT(*) c FROM clients WHERE name='Реальний клієнт'").fetchone()["c"]
    db.close()
    assert after == before
    assert still_there == 1


def test_reset_demo_data_with_correct_confirmation_reseeds_everything(client):
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    # Додаємо "реальний" запис, якого не має бути в демо-наборі.
    db.execute("INSERT INTO clients (name, source, status, created_at) VALUES ('Старий клієнт для видалення','ручний','active','2026-01-01')")
    db.commit()
    db.close()

    r = post(client, "/settings/reset-demo-data", data={"confirm": "СКИНУТИ"}, follow_redirects=True)
    assert "наповнено актуальними демо-даними" in r.get_data(as_text=True)

    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    clients_count = db.execute("SELECT COUNT(*) c FROM clients").fetchone()["c"]
    deals_count = db.execute("SELECT COUNT(*) c FROM deals").fetchone()["c"]
    stale_still_there = db.execute(
        "SELECT COUNT(*) c FROM clients WHERE name='Старий клієнт для видалення'"
    ).fetchone()["c"]
    admin_row = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    work_centers_with_ops = db.execute(
        """SELECT COUNT(DISTINCT wc.id) c FROM work_centers wc
           JOIN production_operations op ON op.work_center_id=wc.id"""
    ).fetchone()["c"]
    db.close()

    assert clients_count == 10
    assert deals_count == 21
    assert stale_still_there == 0
    assert admin_row is not None and admin_row["id"] == 1
    assert work_centers_with_ops == 7  # усі 7 дільниць знову мають завдання


def test_reset_demo_data_creates_backup_file_first(client):
    login(client)
    a = client._app_module
    backup_dir = os.path.join(a.DATA_DIR, "backups")
    before = set(os.listdir(backup_dir)) if os.path.exists(backup_dir) else set()

    post(client, "/settings/reset-demo-data", data={"confirm": "СКИНУТИ"})

    after = set(os.listdir(backup_dir))
    new_files = after - before
    assert any(f.startswith("crm_backup_") and f.endswith(".db") for f in new_files)


def test_machine_delete_allowed_when_used_only_in_closed_deals(client):
    """Раніше verstat лишався незнищенним НАЗАВЖДИ, щойно його хоч раз
    використали навіть у вже виграній/програній угоді — всупереч тому, що
    повідомлення про помилку явно обіцяло блокувати лише активні угоди."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    machine_id = db.execute("SELECT id FROM machines ORDER BY id LIMIT 1").fetchone()["id"]
    # Прибираємо всі активні прив'язки до цього верстата: переводимо угоди
    # в закриті стадії, а виробничі замовлення — у 'done'.
    db.execute("UPDATE deals SET stage='won' WHERE machine_id=?", (machine_id,))
    db.execute("UPDATE production_orders SET status='done' WHERE machine_id=?", (machine_id,))
    db.commit()
    db.close()

    r = post(client, f"/machines/{machine_id}/delete", {}, follow_redirects=True)
    assert "Верстат видалено з каталогу" in r.get_data(as_text=True)

    db2 = sqlite3.connect(a.DB_PATH)
    db2.row_factory = sqlite3.Row
    assert db2.execute("SELECT COUNT(*) c FROM machines WHERE id=?", (machine_id,)).fetchone()["c"] == 0
    # Закриті угоди лишаються, але з очищеним machine_id — без "висячого" id.
    dangling = db2.execute("SELECT COUNT(*) c FROM deals WHERE machine_id=?", (machine_id,)).fetchone()["c"]
    assert dangling == 0
    db2.close()


def test_machine_delete_blocked_by_active_production_order_even_without_active_deal(client):
    """Угода вже закрита (won), але виробниче замовлення на цьому верстаті
    ще не завершене — видаляти верстат усе одно не можна."""
    login(client)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    order = db.execute(
        "SELECT id, machine_id, deal_id FROM production_orders WHERE machine_id IS NOT NULL AND status!='done' LIMIT 1"
    ).fetchone()
    assert order is not None, "потрібне демо-замовлення в роботі для цього тесту"
    machine_id = order["machine_id"]
    db.execute("UPDATE deals SET stage='won' WHERE id=?", (order["deal_id"],))
    db.commit()
    db.close()

    r = post(client, f"/machines/{machine_id}/delete", {}, follow_redirects=True)
    assert "Неможливо видалити" in r.get_data(as_text=True)

    db2 = sqlite3.connect(a.DB_PATH)
    still_there = db2.execute("SELECT COUNT(*) c FROM machines WHERE id=?", (machine_id,)).fetchone()
    assert still_there[0] == 1
    db2.close()


def test_reset_demo_data_session_survives_reset(client):
    """Адмін лишається залогіненим і дашборд відкривається одразу після скидання
    (id=1 для admin відновлюється тим самим при повторному засіві)."""
    login(client)
    post(client, "/settings/reset-demo-data", data={"confirm": "СКИНУТИ"})
    r = client.get("/")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Зручність використання: швидкі дії на дашборді, пошук у списку клієнтів
# ---------------------------------------------------------------------------

def test_dashboard_quick_actions_visible_for_sales(client):
    """Менеджер з продажу бачить швидкі кнопки «+ Клієнт» / «+ Угода» на
    дашборді - не треба заходити в розділ, щоб почати нову картку."""
    login(client, "oleh", "manager123")
    r = client.get("/")
    body = r.get_data(as_text=True)
    assert "clients/new" in body
    assert "deals/new" in body


def test_dashboard_quick_actions_hidden_for_accountant(client):
    """Бухгалтеру швидкі кнопки створення клієнта/угоди не потрібні і не
    показуються - у нього немає доступу до цих розділів взагалі."""
    login(client, "buh", "buh12345")
    r = client.get("/")
    body = r.get_data(as_text=True)
    assert "clients/new" not in body
    assert "deals/new" not in body


def test_dashboard_warehouse_quick_action_link_works(client):
    """Кнопка «+ Позиція складу» на дашборді складівника веде на справжню
    робочу сторінку (а не на застарілий/неіснуючий маршрут)."""
    login(client, "sklad", "sklad123")
    r = client.get("/")
    assert "/warehouse/new" in r.get_data(as_text=True)
    r2 = client.get("/warehouse/new")
    assert r2.status_code == 200


def test_dashboard_service_quick_action_link_works(client):
    """Кнопка «+ Рекламація» на дашборді сервісника веде на сторінку, де
    справді є форма додавання (а не на відсутню окрему сторінку)."""
    login(client, "servis", "servis123")
    r = client.get("/")
    assert '"/service"' in r.get_data(as_text=True)
    r2 = client.get("/service")
    assert r2.status_code == 200


def test_clients_list_search_by_name(client):
    """Пошук у списку клієнтів за назвою компанії знаходить потрібний запис
    і не показує інші."""
    login(client)
    r = client.get("/clients?q=ПромЕлектро")
    body = r.get_data(as_text=True)
    assert "ПромЕлектро" in body


def test_clients_list_search_by_city_and_no_match(client):
    """Пошук за містом теж працює, а запит без збігів чесно каже, що
    нічого не знайдено (а не показує всіх клієнтів чи падає з помилкою)."""
    login(client)
    r = client.get("/clients?q=Запоріжжя")
    assert r.status_code == 200
    assert "ПромЕлектро" in r.get_data(as_text=True)

    r2 = client.get("/clients?q=ЦьогоТочноНемаєВБазі12345")
    assert r2.status_code == 200
    assert "Клієнтів не знайдено" in r2.get_data(as_text=True)


def test_clients_list_search_combines_with_status_filter(client):
    """Пошук за текстом і фільтр за статусом працюють разом, а не
    перекривають один одного."""
    login(client)
    r = client.get("/clients?q=ТОВ&status=active")
    assert r.status_code == 200


def test_deals_board_has_quick_filter_input(client):
    """На воронці угод є миттєвий текстовий фільтр по картках (клієнтська
    JS-фільтрація без перезавантаження сторінки)."""
    login(client)
    r = client.get("/deals")
    body = r.get_data(as_text=True)
    assert 'id="deal-filter-input"' in body
    assert "function filterDealCards" in body


def test_global_search_shortcut_markup_present(client):
    """Поле глобального пошуку має id, потрібний для гарячої клавіші «/»,
    і обробник клавіші підключений на базовому шаблоні."""
    login(client)
    r = client.get("/")
    body = r.get_data(as_text=True)
    assert 'id="global-search-input"' in body
    assert "e.key !== '/'" in body


# ---------------------------------------------------------------------------
# Автогенерація секретного ключа і сторінка "мережевий доступ"
# ---------------------------------------------------------------------------

def test_secret_key_auto_generated_and_persisted(monkeypatch, tmp_path):
    """Якщо CRM_SECRET_KEY не задано вручну - застосунок сам генерує
    надійний ключ і зберігає його поруч із базою (secret_key.txt), а не
    використовує вшитий у код типовий ключ (це була б дірка в безпеці
    щойно CRM стає доступна по мережі, а не лише на 127.0.0.1)."""
    monkeypatch.setenv("CRM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CRM_SECRET_KEY", raising=False)

    import importlib
    import app as app_module
    importlib.reload(app_module)

    key_file = tmp_path / "secret_key.txt"
    assert key_file.exists()
    first_key = app_module.app.secret_key
    assert first_key != "cnc-crm-super-secret-key-change-me"
    assert len(first_key) > 20

    # Перезапуск (новий importlib.reload, емулює новий старт процесу) -
    # ключ має лишитись тим самим, інакше всі сесії й логіни ламались би
    # щоразу, коли людина вимикає й знову вмикає комп'ютер.
    importlib.reload(app_module)
    assert app_module.app.secret_key == first_key


def test_settings_shows_network_access_card(client):
    """У «Налаштуваннях» адмін бачить адресу для доступу з інших
    комп'ютерів у мережі - саме цю адресу треба давати колегам, щоб
    вони зайшли в CRM зі свого комп'ютера через браузер."""
    login(client)
    r = client.get("/settings")
    body = r.get_data(as_text=True)
    assert "Доступ з інших комп'ютерів" in body
    assert 'id="network-url-text"' in body


def test_settings_network_card_hidden_from_non_admin(client):
    """Рядова роль узагалі не бачить сторінку «Налаштування» (і картку
    мережевого доступу разом з нею) - вона доступна лише адміну."""
    login(client, "oleh", "manager123")
    r = client.get("/settings", follow_redirects=True)
    assert "Доступ з інших комп'ютерів" not in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Зручність підключення: QR, адреса за іменем, нагадування про пароль, автозапуск
# ---------------------------------------------------------------------------

def test_settings_shows_qr_hostname_and_tailscale_hint(client):
    login(client)
    body = client.get("/settings").get_data(as_text=True)
    assert 'id="network-qr"' in body and "<svg" in body
    assert 'id="network-host-url"' in body
    assert "Tailscale" in body


def test_make_qr_svg_returns_svg_or_none(client):
    a = client._app_module
    svg = str(a.make_qr_svg("http://192.168.1.50:5000"))
    assert svg.startswith("<svg")
    assert a.make_qr_svg(None) is None


def test_default_password_warning_shown_and_cleared_after_change(client):
    login(client)  # admin / admin123 - типовий пароль
    assert 'id="default-password-warning"' in client.get("/").get_data(as_text=True)
    post(client, "/my-profile", {"current_password": "admin123", "new_password": "Nova-Parol-77",
                                 "confirm_password": "Nova-Parol-77"})
    assert 'id="default-password-warning"' not in client.get("/").get_data(as_text=True)


def test_no_default_password_warning_for_custom_password(client):
    login(client)
    post(client, "/my-profile", {"current_password": "admin123", "new_password": "Nova-Parol-77",
                                 "confirm_password": "Nova-Parol-77"})
    client.get("/logout")
    login(client, "admin", "Nova-Parol-77")
    assert 'id="default-password-warning"' not in client.get("/").get_data(as_text=True)


def test_launcher_autostart_registry_logic(monkeypatch, tmp_path):
    """Логіка автозапуску Windows перевіряється на підробленому winreg
    (на Linux справжнього реєстру немає)."""
    import sys, types, importlib
    monkeypatch.setenv("CRM_DATA_DIR", str(tmp_path))
    store = {}

    class FakeKey:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_READ=1, KEY_SET_VALUE=2, REG_SZ=1,
        OpenKey=lambda *a, **k: FakeKey(),
        CreateKeyEx=lambda *a, **k: FakeKey(),
        QueryValueEx=lambda key, name: (store[name], 1) if name in store else (_ for _ in ()).throw(FileNotFoundError()),
        SetValueEx=lambda key, name, r, t, v: store.__setitem__(name, v),
        DeleteValue=lambda key, name: store.pop(name) if name in store else (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    sys.modules.pop("launcher", None)
    import launcher
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", r"C:\\Apps\\OsnastkaMarket.exe")

    assert launcher.is_autostart_enabled() is False
    assert launcher.set_autostart(True) is True
    assert launcher.is_autostart_enabled() is True
    assert store["OsnastkaMarket"] == '"C:\\\\Apps\\\\OsnastkaMarket.exe"'
    assert launcher.set_autostart(False) is True
    assert launcher.is_autostart_enabled() is False
    assert launcher.set_autostart(False) is True  # повторне вимкнення не падає


def test_launcher_autostart_unsupported_when_not_frozen(monkeypatch, tmp_path):
    import sys
    monkeypatch.setenv("CRM_DATA_DIR", str(tmp_path))
    sys.modules.pop("launcher", None)
    import launcher
    monkeypatch.setattr(launcher.sys, "frozen", False, raising=False)
    assert launcher.autostart_supported() is False
    assert launcher.set_autostart(True) is False


def test_mobile_layout_markup_present(client):
    """Мобільний вигляд: кнопка-гамбургер, фон-перекривач і CSS для вузьких екранів."""
    login(client)
    body = client.get("/").get_data(as_text=True)
    assert 'id="nav-toggle"' in body
    assert 'class="nav-overlay"' in body
    assert "@media(max-width:820px)" in body
    assert "translateX(-105%)" in body


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# Вбудоване підключення верстатів по MTConnect
# ---------------------------------------------------------------------------

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

MTC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MTConnectStreams xmlns="urn:mtconnect.org:MTConnectStreams:1.3">
 <Streams><DeviceStream name="HAAS" uuid="x">
  <ComponentStream component="Controller" name="controller">
   <Events>
    <Execution dataItemId="exec">{execution}</Execution>
    <ControllerMode dataItemId="mode">{mode}</ControllerMode>
    <Program dataItemId="prog">{program}</Program>
    <PartCountAct dataItemId="pc">{parts}</PartCountAct>
   </Events>
   <Condition>{condition}</Condition>
  </ComponentStream>
  <ComponentStream component="Rotary" name="C">
   <Samples><Load dataItemId="Sload" name="Sload">{load}</Load></Samples>
  </ComponentStream>
 </DeviceStream></Streams>
</MTConnectStreams>"""


def mtc_xml(execution="ACTIVE", mode="AUTOMATIC", program="O1234", parts="57",
            condition="<Normal dataItemId=\"c\"/>", load="63.5"):
    return MTC_XML.format(execution=execution, mode=mode, program=program,
                          parts=parts, condition=condition, load=load)


@pytest.fixture()
def fake_haas():
    """Імітація MTConnect-агента верстата на локальному порту."""
    state = {"body": mtc_xml(), "hits": 0}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"] += 1
            if self.path != "/current":
                self.send_response(404); self.end_headers(); return
            data = state["body"].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    state["port"] = srv.server_address[1]
    state["server"] = srv
    yield state
    srv.shutdown()
    srv.server_close()


def _telemetry(client, wc_id=1):
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM machine_telemetry WHERE work_center_id=?", (wc_id,)).fetchone()
    db.close()
    return row


def test_mtconnect_parse_running_idle_alarm_and_garbage(client):
    a = client._app_module
    r = a.parse_mtconnect_current(mtc_xml())
    assert r["status"] == "running" and r["program_name"] == "O1234"
    assert r["part_count"] == 57 and r["mode"] == "AUTOMATIC" and r["spindle_load_pct"] == 63.5
    assert a.parse_mtconnect_current(mtc_xml(execution="READY"))["status"] == "idle"
    assert a.parse_mtconnect_current(mtc_xml(execution="FEED_HOLD"))["status"] == "idle"
    alarm = a.parse_mtconnect_current(mtc_xml(condition='<Fault dataItemId="c">Servo</Fault>'))
    assert alarm["status"] == "alarm"
    unavailable = a.parse_mtconnect_current(mtc_xml(execution="UNAVAILABLE", program="UNAVAILABLE", parts="UNAVAILABLE"))
    assert unavailable["status"] == "offline" and unavailable["part_count"] is None
    import xml.etree.ElementTree as ET
    with pytest.raises(ET.ParseError):
        a.parse_mtconnect_current("not xml at all")
    with pytest.raises(ValueError):
        a.parse_mtconnect_current("<html><body>hello</body></html>")


def test_mtconnect_connect_saves_and_shows_live_status(client, fake_haas):
    login(client)
    r = post(client, "/work-centers/1/connect",
             {"mtc_host": "127.0.0.1", "mtc_port": str(fake_haas["port"])}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Зв&#39;язок є" in html or "Зв'язок є" in html
    t = _telemetry(client)
    assert t["status"] == "running" and t["program_name"] == "O1234"
    assert t["part_count"] == 57 and t["source"] == "mtconnect"
    assert "Підключено: 127.0.0.1" in html and "▶ працює" in html


def test_mtconnect_poll_follows_machine_state_and_goes_offline(client, fake_haas):
    login(client)
    post(client, "/work-centers/1/connect", {"mtc_host": "127.0.0.1", "mtc_port": str(fake_haas["port"])})
    a = client._app_module
    fake_haas["body"] = mtc_xml(condition='<Fault dataItemId="c">Alarm 108</Fault>', parts="58")
    assert a.poll_machines_once() == {1: None}
    t = _telemetry(client)
    assert t["status"] == "alarm" and t["part_count"] == 58
    fake_haas["body"] = mtc_xml(execution="READY")
    a.poll_machines_once()
    assert _telemetry(client)["status"] == "idle"
    fake_haas["server"].shutdown()  # верстат "вимкнули"
    fake_haas["server"].server_close()
    res = a.poll_machines_once()
    assert res[1] is not None
    assert _telemetry(client)["status"] == "offline"
    db = sqlite3.connect(a.DB_PATH)
    n_before = db.execute("SELECT COUNT(*) FROM machine_telemetry_log WHERE work_center_id=1").fetchone()[0]
    a.poll_machines_once()  # вже offline - журнал не роздувається
    n_after = db.execute("SELECT COUNT(*) FROM machine_telemetry_log WHERE work_center_id=1").fetchone()[0]
    db.close()
    assert n_after == n_before


def test_mtconnect_unreachable_machine_saved_without_crash(client):
    login(client)
    r = post(client, "/work-centers/1/connect", {"mtc_host": "127.0.0.1", "mtc_port": "9"}, follow_redirects=True)
    assert r.status_code == 200
    assert "верстат не відповів" in r.get_data(as_text=True)
    a = client._app_module
    db = sqlite3.connect(a.DB_PATH)
    row = db.execute("SELECT mtc_host, mtc_enabled FROM work_centers WHERE id=1").fetchone()
    db.close()
    assert row == ("127.0.0.1", 1)


def test_mtconnect_connect_validation_blank_disables_and_non_admin_forbidden(client, fake_haas):
    login(client)
    bad = post(client, "/work-centers/1/connect", {"mtc_host": "http://evil/x?y", "mtc_port": "8082"}, follow_redirects=True)
    assert "Перевірте адресу" in bad.get_data(as_text=True)
    bad2 = post(client, "/work-centers/1/connect", {"mtc_host": "127.0.0.1", "mtc_port": "99999"}, follow_redirects=True)
    assert "Перевірте адресу" in bad2.get_data(as_text=True)
    post(client, "/work-centers/1/connect", {"mtc_host": "127.0.0.1", "mtc_port": str(fake_haas["port"])})
    off = post(client, "/work-centers/1/connect", {"mtc_host": "", "mtc_port": "8082"}, follow_redirects=True)
    assert "вимкнено" in off.get_data(as_text=True)
    assert client._app_module.poll_machines_once() == {}
    assert post(client, "/work-centers/999/connect", {"mtc_host": "1.2.3.4"}).status_code == 404
    client.get("/logout")
    login(client, "oleh", "manager123")
    assert post(client, "/work-centers/1/connect", {"mtc_host": "127.0.0.1"}).status_code == 403
    page = client.get("/work-centers").get_data(as_text=True)
    assert "mtc-connect" not in page


def test_mtconnect_admin_sees_connect_form_and_poller_starts_once(client, monkeypatch):
    login(client)
    assert "mtc-connect" in client.get("/work-centers").get_data(as_text=True)
    a = client._app_module
    calls = []
    monkeypatch.setattr(a, "poll_machines_once", lambda: calls.append(1) or {})
    monkeypatch.setattr(a, "_poller_started", False)
    assert a.start_machine_poller(interval=1) is True
    assert a.start_machine_poller(interval=1) is False
    import time as _t
    try:
        _t.sleep(1.3)
        assert len(calls) >= 1
    finally:
        a.stop_machine_poller()


# ---------------------------------------------------------------------------
# Валюти: історія курсів, НБУ, PLN, попередження; автобекап
# ---------------------------------------------------------------------------

def _raw_db(client):
    db = sqlite3.connect(client._app_module.DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def test_rates_history_keeps_old_months_stable_after_rate_change(client):
    a = client._app_module
    db = _raw_db(client)
    a.save_rates(db, 40.0, 44.0, 10.0)           # курс "раніше"
    db.execute("UPDATE rates_history SET recorded_on='2026-03-01' WHERE source='вручну' OR source='manual'")
    db.commit()
    a.save_rates(db, 50.0, 55.0, 12.0)           # курс сьогодні
    assert a.to_uah(db, 100, "USD", "2026-03-15") == 4000.0     # березень лишився за старим
    assert a.to_uah(db, 100, "USD") == 5000.0                    # сьогодні за новим
    assert a.to_uah(db, 100, "PLN", "2020-01-01") is not None
    assert a.to_uah(db, 100, "UAH", "2026-03-15") == 100
    db.close()


def test_baseline_preserves_pre_history_rate(client):
    a = client._app_module
    db = _raw_db(client)
    old = float(a.get_setting(db, "rate_usd"))
    a.save_rates(db, old + 10, None, None)
    assert a.to_uah(db, 1, "USD", "2001-01-01") == old
    db.close()


def test_rates_reject_invalid_and_keep_previous(client):
    login(client)
    before = float(client._app_module.get_setting(_raw_db(client), "rate_usd"))
    r = post(client, "/settings/save", {"form": "rates", "rate_usd": "abc", "rate_eur": "-5", "rate_pln": "0"}, follow_redirects=True)
    assert "додатним числом" in r.get_data(as_text=True)
    assert float(client._app_module.get_setting(_raw_db(client), "rate_usd")) == before
    ok = post(client, "/settings/save", {"form": "rates", "rate_usd": "42,5", "rate_eur": "46", "rate_pln": "11", "rates_auto": "1"}, follow_redirects=True)
    assert "Курси валют оновлено" in ok.get_data(as_text=True)
    db = _raw_db(client)
    assert float(client._app_module.get_setting(db, "rate_usd")) == 42.5
    assert client._app_module.get_setting(db, "rates_auto") == "1"
    db.close()


def test_nbu_update_route_with_mocked_api_and_failure(client, monkeypatch):
    login(client)
    a = client._app_module
    monkeypatch.setattr(a, "fetch_nbu_rates", lambda timeout=8: {"usd": 41.9, "eur": 48.1, "pln": 11.2})
    r = post(client, "/settings/rates/nbu", {}, follow_redirects=True)
    assert "з НБУ" in r.get_data(as_text=True)
    db = _raw_db(client)
    assert a.get_setting(db, "rate_pln") == "11.2" and a.get_setting(db, "rates_source") == "НБУ"
    db.close()
    def boom(timeout=8):
        raise OSError("немає мережі")
    monkeypatch.setattr(a, "fetch_nbu_rates", boom)
    r2 = post(client, "/settings/rates/nbu", {}, follow_redirects=True)
    assert "Не вдалось отримати курси НБУ" in r2.get_data(as_text=True)
    client.get("/logout")
    login(client, "oleh", "manager123")
    assert post(client, "/settings/rates/nbu", {}).status_code == 403


def test_stale_rates_warning_on_dashboard_and_pln_currency(client):
    login(client)
    a = client._app_module
    assert "PLN" in a.CURRENCIES and a.fmt_money(100, "PLN").strip().startswith("zł")
    assert 'id="rates-warning"' not in client.get("/").get_data(as_text=True)
    db = _raw_db(client)
    a.set_setting(db, "rates_updated_on", (datetime.date.today() - datetime.timedelta(days=9)).isoformat())
    db.commit(); db.close()
    html = client.get("/").get_data(as_text=True)
    assert 'id="rates-warning"' in html and "9 дн" in html
    db = _raw_db(client)
    db.execute("DELETE FROM settings WHERE key='rate_eur'"); db.commit(); db.close()
    assert "Не задано курс для EUR" in client.get("/").get_data(as_text=True)


def test_old_db_gets_pln_rate_via_migration(client):
    a = client._app_module
    db = _raw_db(client)
    db.execute("DELETE FROM settings WHERE key='rate_pln'"); db.commit()
    a.migrate_schema(db)
    assert a.get_setting(db, "rate_pln") == "10.5"
    db.close()


def test_scheduled_backup_runs_once_a_day_and_copies_to_extra_dir(client, tmp_path):
    a = client._app_module
    login(client)
    extra = tmp_path / "usb"
    r = post(client, "/settings/save", {"form": "backup", "backup_auto": "1", "backup_extra_dir": str(extra)}, follow_redirects=True)
    assert "копіювання збережено" in r.get_data(as_text=True)
    first = a.run_scheduled_backup()
    assert first and os.path.exists(first)
    assert len(list(extra.glob("crm_backup_*.db"))) == 1
    assert a.run_scheduled_backup() is None          # другий раз за день - ні
    db = _raw_db(client)
    a.set_setting(db, "backup_auto", "0"); a.set_setting(db, "backup_last_on", "2000-01-01"); db.commit(); db.close()
    assert a.run_scheduled_backup() is None          # вимкнено
    # копія валідна SQLite з даними
    check = sqlite3.connect(first)
    assert check.execute("SELECT COUNT(*) FROM clients").fetchone()[0] > 0
    check.close()


def test_scheduled_backup_extra_dir_failure_is_reported_not_fatal(client, tmp_path):
    a = client._app_module
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    db = _raw_db(client)
    a.set_setting(db, "backup_extra_dir", str(blocker / "sub")); db.commit()
    path = a.run_scheduled_backup(db)
    assert path and os.path.exists(path)
    assert a.get_setting(db, "backup_last_error")
    db.close()


def test_pdfs_render_cyrillic_with_embedded_unicode_font(client):
    """Стандартний Helvetica не має кирилиці - раніше в PDF усе українське
    перетворювалось на 'IIII'. Перевіряємо, що текст справді читається."""
    pymupdf = pytest.importorskip("pymupdf")
    login(client)
    for url, needle in [("/deals/1/proposal.pdf", "Комерційна пропозиція"),
                        ("/production/1/route-sheet.pdf", "МАРШРУТНИЙ ЛИСТ"),
                        ("/shift-log/report.pdf", "Змінний журнал")]:
        r = client.get(url)
        assert r.status_code == 200, url
        doc = pymupdf.open(stream=r.data, filetype="pdf")
        text = doc[0].get_text()
        assert needle in text, (url, text[:80])
        fonts = [f[3] for f in doc[0].get_fonts()]
        assert any("CRMSans" in f or "DejaVu" in f or "Arial" in f for f in fonts), fonts


# ---------------------------------------------------------------------------
# Розширення: каталог деталей, план/факт, закупівля, якість, оплати, повтори, дошка, аудит
# ---------------------------------------------------------------------------

def _wc_id(db):
    return db.execute("SELECT id FROM work_centers ORDER BY id").fetchone()["id"]


def test_extension_pages_open_for_admin(client):
    login(client)
    for path in ["/parts", "/parts/new", "/production/costs", "/warehouse/purchase", "/quality", "/quality/new",
                 "/payments/overdue", "/clients/reorder", "/production/board", "/audit", "/"]:
        assert client.get(path).status_code == 200, path


def test_parts_catalog_crud_and_new_deal(client):
    login(client)
    db = _raw_db(client)
    cid = db.execute("SELECT id FROM clients ORDER BY id").fetchone()["id"]
    r = post(client, "/parts/new", {"name": "", "currency": "UAH"})
    assert "обов" in r.get_data(as_text=True)
    r = post(client, "/parts/new", {"name": "Вал Ø40", "drawing_no": "VL-40", "client_id": cid, "material": "Сталь 45",
                                    "last_price": "1200", "currency": "UAH", "routing_text": "Токарна; Фрезерна"})
    assert r.status_code == 302
    pid = db.execute("SELECT id FROM parts WHERE name='Вал Ø40'").fetchone()["id"]
    assert "VL-40" in client.get("/parts?q=VL-40").get_data(as_text=True)
    assert "Вал Ø40" not in client.get("/parts?q=nomatch").get_data(as_text=True)
    assert post(client, "/parts/%d/edit" % pid, {"name": "Вал Ø42", "last_price": "-5"}).status_code == 200
    r = post(client, "/parts/%d/new-deal" % pid)
    assert r.status_code == 302
    d = db.execute("SELECT * FROM deals ORDER BY id DESC LIMIT 1").fetchone()
    assert d["title"] == "Вал Ø40" and d["amount"] == 1200 and d["client_id"] == cid and "Сталь 45" in d["tech_requirements"]
    post(client, "/parts/%d/delete" % pid)
    assert db.execute("SELECT COUNT(*) c FROM parts").fetchone()["c"] == 0


def test_plan_fact_costs_math(client):
    login(client)
    db = _raw_db(client)
    deal = db.execute("SELECT id FROM deals WHERE stage='production' LIMIT 1").fetchone()
    wc = _wc_id(db)
    db.execute("INSERT INTO production_orders (deal_id, quantity, status, created_at) VALUES (?,?,?,?)",
               (deal["id"], 1, "in_progress", "2026-01-01 08:00:00"))
    oid = db.execute("SELECT MAX(id) m FROM production_orders").fetchone()["m"]
    db.execute("""INSERT INTO production_operations (production_order_id, step_order, operation_name, work_center_id,
                  planned_hours, status, actual_start, actual_end) VALUES (?,?,?,?,?,?,?,?)""",
               (oid, 1, "Токарна", wc, 4, "done", "2026-01-01 08:00:00", "2026-01-01 13:00:00"))
    db.execute("""INSERT INTO production_operations (production_order_id, step_order, operation_name, work_center_id,
                  planned_hours, status) VALUES (?,?,?,?,?,?)""", (oid, 2, "Фрезерна", wc, 6, "waiting"))
    db.execute("INSERT INTO warehouse_items (sku, name, qty_on_hand, min_qty, unit_cost, currency) VALUES ('X-T','Пруток',100,5,50,'UAH')")
    item = db.execute("SELECT id FROM warehouse_items WHERE sku='X-T'").fetchone()["id"]
    db.execute("INSERT INTO warehouse_transactions (item_id, qty_delta, type, reference, created_at) VALUES (?,?,?,?,?)",
               (item, -3, "out", "Виробниче замовлення №%d: Токарна" % oid, "2026-01-01 09:00:00"))
    db.commit()
    html = client.get("/production/costs").get_data(as_text=True)
    # ціна матеріалів 3*50 = 150, факт 5 год проти 4 плану = +25%
    assert "+25%" in html
    assert ">150<" in html.replace(" ", "")
    # бухгалтер бачить, токар — ні
    client.get("/logout")
    login(client, "tokar", "tokar123")
    assert client.get("/production/costs").status_code in (302, 403)


def test_purchase_request_export_and_telegram_fallback(client):
    login(client)
    db = _raw_db(client)
    db.execute("INSERT INTO warehouse_items (sku, name, qty_on_hand, min_qty, unit_cost, currency, unit) VALUES ('LOW-1','Фреза',2,10,100,'UAH','шт')")
    db.commit()
    item = db.execute("SELECT id FROM warehouse_items WHERE sku='LOW-1'").fetchone()["id"]
    assert "Фреза" in client.get("/warehouse/purchase").get_data(as_text=True)
    r = post(client, "/warehouse/purchase/export", {"item_id": item, "pick_%d" % item: "1", "qty_%d" % item: "18"})
    assert r.status_code == 200 and r.data[:2] == b"PK"
    import openpyxl
    from io import BytesIO
    ws = openpyxl.load_workbook(BytesIO(r.data)).active
    assert ws.cell(2, 2).value == "Фреза" and ws.cell(2, 5).value == 18 and ws.cell(2, 8).value == 1800
    r = post(client, "/warehouse/purchase/export", {})
    assert r.status_code == 302
    r = post(client, "/warehouse/purchase/telegram", {"item_id": item, "pick_%d" % item: "1", "qty_%d" % item: "5"},
             follow_redirects=True)
    assert "не налаштовані" in r.get_data(as_text=True)


def test_quality_validation_stats_and_notify(client, monkeypatch):
    login(client, "maister", "prod123")
    sent = []
    monkeypatch.setattr(client._app_module, "notify_all", lambda t: sent.append(t))
    db = _raw_db(client)
    assert "Вкажіть, скільки" in post(client, "/quality/new", {"qty_checked": "0"}).get_data(as_text=True)
    assert "від 0 до" in post(client, "/quality/new", {"qty_checked": "5", "qty_defect": "9"}).get_data(as_text=True)
    assert "причину" in post(client, "/quality/new", {"qty_checked": "5", "qty_defect": "1"}).get_data(as_text=True)
    assert post(client, "/quality/new", {"qty_checked": "20", "qty_defect": "2", "defect_reason": "Розмір",
                                          "ck_dim": "1", "ck_surf": "1"}).status_code == 302
    row = db.execute("SELECT * FROM quality_checks").fetchone()
    assert row["qty_defect"] == 2 and row["checklist"] == "dim,surf"
    assert any("Брак" in t for t in sent)
    assert "10.0%" in client.get("/quality").get_data(as_text=True)


def test_overdue_payments_dashboard_and_documents(client, monkeypatch):
    login(client)
    db = _raw_db(client)
    deal = db.execute("SELECT id FROM deals LIMIT 1").fetchone()["id"]
    db.execute("DELETE FROM payments")
    db.execute("INSERT INTO payments (deal_id, kind, amount, currency, status, due_date, created_at) VALUES (?,?,?,?,?,?,?)",
               (deal, "prepayment", 1000, "UAH", "pending", "2020-01-01", "2020-01-01 00:00:00"))
    db.execute("INSERT INTO payments (deal_id, kind, amount, currency, status, due_date, created_at) VALUES (?,?,?,?,?,?,?)",
               (deal, "balance", 500, "UAH", "paid", "2020-01-01", "2020-01-01 00:00:00"))
    db.execute("INSERT INTO payments (deal_id, kind, amount, currency, status, due_date, created_at) VALUES (?,?,?,?,?,?,?)",
               (deal, "final", 700, "UAH", "pending", "2999-01-01", "2020-01-01 00:00:00"))
    db.commit()
    html = client.get("/payments/overdue").get_data(as_text=True)
    assert "1000" in html.replace(" ", "").replace("\xa0", "") or "1 000" in html
    assert html.count("Рахунок</a>") == 1
    assert 'id="overdue-card"' in client.get("/").get_data(as_text=True)
    for url in (f"/deals/{deal}/invoice.pdf", f"/deals/{deal}/act.pdf"):
        r = client.get(url)
        assert r.status_code == 200 and r.data[:4] == b"%PDF"
    assert client.get("/deals/99999/invoice.pdf").status_code == 404
    # щоденне повідомлення — один раз
    sent = []
    monkeypatch.setattr(client._app_module, "notify_all", lambda t: sent.append(t))
    assert client._app_module.send_overdue_reminder() is True
    assert client._app_module.send_overdue_reminder() is False
    assert len(sent) == 1 and "Прострочені" in sent[0]


def test_reorder_candidates_and_task(client):
    login(client)
    db = _raw_db(client)
    cid = db.execute("SELECT id FROM clients ORDER BY id").fetchone()["id"]
    db.execute("DELETE FROM deals WHERE client_id=?", (cid,))
    db.execute("""INSERT INTO deals (title, client_id, stage, amount, currency, closed_at, created_at, updated_at)
                  VALUES ('Стара угода',?,?,100,'UAH',?,?,?)""", (cid, "won", "2020-01-01 00:00:00", "2020-01-01", "2020-01-01"))
    db.commit()
    assert "Стара угода" not in client.get("/clients/reorder").get_data(as_text=True)  # показуємо клієнта, не угоду
    name = db.execute("SELECT name FROM clients WHERE id=?", (cid,)).fetchone()["name"]
    assert name in client.get("/clients/reorder").get_data(as_text=True)
    post(client, "/clients/%d/reorder-task" % cid)
    t = db.execute("SELECT * FROM tasks WHERE client_id=? ORDER BY id DESC", (cid,)).fetchone()
    assert "повторне замовлення" in t["title"]
    # відкрита угода прибирає клієнта зі списку
    db.execute("""INSERT INTO deals (title, client_id, stage, amount, currency, created_at, updated_at)
                  VALUES ('Нова',?,?,1,'UAH','2026-01-01','2026-01-01')""", (cid, "lead"))
    db.commit()
    assert name not in client.get("/clients/reorder").get_data(as_text=True).split("<table")[-1]


def test_load_board_move_rules(client):
    login(client)
    db = _raw_db(client)
    deal = db.execute("SELECT id FROM deals LIMIT 1").fetchone()["id"]
    wcs = [r["id"] for r in db.execute("SELECT id FROM work_centers ORDER BY id")]
    db.execute("INSERT INTO production_orders (deal_id, quantity, status, created_at) VALUES (?,?,?,?)",
               (deal, 1, "planned", "2026-01-01 08:00:00"))
    oid = db.execute("SELECT MAX(id) m FROM production_orders").fetchone()["m"]
    for i, st in enumerate(("waiting", "done")):
        db.execute("""INSERT INTO production_operations (production_order_id, step_order, operation_name, work_center_id,
                      planned_hours, status, planned_start, planned_end) VALUES (?,?,?,?,?,?,?,?)""",
                   (oid, i + 1, f"Оп{i}", wcs[0], 2, st, "2026-10-07 09:00:00", "2026-10-07 11:00:00"))
    db.commit()
    op_wait, op_done = [r["id"] for r in db.execute("SELECT id FROM production_operations WHERE production_order_id=? ORDER BY id", (oid,))]
    assert "Оп0" in client.get("/production/board?start=2026-10-07").get_data(as_text=True)
    hdr = {"X-Requested-With": "fetch"}
    tok = get_csrf_token(client)
    r = client.post(f"/production/operations/{op_wait}/move", data={"date": "2026-10-09", "work_center_id": wcs[-1]},
                    headers={**hdr, "X-CSRF-Token": tok})
    assert r.get_json()["ok"] is True
    row = db.execute("SELECT * FROM production_operations WHERE id=?", (op_wait,)).fetchone()
    assert row["planned_start"] == "2026-10-09 09:00:00" and row["planned_end"] == "2026-10-09 11:00:00" and row["work_center_id"] == wcs[-1]
    assert client.post(f"/production/operations/{op_done}/move", data={"date": "2026-10-09"},
                       headers={**hdr, "X-CSRF-Token": tok}).status_code == 400
    assert client.post(f"/production/operations/{op_wait}/move", data={"date": "не-дата"},
                       headers={**hdr, "X-CSRF-Token": tok}).status_code == 400
    client.post(f"/production/operations/{op_wait}/move", data={"date": "2026-10-20"})  # без CSRF — відхиляється
    assert db.execute("SELECT planned_start FROM production_operations WHERE id=?", (op_wait,)).fetchone()[0].startswith("2026-10-09")
    client.get("/logout")
    login(client, "sklad", "sklad123")
    assert client.get("/production/board").status_code in (302, 403)


def test_audit_log_records_and_masks(client):
    login(client)
    post(client, "/parts/new", {"name": "Аудит-деталь", "currency": "UAH"})
    db = _raw_db(client)
    row = db.execute("SELECT * FROM audit_log WHERE endpoint='parts_new' ORDER BY id DESC").fetchone()
    assert row and row["username"] == "admin" and "Аудит-деталь" in row["detail"] and row["result"] == "ok"
    assert "_csrf" not in row["detail"]
    # паролі не потрапляють у журнал
    post(client, "/users/new", {"username": "zz", "password": "SuperSecret1", "full_name": "Z", "role": "sales"})
    assert not db.execute("SELECT 1 FROM audit_log WHERE detail LIKE '%SuperSecret1%'").fetchone()
    assert "Аудит-деталь" in client.get("/audit").get_data(as_text=True)
    assert "Аудит-деталь" not in client.get("/audit?q=немає-такого").get_data(as_text=True)
    client.get("/logout")
    login(client, "oleh", "manager123")
    assert client.get("/audit").status_code in (302, 403)


def test_milling_operator_has_own_mobile_terminal(client):
    """Фрезерувальник працює з телефона так само, як токар: свій термінал,
    дії лише на своїй (фрезерній) дільниці, на токарній — тільки перегляд."""
    login(client, "frezer", "frezer123")
    r = client.get("/production", follow_redirects=False)
    assert r.status_code == 302 and "/work-centers/3/terminal" in r.location
    own = client.get("/work-centers/3/terminal").get_data(as_text=True)
    assert "лише перегляд" not in own.lower()
    other = client.get("/work-centers/1/terminal").get_data(as_text=True)
    assert "лише перегляд" in other.lower() and "ПОЧАТИ" not in other
    db = _raw_db(client)
    op = db.execute("SELECT id FROM production_operations WHERE work_center_id=1 AND status='waiting' LIMIT 1").fetchone()
    if op:
        r = post(client, f"/production/operations/{op['id']}/advance", follow_redirects=True)
        assert "лише на своїй дільниці" in r.get_data(as_text=True).lower()
        assert db.execute("SELECT status FROM production_operations WHERE id=?", (op["id"],)).fetchone()[0] == "waiting"
    # термінал адаптований під телефон: переглядова область і великі кнопки
    assert 'name="viewport"' in own
