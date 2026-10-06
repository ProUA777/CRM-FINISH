# -*- coding: utf-8 -*-
"""
Додаткові функції Оснастка-Маркет (окремий модуль, щоб не роздувати app.py):

  * Каталог деталей (креслення, матеріал, техпроцес, остання ціна) і швидке створення угоди
  * План/факт по виробничих замовленнях: години, матеріали, маржа
  * Заявка на закупівлю матеріалів за мінімальними залишками (Excel / Telegram)
  * Контроль якості: чек-лист перевірки і облік браку
  * Прострочені оплати, рахунок і акт у PDF
  * Повторні замовлення: клієнти, які давно не замовляли
  * Журнал змін («хто що змінив»)
  * Дошка завантаження верстатів з перетягуванням операцій

Підключається викликом register(app_module) у кінці app.py.
"""

import datetime
import json
from io import BytesIO

from flask import (render_template, request, redirect, url_for, flash, abort,
                   jsonify, session, send_file)

QUALITY_CHECKLIST = [
    ("dim", "Розміри за кресленням"),
    ("surf", "Шорсткість і якість поверхні"),
    ("thread", "Різьба та отвори"),
    ("mark", "Маркування"),
    ("pack", "Комплектність і упаковка"),
]

AUDIT_LABELS = {
    "deal_new": "Створено угоду", "deal_edit": "Змінено угоду", "deal_delete": "Видалено угоду",
    "deal_move": "Змінено етап угоди", "payment_add": "Додано платіж", "payment_toggle": "Змінено статус платежу",
    "payment_delete": "Видалено платіж", "client_new": "Створено клієнта", "client_edit": "Змінено клієнта",
    "client_delete": "Видалено клієнта", "settings_save": "Змінено налаштування",
    "settings_rates_nbu": "Курси з НБУ", "work_center_connect": "Підключення верстата",
    "warehouse_item_new": "Нова позиція складу", "warehouse_transaction": "Рух по складу",
    "parts_save": "Збережено деталь", "parts_delete": "Видалено деталь",
    "quality_new": "Перевірка якості", "operation_move": "Перенесено операцію",
    "operation_advance": "Статус операції", "users_toggle": "Користувача змінено",
}
SENSITIVE_MARKS = ("pass", "token", "key", "secret", "csrf")


