# -*- coding: utf-8 -*-
"""Шаблони розділу «Замовлення», закупівлі заготовок і бібліотеки техпроцесів."""


def _page(title, heading, sub, body):
    return ('{% extends "base.html" %}{% block title %}' + title + '{% endblock %}{% block heading %}' + heading +
            '{% endblock %}{% block subheading %}' + sub + '{% endblock %}{% block content %}' + body + '{% endblock %}')


STYLE = """<style>
.o-chip{display:inline-block;padding:3px 9px;border-radius:12px;font-size:11.5px;font-weight:600;border:1px solid;white-space:nowrap}
.o-flow{display:flex;gap:6px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--text-dim)}
.o-item{border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:14px;background:var(--panel)}
.o-item h3{margin:0;font-size:16px}
.o-cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-top:12px}
.o-box{border:1px solid var(--border);border-radius:10px;padding:12px}
.o-box h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-dim)}
.o-box.done{border-color:rgba(63,178,127,.5)}
.o-box.todo{border-color:rgba(232,163,61,.6)}
.o-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}
.o-row input,.o-row select{flex:1;min-width:140px}
.o-bar{height:6px;background:var(--border);border-radius:4px;overflow:hidden;margin-top:6px}.o-bar i{display:block;height:100%;background:var(--accent)}
.o-late{color:#e5534b;font-weight:600}
.items-table input,.items-table select{width:100%;min-width:60px}
.items-table td{padding:4px}
</style>"""

ORDERS_LIST = _page("Замовлення", "Замовлення", "Службові записки: маркетинг → склад і технолог → виробництво", STYLE + """
<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center">
<form method="get" style="display:flex;gap:8px;flex-wrap:wrap">
<input type="text" name="q" value="{{ q }}" placeholder="№, замовник, деталь, креслення, шифр" style="min-width:280px">
<select name="show" style="max-width:160px"><option value="active" {{ 'selected' if show!='all' }}>Лише в роботі</option><option value="all" {{ 'selected' if show=='all' }}>Усі</option></select>
<button class="btn btn-sm">Знайти</button></form>
{% if current_user['role'] in ('admin','sales') %}<a class="btn btn-primary" href="{{ url_for('orders_new') }}">+ Нове замовлення</a>{% endif %}
</div>
<div class="o-flow" style="margin-bottom:14px">📑 Маркетинг <span>→</span> 📦 Склад (заготовка) <span>+</span> 🧭 Технолог (техпроцес) <span>→</span> 🏭 Виробництво</div>
{% for e in orders %}{% set o = e.o %}
<a href="{{ url_for('orders_detail', order_id=o['id']) }}" style="text-decoration:none;color:inherit;display:block">
<div class="o-item">
 <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap">
  <h3>№ {{ o['number'] }} · {{ o['client_name'] or '—' }}</h3>
  <div style="font-size:12.5px;color:var(--text-dim)">від {{ o['doc_date'] or '' }}{% if e.due %} · найближчий термін <span class="{{ 'o-late' if e.overdue }}">{{ e.due }}</span>{% endif %}{% if money and e.totals %} · <b style="color:var(--text)">{% for cur, v in e.totals.items() %}{{ v|money(cur) }}{% if not loop.last %} + {% endif %}{% endfor %}</b>{% endif %}{% if o['status']!='active' %} · <b>{{ 'закрито' if o['status']=='closed' else 'скасовано' }}</b>{% endif %}</div>
 </div>
 <div style="margin-top:8px;display:grid;gap:6px">
 {% for i in e['items'] %}<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px">
  <b style="min-width:70px">{{ i.code }}</b><span style="flex:1;min-width:180px">{{ i.name }} <span style="color:var(--text-dim)">{{ i.drawing_no or '' }}</span> · {{ '%g'|format(i.quantity) }} {{ i.unit }}</span>
  <span class="o-chip" style="color:{{ i.stage_color }};border-color:{{ i.stage_color }}">{{ i.stage_label }}</span>
 </div>{% endfor %}
 </div>
</div></a>
{% else %}<div class="card card-pad empty">Замовлень ще немає{% if current_user['role'] in ('admin','sales') %} — натисніть «+ Нове замовлення»{% endif %}.</div>{% endfor %}
""")

