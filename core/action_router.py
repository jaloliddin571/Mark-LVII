"""Direct routing for unambiguous natural-language commands."""
from __future__ import annotations

from core.command_parser import parse_command
from core import outcome


def _runnable(registry, command: dict) -> bool:
    action = command["action"]
    if action == "computer_report":
        return registry.has("computer_report")
    if action == "inbox_report":
        return registry.has("inbox_report")
    if action == "open_app":
        return bool(command["target"]) and registry.has("open_app")
    if action == "search":
        return bool(command["argument"]) and (
            registry.has("web_search") or registry.has("browser_control")
        )
    if action == "send_message":
        return bool(command["target"] and command["argument"]) and registry.has("send_message")
    if action == "computer_info":
        return True
    if action == "list_files":
        return bool(command["target"]) and registry.has("file_controller")
    return False


def _dispatch(registry, command: dict, text: str, context: dict) -> str | None:
    action = command["action"]
    if action == "computer_report" and registry.has("computer_report"):
        return registry.run("computer_report", {}, context)

    if action == "inbox_report" and registry.has("inbox_report"):
        source = command["target"] or "all"
        return registry.run("inbox_report", {"source": source}, context)

    if action == "open_app" and command["target"] and registry.has("open_app"):
        return registry.run("open_app", {"app_name": command["target"]}, context)

    if action == "search" and command["argument"]:
        if registry.has("web_search"):
            return registry.run("web_search", {"query": command["argument"]}, context)
        if registry.has("browser_control"):
            return registry.run(
                "browser_control",
                {"action": "search", "query": command["argument"]},
                context,
            )

    if action == "send_message" and command["target"] and command["argument"]:
        if registry.has("send_message"):
            platform = "Telegram"
            low = text.lower()
            if "whatsapp" in low or "вацап" in low or "ватсап" in low:
                platform = "WhatsApp"
            elif "instagram" in low:
                platform = "Instagram"
            elif "discord" in low:
                platform = "Discord"
            elif "signal" in low:
                platform = "Signal"
            return registry.run(
                "send_message",
                {
                    "platform": platform,
                    "receiver": command["target"].lstrip("@"),
                    "message_text": command["argument"],
                },
                context,
            )

    if action == "computer_info":
        if registry.has("computer_settings"):
            return registry.run("computer_settings", {"action": "status"}, context)
        try:
            from actions.system_monitor import get_system_status
            status = get_system_status()
            return outcome.done(
                "computer_info",
                cpu=status["cpu_percent"],
                ram=status["ram_percent"],
                uptime=status["uptime"],
                processes=status["process_count"],
            )
        except Exception as exc:
            return outcome.failed("computer_info", f"could not read system status: {exc}")

    if action == "list_files" and command["target"] and registry.has("file_controller"):
        return registry.run(
            "file_controller",
            {"action": "list", "path": command["target"]},
            context,
        )
    return None


def route_text_command(registry, text: str, player=None) -> str | None:
    """Execute a high-confidence command through the existing action registry.

    Several actions in one sentence run in spoken order. If any of them is not
    a direct command, nothing runs: a partial chain would drop the rest.
    """
    command = parse_command(text)
    steps = command.get("steps") or [command]
    if any(step.get("confidence", 0) < 0.80 or not _runnable(registry, step) for step in steps):
        return None

    context = {"player": player} if player is not None else {}
    lines: list[str] = []
    try:
        for step in steps:
            result = _dispatch(registry, step, step.get("raw") or text, context)
            if not result:
                return "\n".join(lines) if lines else None
            lines.append(result)
            if str(result).startswith("failed"):
                break
    except Exception as exc:
        lines.append(outcome.failed(command["action"], f"direct routing failed: {exc}"))
    return "\n".join(lines) if lines else None
