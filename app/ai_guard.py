"""Check what the model said before it is stored or shown.

The model has no tools and sees no code, but a hostile note can still try to make it repeat our
instructions or a key it should not know. Every model result passes through `OutputGuard`:

* secret_value: a loaded secret (model key, internal token, data key, ...) appears in the text;
* prompt_fragment: 60 or more characters of our own prompts are repeated;
* secret_pattern_*: something that looks like a key (`sk-...`, `Bearer ...`, a PEM block,
  `NOTES_X=`, a long token) that the person did not write themselves.

Fragments and patterns that already stand in the person's own text are fine: they did not leak.
Secret values are compared in memory only. Nothing here logs a value, only the name of the rule.
"""

import os
import re

from app.config import read_secret_file
from app.providers import (
    ASK_PROMPT,
    ASSISTANT_RULES,
    DIGEST_PROMPT,
    PROFILE_PROMPT,
    RECOMMEND_PROMPT,
    SYSTEM_PROMPT,
)
from app.schemas import StructuredNote

BLOCKED_CODE = "ai_output_blocked"
PROMPT_WINDOW = 60
MIN_SECRET_LENGTH = 12

# Names of environment settings that hold a secret directly or as a path to a file with it.
SECRET_ENV = (
    "NOTES_CLOUDRU_API_KEY", "NOTES_INTERNAL_API_TOKEN", "NOTES_ANALYTICS_PSEUDONYM_KEY",
    "NOTES_SMTP_BZ_API_KEY", "NOTES_SMTP_PASSWORD", "NOTES_DATABASE_PASSWORD", "NOTES_DATA_KEY",
    "BERESTA_BOT_TOKEN", "BERESTA_SERVICE_TOKEN", "NOTES_FALLBACK_TOKEN",
    # Ключа OpenAI в ядре быть не должно. Если он всё же попал в окружение, ответ с ним не пройдёт.
    "OPENAI_API_KEY",
)
# Settings fields that hold a path to a secret file, and the one that holds a comma separated list.
SECRET_SETTING_FILES = ("data_key_file", "smtp_bz_api_key_file", "smtp_password_file")
SECRET_SETTING_VALUES = ("internal_api_token", "analytics_pseudonym_key", "fallback_token")

_INVISIBLE = "​‌‍‎‏⁠﻿"
_STRIP_INVISIBLE = re.compile("[" + _INVISIBLE + "]")
_COMPACT = re.compile(r"[\s" + _INVISIBLE + "]+")
_WORDS = re.compile(r"[\W_]+")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")
PATTERNS = (
    ("api_key", re.compile(r"\bsk-(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{16,}")),
    ("bearer", re.compile(r"\bBearer\s+(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)),
    ("pem_block", re.compile(r"-----BEGIN [A-Z0-9 ]+-----")),
    ("env_assignment", re.compile(r"\bNOTES_[A-Z0-9_]+\s*=")),
)
# Keys whose values are ids, not text.
_ID_KEYS = frozenset({"note_id", "note_ids", "open_tasks", "kind", "id"})


def _words(text):
    """Letters and digits only, one space between words, lower case. Markdown and spacing do not matter."""
    return _WORDS.sub(" ", _STRIP_INVISIBLE.sub("", text).casefold()).strip()


def _windows(text):
    return {text[start:start + PROMPT_WINDOW] for start in range(max(0, len(text) - PROMPT_WINDOW + 1))}


def _prompt_windows():
    prompts = (SYSTEM_PROMPT, ASSISTANT_RULES, ASK_PROMPT, RECOMMEND_PROMPT, DIGEST_PROMPT, PROFILE_PROMPT)
    return frozenset().union(*(_windows(_words(prompt)) for prompt in prompts))


def _read_value(name):
    value = os.environ.get(name, "")
    path = os.environ.get(name + "_FILE", "")
    found = [value] if value else []
    if path:
        found.append(_read_file(path))
    return found


def _read_file(path):
    try:
        return read_secret_file(path, maximum=65536)
    except (OSError, ValueError):
        return ""


def loaded_secrets(settings=None, provider=None, *more_providers):
    """Secret values that are loaded now. Held in memory for comparison, never logged or returned to a caller."""
    found = []
    for name in SECRET_ENV:
        found.extend(_read_value(name))
    found.append(os.environ.get("NOTES_DATA_KEY", ""))
    for path in os.environ.get("NOTES_DATA_OLD_KEY_FILES", "").split(","):
        if path.strip():
            found.append(_read_file(path.strip()))
    if settings is not None:
        for field in SECRET_SETTING_VALUES:
            found.append(getattr(settings, field, "") or "")
        for field in SECRET_SETTING_FILES:
            path = getattr(settings, field, "") or ""
            if path:
                found.append(_read_file(path))
        for path in (getattr(settings, "data_old_key_files", "") or "").split(","):
            if path.strip():
                found.append(_read_file(path.strip()))
    for client in (provider, *more_providers):
        key = getattr(client, "_api_key", "")
        if isinstance(key, str):
            found.append(key)
    return {value.strip() for value in found if isinstance(value, str) and len(value.strip()) >= MIN_SECRET_LENGTH}


def _strings(value, key=None):
    if key in _ID_KEYS:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for name, inner in value.items():
            yield from _strings(inner, name)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _strings(inner)


class OutputGuard:
    def __init__(self, secrets=()):
        values = {value.strip() for value in secrets if isinstance(value, str) and len(value.strip()) >= MIN_SECRET_LENGTH}
        self._secrets = tuple(_COMPACT.sub("", value) for value in sorted(values))
        self._prompt_windows = _prompt_windows()

    @classmethod
    def for_runtime(cls, settings=None, provider=None, *more_providers):
        return cls(loaded_secrets(settings, provider, *more_providers))

    def violation(self, texts, source=""):
        """Name of the first rule that the texts break, or None. `source` is what the person wrote themselves."""
        texts = [text for text in texts if isinstance(text, str) and text]
        if not texts:
            return None
        compact = [_COMPACT.sub("", text) for text in texts]
        if any(secret in text for secret in self._secrets for text in compact):
            return "secret_value"
        own_words = _words(source)
        for text in texts:
            normalized = _words(text)
            for window in _windows(normalized):
                if window in self._prompt_windows and window not in own_words:
                    return "prompt_fragment"
        for text in texts:
            visible = _STRIP_INVISIBLE.sub("", text)
            for rule, pattern in PATTERNS:
                if any(match.group(0) not in source for match in pattern.finditer(visible)):
                    return "secret_pattern_" + rule
            for match in _LONG_TOKEN.finditer(visible):
                token = match.group(0)
                if re.search(r"\d", token) and re.search(r"[A-Za-z]", token) and token not in source:
                    return "secret_pattern_long_token"
        return None

    def structured_violation(self, result, original):
        texts = [result.title, result.markdown, result.category_name]
        for conclusion in result.conclusions:
            texts += [conclusion.text, conclusion.source_quote]
        for item in result.items:
            texts += [item.text, item.source_quote, item.due_text]
        return self.violation(texts, original)

    def assistant_violation(self, result, context, question=None):
        source = "\n".join([question or "", *(text for texts in context.corpus.values() for text in texts)])
        return self.violation(list(_strings(result)), source)

    @staticmethod
    def safe_note(original):
        """What the person wrote, as a plain note. No conclusions, items or category from the model."""
        first = next((line.strip() for line in original.splitlines() if line.strip()), "Запись")
        return StructuredNote(title=first[:60], markdown=original, conclusions=[], items=[], category_name=None)
