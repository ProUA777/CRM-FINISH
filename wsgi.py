# -*- coding: utf-8 -*-
"""
WSGI-точка входу для продакшн-запуску (замість вбудованого dev-сервера Flask).

Приклади запуску:

  Linux/macOS (gunicorn, рекомендовано):
      pip install gunicorn
      gunicorn --workers 3 --bind 0.0.0.0:5000 --access-logfile logs/access.log wsgi:app

  Windows (waitress, бо gunicorn там не працює):
      pip install waitress
      waitress-serve --host=0.0.0.0 --port=5000 wsgi:app

Чому не `python app.py` у бою: вбудований сервер Flask — однопотоковий
dev-інструмент без правильної обробки навантаження, таймаутів і
одночасних з'єднань. gunicorn/waitress — це те, що реально тримає
навантаження кількох користувачів одночасно.

ВАЖЛИВО: перед запуском обов'язково встанови власний секретний ключ:
    export CRM_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
Без цього кроку сесії користувачів захищені лише типовим ключем із коду,
який будь-хто може прочитати у відкритому вихідному коді.
"""

from app import app  # noqa: F401  (gunicorn/waitress імпортують об'єкт `app` звідси)
