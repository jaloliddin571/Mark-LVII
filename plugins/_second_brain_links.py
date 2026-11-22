"""
Shared driver for second_brain: links notes to each other by meaning, by itself.

Not a skill (the leading underscore makes the loader skip it).

HOW
    Every note gets an embedding (a meaning fingerprint) from the same
    OpenAI-compatible router the assistant already uses (FreeLLMAPI ->
    /v1/embeddings). Notes whose fingerprints are close are related, in any
    language and without sharing a single word: "Instagram content ideas" and
    "posts for social networks" come out close. Fingerprints are cached by the
    note's content, so an unchanged note is never sent twice. With no router,
    no key or no quota, shared words decide instead, so linking never just
    stops.

WHERE THE LINKS GO
    Into the notes themselves, as real [[wikilinks]], so Obsidian's own graph
    shows them too. They live in one marked block at the end of the note:

        <!-- jarvis:related -->
        🔗 [[Note A]] · [[Note B]]
        <!-- /jarvis:related -->

    Only that block is ever written or rewritten; the user's text is never
    touched. unlink_all() removes every block again.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import urllib.request

from plugins import _second_brain_index as index

CACHE = os.path.join(index.WEB_DIR, "embeddings.json")
DIMENSIONS = 256
MODEL = "auto"
# Thresholds are RELATIVE to the vault. Measured: in a vault of short Uzbek notes every pair scored
# 0.61-0.91 (median 0.69), in an English one even unrelated notes scored 0.68+ (median 0.77). The
# model finds everything in one language and style "a bit alike", so a fixed cut-off links all or
# nothing. A pair is related when it stands out from this vault's own spread.
RELATED_SD = 1.0       # related: at least this many standard deviations above the vault's mean pair
ORPHAN_SD = 0.0        # a note with no link at all still joins its closest note if that is above the mean
MAX_LINKS = 4          # per note, so a popular topic does not turn into a hairball
WORD_OVERLAP = 2       # the fallback: stems two notes must share

BLOCK = re.compile(r"\n*<!-- jarvis:related -->.*?<!-- /jarvis:related -->\n?", re.S)
_lock = threading.Lock()


def strip_block(text: str) -> str:
    return BLOCK.sub("\n", text).rstrip() + "\n"


# ------------------------------------------------------------------ fingerprints
def _router() -> tuple[str, str] | None:
    """The address and key of the OpenAI-compatible brain set up in ENGINE & PROVIDERS."""
    try:
        from memory.config_manager import get_providers
        c = (get_providers().get("creds") or {}).get("openai_compat") or {}
        base, key = (c.get("base_url") or "").rstrip("/"), c.get("api_key") or ""
        return (base, key) if base else None
    except Exception:
        return None


def _load_cache() -> dict:
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE)


def _content(n: dict) -> str:
    return (n["label"] + "\n" + strip_block(n["text"]))[:2000]


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _embed(texts: list[str]) -> list[list[float]]:
    base, key = _router()
    body = json.dumps({"model": MODEL, "input": texts, "dimensions": DIMENSIONS}).encode()
    req = urllib.request.Request(f"{base}/embeddings", data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    # A proxy set for the whole machine must not be used to reach a router on this one.
    local = "127.0.0.1" in base or "localhost" in base
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}) if local else urllib.request.ProxyHandler())
    with opener.open(req, timeout=60) as r:
        data = json.load(r)["data"]
    return [d["embedding"] for d in sorted(data, key=lambda d: d.get("index", 0))]


def fingerprints(nodes: list[dict]) -> dict[int, list[float]] | None:
    """Node id -> embedding, fetching only what the cache lacks. None when the router cannot be used."""
    if not nodes or _router() is None:
        return None
    cache = _load_cache()
    texts = {n["id"]: _content(n) for n in nodes}
    missing = [(nid, t) for nid, t in texts.items() if _key(t) not in cache]
    try:
        for i in range(0, len(missing), 64):
            batch = missing[i:i + 64]
            for (nid, t), vec in zip(batch, _embed([t for _, t in batch])):
                cache[_key(t)] = [round(x, 5) for x in vec]
    except Exception as e:
        print(f"[SecondBrain] meaning-based links unavailable ({e}); linking by shared words")
        return None
    if missing:
        alive = {_key(t) for t in texts.values()}
        _save_cache({k: v for k, v in cache.items() if k in alive})
    return {nid: cache[_key(t)] for nid, t in texts.items()}


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ------------------------------------------------------------------ deciding
def related_pairs(nodes: list[dict]) -> tuple[dict[int, list[int]], str]:
    """Node id -> the ids it should link to, best first; and how it was decided."""
    vecs = fingerprints(nodes)
    ids = [n["id"] for n in nodes]
    if len(ids) < 2:
        return {i: [] for i in ids}, "nothing to link yet"
    if vecs is not None:
        near, (mean, sd) = _nearest_by_meaning(ids, vecs)
        how, strong, weak = "meaning", mean + RELATED_SD * sd, mean + ORPHAN_SD * sd
    else:
        near = _nearest_by_words(nodes)
        how, strong, weak = "shared words", WORD_OVERLAP, WORD_OVERLAP
    chosen: dict[int, dict[int, float]] = {i: {} for i in ids}
    for a in ids:
        ranked = near[a]
        picks = [(s, b) for s, b in ranked if s >= strong][:MAX_LINKS]
        if not picks and ranked and ranked[0][0] >= weak:
            picks = ranked[:1]              # an orphan still joins the note closest to it
        for s, b in picks:                  # links are mutual: both notes list each other
            chosen[a][b] = s
            chosen[b][a] = s
    return {a: sorted(bs, key=lambda b: -bs[b])[:MAX_LINKS + 2] for a, bs in chosen.items()}, how


def _nearest_by_meaning(ids: list[int], vecs: dict):
    """Each note's closest notes, best first, and (mean, sd) of all pairs in the vault.
    One matrix product rather than every pair in Python."""
    k = MAX_LINKS + 4
    try:
        import numpy as np
        m = np.array([vecs[i] for i in ids], dtype=np.float32)
        m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
        sim = m @ m.T
        pairs = sim[np.triu_indices(len(ids), 1)]
        stats = (float(pairs.mean()), float(pairs.std()))
        np.fill_diagonal(sim, -1.0)
        out = {}
        for row, a in enumerate(ids):
            top = np.argsort(-sim[row])[:k]
            out[a] = [(float(sim[row, j]), ids[j]) for j in top if j != row]
        return out, stats
    except ImportError:
        out = {a: sorted(((_cosine(vecs[a], vecs[b]), b) for b in ids if b != a), reverse=True) for a in ids}
        pairs = [_cosine(vecs[a], vecs[b]) for i, a in enumerate(ids) for b in ids[i + 1:]]
        mean = sum(pairs) / len(pairs)
        sd = math.sqrt(sum((p - mean) ** 2 for p in pairs) / len(pairs))
        return {a: v[:k] for a, v in out.items()}, (mean, sd)


def _nearest_by_words(nodes: list[dict]) -> dict[int, list[tuple[float, int]]]:
    words = {n["id"]: n["body_words"] | n["title_words"] for n in nodes}
    return {a: sorted(((float(len(words[a] & words[b])), b) for b in words if b != a), reverse=True)[:MAX_LINKS + 4]
            for a in words}


# ------------------------------------------------------------------ writing
def _write_block(vault: index.Vault, node: dict, labels: list[str]) -> bool:
    """Rewrites only the marked block. True when the file changed."""
    path = os.path.join(vault.path, node["path"])
    with open(path, encoding="utf-8") as f:
        old = f.read()
    base = strip_block(old)
    already = {w.strip().split("/")[-1].lower() for w in index.WIKILINK.findall(base)}
    labels = [l for l in labels if l.lower() not in already]     # the user linked it already: say nothing
    new = base if not labels else base.rstrip() + (
        "\n\n<!-- jarvis:related -->\n🔗 " + " · ".join(f"[[{l}]]" for l in labels) + "\n<!-- /jarvis:related -->\n")
    if new == old:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    return True


def link_all(vault: index.Vault) -> dict:
    """Links every note to its related notes. Returns what was done."""
    with _lock:
        vault.refresh()
        nodes = list(vault.nodes)
        pairs, how = related_pairs(nodes)
        by_id = {n["id"]: n for n in nodes}
        changed = sum(_write_block(vault, by_id[a], [by_id[b]["label"] for b in bs]) for a, bs in pairs.items())
        vault.refresh()
        linked = sum(1 for bs in pairs.values() if bs)
        return {"notes": len(nodes), "linked": linked, "files_changed": changed, "by": how,
                "links": len(vault.links)}


def unlink_all(vault: index.Vault) -> int:
    """Removes every block this module wrote. Returns how many files changed."""
    with _lock:
        changed = 0
        for n in vault.nodes:
            path = os.path.join(vault.path, n["path"])
            with open(path, encoding="utf-8") as f:
                old = f.read()
            if BLOCK.search(old):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(strip_block(old))
                changed += 1
        vault.refresh()
        return changed