def register(A):
    app = A.app
    get_db = A.get_db
    login_required = A.login_required
    role_required = A.role_required

    # ------------------------------------------------------------------ helpers
    def today():
        return A.today_str()

    def num(value, default=0.0):
        try:
            return float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            return default

    def uah(db, amount, currency, on_date=None):
        return A.to_uah(db, amount or 0, currency or "UAH", on_date)

    def can_see_money():
        u = A.get_current_user()
        return bool(u and u["role"] in ("admin", "accountant", "sales"))

    # ------------------------------------------------------------------ audit
    @app.after_request
    def audit_after_request(resp):
        try:
            if request.method != "POST" or request.endpoint in (None, "login", "api_telemetry", "static"):
                return resp
            if resp.status_code >= 400:
                return resp
            user = A.get_current_user()
            if not user:
                return resp
            flashes = session.get("_flashes") or []
            rejected = any(len(f) > 1 and f[0] == "error" for f in flashes)
            parts = []
            for key in list(request.form.keys())[:10]:
                if any(m in key.lower() for m in SENSITIVE_MARKS):
                    continue
                val = request.form.get(key, "")
                if val:
                    parts.append(f"{key}={val[:60]}")
            for key, val in (request.view_args or {}).items():
                parts.insert(0, f"{key}={val}")
            db = get_db()
            db.execute(
                """INSERT INTO audit_log (at, user_id, username, method, endpoint, path, label, detail, result)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (A.now_iso(), user["id"], user["username"], request.method, request.endpoint, request.path,
                 AUDIT_LABELS.get(request.endpoint, request.endpoint), "; ".join(parts)[:500],
                 "відхилено" if rejected else "ok"),
            )
            db.execute("DELETE FROM audit_log WHERE id < (SELECT MAX(id) - 20000 FROM audit_log)")
            db.commit()
        except Exception:
            pass
        return resp

    @app.route("/audit")
    @login_required
    def audit_log_view():
        if not A.is_admin():
            abort(403)
        db = get_db()
        q = request.args.get("q", "").strip()
        user = request.args.get("user", "").strip()
        page = max(request.args.get("page", 1, type=int), 1)
        where, params = ["1=1"], []
        if q:
            where.append("(label LIKE ? OR detail LIKE ? OR path LIKE ?)")
            params += [f"%{q}%"] * 3
        if user:
            where.append("username=?")
            params.append(user)
        rows = db.execute(
            f"SELECT * FROM audit_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 101 OFFSET ?",
            params + [(page - 1) * 100]).fetchall()
        users = [r["username"] for r in db.execute("SELECT DISTINCT username FROM audit_log ORDER BY username")]
        return render_template("audit.html", active="audit", rows=rows[:100], has_next=len(rows) > 100,
                               page=page, q=q, user=user, users=users)

    # ------------------------------------------------------------------ parts catalog
    @app.route("/parts")
    @login_required
    @role_required("sales", "production", "viewer")
    def parts_list():
        db = get_db()
        q = request.args.get("q", "").strip()
        sql = """SELECT p.*, c.name AS client_name FROM parts p LEFT JOIN clients c ON c.id=p.client_id WHERE 1=1"""
        params = []
        if q:
            sql += " AND (p.name LIKE ? OR p.drawing_no LIKE ? OR p.material LIKE ? OR c.name LIKE ?)"
            params = [f"%{q}%"] * 4
        parts = db.execute(sql + " ORDER BY p.name", params).fetchall()
        return render_template("parts_list.html", active="parts", parts=parts, q=q)

    def _part_form_context(db, part):
        return dict(active="parts", part=part, CURRENCIES=A.CURRENCIES,
                    clients=db.execute("SELECT id, name FROM clients ORDER BY name").fetchall())

    @app.route("/parts/new", methods=["GET", "POST"])
    @login_required
    @role_required("sales", "production")
    def parts_new():
        return _part_save(None)

    @app.route("/parts/<int:part_id>/edit", methods=["GET", "POST"])
    @login_required
    @role_required("sales", "production")
    def parts_edit(part_id):
        db = get_db()
        part = db.execute("SELECT * FROM parts WHERE id=?", (part_id,)).fetchone()
        if not part:
            abort(404)
        return _part_save(part)

    def _part_save(part):
        db = get_db()
        if request.method == "POST":
            f = request.form
            name = (f.get("name") or "").strip()
            price = num(f.get("last_price"), None) if f.get("last_price") else None
            currency = f.get("currency") if f.get("currency") in A.CURRENCIES else "UAH"
            if not name:
                flash("Поле «Назва деталі» обов'язкове", "error")
            elif price is not None and price < 0:
                flash("Ціна не може бути від'ємною", "error")
            else:
                values = (name, (f.get("drawing_no") or "").strip() or None, f.get("client_id") or None,
                          (f.get("material") or "").strip() or None, (f.get("routing_text") or "").strip() or None,
                          price, currency, (f.get("notes") or "").strip() or None)
                if part:
                    db.execute("""UPDATE parts SET name=?, drawing_no=?, client_id=?, material=?, routing_text=?,
                                  last_price=?, currency=?, notes=?, updated_at=? WHERE id=?""",
                               values + (A.now_iso(), part["id"]))
                else:
                    db.execute("""INSERT INTO parts (name, drawing_no, client_id, material, routing_text, last_price,
                                  currency, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?)""", values + (A.now_iso(),))
                db.commit()
                flash("Деталь збережено", "success")
                return redirect(url_for("parts_list"))
        return render_template("parts_form.html", **_part_form_context(db, part))

    @app.route("/parts/<int:part_id>/delete", methods=["POST"])
    @login_required
    @role_required("sales", "production")
    def parts_delete(part_id):
        db = get_db()
        db.execute("DELETE FROM parts WHERE id=?", (part_id,))
        db.commit()
        flash("Деталь видалено з каталогу", "success")
        return redirect(url_for("parts_list"))

    @app.route("/parts/<int:part_id>/new-deal", methods=["POST"])
    @login_required
    @role_required("sales")
    def parts_new_deal(part_id):
        db = get_db()
        part = db.execute("SELECT * FROM parts WHERE id=?", (part_id,)).fetchone()
        if not part:
            abort(404)
        if not part["client_id"]:
            flash("Спершу вкажіть клієнта в картці деталі", "error")
            return redirect(url_for("parts_edit", part_id=part_id))
        tech = "\n".join(x for x in [
            f"Креслення: {part['drawing_no']}" if part["drawing_no"] else "",
            f"Матеріал: {part['material']}" if part["material"] else "",
            part["routing_text"] or ""] if x)
        db.execute(
            """INSERT INTO deals (title, client_id, stage, amount, currency, manager_id, tech_requirements,
               created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (part["name"], part["client_id"], "lead", part["last_price"] or 0, part["currency"] or "UAH",
             session.get("user_id"), tech, A.now_iso(), A.now_iso()))
        deal_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        A.log_activity(db, deal_id, part["client_id"], "note", f"Угоду створено з каталогу деталей: «{part['name']}»")
        db.commit()
        A.notify_event(f"🆕 Нова угода з каталогу: «{part['name']}»")
        flash("Угоду створено з даних деталі. Перевірте суму й терміни.", "success")
        return redirect(url_for("deal_detail", deal_id=deal_id))

    # ------------------------------------------------------------------ plan vs fact
    def hour_rate(db):
        v = num(A.get_setting(db, "machine_hour_rate", "800"), 800)
        return v if v > 0 else 800.0

    def order_cost_rows(db):
        rate = hour_rate(db)
        orders = db.execute(
            """SELECT po.id, po.quantity, po.status, d.title, d.amount, d.currency, d.closed_at, d.stage,
                      c.name AS client_name
               FROM production_orders po LEFT JOIN deals d ON d.id=po.deal_id
               LEFT JOIN clients c ON c.id=d.client_id ORDER BY po.id DESC""").fetchall()
        result = []
        fmt = "%Y-%m-%d %H:%M:%S"
        for o in orders:
            ops = db.execute("SELECT * FROM production_operations WHERE production_order_id=?", (o["id"],)).fetchall()
            planned_total = sum(x["planned_hours"] or 0 for x in ops)
            actual = planned_done = 0.0
            done_count = 0
            for x in ops:
                if x["actual_start"] and x["actual_end"]:
                    try:
                        delta = (datetime.datetime.strptime(x["actual_end"], fmt) -
                                 datetime.datetime.strptime(x["actual_start"], fmt)).total_seconds() / 3600
                    except ValueError:
                        continue
                    actual += max(delta, 0)
                    planned_done += x["planned_hours"] or 0
                    done_count += 1
            mats = 0.0
            for t in db.execute(
                    """SELECT -t.qty_delta AS qty, i.unit_cost, i.currency FROM warehouse_transactions t
                       JOIN warehouse_items i ON i.id=t.item_id
                       WHERE t.type='out' AND t.reference LIKE ?""", (f"Виробниче замовлення №{o['id']}:%",)):
                mats += uah(db, (t["qty"] or 0) * (t["unit_cost"] or 0), t["currency"])
            price = uah(db, o["amount"], o["currency"], o["closed_at"])
            plan_cost = planned_total * rate + mats
            fact_cost = actual * rate + mats if done_count else None
            deviation = round((actual - planned_done) / planned_done * 100) if planned_done else None
            result.append(dict(
                id=o["id"], title=o["title"] or f"Замовлення №{o['id']}", client=o["client_name"], status=o["status"],
                planned=round(planned_total, 1), planned_done=round(planned_done, 1), actual=round(actual, 1),
                done_ops=done_count, total_ops=len(ops), deviation=deviation, materials=round(mats),
                price=round(price), plan_cost=round(plan_cost), fact_cost=None if fact_cost is None else round(fact_cost),
                margin_plan=round(price - plan_cost) if price else None,
                margin_fact=round(price - fact_cost) if (price and fact_cost is not None) else None))
        return result, rate

    @app.route("/production/costs")
    @login_required
    @role_required("accountant", "sales")
    def production_costs():
        db = get_db()
        rows, rate = order_cost_rows(db)
        return render_template("production_costs.html", active="costs", rows=rows, rate=rate)

    @app.route("/production/costs/rate", methods=["POST"])
    @login_required
    def production_costs_rate():
        if not A.is_admin():
            abort(403)
        value = num(request.form.get("machine_hour_rate"), 0)
        if value <= 0:
            flash("Вартість години має бути додатною", "error")
        else:
            db = get_db()
            A.set_setting(db, "machine_hour_rate", str(value))
            db.commit()
            flash("Вартість машино-години збережено", "success")
        return redirect(url_for("production_costs"))

    # ------------------------------------------------------------------ purchase request
    def low_stock_items(db):
        items = db.execute(
            """SELECT * FROM warehouse_items WHERE qty_on_hand <= min_qty ORDER BY name""").fetchall()
        out = []
        for it in items:
            suggested = max(round(it["min_qty"] * 2 - it["qty_on_hand"], 2), it["min_qty"] or 1)
            out.append(dict(it, suggested=suggested))
        return out

    @app.route("/warehouse/purchase")
    @login_required
    @role_required("warehouse", "production", "viewer")
    def purchase_request():
        return render_template("purchase.html", active="warehouse", items=low_stock_items(get_db()))

    def _chosen_lines(db):
        lines = []
        for item_id in request.form.getlist("item_id"):
            if request.form.get(f"pick_{item_id}") != "1":
                continue
            qty = num(request.form.get(f"qty_{item_id}"), 0)
            it = db.execute("SELECT * FROM warehouse_items WHERE id=?", (item_id,)).fetchone()
            if it and qty > 0:
                lines.append((it, qty))
        return lines

    @app.route("/warehouse/purchase/export", methods=["POST"])
    @login_required
    @role_required("warehouse", "production")
    def purchase_export():
        from openpyxl import Workbook
        db = get_db()
        lines = _chosen_lines(db)
        if not lines:
            flash("Оберіть хоча б одну позицію з кількістю більше нуля", "error")
            return redirect(url_for("purchase_request"))
        wb = Workbook()
        ws = wb.active
        ws.title = "Заявка"
        ws.append(["Артикул", "Назва", "Од.", "Залишок", "Замовити", "Орієнт. ціна за од.", "Валюта", "Сума"])
        for it, qty in lines:
            ws.append([it["sku"], it["name"], it["unit"], it["qty_on_hand"], qty, it["unit_cost"], it["currency"],
                       round(qty * (it["unit_cost"] or 0), 2)])
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name=f"zayavka_{today()}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.route("/warehouse/purchase/telegram", methods=["POST"])
    @login_required
    @role_required("warehouse", "production")
    def purchase_telegram():
        db = get_db()
        lines = _chosen_lines(db)
        if not lines:
            flash("Оберіть хоча б одну позицію з кількістю більше нуля", "error")
            return redirect(url_for("purchase_request"))
        text = "🛒 Заявка на закупівлю:\n" + "\n".join(
            f"• {it['name']} — {qty:g} {it['unit']}" for it, qty in lines)
        ok1, _ = A.send_webhook_notification(text)
        ok2, _ = A.send_telegram_notification(text)
        flash("Заявку надіслано" if (ok1 or ok2) else
              "Канали сповіщень не налаштовані (Налаштування → Telegram/вебхук). Скористайтесь вивантаженням у Excel.",
              "success" if (ok1 or ok2) else "error")
        return redirect(url_for("purchase_request"))

    # ------------------------------------------------------------------ quality
    @app.route("/quality")
    @login_required
    @role_required("production", "sales", "viewer")
    def quality_list():
        db = get_db()
        since = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        rows = db.execute(
            """SELECT q.*, wc.name AS wc_name, u.full_name AS inspector FROM quality_checks q
               LEFT JOIN work_centers wc ON wc.id=q.work_center_id LEFT JOIN users u ON u.id=q.inspector_user_id
               ORDER BY q.id DESC LIMIT 200""").fetchall()
        stat = db.execute(
            "SELECT COALESCE(SUM(qty_checked),0) c, COALESCE(SUM(qty_defect),0) d FROM quality_checks WHERE created_at>=?",
            (since,)).fetchone()
        by_wc = db.execute(
            """SELECT wc.name, SUM(q.qty_checked) c, SUM(q.qty_defect) d FROM quality_checks q
               JOIN work_centers wc ON wc.id=q.work_center_id WHERE q.created_at>=? GROUP BY wc.id ORDER BY d DESC""",
            (since,)).fetchall()
        pct = round(stat["d"] / stat["c"] * 100, 1) if stat["c"] else 0
        return render_template("quality_list.html", active="quality", rows=rows, checked=stat["c"],
                               defects=stat["d"], pct=pct, by_wc=by_wc, checklist=dict(QUALITY_CHECKLIST))

    @app.route("/quality/new", methods=["GET", "POST"])
    @login_required
    @role_required("production")
    def quality_new():
        db = get_db()
        orders = db.execute(
            """SELECT po.id, d.title, c.name AS client FROM production_orders po LEFT JOIN deals d ON d.id=po.deal_id
               LEFT JOIN clients c ON c.id=d.client_id WHERE po.status!='cancelled' ORDER BY po.id DESC""").fetchall()
        wcs = db.execute("SELECT id, name FROM work_centers ORDER BY name").fetchall()
        if request.method == "POST":
            f = request.form
            checked = int(num(f.get("qty_checked"), -1))
            defect = int(num(f.get("qty_defect"), 0))
            ticked = [k for k, _ in QUALITY_CHECKLIST if f.get(f"ck_{k}") == "1"]
            if checked <= 0:
                flash("Вкажіть, скільки деталей перевірено", "error")
            elif defect < 0 or defect > checked:
                flash("Кількість браку має бути від 0 до кількості перевірених", "error")
            elif defect and not (f.get("defect_reason") or "").strip():
                flash("Вкажіть причину браку", "error")
            else:
                db.execute(
                    """INSERT INTO quality_checks (production_order_id, work_center_id, inspector_user_id, qty_checked,
                       qty_defect, defect_reason, checklist, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (f.get("production_order_id") or None, f.get("work_center_id") or None, session.get("user_id"),
                     checked, defect, (f.get("defect_reason") or "").strip() or None, ",".join(ticked),
                     (f.get("note") or "").strip() or None, A.now_iso()))
                db.commit()
                if defect:
                    A.notify_event(f"⚠️ Брак: {defect} із {checked} шт"
                                   + (f" (замовлення №{f.get('production_order_id')})" if f.get("production_order_id") else "")
                                   + f" — {(f.get('defect_reason') or '').strip()}")
                flash("Перевірку збережено", "success")
                return redirect(url_for("quality_list"))
        return render_template("quality_form.html", active="quality", orders=orders, wcs=wcs, checklist=QUALITY_CHECKLIST)

    # ------------------------------------------------------------------ overdue payments + documents
    def overdue_payments(db):
        scope, params = A.manager_scope_sql("d.manager_id")
        rows = db.execute(
            f"""SELECT p.*, d.title, d.id AS deal_id, c.name AS client_name, c.phone FROM payments p
                JOIN deals d ON d.id=p.deal_id JOIN clients c ON c.id=d.client_id
                WHERE p.status!='paid' AND p.due_date IS NOT NULL AND p.due_date < ?{scope}
                ORDER BY p.due_date""", [today()] + params).fetchall()
        out = []
        for r in rows:
            days = (datetime.date.today() - datetime.date.fromisoformat(r["due_date"][:10])).days
            out.append(dict(r, days=days, amount_uah=round(uah(db, r["amount"], r["currency"]))))
        return out

    @app.route("/payments/overdue")
    @login_required
    @role_required("accountant", "sales")
    def payments_overdue():
        rows = overdue_payments(get_db())
        return render_template("overdue.html", active="finance", rows=rows,
                               total=sum(r["amount_uah"] for r in rows))

    def _pdf_document(deal_id, kind):
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.pdfgen import canvas as pdf_canvas
        db = get_db()
        deal = db.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if not deal:
            abort(404)
        client = db.execute("SELECT * FROM clients WHERE id=?", (deal["client_id"],)).fetchone()
        payment = None
        if request.args.get("payment_id", type=int):
            payment = db.execute("SELECT * FROM payments WHERE id=? AND deal_id=?",
                                 (request.args.get("payment_id", type=int), deal_id)).fetchone()
        amount = payment["amount"] if payment else deal["amount"]
        currency = (payment["currency"] if payment else deal["currency"]) or "UAH"
        symbol = {"USD": "$", "EUR": "€", "UAH": "₴", "PLN": "zł"}.get(currency, currency)
        title = "РАХУНОК" if kind == "invoice" else "АКТ ВИКОНАНИХ РОБІТ"
        company = A.get_setting(db, "company_name", "") or "Оснастка-Маркет"
        details = A.get_setting(db, "company_details", "")
        buf = BytesIO()
        c = pdf_canvas.Canvas(buf, pagesize=A4)
        w, h = A4
        c.setFillColor(colors.HexColor("#2a2e35"))
        c.rect(0, h - 26 * mm, w, 26 * mm, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont(A.PDF_FONT_BOLD, 17)
        c.drawString(20 * mm, h - 16 * mm, company)
        y = h - 40 * mm
        c.setFillColor(colors.black)
        c.setFont(A.PDF_FONT_BOLD, 14)
        c.drawString(20 * mm, y, f"{title} № {deal_id:04d}{'-' + str(payment['id']) if payment else ''}  від  "
                                 f"{datetime.date.today().strftime('%d.%m.%Y')}")
        y -= 11 * mm
        c.setFont(A.PDF_FONT, 10)
        for line in (["Постачальник:"] + (details.splitlines() if details else [company])):
            c.drawString(20 * mm, y, line[:95])
            y -= 5.5 * mm
        y -= 3 * mm
        c.setFont(A.PDF_FONT_BOLD, 10)
        c.drawString(20 * mm, y, "Замовник:")
        c.setFont(A.PDF_FONT, 10)
        c.drawString(45 * mm, y, (client["name"] if client else "—")[:80])
        y -= 5.5 * mm
        if client and client["edrpou"]:
            c.drawString(45 * mm, y, f"ЄДРПОУ: {client['edrpou']}")
            y -= 5.5 * mm
        y -= 8 * mm
        c.setFillColor(colors.HexColor("#eeeeee"))
        c.rect(20 * mm, y - 2 * mm, w - 40 * mm, 8 * mm, fill=1, stroke=0)
        c.setFillColor(colors.black)
        c.setFont(A.PDF_FONT_BOLD, 10)
        c.drawString(22 * mm, y, "Найменування")
        c.drawRightString(w - 22 * mm, y, "Сума")
        y -= 9 * mm
        c.setFont(A.PDF_FONT, 10)
        label = deal["title"]
        if payment:
            label += f" — {dict(A.PAYMENT_KINDS).get(payment['kind'], payment['kind'])}" if hasattr(A, "PAYMENT_KINDS") else ""
        c.drawString(22 * mm, y, label[:75])
        c.drawRightString(w - 22 * mm, y, f"{amount:,.2f} {symbol}".replace(",", " "))
        y -= 12 * mm
        c.setFont(A.PDF_FONT_BOLD, 12)
        c.drawRightString(w - 22 * mm, y, f"Разом до сплати: {amount:,.2f} {symbol}".replace(",", " ")
                          if kind == "invoice" else f"Вартість робіт: {amount:,.2f} {symbol}".replace(",", " "))
        y -= 20 * mm
        c.setFont(A.PDF_FONT, 10)
        if kind == "act":
            c.drawString(20 * mm, y, "Роботи виконано в повному обсязі, замовник претензій щодо якості не має.")
            y -= 18 * mm
            c.drawString(20 * mm, y, "Виконавець: ____________________")
            c.drawString(115 * mm, y, "Замовник: ____________________")
        else:
            c.drawString(20 * mm, y, "Рахунок дійсний до оплати протягом 5 банківських днів.")
        c.showPage()
        c.save()
        buf.seek(0)
        return send_file(buf, mimetype="application/pdf", download_name=f"{kind}_{deal_id}.pdf")

    @app.route("/deals/<int:deal_id>/invoice.pdf")
    @login_required
    @role_required("sales", "accountant")
    def deal_invoice_pdf(deal_id):
        return _pdf_document(deal_id, "invoice")

    @app.route("/deals/<int:deal_id>/act.pdf")
    @login_required
    @role_required("sales", "accountant")
    def deal_act_pdf(deal_id):
        return _pdf_document(deal_id, "act")

    # ------------------------------------------------------------------ repeat orders
    def reorder_candidates(db):
        days = int(num(A.get_setting(db, "reorder_days", "60"), 60)) or 60
        cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        scope, params = A.manager_scope_sql("c.manager_id")
        rows = db.execute(
            f"""SELECT c.id, c.name, c.phone, c.email, MAX(d.closed_at) AS last_won, COUNT(d.id) AS won_cnt
                FROM clients c JOIN deals d ON d.client_id=c.id AND d.stage='won'
                WHERE c.status='active'{scope}
                  AND NOT EXISTS (SELECT 1 FROM deals o WHERE o.client_id=c.id AND o.stage NOT IN ('won','lost'))
                GROUP BY c.id HAVING MAX(d.closed_at) < ? ORDER BY last_won""", params + [cutoff]).fetchall()
        out = []
        for r in rows:
            quiet = (datetime.date.today() - datetime.date.fromisoformat(r["last_won"][:10])).days
            out.append(dict(r, quiet_days=quiet))
        return out, days

    @app.route("/clients/reorder")
    @login_required
    @role_required("sales")
    def clients_reorder():
        rows, days = reorder_candidates(get_db())
        return render_template("reorder.html", active="clients", rows=rows, days=days)

    @app.route("/clients/<int:client_id>/reorder-task", methods=["POST"])
    @login_required
    @role_required("sales")
    def clients_reorder_task(client_id):
        db = get_db()
        client = db.execute("SELECT id, name FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client:
            abort(404)
        A.create_task(db, None, client_id, f"Зателефонувати: {client['name']} — запропонувати повторне замовлення",
                      "call", 1)
        db.commit()
        flash("Задачу створено на завтра", "success")
        return redirect(url_for("clients_reorder"))

    # ------------------------------------------------------------------ load board
    @app.route("/production/board")
    @login_required
    @role_required("production", "sales", "viewer")
    def production_board():
        db = get_db()
        try:
            start = datetime.date.fromisoformat(request.args.get("start", today()))
        except ValueError:
            start = datetime.date.today()
        days = [start + datetime.timedelta(days=i) for i in range(14)]
        wcs = db.execute("SELECT * FROM work_centers ORDER BY id").fetchall()
        ops = db.execute(
            """SELECT op.*, po.id AS order_id, d.title AS deal_title FROM production_operations op
               JOIN production_orders po ON po.id=op.production_order_id LEFT JOIN deals d ON d.id=po.deal_id
               WHERE op.status!='done' AND po.status!='cancelled'""").fetchall()
        cells, unscheduled = {}, []
        for op in ops:
            if not op["planned_start"] or not op["work_center_id"]:
                unscheduled.append(op)
                continue
            cells.setdefault((op["work_center_id"], op["planned_start"][:10]), []).append(op)
        grid = []
        for wc in wcs:
            row = []
            for day in days:
                items = cells.get((wc["id"], day.isoformat()), [])
                hours = round(sum(i["planned_hours"] or 0 for i in items), 1)
                row.append(dict(date=day.isoformat(), items=items, hours=hours, over=hours > (wc["capacity_hours_per_day"] or 8)))
            grid.append(dict(wc=wc, cells=row))
        fw = A.foreman_work_center_id(A.get_current_user())
        can_move = A.get_current_user()["role"] in ("admin", "production")
        return render_template("board.html", active="production", days=days, grid=grid, unscheduled=unscheduled,
                               prev_start=(start - datetime.timedelta(days=7)).isoformat(),
                               next_start=(start + datetime.timedelta(days=7)).isoformat(),
                               can_move=can_move, foreman_wc=fw, wcs=wcs)

    @app.route("/production/operations/<int:op_id>/move", methods=["POST"])
    @login_required
    @role_required("production")
    def operation_move(op_id):
        db = get_db()
        op = db.execute("SELECT * FROM production_operations WHERE id=?", (op_id,)).fetchone()
        wants_json = request.headers.get("X-Requested-With") == "fetch"

        def fail(msg, code=400):
            if wants_json:
                return jsonify(ok=False, error=msg), code
            flash(msg, "error")
            return redirect(url_for("production_board"))

        if not op:
            abort(404)
        if op["status"] != "waiting":
            return fail("Переносити можна лише операції, що ще не почались")
        try:
            day = datetime.date.fromisoformat(request.form.get("date", ""))
        except ValueError:
            return fail("Некоректна дата")
        wc_id = request.form.get("work_center_id", type=int) or op["work_center_id"]
        if not db.execute("SELECT 1 FROM work_centers WHERE id=?", (wc_id,)).fetchone():
            return fail("Невідомий робочий центр")
        fw = A.foreman_work_center_id(A.get_current_user())
        if fw and (op["work_center_id"] != fw or wc_id != fw):
            return fail("Майстер може переносити операції лише на своїй дільниці", 403)
        old_time = (op["planned_start"] or "")[11:19] or "08:00:00"
        start = datetime.datetime.strptime(f"{day.isoformat()} {old_time}", "%Y-%m-%d %H:%M:%S")
        end = start + datetime.timedelta(hours=op["planned_hours"] or 1)
        db.execute("UPDATE production_operations SET work_center_id=?, planned_start=?, planned_end=? WHERE id=?",
                   (wc_id, start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), op_id))
        db.commit()
        if wants_json:
            return jsonify(ok=True)
        flash("Операцію перенесено", "success")
        return redirect(request.referrer or url_for("production_board"))

    # ------------------------------------------------------------------ dashboard globals
    def overdue_summary():
        try:
            if not A.get_current_user():
                return {"count": 0, "total": 0}
            rows = overdue_payments(get_db())
            return {"count": len(rows), "total": sum(r["amount_uah"] for r in rows)}
        except Exception:
            return {"count": 0, "total": 0}

    def reorder_count():
        try:
            return len(reorder_candidates(get_db())[0])
        except Exception:
            return 0

    app.jinja_env.globals["overdue_summary"] = overdue_summary
    app.jinja_env.globals["reorder_count"] = reorder_count

    # ------------------------------------------------------------------ daily reminders (called from app.run_daily_tasks)
    def send_overdue_reminder():
        """Раз на день одним повідомленням: прострочені оплати."""
        with app.app_context():
            db = get_db()
            if A.get_setting(db, "notify_overdue", "1") != "1" or A.get_setting(db, "overdue_notified_on", "") == today():
                return False
            rows = db.execute(
                """SELECT p.amount, p.currency, p.due_date, d.title, c.name FROM payments p JOIN deals d ON d.id=p.deal_id
                   JOIN clients c ON c.id=d.client_id WHERE p.status!='paid' AND p.due_date IS NOT NULL AND p.due_date < ?
                   ORDER BY p.due_date""", (today(),)).fetchall()
            A.set_setting(db, "overdue_notified_on", today())
            db.commit()
            if not rows:
                return False
            lines = [f"• {r['name']} — {r['title']}: {r['amount']:,.0f} {r['currency']} (до {r['due_date']})".replace(",", " ")
                     for r in rows[:15]]
            A.notify_all(f"💸 Прострочені оплати ({len(rows)}):\n" + "\n".join(lines))
            return True

    A.send_overdue_reminder = send_overdue_reminder

    # ------------------------------------------------------------------ templates
    from features_templates import TEMPLATES
    app.jinja_loader.mapping.update(TEMPLATES)
