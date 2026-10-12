# -*- coding: utf-8 -*-
"""
Потік замовлення (як у службовій записці):

  1. Маркетинг заводить замовлення: № (193-6), дата, замовник, позиції з
     кресленням, кількістю і терміном («віддавати партійно» - теж варіант).
     Кожна позиція отримує шифр 193-6-1, 193-6-2 ...
  2. Паралельно:
       * склад перевіряє заготовку; якщо її немає - заявка постачальнику
         (Сьомаку) в Telegram, він закуповує і вписує ТТН, статус посилки
         підтягується з API Нової Пошти;
       * технолог прив'язує до позиції техпроцес зі своєї бібліотеки.
  3. Коли є і заготовка, і техпроцес - позицію запускають у виробництво:
     створюється виробниче замовлення з операціями по дільницях (верстатах),
     далі працює звичний маршрут: термінал цеху, журнал змін, Haas.

Підключається викликом register(app_module) у кінці app.py (до init_db()).
"""

import datetime
import json

from flask import render_template, request, redirect, url_for, flash, abort, g

ORDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL,
    doc_date TEXT,
    client_id INTEGER,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    drawing_no TEXT,
    unit TEXT DEFAULT 'шт',
    quantity REAL NOT NULL DEFAULT 1,
    due_date TEXT,
    batch_delivery INTEGER NOT NULL DEFAULT 0,
    code TEXT,
    blank_status TEXT NOT NULL DEFAULT 'unchecked',
    blank_note TEXT,
    blank_checked_by INTEGER,
    blank_checked_at TEXT,
    supplier_note TEXT,
    ttn TEXT,
    np_status TEXT,
    np_status_code INTEGER,
    np_checked_at TEXT,
    routing_id INTEGER,
    production_order_id INTEGER,
    FOREIGN KEY(order_id) REFERENCES orders(id)
);
"""

# Стан заготовки по позиції
BLANK_STATUSES = {
    "unchecked": ("Склад: перевірити заготовку", "#e8a33d"),
    "in_stock": ("Заготовка є на складі", "#3fb27f"),
    "to_buy": ("Заготовку треба купити", "#e5534b"),
    "ordered": ("Замовлено у постачальника", "#e8a33d"),
    "shipped": ("Заготовка в дорозі", "#4c9be8"),
    "received": ("Заготовку отримано", "#3fb27f"),
}
BLANK_READY = ("in_stock", "received")

NP_API_URL = "https://api.novaposhta.ua/v2.0/json/"
# Коди статусів Нової Пошти: 7, 8 - прибуло у відділення/поштомат; 9, 10, 11 - отримано
NP_ARRIVED_CODES = (7, 8)
NP_RECEIVED_CODES = (9, 10, 11)


def register(A):
    app = A.app
    get_db = A.get_db
    login_required = A.login_required
    role_required = A.role_required

    A.SCHEMA = A.SCHEMA + ORDERS_SCHEMA
    A.DEFAULT_PASSWORDS.setdefault("tehnolog", "tehnolog123")
    A.DEFAULT_PASSWORDS.setdefault("postach", "postach123")

    VIEW_ROLES = ("sales", "warehouse", "technologist", "purchaser", "production", "viewer")

    # ------------------------------------------------------------------ helpers
    def role():
        u = A.get_current_user()
        return u["role"] if u else None

    def num(value, default=None):
        try:
            v = float(str(value).replace(",", ".").strip())
            return v
        except (TypeError, ValueError):
            return default

    def base_url(db):
        """Адреса CRM для посилань у Telegram: постійна (Cloudflare), якщо
        ввімкнена, інакше адреса в локальній мережі."""
        try:
            pub = A._public_url(db)
            if pub:
                return pub
        except Exception:
            pass
        ip = A.get_lan_ip()
        port = A.os.environ.get("CRM_PORT", "5000")
        return f"http://{ip}:{port}" if ip else ""

    def send_telegram_to(chat_id, text):
        """Надсилає повідомлення в конкретний чат (напр. постачальнику)."""
        db = get_db()
        token = A.get_setting(db, "telegram_bot_token", "")
        if not token or not chat_id:
            return False, "Не налаштовано токен бота або chat_id"
        try:
            import urllib.request
            payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return (True, "OK") if result.get("ok") else (False, result.get("description", "помилка Telegram"))
        except Exception as e:
            return False, str(e)

    A.send_telegram_to = send_telegram_to

    def np_track(api_key, ttns):
        """Статуси посилок Нової Пошти: {ТТН: (код, текст)}. Кидає RuntimeError."""
        import urllib.request
        docs = [{"DocumentNumber": t, "Phone": ""} for t in ttns]
        body = {"apiKey": api_key, "modelName": "TrackingDocument", "calledMethod": "getStatusDocuments",
                "methodProperties": {"Documents": docs}}
        req = urllib.request.Request(NP_API_URL, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data.get("success"):
            errs = data.get("errors") or ["Нова Пошта повернула помилку"]
            raise RuntimeError("; ".join(str(e) for e in errs))
        out = {}
        for row in data.get("data", []):
            try:
                code = int(row.get("StatusCode"))
            except (TypeError, ValueError):
                code = None
            out[str(row.get("Number"))] = (code, row.get("Status") or "")
        return out

    A.np_track = np_track

    def refresh_np(db, items, force=False):
        """Оновлює статуси ТТН (не частіше ніж раз на 30 хв, якщо не force)."""
        api_key = A.get_setting(db, "np_api_key", "")
        now = datetime.datetime.now()
        todo = []
        for it in items:
            if not it["ttn"] or it["blank_status"] not in ("ordered", "shipped"):
                continue
            if not force and it["np_checked_at"]:
                try:
                    if now - datetime.datetime.fromisoformat(it["np_checked_at"]) < datetime.timedelta(minutes=30):
                        continue
                except ValueError:
                    pass
            todo.append(it)
        if not todo:
            return None
        if not api_key:
            return "Не задано API-ключ Нової Пошти (Налаштування)"
        try:
            statuses = A.np_track(api_key, [it["ttn"] for it in todo])
        except Exception as e:
            return f"Нова Пошта: {e}"
        for it in todo:
            code, text = statuses.get(it["ttn"], (None, ""))
            new_blank = it["blank_status"]
            if code in NP_RECEIVED_CODES:
                new_blank = "received"
            elif it["blank_status"] == "ordered":
                new_blank = "shipped"
            if code in NP_ARRIVED_CODES and it["np_status_code"] not in NP_ARRIVED_CODES:
                A.notify_all(f"📦 Заготовка для {it['code'] or it['name']} прибула у відділення НП (ТТН {it['ttn']}). Заберіть!")
            if new_blank == "received" and it["blank_status"] != "received":
                A.notify_all(f"✅ Заготовку для {it['code'] or it['name']} отримано (ТТН {it['ttn']}).")
            db.execute("""UPDATE order_items SET np_status=?, np_status_code=?, np_checked_at=?, blank_status=?
                          WHERE id=?""", (text or None, code, A.now_iso(), new_blank, it["id"]))
        db.commit()
        return None

    def item_progress(db, item):
        if not item["production_order_id"]:
            return None
        r = db.execute("""SELECT COUNT(*) t, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) d
                          FROM production_operations WHERE production_order_id=?""",
                       (item["production_order_id"],)).fetchone()
        po = db.execute("SELECT status FROM production_orders WHERE id=?", (item["production_order_id"],)).fetchone()
        total, done = r["t"] or 0, r["d"] or 0
        return {"total": total, "done": done, "pct": int(done * 100 / total) if total else 0,
                "status": po["status"] if po else None}

    def item_stage(db, item):
        """Головний стан позиції для бейджа: (ключ, текст, колір)."""
        prog = item_progress(db, item)
        if prog:
            if prog["status"] == "done":
                return "done", "Виготовлено", "#3fb27f"
            return "production", f"У виробництві {prog['done']}/{prog['total']}", "#4c9be8"
        blank_ok = item["blank_status"] in BLANK_READY
        if blank_ok and item["routing_id"]:
            return "ready", "Готово до запуску", "#f08a24"
        waits = []
        if not blank_ok:
            waits.append("заготовка")
        if not item["routing_id"]:
            waits.append("техпроцес")
        return "waiting", "Чекає: " + ", ".join(waits), "#e8a33d"

    def due_text(item):
        parts = []
        if item["batch_delivery"]:
            parts.append("партійно")
        if item["due_date"]:
            parts.append(("до " if item["batch_delivery"] else "") + item["due_date"])
        return ", ".join(parts) or "—"

    def enrich(db, items):
        today = A.today_str()
        out = []
        routings = {r["id"]: r for r in db.execute("SELECT id, name, drawing_no FROM routing_templates").fetchall()}
        for it in items:
            d = dict(it)
            d["stage_key"], d["stage_label"], d["stage_color"] = item_stage(db, it)
            d["blank_label"], d["blank_color"] = BLANK_STATUSES.get(it["blank_status"], (it["blank_status"], "#9aa1ad"))
            d["routing"] = routings.get(it["routing_id"])
            d["progress"] = item_progress(db, it)
            d["due_text"] = due_text(it)
            d["overdue"] = bool(it["due_date"] and it["due_date"] < today and d["stage_key"] != "done")
            out.append(d)
        return out

    def norm_drawing(text):
        return "".join((text or "").casefold().split())

    def auto_assign_routings(db, order_id):
        """Якщо деталь з таким кресленням уже має техпроцес - підставляємо його.
        Порівнюємо без регістру й пробілів у Python (SQLite lower() не знає кирилиці)."""
        lib = {}
        for r in db.execute("SELECT id, drawing_no FROM routing_templates WHERE drawing_no IS NOT NULL ORDER BY id").fetchall():
            if norm_drawing(r["drawing_no"]):
                lib[norm_drawing(r["drawing_no"])] = r["id"]
        n = 0
        for it in db.execute("""SELECT id, drawing_no FROM order_items WHERE order_id=? AND routing_id IS NULL""",
                             (order_id,)).fetchall():
            rid = lib.get(norm_drawing(it["drawing_no"])) if norm_drawing(it["drawing_no"]) else None
            if rid:
                db.execute("UPDATE order_items SET routing_id=? WHERE id=?", (rid, it["id"]))
                n += 1
        return n

    # ------------------------------------------------------------------ context
    @app.context_processor
    def inject_orders_helpers():
        def orders_attention():
            if "orders_attention" in g:
                return g.orders_attention
            n = 0
            try:
                r = role()
                db = get_db()
                if r == "warehouse":
                    n = db.execute("SELECT COUNT(*) c FROM order_items WHERE blank_status IN ('unchecked','received') "
                                   "AND production_order_id IS NULL AND blank_status='unchecked'").fetchone()["c"]
                elif r == "purchaser":
                    n = db.execute("SELECT COUNT(*) c FROM order_items WHERE blank_status='to_buy'").fetchone()["c"]
                elif r == "technologist":
                    n = db.execute("SELECT COUNT(*) c FROM order_items oi JOIN orders o ON o.id=oi.order_id "
                                   "WHERE oi.routing_id IS NULL AND oi.production_order_id IS NULL "
                                   "AND o.status='active'").fetchone()["c"]
                elif r in ("production", "admin"):
                    n = db.execute("SELECT COUNT(*) c FROM order_items WHERE routing_id IS NOT NULL "
                                   "AND blank_status IN ('in_stock','received') AND production_order_id IS NULL").fetchone()["c"]
            except Exception:
                n = 0
            g.orders_attention = n
            return n

        def routing_library():
            try:
                return get_db().execute(
                    "SELECT id, name, drawing_no FROM routing_templates ORDER BY name").fetchall()
            except Exception:
                return []

        return dict(orders_attention=orders_attention, routing_library=routing_library,
                    BLANK_STATUSES=BLANK_STATUSES)

    # ------------------------------------------------------------------ orders
    @app.route("/orders")
    @login_required
    @role_required(*VIEW_ROLES)
    def orders_list():
        db = get_db()
        q = (request.args.get("q") or "").strip()
        show = request.args.get("show", "active")
        sql = """SELECT o.*, c.name AS client_name FROM orders o LEFT JOIN clients c ON c.id=o.client_id WHERE 1=1"""
        params = []
        if show != "all":
            sql += " AND o.status='active'"
        if q:
            like = f"%{q}%"
            sql += """ AND (o.number LIKE ? OR c.name LIKE ? OR EXISTS (SELECT 1 FROM order_items oi WHERE oi.order_id=o.id
                       AND (oi.name LIKE ? OR oi.drawing_no LIKE ? OR oi.code LIKE ?)))"""
            params += [like] * 5
        sql += " ORDER BY o.id DESC LIMIT 300"
        orders = []
        for o in db.execute(sql, params).fetchall():
            items = enrich(db, db.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY line_no", (o["id"],)).fetchall())
            dues = [i["due_date"] for i in items if i["due_date"]]
            orders.append({"o": o, "items": items, "due": min(dues) if dues else None,
                           "overdue": any(i["overdue"] for i in items)})
        return render_template("orders_list.html", active="orders", orders=orders, q=q, show=show)

    def _order_form_context(db, order=None, items=None):
        clients = db.execute("SELECT id, name FROM clients ORDER BY name").fetchall()
        return dict(active="orders", order=order, items=items or [], clients=clients, today=A.today_str())

    def _parse_items(form):
        rows = {}
        for key in form.keys():
            if not key.startswith("items-"):
                continue
            try:
                _, idx, field = key.split("-", 2)
                idx = int(idx)
            except ValueError:
                continue
            rows.setdefault(idx, {})[field] = form.get(key, "").strip()
        out = []
        for idx in sorted(rows):
            r = rows[idx]
            if not r.get("name"):
                continue
            qty = num(r.get("qty"), None)
            if qty is None or qty <= 0:
                raise A.ValidationError(f"Позиція «{r['name']}»: кількість має бути більше нуля")
            due = r.get("due") or None
            if due:
                try:
                    datetime.date.fromisoformat(due)
                except ValueError:
                    raise A.ValidationError(f"Позиція «{r['name']}»: некоректна дата терміну")
            out.append({"id": int(r["id"]) if r.get("id", "").isdigit() else None, "name": r["name"][:200],
                        "drawing_no": (r.get("drawing") or "")[:100] or None, "unit": (r.get("unit") or "шт")[:10],
                        "quantity": qty, "due_date": due, "batch_delivery": 1 if r.get("batch") else 0,
                        "code": (r.get("code") or "")[:50] or None})
        if not out:
            raise A.ValidationError("Додайте хоча б одну позицію")
        return out

    def _items_for_rerender(form):
        """Позиції з форми для повторного показу після помилки (щоб не губити введене)."""
        rows = {}
        for key in form.keys():
            if key.startswith("items-"):
                try:
                    _, idx, field = key.split("-", 2)
                    rows.setdefault(int(idx), {})[field] = form.get(key, "")
                except ValueError:
                    continue
        out = []
        for idx in sorted(rows):
            r = rows[idx]
            out.append({"id": r.get("id") or "", "name": r.get("name", ""), "drawing_no": r.get("drawing", ""),
                        "unit": r.get("unit") or "шт", "quantity": num(r.get("qty"), 0) or 0,
                        "due_date": r.get("due", ""), "batch_delivery": bool(r.get("batch")),
                        "code": r.get("code", ""), "production_order_id": None})
        return out

    def _save_order(db, order=None):
        f = request.form
        number = (f.get("number") or "").strip()
        if not number:
            raise A.ValidationError("Вкажіть № замовлення (шифр), напр. 193-6")
        client_id = f.get("client_id") or None
        if not client_id:
            raise A.ValidationError("Оберіть замовника")
        items = _parse_items(f)
        doc_date = f.get("doc_date") or A.today_str()
        if order:
            db.execute("UPDATE orders SET number=?, doc_date=?, client_id=?, notes=?, updated_at=? WHERE id=?",
                       (number[:50], doc_date, client_id, (f.get("notes") or "").strip() or None, A.now_iso(), order["id"]))
            oid = order["id"]
        else:
            db.execute("""INSERT INTO orders (number, doc_date, client_id, notes, status, created_by, created_at)
                          VALUES (?,?,?,?,?,?,?)""",
                       (number[:50], doc_date, client_id, (f.get("notes") or "").strip() or None, "active",
                        A.session.get("user_id"), A.now_iso()))
            oid = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        existing = {r["id"]: r for r in db.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall()}
        keep = set()
        for line_no, it in enumerate(items, start=1):
            code = it["code"] or f"{number}-{line_no}"
            if it["id"] and it["id"] in existing:
                keep.add(it["id"])
                db.execute("""UPDATE order_items SET line_no=?, name=?, drawing_no=?, unit=?, quantity=?, due_date=?,
                              batch_delivery=?, code=? WHERE id=?""",
                           (line_no, it["name"], it["drawing_no"], it["unit"], it["quantity"], it["due_date"],
                            it["batch_delivery"], code, it["id"]))
            else:
                db.execute("""INSERT INTO order_items (order_id, line_no, name, drawing_no, unit, quantity, due_date,
                              batch_delivery, code) VALUES (?,?,?,?,?,?,?,?,?)""",
                           (oid, line_no, it["name"], it["drawing_no"], it["unit"], it["quantity"], it["due_date"],
                            it["batch_delivery"], code))
        for iid, row in existing.items():
            if iid not in keep:
                if row["production_order_id"]:
                    raise A.ValidationError(f"Позицію «{row['name']}» уже запущено у виробництво — її не можна видалити")
                db.execute("DELETE FROM order_items WHERE id=?", (iid,))
        auto = auto_assign_routings(db, oid)
        return oid, len(items), auto

    @app.route("/orders/new", methods=["GET", "POST"])
    @login_required
    @role_required("sales")
    def orders_new():
        db = get_db()
        if request.method == "POST":
            try:
                oid, n, auto = _save_order(db)
            except A.ValidationError as e:
                db.rollback()
                flash(str(e), "error")
                return render_template("order_form.html", **_order_form_context(db, None, _items_for_rerender(request.form)),
                                       form=request.form)
            db.commit()
            o = db.execute("SELECT o.number, c.name FROM orders o LEFT JOIN clients c ON c.id=o.client_id WHERE o.id=?",
                           (oid,)).fetchone()
            A.notify_all(f"📑 Нове замовлення {o['number']} ({o['name'] or ''}): позицій {n}.\n"
                         f"Склад — перевірити заготовки, технолог — техпроцеси.\n{base_url(db)}/orders/{oid}")
            flash("Замовлення створено. Склад і технолог бачать його у своїх чергах." +
                  (f" Техпроцес підставлено автоматично для {auto} поз. (така деталь уже робилась)." if auto else ""),
                  "success")
            return redirect(url_for("orders_detail", order_id=oid))
        return render_template("order_form.html", **_order_form_context(db), form=None)

    @app.route("/orders/<int:order_id>/edit", methods=["GET", "POST"])
    @login_required
    @role_required("sales")
    def orders_edit(order_id):
        db = get_db()
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            abort(404)
        if request.method == "POST":
            try:
                _save_order(db, order)
            except A.ValidationError as e:
                db.rollback()
                flash(str(e), "error")
                return redirect(url_for("orders_edit", order_id=order_id))
            db.commit()
            flash("Замовлення збережено", "success")
            return redirect(url_for("orders_detail", order_id=order_id))
        items = db.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY line_no", (order_id,)).fetchall()
        return render_template("order_form.html", **_order_form_context(db, order, items), form=None)

    @app.route("/orders/<int:order_id>/status", methods=["POST"])
    @login_required
    @role_required("sales")
    def orders_set_status(order_id):
        db = get_db()
        status = request.form.get("status")
        if status not in ("active", "closed", "cancelled"):
            abort(400)
        db.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (status, A.now_iso(), order_id))
        db.commit()
        flash({"active": "Замовлення знову в роботі", "closed": "Замовлення закрито",
               "cancelled": "Замовлення скасовано"}[status], "success")
        return redirect(url_for("orders_detail", order_id=order_id))

    @app.route("/orders/<int:order_id>")
    @login_required
    @role_required(*VIEW_ROLES)
    def orders_detail(order_id):
        db = get_db()
        order = db.execute("""SELECT o.*, c.name AS client_name, u.full_name AS author
                              FROM orders o LEFT JOIN clients c ON c.id=o.client_id
                              LEFT JOIN users u ON u.id=o.created_by WHERE o.id=?""", (order_id,)).fetchone()
        if not order:
            abort(404)
        raw = db.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY line_no", (order_id,)).fetchall()
        err = refresh_np(db, raw)
        if err and role() in ("admin", "warehouse", "purchaser"):
            flash(err, "error")
        raw = db.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY line_no", (order_id,)).fetchall()
        items = enrich(db, raw)
        routings = db.execute("SELECT id, name, drawing_no FROM routing_templates ORDER BY name").fetchall()
        purchaser_ready = bool(A.get_setting(db, "purchaser_chat_id", "") and A.get_setting(db, "telegram_bot_token", ""))
        return render_template("order_detail.html", active="orders", order=order, items=items, routings=routings,
                               r=role(), purchaser_ready=purchaser_ready)

    def _item(db, item_id):
        it = db.execute("""SELECT oi.*, o.number, o.client_id, c.name AS client_name FROM order_items oi
                           JOIN orders o ON o.id=oi.order_id LEFT JOIN clients c ON c.id=o.client_id
                           WHERE oi.id=?""", (item_id,)).fetchone()
        if not it:
            abort(404)
        return it

    # -------------------------------------------------- склад: заготовка
    @app.route("/orders/items/<int:item_id>/blank", methods=["POST"])
    @login_required
    @role_required("warehouse")
    def order_item_blank(item_id):
        db = get_db()
        it = _item(db, item_id)
        action = request.form.get("action")
        note = (request.form.get("blank_note") or "").strip()[:300] or it["blank_note"]
        if action == "in_stock":
            db.execute("""UPDATE order_items SET blank_status='in_stock', blank_note=?, blank_checked_by=?,
                          blank_checked_at=? WHERE id=?""", (note, A.session.get("user_id"), A.now_iso(), item_id))
            db.commit()
            flash(f"{it['code']}: заготовка є", "success")
        elif action == "to_buy":
            if not note:
                flash("Опишіть, яку заготовку купити (матеріал, розмір, кількість)", "error")
                return redirect(url_for("orders_detail", order_id=it["order_id"]) + f"#item-{item_id}")
            db.execute("""UPDATE order_items SET blank_status='to_buy', blank_note=?, blank_checked_by=?,
                          blank_checked_at=? WHERE id=?""", (note, A.session.get("user_id"), A.now_iso(), item_id))
            db.commit()
            text = (f"🛒 Заявка на заготовку\n{it['code']} — {it['name']}"
                    + (f" (кр. {it['drawing_no']})" if it["drawing_no"] else "")
                    + f"\nКількість деталей: {it['quantity']:g} {it['unit']}\nЗаготовка: {note}"
                    + f"\nЗамовник: {it['client_name'] or '—'} · термін: {due_text(it)}"
                    + f"\nПісля закупівлі впишіть ТТН: {base_url(db)}/procurement")
            chat = A.get_setting(db, "purchaser_chat_id", "")
            if chat:
                ok, msg = A.send_telegram_to(chat, text)
                flash("Заявку надіслано постачальнику в Telegram" if ok else f"Заявку збережено, але Telegram: {msg}",
                      "success" if ok else "error")
            else:
                A.notify_all(text)
                flash("Заявку збережено. Щоб вона йшла саме постачальнику, вкажіть його chat_id у Налаштуваннях → Telegram",
                      "success")
        elif action == "reset":
            db.execute("UPDATE order_items SET blank_status='unchecked' WHERE id=? AND production_order_id IS NULL", (item_id,))
            db.commit()
        else:
            abort(400)
        return redirect(url_for("orders_detail", order_id=it["order_id"]) + f"#item-{item_id}")

    # -------------------------------------------------- постачальник: ТТН
    @app.route("/orders/items/<int:item_id>/purchase", methods=["POST"])
    @login_required
    @role_required("purchaser", "warehouse")
    def order_item_purchase(item_id):
        db = get_db()
        it = _item(db, item_id)
        action = request.form.get("action", "ttn")
        back = request.form.get("back") or url_for("procurement_list")
        if not back.startswith("/") or back.startswith("//"):
            back = url_for("procurement_list")
        supplier = (request.form.get("supplier_note") or "").strip()[:300] or it["supplier_note"]
        if action == "ttn":
            ttn = "".join(ch for ch in (request.form.get("ttn") or "") if ch.isdigit())
            if ttn and not 10 <= len(ttn) <= 20:
                flash("ТТН Нової Пошти — це 14 цифр (напр. 20450000000000)", "error")
                return redirect(back)
            status = "shipped" if ttn else "ordered"
            db.execute("""UPDATE order_items SET ttn=?, supplier_note=?, blank_status=?, np_checked_at=NULL
                          WHERE id=?""", (ttn or None, supplier, status, item_id))
            db.commit()
            if ttn:
                err = refresh_np(db, [_item(db, item_id)], force=True)
                if err:
                    flash(err, "error")
            flash(f"{it['code']}: " + (f"ТТН {ttn} збережено" if ttn else "позначено як замовлене"), "success")
        elif action == "received":
            db.execute("UPDATE order_items SET blank_status='received', supplier_note=? WHERE id=?", (supplier, item_id))
            db.commit()
            A.notify_all(f"✅ Заготовку для {it['code']} — {it['name']} отримано на склад.")
            flash(f"{it['code']}: заготовку отримано", "success")
        elif action == "refresh":
            err = refresh_np(db, [it], force=True)
            flash(err or "Статус оновлено", "error" if err else "success")
        else:
            abort(400)
        return redirect(back)

    @app.route("/procurement")
    @login_required
    @role_required("purchaser", "warehouse")
    def procurement_list():
        db = get_db()
        raw = db.execute("""SELECT oi.*, o.number, c.name AS client_name FROM order_items oi
                            JOIN orders o ON o.id=oi.order_id LEFT JOIN clients c ON c.id=o.client_id
                            WHERE oi.blank_status IN ('to_buy','ordered','shipped')
                               OR (oi.blank_status='received' AND oi.ttn IS NOT NULL
                                   AND oi.production_order_id IS NULL)
                            ORDER BY CASE oi.blank_status WHEN 'to_buy' THEN 0 WHEN 'ordered' THEN 1
                                     WHEN 'shipped' THEN 2 ELSE 3 END, oi.id""").fetchall()
        err = refresh_np(db, raw)
        if err:
            flash(err, "error")
            raw_ids = [r["id"] for r in raw]
        else:
            raw_ids = [r["id"] for r in raw]
        rows = []
        for iid in raw_ids:
            it = _item(db, iid)
            d = dict(it)
            d["blank_label"], d["blank_color"] = BLANK_STATUSES.get(it["blank_status"], (it["blank_status"], "#9aa1ad"))
            d["due_text"] = due_text(it)
            rows.append(d)
        return render_template("procurement.html", active="procurement", rows=rows,
                               np_ready=bool(A.get_setting(db, "np_api_key", "")))

    # -------------------------------------------------- технолог: техпроцес
    @app.route("/orders/items/<int:item_id>/routing", methods=["POST"])
    @login_required
    @role_required("technologist")
    def order_item_routing(item_id):
        db = get_db()
        it = _item(db, item_id)
        if it["production_order_id"]:
            flash("Позиція вже у виробництві — техпроцес змінювати пізно", "error")
            return redirect(url_for("orders_detail", order_id=it["order_id"]))
        rid = request.form.get("routing_id") or None
        if rid and not db.execute("SELECT 1 FROM routing_templates WHERE id=?", (rid,)).fetchone():
            abort(400)
        db.execute("UPDATE order_items SET routing_id=? WHERE id=?", (rid, item_id))
        db.commit()
        flash(f"{it['code']}: техпроцес " + ("прив'язано" if rid else "знято"), "success")
        return redirect(url_for("orders_detail", order_id=it["order_id"]) + f"#item-{item_id}")

    # -------------------------------------------------- запуск у виробництво
    @app.route("/orders/items/<int:item_id>/launch", methods=["POST"])
    @login_required
    @role_required("technologist", "production")
    def order_item_launch(item_id):
        db = get_db()
        it = _item(db, item_id)
        back = url_for("orders_detail", order_id=it["order_id"]) + f"#item-{item_id}"
        if it["production_order_id"]:
            flash("Позицію вже запущено", "error")
            return redirect(back)
        if not it["routing_id"]:
            flash("Спершу технолог має прив'язати техпроцес", "error")
            return redirect(back)
        if it["blank_status"] not in BLANK_READY and not request.form.get("force"):
            flash("Заготовки ще немає на складі — запуск неможливий", "error")
            return redirect(back)
        steps = db.execute("SELECT * FROM routing_steps WHERE routing_template_id=? ORDER BY step_order",
                           (it["routing_id"],)).fetchall()
        if not steps:
            flash("У техпроцесі немає жодної операції", "error")
            return redirect(back)
        qty = int(round(it["quantity"])) or 1
        db.execute("""INSERT INTO production_orders (deal_id, machine_id, routing_template_id, quantity, status,
                      due_date, created_at, title, client_id, order_item_id)
                      VALUES (NULL, NULL, ?, ?, 'planned', ?, ?, ?, ?, ?)""",
                   (it["routing_id"], qty, it["due_date"], A.now_iso(),
                    f"{it['code']} {it['name']}"[:200], it["client_id"], item_id))
        po_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        for s in steps:
            db.execute("""INSERT INTO production_operations (production_order_id, step_order, operation_name,
                          work_center_id, planned_hours, status) VALUES (?,?,?,?,?, 'waiting')""",
                       (po_id, s["step_order"], s["operation_name"], s["work_center_id"], s["planned_hours"]))
        A.schedule_production_order(db, po_id)
        db.execute("UPDATE order_items SET production_order_id=? WHERE id=?", (po_id, item_id))
        db.commit()
        A.notify_all(f"🏭 У виробництво: {it['code']} — {it['name']}, {qty} {it['unit']} (виробниче №{po_id}).")
        flash(f"Запущено у виробництво: виробниче замовлення №{po_id}", "success")
        return redirect(back)

    # ------------------------------------------------------------------ techprocess library
    def _routing_form_context(db, routing=None, steps=None):
        wcs = db.execute("SELECT id, name FROM work_centers ORDER BY id").fetchall()
        return dict(active="routings", routing=routing, steps=steps or [], wcs=wcs,
                    for_item=request.args.get("for_item", type=int),
                    prefill_name=request.args.get("name", ""), prefill_drawing=request.args.get("drawing", ""))

    @app.route("/routings")
    @login_required
    @role_required("technologist", "production")
    def routings_list():
        db = get_db()
        q = (request.args.get("q") or "").strip()
        sql = """SELECT r.*, (SELECT COUNT(*) FROM routing_steps s WHERE s.routing_template_id=r.id) AS steps,
                        (SELECT ROUND(SUM(planned_hours)*60, 1) FROM routing_steps s WHERE s.routing_template_id=r.id) AS minutes,
                        (SELECT COUNT(*) FROM order_items oi WHERE oi.routing_id=r.id) AS used
                 FROM routing_templates r WHERE 1=1"""
        params = []
        if q:
            sql += " AND (r.name LIKE ? OR r.drawing_no LIKE ? OR r.material LIKE ?)"
            params += [f"%{q}%"] * 3
        rows = db.execute(sql + " ORDER BY r.name", params).fetchall()
        waiting = db.execute("""SELECT oi.id, oi.code, oi.name, oi.drawing_no, oi.order_id FROM order_items oi
                                JOIN orders o ON o.id=oi.order_id
                                WHERE oi.routing_id IS NULL AND oi.production_order_id IS NULL AND o.status='active'
                                ORDER BY oi.id LIMIT 50""").fetchall()
        return render_template("routings_list.html", active="routings", rows=rows, q=q, waiting=waiting, r=role())

    def _save_routing(db, routing=None):
        f = request.form
        name = (f.get("name") or "").strip()
        if not name:
            raise A.ValidationError("Вкажіть назву техпроцесу (деталі)")
        steps = {}
        for key in f.keys():
            if key.startswith("steps-"):
                try:
                    _, idx, field = key.split("-", 2)
                    steps.setdefault(int(idx), {})[field] = f.get(key, "").strip()
                except ValueError:
                    continue
        clean = []
        for idx in sorted(steps):
            s = steps[idx]
            if not s.get("op"):
                continue
            minutes = num(s.get("min"), None)
            if minutes is None or minutes <= 0:
                raise A.ValidationError(f"Операція «{s['op']}»: вкажіть час на 1 шт у хвилинах")
            wc = s.get("wc") or None
            clean.append((s["op"][:120], int(wc) if wc and wc.isdigit() else None, round(minutes / 60.0, 4)))
        if not clean:
            raise A.ValidationError("Додайте хоча б одну операцію")
        vals = (name[:200], (f.get("drawing_no") or "").strip()[:100] or None, (f.get("material") or "").strip()[:200] or None,
                (f.get("blank") or "").strip()[:200] or None, (f.get("notes") or "").strip()[:1000] or None)
        if routing:
            db.execute("""UPDATE routing_templates SET name=?, drawing_no=?, material=?, blank=?, notes=?, updated_at=?
                          WHERE id=?""", vals + (A.now_iso(), routing["id"]))
            rid = routing["id"]
            db.execute("DELETE FROM routing_steps WHERE routing_template_id=?", (rid,))
        else:
            db.execute("""INSERT INTO routing_templates (category, name, drawing_no, material, blank, notes, created_at)
                          VALUES ('Техпроцес',?,?,?,?,?,?)""", vals + (A.now_iso(),))
            rid = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        for i, (op, wc, hours) in enumerate(clean, start=1):
            db.execute("""INSERT INTO routing_steps (routing_template_id, step_order, operation_name, work_center_id,
                          planned_hours) VALUES (?,?,?,?,?)""", (rid, i * 5, op, wc, hours))
        return rid

    @app.route("/routings/new", methods=["GET", "POST"])
    @login_required
    @role_required("technologist")
    def routings_new():
        db = get_db()
        if request.method == "POST":
            try:
                rid = _save_routing(db)
            except A.ValidationError as e:
                db.rollback()
                flash(str(e), "error")
                return render_template("routing_form.html", **_routing_form_context(db), form=request.form)
            for_item = request.form.get("for_item", type=int)
            if for_item:
                it = db.execute("SELECT * FROM order_items WHERE id=?", (for_item,)).fetchone()
                if it and not it["production_order_id"]:
                    db.execute("UPDATE order_items SET routing_id=? WHERE id=?", (rid, for_item))
                    db.commit()
                    flash("Техпроцес створено і прив'язано до позиції", "success")
                    return redirect(url_for("orders_detail", order_id=it["order_id"]) + f"#item-{for_item}")
            db.commit()
            flash("Техпроцес збережено в бібліотеці", "success")
            return redirect(url_for("routings_list"))
        return render_template("routing_form.html", **_routing_form_context(db), form=None)

    @app.route("/routings/<int:routing_id>/edit", methods=["GET", "POST"])
    @login_required
    @role_required("technologist", "production")
    def routings_edit(routing_id):
        db = get_db()
        routing = db.execute("SELECT * FROM routing_templates WHERE id=?", (routing_id,)).fetchone()
        if not routing:
            abort(404)
        if request.method == "POST":
            if role() not in ("admin", "technologist"):
                abort(403)
            try:
                _save_routing(db, routing)
            except A.ValidationError as e:
                db.rollback()
                flash(str(e), "error")
                return redirect(url_for("routings_edit", routing_id=routing_id))
            db.commit()
            flash("Техпроцес збережено. Вже запущені замовлення не змінюються.", "success")
            return redirect(url_for("routings_list"))
        steps = db.execute("SELECT * FROM routing_steps WHERE routing_template_id=? ORDER BY step_order",
                           (routing_id,)).fetchall()
        return render_template("routing_form.html", **_routing_form_context(db, routing, steps), form=None)

    @app.route("/routings/<int:routing_id>/delete", methods=["POST"])
    @login_required
    @role_required("technologist")
    def routings_delete(routing_id):
        db = get_db()
        used = db.execute("SELECT COUNT(*) c FROM order_items WHERE routing_id=?", (routing_id,)).fetchone()["c"]
        used += db.execute("SELECT COUNT(*) c FROM deals WHERE routing_template_id=?", (routing_id,)).fetchone()["c"]
        if used:
            flash("Техпроцес використовується в замовленнях — видалити не можна", "error")
            return redirect(url_for("routings_list"))
        db.execute("DELETE FROM routing_steps WHERE routing_template_id=?", (routing_id,))
        db.execute("DELETE FROM routing_templates WHERE id=?", (routing_id,))
        db.commit()
        flash("Техпроцес видалено", "success")
        return redirect(url_for("routings_list"))

    # ------------------------------------------------------------------ PDF
    def _pdf_table(title, subtitle_lines, header, rows, col_x, right_cols=(), footer_lines=()):
        from io import BytesIO
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.pdfgen import canvas as pdf_canvas
        from flask import send_file
        buf = BytesIO()
        c = pdf_canvas.Canvas(buf, pagesize=A4)
        w, h = A4
        F, FB = A.PDF_FONT, A.PDF_FONT_BOLD

        def clip(text, width_mm, font, size):
            text = str(text if text is not None else "")
            from reportlab.pdfbase.pdfmetrics import stringWidth
            while text and stringWidth(text, font, size) > width_mm * mm:
                text = text[:-2] + "…" if len(text) > 2 else ""
            return text

        def head(y):
            c.setFillColor(colors.HexColor("#eeeeee"))
            c.rect(15 * mm, y - 2.5 * mm, w - 30 * mm, 8 * mm, fill=1, stroke=0)
            c.setFillColor(colors.black)
            c.setFont(FB, 9)
            for i, (x, label) in enumerate(zip(col_x, header)):
                c.drawString(x * mm, y, label)
            return y - 8 * mm

        y = h - 20 * mm
        c.setFont(FB, 15)
        c.drawCentredString(w / 2, y, title)
        y -= 8 * mm
        c.setFont(F, 10)
        for line in subtitle_lines:
            c.drawString(15 * mm, y, line[:110])
            y -= 5.5 * mm
        y -= 4 * mm
        y = head(y)
        c.setFont(F, 9)
        widths = [(col_x[i + 1] if i + 1 < len(col_x) else 195) - col_x[i] - 2 for i in range(len(col_x))]
        for row in rows:
            if y < 25 * mm:
                c.showPage()
                y = head(h - 20 * mm)
                c.setFont(F, 9)
            for i, (x, val) in enumerate(zip(col_x, row)):
                c.drawString(x * mm, y, clip(val, widths[i], F, 9))
            c.setStrokeColor(colors.HexColor("#dddddd"))
            c.line(15 * mm, y - 2.5 * mm, w - 15 * mm, y - 2.5 * mm)
            y -= 7 * mm
        y -= 8 * mm
        c.setFont(F, 10)
        for line in footer_lines:
            if y < 20 * mm:
                c.showPage()
                y = h - 20 * mm
                c.setFont(F, 10)
            c.drawString(15 * mm, y, line)
            y -= 9 * mm
        c.showPage()
        c.save()
        buf.seek(0)
        return send_file(buf, mimetype="application/pdf", download_name="document.pdf")

    @app.route("/orders/<int:order_id>/note.pdf")
    @login_required
    @role_required(*VIEW_ROLES)
    def orders_note_pdf(order_id):
        """Службова записка по замовленню (як паперова, для складу й цеху)."""
        db = get_db()
        o = db.execute("SELECT o.*, c.name AS client_name FROM orders o LEFT JOIN clients c ON c.id=o.client_id "
                       "WHERE o.id=?", (order_id,)).fetchone()
        if not o:
            abort(404)
        company = A.get_setting(db, "company_name", "") or "Оснастка-Маркет"
        items = db.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY line_no", (order_id,)).fetchall()
        rows = [(str(i["line_no"]), f"{i['name']} {i['drawing_no'] or ''}".strip(), i["unit"] or "шт",
                 f"{i['quantity']:g}", due_text(i), i["code"] or "") for i in items]
        try:
            dd = datetime.date.fromisoformat(o["doc_date"]).strftime("%d.%m.%Y")
        except (TypeError, ValueError):
            dd = o["doc_date"] or ""
        return _pdf_table(f"СЛУЖБОВА ЗАПИСКА № {o['number']}", [f"Постачальник: {company}", f"Одержувач: {o['client_name'] or '—'}",
                          f"Дата: {dd}"], ["№", "Назва", "Од.", "К-сть", "Термін", "Шифр"], rows,
                          [16, 24, 120, 134, 150, 178], footer_lines=["Склав: ____________________",
                                                                      "Одержав (склад): ____________________",
                                                                      "Одержав (технолог): ____________________"])

    @app.route("/procurement/request.pdf")
    @login_required
    @role_required("purchaser", "warehouse")
    def procurement_pdf():
        """Заявка постачальнику: що купити, під яке замовлення, до якого терміну."""
        db = get_db()
        items = db.execute("""SELECT oi.*, o.number FROM order_items oi JOIN orders o ON o.id=oi.order_id
                              WHERE oi.blank_status IN ('to_buy','ordered') ORDER BY oi.id""").fetchall()
        rows = [(i["code"] or "", f"{i['name']} {i['drawing_no'] or ''}".strip(), f"{i['quantity']:g} {i['unit']}",
                 i["blank_note"] or "", due_text(i), BLANK_STATUSES[i["blank_status"]][0] if i["blank_status"] in BLANK_STATUSES else "")
                for i in items]
        return _pdf_table("ЗАЯВКА НА ЗАКУПІВЛЮ ЗАГОТОВОК",
                          [f"Дата: {datetime.date.today().strftime('%d.%m.%Y')}", f"Позицій: {len(rows)}"],
                          ["Шифр", "Деталь", "Деталей", "Що купити (заготовка)", "Термін", "Стан"], rows,
                          [16, 34, 82, 100, 160, 178],
                          footer_lines=["Склав (склад): ____________________", "Прийняв (постачальник): ____________________"])

    # ------------------------------------------------------------------ settings
    @app.route("/settings/orders", methods=["POST"])
    @login_required
    def settings_orders_save():
        if not A.is_admin():
            abort(403)
        db = get_db()
        f = request.form
        A.set_setting(db, "purchaser_chat_id", (f.get("purchaser_chat_id") or "").strip())
        A.set_setting(db, "np_api_key", (f.get("np_api_key") or "").strip())
        db.commit()
        if f.get("test"):
            chat = A.get_setting(db, "purchaser_chat_id", "")
            ok, msg = A.send_telegram_to(chat, "🔧 Тест: сюди приходитимуть заявки на заготовки з Оснастка-Маркет")
            flash("Тестове повідомлення постачальнику надіслано" if ok else f"Telegram: {msg}",
                  "success" if ok else "error")
        else:
            flash("Налаштування закупівлі збережено", "success")
        return redirect(url_for("settings_page") + "#orders-settings")

    # ------------------------------------------------------------------ demo seed
    original_seed = A.seed_data

    def seed_with_orders(db):
        original_seed(db)
        try:
            seed_orders_demo(A, db)
        except Exception:
            pass

    A.seed_data = seed_with_orders

    # ------------------------------------------------------------------ templates
    from orders_templates import TEMPLATES
    app.jinja_loader.mapping.update(TEMPLATES)


def seed_orders_demo(A, db):
    """Демо: користувачі технолог і постачальник, службова записка 193-6
    з двома позиціями і два техпроцеси."""
    now = A.now_iso()
    for username, full_name, role_key in (("tehnolog", "Технолог", "technologist"),
                                          ("postach", "Постачальник (Сьомак)", "purchaser")):
        if not db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            db.execute("INSERT INTO users (username, password_hash, full_name, role, active) VALUES (?,?,?,?,1)",
                       (username, A.generate_password_hash(A.DEFAULT_PASSWORDS[username]), full_name, role_key))
    wc = {r[1]: r[0] for r in db.execute("SELECT id, name FROM work_centers").fetchall()}

    def wc_like(word):
        for name, wid in wc.items():
            if word.lower() in name.lower():
                return wid
        return None

    cur = db.cursor()
    routes = [
        ("Упор пружини", "кр.322100.022", "Сталь 45", "Коло Ø40, відрізка 52 мм",
         [("Відрізна: пила", wc_like("Заготів"), 1.5), ("Токарна ЧПУ: точіння", wc_like("Токар"), 6),
          ("Фрезерна: паз", wc_like("Фрезер"), 4), ("Контроль ВТК", wc_like("ВТК"), 0.5)]),
        ("Кронштейн пружини", "кр.322200.012", "Сталь 09Г2С", "Лист 8 мм, вирізка 60×120",
         [("Різка лазер", wc_like("Заготів"), 1), ("Фрезерна ЧПУ", wc_like("Фрезер"), 5),
          ("Контроль ВТК", wc_like("ВТК"), 0.5)]),
    ]
    rids = []
    for name, dwg, mat, blank, steps in routes:
        cur.execute("""INSERT INTO routing_templates (category, name, drawing_no, material, blank, created_at)
                       VALUES ('Техпроцес',?,?,?,?,?)""", (name, dwg, mat, blank, now))
        rid = cur.lastrowid
        rids.append(rid)
        for i, (op, w, minutes) in enumerate(steps, start=1):
            cur.execute("""INSERT INTO routing_steps (routing_template_id, step_order, operation_name, work_center_id,
                           planned_hours) VALUES (?,?,?,?,?)""", (rid, i * 5, op, w, round(minutes / 60, 4)))
    client = db.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()
    cur.execute("""INSERT INTO orders (number, doc_date, client_id, status, created_at) VALUES (?,?,?,?,?)""",
                ("193-6", A.today_str(), client[0] if client else None, "active", now))
    oid = cur.lastrowid
    due = (datetime.date.today() + datetime.timedelta(days=21)).isoformat()
    cur.execute("""INSERT INTO order_items (order_id, line_no, name, drawing_no, unit, quantity, due_date, batch_delivery,
                   code, blank_status, blank_note, routing_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (oid, 1, "Упор пружини", "кр.322100.022", "шт", 200, due, 1, "193-6-1", "in_stock",
                 "Коло Ст45 Ø40 — 120 кг на складі", rids[0]))
    cur.execute("""INSERT INTO order_items (order_id, line_no, name, drawing_no, unit, quantity, due_date, batch_delivery,
                   code, blank_status, blank_note, routing_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (oid, 2, "Кронштейн пружини", "кр.322200.012", "шт", 400, None, 0, "193-6-2", "unchecked", None, None))
    db.commit()
