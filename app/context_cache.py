import asyncio
import json
import re
import time
from difflib import SequenceMatcher
import aiosqlite

_STOP = {"the","a","an","is","are","am","do","does","did","i","me","my","you","your",
"hai","ka","ki","ke","ko","me","mein","mujhe","mera","mere","sir","mam","bhai",
"course","courses","batch","batches","please","plz","kya","h","kaisa","kaise",
"what","which","have","for","of","and","or","need","chahiye"}

def _norm(s):
    s = str(s or "").casefold().replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()

def _tokens(s):
    return {x for x in re.findall(r"[a-z0-9\u0900-\u097f]+", _norm(s))
            if len(x) > 1 and x not in _STOP}

class BusinessCache:
    def __init__(self, path, refresh_seconds=60):
        self.path = path
        self.refresh_seconds = refresh_seconds
        self.lock = asyncio.Lock()
        self.loaded_at = 0.0
        self.batches = []
        self.courses = []

    async def refresh(self, force=False):
        async with self.lock:
            if not force and self.loaded_at and time.monotonic()-self.loaded_at < self.refresh_seconds:
                return
            async with aiosqlite.connect(self.path) as db:
                db.row_factory = aiosqlite.Row
                self.batches = [dict(r) for r in await (await db.execute(
                    "SELECT id,name,description,aliases_json,search_keywords,enabled FROM batches WHERE enabled=1 ORDER BY id")).fetchall()]
                self.courses = [dict(r) for r in await (await db.execute(
                    "SELECT id,name,description,aliases_json,keywords,enabled FROM courses WHERE enabled=1 ORDER BY id")).fetchall()]
            for r in self.batches + self.courses:
                try:
                    a = json.loads(r.get("aliases_json") or "[]")
                    r["_aliases"] = a if isinstance(a, list) else []
                except Exception:
                    r["_aliases"] = []
            self.loaded_at = time.monotonic()

    def _score(self, message, row, kind):
        msg = _norm(message)
        mt = _tokens(msg)
        name = _norm(row.get("name"))
        aliases = [_norm(x) for x in row.get("_aliases", []) if x]
        raw = row.get("search_keywords") if kind == "batch" else row.get("keywords")
        keys = []
        if raw:
            try:
                x = json.loads(raw)
                keys = x if isinstance(x, list) else []
            except Exception:
                keys = re.split(r"[,|\n]+", str(raw))
        best = 0.0
        for c in [name] + [_norm(x) for x in keys if x] + aliases:
            if not c: continue
            ct = _tokens(c)
            overlap = len(mt & ct) / max(1, len(ct))
            contains = 1.0 if c in msg or msg in c else 0.0
            ratio = SequenceMatcher(None, msg, c).ratio() if len(c) >= 4 else 0.0

            # Strong evidence only:
            # exact phrase > meaningful token overlap > fuzzy similarity.
            if contains:
                score = 1.0
            elif len(ct) >= 1 and len(mt & ct) >= 1:
                score = overlap * 0.85
                if len(mt & ct) == 1 and len(ct) >= 3:
                    score *= 0.55
            else:
                score = 0.0

            # Fuzzy similarity is only useful for short, name-like queries.
            if len(mt) <= 4 and ratio >= 0.82:
                score = max(score, ratio * 0.75)

            best = max(best, score)
        return best

    async def candidates(self, message, limit=6):
        await self.refresh()
        out = []
        for r in self.batches:
            s = self._score(message, r, "batch")
            if s >= .22: out.append((s,"batch",r))
        for r in self.courses:
            s = self._score(message, r, "course")
            if s >= .22: out.append((s,"course",r))
        out.sort(key=lambda x:x[0], reverse=True)
        return [(k,r,s) for s,k,r in out[:limit]]

    async def compact_context(self, message, limit=6):
        rows = await self.candidates(message, limit)
        if not rows:
            return "NO SPECIFIC BATCH/COURSE CANDIDATE FOUND IN LOCAL CACHE."
        lines = ["RELEVANT LOCAL BUSINESS CANDIDATES:"]
        for kind,r,score in rows:
            aliases = ", ".join(str(x) for x in r.get("_aliases",[])[:12])
            keys = r.get("search_keywords") if kind=="batch" else r.get("keywords")
            lines.append(f"{kind.upper()} | ID={r['id']} | NAME={r.get('name','')} | ALIASES={aliases} | KEYWORDS={keys or ''} | MATCH={score:.2f}")
        return "\n".join(lines)