ORDER_FORM = _page("Замовлення", "{{ 'Замовлення № ' ~ order['number'] if order else 'Нове замовлення' }}",
                   "Як службова записка: № (шифр), замовник, позиції з кресленням, кількістю і терміном", STYLE + """
{% set fv = form or {} %}
<form method="post" class="card card-pad" id="order-form">
<input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px">
 <div class="field"><label>№ замовлення (шифр) *</label><input type="text" name="number" required placeholder="напр. 193-6" value="{{ fv.get('number') or (order['number'] if order else '') }}"></div>
 <div class="field"><label>Дата записки</label><input type="date" name="doc_date" value="{{ fv.get('doc_date') or (order['doc_date'] if order else today) }}"></div>
 <div class="field" style="grid-column:1/-1"><label>Замовник (одержувач) *</label><select name="client_id" required><option value="">— оберіть —</option>
 {% for c in clients %}<option value="{{ c['id'] }}" {{ 'selected' if (fv.get('client_id')|string)==(c['id']|string) or (order and order['client_id']==c['id']) }}>{{ c['name'] }}</option>{% endfor %}</select>
 <div style="font-size:11px;color:var(--text-dim);margin-top:4px">Немає в списку? <a href="{{ url_for('client_new') }}" target="_blank" style="color:var(--accent-2)">Додати клієнта</a> і оновіть сторінку.</div></div>
</div>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:12px">
 <div class="field"><label>Контактна особа замовника</label><input type="text" name="contact_person" placeholder="ПІБ, телефон" value="{{ fv.get('contact_person') or (order['contact_person'] or '' if order else '') }}"></div>
 <div class="field"><label>Умови оплати</label><input type="text" name="payment_terms" placeholder="напр. 50% передоплата, решта після відвантаження" value="{{ fv.get('payment_terms') or (order['payment_terms'] or '' if order else '') }}"></div>
 <div class="field"><label>Доставка</label><input type="text" name="delivery" placeholder="самовивіз / НП, адреса" value="{{ fv.get('delivery') or (order['delivery'] or '' if order else '') }}"></div>
</div>
<h3 style="margin:18px 0 8px;font-size:15px">Позиції</h3>
<div class="table-wrap"><table class="items-table" style="width:100%">
<tr><th style="width:34px">№</th><th style="min-width:220px">Назва *</th><th style="min-width:150px">Креслення</th><th style="width:70px">Од.</th><th style="width:90px">К-сть *</th><th style="width:150px">Термін</th><th style="width:90px">Партійно</th><th style="width:120px">Шифр</th><th style="width:110px">Ціна за од.</th><th style="width:80px">Валюта</th><th style="width:40px"></th></tr>
<tbody id="item-rows">
{% for it in items %}<tr>
 <td class="ln">{{ loop.index }}<input type="hidden" name="items-{{ loop.index0 }}-id" value="{{ it['id'] }}"></td>
 <td><input type="text" name="items-{{ loop.index0 }}-name" value="{{ it['name'] }}" required></td>
 <td><input type="text" name="items-{{ loop.index0 }}-drawing" value="{{ it['drawing_no'] or '' }}" placeholder="кр.322100.022"></td>
 <td><input type="text" name="items-{{ loop.index0 }}-unit" value="{{ it['unit'] or 'шт' }}"></td>
 <td><input name="items-{{ loop.index0 }}-qty" type="number" step="any" min="0" value="{{ '%g'|format(it['quantity']) }}" required></td>
 <td><input name="items-{{ loop.index0 }}-due" type="date" value="{{ it['due_date'] or '' }}"></td>
 <td style="text-align:center"><input name="items-{{ loop.index0 }}-batch" type="checkbox" value="1" style="width:auto" {{ 'checked' if it['batch_delivery'] }}></td>
 <td><input type="text" name="items-{{ loop.index0 }}-code" value="{{ it['code'] or '' }}"></td>
 <td><input name="items-{{ loop.index0 }}-price" type="number" step="0.01" min="0" value="{{ '%g'|format(it['price']) if it['price'] is not none else '' }}"></td>
 <td><select name="items-{{ loop.index0 }}-currency">{% for c in CURRENCIES %}<option {{ 'selected' if (it['currency'] or 'UAH')==c }}>{{ c }}</option>{% endfor %}</select></td>
 <td>{% if not it['production_order_id'] %}<button type="button" class="btn btn-sm btn-ghost" onclick="this.closest('tr').remove();renum()">✕</button>{% else %}🏭{% endif %}</td>
</tr>{% endfor %}
</tbody></table></div>
<button type="button" class="btn btn-sm" onclick="addRow()" style="margin-top:8px">+ Позиція</button>
<div style="font-size:11.5px;color:var(--text-dim);margin-top:6px">Шифр порожній — заповниться сам: № замовлення + номер позиції (193-6-1). «Партійно» — віддавати партіями; дата тоді означає кінцевий термін.</div>
<div class="field" style="margin-top:14px"><label>Примітки</label><textarea name="notes" rows="2">{{ fv.get('notes') or (order['notes'] or '' if order else '') }}</textarea></div>
<div style="display:flex;gap:8px;margin-top:14px"><button class="btn btn-primary">{{ 'Зберегти' if order else 'Створити й передати на склад і технологу' }}</button>
<a class="btn btn-ghost" href="{{ url_for('orders_detail', order_id=order['id']) if order else url_for('orders_list') }}">Скасувати</a></div>
</form>
<script>
var nextIdx = {{ items|length }};
function addRow(){
  var i = nextIdx++, tb = document.getElementById('item-rows'), tr = document.createElement('tr');
  tr.innerHTML = '<td class="ln"></td>'+
   '<td><input type="text" name="items-'+i+'-name" required></td>'+
   '<td><input type="text" name="items-'+i+'-drawing" placeholder="кр.322100.022"></td>'+
   '<td><input type="text" name="items-'+i+'-unit" value="шт"></td>'+
   '<td><input name="items-'+i+'-qty" type="number" step="any" min="0" required></td>'+
   '<td><input name="items-'+i+'-due" type="date"></td>'+
   '<td style="text-align:center"><input name="items-'+i+'-batch" type="checkbox" value="1" style="width:auto"></td>'+
   '<td><input type="text" name="items-'+i+'-code" placeholder="авто"></td>'+
   '<td><input name="items-'+i+'-price" type="number" step="0.01" min="0"></td>'+
   '<td><select name="items-'+i+'-currency">{% for c in CURRENCIES %}<option>{{ c }}</option>{% endfor %}</select></td>'+
   '<td><button type="button" class="btn btn-sm btn-ghost" onclick="this.closest(\\'tr\\').remove();renum()">✕</button></td>';
  tb.appendChild(tr); renum(); tr.querySelector('input').focus();
}
function renum(){ document.querySelectorAll('#item-rows tr').forEach(function(tr,k){ var c=tr.querySelector('.ln'); if(c){ var h=c.querySelector('input'); c.textContent=(k+1); if(h) c.appendChild(h);} }); }
if (!nextIdx) addRow();
</script>
""")

