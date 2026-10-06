# -*- coding: utf-8 -*-
"""Шаблони для features_ext (Jinja-рядки)."""

def _page(title, heading, sub, body):
    return ('{% extends "base.html" %}{% block title %}' + title + '{% endblock %}{% block heading %}' + heading +
            '{% endblock %}{% block subheading %}' + sub + '{% endblock %}{% block content %}' + body + '{% endblock %}')

AUDIT = _page("Журнал змін", "Журнал змін", "Хто і що змінював у системі", """
<form class="toolbar" method="get" style="display:flex;gap:8px;margin-bottom:12px">
  <input name="q" value="{{ q }}" placeholder="Пошук за дією чи змістом" style="max-width:280px">
  <select name="user" style="max-width:180px"><option value="">Усі користувачі</option>
  {% for u in users %}<option value="{{ u }}" {{ 'selected' if u==user }}>{{ u }}</option>{% endfor %}</select>
  <button class="btn btn-primary btn-sm">Фільтр</button></form>
<div class="card table-wrap"><table id="audit-table">
<tr><th>Час</th><th>Користувач</th><th>Дія</th><th>Деталі</th><th>Результат</th></tr>
{% for r in rows %}<tr><td>{{ r['at'] }}</td><td>{{ r['username'] }}</td><td>{{ r['label'] }}</td>
<td style="color:var(--text-dim);font-size:12px">{{ r['detail'] }}</td><td>{{ r['result'] }}</td></tr>
{% else %}<tr><td colspan="5" class="empty">Записів ще немає</td></tr>{% endfor %}</table></div>
<div style="margin-top:12px;display:flex;gap:8px">
{% if page>1 %}<a class="btn btn-sm" href="?page={{ page-1 }}&q={{ q }}&user={{ user }}">← Назад</a>{% endif %}
{% if has_next %}<a class="btn btn-sm" href="?page={{ page+1 }}&q={{ q }}&user={{ user }}">Далі →</a>{% endif %}</div>""")

PARTS_LIST = _page("Каталог деталей", "Каталог деталей", "Креслення, матеріал, техпроцес і остання ціна", """
<div class="toolbar" style="display:flex;gap:8px;margin-bottom:12px">
<form method="get" style="display:flex;gap:8px"><input name="q" value="{{ q }}" placeholder="Назва, креслення, матеріал, клієнт"><button class="btn btn-sm">Знайти</button></form>
{% if current_user['role'] in ('admin','sales','production') %}<a class="btn btn-primary" href="{{ url_for('parts_new') }}">+ Нова деталь</a>{% endif %}</div>
<div class="card table-wrap"><table id="parts-table">
<tr><th>Назва</th><th>Креслення</th><th>Клієнт</th><th>Матеріал</th><th>Остання ціна</th><th></th></tr>
{% for p in parts %}<tr><td>{{ p['name'] }}</td><td>{{ p['drawing_no'] or '—' }}</td><td>{{ p['client_name'] or '—' }}</td>
<td>{{ p['material'] or '—' }}</td><td>{% if p['last_price'] %}{{ p['last_price'] | money(p['currency']) }}{% else %}—{% endif %}</td>
<td style="white-space:nowrap">{% if current_user['role'] in ('admin','sales','production') %}<a class="btn btn-sm" href="{{ url_for('parts_edit', part_id=p['id']) }}">Редагувати</a>{% endif %}
{% if current_user['role'] in ('admin','sales') %}<form method="post" action="{{ url_for('parts_new_deal', part_id=p['id']) }}" style="display:inline"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm btn-primary">→ Угода</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan="6" class="empty">Каталог порожній</td></tr>{% endfor %}</table></div>""")

