#!/usr/bin/env python3
"""Просмотр дизайна beresta без сервера, базы и Docker.

Запуск: python3 scripts/design_preview.py [--port 8765] [--no-browser] [--signed-in]

Отдаёт app/static так же, как приложение (те же пути), отвечает на /health и /api/v1/*
тестовыми данными и принимает записи в память. Только стандартная библиотека Python 3.
Наружу не ходит: слушает только 127.0.0.1.
"""
import argparse
import json
import mimetypes
import re
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
PAGES = {"/privacy": "privacy.html", "/terms": "terms.html", "/cookies": "cookies.html",
         "/data-deletion": "data-deletion.html"}
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("text/javascript", ".js")

DAY = 86400


def new_id():
    return str(uuid.uuid4())


class Demo:
    """Всё состояние в памяти. Каждый запуск начинается с одних и тех же записей."""

    def __init__(self, signed_in=False):
        self.lock = threading.Lock()
        self.signed_in = signed_in
        self.csrf = "demo-csrf-token"
        self.now = time.time()
        self.categories = []
        for index, name in enumerate(["Общее", "Учёба", "Стартап", "Работа"]):
            self.categories.append({"id": f"cat-{index}", "name": name, "version": 1, "is_general": index == 0})
        self.notes = {}
        self.order = []
        self.reminders = {}
        self._seed()

    def category(self, name):
        return next(c["id"] for c in self.categories if c["name"] == name)

    def add_note(self, title, category, text, markdown, items, days_ago, channel="web", kind="text", conclusions=None):
        note_id = new_id()
        stamp = self.now - days_ago * DAY - 3600
        built = []
        for index, (item_kind, body, status, due_days) in enumerate(items):
            due_at = self.now + due_days * DAY if due_days is not None else None
            built.append({
                "id": new_id(), "note_id": note_id, "kind": item_kind, "text": body, "status": status,
                "version": 1, "source_quote": body, "due_text": None, "due_at": due_at,
                "position": index, "proposed_reminder": None,
            })
        self.notes[note_id] = {
            "id": note_id, "capture_id": new_id(), "channel": channel, "original_text": text,
            "input_kind": kind, "transcript": text if kind == "audio" else None, "transcript_version": 1,
            "transcript_origin": "stt" if kind == "audio" else None,
            "audio_seconds": 42 if kind == "audio" else None, "audio_media_type": "audio/webm" if kind == "audio" else None,
            "title": title, "markdown": markdown, "conclusions": conclusions or [],
            "category_id": self.category(category), "category_name": category,
            "structure_confirmed_at": stamp, "items": built, "version": 1, "provider": "mock",
            "created_at": stamp, "updated_at": stamp,
        }
        self.order.append(note_id)
        return note_id

    def _seed(self):
        n = self.add_note
        n("Идея для курсовой по логистике", "Учёба",
          "Надо подумать над курсовой. Тема: последняя миля в малых городах. Позвонить научруку в четверг, спросить про формат.",
          "# Идея для курсовой по логистике\n\nТема: последняя миля в малых городах.\n\n- Найти три источника по теме\n- Уточнить формат у научного руководителя",
          [("task", "Позвонить научруку", "open", 2), ("idea", "Сравнить доставку в Туле и Калуге", "open", None),
           ("task", "Найти три источника по теме", "open", 5)], 0)
        n("Питч для акселератора", "Стартап",
          "Питч на 3 минуты: проблема, решение, первые 20 пользователей. Показать макет в пятницу. Не забыть цифры по кастдеву.",
          "# Питч для акселератора\n\nСтруктура на три минуты.\n\n1. Проблема\n2. Решение\n3. Первые двадцать пользователей",
          [("task", "Собрать цифры по кастдеву", "open", 1), ("task", "Показать макет команде", "open", 3),
           ("goal", "Уложиться в три минуты", "open", None)], 1, conclusions=[
              {"id": new_id(), "text": "Начать питч с истории одного пользователя", "source_quote": "первые 20 пользователей", "status": "proposed"}])
        n("Созвон с клиентом", "Работа",
          "Созвон с клиентом в среду в 15:00. Обсудить сроки, прислать договор. Он просил скидку, подумать.",
          "# Созвон с клиентом\n\nСреда, 15:00.\n\n- Сроки\n- Договор\n- Скидка, вопрос открыт",
          [("task", "Прислать договор", "open", 1), ("note", "Клиент просил скидку", "open", None)], 2)
        n("Конспект: микроэкономика, неделя 6", "Учёба",
          "Эластичность спроса. Если цена растёт на 10 процентов и спрос падает на 20, эластичность по модулю два.",
          "# Эластичность спроса\n\nЕсли цена растёт на 10%, а спрос падает на 20%, эластичность по модулю равна 2.",
          [("note", "Эластичность больше 1 значит спрос эластичный", "open", None)], 3, kind="audio")
        n("Продукт: что делаем в этом месяце", "Стартап",
          "Месяц: онбординг, напоминания в Telegram, цены не трогаем. Поговорить с десятью людьми.",
          "# План на месяц\n\n- Онбординг\n- Напоминания в Telegram\n- Десять разговоров с пользователями",
          [("plan", "Переделать онбординг", "open", 14), ("task", "Провести десять интервью", "open", 20),
           ("task", "Проверить напоминания в Telegram", "completed", None)], 4, channel="telegram")
        n("Список покупок на неделю", "Общее",
          "Молоко, гречка, яблоки, кофе. Ещё купить лампочку в коридор.",
          "# Список покупок\n\n- Молоко\n- Гречка\n- Яблоки\n- Кофе\n- Лампочка в коридор",
          [("task", "Купить продукты", "open", 0), ("task", "Лампочка в коридор", "completed", None)], 5)
        n("Книга для чтения", "Общее",
          "Прочесть «Эссенциализм». Откладывать по 30 минут вечером.",
          "# Книга для чтения\n\n«Эссенциализм», по 30 минут вечером.",
          [("goal", "Дочитать до конца месяца", "open", 25)], 6)
        n("Подготовка к защите", "Учёба",
          "Защита через неделю. Сделать слайды, прогнать речь два раза, попросить друга послушать.",
          "# Подготовка к защите\n\n- Слайды\n- Репетиция дважды\n- Слушатель",
          [("task", "Сделать слайды", "open", 3), ("task", "Прогнать речь два раза", "open", 5),
           ("task", "Попросить друга послушать", "open", 6)], 7)
        n("Встреча с наставником", "Стартап",
          "Спросить про первые продажи и про то, когда нанимать. Записать три вывода.",
          "# Встреча с наставником\n\nВопросы про первые продажи и найм.",
          [("task", "Записать три вывода", "open", 2)], 8, kind="audio")
        n("Отчёт за квартал", "Работа",
          "Отчёт собрать до пятницы. Взять данные из таблицы, добавить график, выслать начальнику.",
          "# Отчёт за квартал\n\nСобрать данные, добавить график, отправить.",
          [("task", "Собрать данные из таблицы", "completed", None), ("task", "Выслать отчёт", "open", 4)], 9)
        n("Мысли о приложении", "Стартап",
          "Хочется, чтобы заметка сама подсказывала, когда к ней вернуться. Проверить на пяти людях.",
          "# Мысли о приложении\n\nЗаметка подсказывает, когда к ней вернуться.",
          [("idea", "Проверить подсказку возврата на пяти людях", "open", None)], 11)
        # Напоминания для двух задач.
        for note in list(self.notes.values())[:2]:
            task = next(i for i in note["items"] if i["kind"] == "task" and i["due_at"])
            rid = new_id()
            self.reminders[rid] = {
                "id": rid, "note_id": note["id"], "item_id": task["id"],
                "scheduled_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(task["due_at"])),
                "timezone": "Europe/Moscow", "text": task["text"], "status": "scheduled", "generation": 1,
                "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(self.now)),
                "delivery_status": None, "previous_attempt_unknown": False, "time_check": None,
                "calendar_url": None, "advance_of": None,
            }

    # ---- представления ----
    def user(self):
        return {"id": "demo-user", "username": "demo", "csrf_token": self.csrf, "policy_current": True,
                "policy_version": "2026-10-28", "has_password": False, "deletion_requested": False,
                "timezone": "Europe/Moscow", "profile_state": "ready", "calendar_offer": False}

    def summary(self, note):
        return {"id": note["id"], "title": note["title"], "version": note["version"],
                "updated_at": note["updated_at"], "category_id": note["category_id"],
                "channel": note["channel"], "input_kind": note["input_kind"]}

    def list_notes(self, query):
        limit = int(query.get("limit", ["20"])[0])
        offset = int(query.get("offset", ["0"])[0])
        text = (query.get("q", [""])[0] or "").casefold()
        category = query.get("category_id", [""])[0]
        channel = query.get("channel", [""])[0]
        kind = query.get("input_kind", [""])[0]
        rows = [self.notes[i] for i in self.order if i in self.notes]
        rows.sort(key=lambda n: -n["updated_at"])
        rows = [n for n in rows
                if (not text or text in (n["title"] + " " + n["original_text"]).casefold())
                and (not category or n["category_id"] == category)
                and (not channel or n["channel"] == channel)
                and (not kind or n["input_kind"] == kind)]
        page = rows[offset:offset + limit + 1]
        headers = {"X-Next-Notes-Offset": str(offset + limit)} if len(page) > limit else {}
        return [self.summary(n) for n in page[:limit]], headers

    def tasks(self, query):
        status = query.get("status", ["open"])[0]
        limit = int(query.get("limit", ["100"])[0])
        offset = int(query.get("offset", ["0"])[0])
        rows = []
        counts = {"open": 0, "completed": 0}
        for note in self.notes.values():
            for item in note["items"]:
                if item["kind"] != "task":
                    continue
                counts[item["status"]] = counts.get(item["status"], 0) + 1
                if status in ("all", item["status"]):
                    rows.append({
                        "id": item["id"], "text": item["text"], "status": item["status"], "version": item["version"],
                        "due_at": item["due_at"], "due_text": item["due_text"], "due_resolved_at": item["due_at"],
                        "due_has_time": True, "position": item["position"], "category_id": note["category_id"],
                        "note_id": note["id"], "note_title": note["title"], "note_version": note["version"],
                        "note_updated_at": note["updated_at"],
                    })
        rows.sort(key=lambda r: (r["status"] == "completed", r["due_at"] is None, r["due_at"] or 0))
        page = rows[offset:offset + limit + 1]
        return {"status": status, "limit": limit, "offset": offset, "has_more": len(page) > limit,
                "counts": counts, "items": page[:limit]}

    def capture_text(self, body):
        text = str(body.get("text") or "").strip() or "Пустая запись"
        title = re.split(r"[.!?\n]", text, maxsplit=1)[0][:60] or "Новая запись"
        category = body.get("category_id") or self.category("Общее")
        name = next((c["name"] for c in self.categories if c["id"] == category), "Общее")
        note_id = self.add_note(title, name, text, f"# {title}\n\n{text}", [], 0)
        note = self.notes[note_id]
        note["created_at"] = note["updated_at"] = time.time()
        return {"capture_id": note["capture_id"], "original_text": text, "processing_mode": body.get("processing_mode", "manual"),
                "input_kind": "text", "transcript": None, "transcript_version": 1, "transcript_origin": None,
                "audio_seconds": None, "audio_media_type": None, "note_id": note_id, "job": None, "images": []}


