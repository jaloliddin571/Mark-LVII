"""Parse user requests into a structured action object.

The goal is to convert a free-form request such as "Telegramga @ali ga 'salom' yoz"
into a stable task shape that a tool layer can execute without guessing.

A request that names two actions ("qidirib ber X va YouTube och") comes back as
the first action, for callers that only read one, plus `steps` in spoken order.
"""
from __future__ import annotations

import re

from core.language import normalize_language_code, preferred_language

_QUOTE = re.compile(r"('([^']*)'|\"([^\"]*)\"|«([^»]*)»)")
_SPLIT = re.compile(
    r"\s+(?:va|hamda|keyin|so['’ʻ]?ng|and then|and|then|потом|затем|и)\s+",
    flags=re.IGNORECASE | re.UNICODE,
)
_FOLDS = (
    ("bergin", "bering"),
    ("ochgin", "oching"),
    ("yozgin", "yozing"),
    ("qidirgin", "qidiring"),
    ("yuborgin", "yuboring"),
    ("topgin", "toping"),
    ("korgin", "koring"),
)


def _clean(text: str) -> str:
    return " ".join((text or "").replace("’", "'").replace("ʻ", "'").split())


def _fold_verbs(text: str) -> str:
    """Map a few spoken Uzbek verb endings onto the forms the patterns know."""
    out = text
    for src, dst in _FOLDS:
        out = re.sub(rf"(?i)(?<!\w){re.escape(src)}(?!\w)", dst, out)
    return out


def _mask_quotes(text: str) -> tuple[str, list[str]]:
    held: list[str] = []

    def repl(match: re.Match) -> str:
        held.append(match.group(0))
        return f"‹{len(held) - 1}›"

    return _QUOTE.sub(repl, text), held


def _restore_quotes(text: str, held: list[str]) -> str:
    def repl(match: re.Match) -> str:
        idx = int(match.group(1))
        return held[idx] if 0 <= idx < len(held) else match.group(0)

    return re.sub(r"‹(\d+)›", repl, text)


def _split_clauses(text: str) -> list[str]:
    masked, held = _mask_quotes(text)
    parts = [_restore_quotes(part, held).strip(" ,.;") for part in _SPLIT.split(masked)]
    parts = [part for part in parts if part]
    return parts if len(parts) > 1 else [text]


def _extract_value(text: str, patterns: list[str]) -> str:
    cleaned = _clean(text)
    for pat in patterns:
        m = re.search(pat, cleaned, flags=re.IGNORECASE | re.UNICODE)
        if m:
            val = cleaned[m.end():].strip()
            if val:
                return val.strip("'\"")
    return ""


