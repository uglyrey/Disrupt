import asyncio
import logging
import re
import time

from .clients import (
    ACTION_PREFIXES,
    MAX_AUDIO_BYTES,
    REJECTIONS,
    TOKEN_CHARS,
    CoreClient,
    Rejected,
    RemoteFailure,
    TelegramClient,
)
from .config import Settings
from .limit_text import saved_reply
from .state import OffsetStore

logger = logging.getLogger(__name__)

BTN_WRITE, BTN_MANUAL = "📝 Записать", "✍️ Без ИИ"
BTN_LINK, BTN_WEB = "🔗 Подключить аккаунт", "🌐 Открыть beresta"
BTN_HELP, BTN_MENU = "❓ Помощь", "🏠 Меню"
BUTTONS = {BTN_WRITE, BTN_MANUAL, BTN_LINK, BTN_WEB, BTN_HELP, BTN_MENU}
MAIN_MENU = {
    "keyboard": [
        [{"text": BTN_WRITE}, {"text": BTN_MANUAL}],
        [{"text": BTN_LINK}, {"text": BTN_WEB}],
        [{"text": BTN_HELP}, {"text": BTN_MENU}],
    ],
    "is_persistent": True,
    "resize_keyboard": True,
}
BOT_COMMANDS = [
    {"command": "start", "description": "Начать и подключить аккаунт"},
    {"command": "menu", "description": "Показать кнопки"},
    {"command": "help", "description": "Что умеет бот"},
    {"command": "save", "description": "Сохранить текст без ИИ"},
    {"command": "timezone", "description": "Выбрать часовой пояс"},
]
HELP_TEXT = (
    "Что умеют кнопки.\n"
    "📝 Записать. Напиши текст или отправь голосовое, я сохраню запись.\n"
    "✍️ Без ИИ. Следующее сообщение сохраню как есть, без разбора.\n"
    "🔗 Подключить аккаунт. Свяжу этот чат с beresta.\n"
    "🌐 Открыть beresta. Ссылка на веб.\n"
    "🏠 Меню. Вернуть кнопки, если они пропали.\n\n"
    "Голосовое тоже сохраняется. Текст из него появится в beresta.\n"
    "ИИ на день есть лимит. Текст стоит 1, голос 2 и ещё 1 за минуту. Когда лимит кончится, "
    "записи сохраню без ИИ."
)
LOGIN_TEXT = (
    "Подтвердите действие в beresta. Нажмите «Да, это я», только если вы сами нажали «Войти через Telegram» "
    "или «Подтвердить через Telegram» на сайте beresta в последние минуты. "
    "Если ссылку прислал кто-то другой, нажмите «Нет, не я» и никому её не пересылайте."
)
LINK_TEXT = (
    "Подключить этот Telegram к аккаунту beresta? "
    "Нажимайте «Да», только если вы сами начали подключение на сайте beresta."
)
# Time zone. Buttons carry the IANA name: "tz:Europe/Moscow" chooses it, "tzy:..." confirms a place found by location.
ZONE_PREFIX, ZONE_CONFIRM_PREFIX = "tz:", "tzy:"
ZONE_OTHER, ZONE_NO = "tz:other", "tz:no"
ZONE_NAME = re.compile(r"[A-Za-z0-9_+/-]{1,60}")
ZONE_PRESETS = (
    ("Калининград", "Europe/Kaliningrad"), ("Москва", "Europe/Moscow"),
    ("Самара", "Europe/Samara"), ("Екатеринбург", "Asia/Yekaterinburg"),
    ("Омск", "Asia/Omsk"), ("Новосибирск", "Asia/Novosibirsk"),
    ("Красноярск", "Asia/Krasnoyarsk"), ("Иркутск", "Asia/Irkutsk"),
    ("Якутск", "Asia/Yakutsk"), ("Владивосток", "Asia/Vladivostok"),
)
ZONE_ASK = "Чтобы напоминания приходили вовремя, выберите ваш часовой пояс."
ZONE_OTHER_TEXT = (
    "Отправьте местоположение кнопкой внизу или напишите крупный город рядом, например Томск. "
    "Можно и смещение, например +5 или UTC+5. Координаты я не сохраняю."
)
ZONE_NOT_FOUND = "Не нашёл такой город. Напишите крупный город рядом."
ZONE_LOCATION_MISS = "Не получилось определить пояс по местоположению. Выберите город кнопкой или напишите крупный город рядом."
LOCATION_KEYBOARD = {
    "keyboard": [[{"text": "📍 Отправить местоположение", "request_location": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}
ZONE_TEXTS = {"часовой пояс", "мой часовой пояс", "часовой пояс?"}
ZONE_WAIT_MAX_CHARS = 40
UTC_TEXT = re.compile(r"UTC([+-]\d{1,2}(:\d{2})?)?")
LOGIN_PREFIX, LINK_PREFIX = "login:", "link:"
LOGIN_DECLINE, LINK_DECLINE = "login-no", "link-no"
LOGIN_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
# callback_data is limited to 64 bytes, so a longer code cannot travel through a button.
LINK_CODE = re.compile(r"[A-Za-z0-9_-]{22,%d}" % (64 - len(LINK_PREFIX)))
MANUAL_TTL = 600
TELEGRAM_UNITS = 4096
WATCH_TTL = 600
WATCH_INTERVAL = 3.0
CHAT_LIMIT, CHAT_WINDOW = 20, 60.0  # messages per chat per minute, buttons are not counted
CHAT_TABLE_MAX = 2000


def plural(count, one, few, many):
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


class Bot:
    def __init__(self, settings: Settings, core: CoreClient, telegram: TelegramClient, state: OffsetStore):
        self.settings, self.core, self.telegram, self.state = settings, core, telegram, state
        self.clock = time.monotonic
        self.watch_interval = WATCH_INTERVAL
        # In-memory only: a restart forgets pending "next message without AI" marks and link watchers.
        self.manual_until = {}
        self.watchers = {}
        self.recent = {}  # chat_id -> times of recent messages, in memory only
        self.awaiting_zone = {}  # chat_id -> until when the next short text is a city or an offset

    @property
    def web(self):
        return self.settings.web_url.rstrip("/")

    async def setup(self):
        """Best effort: the command list is a convenience, never a reason to stop the bot."""
        try:
            await self.telegram.call("setMyCommands", {"commands": BOT_COMMANDS})
        except RemoteFailure as error:
            logger.warning("bot_commands_not_set code=%s", error.code)

    async def close(self):
        tasks = list(self.watchers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.watchers.clear()

    def start_watch(self, chat_id, telegram_user_id, link_request_id):
        old = self.watchers.get(chat_id)
        if old is not None:
            old.cancel()
        task = asyncio.create_task(self._watch(chat_id, telegram_user_id, link_request_id))
        self.watchers[chat_id] = task

        def forget(done):
            if self.watchers.get(chat_id) is done:
                del self.watchers[chat_id]

        task.add_done_callback(forget)

    async def _watch(self, chat_id, telegram_user_id, link_request_id):
        deadline = self.clock() + WATCH_TTL
        try:
            while self.clock() < deadline:
                await asyncio.sleep(self.watch_interval)
                try:
                    data = await self.core.get(
                        "/telegram/link-requests/" + link_request_id,
                        {"telegram_user_id": telegram_user_id},
                    )
                except RemoteFailure as error:
                    logger.warning("bot_watch_paused code=%s", error.code)
                    if error.fatal:
                        return
                    continue
                if data is None or data.get("status") == "expired":
                    return
                if data.get("status") == "confirmed":
                    name = data.get("username")
                    name = name if isinstance(name, str) and 0 < len(name) <= 64 else None
                    text = "Вы присоединили учётную запись. Добро пожаловать" + (f", {name}!" if name else "!")
                    await self.notify(chat_id, text, reply_markup=MAIN_MENU)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never log exception text: it may carry URLs or private payloads.
            logger.warning("bot_watch_failed")

    async def notify(self, chat_id, text, url=None, *, button="Открыть Beresta", reply_markup=None):
        # Capture acceptance is durable even when its acknowledgement cannot be delivered.
        # Never replay an ambiguous sendMessage just to obtain a success response.
        try:
            extra = {}
            if button != "Открыть Beresta":
                extra["button"] = button
            if reply_markup is not None:
                extra["reply_markup"] = reply_markup
            await self.telegram.reply(chat_id, text, url, **extra)
        except RemoteFailure as error:
            if error.fatal:
                raise
            logger.warning("bot_reply_unconfirmed code=%s", error.code)

    def throttle(self, chat_id):
        """Answer ok, warn (the first message over the limit) or drop. Counts messages per chat in memory."""
        now = self.clock()
        if len(self.recent) > CHAT_TABLE_MAX:
            self.recent = {k: v for k, v in self.recent.items() if v and now - v[-1] < CHAT_WINDOW}
        times = [t for t in self.recent.get(chat_id, ()) if now - t < CHAT_WINDOW]
        over = len(times) >= CHAT_LIMIT
        times.append(now)  # Dropped messages count too, so a flood stays blocked until it pauses.
        self.recent[chat_id] = times
        if not over:
            return "ok"
        return "warn" if len(times) == CHAT_LIMIT + 1 else "drop"

    async def handle(self, update):
        callback = update.get("callback_query")
        message = callback.get("message") if isinstance(callback, dict) else update.get("message")
        if not isinstance(message, dict):
            return
        sender = callback.get("from", {}) if callback else message.get("from", {})
        chat = message.get("chat", {})
        if (
            chat.get("type") != "private"
            or type(sender.get("id")) is not int
            or chat.get("id") != sender["id"]
            or sender.get("is_bot")
        ):
            return
        chat_id = chat["id"]
        if not callback:
            verdict = self.throttle(chat_id)
            if verdict != "ok":
                if verdict == "warn":
                    await self.notify(chat_id, "Слишком часто. Подожди минуту и отправь ещё раз.")
                return
        identity = {
            "bot_id": self.settings.bot_id,
            "update_id": update["update_id"],
            "telegram_user_id": sender["id"],
            "chat_id": chat_id,
        }
        try:
            if callback:
                token = callback.get("data")
                if not isinstance(token, str) or not 1 <= len(token.encode()) <= 64:
                    return
                if token.startswith((ZONE_PREFIX, ZONE_CONFIRM_PREFIX)):
                    await self.answer_callback(callback)
                    await self.zone_button(chat_id, identity, token)
                    return
                if token.startswith((LOGIN_PREFIX, LINK_PREFIX)) or token in {LOGIN_DECLINE, LINK_DECLINE}:
                    # Answer first so the button stops spinning even when the core refuses.
                    await self.answer_callback(callback)
                    await self.answer_buttons(chat_id, identity, token, sender)
                    return
                await self.reminder_button(callback, chat_id, identity, token)
                return
            text = message.get("text", "")
            command, _, argument = text.partition(" ") if isinstance(text, str) else ("", "", "")
            command = command.split("@", 1)[0]
            # Button labels arrive as plain text: answer them before anything can be saved as a note.
            if isinstance(text, str) and text.strip() in BUTTONS:
                await self.press(chat_id, text.strip())
                return
            if isinstance(text, str) and (command == "/timezone" or text.strip().lower() in ZONE_TEXTS):
                await self.timezone_command(chat_id, identity, argument.strip() if command == "/timezone" else "")
                return
            if (
                isinstance(text, str) and text.strip() and not command.startswith("/")
                and self.awaiting_zone.get(chat_id, 0) > self.clock()
            ):
                if len(text.strip()) <= ZONE_WAIT_MAX_CHARS and "\n" not in text.strip():
                    await self.typed_zone(chat_id, identity, text.strip())
                    return
                self.awaiting_zone.pop(chat_id, None)  # A long text is a note, not a city.
            if command in {"/menu", "/help"} or (command == "/start" and not argument.strip()):
                if command == "/help":
                    await self.notify(chat_id, HELP_TEXT, reply_markup=MAIN_MENU)
                else:
                    await self.notify(chat_id, "Привет! Кнопки внизу, выбирай.", reply_markup=MAIN_MENU)
                return
            if command == "/start" and re.fullmatch(r"login_[A-Za-z0-9_-]{43}", argument):
                # A link can be forwarded by someone else, so nothing reaches the core before the person says yes.
                keyboard = [[
                    {"text": "Да, это я", "callback_data": LOGIN_PREFIX + argument[len("login_"):]},
                    {"text": "Нет, не я", "callback_data": LOGIN_DECLINE},
                ]]
                await self.notify(chat_id, LOGIN_TEXT, reply_markup={"inline_keyboard": keyboard})
                return
            if command == "/start":
                if not LINK_CODE.fullmatch(argument):
                    raise Rejected("invalid_link")
                keyboard = [[
                    {"text": "Да, подключить", "callback_data": LINK_PREFIX + argument},
                    {"text": "Нет", "callback_data": LINK_DECLINE},
                ]]
                await self.notify(chat_id, LINK_TEXT, reply_markup={"inline_keyboard": keyboard})
                return
            if isinstance(text, str) and text:
                mode = "manual" if command == "/save" else "ai"
                content = argument if mode == "manual" else text
                if command.startswith("/") and mode != "manual":
                    await self.notify(chat_id, "Команда неизвестна. Доступны /menu, /help и /save текст записи.")
                    return
                if mode == "ai" and self.manual_until.pop(chat_id, 0) > self.clock():
                    mode = "manual"
                if not content.strip():
                    raise Rejected("empty_input")
                if len(content) > 12000:
                    raise Rejected("input_too_large")
                result = await self.core.post(
                    "/telegram/updates", {**identity, "text": content, "processing_mode": mode}
                )
            elif isinstance(message.get("voice"), dict):
                voice = message["voice"]
                if voice.get("duration", 181) > 180 or voice.get("file_size", 0) > MAX_AUDIO_BYTES:
                    raise Rejected("input_too_large")
                if not isinstance(voice.get("file_id"), str):
                    raise Rejected("unsupported_audio")
                audio = await self.telegram.voice(voice["file_id"])
                result = await self.core.post(
                    "/telegram/voice", {**identity, "processing_mode": "ai"}, audio=audio
                )
            elif isinstance(message.get("location"), dict):
                await self.location(chat_id, identity, message["location"])
                return
            elif isinstance(message.get("audio"), dict) or isinstance(message.get("video_note"), dict):
                await self.notify(chat_id, "Пока принимаю только голосовые сообщения. Запиши голосовое кнопкой микрофона.")
                return
            else:
                await self.notify(
                    chat_id, "Отправь текст или голосовое сообщение. Этот тип вложения пока недоступен."
                )
                return
            url = self.core.validate_saved(result)
            saved = saved_reply(result)
            await self.notify(chat_id, saved, url)
            if result.get("ask_timezone") is True:
                # The core says this account has no zone and was never asked. It marks the question as asked.
                await self.notify(chat_id, ZONE_ASK, reply_markup=self.zone_keyboard())
        except Rejected as error:
            await self.notify(chat_id, REJECTIONS[str(error)], self.settings.web_url)

    # ---- Time zone ----

    @staticmethod
    def zone_keyboard():
        """Ten common Russian zones, two per row, and «Другой»."""
        rows = []
        for index in range(0, len(ZONE_PRESETS), 2):
            rows.append([
                {"text": label, "callback_data": ZONE_PREFIX + zone} for label, zone in ZONE_PRESETS[index:index + 2]
            ])
        rows.append([{"text": "Другой", "callback_data": ZONE_OTHER}])
        return {"inline_keyboard": rows}

    async def zone_state(self, identity):
        """The zone state of the linked person, or None when this Telegram is not linked."""
        data = await self.core.get(
            "/telegram/timezone",
            {"bot_id": identity["bot_id"], "telegram_user_id": identity["telegram_user_id"]},
        )
        if data is None:
            return None
        zone, label, utc = data.get("timezone"), data.get("label"), data.get("utc")
        if (
            not (zone is None or isinstance(zone, str))
            or not (label is None or isinstance(label, str) and len(label) <= 64)
            or not (utc is None or isinstance(utc, str) and UTC_TEXT.fullmatch(utc))
            or type(data.get("asked")) is not bool
        ):
            raise RemoteFailure("core_invalid_response")
        return data

    async def timezone_command(self, chat_id, identity, argument):
        """/timezone shows the zone and the buttons. With a city or an offset after it, the zone is set at once."""
        if argument:
            await self.typed_zone(chat_id, identity, argument[:ZONE_WAIT_MAX_CHARS * 2])
            return
        state = await self.zone_state(identity)
        if state is None:
            raise Rejected("telegram_not_linked")
        if state.get("timezone") and state.get("label") and state.get("utc"):
            text = f"Ваш часовой пояс: {state['label']} ({state['utc']}). Выберите другой, если нужно."
        else:
            text = "Часовой пояс не выбран, напоминания идут по Москве. " + ZONE_ASK
            if not state.get("asked"):
                # The buttons are on screen now, so the automatic question after a note is not needed any more.
                await self.core.post("/telegram/timezone-asked", identity)
        await self.notify(chat_id, text, reply_markup=self.zone_keyboard())

    async def zone_button(self, chat_id, identity, token):
        if token == ZONE_OTHER:
            self.awaiting_zone[chat_id] = self.clock() + MANUAL_TTL
            await self.notify(chat_id, ZONE_OTHER_TEXT, reply_markup=LOCATION_KEYBOARD)
        elif token == ZONE_NO:
            await self.notify(chat_id, "Хорошо, часовой пояс не меняю.", reply_markup=MAIN_MENU)
        else:
            zone = token[len(ZONE_CONFIRM_PREFIX):] if token.startswith(ZONE_CONFIRM_PREFIX) else token[len(ZONE_PREFIX):]
            if not ZONE_NAME.fullmatch(zone):
                raise Rejected("invalid_timezone")
            await self.apply_zone(chat_id, identity, zone)

    async def find_zone(self, identity, **what):
        payload = {k: identity[k] for k in ("bot_id", "telegram_user_id", "chat_id")}
        result = await self.core.post("/telegram/timezone-find", {**payload, **what})
        if result.get("found") is not True:
            return None
        zone, label, utc, city = result.get("timezone"), result.get("label"), result.get("utc"), result.get("city")
        if (
            not isinstance(zone, str) or not ZONE_NAME.fullmatch(zone)
            or not isinstance(label, str) or not 0 < len(label) <= 64
            or not isinstance(utc, str) or not UTC_TEXT.fullmatch(utc)
            or not (city is None or isinstance(city, str) and len(city) <= 64)
        ):
            raise RemoteFailure("core_invalid_response")
        return result

    async def typed_zone(self, chat_id, identity, text):
        found = await self.find_zone(identity, text=text)
        if found is None:
            await self.notify(chat_id, ZONE_NOT_FOUND)  # Stays in the waiting state until the time runs out.
            return
        await self.apply_zone(chat_id, identity, found["timezone"])

    async def location(self, chat_id, identity, place):
        """A shared place. After the «Другой» question the zone is set; otherwise the person confirms it first.

        The coordinates go to the core in one request and are dropped. They are not kept here and not logged.
        """
        latitude, longitude = place.get("latitude"), place.get("longitude")
        if any(type(v) not in {int, float} for v in (latitude, longitude)):
            await self.notify(chat_id, ZONE_LOCATION_MISS, reply_markup=MAIN_MENU)
            return
        asked = self.awaiting_zone.pop(chat_id, 0) > self.clock()
        found = await self.find_zone(identity, latitude=latitude, longitude=longitude)
        if found is None:
            await self.notify(chat_id, ZONE_LOCATION_MISS, reply_markup=MAIN_MENU)
            return
        if asked:
            await self.apply_zone(chat_id, identity, found["timezone"])
            return
        near = f"Похоже, вы рядом с городом {found['city']}. " if found.get("city") else ""
        keyboard = [[
            {"text": "Да, сохранить", "callback_data": ZONE_CONFIRM_PREFIX + found["timezone"]},
            {"text": "Нет", "callback_data": ZONE_NO},
        ]]
        await self.notify(
            chat_id, f"{near}Сохранить часовой пояс {found['label']} ({found['utc']})?",
            reply_markup={"inline_keyboard": keyboard},
        )

    async def apply_zone(self, chat_id, identity, zone):
        result = await self.core.post("/telegram/timezone", {**identity, "timezone": zone})
        label, utc, moved = result.get("label"), result.get("utc"), result.get("moved")
        if (
            not isinstance(label, str) or not 0 < len(label) <= 64
            or not isinstance(utc, str) or not UTC_TEXT.fullmatch(utc)
            or type(moved) is not int or not 0 <= moved <= 100000
        ):
            raise RemoteFailure("core_invalid_response")
        self.awaiting_zone.pop(chat_id, None)
        text = f"Часовой пояс: {label} ({utc}). Напоминания будут приходить по этому времени."
        if moved:
            text += f"\nПеренёс {moved} {plural(moved, 'напоминание', 'напоминания', 'напоминаний')} на это время."
        # The main menu replaces the location button, so the temporary keyboard is gone.
        await self.notify(chat_id, text, reply_markup=MAIN_MENU)

    async def answer_callback(self, callback, text=None, *, alert=False):
        payload = {"callback_query_id": callback["id"]}
        if text:
            payload["text"] = text[:200]
            if alert:
                payload["show_alert"] = True
        try:
            await self.telegram.call("answerCallbackQuery", payload)
        except RemoteFailure as error:
            if error.fatal:
                raise

    async def reminder_button(self, callback, chat_id, identity, data):
        """Done, in an hour or tomorrow under a delivered reminder. The core decides, the bot reports."""
        action, token = "done", data
        if data[:2] in ACTION_PREFIXES:
            action, token = ACTION_PREFIXES[data[:2]], data[2:]
            if not TOKEN_CHARS.fullmatch(token):
                return
        payload = {k: v for k, v in identity.items() if k != "chat_id"}
        try:
            result = await self.core.post("/telegram/actions", {**payload, "callback_token": token, "action": action})
        except Rejected as error:
            # Stop the spinner and say why. Nothing else is sent to the chat.
            await self.answer_callback(callback, REJECTIONS[str(error)], alert=True)
            return
        status, local_time = result.get("status"), result.get("local_time")
        if status == "completed":
            outcome, toast = "Готово, отметил выполненным", "Выполнено"
        elif status == "already_completed":
            outcome, toast = "Уже выполнено", "Уже выполнено"
        elif status == "snoozed" and isinstance(local_time, str) and re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", local_time):
            outcome = ("Напомню через час, в " if action == "snooze_hour" else "Напомню завтра в ") + local_time
            toast = outcome
        else:
            raise RemoteFailure("core_invalid_response")
        await self.answer_callback(callback, toast)
        await self.show_outcome(callback["message"], chat_id, outcome)

    async def show_outcome(self, message, chat_id, outcome):
        """Take the buttons away and add the outcome to the same message. If Telegram refuses, send a short new one."""
        message_id = message.get("message_id")
        original = message.get("text")
        text = outcome
        if isinstance(original, str) and original:
            joined = original + "\n\n" + outcome
            if len(joined.encode("utf-16-le")) // 2 <= TELEGRAM_UNITS:
                text = joined
        markup = message.get("reply_markup")
        keyboard = markup.get("inline_keyboard") if isinstance(markup, dict) else None
        links = [
            [{"text": button["text"], "url": button["url"]}]
            for row in (keyboard if isinstance(keyboard, list) else []) if isinstance(row, list)
            for button in row
            if isinstance(button, dict) and isinstance(button.get("text"), str) and isinstance(button.get("url"), str)
        ]
        if type(message_id) is int:
            try:
                await self.telegram.call("editMessageText", {
                    "chat_id": chat_id, "message_id": message_id, "text": text,
                    "link_preview_options": {"is_disabled": True}, "reply_markup": {"inline_keyboard": links},
                })
                return
            except RemoteFailure as error:
                if error.fatal:
                    raise
                logger.warning("bot_edit_failed code=%s", error.code)
        await self.notify(chat_id, outcome)

    async def answer_buttons(self, chat_id, identity, token, sender):
        """Buttons of the sign-in and linking questions. Only the explicit yes reaches the core."""
        if token == LOGIN_DECLINE:
            await self.notify(chat_id, "Хорошо, вход не подтверждён. Если это были не вы, просто закройте ссылку.")
        elif token == LINK_DECLINE:
            await self.notify(chat_id, "Хорошо, Telegram не подключён.")
        elif token.startswith(LOGIN_PREFIX):
            if not LOGIN_TOKEN.fullmatch(token[len(LOGIN_PREFIX):]):
                raise Rejected("invalid_login")
            await self.confirm_login(chat_id, identity, token[len(LOGIN_PREFIX):], sender)
        else:
            code = token[len(LINK_PREFIX):]
            if not LINK_CODE.fullmatch(code):
                raise Rejected("invalid_link")
            await self.request_link(chat_id, identity, sender, code)

    async def request_link(self, chat_id, identity, sender, code):
        result = await self.core.post("/telegram/link-request", {**identity, "code": code})
        link_id = result.get("link_request_id")
        if (
            result.get("status") != "pending"
            or not isinstance(link_id, str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", link_id)
        ):
            raise RemoteFailure("core_invalid_response")
        await self.notify(
            chat_id,
            "Почти готово. Подтверждаю подключение в beresta…",
            self.web + "/#telegram-confirm",
            button="Открыть beresta",
        )
        self.start_watch(chat_id, sender["id"], link_id)

    async def confirm_login(self, chat_id, identity, token, sender):
        """Browser sign-in or account deletion. The core decides what the press means; the bot only reports it."""
        payload = {**identity, "token": token}
        username = sender.get("username")
        if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
            payload["telegram_username"] = username
        result = await self.core.post("/telegram/login-confirm", payload)
        purpose, new_account = result.get("purpose"), result.get("new_account")
        if result.get("status") != "confirmed" or purpose not in {"login", "delete"} or type(new_account) is not bool:
            raise RemoteFailure("core_invalid_response")
        if purpose == "delete":
            text = "Запрос на удаление аккаунта подтверждён. Вернитесь в браузер."
        elif new_account:
            text = "Подтверждено. Вернитесь в браузер, там мы создадим ваш аккаунт."
        else:
            text = "Вход подтверждён. Вернитесь в браузер."
        await self.notify(chat_id, text, reply_markup=MAIN_MENU)

    async def press(self, chat_id, label):
        if label == BTN_WRITE:
            await self.notify(chat_id, "Напиши текст или отправь голосовое. Я сохраню и разберу его с ИИ.")
        elif label == BTN_MANUAL:
            self.manual_until[chat_id] = self.clock() + MANUAL_TTL
            await self.notify(chat_id, "Следующее сообщение сохраню без ИИ.")
        elif label == BTN_LINK:
            await self.notify(
                chat_id,
                "Открой beresta, нажми «Подключить Telegram», затем «Открыть бота».",
                self.web + "/#telegram",
                button="Открыть beresta",
            )
        elif label == BTN_WEB:
            await self.notify(chat_id, "Вот ссылка на beresta.", self.web, button="Открыть beresta")
        elif label == BTN_HELP:
            await self.notify(chat_id, HELP_TEXT, reply_markup=MAIN_MENU)
        else:
            await self.notify(chat_id, "Кнопки снова на месте.", reply_markup=MAIN_MENU)

    async def poll_once(self):
        payload = {"timeout": 25, "limit": 50, "allowed_updates": ["message", "callback_query"]}
        if self.state.offset is not None:
            payload["offset"] = self.state.offset
        updates = await self.telegram.call("getUpdates", payload)
        if not isinstance(updates, list):
            raise RemoteFailure("telegram_invalid_response")
        # Validate the whole batch before committing any offset.
        if any(
            not isinstance(u, dict) or type(u.get("update_id")) is not int or u["update_id"] < 0
            for u in updates
        ):
            raise RemoteFailure("telegram_invalid_response")
        for update in sorted(updates, key=lambda item: item["update_id"]):
            if self.state.offset is not None and update["update_id"] < self.state.offset:
                continue
            await self.handle(update)
            self.state.advance(update["update_id"])