PARTS_FORM = _page("Деталь", "Деталь", "Картка деталі в каталозі", """
<form method="post" class="card" style="padding:20px;display:grid;gap:12px;max-width:720px">
<input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
<div class="field"><label>Назва деталі *</label><input name="name" value="{{ part['name'] if part }}" required></div>
<div class="field"><label>Номер креслення</label><input name="drawing_no" value="{{ part['drawing_no'] or '' if part }}"></div>
<div class="field"><label>Клієнт</label><select name="client_id"><option value="">—</option>
{% for c in clients %}<option value="{{ c['id'] }}" {{ 'selected' if part and part['client_id']==c['id'] }}>{{ c['name'] }}</option>{% endfor %}</select></div>
<div class="field"><label>Матеріал</label><input name="material" value="{{ part['material'] or '' if part }}"></div>
<div class="field"><label>Техпроцес (операції)</label><textarea name="routing_text" rows="4">{{ part['routing_text'] or '' if part }}</textarea></div>
<div class="field"><label>Остання ціна</label><div style="display:flex;gap:8px"><input name="last_price" value="{{ part['last_price'] if part and part['last_price'] is not none }}"><select name="currency" style="max-width:100px">
{% for c in CURRENCIES %}<option {{ 'selected' if part and part['currency']==c }}>{{ c }}</option>{% endfor %}</select></div></div>
<div class="field"><label>Примітки</label><textarea name="notes" rows="2">{{ part['notes'] or '' if part }}</textarea></div>
<div style="display:flex;gap:8px"><button class="btn btn-primary">Зберегти</button><a class="btn btn-ghost" href="{{ url_for('parts_list') }}">Скасувати</a></div></form>
{% if part and current_user['role'] in ('admin','sales','production') %}<form method="post" action="{{ url_for('parts_delete', part_id=part['id']) }}" style="margin-top:12px" onsubmit="return confirm('Видалити деталь з каталогу?')"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm">Видалити</button></form>{% endif %}""")

COSTS = _page("План/факт", "План / факт по замовленнях", "Години, матеріали та маржа (за ціною угоди)", """
<div class="card" style="padding:14px;margin-bottom:12px;display:flex;gap:12px;align-items:center">Вартість машино-години: <b>{{ rate|round|int }} грн</b>
{% if current_user['role']=='admin' %}<form method="post" action="{{ url_for('production_costs_rate') }}" style="display:flex;gap:6px"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><input name="machine_hour_rate" value="{{ rate }}" style="width:100px"><button class="btn btn-sm">Змінити</button></form>{% endif %}</div>
<div class="card table-wrap"><table id="costs-table">
<tr><th>№</th><th>Замовлення</th><th>План, год</th><th>Факт, год</th><th>Відхилення</th><th>Матеріали, грн</th><th>Ціна, грн</th><th>Маржа план</th><th>Маржа факт</th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('production_detail', order_id=r.id) }}">{{ r.id }}</a></td><td>{{ r.title }}<br><small>{{ r.client or '' }}</small></td>
<td>{{ r.planned }}</td><td>{{ r.actual }} ({{ r.done_ops }}/{{ r.total_ops }} оп.)</td>
<td style="color:{{ 'var(--red)' if r.deviation and r.deviation>10 else 'inherit' }}">{{ ('%+d%%' % r.deviation) if r.deviation is not none else '—' }}</td>
<td>{{ r.materials }}</td><td>{{ r.price }}</td><td>{{ r.margin_plan if r.margin_plan is not none else '—' }}</td><td>{{ r.margin_fact if r.margin_fact is not none else '—' }}</td></tr>
{% else %}<tr><td colspan="9" class="empty">Немає виробничих замовлень</td></tr>{% endfor %}</table></div>""")

PURCHASE = _page("Заявка на закупівлю", "Заявка на закупівлю", "Позиції, що досягли мінімального залишку", """
{% if items %}<form method="post" class="card table-wrap" id="purchase-form"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
<table><tr><th></th><th>Назва</th><th>Залишок</th><th>Мін.</th><th>Замовити</th></tr>
{% for i in items %}<tr><td><input type="hidden" name="item_id" value="{{ i['id'] }}"><input type="checkbox" name="pick_{{ i['id'] }}" value="1" checked></td>
<td>{{ i['name'] }}</td><td>{{ i['qty_on_hand'] }} {{ i['unit'] }}</td><td>{{ i['min_qty'] }}</td>
<td><input name="qty_{{ i['id'] }}" value="{{ i['suggested'] }}" style="width:90px"> {{ i['unit'] }}</td></tr>{% endfor %}</table>
{% if current_user['role'] in ('admin','warehouse','production') %}<div style="padding:12px;display:flex;gap:8px">
<button class="btn btn-primary" formaction="{{ url_for('purchase_export') }}">⬇ Excel</button>
<button class="btn" formaction="{{ url_for('purchase_telegram') }}">✈ Надіслати в Telegram</button></div>{% endif %}</form>
{% else %}<div class="card empty" style="padding:24px">Усі залишки в нормі 👍</div>{% endif %}""")