def _parse_one(raw: str, lang: str) -> dict:
    lowered = raw.lower()

    def _match_any(*values: str) -> bool:
        for value in values:
            phrase = value.lower().strip()
            if " " in phrase:
                if phrase in lowered:
                    return True
            elif re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", lowered, re.UNICODE):
                return True
        return False

    def _extract_after(patterns: list[str], fallback: str = "") -> str:
        for pat in patterns:
            m = re.search(pat, raw, flags=re.IGNORECASE | re.UNICODE)
            if m:
                val = m.group(1).strip() if m.lastindex else m.group(0).strip()
                val = val.strip(" \t\n\r'\"`[](){}")
                if val:
                    return val
        return fallback.strip(" \t\n\r'\"`[](){}")

    action = "unknown"
    target = ""
    argument = ""
    confidence = 0.55

    if _match_any("telegram", "telegramga", "телеграм", "whatsapp", "whatsappga", "wa", "вконтакте", "instagram", "instagramga", "sms") and _match_any(
        "yoz", "yozing", "write", "напиши", "отправь", "yubor", "yuboring", "yuborish"
    ):
        action = "send_message"
        target_match = re.search(
            r"(?:to|ga|кому|для|kanal|gruppa|chat)\s+@?([\w.-]+)|@([\w.-]+)",
            raw,
            flags=re.IGNORECASE,
        )
        if target_match:
            target = (target_match.group(1) or target_match.group(2) or "").strip()
            if target and "@" + target.lower() not in raw.lower() and re.search(r"(?:ga|to|кому|для|kanal|chat)\s+", raw, flags=re.IGNORECASE):
                target = "@" + target
        remaining = raw
        if target:
            target_literal = target if target.startswith("@") else "@" + target
            idx = raw.lower().find(target_literal.lower())
            if idx >= 0:
                remaining = raw[idx + len(target_literal):]
            else:
                idx = raw.lower().find(target.lower())
                if idx >= 0:
                    remaining = raw[idx + len(target):]
        argument = re.sub(r"^(?:ga|to|кому|для|kanal|chat|\s|:)+", "", remaining, flags=re.IGNORECASE)
        argument = re.sub(r"(?:\s+)?(?:yoz(?:ing)?|write|напиши|отправь|send|message|xabar|yubor(?:ing|ish)?)\s*$", "", argument, flags=re.IGNORECASE)
        argument = argument.strip(" \t\n\r'\"")
        if not argument:
            argument = raw
        confidence = 0.9

    elif _match_any(
        "gmail", "email", "pochta", "maktub", "почта", "письмо",
        "telegramdagi", "telegram xabar", "телеграм", "telefon xabar",
        "phone notification", "bildirishnoma",
    ) and _match_any(
        "o'qi", "o'qib", "o'qing", "uqi", "uqib", "прочитай", "читать", "hisobot", "roqsi",
        "ko'r", "tekshir", "check", "show", "покажи",
    ):
        action = "inbox_report"
        source = "all"
        email_hint = _match_any("gmail", "email", "pochta", "почта", "письмо", "maktub")
        telegram_hint = _match_any("telegramdagi", "telegram xabar", "телеграм")
        phone_hint = _match_any("telefon", "phone", "bildirishnoma")
        if email_hint and not telegram_hint and not phone_hint:
            source = "email"
        elif telegram_hint and not email_hint and not phone_hint:
            source = "telegram"
        elif phone_hint and not email_hint and not telegram_hint:
            source = "phone"
        target = source
        argument = source
        confidence = 0.91

    elif _match_any("download", "yuklab", "yuklash", "yuklab ol", "скачать", "download file", "download this", "quyib ber", "quyib bering"):
        action = "download"
        target = _extract_value(raw, [
            r"(?:to|qayerga|куда|в|into|folder|papka|папку)\s+(.+)$",
            r"(?:file|fayl|файл)\s+(?:named|nomi|with|dagi)?\s*(.+)$",
            r"(?:download|yuklab|скачать|quyib ber|quyib bering)\s+(?:the\s+)?(.+)$",
        ])
        argument = target or raw
        confidence = 0.88

    elif _match_any("qidir", "qidiring", "qidirib", "topib ber", "izla", "izlang", "find", "search", "найди", "поиск", "look for", "search for", "searching"):
        action = "search"
        argument = _extract_after([
            r"qidir(?:ing|ib)?(?:\s+topib\s+ber(?:ing)?)?\s+(?:the\s+)?(.+)$",
            r"topib\s+ber(?:ing)?\s+(?:the\s+)?(.+)$",
            r"(?:izla|search(?:\s+for)?|find|найди|ищи|поиск)\s+(?:the\s+)?(.+)$",
        ], raw)
        if not argument:
            argument = raw
        confidence = 0.85

    elif _match_any("open", "och", "oching", "ochib", "открой", "откройте", "открыть", "launch", "start", "run", "boshla", "ochiq"):
        action = "open_app"
        target = _extract_after([
            r"(?:open|och(?:ing|ib)?|открой(?:те)?|открыть|launch|start|run|boshla|ochiq|otkrit)\s+(?:the\s+)?(.+)$",
            r"(?:open|och(?:ing|ib)?|открой(?:те)?|открыть|launch|start|run)\s+(.+)$",
        ], raw)
        if not target:
            target = raw
        target = re.sub(
            r"\s+(?:ni\s+)?(?:och(?:ing|ib)?|open|launch|start|run|открой|откройте)\s*$",
            "",
            target,
            flags=re.IGNORECASE,
        ).strip()
        target = re.sub(r"(?:ni|nı|ты|it)$", "", target, flags=re.IGNORECASE).strip()
        confidence = 0.82

    elif _match_any("kompyuterimdagi", "kompyuterimdagi hamma", "fayllarimdagi", "computer files", "computer contents") and _match_any(
        "hisobot", "report", "o'qib", "oqib", "summarize", "summary"
    ):
        action = "computer_report"
        confidence = 0.92

    elif _match_any("kompyuter", "computer", "ma'lumot", "malumot", "info", "about", "информацию", "система", "состояние", "holati"):
        action = "computer_info"
        confidence = 0.86

    elif _match_any("list files", "fayllar", "fayl", "fayllarni", "papka", "papkalarni", "ro'yxat", "directory", "folder", "список", "папка", "files in", "fayllarni ko'rsat", "fayllarni ko‘rsat"):
        action = "list_files"
        target = re.search(r"(?:in|da|to|into|in folder|papkada|papkasida|в папке|в директории)\s+(.+)$", raw, flags=re.IGNORECASE)
        target = target.group(1).strip() if target else re.search(r"(?:fayllar|files|papka|directory|folder|список|папка)\s+(?:for|in|da)?\s*(.+)$", raw, flags=re.IGNORECASE)
        target = target.group(1).strip() if isinstance(target, re.Match) else ""
        if not target:
            m = re.search(r"(.+?)\s+papkasidagi\s+fayllar", raw, flags=re.IGNORECASE)
            if m:
                target = m.group(1).strip()
        confidence = 0.8

    elif _match_any("yuklab olish", "download", "скачать", "save", "saqlab", "save as"):
        action = "download"
        argument = _extract_after([
            r"(?:download|yuklab|скачать|save|saqlab)\s+(?:the\s+)?(.+)$",
            r"(?:download|yuklab|скачать|save|saqlab)\s+(.+)$",
        ], raw)
        if not argument:
            argument = raw
        confidence = 0.88

    return {
        "action": action,
        "language": lang,
        "target": target.strip(),
        "argument": argument.strip(),
        "confidence": round(confidence, 2),
        "raw": raw,
    }


