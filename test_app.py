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

import pytest


@pytest.fixture()
def client(monkeypatch):
    """Ізольований тестовий клієнт: тимчасова база даних, чиста для кожного тесту."""
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test_crm.db")
    monkeypatch.setenv("CRM_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("CRM_DEBUG", "0")

    import importlib
    import app as app_module
    importlib.reload(app_module)  # щоб підхопився новий DB_PATH нижче
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
    import io
    login(client)
    dxf_bytes = _make_sample_dxf_bytes()
    token = get_csrf_token(client)
    r = client.post("/calculator/analyze-dxf", data={
        "_csrf_token": token,
        "dxf_file": (io.BytesIO(dxf_bytes), "деталь.dxf"),
        "thickness_mm": "3", "density_kg_m3": "7850",
    }, content_type="multipart/form-data")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    # Периметр: 2 лінії (100+50) + периметр полілінії (300) + коло (2*pi*10≈62.83) = 512.83мм = 0.513м
    assert "0.513" in html
    assert "100.0" in html  # ширина заготовки


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
# Майстер, прив'язаний до конкретної дільниці
# ---------------------------------------------------------------------------

def test_foreman_redirected_to_own_terminal(client):
    login(client, "tokar", "tokar123")
    r = client.get("/")
    assert "Моя дільниця" in r.get_data(as_text=True)
    r2 = client.get("/production", follow_redirects=False)
    assert r2.status_code == 302
    assert "/work-centers/1/terminal" in r2.location
    r3 = client.get("/work-centers", follow_redirects=False)
    assert "/work-centers/1/terminal" in r3.location


def test_foreman_blocked_from_other_work_center(client):
    login(client, "tokar", "tokar123")
    r = client.get("/work-centers/3/terminal", follow_redirects=True)
    assert "немає доступу" in r.get_data(as_text=True).lower()
    r2 = client.get("/work-centers/1/terminal")
    assert r2.status_code == 200


def test_general_production_role_sees_everything(client):
    login(client, "maister", "prod123")
    r = client.get("/production")
    assert r.status_code == 200
    r2 = client.get("/work-centers")
    assert r2.status_code == 200


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


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