ORDER_DETAIL = _page("Замовлення {{ order['number'] }}", "Замовлення № {{ order['number'] }}",
                     "{{ order['client_name'] or '' }} · від {{ order['doc_date'] or '' }}{% if order['author'] %} · завів {{ order['author'] }}{% endif %}", STYLE + """
<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px">
<a class="btn btn-sm" href="{{ url_for('orders_list') }}">← Усі замовлення</a>
<a class="btn btn-sm" href="{{ url_for('orders_note_pdf', order_id=order['id']) }}" target="_blank">🖨 Службова записка (PDF)</a>
{% if r in ('admin','sales') %}<a class="btn btn-sm" href="{{ url_for('orders_edit', order_id=order['id']) }}">✎ Редагувати</a>
<form method="post" action="{{ url_for('orders_set_status', order_id=order['id']) }}" style="display:inline"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
{% if order['status']=='active' %}<input type="hidden" name="status" value="closed"><button class="btn btn-sm">✔ Закрити замовлення</button>
{% else %}<input type="hidden" name="status" value="active"><button class="btn btn-sm">↺ Повернути в роботу</button>{% endif %}</form>{% endif %}
</div>
<div class="card card-pad" style="margin-bottom:14px;display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;font-size:13px">
 <div><span style="color:var(--text-dim);font-size:11.5px">Замовник</span><br><b>{{ order['client_name'] or '—' }}</b></div>
 <div><span style="color:var(--text-dim);font-size:11.5px">Контактна особа</span><br>{{ order['contact_person'] or '—' }}</div>
 <div><span style="color:var(--text-dim);font-size:11.5px">Умови оплати</span><br>{{ order['payment_terms'] or '—' }}</div>
 <div><span style="color:var(--text-dim);font-size:11.5px">Доставка</span><br>{{ order['delivery'] or '—' }}</div>
 {% if money %}<div><span style="color:var(--text-dim);font-size:11.5px">Сума замовлення</span><br><b style="font-size:16px">{% for cur, v in totals.items() %}{{ v|money(cur) }}{% if not loop.last %} + {% endif %}{% else %}—{% endfor %}</b></div>
 {% if purchase_total %}<div><span style="color:var(--text-dim);font-size:11.5px">Закупівля заготовок</span><br><b>{{ purchase_total|money('UAH') }}</b></div>{% endif %}{% endif %}
 {% if order['notes'] %}<div style="grid-column:1/-1"><span style="color:var(--text-dim);font-size:11.5px">Примітки</span><br>{{ order['notes'] }}</div>{% endif %}
</div>
<div class="o-flow" style="margin-bottom:14px">📑 Замовлення <span>→</span> 📦 Склад: заготовка <span>+</span> 🧭 Технолог: техпроцес <span>→</span> 🏭 Запуск у виробництво</div>

{% for i in items %}
<div class="o-item" id="item-{{ i.id }}">
 <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center">
  <h3>{{ i.code }} · {{ i.name }} <span style="color:var(--text-dim);font-weight:400">{{ i.drawing_no or '' }}</span></h3>
  <span class="o-chip" style="color:{{ i.stage_color }};border-color:{{ i.stage_color }}">{{ i.stage_label }}</span>
 </div>
 <div style="font-size:13px;color:var(--text-dim);margin-top:4px">{{ '%g'|format(i.quantity) }} {{ i.unit }} · термін: <span class="{{ 'o-late' if i.overdue }}">{{ i.due_text }}</span>{% if i.overdue %} (прострочено){% endif %}{% if money and i.price is not none %} · ціна {{ '%.2f'|format(i.price) }} {{ i.currency or 'UAH' }} за {{ i.unit }} · сума <b style="color:var(--text)">{{ i.line_sum|money(i.currency or 'UAH') }}</b>{% endif %}{% if money and i.purchase_cost %} · закупівля заготовки {{ i.purchase_cost|money('UAH') }}{% endif %}</div>

 <div class="o-cols">
  <!-- СКЛАД -->
  <div class="o-box {{ 'done' if i.blank_status in ('in_stock','received') else 'todo' }}">
   <h4>📦 Склад · заготовка</h4>
   <div><span class="o-chip" style="color:{{ i.blank_color }};border-color:{{ i.blank_color }}">{{ i.blank_label }}</span></div>
   {% if i.blank_note %}<div style="font-size:13px;margin-top:6px">{{ i.blank_note }}</div>{% endif %}
   {% if i.ttn %}<div style="font-size:12.5px;margin-top:6px">ТТН <b>{{ i.ttn }}</b>{% if i.np_status %} · {{ i.np_status }}{% endif %}</div>{% endif %}
   {% if i.supplier_note %}<div style="font-size:12px;color:var(--text-dim);margin-top:4px">Постачальник: {{ i.supplier_note }}</div>{% endif %}
   {% if r in ('admin','warehouse') and not i.production_order_id %}
    {% if i.blank_status in ('unchecked','in_stock','to_buy') %}
    <form method="post" action="{{ url_for('order_item_blank', item_id=i.id) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
     <div class="o-row"><input type="text" name="blank_note" value="{{ i.blank_note or '' }}" placeholder="Заготовка: напр. коло Ст45 Ø40, 120 кг"></div>
     <div class="o-row">
      <button class="btn btn-sm btn-primary" name="action" value="in_stock">✔ Заготовка є</button>
      <button class="btn btn-sm" name="action" value="to_buy">🛒 Немає — заявка постачальнику{{ '' if purchaser_ready else ' (у загальний чат)' }}</button>
     </div></form>
    {% endif %}
   {% endif %}
   {% if r in ('admin','warehouse','purchaser') and i.blank_status in ('to_buy','ordered','shipped') %}
    <form method="post" action="{{ url_for('order_item_purchase', item_id=i.id) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
     <input type="hidden" name="back" value="{{ url_for('orders_detail', order_id=order['id']) }}#item-{{ i.id }}">
     <div class="o-row"><input type="text" name="ttn" value="{{ i.ttn or '' }}" placeholder="ТТН Нової Пошти (14 цифр)" inputmode="numeric">
      <input name="purchase_cost" type="number" step="0.01" min="0" value="{{ '%g'|format(i.purchase_cost) if i.purchase_cost else '' }}" placeholder="Сума закупівлі, грн"></div>
     <div class="o-row"><button class="btn btn-sm" name="action" value="ttn">Зберегти</button>
      <button class="btn btn-sm btn-primary" name="action" value="received">✔ Отримано</button>
      {% if i.ttn %}<button class="btn btn-sm btn-ghost" name="action" value="refresh">↻ Статус НП</button>{% endif %}</div></form>
   {% endif %}
  </div>

  <!-- ТЕХНОЛОГ -->
  <div class="o-box {{ 'done' if i.routing else 'todo' }}">
   <h4>🧭 Технолог · техпроцес</h4>
   {% if i.routing %}<div style="font-size:13.5px"><b>{{ i.routing['name'] }}</b>{% if i.routing['drawing_no'] %} · {{ i.routing['drawing_no'] }}{% endif %}</div>
   {% if r in ('admin','technologist','production') %}<a href="{{ url_for('routings_edit', routing_id=i.routing['id']) }}" style="font-size:12px;color:var(--accent-2)">Відкрити техпроцес</a>{% endif %}
   {% else %}<div style="font-size:13px;color:var(--text-dim)">Ще не прив'язано</div>{% endif %}
   {% if r in ('admin','technologist') and not i.production_order_id %}
    <form method="post" action="{{ url_for('order_item_routing', item_id=i.id) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
     <div class="o-row"><select name="routing_id"><option value="">— обрати з бібліотеки —</option>
     {% for rt in routings %}<option value="{{ rt['id'] }}" {{ 'selected' if i.routing_id==rt['id'] }}>{{ rt['name'] }}{% if rt['drawing_no'] %} · {{ rt['drawing_no'] }}{% endif %}</option>{% endfor %}</select>
     <button class="btn btn-sm">Прив'язати</button></div></form>
    <a class="btn btn-sm btn-ghost" style="margin-top:8px" href="{{ url_for('routings_new', for_item=i.id, name=i.name, drawing=i.drawing_no or '') }}">+ Створити новий техпроцес для цієї деталі</a>
   {% endif %}
  </div>

  <!-- ВИРОБНИЦТВО -->
  <div class="o-box {{ 'done' if i.production_order_id else '' }}">
   <h4>🏭 Виробництво</h4>
   {% if i.progress %}
    <div style="font-size:13.5px"><a href="{{ url_for('production_detail', order_id=i.production_order_id) }}" style="color:var(--accent-2)">Виробниче замовлення №{{ i.production_order_id }}</a></div>
    <div style="font-size:12.5px;color:var(--text-dim)">{{ i.progress.done }}/{{ i.progress.total }} операцій</div>
    <div class="o-bar"><i style="width:{{ i.progress.pct }}%"></i></div>
   {% elif i.stage_key=='ready' %}
    <div style="font-size:13px">Заготовка і техпроцес є — можна запускати.</div>
    {% if r in ('admin','technologist','production') %}
    <form method="post" action="{{ url_for('order_item_launch', item_id=i.id) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
     <button class="btn btn-primary" style="margin-top:8px">▶ Запустити у виробництво</button></form>{% endif %}
   {% else %}
    <div style="font-size:13px;color:var(--text-dim)">Запуск стане доступним, коли буде заготовка і техпроцес.</div>
   {% endif %}
  </div>
 </div>
</div>
{% endfor %}
""")