def make_handler(demo):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BerestaPreview"

        def log_message(self, fmt, *args):
            if self.path.startswith("/api/") or self.path == "/":
                print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

        # ---- общее ----
        def send(self, code, body=b"", ctype="application/json", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # Та же политика, что у приложения, без счётчиков.
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
                             "media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                             "frame-ancestors 'none'; form-action 'self'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, data, code=200, headers=None):
            self.send(code, json.dumps(data, ensure_ascii=False).encode(), headers=headers)

        def body(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw) if raw else {}
            except ValueError:
                return {}

        def static_file(self, path, ctype=None):
            if not path.is_file():
                return self.json({"detail": "Не найдено"}, 404)
            kind = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if kind.startswith("text/") or kind in ("application/javascript", "image/svg+xml"):
                kind += "; charset=utf-8"
            self.send(200, path.read_bytes(), kind)

        # ---- маршруты ----
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            if path == "/":
                html = (STATIC / "index.html").read_text(encoding="utf-8")
                html = html.replace("<body>", '<body data-source-token="demo">', 1)
                return self.send(200, html.encode(), "text/html; charset=utf-8")
            if path in PAGES:
                return self.static_file(STATIC / PAGES[path])
            if path.startswith("/static/"):
                target = (STATIC / path[len("/static/"):]).resolve()
                if STATIC.resolve() not in target.parents:
                    return self.json({"detail": "Не найдено"}, 404)
                return self.static_file(target)
            if path == "/favicon.ico":
                return self.static_file(STATIC / "favicon.svg")
            if path == "/health":
                return self.json({"status": "ok", "provider": "mock", "simulation": True})
            if path.startswith("/api/"):
                return self.api("GET", path, parse_qs(url.query))
            self.json({"detail": "Не найдено"}, 404)

        def do_POST(self):
            self.api_write("POST")

        def do_PATCH(self):
            self.api_write("PATCH")

        def do_PUT(self):
            self.api_write("PUT")

        def do_DELETE(self):
            self.api_write("DELETE")

        def api_write(self, method):
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                return self.json({"detail": "Не найдено"}, 404)
            self.api(method, url.path, parse_qs(url.query), self.body())

        def api(self, method, path, query, body=None):
            body = body or {}
            try:
                with demo.lock:
                    self.route(method, path, query, body)
            except Exception as error:  # страница не должна падать из-за макета
                print("  ошибка макета:", repr(error))
                self.json([] if method == "GET" else {}, 200)

        def deny(self):
            return self.json({"detail": "Войдите в аккаунт"}, 401)

        def route(self, method, path, query, body):
            p = path[len("/api/v1"):] if path.startswith("/api/v1") else path
            # Вход: любой код, любой логин, любой Telegram.
            if p == "/auth/email/options":
                return self.json({"code_login_enabled": True})
            if p == "/auth/email/code/start":
                return self.json({"resend_after": 5, "sent": True})
            if p in ("/auth/email/code/confirm", "/auth/login", "/auth/email/login"):
                demo.signed_in = True
                return self.json(demo.user())
            if p == "/auth/telegram/start":
                return self.json({"id": "demo-tg", "url": "https://t.me/", "app_url": "tg://resolve", "expires_in": 300})
            if p.startswith("/auth/telegram/status/"):
                demo.signed_in = True
                return self.json({"status": "confirmed", **demo.user()})
            if p == "/auth/logout":
                demo.signed_in = False
                return self.send(204)
            if p == "/auth/me":
                return self.json(demo.user()) if demo.signed_in else self.deny()
            if not demo.signed_in and p not in ("/example", "/timezones"):
                return self.deny()
            if p == "/provider/usage":
                return self.json({"provider": "mock", "simulation": True, "daily_unit_limit": 100, "daily_units_used": 12,
                                  "daily_units_remaining": 88, "text_unit_cost": 1, "audio_unit_base": 2,
                                  "audio_unit_per_minute": 1, "limit_resets_at": "2099-01-01T00:00:00+03:00"})
            if p == "/categories":
                if method == "POST":
                    name = str(body.get("name") or "").strip()[:60] or "Новая"
                    cat = {"id": new_id(), "name": name, "version": 1, "is_general": False}
                    demo.categories.append(cat)
                    return self.json(cat, 201)
                return self.json(demo.categories)
            m = re.fullmatch(r"/categories/([^/]+)", p)
            if m:
                cat = next((c for c in demo.categories if c["id"] == m.group(1)), None)
                if cat and method == "PATCH" and body.get("name"):
                    cat["name"] = str(body["name"])[:60]
                    cat["version"] += 1
                if cat and method == "DELETE" and not cat["is_general"]:
                    demo.categories.remove(cat)
                    return self.send(204)
                return self.json(cat or {}, 200)
            if p == "/notes" and method == "GET":
                rows, headers = demo.list_notes(query)
                return self.json(rows, headers=headers)
            m = re.fullmatch(r"/notes/([^/]+)", p)
            if m:
                note = demo.notes.get(m.group(1))
                if note is None:
                    return self.json({"detail": "Запись не найдена"}, 404)
                if method == "DELETE":
                    del demo.notes[note["id"]]
                    return self.send(204)
                if method in ("PATCH", "PUT"):
                    for key in ("title", "markdown", "category_id"):
                        if key in body:
                            note[key] = body[key]
                    if "category_id" in body:
                        note["category_name"] = next((c["name"] for c in demo.categories if c["id"] == body["category_id"]), None)
                    note["version"] += 1
                    note["updated_at"] = time.time()
                return self.json(note)
            if re.fullmatch(r"/notes/[^/]+/reminders", p):
                nid = p.split("/")[2]
                return self.json([r for r in demo.reminders.values() if r["note_id"] == nid])
            if re.fullmatch(r"/notes/[^/]+/(revisions|links|images)", p):
                return self.json([])
            if re.fullmatch(r"/notes/[^/]+/related", p):
                return self.json({"note_id": p.split("/")[2], "related": []})
            m = re.fullmatch(r"/notes/([^/]+)/items/([^/]+)", p)
            if m and m.group(1) in demo.notes:
                note = demo.notes[m.group(1)]
                item = next((i for i in note["items"] if i["id"] == m.group(2)), None)
                if item and method == "PATCH":
                    item.update({k: v for k, v in body.items() if k in ("text", "status", "kind")})
                    item["version"] += 1
                    note["version"] += 1
                return self.json(note)
            if p == "/tasks":
                return self.json(demo.tasks(query))
            if p == "/captures/text":
                return self.json(demo.capture_text(body), 201)
            if p == "/jobs":
                return self.json([])
            if p == "/dashboard":
                return self.json({"days": 30, "notes": len(demo.notes), "tasks": demo.tasks({"status": ["all"]})["counts"]})
            if p == "/profile":
                return self.json({"state": "ready", "questions": [], "answers": None,
                                  "facts": [{"id": "f1", "text": "Учится и запускает свой проект", "source": "ai"},
                                            {"id": "f2", "text": "Любит короткие списки дел", "source": "ai"}],
                                  "updated_at": int(demo.now), "calendar_offer": False, "suggested_categories": [],
                                  "categories_confirmed": True, "intro": "", "privacy": "", "error": None})
            if p == "/example":
                return self.json({"text": "завтра в 10 позвонить маме, купить молоко", "simulation": True})
            if p == "/timezones":
                return self.json([{"name": "Europe/Moscow", "label": "Москва"}])
            if p.startswith("/reminders/"):
                return self.json(demo.reminders.get(p.split("/")[2], {}))
            if p == "/assistant/settings":
                return self.json({"enabled": False})
            if p.startswith("/telegram/") or p.startswith("/account/") or p.startswith("/assistant/"):
                return self.json({} if method != "GET" else {"linked": False, "links": []})
            return self.json([] if method == "GET" else {})

    return Handler


def free_port(start):
    import socket
    for port in range(start, start + 50):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit("Не нашёл свободный порт, укажите --port")


def build_server(port=0, signed_in=False):
    demo = Demo(signed_in=signed_in)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(demo))
    server.demo = demo
    return server


def main():
    parser = argparse.ArgumentParser(description="Просмотр дизайна beresta без сервера и базы")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--landing", action="store_true", help="начать без входа (так по умолчанию)")
    parser.add_argument("--signed-in", action="store_true", help="сразу открыть приложение с тестовыми заметками")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    args = parser.parse_args()
    port = free_port(args.port)
    server = build_server(port, signed_in=args.signed_in and not args.landing)
    url = f"http://127.0.0.1:{port}/"
    print(f"beresta, просмотр дизайна: {url}")
    print("Войти можно любым способом, вход ненастоящий. Остановить: Ctrl+C.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