def parse_command(text: str, language: str | None = None) -> dict:
    """Return a structured command object.

    Fields:
      - action: canonical action type of the first step
      - language: normalized language code
      - target: file/app/contact/query/path/receiver when identifiable
      - argument: main payload (message, search query, file name, etc.)
      - confidence: 0..1 heuristic score
      - steps: present when the request names more than one action, in order
    """
    original = _clean(text)
    if not original:
        return {"action": "unknown", "language": "", "target": "", "argument": "", "confidence": 0.0, "raw": original}

    lang = normalize_language_code(language or preferred_language(original, default=""))
    raw = _fold_verbs(original)
    whole = _parse_one(raw, lang)

    # One report about several inboxes is one action. Splitting on "va" would
    # turn it into two weaker requests and drop the report.
    if whole["action"] in {"inbox_report", "computer_report", "send_message"}:
        whole["raw"] = original
        return whole

    clauses = _split_clauses(raw)
    if len(clauses) < 2:
        whole["raw"] = original
        return whole

    steps = [_parse_one(clause, lang) for clause in clauses]
    if any(step["action"] == "unknown" or step["confidence"] < 0.80 for step in steps):
        # A half-parsed chain would run the first part and drop the rest.
        return {
            "action": "unknown",
            "language": lang,
            "target": "",
            "argument": "",
            "confidence": 0.0,
            "raw": original,
            "steps": steps,
        }

    first = dict(steps[0])
    first["raw"] = original
    first["language"] = lang
    first["confidence"] = round(min(step["confidence"] for step in steps), 2)
    first["steps"] = steps
    return first