QUALITY_LIST = _page("Контроль якості", "Контроль якості", "Перевірки та облік браку за 30 днів", """
<div style="display:flex;gap:12px;margin-bottom:12px"><div class="card stat" style="padding:14px">Перевірено<b>{{ checked }}</b></div>
<div class="card stat" style="padding:14px">Брак<b>{{ defects }}</b></div><div class="card stat" style="padding:14px">% браку<b id="defect-pct">{{ pct }}%</b></div>
{% if current_user['role'] in ('admin','production') %}<a class="btn btn-primary" style="margin-left:auto;align-self:center" href="{{ url_for('quality_new') }}">+ Перевірка</a>{% endif %}</div>
{% if by_wc %}<div class="card" style="padding:12px;margin-bottom:12px">{% for w in by_wc %}<span class="tag">{{ w['name'] }}: брак {{ w['d'] }} з {{ w['c'] }}</span> {% endfor %}</div>{% endif %}
<div class="card table-wrap"><table id="quality-table"><tr><th>Дата</th><th>Замовлення</th><th>Верстат</th><th>Перевірено</th><th>Брак</th><th>Причина</th><th>Інспектор</th></tr>
{% for r in rows %}<tr><td>{{ r['created_at'][:16] }}</td><td>{{ r['production_order_id'] or '—' }}</td><td>{{ r['wc_name'] or '—' }}</td><td>{{ r['qty_checked'] }}</td>
<td style="color:{{ 'var(--red)' if r['qty_defect'] else 'inherit' }}">{{ r['qty_defect'] }}</td><td>{{ r['defect_reason'] or '' }}</td><td>{{ r['inspector'] or '' }}</td></tr>
{% else %}<tr><td colspan="7" class="empty">Перевірок ще не було</td></tr>{% endfor %}</table></div>""")

QUALITY_FORM = _page("Нова перевірка", "Нова перевірка якості", "Чек-лист і облік браку", """
<form method="post" class="card" style="padding:20px;display:grid;gap:12px;max-width:640px"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
<div class="field"><label>Виробниче замовлення</label><select name="production_order_id"><option value="">—</option>
{% for o in orders %}<option value="{{ o['id'] }}">№{{ o['id'] }} — {{ o['title'] or '' }} ({{ o['client'] or '' }})</option>{% endfor %}</select></div>
<div class="field"><label>Верстат</label><select name="work_center_id"><option value="">—</option>{% for w in wcs %}<option value="{{ w['id'] }}">{{ w['name'] }}</option>{% endfor %}</select></div>
<div class="field"><label>Перевірено, шт *</label><input name="qty_checked" type="number" min="1" required></div>
<div class="field"><label>З них брак, шт</label><input name="qty_defect" type="number" min="0" value="0"></div>
<div class="field"><label>Причина браку</label><input name="defect_reason"></div>
<div class="field"><label>Чек-лист</label>{% for k,l in checklist %}<label style="display:block"><input type="checkbox" name="ck_{{ k }}" value="1"> {{ l }}</label>{% endfor %}</div>
<div class="field"><label>Примітка</label><textarea name="note" rows="2"></textarea></div>
<button class="btn btn-primary">Зберегти</button></form>""")

OVERDUE = _page("Прострочені оплати", "Прострочені оплати", "Платежі, строк яких минув", """
<div class="card" style="padding:14px;margin-bottom:12px">Загалом прострочено: <b id="overdue-total">{{ total | money('UAH') }}</b> ({{ rows|length }})</div>
<div class="card table-wrap"><table id="overdue-table"><tr><th>Клієнт</th><th>Угода</th><th>Сума</th><th>Строк</th><th>Прострочено</th><th>Документи</th></tr>
{% for r in rows %}<tr><td>{{ r['client_name'] }}<br><small>{{ r['phone'] or '' }}</small></td><td><a href="{{ url_for('deal_detail', deal_id=r['deal_id']) }}">{{ r['title'] }}</a></td>
<td>{{ r['amount'] | money(r['currency']) }}</td><td>{{ r['due_date'] }}</td><td style="color:var(--red)">{{ r['days'] }} дн.</td>
<td><a class="btn btn-sm" target="_blank" href="{{ url_for('deal_invoice_pdf', deal_id=r['deal_id'], payment_id=r['id']) }}">Рахунок</a></td></tr>
{% else %}<tr><td colspan="6" class="empty">Прострочених оплат немає 👍</td></tr>{% endfor %}</table></div>""")

