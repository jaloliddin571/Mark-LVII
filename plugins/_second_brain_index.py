"""
Shared driver for second_brain: reads an Obsidian vault into a graph and searches it.

Not a skill (the leading underscore makes the loader skip it). second_brain.py and
_second_brain_window.py both import it, so there is one index for the whole app.

An Obsidian vault is a folder of .md files; [[wikilinks]] between them are the
edges. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "_second_brain")
GRAPH_JS = os.path.join(WEB_DIR, "graph-data.js")

SKIP_DIRS = {".obsidian", ".trash", ".git"}
CAPTURE_DIR = "Jarvis"            # notes the assistant writes go here, inside the vault
EXCERPT_CHARS = 4000           # what the note panel shows; the graph itself never draws it
WIKILINK = re.compile(r"\[\[([^\]|#^]+)")
FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)

# Short function words in the languages people actually speak to it. Anything
# not listed still works; these only stop "what" and "что" from matching everything.
STOPWORDS = set("""
the and are for from has have how its not that this was what when where which who why will with you your
about tell into than then there these they them some any can could would should please know notes note
что как это для или его она они про мне мой моя мои есть был была было были где когда кто чем при над
под без так тоже ещё еще уже вот все всё мои твой ваш наш заметки заметка заметках
""".split())


def _words(text: str) -> set[str]:
    """Lower-cased word stems. Five letters of prefix is enough to match most
    inflections ("заметки"/"заметках", "delivery"/"deliveries") without a stemmer."""
    out = set()
    for w in re.findall(r"\w+", text.lower()):
        if len(w) > 2 and w not in STOPWORDS and not w.isdigit():
            out.add(w[:5] if len(w) > 5 else w)
    return out


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("o‘", "o").replace("o'", "o").replace("g‘", "g").replace("g'", "g")
    text = text.replace("ў", "у").replace("қ", "к").replace("ғ", "г").replace("ҳ", "х")
    return " ".join(re.findall(r"[\w]+", text, flags=re.UNICODE))


class Vault:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.nodes: list[dict] = []
        self.by_id: dict[int, dict] = {}
        self.links: list[dict] = []
        self._stamp = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- reading
    def _files(self) -> list[str]:
        found = []
        for dirpath, dirnames, files in os.walk(self.path):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            found += [os.path.join(dirpath, f) for f in sorted(files) if f.lower().endswith(".md")]
        return found

    def refresh(self) -> bool:
        """Re-reads the vault if anything changed since last time. True when it did."""
        with self._lock:
            files = self._files()
            stamp = []
            for path in files:
                stat = os.stat(path)
                stamp.append((
                    os.path.relpath(path, self.path).replace(os.sep, "/"),
                    stat.st_mtime_ns,
                    stat.st_size,
                ))
            stamp = tuple(stamp)
            if stamp == self._stamp:
                return False
            self._scan(files)
            self._stamp = stamp
            return True

    def _scan(self, files: list[str]) -> None:
        # Ids stay stable across rescans: a note keeps its id for as long as the app runs and a new
        # note gets the next free one, so an open galaxy can add a star without renumbering the others.
        known = {n["path"]: n["id"] for n in self.nodes}
        next_id = max(known.values(), default=-1) + 1
        nodes = []
        for path in files:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            rel = os.path.relpath(path, self.path).replace(os.sep, "/")
            parts = rel.split("/")
            label = os.path.splitext(parts[-1])[0]
            body = FRONTMATTER.sub("", text)
            if rel in known:
                node_id = known[rel]
            else:
                node_id, next_id = next_id, next_id + 1
            nodes.append({
                "id": node_id, "label": label,
                "group": parts[0] if len(parts) > 1 else "vault",
                # The related-notes block's HTML markers are for the linker, not for reading.
                "excerpt": re.sub(r"<!--.*?-->", "", body)[:EXCERPT_CHARS].strip(), "path": rel,
                "text": body, "title_words": _words(label), "body_words": _words(body),
                "wikilinks": {w.strip().split("/")[-1].lower() for w in WIKILINK.findall(body)},
                "search_text": _normalize(label + " " + body),
            })
        self.nodes = sorted(nodes, key=lambda n: n["id"])
        self.by_id = {n["id"]: n for n in self.nodes}
        self.links = self._link_all(self.nodes)
        self._write_graph()

    @staticmethod
    def _related(a: dict, b: dict) -> bool:
        """The same rule Obsidian's graph draws, plus plain mentions of a title."""
        la, lb = a["label"].lower(), b["label"].lower()
        if lb in a["wikilinks"] or la in b["wikilinks"]:
            return True
        return (len(lb) >= 4 and lb in a["text"].lower()) or (len(la) >= 4 and la in b["text"].lower())

    def _link_all(self, nodes: list[dict]) -> list[dict]:
        return [{"source": a["id"], "target": b["id"]}
                for i, a in enumerate(nodes) for b in nodes[i + 1:] if self._related(a, b)]

    def _write_graph(self) -> None:
        public = [{k: n[k] for k in ("id", "label", "group", "excerpt", "path")} for n in self.nodes]
        os.makedirs(WEB_DIR, exist_ok=True)
        tmp = GRAPH_JS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("const GRAPH = " + json.dumps({"nodes": public, "links": self.links}, ensure_ascii=False) + ";\n")
        os.replace(tmp, GRAPH_JS)

    # ---------------------------------------------------------------- asking
    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Notes that match the query, best first: title words count three times."""
        self.refresh()
        normalized = _normalize(query)
        asked = _words(normalized)
        scored = []
        for n in self.nodes:
            title = _normalize(n["label"])
            body = n["search_text"]
            title_hits = len(asked & n["title_words"])
            body_hits = len(asked & n["body_words"])
            score = 5 * title_hits + 2 * body_hits
            if normalized and normalized in body:
                score += 8
            if normalized and normalized in title:
                score += 12
            if score:
                scored.append((score, n["id"]))
        scored = sorted(scored, key=lambda s: (-s[0], s[1]))
        best = scored[0][0] if scored else 0
        return [self.by_id[i] for score, i in scored[:limit] if score > 0 and score >= best * 0.25]

    def recent(self, limit: int = 5) -> list[dict]:
        """Notes most recently modified on disk, useful for 'what did I save recently?'."""
        self.refresh()
        ranked = []
        for n in self.nodes:
            path = os.path.join(self.path, n["path"])
            try:
                ranked.append((os.path.getmtime(path), n))
            except OSError:
                continue
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [n for _mtime, n in ranked[:limit]]

    def update(self, query: str, text: str) -> dict:
        """Append text to the best matching existing note."""
        text = text.strip()
        if not text:
            raise ValueError("there was nothing to add")
        matches = self.search(query, limit=1)
        if not matches:
            raise ValueError("no note matched that name or topic")
        node = matches[0]
        path = os.path.join(self.path, node["path"])
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n\n- {text}\n")
        self.refresh()
        return next(n for n in self.nodes if n["path"] == node["path"])

    # ---------------------------------------------------------------- writing
    def add(self, text: str) -> dict:
        """Writes a real note into the vault and returns its node once it is indexed."""
        text = text.strip()
        if not text:
            raise ValueError("there was nothing to write down")
        words = re.findall(r"[\w'-]+", text)[:7]
        title = re.sub(r'[<>:"/\\|?*#^\[\]]', "", " ".join(words)).strip() or "Note"
        title = title[0].upper() + title[1:]
        folder = os.path.join(self.path, CAPTURE_DIR)
        os.makedirs(folder, exist_ok=True)
        path, k = os.path.join(folder, title + ".md"), 2
        while os.path.exists(path):
            path, k = os.path.join(folder, f"{title} {k}.md"), k + 1
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"---\ncreated: {time.strftime('%Y-%m-%d %H:%M')}\nsource: Jarvis\n---\n\n"
                    f"{text[0].upper() + text[1:]}\n")
        self.refresh()
        rel = os.path.relpath(path, self.path).replace(os.sep, "/")
        node = next((n for n in self.nodes if n["path"] == rel), None)
        if node is None:
            raise RuntimeError("the note was written but did not show up in the index")
        return node

    def nearest(self, node: dict) -> int | None:
        """The existing note closest in wording, where a new star is born."""
        others = [n for n in self.nodes if n["id"] != node["id"]]
        if not others:
            return None
        return max(others, key=lambda n: len(n["body_words"] & node["body_words"]))["id"]
