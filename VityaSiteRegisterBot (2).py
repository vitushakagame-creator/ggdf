#!/usr/bin/env python3
"""Бот заявок для @VityaSiteRegisterBot. Запуск на Windows: py VityaSiteRegisterBot.py.

Нужен Python 3.10+. На Windows открывается окно настройки. Укажите токен
из @BotFather и URL сайта, затем отправьте боту команду /admin из окна.
Для запуска в терминале добавьте --console. В обоих режимах можно задать
TELEGRAM_BOT_TOKEN и SITE_URL через переменные окружения.
Заявки и настройки хранятся в SQLite рядом со скриптом.
"""

import getpass
import json
import os
import re
import secrets
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Thread
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


BOT_USERNAME = "VityaSiteRegisterBot"
DATABASE = Path(__file__).resolve().with_suffix(".sqlite3")
EMAIL_LOCAL = re.compile(r"[a-zA-Z0-9!#$%&'*+/=?^`{|}~_.-]+")
DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
TOKEN_PATTERN = re.compile(r"[0-9]+:[a-zA-Z0-9_-]+")
WELCOME = (
    "Здравствуйте! 👋 Добро пожаловать!\n\n"
    "Для создания аккаунта на нашем сайте отправьте свою электронную почту. "
    "Администратор получит ваш адрес и Telegram ID для рассмотрения заявки."
)
PENDING_TEXT = "Ваша заявка уже на рассмотрении. Ожидайте решения администратора."
SAVED_TEXT = "Заявка сохранена. Бот повторит отправку администратору."
APPROVED_TEXT = "Ваша регистрация подтверждена! Откройте сайт по кнопке ниже."
REJECTED_TEXT = "Администратор отклонил вашу заявку. Вы можете отправить новую почту для повторной заявки."
STATUS_LABELS = {"approved": "Подтверждена", "rejected": "Отклонена"}
ADMIN_LINKED = "Администратор подключён. Бот принимает заявки."
TOKEN_FORMAT_ERROR = "Вставьте полный токен из @BotFather вместе с двоеточием."
SITE_FORMAT_ERROR = "Укажите полный адрес сайта, начинающийся с https://."


def normalize_email(address):
    local, separator, domain = address.rpartition("@")
    if not separator or not 0 < len(local) <= 64 or not EMAIL_LOCAL.fullmatch(local):
        return None
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    labels = domain.split(".")
    if len(labels) < 2 or not all(DOMAIN_LABEL.fullmatch(label) for label in labels):
        return None
    if labels[-1].isdigit() or len(local) + 1 + len(domain) > 254:
        return None
    return f"{local}@{domain}"


def valid_site_url(value):
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        return False
    try:
        parts = urlsplit(value)
        return bool(parts.scheme == "https" and parts.hostname and not parts.username
                    and not parts.password and (parts.port is None or 0 < parts.port < 65536)
                    and "\\" not in value)
    except ValueError:
        return False


class TelegramError(Exception):
    def __init__(self, method, code=0, retry_after=0):
        super().__init__(f"{method} (код {code})" if code else f"{method} (ошибка соединения)")
        self.code = code
        self.retry_after = retry_after

    @property
    def temporary(self):
        return self.code in (0, 429) or self.code >= 500