PROCUREMENT = _page("Закупівля заготовок", "Закупівля заготовок", "Заявки зі складу → закупівля → ТТН Нової Пошти → отримано", STYLE + """
<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
<a class="btn btn-sm" href="{{ url_for('procurement_pdf') }}" target="_blank">🖨 Заявка постачальнику (PDF)</a>
{% if not np_ready %}<span style="font-size:12px;color:var(--text-dim);align-self:center">Статуси Нової Пошти: адміністратор має вписати API-ключ НП у Налаштуваннях.</span>{% endif %}
</div>
{% for i in rows %}
<div class="o-item" id="item-{{ i.id }}">
 <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center">
  <h3>{{ i.code }} · {{ i.name }} <span style="color:var(--text-dim);font-weight:400">{{ i.drawing_no or '' }}</span></h3>
  <span class="o-chip" style="color:{{ i.blank_color }};border-color:{{ i.blank_color }}">{{ i.blank_label }}</span>
 </div>
 <div style="font-size:13px;margin-top:6px"><b>Що купити:</b> {{ i.blank_note or '—' }}</div>
 <div style="font-size:12.5px;color:var(--text-dim);margin-top:2px">Деталей: {{ '%g'|format(i.quantity) }} {{ i.unit }} · замовник {{ i.client_name or '—' }} · термін {{ i.due_text }} · <a href="{{ url_for('orders_detail', order_id=i.order_id) }}#item-{{ i.id }}" style="color:var(--accent-2)">замовлення {{ i.number }}</a></div>
 {% if i.ttn %}<div style="font-size:13px;margin-top:6px">ТТН <b>{{ i.ttn }}</b>{% if i.np_status %} · {{ i.np_status }}{% endif %}{% if i.np_checked_at %} <span style="color:var(--text-dim);font-size:11.5px">(перевірено {{ i.np_checked_at[11:16] }})</span>{% endif %}</div>{% endif %}
 {% if i.blank_status != 'received' %}
 <form method="post" action="{{ url_for('order_item_purchase', item_id=i.id) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
  <div class="o-row"><input type="text" name="supplier_note" value="{{ i.supplier_note or '' }}" placeholder="Постачальник (де купив)">
   <input name="purchase_cost" type="number" step="0.01" min="0" value="{{ '%g'|format(i.purchase_cost) if i.purchase_cost else '' }}" placeholder="Сума закупівлі, грн"></div>
  <div class="o-row"><input type="text" name="ttn" value="{{ i.ttn or '' }}" placeholder="ТТН Нової Пошти (14 цифр)" inputmode="numeric"></div>
  <div class="o-row">
   <button class="btn btn-sm" name="action" value="ttn">{{ 'Зберегти ТТН' }}</button>
   <button class="btn btn-sm btn-primary" name="action" value="received">✔ Отримано</button>
   {% if i.ttn %}<button class="btn btn-sm btn-ghost" name="action" value="refresh">↻ Статус</button>{% endif %}
  </div>
  <div style="font-size:11px;color:var(--text-dim);margin-top:4px">Без ТТН «Зберегти» означає «замовлено, ще не відправлено». Коли НП покаже «отримано», статус зміниться сам.</div>
 </form>{% endif %}
</div>
{% else %}<div class="card card-pad empty">Заготовок під замовлення купувати не треба 👍</div>{% endfor %}

<h3 style="margin:22px 0 10px;font-size:16px">📦 Матеріали для складу</h3>
{% for s in stock %}
<div class="o-item" id="stock-{{ s.id }}">
 <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center">
  <h3>{{ s.name }}{% if s.sku %} <span style="color:var(--text-dim);font-weight:400">{{ s.sku }}</span>{% endif %}</h3>
  <span class="o-chip" style="color:{{ s.color }};border-color:{{ s.color }}">{{ s.label }}</span>
 </div>
 <div style="font-size:13px;margin-top:6px"><b>Купити:</b> {{ '%g'|format(s.qty) }} {{ s.unit or '' }}{% if s.qty_on_hand is not none %} <span style="color:var(--text-dim)">· зараз на складі {{ '%g'|format(s.qty_on_hand) }}, мінімум {{ '%g'|format(s.min_qty or 0) }}</span>{% endif %}</div>
 {% if s.price is not none %}<div style="font-size:12.5px;margin-top:2px">Ціна {{ '%.2f'|format(s.price) }} грн за {{ s.unit or 'од.' }} · сума <b>{{ s.sum|money('UAH') }}</b>{% if s.supplier_note %} · {{ s.supplier_note }}{% endif %}</div>{% elif s.supplier_note %}<div style="font-size:12.5px;margin-top:2px">{{ s.supplier_note }}</div>{% endif %}
 {% if s.ttn %}<div style="font-size:13px;margin-top:6px">ТТН <b>{{ s.ttn }}</b>{% if s.np_status %} · {{ s.np_status }}{% endif %}</div>{% endif %}
 {% if s.status != 'received' %}
 <form method="post" action="{{ url_for('stock_request_update', req_id=s.id) }}"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
  <div class="o-row"><input type="text" name="supplier_note" value="{{ s.supplier_note or '' }}" placeholder="Постачальник (де купив)">
   <input name="price" type="number" step="0.01" min="0" value="{{ '%g'|format(s.price) if s.price is not none else '' }}" placeholder="Ціна за од., грн"></div>
  <div class="o-row"><input type="text" name="ttn" value="{{ s.ttn or '' }}" placeholder="ТТН Нової Пошти (14 цифр)" inputmode="numeric">
   <input name="qty_received" type="number" step="any" min="0" value="{{ '%g'|format(s.qty) }}" title="Скільки фактично отримано"></div>
  <div class="o-row">
   <button class="btn btn-sm" name="action" value="ttn">Зберегти</button>
   <button class="btn btn-sm btn-primary" name="action" value="received">✔ Отримано на склад</button>
   {% if s.ttn %}<button class="btn btn-sm btn-ghost" name="action" value="refresh">↻ Статус</button>{% endif %}
   <button class="btn btn-sm btn-ghost" name="action" value="cancel" onclick="return confirm('Скасувати заявку?')">Скасувати</button>
  </div>
  <div style="font-size:11px;color:var(--text-dim);margin-top:4px">«Отримано на склад» додає вказану кількість до залишку і оновлює ціну позиції.</div>
 </form>{% endif %}
</div>
{% else %}<div class="card card-pad empty">Заявок складу немає. Склад створює їх у «Заявка на закупівлю».</div>{% endfor %}
""")

