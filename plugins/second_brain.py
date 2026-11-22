"""
second_brain — the user's Obsidian vault as a second brain, drawn as a 3D galaxy.

One tool, five-plus actions: show / hide the galaxy, search, remember, update,
recent, link and unlink. The trigger is the model, not a phrase list,
so it works in whatever language the user speaks. Ctrl+Shift+B and the 🧠 button
in the HUD header do show / hide without a word.

Set the vault folder in ⚙ → PLUGIN SETTINGS → Second Brain.
The window and the index live in _second_brain_window.py / _second_brain_index.py.
"""
import os

from core.outcome import done, failed, unverified, untrusted
from plugins import _second_brain_links as links
from plugins import _second_brain_window as brain

PLUGIN = {
    "name": "second_brain",
    "description": (
        "The user's SECOND BRAIN: their Obsidian notes vault, shown as a graph of linked notes. "
        "Works in any language. Use action='show' when they ask to show / open / display their brain, "
        "second brain, notes, galaxy or mind map (e.g. 'покажи мой мозг', 'show my brain', "
        "'miyamni ko'rsat'); add query to fly to a topic ('show my notes about coffee'). "
        "action='hide' to close it. action='search' with query when they ask what their notes say "
        "about something. action='remember' with text when they ask to write something down, save "
        "it to their notes, vault, Obsidian or second brain (it is linked to related notes by itself). "
        "action='update' with query and text to add information to the best matching existing note. "
        "action='recent' to name the notes most recently written or changed. "
        "action='link' when they ask to connect / link / relate their notes; action='unlink' to remove "
        "the links the assistant added. "
        "Do NOT use file_processor or file_controller for the vault. Facts about the user themselves "
        "(their name, preferences) belong to the assistant's own memory, not here."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "show | hide | search | remember | update | recent | link | unlink"},
            "query": {"type": "STRING", "description": "for show/search: the topic, in the user's own words"},
            "text": {"type": "STRING", "description": "for remember: the note to write, complete and in the user's language"},
        },
        "required": ["action"],
    },
    # A router on this PC (FreeLLMAPI, LM Studio...) gets a tool budget; core tools survive it.
    "tier": "core",
    "brief": "User's Obsidian notes as a graph, any language: show/hide it, search (query), "
             "remember (text) writes a linked note, update (query + text) extends a note, "
             "recent lists recent notes, link connects notes, unlink undoes that.",
}

PLUGIN_SETTINGS = {
    "title": "Second Brain (Obsidian)",
    "fields": [
        {"key": "vault_path", "label": "Obsidian vault folder", "type": "text",
         "placeholder": r"F:\jarvis_local\Obsidian vault"},
        {"key": "display", "label": "Where it opens", "type": "choice",
         "options": ["Inside the HUD", "Second monitor"], "default": "Inside the HUD"},
    ],
}

NOTE_CHARS = 1500

brain.start_hotkey()


def _no_vault(action: str) -> str:
    path = brain.vault_path()
    reason = (f"the vault folder {path} does not exist" if path else "no Obsidian vault folder is set yet")
    return failed(action, reason + ". Set it in ⚙ → PLUGIN SETTINGS → Second Brain (Obsidian)")


def _search(vault, query: str) -> list:
    return vault.search(query) if query else []


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = (parameters.get("action") or "show").strip().lower()
    query = (parameters.get("query") or "").strip()
    try:
        vault = brain.get_vault()
        if action == "hide":
            return done("second brain closed") if brain.hide() else unverified(
                "close second brain", "the window did not report that it closed")
        if vault is None:
            return _no_vault(f"second brain {action}")

        if action == "show":
            notes = _search(vault, query)
            state = brain.show(focus_id=notes[0]["id"] if len(notes) in (1, 2, 3) else None,
                               light_ids=[n["id"] for n in notes] if len(notes) >= 4 else None)
            if state.get("error"):
                return failed("show second brain", state["error"])
            if not state.get("visible"):
                return unverified("show second brain", "the window did not report that it is visible")
            facts = {"where": state["where"], "notes": len(vault.nodes), "links": len(vault.links)}
            if query:
                facts["matching"] = ", ".join(n["label"] for n in notes) or "nothing matched"
            return done("second brain shown", **facts)

        if action == "search":
            notes = _search(vault, query)
            if not notes:
                return done("searched the notes", query=query, found=0,
                            note="nothing in the vault matches; say so plainly, do not guess")
            if len(notes) >= 4:
                brain.light([n["id"] for n in notes])
            else:
                brain.focus(notes[0]["id"])
            content = "\n\n".join(f"# {n['label']}  ({n['path']})\n{n['text'][:NOTE_CHARS].strip()}" for n in notes)
            return (done("searched the notes", query=query, found=len(notes),
                         sources=", ".join(n["label"] for n in notes))
                    + "\nAnswer ONLY from these notes, briefly, and name the note it came from.\n"
                    + untrusted("Obsidian vault", content))

        if action == "recent":
            notes = vault.recent(limit=5)
            if not notes:
                return done("recent notes", found=0, note="the vault is empty")
            brain.light([n["id"] for n in notes])
            return done(
                "recent notes",
                found=len(notes),
                titles=", ".join(n["label"] for n in notes),
                paths=", ".join(n["path"] for n in notes),
            )

        if action == "update":
            text = (parameters.get("text") or "").strip()
            if not query or not text:
                return failed("update a note", "both query and text are required")
            node = vault.update(query, text)
            brain.reload_open()
            return done("note updated", title=node["label"], file=node["path"], added=text)

        if action == "remember":
            text = (parameters.get("text") or query).strip()
            node = vault.add(text)
            path = os.path.join(vault.path, node["path"])
            if not os.path.isfile(path):
                return failed("write the note", "the file is not on disk after writing")
            linked = links.link_all(vault)          # the new note joins its related notes by itself
            node = next(n for n in vault.nodes if n["path"] == node["path"])
            mine = [l for l in vault.links if node["id"] in (l["source"], l["target"])]
            related = [vault.by_id[l["target"] if l["source"] == node["id"] else l["source"]]["label"] for l in mine]
            brain.add_star(node, mine, vault.nearest(node))
            return done("note written to the vault", title=node["label"], file=node["path"],
                        linked_to=", ".join(related) or "nothing related yet", linked_by=linked["by"])

        if action == "link":
            result = links.link_all(vault)
            brain.reload_open()
            return done("notes linked to each other", notes=result["notes"], notes_with_links=result["linked"],
                        links=result["links"], files_changed=result["files_changed"], decided_by=result["by"])

        if action == "unlink":
            changed = links.unlink_all(vault)
            brain.reload_open()
            return done("the assistant's links removed", files_changed=changed, links_left=len(vault.links))

        return failed(
            f"second brain {action}",
            "unknown action; use show, hide, search, remember, update, recent, link or unlink",
        )
    except Exception as e:
        return failed(f"second brain {action}", str(e))