class Telegram:
    def __init__(self, token):
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method, *, http_timeout=15, **params):
        request = Request(
            self.base_url + method,
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            try:
                with urlopen(request, timeout=http_timeout) as response:
                    result = json.load(response)
            except HTTPError as error:
                with error:
                    try:
                        result = json.load(error)
                    except (ValueError, OSError):
                        raise TelegramError(method, error.code) from None
        except (URLError, OSError, ValueError):
            raise TelegramError(method) from None
        if not result.get("ok"):
            raise TelegramError(
                method, result.get("error_code", 0),
                result.get("parameters", {}).get("retry_after", 0),
            )
        return result["result"]

    def send(self, chat_id, text, buttons=None):
        params = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self.call("sendMessage", **params)


def init_database(db):
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected')),
            admin_message_id INTEGER,
            result_handled INTEGER NOT NULL DEFAULT 0,
            UNIQUE (user_id, source_message_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_application
            ON applications(user_id) WHERE status IN ('pending', 'approved');
    """)


def get_setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


class RegistrationBot:
    def __init__(self, api, db, site_url, report=print):
        self.api = api
        self.db = db
        self.site_url = site_url
        self.report = report
        saved_admin = get_setting(db, "admin_id")
        self.admin_id = int(saved_admin) if saved_admin else None
        self.setup_code = secrets.token_urlsafe(12) if self.admin_id is None else None

    def send_result(self, user_id, status):
        if status == "approved":
            self.api.send(user_id, APPROVED_TEXT, [[{"text": "Открыть сайт", "url": self.site_url}]])
        else:
            self.api.send(user_id, REJECTED_TEXT)

    def application_text(self, row):
        title = "Тестовая заявка" if row["user_id"] == self.admin_id else "Новая заявка"
        return f"{title} №{row['id']}\n\n📧 Почта: {row['email']}\n👤 Telegram ID: {row['user_id']}"

    def notify_admin(self, row):
        if row["admin_message_id"] is not None:
            return True
        buttons = [[
            {"text": "✅ Подтвердить", "callback_data": f"approve:{row['id']}"},
            {"text": "❌ Отклонить", "callback_data": f"reject:{row['id']}"},
        ]]
        try:
            message = self.api.send(self.admin_id, self.application_text(row), buttons)
        except TelegramError as error:
            if error.temporary:
                raise
            return False
        with self.db:
            self.db.execute("UPDATE applications SET admin_message_id = ? WHERE id = ?",
                            (message["message_id"], row["id"]))
        return True

    def flush_notifications(self):
        if self.admin_id is None:
            return
        pending = self.db.execute("""
            SELECT * FROM applications WHERE status = 'pending' AND admin_message_id IS NULL
            ORDER BY id LIMIT 10
        """).fetchall()
        for row in pending:
            if not self.notify_admin(row):
                self.report("Telegram пока не принимает заявки для администратора. Проверьте личный чат с ботом.")
                break

        decisions = self.db.execute("""
            SELECT * FROM applications WHERE status != 'pending' AND result_handled = 0
            ORDER BY id LIMIT 10
        """).fetchall()
        for row in decisions:
            try:
                self.send_result(row["user_id"], row["status"])
            except TelegramError as error:
                if error.code != 403:
                    raise
                self.report(f"Уведомление по заявке №{row['id']} недоступно. Решение видно по /status.")
            with self.db:
                self.db.execute("UPDATE applications SET result_handled = 1 WHERE id = ?", (row["id"],))

    def handle_message(self, message):
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if chat.get("type") != "private" or chat_id is None or message.get("from", {}).get("id") != chat_id:
            return
        text = message.get("text", "").strip()
        words = text.split(maxsplit=1)
        command = words[0].lower().removesuffix(f"@{BOT_USERNAME.lower()}") if words else ""
        if self.admin_id is None:
            self.pair_admin(chat_id, command, words[1] if len(words) == 2 else "")
            return
        if chat_id == self.admin_id and command == "/start":
            with self.db:
                self.db.execute("DELETE FROM applications WHERE user_id = ?", (chat_id,))
            self.api.send(chat_id, "🧪 Проверка регистрации\n\n" + WELCOME)
            return

        row = self.db.execute("""
            SELECT * FROM applications WHERE user_id = ? ORDER BY id DESC LIMIT 1
        """, (chat_id,)).fetchone()
        if row and row["status"] == "approved":
            self.send_result(chat_id, "approved")
            return
        if row and row["status"] == "pending":
            sent = self.notify_admin(row)
            self.api.send(chat_id, PENDING_TEXT if sent else SAVED_TEXT)
            return
        if command in ("/start", "/status"):
            self.api.send(chat_id, REJECTED_TEXT if row else WELCOME)
            return
        if not text:
            self.api.send(chat_id, "Отправьте электронную почту обычным текстом.")
            return
        email = normalize_email(text)
        if email is None:
            response = "Проверьте адрес и отправьте его ещё раз. Пример: example@gmail.com"
            if text.startswith("/"):
                response = "Используйте /start для начала или /status для проверки заявки."
            self.api.send(chat_id, response)
            return

        with self.db:
            cursor = self.db.execute("""
                INSERT OR IGNORE INTO applications(user_id, source_message_id, email) VALUES (?, ?, ?)
            """, (chat_id, message["message_id"], email))
        if cursor.rowcount == 0:
            return
        row = self.db.execute("SELECT * FROM applications WHERE id = ?", (cursor.lastrowid,)).fetchone()
        try:
            sent = self.notify_admin(row)
        except TelegramError as error:
            if error.code == 429:
                raise
            sent = False
        self.api.send(chat_id, "Спасибо! Ваша заявка отправлена администратору. "
                      "Ожидайте подтверждения." if sent else SAVED_TEXT)

    def pair_admin(self, user_id, command, argument):
        if (command == "/admin" and argument.isascii()
                and secrets.compare_digest(argument, self.setup_code)):
            with self.db:
                cursor = self.db.execute("INSERT OR IGNORE INTO settings VALUES ('admin_id', ?)",
                                         (str(user_id),))
            self.admin_id = int(get_setting(self.db, "admin_id"))
            self.setup_code = None
            if cursor.rowcount:
                self.report(ADMIN_LINKED)
                self.api.send(user_id, "Вы подключены как администратор. Заявки будут приходить сюда. "
                              "Отправьте /start, чтобы проверить регистрацию со своего аккаунта.")
                return
        self.api.send(user_id, "Бот пока настраивается. Попробуйте позже.")

    def handle_callback(self, query):
        callback_id = query["id"]
        chat = query.get("message", {}).get("chat", {})
        if (self.admin_id is None or query.get("from", {}).get("id") != self.admin_id
                or chat.get("id") != self.admin_id or chat.get("type") != "private"):
            self.answer_callback(callback_id, "Действие доступно администратору.")
            return
        match = re.fullmatch(r"(approve|reject):([0-9]{1,18})", query.get("data", ""))
        if not match:
            self.answer_callback(callback_id, "Кнопка недоступна.")
            return
        application_id = int(match[2])
        message_id = query["message"].get("message_id")
        status = "approved" if match[1] == "approve" else "rejected"
        with self.db:
            cursor = self.db.execute("""
                UPDATE applications SET status = ?
                WHERE id = ? AND admin_message_id = ? AND status = 'pending'
            """, (status, application_id, message_id))
            row = self.db.execute("""
                SELECT * FROM applications WHERE id = ? AND admin_message_id = ?
            """, (application_id, message_id)).fetchone()
        if not row:
            self.answer_callback(callback_id, "Эта карточка заявки уже недействительна.")
            return
        self.answer_callback(callback_id, "Решение сохранено." if cursor.rowcount else "Заявка уже рассмотрена.")
        try:
            self.api.call(
                "editMessageText", chat_id=self.admin_id, message_id=message_id,
                text=f"{self.application_text(row)}\n\nСтатус: {STATUS_LABELS[row['status']]}",
                reply_markup={"inline_keyboard": []},
            )
        except TelegramError as error:
            if error.temporary:
                raise

    def answer_callback(self, callback_id, text):
        try:
            self.api.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError as error:
            if error.temporary:
                raise

    def run(self, stop=None):
        stop = stop if stop is not None else Event()
        offset = 0
        delay = 2
        recovering = False
        while not stop.is_set():
            try:
                updates = self.api.call("getUpdates", http_timeout=25, offset=offset, timeout=12,
                                        allowed_updates=["message", "callback_query"])
                for update in updates:
                    if stop.is_set():
                        return
                    try:
                        if "message" in update:
                            self.handle_message(update["message"])
                        elif "callback_query" in update:
                            self.handle_callback(update["callback_query"])
                    except TelegramError as error:
                        if error.temporary or error.code in (401, 409):
                            raise
                        self.report(f"Telegram отклонил действие ({error}).")
                    offset = update["update_id"] + 1
                if not stop.is_set():
                    self.flush_notifications()
                if recovering and not stop.is_set():
                    self.report("Связь с Telegram восстановлена.")
                    recovering = False
                delay = 2
            except TelegramError as error:
                if not error.temporary:
                    raise
                self.report(f"Связь с Telegram прервалась ({error}). Повторяем попытку.")
                recovering = True
                stop.wait(max(delay, error.retry_after))
                delay = min(delay * 2, 30)


def connect_telegram(token):
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError(TOKEN_FORMAT_ERROR)
    api = Telegram(token)
    bot = api.call("getMe")
    if bot.get("username", "").lower() != BOT_USERNAME.lower():
        raise ValueError(f"Этот токен выдан другому боту. Нужен токен @{BOT_USERNAME}.")
    if api.call("getWebhookInfo").get("url"):
        raise ValueError("Для бота уже настроен webhook. Отключите его в прежнем сервисе перед запуском.")
    return api


def save_site_url(db, site_url):
    with db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('site_url', ?)", (site_url,))


def error_message(error):
    if isinstance(error, TelegramError):
        hints = {
            401: "Telegram отклонил токен. Скопируйте действующий токен из @BotFather.",
            404: "Telegram не нашёл бота по этому токену. Проверьте токен в @BotFather.",
            409: "Бот уже запущен в другом окне или сервисе. Остановите предыдущий запуск.",
        }
        return hints.get(error.code, f"Ошибка Telegram ({error}). Проверьте подключение и попробуйте снова.")
    return str(error)


def run_console():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        if not sys.stdin.isatty():
            raise ValueError("Запустите скрипт из терминала или задайте TELEGRAM_BOT_TOKEN.")
        token = getpass.getpass("Вставьте токен из @BotFather и нажмите Enter (ввод скрыт). ").strip()
    api = connect_telegram(token)

    with closing(sqlite3.connect(DATABASE)) as db:
        init_database(db)
        site_url = os.environ.get("SITE_URL", "").strip() or get_setting(db, "site_url")
        if site_url and not valid_site_url(site_url):
            raise ValueError(SITE_FORMAT_ERROR)
        while not site_url:
            candidate = input("Адрес сайта для одобренных пользователей (https://…): ").strip()
            if valid_site_url(candidate):
                site_url = candidate
            else:
                print("Нужен полный адрес сайта, например https://example.com/register.")
        save_site_url(db, site_url)
        bot = RegistrationBot(api, db, site_url)
        if bot.setup_code:
            print(f"Откройте https://t.me/{BOT_USERNAME} и отправьте команду /admin {bot.setup_code}")
        else:
            print("Бот запущен и принимает заявки.")
        print("Для остановки нажмите Ctrl+C.")
        bot.run()


def run_window():
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        return False
    try:
        root = tk.Tk()
    except tk.TclError:
        return False

    root.title(f"Настройка @{BOT_USERNAME}")
    root.columnconfigure(0, weight=1)
    form = ttk.Frame(root, padding=22)
    form.grid(sticky="nsew")
    form.columnconfigure(0, weight=1)

    ttk.Label(form, text="Бот регистрации", font=("Segoe UI", 17, "bold")).grid(sticky="w")
    ttk.Label(form, text=f"@{BOT_USERNAME}").grid(sticky="w", pady=(0, 18))

    token = tk.StringVar(value=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
    site = tk.StringVar(value=os.environ.get("SITE_URL", "").strip())
    command = tk.StringVar()
    status = tk.StringVar(value="Вставьте токен из @BotFather и укажите адрес своего сайта.")
    if not site.get() and DATABASE.is_file():
        try:
            with closing(sqlite3.connect(DATABASE)) as db:
                db.row_factory = sqlite3.Row
                site.set(get_setting(db, "site_url") or "")
        except sqlite3.Error as error:
            status.set(error_message(error))

    stop = Event()
    events = SimpleQueue()
    worker = None

    def paste_token():
        try:
            value = root.clipboard_get().strip()
        except tk.TclError:
            value = ""
        if value:
            token.set(value)
            token_entry.icursor("end")
            token_entry.focus_set()
            status.set("Токен вставлен. Нажмите «Показать», если хотите проверить его.")
        else:
            status.set("В буфере обмена пусто. Скопируйте токен из @BotFather.")

    def toggle_token():
        reveal = bool(token_entry.cget("show"))
        token_entry.configure(show="" if reveal else "•")
        show.configure(text="Скрыть" if reveal else "Показать")

    def copy_command():
        root.clipboard_clear()
        root.clipboard_append(command.get())
        status.set(f"Команда скопирована. Отправьте её в личный чат с @{BOT_USERNAME}.")

    def enable_form(enabled):
        state = "normal" if enabled else "disabled"
        for widget in (token_entry, site_entry, paste, show, start):
            widget.configure(state=state)

    def connect_and_run(bot_token, site_url):
        try:
            api = connect_telegram(bot_token)
            if stop.is_set():
                return
            with closing(sqlite3.connect(DATABASE)) as db:
                init_database(db)
                save_site_url(db, site_url)
                bot = RegistrationBot(api, db, site_url, report=lambda msg: events.put(("status", msg)))
                events.put(("ready", f"/admin {bot.setup_code}" if bot.setup_code else ""))
                bot.run(stop)
        except (TelegramError, ValueError, sqlite3.Error) as error:
            events.put(("error", error_message(error)))

    def start_bot():
        nonlocal worker
        bot_token, site_url = token.get().strip(), site.get().strip()
        if not TOKEN_PATTERN.fullmatch(bot_token):
            status.set(TOKEN_FORMAT_ERROR)
            token_entry.focus_set()
            return
        if not valid_site_url(site_url):
            status.set(SITE_FORMAT_ERROR)
            site_entry.focus_set()
            return
        token_entry.configure(show="•")
        show.configure(text="Показать")
        enable_form(False)
        status.set("Подключение к Telegram…")
        worker = Thread(target=connect_and_run, args=(bot_token, site_url))
        worker.start()

    def close_window():
        stop.set()
        if worker is not None and worker.is_alive():
            status.set("Останавливаем бота…")
            root.after(100, close_window)
        else:
            root.destroy()

    def show_events():
        if stop.is_set():
            return
        while True:
            try:
                event, value = events.get_nowait()
            except Empty:
                break
            if event == "ready":
                token.set("")
                command.set(value)
                if value:
                    copy.configure(state="normal")
                    status.set(f"Отправьте команду из поля ниже в личный чат с @{BOT_USERNAME}.")
                else:
                    status.set(ADMIN_LINKED)
            elif event == "error":
                command.set("")
                copy.configure(state="disabled")
                status.set(value)
                enable_form(True)
            else:
                if value == ADMIN_LINKED:
                    command.set("")
                    copy.configure(state="disabled")
                status.set(value)
        root.after(100, show_events)

    ttk.Label(form, text="Токен бота").grid(row=2, sticky="w")
    token_entry = ttk.Entry(form, textvariable=token, show="•", width=42)
    token_entry.grid(row=3, column=0, sticky="ew", pady=(4, 14))
    paste = ttk.Button(form, text="Вставить", command=paste_token)
    paste.grid(row=3, column=1, padx=(6, 0), pady=(4, 14))
    show = ttk.Button(form, text="Показать", command=toggle_token)
    show.grid(row=3, column=2, padx=(6, 0), pady=(4, 14))

    ttk.Label(form, text="HTTPS-адрес сайта").grid(row=4, sticky="w")
    site_entry = ttk.Entry(form, textvariable=site)
    site_entry.grid(row=5, columnspan=3, sticky="ew", pady=(4, 14))
    start = ttk.Button(form, text="Запустить бота", command=start_bot)
    start.grid(row=6, sticky="w")
    ttk.Label(form, textvariable=status, wraplength=490, justify="left").grid(
        row=7, columnspan=3, sticky="w", pady=(14, 12),
    )
    ttk.Label(form, text="Команда для первой привязки администратора").grid(row=8, columnspan=3, sticky="w")
    ttk.Entry(form, textvariable=command, state="readonly").grid(row=9, sticky="ew", pady=(4, 0))
    copy = ttk.Button(form, text="Копировать команду", command=copy_command, state="disabled")
    copy.grid(row=9, column=1, columnspan=2, padx=(6, 0), pady=(4, 0), sticky="ew")

    token_entry.focus_set()
    root.bind("<Return>", lambda _event: start.invoke())
    root.protocol("WM_DELETE_WINDOW", close_window)
    root.after(100, show_events)
    try:
        root.mainloop()
    finally:
        stop.set()
    return True


def main():
    if os.name == "nt" and "--console" not in sys.argv:
        if run_window():
            return
        print("Окно настройки недоступно. Продолжаем в терминале.")
    run_console()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nБот остановлен.")
    except EOFError:
        sys.exit("Для настройки откройте скрипт из терминала.")
    except (TelegramError, ValueError, sqlite3.Error) as error:
        sys.exit(error_message(error))