ROUTINGS_LIST = _page("Техпроцеси", "Бібліотека техпроцесів", "Операції по дільницях з нормою часу — технолог веде, маркетинг і цех обирають", STYLE + """
{% if waiting %}<div class="card card-pad" style="margin-bottom:14px">
<div style="font-weight:600;margin-bottom:8px">⏳ Чекають техпроцесу ({{ waiting|length }})</div>
{% for w in waiting %}<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px;margin-bottom:6px">
<b style="min-width:70px">{{ w['code'] }}</b><span style="flex:1">{{ w['name'] }} <span style="color:var(--text-dim)">{{ w['drawing_no'] or '' }}</span></span>
<a class="btn btn-sm" href="{{ url_for('orders_detail', order_id=w['order_id']) }}#item-{{ w['id'] }}">Обрати готовий</a>
{% if r in ('admin','technologist') %}<a class="btn btn-sm btn-primary" href="{{ url_for('routings_new', for_item=w['id'], name=w['name'], drawing=w['drawing_no'] or '') }}">+ Створити</a>{% endif %}
</div>{% endfor %}</div>{% endif %}
<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
<form method="get" style="display:flex;gap:8px"><input type="text" name="q" value="{{ q }}" placeholder="Назва, креслення, матеріал" style="min-width:260px"><button class="btn btn-sm">Знайти</button></form>
{% if r in ('admin','technologist') %}<a class="btn btn-primary" href="{{ url_for('routings_new') }}">+ Новий техпроцес</a>{% endif %}
</div>
<div class="card table-wrap"><table>
<tr><th>Деталь / техпроцес</th><th>Креслення</th><th>Матеріал / заготовка</th><th>Операцій</th><th>Хв на 1 шт</th><th>У замовленнях</th><th></th></tr>
{% for t in rows %}<tr><td><b>{{ t['name'] }}</b></td><td>{{ t['drawing_no'] or '—' }}</td><td>{{ t['material'] or '—' }}{% if t['blank'] %}<br><span style="color:var(--text-dim);font-size:12px">{{ t['blank'] }}</span>{% endif %}</td>
<td>{{ t['steps'] }}</td><td>{{ t['minutes'] or 0 }}</td><td>{{ t['used'] }}</td>
<td><a class="btn btn-sm" href="{{ url_for('routings_edit', routing_id=t['id']) }}">{{ 'Редагувати' if r in ('admin','technologist') else 'Переглянути' }}</a></td></tr>
{% else %}<tr><td colspan="7" class="empty">Бібліотека порожня — створіть перший техпроцес.</td></tr>{% endfor %}</table></div>
""")

