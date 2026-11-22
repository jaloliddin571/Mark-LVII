"""Shared language helpers for the app."""
from __future__ import annotations

import re

_LANGUAGE_ALIASES = {
    "uz": "uz",
    "uzb": "uz",
    "uzbek": "uz",
    "uzbekcha": "uz",
    "uzbekchasi": "uz",
    "o'zbek": "uz",
    "o'zbekcha": "uz",
    "o'zbekchasi": "uz",
    "oʻzbek": "uz",
    "oʻzbekcha": "uz",
    "ozbek": "uz",
    "ozbekcha": "uz",
    "ozbekchasi": "uz",
    "uzbektili": "uz",
    "o'zbektili": "uz",
    "uzbektil": "uz",
    "o'zbektil": "uz",
    "uzbek tili": "uz",
    "uzbektili": "uz",
    "en": "en",
    "eng": "en",
    "english": "en",
    "inglizcha": "en",
    "ingliz": "en",
    "ingliztili": "en",
    "inglizchatili": "en",
    "inglizce": "en",
    "angliyski": "en",
    "anglischa": "en",
    "englishlanguage": "en",
    "tr": "tr",
    "turk": "tr",
    "turkish": "tr",
    "turkce": "tr",
    "turkcha": "tr",
    "turktili": "tr",
    "turkcedil": "tr",
    "türkçe": "tr",
    "türkcha": "tr",
    "turk dili": "tr",
    "ru": "ru",
    "russian": "ru",
    "ruscha": "ru",
    "rus": "ru",
    "russkiy": "ru",
    "russkij": "ru",
    "russianlanguage": "ru",
    "russkiyyazyk": "ru",
    "russkiyazyk": "ru",
    "русский": "ru",
    "русск": "ru",
    "русса": "ru",
    "русскийязык": "ru",
    "ar": "ar",
    "arabic": "ar",
    "arabcha": "ar",
    "arab": "ar",
    "arabiclanguage": "ar",
    "arabiclanguage": "ar",
}

_LANGUAGE_HINTS = {
    "uz": {
        "uzbek", "uzbekcha", "o'zbek", "oʻzbek", "ozbek", "salom", "qidir",
        "qidirib", "topib", "toping", "quyib", "yuklab", "yozsin", "tekshirib",
        "bajar", "bering", "yozish", "qo'shig'", "kompyuterim", "malumot",
        "papka", "fayl", "telegram", "youtubega", "youtube", "kanal",
        "menga", "bering", "bergin", "qilib", "loyiha", "yaxshi", "yaxshilab",
    },
    "ru": {
        "русский", "русск", "найди", "найти", "открой", "открыть", "напиши",
        "сделай", "покажи", "запусти", "скажи", "проверь", "сообщение",
        "файл", "папка", "telegram", "youtube", "папку", "поиск", "ищи",
    },
    "en": {
        "english", "englishplease", "open", "find", "write", "show", "search",
        "download", "save", "send", "check", "computer", "file", "browser",
        "telegram", "youtube", "launch", "look for", "search for", "send message",
    },
    "tr": {
        "turkce", "turkcha", "aç", "bul", "göster", "yaz", "indir", "dosya",
        "bilgisayar", "arama", "gönder", "telegram", "youtube", "kayıt",
    },
}

_LANGUAGE_NAMES = {
    "uz": "Uzbek",
    "en": "English",
    "tr": "Turkish",
    "ru": "Russian",
    "ar": "Arabic",
}


def normalize_language_code(code: str | None) -> str:
    """Turn a user-provided name or code into an app-normalized language code.

    Examples:
        "uz" -> "uz"
        "Uzbek" -> "uz"
        "o'zbek" -> "uz"
        "uz_UZ" -> "uz"
    """
    text = (code or "").strip().lower().replace("ʻ", "'").replace("’", "'")
    if not text:
        return ""

    text = text.replace("\t", "").replace("\n", "")
    text = text.split("_", 1)[0].split("-", 1)[0]
    text = text.replace(" ", "")
    text = re.sub(r"[^a-zA-Z0-9'А-Яа-яЁё]", "", text)
    text = text.replace("ü", "u").replace("ç", "c").replace("ö", "o").replace("ğ", "g").replace("ş", "s")
    text = text.replace("\u00a0", "")

    candidates = []
    for candidate in (text, text.replace("'", "")):
        if candidate:
            candidates.append(candidate)

    for candidate in candidates:
        direct = _LANGUAGE_ALIASES.get(candidate)
        if direct:
            return direct

    return ""


def language_name(code: str | None) -> str:
    """Return a human-readable name for a normalized or user-supplied language."""
    normalized = normalize_language_code(code)
    if not normalized:
        return ""
    return _LANGUAGE_NAMES.get(normalized, normalized.title())


def user_language_name(value: str | dict | None, default: str = "English") -> str:
    """Best-effort language label for user-facing prompts and saved memory."""
    if isinstance(value, dict):
        value = value.get("value") or value.get("language") or value.get("code") or ""
    label = str(value or "").strip()
    if not label:
        return default
    normalized = normalize_language_code(label)
    if not normalized:
        return label.title() if label else default
    return language_name(normalized) or default


def _text_language_hint(text: str) -> str:
    """Best-effort language detection for raw user text.

    This is intentionally conservative: it looks for repeated language-specific
    cues in the message, rather than guessing from a single unrelated word.
    """
    clean = (text or "").strip().lower().replace("ʻ", "'").replace("’", "'")
    if not clean:
        return ""
    normalized = re.sub(r"[^a-zA-Z0-9\s'А-Яа-яЁё]", " ", clean)
    tokens = set(normalized.split())
    scores = {code: 0 for code in _LANGUAGE_HINTS}
    for code, hints in _LANGUAGE_HINTS.items():
        for hint in hints:
            if hint in clean or hint in normalized:
                scores[code] += 2
        for token in tokens:
            if token in hints:
                scores[code] += 3
    best = max(scores.items(), key=lambda item: item[1], default=("", 0))
    if best[1] <= 0:
        return ""
    return best[0]


def preferred_language(value: str | dict | None, default: str = "") -> str:
    """Return the normalized language code, falling back to a stable label."""
    if isinstance(value, dict):
        value = value.get("value") or value.get("language") or value.get("code") or ""
    code = normalize_language_code(value)
    if code:
        return code
    label = str(value or "").strip()
    if not label:
        return default
    hint = _text_language_hint(label)
    return hint or label


def language_from_voice_name(voice: str | None) -> str:
    """Return the language code implied by a Piper voice name."""
    if not voice:
        return ""
    # Piper voice names look like "tr_TR-dfki-medium" / "en_US-lessac-medium".
    head = (voice or "").strip().split("_", 1)[0].split("-", 1)[0]
    return normalize_language_code(head)