REORDER = _page("Повторні замовлення", "Повторні замовлення", "Клієнти, які давно не замовляли (понад {{ days }} дн.)", """
<div class="card table-wrap"><table id="reorder-table"><tr><th>Клієнт</th><th>Виграних угод</th><th>Остання</th><th>Тиша, дн.</th><th></th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('client_detail', client_id=r['id']) }}">{{ r['name'] }}</a><br><small>{{ r['phone'] or '' }}</small></td><td>{{ r['won_cnt'] }}</td><td>{{ r['last_won'][:10] }}</td><td>{{ r['quiet_days'] }}</td>
<td><form method="post" action="{{ url_for('clients_reorder_task', client_id=r['id']) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm btn-primary">Створити задачу</button></form></td></tr>
{% else %}<tr><td colspan="5" class="empty">Усі активні клієнти нещодавно замовляли</td></tr>{% endfor %}</table></div>""")

BOARD = _page("Дошка завантаження", "Завантаження верстатів", "14 днів: години проти потужності. Перетягніть операцію на іншу дату чи верстат", """
<div style="display:flex;gap:8px;margin-bottom:12px"><a class="btn btn-sm" href="?start={{ prev_start }}">← Тиждень</a><a class="btn btn-sm" href="?start={{ next_start }}">Тиждень →</a></div>
<div class="card table-wrap"><table id="board-table" style="font-size:12px"><tr><th>Верстат</th>{% for d in days %}<th>{{ d.strftime('%d.%m') }}</th>{% endfor %}</tr>
{% for g in grid %}<tr><td><b>{{ g.wc['name'] }}</b></td>{% for c in g.cells %}
<td class="board-cell" data-date="{{ c.date }}" data-wc="{{ g.wc['id'] }}" style="min-width:90px;vertical-align:top;background:{{ 'rgba(224,80,80,.18)' if c.over else 'transparent' }}">
<div style="color:var(--text-dim)">{{ c.hours }} / {{ g.wc['capacity_hours_per_day'] }} г</div>
{% for o in c['items'] %}<div class="board-op" {% if can_move and o['status']=='waiting' %}draggable="true"{% endif %} data-op="{{ o['id'] }}" style="background:var(--accent);color:#000;border-radius:4px;padding:2px 4px;margin:2px 0">№{{ o['order_id'] }} {{ o['operation_name'] }}</div>{% endfor %}</td>{% endfor %}</tr>{% endfor %}</table></div>
{% if unscheduled %}<div class="card" style="padding:12px;margin-top:12px"><b>Без дати / верстата:</b> {% for o in unscheduled %}<span class="tag">№{{ o['order_id'] }} {{ o['operation_name'] }}</span> {% endfor %}</div>{% endif %}
<script>
(function(){var tok=document.querySelector('meta[name=csrf-token]');var drag=null;
document.querySelectorAll('.board-op[draggable=true]').forEach(function(e){e.addEventListener('dragstart',function(){drag=e.dataset.op;});});
document.querySelectorAll('.board-cell').forEach(function(c){c.addEventListener('dragover',function(ev){ev.preventDefault();});
c.addEventListener('drop',function(ev){ev.preventDefault();if(!drag)return;var fd=new URLSearchParams();fd.set('date',c.dataset.date);fd.set('work_center_id',c.dataset.wc);
fd.set('_csrf_token','{{ csrf_token() }}');
fetch('/production/operations/'+drag+'/move',{method:'POST',headers:{'X-Requested-With':'fetch','X-CSRF-Token':'{{ csrf_token() }}'},body:fd}).then(function(r){return r.json();}).then(function(j){if(j.ok)location.reload();else alert(j.error);});});});})();
</script>""")

TEMPLATES = {
    "audit.html": AUDIT, "parts_list.html": PARTS_LIST, "parts_form.html": PARTS_FORM,
    "production_costs.html": COSTS, "purchase.html": PURCHASE, "quality_list.html": QUALITY_LIST,
    "quality_form.html": QUALITY_FORM, "overdue.html": OVERDUE, "reorder.html": REORDER, "board.html": BOARD,
}