ROUTING_FORM = _page("Техпроцес", "{{ routing['name'] if routing else 'Новий техпроцес' }}",
                     "Операції по порядку: дільниця (верстат) і норма часу на 1 деталь", STYLE + """
{% set fv = form or {} %}
{% set editable = current_user['role'] in ('admin','technologist') %}
<form method="post" class="card card-pad">
<input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
{% if for_item %}<input type="hidden" name="for_item" value="{{ for_item }}">
<div style="font-size:12.5px;margin-bottom:10px;color:var(--accent-2)">Після збереження техпроцес одразу прив'яжеться до позиції замовлення.</div>{% endif %}
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
 <div class="field"><label>Назва деталі *</label><input type="text" name="name" required value="{{ fv.get('name') or (routing['name'] if routing else prefill_name) }}"></div>
 <div class="field"><label>Креслення</label><input type="text" name="drawing_no" value="{{ fv.get('drawing_no') or (routing['drawing_no'] or '' if routing else prefill_drawing) }}"></div>
 <div class="field"><label>Матеріал</label><input type="text" name="material" placeholder="Сталь 45" value="{{ fv.get('material') or (routing['material'] or '' if routing else '') }}"></div>
 <div class="field"><label>Заготовка</label><input type="text" name="blank" placeholder="Коло Ø40, відрізка 52 мм" value="{{ fv.get('blank') or (routing['blank'] or '' if routing else '') }}"></div>
</div>
<h3 style="margin:18px 0 8px;font-size:15px">Операції</h3>
<div class="table-wrap"><table class="items-table" style="width:100%">
<tr><th style="width:40px">№</th><th>Операція *</th><th style="width:240px">Дільниця / верстат</th><th style="width:130px">Хв на 1 шт *</th><th style="width:40px"></th></tr>
<tbody id="step-rows">
{% for s in steps %}<tr><td class="ln">{{ loop.index }}</td>
<td><input type="text" name="steps-{{ loop.index0 }}-op" value="{{ s['operation_name'] }}" required></td>
<td><select name="steps-{{ loop.index0 }}-wc"><option value="">—</option>{% for w in wcs %}<option value="{{ w['id'] }}" {{ 'selected' if s['work_center_id']==w['id'] }}>{{ w['name'] }}</option>{% endfor %}</select></td>
<td><input name="steps-{{ loop.index0 }}-min" type="number" step="any" min="0" value="{{ '%g'|format((s['planned_hours']*60)|round(2)) }}" required></td>
<td><button type="button" class="btn btn-sm btn-ghost" onclick="this.closest('tr').remove();renum()">✕</button></td></tr>{% endfor %}
</tbody></table></div>
{% if editable %}<button type="button" class="btn btn-sm" onclick="addStep()" style="margin-top:8px">+ Операція</button>{% endif %}
<div style="font-size:11.5px;color:var(--text-dim);margin-top:6px">Норма — на одну деталь; при запуску вона множиться на кількість у замовленні, і операції стають у чергу на свої дільниці (термінал цеху, Haas).</div>
<div class="field" style="margin-top:14px"><label>Примітки технолога</label><textarea name="notes" rows="3">{{ fv.get('notes') or (routing['notes'] or '' if routing else '') }}</textarea></div>
{% if editable %}<div style="display:flex;gap:8px;margin-top:14px"><button class="btn btn-primary">Зберегти техпроцес</button><a class="btn btn-ghost" href="{{ url_for('routings_list') }}">Скасувати</a></div>{% endif %}
</form>
{% if routing and editable %}<form method="post" action="{{ url_for('routings_delete', routing_id=routing['id']) }}" style="margin-top:12px" onsubmit="return confirm('Видалити техпроцес?')"><input type="hidden" name="_csrf_token" value="{{ csrf_token() }}"><button class="btn btn-sm">Видалити</button></form>{% endif %}
<script>
var wcOpts = '<option value="">—</option>{% for w in wcs %}<option value="{{ w["id"] }}">{{ w["name"]|e }}</option>{% endfor %}';
var nextStep = {{ steps|length }};
function addStep(){
  var i = nextStep++, tr = document.createElement('tr');
  tr.innerHTML = '<td class="ln"></td><td><input type="text" name="steps-'+i+'-op" required placeholder="напр. Токарна ЧПУ: точіння"></td>'+
   '<td><select name="steps-'+i+'-wc">'+wcOpts+'</select></td>'+
   '<td><input name="steps-'+i+'-min" type="number" step="any" min="0" required></td>'+
   '<td><button type="button" class="btn btn-sm btn-ghost" onclick="this.closest(\\'tr\\').remove();renum()">✕</button></td>';
  document.getElementById('step-rows').appendChild(tr); renum(); tr.querySelector('input').focus();
}
function renum(){ document.querySelectorAll('#step-rows tr').forEach(function(tr,k){ var c=tr.querySelector('.ln'); if(c) c.textContent=k+1; }); }
{% if editable %}if (!nextStep) addStep();{% endif %}
</script>
""")

TEMPLATES = {
    "orders_list.html": ORDERS_LIST,
    "order_form.html": ORDER_FORM,
    "order_detail.html": ORDER_DETAIL,
    "procurement.html": PROCUREMENT,
    "routings_list.html": ROUTINGS_LIST,
    "routing_form.html": ROUTING_FORM,
}
