#!/usr/bin/env python3
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
# -*- coding: utf-8 -*-
"""brain.py — engine of the orbital second brain. Standard library only.

Layers: L0 core (CLAUDE.md + MAP.md) · L1 domains · L2 concepts · L3 details · cold ring.
Usage:  python _brain/scripts/brain.py <command> -h
Exit:   0 ok · 1 usage or validation error · 2 budget or limit exhausted.
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import json
import math
import os
import pickle
import re
import sys
import time
import unicodedata
# subprocess and urllib.parse are imported where used, and Node and Synapse are plain classes (no dataclasses):
# a recall needs none of them, and each import costs a call several milliseconds on Windows
from pathlib import Path

VERSION = "2.20.1"

# --------------------------------------------------------------------------- rules
SYSTEM_FOLDERS = {"_brain", "_inbox", "_sources", "_log", "_archive", "_templates", "_attachments"}
COLD_FOLDERS = ("_inbox", "_sources", "_log", "_archive")
IGNORED = {".git", ".obsidian", ".trash", ".claude", ".githooks", "node_modules"}
ROOT_FILES = {"CLAUDE.md", "AGENTS.md", "MAP.md"}
# "tools" / "tool": the agent's limbs, one L2 per tool it uses, its L3 lessons are experience with that tool
TYPES_BY_LAYER = {1: {"domain", "tools"}, 2: {"concept", "tool"}, 3: {"fact", "procedure", "example", "error", "decision"}}
DEFAULT_TYPE = {1: "domain", 2: "concept", 3: "fact"}
# provisional: an agent's own learning (brain.py learn), ranked low until use confirms it (2026-10-29)
STATUS_FACTOR = {"provisional": 0.6, "seed": 0.8, "consolidated": 1.0, "obsolete": 0.0}
# a fact known to be wrong now answers less often than a current one; a node another one `supersedes` counts as
# superseded whatever its own tag says. A historical node is still true about its date, and questions about the past
# need it: weighing it down at 0.75 cost five top answers on the 2026-10-30 bench (120 questions, 4 vaults)
TRUTH_FACTOR = {"current": 1.0, "historical": 1.0, "superseded": 0.4}
CONFIRM_SESSIONS, CONFIRM_MIN_DAYS, MAX_PROVISIONAL = 3, 2, 25  # config: confirm_sessions, confirm_min_days, max_provisional
SYNAPSES = {"depends_on": 1, "contradicts": 1, "supersedes": 1, "causes": 2, "example_of": 3, "related": 4}
MAX_SYNAPSES = 7
HEBB_THRESHOLD = 2.9  # ≈3 recent joint uses (decay takes off a few tenths)
MAX_WORDS = {1: 250, 2: 300, 3: 300}
MAX_SUMMARY = 140
MAX_TITLE = 80
MAX_L1_CHILDREN = 12
MAX_DEPTH = 3
MAX_BODY = 2000
# read hands over only the lines and sentences of a body that match the session's last question when the body is
# longer than EXCERPT_MIN characters: a unit stays when it holds at least EXCERPT_KEEP of the best unit's idf; the
# lead sentence always stays, and an excerpt that would keep most of the body gives way to the whole body
EXCERPT_MIN = 600
EXCERPT_KEEP = 0.35
# recall hands over the top body itself when a logistic model gives the top answer a probability of being right of at
# least GATE["threshold"]. Its inputs: the runner-up's score over the top's, the share of the query's idf mass the top
# and the runner-up cover in title, aliases and summary (two query words at least), the number of query terms the
# vault knows (log1p) and whether the query is Spanish. Fitted on the 2026-10-30 bench with engine v2.7.0 (240 queries,
# 4 vaults), threshold set for 95% training precision: out of fold (5-fold CV grouped by question, 3 repeats) it
# fired on 46 of 240 at 92.1% right, where the old rule (cover ≥ 0.7, runner-up below) fired on 21 at 100%.
GATE = {"bias": 0.9537, "w": (-0.2875, 0.8957, -0.0917, 0.0454, 0.1065), "mu": (0.7825, 0.3733, 0.2288, 1.8907, 0.5),
        "sd": (0.1797, 0.2641, 0.2178, 0.2798, 0.5), "threshold": 0.8678}
# a recall or read without --session uses the agent's task session (session new --by) or, with none open, a fallback
# session; after AUTO_IDLE seconds idle (config auto_idle_minutes) the next call opens a new fallback, and a session
# that read bodies but credited none credits the last body it read (the funnel stopped there)
AUTO_IDLE = 1800
FORBIDDEN = set(':?/\\#^[]|*"<>')
CHILD_SECTION = {1: "Branches", 2: "Details"}
TEMPLATES = {1: "L1 Domain.md", 2: "L2 Concept.md", 3: "L3 Detail.md"}
FM_ORDER = ["id", "layer", "type", "summary", "up", "aliases", "status", "source", "external",
            "created", "updated", "reviewed"]
# verify contract: how renewal checks a current note against the live system with code before any model. The intent
# routes it (history never goes stale, a broken rule is drift, behavior needs an experiment); each check is one
# read-only line whose expected value is what the note claims
VERIFY_INTENTS = ("config", "file", "service", "endpoint", "version", "code", "rule", "behavior", "history")
VERIFY_FORMS = (r"(key|nokey)\s+\S+\s+in\s+\S+(\s*=\s*.+)?", r"(path|nopath)\s+\S+", r"grep\s+.+?\s+in\s+\S+",
                r"url\s+http://(localhost|127\.0\.0\.1)\S*\s+\S+\s*=\s*.+", r"version\s+\S+\s+contains\s+.+",
                r"service\s+\S+\s+active")
MAX_CHECKS = 5
# system scope: a note with `system: [windows]` holds only on that system. Recall still finds it and marks it, since
# it says how it is done there; only when this system has its twin (a note of this system linked to it) does it rank
# at OTHER_SYSTEM, so the twin comes first. Hermes Cortex bench 2026-11-01 (twins count as right): every foreign note
# x0.5 → hit@1 27 MRR 0.569; x0.75 → 31 0.630; none → 34 0.656; only twinned x0.5 → 34 0.656 (best MRR)
OTHER_SYSTEM = 0.5
EVENT_WEIGHT = {"activated": 0.2, "read": 1.0, "used": 3.0, "validated": 5.0}
# hot memory (ACT-R base-level activation): B = ln Σ w·age_hours^-HOT_DECAY over the reads, uses and validations of a
# node (being listed by recall is not being needed). build puts the HOT_MAX most active L2/L3 nodes in MAP.md, so an
# agent answers from their summaries with no call; HOT_MIN is one read a week ago (ln 168^-0.5 ≈ -2.56)
HOT_DECAY = 0.5
HOT_MAX = 8
HOT_MIN = -2.6
ATTACHMENT_EXTS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "pdf", "canvas", "base", "mp3", "mp4",
                   "webm", "csv", "xlsx", "docx", "pptx"}

# normal mode lists 900 characters (1,600 until v2.3.0): on the 2026-10-29 bench the cut kept hit@1 and hit@5 and took
# a full answer (recall plus one read) from ~695 to ~480 tokens; context lines (↑ parent, ↳ neighbour) carry titles only
MODES = {
    "quick":  dict(top=5, expand=0, neighbors=0, hops=0, bodies=1, session_bodies=8, queries=8, lines=8, chars=900),
    "normal": dict(top=8, expand=3, neighbors=3, hops=1, bodies=5, session_bodies=20, queries=10, lines=20, chars=900),
    "deep":   dict(top=15, expand=6, neighbors=4, hops=2, bodies=12, session_bodies=40, queries=15, lines=45, chars=4000),
}

# English plus Spanish: queries may still arrive in Spanish (matching is accent-insensitive).
STOPWORDS = set("""a about after all also an and any are as at be been but by can could did do does for from
had has have how if in into is it its no not of on or our so than that the their them then there these they
this those to too was we were what when where which who why will with would you your""".split()) | set(
"""a al algo ante antes como con contra cual cuando de del desde donde e el ella ellas ellos en
entre era es esa ese eso esta este esto estos fue ha hay la las le les lo los mas me mi muy no nos o otra otro
para pero poco por porque que se segun ser si sin sobre su sus tambien te tiene tu un una uno unos y ya""".split())

# Secret triggers stay bilingual (password/contraseña): content may still be written in Spanish.
SECRETS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("API token", re.compile(r"\b(?:sk-ant-|sk-)[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"\b(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]{20,}")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("connection string", re.compile(r"(?i)(?:server|data source)\s*=[^;\n]+;[^\n]*(?:password|pwd)\s*=\s*[^;\s]+")),
    ("password", re.compile(r"(?i)\b(?:password|passwd|pwd|contraseña|contrasena)\s*[:=]\s*(?![<*{\[])(?=[^\s,;]*[\d@$!%?&#])[^\s,;]{4,}")),
]
SENSITIVE_PREFILTER = re.compile(r"(?i)begin|akia|sk-|gh[po]_|github_pat|xox|server|data source|pass|pwd|contrase|token|secret|key|@")
GENERIC_SECRET = re.compile(r"(?i)\b(?:api[_-]?key|apikey|secret|client[_-]?secret|access[_-]?token|auth[_-]?token|token)\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.\/+=]{16,})")
# an address needs a letter TLD that is not .local: root@server.local, root@192.0.2.10 and uv@0.13.0 are not people
EMAIL = re.compile(r"\b[\w.+-]+@(?:[\w-]+\.)+(?!local\b)[A-Za-z]{2,}\b")

# not_english warning: the vault is written in English; these give away Spanish text.
SPANISH_CHARS = re.compile(r"[ñÑ¿¡]")
# Spanish function words a translated query drops (a vault glossary may add more)
SPANISH_STOP = frozenset("""a ahi ahora al algo alguna alguno algunos alli ante antes aqui asi aun cada como con contra cual cuales cuando cuanta
cuantas cuanto cuantos cuentame cuya cuyo dame de debe deben deberia deberian debo del desde dime donde dos e el ella
ellos en entonces entre era eran es esa ese eso esta estaba estan estar este esto estos estoy fue fueron ha habia hace
hacen hacer hago han hasta hay la las le les lo los mas me mi mis mucho muy nada necesito ni no nos nuestro nunca o
otra otras otro otros para pero poco por porque puede pueden puedo que quien quiero se segun ser si siempre sin sobre
solo son soy su sus tambien tan te tener tengo tiene tienen todo todos tu un una uno unos usted varias varios ya yo""".split())
SPANISH_WORDS = frozenset("""el la los las del que para por con una es son y en se su sus como cuando donde pero mas
sin sobre al""".split())

FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
COMMENT_RE = re.compile(r"%%.*?%%", re.S)
LINK_RE = re.compile(r"!?\[\[([^\[\]|#^]+)(?:[#^][^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")
RAW_LINK_RE = re.compile(r"!?\[\[[^\[\]]+\]\]")
SYNAPSE_RE = re.compile(r"^\s*[-*]\s*([A-Za-z_]+)::\s*\[\[([^\[\]|#^]+)(?:[#^][^\[\]|]*)?(?:\|[^\[\]]*)?\]\]\s*(?:(?:—|–|--|-|:)\s*(.*))?$")
URI_RE = re.compile(r"obsidian://open\?vault=([^&\s\"']+)&file=([^\s\"'&\]]+)")
ID_RE = re.compile(r"^\d{8}-\d{6}(?:-[0-9a-f]{2,8})?$")


class BrainError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- utilities
@functools.lru_cache(maxsize=200_000)
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.casefold()).strip()


def word_stem(t: str) -> str:
    # the Snowball English stemmer (snowball.py, vendored): on the 2026-10-30 bench (240 queries, 4 vaults, 5-fold CV
    # grouped by question) it took hit@1 from 147 with the old suffix list to 152 at the same retuned weights
    import snowball
    return snowball.stem_en(t)


def tokens(s: str) -> set:
    return {word_stem(t) for t in re.findall(r"[a-z0-9]+", norm(s)) if t not in STOPWORDS and len(t) > 1}


def count_tokens(text: str) -> int:
    """Estimate modern LLM tokens without a tokenizer: ceil(chars/4) blended 50/50 with ceil(words*1.3)."""
    chars = math.ceil(len(text) / 4)
    words = math.ceil(len(re.findall(r"\w+", text, re.UNICODE)) * 1.3)
    return math.ceil((chars + words) / 2)


def looks_spanish(text: str, ignore=()) -> str | None:
    """Why a text looks Spanish (ñ, ¿, ¡ or ≥2 Spanish-only function words), or None.
    Text inside `ignore` (the node's aliases) is skipped; capitalized words count as proper nouns (Los Angeles)."""
    for a in sorted((x.strip() for x in ignore), key=len, reverse=True):
        if a:
            text = re.sub(re.escape(a), " ", text, flags=re.I)
    m = SPANISH_CHARS.search(text)
    if m:
        return f"contains {m.group(0)}"
    words = sorted({w for w in re.findall(r"[^\W\d_]+", text) if w.islower() and norm(w) in SPANISH_WORDS})
    return f"Spanish words: {', '.join(words)}" if len(words) >= 2 else None


def today() -> str:
    return dt.date.today().isoformat()


def now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def new_id() -> str:
    return now().strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(4).hex()


def parse_date(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def parse_instant(value) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def decay(instant: dt.datetime, half_life: float, reference: dt.datetime | None = None) -> float:
    days = max(0.0, ((reference or dt.datetime.now()) - instant).total_seconds() / 86400)
    return 0.5 ** (days / half_life)


def write_atomic(path: Path, text: str) -> None:
    """A name unique to this write, not one shared by every writer of the path: the lock-free state files (the
    automatic session pointer, telemetry, the cache) are written by any read call, so two agents reading the same
    vault at the same moment would otherwise truncate each other's tmp and the loser's os.replace raised."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.urandom(2).hex()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def link_target(raw: str) -> str | None:
    name = raw.strip().split("/")[-1].strip()
    if name.lower().endswith(".md"):
        return name[:-3]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return None if ext in ATTACHMENT_EXTS else (name or None)


def title_from_link(value) -> str | None:
    if not value:
        return None
    m = LINK_RE.search(str(value))
    return link_target(m.group(1)) if m else str(value).strip() or None


def obsidian_uri(vault: str, note: str) -> str:
    import urllib.parse
    q = urllib.parse.quote
    return f"obsidian://open?vault={q(vault, safe='')}&file={q(note, safe='')}"


def parse_uri(uri: str) -> tuple[str, str] | None:
    m = URI_RE.search(uri or "")
    if not m:
        return None
    import urllib.parse
    return urllib.parse.unquote(m.group(1)), urllib.parse.unquote(m.group(2))


class Lock:
    """Simple inter-process lock (concurrent agent sessions)."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 30:
                        self.path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(0.1)
        raise BrainError("could not acquire the vault lock (another session writing?)")

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- frontmatter
def split_fm(text: str) -> tuple[str | None, str]:
    m = FM_RE.match(text)
    return (m.group(1), text[m.end():]) if m else (None, text)


def _scalar(v: str):
    v = v.strip()
    if not v:
        return None
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return re.sub(r'\\(["\\])', r"\1", v[1:-1])
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if " #" in v:
        v = v.split(" #", 1)[0].rstrip()
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if v in ("true", "false"):
        return v == "true"
    if v in ("null", "~"):
        return None
    return v


def _inline_list(v: str) -> list:
    items, current, quote = [], "", None
    for c in v.strip()[1:-1]:
        if quote:
            current += c
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
            current += c
        elif c == ",":
            items.append(current)
            current = ""
        else:
            current += c
    if current.strip():
        items.append(current)
    return [x for x in (_scalar(i) for i in items) if x is not None]


def parse_fm(raw: str) -> tuple[dict, list]:
    data, errors, key = {}, [], None
    for n, line in enumerate(raw.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*)$", line) or re.match(r"^\s*-$", line)
        if item and key is not None:
            if not isinstance(data.get(key), list):
                data[key] = []
            value = _scalar(item.group(1)) if item.groups() else None
            if value is not None:
                data[key].append(value)
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:(.*)$", line)
        if not m:
            errors.append(f"frontmatter line {n} is invalid")
            continue
        key, value = m.group(1), m.group(2).strip()
        data[key] = _inline_list(value) if value.startswith("[") and value.endswith("]") else _scalar(value)
    return data, errors


def _quote(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = "" if v is None else str(v)
    safe = (s and s == s.strip() and not re.search(r"[:#\[\]{},\"'&*!|>%@`]", s)
            and not s.startswith(("-", "?")) and not re.fullmatch(r"-?\d+|true|false|null|~", s))
    if safe:
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def serialize_fm(data: dict) -> str:
    keys = [k for k in FM_ORDER if k in data] + [k for k in data if k not in FM_ORDER]
    lines = []
    for k in keys:
        v = data[k]
        if isinstance(v, list):
            lines.append(f"{k}: []" if not v else f"{k}:\n" + "\n".join(f"  - {_quote(x)}" for x in v))
        elif v is None:
            lines.append(f"{k}:")
        else:
            lines.append(f"{k}: {_quote(v)}")
    return "---\n" + "\n".join(lines) + "\n---\n"


# --------------------------------------------------------------------------- nodes
class Synapse:
    __slots__ = ("type", "target", "reason", "line")

    def __init__(self, type: str, target: str, reason: str, line: int):
        self.type, self.target, self.reason, self.line = type, target, reason, line

    def __getstate__(self):
        return (self.type, self.target, self.reason, self.line)

    def __setstate__(self, state):
        self.type, self.target, self.reason, self.line = state


class Node:
    def __init__(self, path: Path, title: str, fm: dict, body: str, fm_errors: list, synapses: list | None = None,
                 malformed: list | None = None, links: set | None = None, words: int = 0, has_fm: bool = True,
                 cold: bool = False):
        self.path, self.title, self.fm, self.body, self.fm_errors = path, title, fm, body, fm_errors
        self.synapses = [] if synapses is None else synapses
        self.malformed = [] if malformed is None else malformed
        self.links = set() if links is None else links
        self.words, self.has_fm, self.cold = words, has_fm, cold

    @property
    def layer(self):
        c = self.fm.get("layer")
        return c if isinstance(c, int) else None

    @property
    def type(self):
        return self.fm.get("type")

    @property
    def summary(self) -> str:
        return str(self.fm.get("summary") or "")

    @property
    def status(self) -> str:
        return str(self.fm.get("status") or "seed")

    @property
    def up(self):
        raw = self.fm.get("up")
        if self.__dict__.get("_up_raw", ...) != raw:
            self._up_raw, self._up = raw, title_from_link(raw)
        return self._up

    @property
    def aliases(self) -> list:
        a = self.fm.get("aliases")
        return [str(x) for x in a] if isinstance(a, list) else ([str(a)] if a else [])

    @property
    def external(self) -> list:
        e = self.fm.get("external")
        return [str(x) for x in e] if isinstance(e, list) else ([str(e)] if e else [])

    @property
    def key(self) -> str:
        i = self.fm.get("id")
        return str(i) if i else "t:" + norm(self.title)

    @property
    def k(self) -> str:
        return norm(self.title)

    def text(self) -> str:
        return serialize_fm(self.fm) + self.body


def parse_body(body: str):
    synapses, malformed, links, content = [], [], set(), []
    section = None
    for i, line in enumerate(COMMENT_RE.sub("", body).split("\n"), 1):
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            section = norm(m.group(1))
            continue
        for lm in LINK_RE.finditer(line):
            t = link_target(lm.group(1))
            if t:
                links.add(t)
        if section == "connections":
            if re.match(r"^\s*[-*]\s+", line) and "[[" in line:
                sm = SYNAPSE_RE.match(line)
                if sm and link_target(sm.group(2)):
                    synapses.append(Synapse(sm.group(1), link_target(sm.group(2)), (sm.group(3) or "").strip(), i))
                else:
                    malformed.append(i)
        elif section not in LINK_SECTIONS and not line.lstrip().startswith("#"):
            content.append(line)
    # words count the node's own text: child lists and links are an index, not content
    return synapses, malformed, links, len(re.findall(r"\w+", "\n".join(content)))


def over_limit(n) -> bool:
    """Episodes are evidence and are not atomized: the limit applies to knowledge (2026-10-28)."""
    if n.layer == 3 and n.type == "example" and n.fm.get("truth") == "historical":
        return False
    return n.words > MAX_WORDS[n.layer]


STATE_KEY = None


def state_key() -> tuple:
    """What a persisted state depends on besides the vault: the engine file and the Python version."""
    global STATE_KEY
    if STATE_KEY is None:
        st = os.stat(__file__)
        STATE_KEY = (VERSION, st.st_mtime_ns, st.st_size, sys.version_info[:2])
    return STATE_KEY


def load_state(path: Path) -> dict:
    """A pickled state written by this engine for this vault (state/ is local and gitignored); {} if absent or stale."""
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
        return data["items"] if data.get("key") == state_key() else {}
    except Exception:  # noqa: BLE001 — a damaged or foreign cache is rebuilt, never trusted
        return {}


def save_state(path: Path, items: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{os.urandom(2).hex()}.tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"key": state_key(), "items": items}, f, pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except OSError:
        pass


def read_node(root: Path, rel: Path, cold: bool = False, text: str | None = None) -> Node:
    if text is None:
        text = (root / rel).read_text(encoding="utf-8")
    raw, body = split_fm(text)
    fm, errors = parse_fm(raw) if raw is not None else ({}, [])
    s, bad, lnk, words = parse_body(body)
    n = Node(rel, rel.stem, fm, body, errors, s, bad, lnk, words, raw is not None, cold)
    n.tokens = count_tokens(text)
    return n


def save_node(root: Path, n: Node, touch: bool = True) -> None:
    if touch:
        n.fm["updated"] = today()
    write_atomic(root / n.path, n.text())


# --------------------------------------------------------------------------- vault
class Vault:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise BrainError(f"vault does not exist: {self.root}")
        self.cfg_path = self.root / "_brain" / "config.json"
        self.cfg = {"vault": self.root.name, "siblings": {}, "warn_dependency_on": [], "half_life_days": 30}
        if self.cfg_path.exists():
            self.cfg.update(json.loads(self.cfg_path.read_text(encoding="utf-8")))
        self.name = self.cfg["vault"]
        self.state_dir = self.root / "_brain" / "state"
        self.gen_dir = self.root / "_brain" / "generated"
        self.net: dict[str, Node] = {}
        self.archive: dict[str, Node] = {}
        self.titles: dict[str, list[Path]] = {}
        self.cold: dict[str, list[Path]] = {c: [] for c in COLD_FOLDERS}
        self.loose: list[Path] = []
        self._load()

    # -- loading and indexes
    def _scan(self) -> list[tuple[str, int, int]]:
        """Walks the vault with os.scandir: on Windows it brings mtime and size without opening files.
        Paths stay strings ("Folder/Note.md"): building a Path per file cost a warm recall ~40 ms."""
        found, stack, base = [], [""], str(self.root)
        while stack:
            folder = stack.pop()
            try:
                entries = os.scandir(os.path.join(base, folder) if folder else base)
            except OSError:
                continue
            with entries:
                for e in entries:
                    if e.is_dir(follow_symlinks=False):
                        if not e.name.startswith(".") and e.name not in IGNORED:
                            stack.append(f"{folder}/{e.name}" if folder else e.name)
                    elif e.name.endswith(".md"):
                        st = e.stat()
                        found.append((f"{folder}/{e.name}" if folder else e.name, st.st_mtime_ns, st.st_size))
        return sorted(found)

    def md_files(self, include_brain: bool = False):
        for rel, _, _ in self._scan():
            if include_brain or not rel.startswith("_brain/"):
                yield Path(rel)

    @property
    def _cache(self) -> dict:
        """The text cache (state/cache.json), loaded only when a file has to be read: a warm recall reads none."""
        if self._text_cache is None:
            try:
                path = self.state_dir / "cache.json"
                self._text_cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, json.JSONDecodeError):
                self._text_cache = {}
        return self._text_cache

    def text(self, rel: Path) -> str:
        """Text of a .md: from the cache if unchanged (each open on Windows costs ~5 ms because of the antivirus)."""
        key = rel if isinstance(rel, str) else rel.as_posix()
        if key in self._texts:
            return self._texts[key]
        meta = self._meta.get(key)
        prev = self._cache.get(key)
        if meta and prev and prev[0] == meta[0] and prev[1] == meta[1]:
            text = prev[2]
        else:
            text = (self.root / rel).read_text(encoding="utf-8")
            self._cache_dirty = True
        self._texts[key] = text
        if meta:
            self._cache[key] = [meta[0], meta[1], text]
        return text

    LOADED = ("net", "archive", "titles", "cold", "loose", "children", "incoming", "mentions")

    def _load(self) -> None:
        self._text_cache = None
        self._texts: dict[str, str] = {}
        self._cache_dirty = False
        scan = self._scan()
        self._meta = {rel: (mt, sz) for rel, mt, sz in scan}
        self.struct_key = (len(scan), max((mt for _, mt, _ in scan), default=0))
        # the loaded vault persists between calls in state/snapshot.pickle: when no file changed (same names, mtimes
        # and sizes) one unpickle restores it whole; otherwise only the changed files are read and parsed again
        snap = load_state(self.state_dir / "snapshot.pickle")
        if snap.get("meta") == self._meta:
            try:
                for name, value in zip(self.LOADED, pickle.loads(snap["full"])):
                    setattr(self, name, value)
                return
            except Exception:  # noqa: BLE001 — a damaged snapshot is rebuilt below
                self.net, self.archive, self.titles, self.loose = {}, {}, {}, []
                self.cold = {c: [] for c in COLD_FOLDERS}
        files, fresh = load_state(self.state_dir / "nodes.pickle"), {}
        for key, mt, sz in scan:
            top = key.split("/", 1)[0] if "/" in key else ""
            if top in ("_templates", "_brain"):
                continue
            rel = Path(key)
            self.titles.setdefault(norm(key.rsplit("/", 1)[-1][:-3]), []).append(rel)
            if top in COLD_FOLDERS:
                self.cold[top].append(rel)
                if top == "_archive":
                    self.archive.setdefault(norm(rel.stem), read_node(self.root, rel, cold=True, text=self.text(rel)))
                continue
            if top in SYSTEM_FOLDERS:
                continue
            if not top:
                if key not in ROOT_FILES:
                    self.loose.append(rel)
                continue
            prev = files.get(key)
            if prev and prev[0] == mt and prev[1] == sz:
                fresh[key] = prev
                n = pickle.loads(prev[2])
            else:
                n = read_node(self.root, rel, text=self.text(rel))
                fresh[key] = (mt, sz, pickle.dumps(n, pickle.HIGHEST_PROTOCOL))
            n.rel = key
            self.net.setdefault(n.k, n)
        current = set(self._meta)
        if self._text_cache is not None and (self._cache_dirty or set(self._cache) - current):
            self._text_cache = {k: x for k, x in self._cache.items() if k in current}
            if (self.root / "_brain").is_dir():
                try:
                    write_atomic(self.state_dir / "cache.json", json.dumps(self._cache, ensure_ascii=False))
                except OSError:
                    pass
        self.children: dict[str, list[Node]] = {}
        self.incoming: dict[str, list[tuple[str, Node]]] = {}
        self.mentions: dict[str, set] = {}
        for k, n in self.net.items():
            if n.up:
                self.children.setdefault(norm(n.up), []).append(n)
            for s in n.synapses:
                self.incoming.setdefault(norm(s.target), []).append((s.type, n))
            for t in n.links:
                if norm(t) != k:
                    self.mentions.setdefault(norm(t), set()).add(k)
        for group in self.children.values():
            group.sort(key=lambda x: x.k)
        if (self.root / "_brain").is_dir():
            full = pickle.dumps(tuple(getattr(self, name) for name in self.LOADED), pickle.HIGHEST_PROTOCOL)
            save_state(self.state_dir / "snapshot.pickle", {"meta": self._meta, "full": full})
            save_state(self.state_dir / "nodes.pickle", fresh)  # per file, read only when something changed

    def exists(self, title: str) -> bool:
        return norm(title) in self.titles or norm(title) == "map"

    def node(self, title: str, archive: bool = False) -> Node:
        k = norm(title)
        n = self.net.get(k) or (self.archive.get(k) if archive else None)
        if n:
            return n
        for m in self.net.values():
            if k in (norm(a) for a in m.aliases):
                return m
        import difflib
        close = difflib.get_close_matches(k, list(self.net), n=3, cutoff=0.6)
        hint = "; did you mean: " + ", ".join(self.net[c].title for c in close) + "?" if close else ""
        raise BrainError(f'no node named "{title}"{hint}')

    def by_key(self) -> dict[str, Node]:
        return {n.key: n for n in self.net.values()}

    def parent(self, n: Node) -> Node | None:
        return self.net.get(norm(n.up)) if n.up else None

    def chain(self, n: Node) -> list[Node]:
        out, seen, p = [], {n.k}, self.parent(n)
        while p and p.k not in seen:
            out.append(p)
            seen.add(p.k)
            p = self.parent(p)
        return out

    def neighbors(self, n: Node) -> list[tuple[str, Node, str]]:
        res = [(s.type, self.net[norm(s.target)], "→") for s in n.synapses if norm(s.target) in self.net]
        res += [(t, m, "←") for t, m in self.incoming.get(n.k, [])]
        unique, seen = [], set()
        for t, m, d in res:
            if m.k not in seen and m.k != n.k:
                seen.add(m.k)
                unique.append((t, m, d))
        return unique

    def sibling(self, name: str) -> "Vault":
        rel = self.cfg.get("siblings", {}).get(name)
        if rel is None:
            raise BrainError(f'"{name}" is not in siblings of _brain/config.json')
        path = (self.root / rel).resolve()
        if not path.is_dir():
            raise BrainError(f'sibling vault "{name}" does not exist at {path}')
        return Vault(path)

    def save_cfg(self) -> None:
        write_atomic(self.cfg_path, json.dumps(self.cfg, ensure_ascii=False, indent=2) + "\n")

    def lock(self) -> Lock:
        return Lock(self.state_dir / ".lock")


# --------------------------------------------------------------------------- telemetry
class Telemetry:
    def __init__(self, v: Vault):
        self.v = v
        self.path = v.state_dir / "usage.jsonl"
        self._events = None

    def record(self, event: str, keys, session: str | None = None, group: str | None = None,
               query: str | None = None, extra: dict | None = None) -> None:
        t = now().isoformat()
        lines = []
        for k in keys:
            e = {"t": t, "s": session, "e": event, "k": k}
            if group:
                e["g"] = group
            if extra:
                e.update(extra)
            lines.append(json.dumps(e, ensure_ascii=False))
        if not lines and query is None:
            return
        with self.v.lock():
            if lines:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                    f.write("\n".join(lines) + "\n")
            log_activity(self.v, event, keys, session, query)

    def events(self) -> list[dict]:
        if self._events is None:
            self._events = []
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        e = json.loads(line)
                        e["_t"] = parse_instant(e.get("t"))
                        if e["_t"] and e.get("k"):
                            self._events.append(e)
                    except json.JSONDecodeError:
                        continue
        return self._events

    def _accumulated(self) -> dict:
        """Running sums of the log, kept in state/telemetry.pickle and advanced over the lines appended since the last
        call: a recall appends to the log, so rebuilding from it on every call cost a warm recall ~30 ms.
        Each event adds w * 2^((t - t0) / half_life); today's value is that sum times 0.5^((now - t0) / half_life),
        exactly the decayed sum of the log."""
        if hasattr(self, "_acc"):
            return self._acc
        hl = float(self.v.cfg.get("half_life_days", 30)) * 86400
        acc = load_state(self.v.state_dir / "telemetry.pickle")
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        if acc.get("hl") != hl or acc.get("off", 0) > size or time.time() - acc.get("t0", 0) > 365 * 86400:
            acc = {"hl": hl, "t0": time.time(), "off": 0, "w": {}, "co": {}}
        if size > acc["off"]:
            with open(self.path, "rb") as f:
                f.seek(acc["off"])
                chunk = f.read()
            cut = chunk.rfind(b"\n") + 1  # a line still being written waits for the next call
            groups: dict[str, list] = {}
            for line in chunk[:cut].decode("utf-8", "replace").splitlines():
                try:
                    e = json.loads(line)
                    t = parse_instant(e.get("t"))
                except json.JSONDecodeError:
                    continue
                if not t or not e.get("k"):
                    continue
                f2 = 2 ** ((t.timestamp() - acc["t0"]) / hl)
                w = EVENT_WEIGHT.get(e.get("e"))
                if w:
                    acc["w"][e["k"]] = acc["w"].get(e["k"], 0.0) + w * f2
                if e.get("e") == "used" and e.get("g"):
                    groups.setdefault(e["g"], [f2, set()])[1].add(e["k"])
            for f2, keys in groups.values():
                ks = sorted(keys)
                for i in range(len(ks)):
                    for j in range(i + 1, len(ks)):
                        acc["co"][(ks[i], ks[j])] = acc["co"].get((ks[i], ks[j]), 0.0) + f2
            acc["off"] += cut
            if cut and (self.v.root / "_brain").is_dir():
                save_state(self.v.state_dir / "telemetry.pickle", acc)
        self._acc = acc
        return acc

    def weights(self) -> dict[str, float]:
        acc = self._accumulated()
        f = 0.5 ** ((time.time() - acc["t0"]) / acc["hl"])
        res = {n.key: acc["w"].get(n.key, 0.0) * f + 0.5 * len(self.v.incoming.get(k, [])) for k, n in self.v.net.items()}
        mx = max(res.values(), default=0.0)
        return {k: (b / mx if mx > 0 else 0.0) for k, b in res.items()}

    def coactivation(self) -> dict[tuple[str, str], float]:
        acc = self._accumulated()
        f = 0.5 ** ((time.time() - acc["t0"]) / acc["hl"])
        return {k: x * f for k, x in acc["co"].items()}

    def weights_from_log(self) -> dict[str, float]:
        """The weights rebuilt from the whole log: the reference for the incremental weights()."""
        hl = float(self.v.cfg.get("half_life_days", 30))
        raw: dict[str, float] = {}
        for e in self.events():
            w = EVENT_WEIGHT.get(e.get("e"))
            if w:
                raw[e["k"]] = raw.get(e["k"], 0.0) + w * decay(e["_t"], hl)
        res = {n.key: raw.get(n.key, 0.0) + 0.5 * len(self.v.incoming.get(k, [])) for k, n in self.v.net.items()}
        mx = max(res.values(), default=0.0)
        return {k: (b / mx if mx > 0 else 0.0) for k, b in res.items()}

    def coactivation_from_log(self) -> dict[tuple[str, str], float]:
        hl = float(self.v.cfg.get("half_life_days", 30))
        groups: dict[str, tuple[dt.datetime, set]] = {}
        for e in self.events():
            if e.get("e") == "used" and e.get("g"):
                groups.setdefault(e["g"], (e["_t"], set()))[1].add(e["k"])
        co: dict[tuple[str, str], float] = {}
        for t, keys in groups.values():
            f, ks = decay(t, hl), sorted(keys)
            for i in range(len(ks)):
                for j in range(i + 1, len(ks)):
                    co[(ks[i], ks[j])] = co.get((ks[i], ks[j]), 0.0) + f
        return co

    def activation(self, at: float | None = None) -> dict[str, float]:
        """ACT-R base-level activation of every node used at least once: ln of the power-law decayed uses."""
        at = at or time.time()
        acc: dict[str, float] = {}
        for e in self.events():
            w = EVENT_WEIGHT.get(e.get("e")) if e.get("e") != "activated" else None
            age = (at - e["_t"].timestamp()) / 3600
            if w and age >= 0:
                acc[e["k"]] = acc.get(e["k"], 0.0) + w * max(age, 1.0) ** -HOT_DECAY
        return {k: math.log(x) for k, x in acc.items()}

    def planted(self) -> dict[str, dict]:
        """Nodes an agent planted and that still hold the status they were born with: key → the planting event.
        The panel draws them in the signal's colour until their status changes (seed, consolidated, obsolete)."""
        born = {e["k"]: e for e in self.events() if e.get("e") == "planted"}
        net = self.v.by_key()
        return {k: e for k, e in born.items() if k in net and net[k].status == e.get("st")}

    def count(self, event: str) -> dict[str, int]:
        c: dict[str, int] = {}
        for e in self.events():
            if e.get("e") == event:
                c[e["k"]] = c.get(e["k"], 0) + 1
        return c


ACTIVITY_MAX = 400
PHASE_TITLES = 60   # titles one life-cycle event carries to the panel
ACTIVITY_PREFIX = "window.BRAIN_ACTIVITY = "


def log_activity(v: Vault, event: str, keys, session: str | None, query: str | None = None, phase: dict | None = None) -> None:
    """Ephemeral feed for the live query signals of Panel.html: _brain/state/activity.js (gitignored).
    A script file (not JSON) because a page opened from disk can load scripts but not fetch files.
    A phase of the life cycle (`brain.py activity`) is one event that carries its own fields and the titles it touched."""
    path = v.state_dir / "activity.js"
    try:
        events = json.loads(path.read_text(encoding="utf-8")[len(ACTIVITY_PREFIX):].rstrip().rstrip(";"))["events"]
    except (OSError, ValueError, KeyError, TypeError):
        events = []
    seq = events[-1].get("i", 0) if events else 0
    t = now().isoformat(timespec="seconds")
    new = []
    if query is not None:
        new.append({"t": t, "s": session, "e": "recall", "q": query[:160], "n": len(keys)})
    by_key = v.by_key()
    if phase is not None:
        titles = [by_key[k].title for k in keys if k in by_key]
        new.append({"t": t, "s": session, "e": event, **phase, **({"titles": titles[:PHASE_TITLES], "n": len(titles)} if titles else {})})
    else:
        for k in keys:
            n = by_key.get(k)
            if n:
                new.append({"t": t, "s": session, "e": event, "title": n.title, "layer": n.layer})
    for e in new:
        seq += 1
        e["i"] = seq
    data = {"vault": v.name, "updated": t, "events": (events + new)[-ACTIVITY_MAX:]}
    write_atomic(path, ACTIVITY_PREFIX + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n")


def pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


# --------------------------------------------------------------------------- sessions
class Sessions:
    def __init__(self, v: Vault):
        self.v = v
        self.dir = v.state_dir / "sessions"

    def new(self, mode: str) -> str:
        sid = "s" + now().strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
        self.save(sid, {"mode": mode, "created": now().isoformat(), "queries": 0,
                        "query_bodies": 0, "total_bodies": 0, "visited": [], "credited": False})
        return sid

    def idle_seconds(self) -> float:
        """How long the fallback session waits for a call before the next one opens a new one (config auto_idle_minutes)."""
        try:
            return max(1.0, float(self.v.cfg.get("auto_idle_minutes", AUTO_IDLE / 60)) * 60)
        except (TypeError, ValueError):
            return float(AUTO_IDLE)

    def task_pointer(self, by: str | None) -> Path | None:
        return self.dir / f"task-{agent_slug(by)}.json" if by else None

    def open_task(self, by: str | None) -> str | None:
        """The session this agent opened for its task (session new --by) and has not closed, if any."""
        ptr = self.task_pointer(by)
        try:
            sid = json.loads(ptr.read_text(encoding="utf-8"))["sid"]
            return sid if not self.load(sid).get("closed") else None
        except (AttributeError, OSError, ValueError, KeyError, BrainError):
            return None

    def open(self, mode: str, by: str | None) -> tuple[str, list[str]]:
        """A task's session: closes this agent's previous one, if still open, and points the agent at the new one."""
        notes = self.close(None, by) if by and self.open_task(by) else []
        sid = self.new(mode)
        if by:
            data = self.load(sid)
            data["by"] = by
            self.save(sid, data)
            write_atomic(self.task_pointer(by), json.dumps({"sid": sid}))
        return sid, notes

    def close(self, sid: str | None, by: str | None) -> list[str]:
        """Ends a session (sid, else this agent's task session): the automatic credit when nothing was credited."""
        sid = sid or self.open_task(by)
        if not sid:
            return []
        data = self.load(sid)
        if data.get("closed"):
            return []
        notes = auto_credit(self.v, sid, data)
        data["closed"] = True
        self.save(sid, data)
        ptr = self.task_pointer(by or data.get("by"))
        try:
            if ptr and json.loads(ptr.read_text(encoding="utf-8")).get("sid") == sid:
                ptr.unlink()
        except (OSError, ValueError):
            pass
        return notes

    def auto(self, mode: str | None = None, touch: bool = True, by: str | None = None) -> tuple[str, list[str]]:
        """The session a call without --session uses. With --by: the agent's task session while it is open (no idle,
        it ends with session close or the agent's next session new); otherwise a fallback session of its own, one per
        agent (auto.json with no --by). The fallback is renewed after auto_idle_minutes with no call, or when a call
        needs a deeper mode; a spent one is not renewed: recall then says to answer (exit 2), as an explicit session
        does. Returns its id and the notes to print (an automatic credit of the previous one)."""
        task = self.open_task(by)
        if task:
            return task, []
        ptr, notes = (self.dir / f"auto-{agent_slug(by)}.json" if by else self.dir / "auto.json"), []
        try:
            cur = json.loads(ptr.read_text(encoding="utf-8"))
            data = self.load(cur["sid"])
        except (OSError, ValueError, KeyError, BrainError):
            cur, data = None, None
        idle = cur is None or time.time() - cur.get("touched", 0) > self.idle_seconds()
        deeper = data is not None and mode and MODES[mode]["bodies"] > MODES[data["mode"]]["bodies"]
        if cur and data and (idle or deeper):
            notes += auto_credit(self.v, cur["sid"], data)
        if not cur or not data or idle or deeper:
            cur = {"sid": self.new(mode or "normal")}
        if touch:
            cur["touched"] = time.time()
            write_atomic(ptr, json.dumps(cur))
        return cur["sid"], notes

    def load(self, sid: str) -> dict:
        path = self.dir / f"{sid}.json"
        if not re.fullmatch(r"s[\w-]+", sid or "") or not path.exists():
            raise BrainError(f'unknown session "{sid}": create one with `brain.py session new`')
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, sid: str, data: dict) -> None:
        write_atomic(self.dir / f"{sid}.json", json.dumps(data, ensure_ascii=False))

    def clean(self, days: int = 3) -> None:
        if not self.dir.exists():
            return
        limit = time.time() - days * 86400
        for p in self.dir.glob("*.json"):
            if p.stat().st_mtime < limit:
                p.unlink()


def agent_slug(by: str | None) -> str:
    """An agent's name as a file name part: claude-code, codex, my-agent."""
    return re.sub(r"[^a-z0-9]+", "-", (by or "").lower()).strip("-")[:40] or "agent"


def auto_credit(v: "Vault", sid: str, data: dict) -> list[str]:
    """A closed automatic session that read bodies and credited none: the last body read is where the funnel
    stopped, so it gets the credit the agent did not give."""
    if data.get("credited") or not data.get("visited"):
        return []
    key = data["visited"][-1]
    n = v.by_key().get(key)
    if not n:
        return []
    Telemetry(v).record("used", [key], sid, group=os.urandom(4).hex())
    return [f'# previous session credited automatically: "{n.title}" (the last body it read)']


# --------------------------------------------------------------------------- validation
def validate_title(v: Vault, title: str, except_node: Node | None = None) -> None:
    if not title or title != title.strip():
        raise BrainError("empty title or title with leading/trailing spaces")
    bad = sorted(set(title) & FORBIDDEN)
    if bad:
        raise BrainError(f"the title contains forbidden characters: {' '.join(bad)}")
    if len(title) > MAX_TITLE:
        raise BrainError(f"title of {len(title)} characters (max {MAX_TITLE})")
    if norm(title) in ("map", "claude", "panel", "proposals"):
        raise BrainError("title reserved by the system")
    if norm(title) in v.titles and not (except_node and norm(title) == except_node.k):
        raise BrainError(f'a note named "{title}" already exists (titles are unique ignoring accents and case)')


def validate_summary(summary: str) -> None:
    if not summary or not summary.strip():
        raise BrainError("summary is required")
    if len(summary) > MAX_SUMMARY:
        raise BrainError(f"summary of {len(summary)} characters (max {MAX_SUMMARY})")
    if "[[" in summary:
        raise BrainError("the summary must not contain links")


def expected_path(layer: int, title: str, parent: Node | None) -> Path:
    if layer == 1:
        base = Path() if parent is None else parent.path.parent
        while len(base.parts) > 1:  # a subdomain of a subdomain sits beside its parent: its details must still fit MAX_DEPTH
            base = base.parent
        path = base / title / f"{title}.md"
    elif layer == 2:
        path = parent.path.parent / f"{title}.md"
    else:
        path = parent.path.parent / parent.title / f"{title}.md"
    if len(path.parent.parts) > MAX_DEPTH:
        raise BrainError(f"would exceed the maximum depth of {MAX_DEPTH} folders: {path.as_posix()}")
    return path


def has_cycle(v: Vault, n: Node) -> bool:
    seen, p = {n.k}, v.parent(n)
    while p:
        if p.k in seen:
            return p.k == n.k
        seen.add(p.k)
        p = v.parent(p)
    return False


def validate_parent(layer: int, parent: Node | None) -> None:
    if parent is None:
        if layer != 1:
            raise BrainError("only L1 nodes hang from MAP")
        return
    if layer == 1 and parent.layer != 1:
        raise BrainError("an L1 can only hang from MAP or from another L1 (subdomain)")
    if layer == 2 and parent.layer != 1:
        raise BrainError("an L2 must hang from an L1")
    if layer == 3 and parent.layer != 2:
        raise BrainError("an L3 must hang from an L2")


def contract_errors(intent, keywords, checks) -> list[str]:
    """What is wrong with a verify contract; empty when it is well formed (or absent)."""
    if intent in (None, "") and not keywords and not checks:
        return []
    errs = []
    if intent not in VERIFY_INTENTS:
        errs.append(f'verify_intent "{intent or "-"}" is not one of {", ".join(VERIFY_INTENTS)}')
    if keywords is not None and not isinstance(keywords, list):
        errs.append("verify_keywords must be a list")
    checks = checks if isinstance(checks, list) else [] if checks in (None, "") else [checks]
    if len(checks) > MAX_CHECKS:
        errs.append(f"{len(checks)} verify checks (max {MAX_CHECKS})")
    for c in checks:
        if not any(re.fullmatch(f, str(c).strip()) for f in VERIFY_FORMS):
            errs.append(f"verify check in no known form: {str(c)[:60]}")
    return errs


def audit(v: Vault, warnings: bool = True) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []

    def E(rel, msg):
        out.append(("ERROR", rel.as_posix() if isinstance(rel, Path) else str(rel), msg))

    def W(rel, msg):
        if warnings:
            out.append(("WARN", rel.as_posix() if isinstance(rel, Path) else str(rel), msg))

    for rel in v.loose:
        E(rel, "note outside a domain folder (the root only allows CLAUDE.md and MAP.md)")
    for paths in v.titles.values():
        if len(paths) > 1:
            E(paths[0], "duplicate title: " + ", ".join(r.as_posix() for r in paths))

    ids: dict[str, Path] = {}
    for k, n in v.net.items():
        r = n.path
        if not n.has_fm:
            E(r, "no frontmatter")
            continue
        for e in n.fm_errors:
            E(r, e)
        bad = sorted(set(n.title) & FORBIDDEN)
        if bad:
            E(r, f"title with forbidden characters: {' '.join(bad)}")
        if len(n.title) > MAX_TITLE:
            E(r, f"title of {len(n.title)} characters (max {MAX_TITLE})")
        i = n.fm.get("id")
        if not i:
            W(r, "no id (run: brain.py lint --fix)")
        elif not ID_RE.match(str(i)):
            E(r, f"invalid id format: {i}")
        elif str(i) in ids:
            E(r, f"duplicate id with {ids[str(i)].as_posix()}")
        else:
            ids[str(i)] = r
        if n.layer not in (1, 2, 3):
            E(r, "layer must be 1, 2 or 3")
            continue
        for e in contract_errors(n.fm.get("verify_intent"), n.fm.get("verify_keywords"), n.fm.get("verify")):
            E(r, e)
        for x in node_systems(n):
            if not re.fullmatch(r"[a-z][a-z0-9-]*", x):
                E(r, f'system "{x}" must be one lowercase word, such as ubuntu or windows')
        if n.type not in TYPES_BY_LAYER[n.layer]:
            E(r, f'type "{n.type}" not valid for L{n.layer}: {", ".join(sorted(TYPES_BY_LAYER[n.layer]))}')
        if n.type == "tool" and n.up and norm(n.up) in v.net and v.net[norm(n.up)].type != "tools":
            W(r, "a tool concept belongs under the Tools domain (type: tools)")
        if n.fm.get("status") not in STATUS_FACTOR:
            E(r, "status must be " + ", ".join(STATUS_FACTOR))
        s = n.summary
        if not s.strip():
            E(r, "missing summary")
        elif len(s) > MAX_SUMMARY:
            E(r, f"summary of {len(s)} characters (max {MAX_SUMMARY})")
        elif "[[" in s:
            E(r, "the summary must not contain links")
        spanish = looks_spanish(f"{n.title}\n{s}", n.aliases)
        if spanish:
            W(r, f"not_english: title or summary looks Spanish ({spanish}); the vault is written in English "
                 "(proper nouns can go in aliases)")
        if not n.fm.get("created"):
            W(r, "no created date")
        # hierarchy
        if not n.up:
            E(r, "missing up")
        elif norm(n.up) == "map":
            if n.layer != 1:
                E(r, "only L1 nodes hang from MAP")
            elif n.path != Path(n.title) / f"{n.title}.md":
                W(r, f"expected location: {n.title}/{n.title}.md (use brain.py move)")
        else:
            p = v.net.get(norm(n.up))
            if not p:
                E(r, f"up points to a missing node: {n.up}")
            else:
                try:
                    validate_parent(n.layer, p)
                    expected = expected_path(n.layer, n.title, p)
                    if expected != n.path:
                        W(r, f"expected location: {expected.as_posix()} (use brain.py move)")
                except BrainError as ex:
                    E(r, str(ex))
                if k not in {norm(t) for t in p.links}:
                    W(r, f'parent "{p.title}" does not link to this node in ## {CHILD_SECTION.get(p.layer, "Branches")}')
                if has_cycle(v, n):
                    E(r, "cycle in the up chain")
        # synapses
        lateral = [x for x in n.synapses]
        if len(lateral) > MAX_SYNAPSES:
            E(r, f"{len(lateral)} synapses (max {MAX_SYNAPSES}): prune the weakest")
        seen = set()
        for x in lateral:
            if x.type not in SYNAPSES:
                E(r, f'line {x.line}: unknown synapse type "{x.type}" ({", ".join(SYNAPSES)})')
            if not x.reason:
                E(r, f'line {x.line}: synapse to "{x.target}" without a reason')
            if norm(x.target) == k:
                E(r, f"line {x.line}: synapse to itself")
            elif norm(x.target) in seen:
                E(r, f'line {x.line}: duplicate synapse to "{x.target}"')
            elif norm(x.target) in v.archive:
                W(r, f"line {x.line}: synapse to an archived node: {x.target}")
            elif not v.exists(x.target):
                E(r, f'line {x.line}: broken synapse to "{x.target}"')
            seen.add(norm(x.target))
        for line in n.malformed:
            E(r, f"line {line} of Connections is malformed: use `- type:: [[Target]] — reason`")
        for t in sorted(n.links):
            if not v.exists(t) and norm(t) not in seen:
                W(r, f"link without target: [[{t}]]")
        if "obsidian://" in n.body:
            E(r, "obsidian:// URIs only go in the external property")
        for uri in n.external:
            ref = parse_uri(uri)
            if not ref:
                E(r, f"invalid external reference (use brain.py external add): {uri}")
            elif ref[0] in v.cfg.get("warn_dependency_on", []):
                W(r, f'external dependency on "{ref[0]}": keep it minimal')
        if over_limit(n):
            W(r, f"{n.words} words (max {MAX_WORDS[n.layer]}): atomize")
        if n.layer == 1:
            l2 = [h for h in v.children.get(k, []) if h.layer == 2]
            if len(l2) > MAX_L1_CHILDREN:
                W(r, f"{len(l2)} concepts (max {MAX_L1_CHILDREN}): split into subdomains")
        if k not in v.mentions and n.layer != 1:
            W(r, "orphan: no node links to it")

    # secrets and personal data across all written content
    # config "allowed_addresses": {address: why it is kept}, for senders and contacts kept on purpose
    allowed = {a.lower() for a in v.cfg.get("allowed_addresses", {})}
    for paths in v.titles.values():
        for rel in paths:
            for num, line in enumerate(v.text(rel).splitlines(), 1):
                if not SENSITIVE_PREFILTER.search(line):
                    continue
                for name, pattern in SECRETS:
                    if pattern.search(line):
                        E(rel, f"line {num}: possible secret ({name}); never store credentials")
                m = GENERIC_SECRET.search(line)
                if m and re.search(r"\d", m.group(1)) and re.search(r"[A-Za-z]", m.group(1)):
                    E(rel, f"line {num}: possible secret (token or key); never store credentials")
                if "obsidian://" not in line and any(m.group(0).lower() not in allowed for m in EMAIL.finditer(line)):
                    W(rel, f"line {num}: possible personal data (email); store roles, not people")
    return out


# --------------------------------------------------------------------------- body editing
def _section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    start = next((i for i, l in enumerate(lines) if re.match(rf"^##\s+{re.escape(section)}\s*$", l.strip(), re.I)), None)
    if start is None:
        return None
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    return start, end


def add_to_section(body: str, section: str, line: str) -> str:
    lines = body.rstrip("\n").split("\n")
    bounds = _section_bounds(lines, section)
    if bounds is None:
        conn = _section_bounds(lines, "Connections")
        block = [f"## {section}", line, ""]
        if conn and section != "Connections":
            lines[conn[0]:conn[0]] = block
        else:
            lines += ["", f"## {section}", line]
        return "\n".join(lines) + "\n"
    start, end = bounds
    pos = end
    while pos - 1 > start and not lines[pos - 1].strip():
        pos -= 1
    lines.insert(pos, line)
    return "\n".join(lines) + "\n"


def remove_from_section(body: str, section: str, title: str) -> str:
    lines = body.split("\n")
    bounds = _section_bounds(lines, section)
    if bounds is None:
        return body
    start, end = bounds
    keep = []
    for i, l in enumerate(lines):
        if start < i < end and l.lstrip().startswith(("-", "*")):
            if any(norm(link_target(m.group(1)) or "") == norm(title) for m in LINK_RE.finditer(l)):
                continue
        keep.append(l)
    return "\n".join(keep)


def replace_links(text: str, old: str, new: str) -> str:
    ko = norm(old)

    def sub(m):
        raw = m.group(0)
        inner = raw[raw.index("[[") + 2:-2]
        target, rest = re.match(r"([^|#^]*)(.*)", inner, re.S).groups()
        base = target.split("/")[-1].strip()
        if base.lower().endswith(".md"):
            base = base[:-3]
        if norm(base) != ko:
            return raw
        return ("!" if raw.startswith("!") else "") + f"[[{new}{rest}]]"

    return RAW_LINK_RE.sub(sub, text)


def template_body(v: Vault, layer: int, title: str) -> str:
    path = v.root / "_templates" / TEMPLATES[layer]
    if path.exists():
        _, body = split_fm(path.read_text(encoding="utf-8"))
    else:
        section = CHILD_SECTION.get(layer, "Content")
        body = f"\n# {{{{title}}}}\n\n## {section}\n\n## Connections\n"
    body = body.replace("{{title}}", title)
    return body if body.startswith("\n") else "\n" + body


# --------------------------------------------------------------------------- recall
# Field boosts, k1, b, FAMILY_LIFT, SYN_WEIGHT and USAGE_WEIGHT were fitted together by coordinate ascent on the
# 2026-10-30 bench (240 queries, 4 vaults; 5-fold CV grouped by question, 3 repeats): out-of-fold hit@1 132 → 158.7,
# frozen 38-question test 48 → 55 of 76, Holm-corrected permutation p < 0.001. "questions" and "questions_es" index
# the generated questions of _brain/questions.jsonl (doc2query, `brain.py questions`); the Spanish ones keep their
# own "es:" vocabulary. k1 and b moved between fits (a flat optimum); the title, body, tags and synonym weights did not.
BM25_FIELDS = {"title": 6, "aliases": 4, "summary": 3, "tags": 0.5, "body": 0.25, "questions": 0.5, "questions_es": 1, "keywords": 0.25}
BM25_K1, BM25_B = 2.0, 1.0
# keywords = the verify_keywords a note is born with. Hermes Cortex bench 2026-11-01 (60 questions, with the REM
# questions): boost 0 → hit@1 34 MRR 0.661; 0.25 → 34 0.662; 0.5 → 33 0.658; 1.5 → 34 0.663; 3 → 33 0.655. Neutral
# beside the generated questions; without them (engine 2.5.3) 0.5 gave +4 hit@1, so a note not yet through REM gains
# share of its best detail's match an L2 concept inherits: 0.5 gave the bench back 9/10 after atomizing
# Infrastructure (2026-10-28); with the generated questions 0 fitted best, 0.25 cost no measurable hit@1 and keeps
# concepts above their details where the details carry the match
FAMILY_LIFT = 0.25
# the usage weight enters the score as 1 + USAGE_WEIGHT·sqrt(w); a static bench cannot reward usage, so 0.25 (0 fitted
# best, within 2 of 240 hit@1) keeps what agents credit in the ranking
USAGE_WEIGHT = 0.25
QUESTIONS_FILE = "questions.jsonl"
# a word the vault never uses is read as the vault word sharing the most trigrams, at Jaccard ≥ TYPO_MIN; on the
# 2026-10-29 bench 0.4, 0.5 and 0.6 scored within one question, and 0.5 corrects the fewest real words by mistake
TYPO_MIN = 0.5
# a query word the vault never uses searches its synonyms_en.tsv group at this weight (full expansion of known
# words cost hit@1 on the 2026-10-29 bench, so only unknown words are bridged; the 2026-10-30 fit raised it from 0.5)
SYN_WEIGHT = 1.0
SURE_PRIMARIES = 5  # recall always shows this many answers before spending budget on their context
LINK_SECTIONS = {"branches", "details", "connections", "more links"}


def term_list(s: str) -> list:
    return [word_stem(t) for t in re.findall(r"[a-z0-9]+", norm(s)) if t not in STOPWORDS and len(t) > 1]


def own_text(body: str) -> str:
    """The body without the sections that only list other nodes (they would make every parent match its children)."""
    keep, section = [], None
    for line in COMMENT_RE.sub("", body).split("\n"):
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            section = norm(m.group(1))
        elif section not in LINK_SECTIONS:
            keep.append(LINK_RE.sub(lambda x: x.group(1), line))
    return "\n".join(keep)


def questions(v: Vault) -> dict:
    """_brain/questions.jsonl: generated questions per node key, {"key": {"en": [...], "es": [...], "hash": ...}}."""
    if getattr(v, "_questions", None) is None:
        v._questions = {}
        path = v.root / "_brain" / QUESTIONS_FILE
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict) and d.get("key"):
                    v._questions[str(d["key"])] = d
    return v._questions


def questions_stamp(v: Vault) -> tuple:
    path = v.root / "_brain" / QUESTIONS_FILE
    st = path.stat() if path.exists() else None
    return (st.st_mtime_ns, st.st_size) if st else (0, 0)


def content_hash(n: Node) -> str:
    """What the generated questions were written from: a changed hash marks them stale for `questions todo`."""
    import hashlib
    return hashlib.sha1(f"{n.title}\n{n.summary}\n{own_text(n.body)}".encode("utf-8")).hexdigest()[:12]


def bm25_index(v: Vault) -> dict:
    """Field-boosted term frequencies per node. Each node's entry persists in state/bm25.pickle, keyed by its file's
    mtime and size, so a call only tokenizes the nodes that changed (on Windows 0.49 s → ~0.02 s for 1,167 nodes).
    Both pickles also carry the engine version, the field boosts and the questions file's stamp: a new stemmer,
    weight or question set rebuilds them."""
    if getattr(v, "_bm25", None) is None:
        tag = (VERSION, tuple(BM25_FIELDS.items()), questions_stamp(v))
        whole = load_state(v.state_dir / "bm25-index.pickle")
        if whole.get("meta") == v._meta and whole.get("n") == len(v.net) and whole.get("tag") == tag:
            v._bm25 = whole["ix"]
            return v._bm25
        saved, kept, docs = load_state(v.state_dir / "bm25.pickle"), {"__tag__": tag}, {}
        if saved.get("__tag__") != tag:
            saved = {}
        qs = questions(v)
        for k, n in v.net.items():
            rel = getattr(n, "rel", None) or n.path.as_posix()
            meta, prev = v._meta.get(rel), saved.get(rel)
            if meta and prev and prev[0] == meta[0] and prev[1] == meta[1]:
                docs[k] = kept[rel] = prev
                continue
            tags = n.fm.get("tags")
            gen = qs.get(n.key) or {}
            fields = {"title": n.title, "aliases": " ".join(n.aliases),
                      "summary": f"{n.summary} {n.fm.get('summary_long') or ''}",
                      "tags": " ".join(map(str, tags)) if isinstance(tags, list) else str(tags or ""),
                      "body": own_text(n.body), "questions": " ".join(gen.get("en") or []),
                      "questions_es": " ".join(gen.get("es") or []),
                      "keywords": " ".join(map(str, n.fm.get("verify_keywords") or []))}
            tf: dict[str, float] = {}
            length = 0.0
            for f, boost in BM25_FIELDS.items():
                terms = es_terms(fields[f]) if f == "questions_es" else term_list(fields[f])
                length += boost * len(terms)
                for t in terms:
                    tf[t] = tf.get(t, 0.0) + boost
            docs[k] = kept[rel] = (meta[0] if meta else 0, meta[1] if meta else 0, tf, length)
        if kept.keys() != saved.keys() or any(kept[r] is not saved.get(r) for r in kept if r != "__tag__"):
            if (v.root / "_brain").is_dir():
                save_state(v.state_dir / "bm25.pickle", kept)
        docs = {k: (d[2], d[3]) for k, d in docs.items()}
        df: dict[str, int] = {}
        for tf, _ in docs.values():
            for t in tf:
                df[t] = df.get(t, 0) + 1
        avg = sum(l for _, l in docs.values()) / max(len(docs), 1)
        tri: dict[str, list] = {}
        for t in df:
            if len(t) >= 4 and not t.startswith("es:"):
                for g in trigrams(t):
                    tri.setdefault(g, []).append(t)
        v._bm25 = {"docs": docs, "df": df, "avg": avg or 1.0, "n": len(docs), "tri": tri}
        if (v.root / "_brain").is_dir():
            save_state(v.state_dir / "bm25-index.pickle", {"meta": v._meta, "n": len(v.net), "ix": v._bm25, "tag": tag})
    return v._bm25


MONTHS_ES = {m: i for i, m in enumerate("enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split(), 1)}


def es_stem(word: str) -> str:
    """The Snowball Spanish stem (snowball.py): ejecutando and ejecutar both give "ejecut"; it matches glossary
    entries by stem and the Spanish generated questions to a Spanish query."""
    import snowball
    return snowball.stem_es(word)


def es_terms(text: str) -> list:
    """A Spanish text as "es:"-namespaced stems, the terms of the questions_es field: kept apart from the English
    vocabulary, they meet only a Spanish query's own words."""
    return ["es:" + es_stem(w) for w in re.findall(r"[a-z0-9]+", norm(text))
            if w not in SPANISH_STOP and w not in STOPWORDS and len(w) > 1]


def glossary(v) -> dict:
    """The Spanish to English vocabulary: the engine's lexicon_es.tsv, then the vault's _brain/glossary.json
    ({"stop": "words", "es": {"spanish words": "english words"}}), which wins. Single words also match by stem."""
    if not hasattr(v, "_glossary"):
        es: dict[tuple, str] = {}
        lex = Path(__file__).resolve().parent / "lexicon_es.tsv"
        if lex.exists():
            for line in lex.read_text(encoding="utf-8").splitlines():
                if line and not line.startswith("#") and "	" in line:
                    k, val = line.split("	", 1)
                    es[tuple(norm(k).split())] = val.strip()
        path = v.root / "_brain" / "glossary.json"
        g = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        es.update({tuple(norm(k).split()): val for k, val in g.get("es", {}).items()})
        stems = {}
        for k, val in es.items():
            if len(k) == 1:
                stems.setdefault(es_stem(k[0]), val)
        v._glossary = {"stop": set(norm(g.get("stop", "")).split()) | SPANISH_STOP, "es": es, "stems": stems,
                       "longest": max((len(k) for k in es), default=1)}
    return v._glossary


def translate_query(v, query: str) -> str:
    """A Spanish query in English keywords: the vault is English. Unknown words (names, codes) pass through,
    Spanish function words drop, and "6 de octubre" becomes 10-06 as the notes write dates."""
    g = glossary(v)
    if not looks_spanish(query):
        return query
    q = norm(query)
    q = re.sub(r"\b(\d{1,2}) de (" + "|".join(MONTHS_ES) + r")\b", lambda m: f"{MONTHS_ES[m.group(2)]:02d}-{int(m.group(1)):02d}", q)
    words, out, i = re.findall(r"[a-z0-9][a-z0-9-]*", q), [], 0
    while i < len(words):
        for size in range(min(g["longest"], len(words) - i), 0, -1):
            key = tuple(words[i:i + size])
            if key in g["es"]:
                out.append(g["es"][key])
                i += size
                break
        else:
            if words[i] not in g["stop"]:
                out.append(g["stems"].get(es_stem(words[i]), words[i]))
            i += 1
    return " ".join(out) or query


@functools.lru_cache(maxsize=1)
def synonyms() -> dict:
    """synonyms_en.tsv next to the engine: each word's stem maps to the stems of the rest of its group."""
    out: dict[str, set] = {}
    path = Path(__file__).resolve().parent / "synonyms_en.tsv"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                group = set(term_list(line))
                for t in group:
                    out.setdefault(t, set()).update(group - {t})
    return out


def trigrams(t: str) -> set:
    t = f"^{t}$"
    return {t[i:i + 3] for i in range(len(t) - 2)}


def correct_typos(ix: dict, terms: set) -> set:
    """A query term the vault never uses (a typo: "vualt", "recal") becomes the vault term closest by shared
    trigrams, if close enough (Jaccard ≥ TYPO_MIN); terms the vault knows are left alone."""
    out = set()
    for t in terms:
        if t in ix["df"] or len(t) < 4:
            out.add(t)
            continue
        mine, votes = trigrams(t), {}
        for g in mine:
            for cand in ix.get("tri", {}).get(g, ()):
                votes[cand] = votes.get(cand, 0) + 1
        best, best_j = None, 0.0
        for cand, shared in votes.items():
            j = shared / (len(mine) + len(cand) - shared)
            if j > best_j or (j == best_j and best and ix["df"][cand] > ix["df"][best]):
                best, best_j = cand, j
        out.add(best if best and best_j >= TYPO_MIN else t)
    return out


def truth_factor(v: Vault, n: Node) -> float:
    # a node another one supersedes counts as superseded, unless the curator tagged it historical: a dated record
    # stays true about its date even when replaced, and overriding that tag cost three past-tense questions ("where
    # was the Copilot CLI on Windows?") on the 2026-10-30 bench of 7 vaults. `current` is the default tag, so it
    # does not shield a replaced node.
    truth = str(n.fm.get("truth") or "current")
    if truth != "historical" and any(t == "supersedes" for t, _ in v.incoming.get(n.k, [])):
        return TRUTH_FACTOR["superseded"]
    return TRUTH_FACTOR.get(truth, 1.0)


def host_system(v: Vault | None = None) -> str:
    """The system this agent runs on: CORTEX_SYSTEM, else the vault's config "system", else detected."""
    if os.environ.get("CORTEX_SYSTEM"):
        return os.environ["CORTEX_SYSTEM"].strip().lower()
    if v is not None and v.cfg.get("system"):
        return str(v.cfg["system"]).strip().lower()
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("ID="):
                return line[3:].strip().strip('"').lower()
    except OSError:
        pass
    return "linux"


def node_systems(n: Node) -> list[str]:
    """The systems a note holds on; empty = every system."""
    x = n.fm.get("system")
    return [str(i).strip().lower() for i in (x if isinstance(x, list) else [x]) if i not in (None, "")]


def foreign(n: Node, host: str) -> bool:
    systems = node_systems(n)
    return bool(systems) and host not in systems


def system_mark(n: Node, host: str) -> str:
    return f" ({'/'.join(node_systems(n)).upper()} ONLY)" if foreign(n, host) else ""


def twinned(v: Vault, host: str) -> set[str]:
    """Keys of the notes this system has a twin of: the targets of links from this system's own notes."""
    return {norm(s.target) for n in v.net.values() if host in node_systems(n) for s in n.synapses}


def score(v: Vault, query: str, weights: dict) -> list[tuple[float, Node]]:
    """BM25 over title, aliases, summary, tags, the node's own body and its generated questions, times a damped
    weight. The weight enters as 1 + USAGE_WEIGHT·sqrt(w): on an earlier bench (2026-10-23) the full weight cost a
    correct answer."""
    raw_query, query = query, translate_query(v, query)
    ix, phrase = bm25_index(v), norm(query)
    # a word the vault never uses is searched through its synonyms the vault does use; failing that, it is
    # taken for a typo of the closest vault word. A Spanish query also searches its own words, untranslated,
    # in the Spanish generated questions
    qw, unknown = {}, set()
    if looks_spanish(raw_query):
        qw.update({t: 1.0 for t in es_terms(raw_query) if t in ix["df"]})
    for t in set(term_list(query)):
        if t in ix["df"]:
            qw[t] = 1.0
            continue
        known = [x for x in synonyms().get(t, ()) if x in ix["df"]]
        for x in known:
            qw.setdefault(x, SYN_WEIGHT)
        if not known:
            unknown.add(t)
    for t in correct_typos(ix, unknown):
        qw.setdefault(t, 1.0)
    q = set(qw)
    idf = {t: qw[t] * math.log(1 + (ix["n"] - ix["df"].get(t, 0) + 0.5) / (ix["df"].get(t, 0) + 0.5)) for t in q}
    raw = {}
    for k, n in v.net.items():
        if STATUS_FACTOR.get(n.status, 0.8) == 0 or n.layer not in (1, 2, 3):
            continue
        tf, length = ix["docs"][k]
        norm_len = BM25_K1 * (1 - BM25_B + BM25_B * length / ix["avg"])
        s = sum(idf[t] * tf[t] * (BM25_K1 + 1) / (tf[t] + norm_len) for t in q if t in tf)
        if not s:
            continue
        if len(phrase) > 3 and phrase in norm(n.title):
            s *= 1.5
        raw[k] = s
    # an atomized concept keeps its facts in its details: it inherits part of its best detail's match
    lift = {}
    for k, s in raw.items():
        p = v.parent(v.net[k]) if v.net[k].layer == 3 else None
        if p is not None and p.layer == 2 and STATUS_FACTOR.get(p.status, 0.8):
            lift[p.k] = max(lift.get(p.k, 0.0), s)
    res, host = {}, host_system(v)
    twins = twinned(v, host)
    for k in set(raw) | set(lift):
        n = v.net[k]
        s = raw.get(k, 0.0) + FAMILY_LIFT * lift.get(k, 0.0)
        w = 0.0 if n.status == "provisional" else weights.get(n.key, 0.0)  # use confirms a provisional node, never ranks it
        res[k] = (s * (1 + USAGE_WEIGHT * math.sqrt(w)) * STATUS_FACTOR.get(n.status, 0.8) * truth_factor(v, n)
                  * (OTHER_SYSTEM if k in twins and foreign(n, host) else 1))
    # a detail that matches on its own at least as well as its concept goes before it: the concept only led on the
    # detail's borrowed match, and the detail holds the fact (2026-10-29 bench: 11 of 18 misses in one vault were this)
    for k, s in raw.items():
        n = v.net[k]
        p = v.parent(n) if n.layer == 3 else None
        # never a superseded detail: it stays below the concept that holds today's fact
        if p is not None and p.k in res and s >= raw.get(p.k, 0.0) and res[k] < res[p.k] and truth_factor(v, n) >= 1.0:
            res[k] = res[p.k] * 1.0001
    out = sorted(((s, v.net[k]) for k, s in res.items()), key=lambda x: (-x[0], x[1].layer, x[1].k))
    return out


def coverage(v: Vault, query: str, n: Node) -> float:
    """Share of the query's idf mass found in the node's title, aliases and summary; 0 when fewer than two of the
    query's words are there (a one-word match says little)."""
    ix = bm25_index(v)
    terms = {t for t in term_list(translate_query(v, query)) if t in ix["df"]}
    if not terms:
        return 0.0
    idf = {t: math.log(1 + (ix["n"] - ix["df"][t] + 0.5) / (ix["df"][t] + 0.5)) for t in terms}
    head = set(term_list(" ".join([n.title, n.summary, *n.aliases])))
    hits = [t for t in terms if t in head]
    return sum(idf[t] for t in hits) / sum(idf.values()) if len(hits) >= 2 else 0.0


def gate_probability(v: Vault, query: str, ranked: list) -> tuple[float, float]:
    """The answer gate's probability that the top of `ranked` is right (see GATE), and the top's coverage."""
    s1 = ranked[0][0]
    s2 = ranked[1][0] if len(ranked) > 1 else 0.0
    ix = bm25_index(v)
    known = {t for t in term_list(translate_query(v, query)) if t in ix["df"]}
    cov = coverage(v, query, ranked[0][1])
    x = (s2 / s1 if s1 else 1.0, cov, coverage(v, query, ranked[1][1]) if len(ranked) > 1 else 0.0,
         math.log1p(len(known)), 1.0 if looks_spanish(query) else 0.0)
    z = GATE["bias"] + sum(w * (xi - m) / s for w, xi, m, s in zip(GATE["w"], x, GATE["mu"], GATE["sd"]))
    return 1 / (1 + math.exp(-max(min(z, 50), -50))), cov


def signature(n: Node, weights: dict, extra: str = "", host: str = "") -> str:
    w = weights.get(n.key, 0.0)
    status = "PROVISIONAL, unconfirmed" if n.status == "provisional" else n.status
    if host and foreign(n, host):
        status += f" · {'/'.join(node_systems(n)).upper()} ONLY, not this {host}"
    return f"[L{n.layer}] {n.title} — {n.summary} (weight {w:.2f} · {n.tokens} tok · {status}{extra})"


class Output:
    def __init__(self, lines: int, chars: int):
        self.max_l, self.max_c, self.buf, self.trimmed = lines, chars, [], 0

    def add(self, line: str, keep: tuple[int, int] = (0, 0)) -> bool:
        """keep = (lines, chars) held back for lines that must still fit after this one."""
        if len(self.buf) + keep[0] >= self.max_l or sum(len(x) + 1 for x in self.buf) + len(line) + keep[1] > self.max_c:
            self.trimmed += 1
            return False
        self.buf.append(line)
        return True


def traverse(v: Vault, query: str, mode: dict, weights: dict, co: dict, label: str = "",
             results: list | None = None) -> tuple[list[str], list[Node]]:
    results = (results if results is not None else score(v, query, weights))[: mode["top"]]
    out, listed = Output(mode["lines"], mode["chars"]), set()
    primary = []
    host = host_system(v)
    sigs = [label + signature(n, weights, host=host) for _, n in results]
    for i, (_, n) in enumerate(results):
        if n.k in listed:
            continue
        if not out.add(sigs[i]):
            break
        listed.add(n.k)
        primary.append(n)
        if i >= mode["expand"]:
            continue
        # context never crowds out the next answers: the first SURE_PRIMARIES signatures keep their room
        ahead = [s for j, s in enumerate(sigs[i + 1:SURE_PRIMARIES], i + 1) if results[j][1].k not in listed]
        keep = (len(ahead), sum(len(s) + 1 for s in ahead))
        for p in v.chain(n):
            if p.k not in listed and out.add(f"   ↑ [L{p.layer}] {p.title}", keep):
                listed.add(p.k)
        nb = sorted(v.neighbors(n), key=lambda x: (SYNAPSES.get(x[0], 9), -co.get(pair(n.key, x[1].key), 0.0), -weights.get(x[1].key, 0.0)))
        for typ, m, d in nb[: mode["neighbors"]]:
            if m.k in listed:
                continue
            if out.add(f"   ↳ {typ}{d} [L{m.layer}] {m.title}" + (" (OBSOLETE)" if m.status == "obsolete" else "")
                       + system_mark(m, host), keep):
                listed.add(m.k)
            if mode["hops"] >= 2 and typ != "related":
                for typ2, m2, d2 in [x for x in v.neighbors(m) if x[0] != "related"][:2]:
                    if m2.k not in listed and out.add(f"      ↳ {typ2}{d2} [L{m2.layer}] {m2.title}" + system_mark(m2, host), keep):
                        listed.add(m2.k)
    if out.trimmed:
        out.buf.append(f"# … {out.trimmed} lines trimmed by budget")
    return out.buf, primary


# --------------------------------------------------------------------------- read commands
def cmd_session(v: Vault, a) -> int:
    s = Sessions(v)
    if a.action == "new":
        sid, notes = s.open(a.mode, a.by)
        m = MODES[a.mode]
        print(sid)
        print(f"# mode {a.mode}: {m['bodies']} bodies per query · {m['session_bodies']} per session · {m['queries']} queries"
              + (f" · the task session of {a.by} until session close or its next session new" if a.by else ""))
        for line in notes:
            print(line)
        return 0
    if a.action == "close":
        if not a.session and not a.by:
            raise BrainError("session close needs --session or --by")
        if not a.session and not s.open_task(a.by):
            print(f"no task session open for {a.by}")
            return 0
        sid = a.session or s.open_task(a.by)
        for line in s.close(sid, a.by):
            print(line)
        print(f"session {sid} closed")
        return 0
    if not a.session:   # the session a call would use, looked at without touching it: a look must not keep it alive
        a.session = s.open_task(a.by)
    if not a.session:
        try:
            ptr = s.dir / (f"auto-{agent_slug(a.by)}.json" if a.by else "auto.json")
            cur = json.loads(ptr.read_text(encoding="utf-8"))
            a.session = cur["sid"] if time.time() - cur.get("touched", 0) <= s.idle_seconds() else None
            s.load(a.session or "")
        except (OSError, ValueError, KeyError, BrainError):
            a.session = None
        if not a.session:
            print("no session open: the next recall or read opens one with the whole budget")
            return 0
    d = s.load(a.session)
    m = MODES[d["mode"]]
    print(f"session {a.session} · mode {d['mode']} · queries {d['queries']}/{m['queries']} · "
          f"bodies {d['total_bodies']}/{m['session_bodies']} · read: {len(d['visited'])}"
          + (" · closed" if d.get("closed") else ""))
    return 0


def cmd_recall(v: Vault, a) -> int:
    ses = Sessions(v)
    notes = []
    if not a.session:
        a.session, notes = ses.auto(a.mode, by=a.by)
    data = ses.load(a.session)
    mode_name = data["mode"] if data and not a.mode else (a.mode or "normal")
    if data and MODES[mode_name]["bodies"] > MODES[data["mode"]]["bodies"]:
        raise BrainError(f"the session is in {data['mode']} mode; deep mode is used in a deep session (subagent)")
    mode = MODES[mode_name]
    if data:
        if data["queries"] >= mode["queries"]:
            raise BrainError(f"limit of {mode['queries']} queries in this session: answer with what you have", 2)
        data["queries"] += 1
        data["query_bodies"] = 0
        data["query"] = a.query
        ses.save(a.session, data)
    tel = Telemetry(v)
    weights, co = tel.weights(), tel.coactivation()
    ranked = score(v, a.query, weights)
    lines, primary = traverse(v, a.query, mode, weights, co, results=ranked)
    for line in notes:
        print(line)
    print(f'# recall "{a.query}" · vault {v.name} · system {host_system(v)} · mode {mode_name} · {len(primary)} signatures')
    if translate_query(v, a.query) != a.query:
        print(f'# searched as: {translate_query(v, a.query)}')
    if not lines:
        print("# no matches. Orientation (L1):")
        for n in sorted((x for x in v.net.values() if x.layer == 1 and x.up and norm(x.up) == "map"), key=lambda x: -weights.get(x.key, 0))[:8]:
            print(f"[L1] {n.title} — {n.summary}")
    else:
        print("\n".join(lines))
    if a.all:
        for name in v.cfg.get("siblings", {}):
            try:
                h = v.sibling(name)
            except BrainError as ex:
                print(f"# {ex}")
                continue
            th = Telemetry(h)
            sub = dict(mode, lines=max(4, mode["lines"] // 2), chars=max(500, mode["chars"] // 2), expand=0)
            lh, _ = traverse(h, a.query, sub, th.weights(), {}, label=f"{{{name}}} ")
            if lh:
                print(f"# sibling vault {name} (read it with its own brain.py):")
                print("\n".join(lh))
    # the gate: a clear winner comes with its body, so the agent answers without a read call
    if ranked and not a.no_body and data["total_bodies"] < mode["session_bodies"]:
        n = ranked[0][1]
        p, cov = gate_probability(v, a.query, ranked)
        if (n.layer in (2, 3) and n.status in ("seed", "consolidated") and n.key not in data["visited"]
                and p >= GATE["threshold"]):
            other = f" · {'/'.join(node_systems(n)).upper()} ONLY, not this {host_system(v)}" if foreign(n, host_system(v)) else ""
            print(f"# answer: [L{n.layer}] {n.title} ({n.path.as_posix()}) · {n.status}{other} · "
                  f"{p:.0%} likely the answer; its title and summary cover {cov:.0%} of the question")
            lines_given = body_lines(n, v, a.query, data)
            print("\n".join(lines_given))
            amount = count_tokens(handed_text(lines_given))
            if n.key in data.get("excerpted", []):
                data.setdefault("excerpt_tokens", {})[n.key] = amount
            data["tokens_read"] = data.get("tokens_read", 0) + amount
            add_tokens_read(v, n.key, amount)
            data["visited"].append(n.key)
            data["query_bodies"] += 1
            data["total_bodies"] += 1
            ses.save(a.session, data)
            tel.record("read", [n.key], a.session)
            print(f"# budget: {data['query_bodies']}/{mode['bodies']} query · {data['total_bodies']}/{mode['session_bodies']} session · "
                  f"{data['tokens_read']} tok read")
    left = mode["session_bodies"] - data["total_bodies"]
    print(f'# read: brain.py read "<Title>" · max {mode["bodies"]} bodies in this query · {left} left in the session')
    tel.record("activated", [n.key for n in primary], a.session, query=a.query)
    return 0


def excerpt(v: Vault, query: str, body: str) -> tuple[str, int, int]:
    """The lines and sentences of a body that answer the query, in order, with "…" where some were left out:
    (text, kept, total). Code blocks and table rows are units of their own; a heading stays with what it heads."""
    units, fence, section = [], None, None  # (kind, text), kind "head", "code" or "text"
    for line in body.split("\n"):
        m = re.match(r"^##\s+(.+?)\s*$", line) if fence is None else None
        if m:
            section = norm(m.group(1))
        if fence is None and (section in LINK_SECTIONS or re.match(r"#\s", line)):
            continue  # recall already lists the linked nodes, and the output's first line carries the title
        if fence is not None:
            fence.append(line)
            if line.lstrip().startswith("```"):
                units.append(("code", "\n".join(fence)))
                fence = None
            continue
        if line.lstrip().startswith("```"):
            fence = [line]
        elif not line.strip():
            continue
        elif line.startswith("#"):
            units.append(("head", line))
        elif re.match(r"\s*([-*+]|\d+\.|\|)", line):
            units.append(("text", line))
        else:
            units += [("text", x) for x in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9¿¡\"(`*])", line) if x.strip()]
    if fence:
        units.append(("code", "\n".join(fence)))
    ix = bm25_index(v)
    terms = {t for t in term_list(translate_query(v, query)) if t in ix["df"]}
    idf = {t: math.log(1 + (ix["n"] - ix["df"][t] + 0.5) / (ix["df"][t] + 0.5)) for t in terms}
    scores = [0.0 if k == "head" else sum(idf[t] for t in set(term_list(u)) & terms) for k, u in units]
    best = max(scores, default=0.0)
    if not best:
        return body, len(units), len(units)
    keep = {i for i, sc in enumerate(scores) if sc >= EXCERPT_KEEP * best}
    keep.add(next(i for i, (k, _) in enumerate(units) if k != "head"))
    for i in sorted(keep):  # the heading over each kept unit, and a table's header rows
        for j in range(i - 1, -1, -1):
            if units[j][0] == "head":
                keep.add(j)
                break
        if units[i][1].lstrip().startswith("|"):
            j = i
            while j > 0 and units[j - 1][1].lstrip().startswith("|"):
                j -= 1
            keep.update(range(j, min(j + 2, i)))
    if sum(len(units[i][1]) for i in keep) > 0.7 * sum(len(u) for _, u in units):
        return body, len(units), len(units)
    out, last = [], -1
    for i in sorted(keep):
        if i > last + 1:
            out.append("…")
        out.append(units[i][1])
        last = i
    if last < len(units) - 1:
        out.append("…")
    return "\n".join(out), len(keep), len(units)


def body_lines(n: Node, v: Vault | None = None, query: str = "", data: dict | None = None) -> list[str]:
    """The body to hand over; with a query, only the part that answers it (see excerpt)."""
    body = COMMENT_RE.sub("", n.body).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    note = None
    if v is not None and query and len(body) > EXCERPT_MIN:
        text, kept, total = excerpt(v, query, body)
        if kept < total:
            body = text
            note = f'# excerpt: {kept} of {total} lines · read --full for the rest'
            if data is not None:
                data.setdefault("excerpted", []).append(n.key)
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY].rstrip() + "\n[truncated: the node exceeds the atomic size — propose atomizing it]"
    out = []
    if n.status == "provisional":
        out.append(f"# provisional: learned by {n.fm.get('learned_by') or 'an agent'} on {n.fm.get('created')}, "
                   "not yet confirmed by use; verify before relying on it")
    if n.external:
        out.append("external: " + "; ".join(f"{r[0]}::{r[1]}" for r in map(parse_uri, n.external) if r))
    out.append(body)
    if note:
        out.append(note)
    return out


def handed_text(lines: list[str]) -> str:
    """The text from body_lines that the CLI actually hands to the agent."""
    return "\n".join(lines)


def add_tokens_read(v: Vault, key: str, amount: int) -> None:
    path = v.state_dir / "tokens_read.json"
    try:
        totals = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        totals = {}
    totals[key] = totals.get(key, 0) + amount
    write_atomic(path, json.dumps(totals, ensure_ascii=False, sort_keys=True))


def cmd_stats(v: Vault, a) -> int:
    nodes = list(v.net.values())
    counts = sorted(n.tokens for n in nodes)
    total = sum(counts)
    mean = total / len(counts) if counts else 0
    print(f"tokens: {total} vault · {mean:.1f} mean · {max(counts, default=0)} max · {len(nodes)} nodes")
    print("heaviest nodes:")
    for n in sorted(nodes, key=lambda x: (-x.tokens, x.k))[:10]:
        print(f"  {n.tokens:>5} tok · {n.title}")
    try:
        reads = json.loads((v.state_dir / "tokens_read.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        reads = {}
    print("most tokens read:")
    by_key = v.by_key()
    ranked = sorted(((amount, by_key[key].title if key in by_key else key)
                     for key, amount in reads.items()), key=lambda x: (-x[0], x[1]))[:10]
    for amount, title in ranked:
        print(f"  {amount:>5} tok · {title}")
    if not ranked:
        print("  (none yet)")
    return 0


def cmd_read(v: Vault, a) -> int:
    ses = Sessions(v)
    if not a.session:
        a.session, notes = ses.auto(by=a.by)
        for line in notes:
            print(line)
    data = ses.load(a.session)
    mode = MODES[data["mode"]]
    k = norm(a.title)
    n = None
    if k in v.net or any(k in (norm(x) for x in m.aliases) for m in v.net.values()):
        n = v.node(a.title)
    elif k in v.titles:
        rel = v.titles[k][0]
        if rel.parts[0] in COLD_FOLDERS and not a.cold:
            raise BrainError('it is a cold-ring note: read it only with --cold "<reason>"')
        n = read_node(v.root, rel, cold=True)
    else:
        v.node(a.title)
    if n.key in data["visited"]:
        if not (a.full and n.key in data.get("excerpted", [])):
            raise BrainError(f'"{n.title}" was already read in this session: it is already in your context', 2)
        # the rest of a body first handed over as an excerpt costs no new body from the budget
        data["excerpted"].remove(n.key)
        ses.save(a.session, data)
        print(f"# [L{n.layer}] {n.title} ({n.path.as_posix()}) · in full · {n.tokens} tok")
        lines = body_lines(n)
        print("\n".join(lines))
        amount = max(0, count_tokens(handed_text(lines)) - data.get("excerpt_tokens", {}).pop(n.key, 0))
        data["tokens_read"] = data.get("tokens_read", 0) + amount
        ses.save(a.session, data)
        add_tokens_read(v, n.key, amount)
        print(f"# budget: {data['query_bodies']}/{mode['bodies']} query · {data['total_bodies']}/{mode['session_bodies']} session · "
              f"{data['tokens_read']} tok read")
        return 0
    if data["query_bodies"] >= mode["bodies"]:
        raise BrainError(f"query budget exhausted ({mode['bodies']} bodies): answer with what you have "
                         "or run a new recall if the question changed", 2)
    if data["total_bodies"] >= mode["session_bodies"]:
        raise BrainError(f"session budget exhausted ({mode['session_bodies']} bodies): answer with what you have "
                         "or delegate to a subagent in deep mode", 2)
    parent = f" · ↑ {n.up}" if n.up else ""
    layer = f"L{n.layer}" if n.layer else "cold"
    print(f"# [{layer}] {n.title} ({n.path.as_posix()}){parent} · {n.status} · {n.tokens} tok")
    if n.summary:
        print(f"summary: {n.summary}")
    lines = body_lines(n, v, "" if a.full else data.get("query", ""), data)
    print("\n".join(lines))
    amount = count_tokens(handed_text(lines))
    if n.key in data.get("excerpted", []):
        data.setdefault("excerpt_tokens", {})[n.key] = amount
    data["visited"].append(n.key)
    data["query_bodies"] += 1
    data["total_bodies"] += 1
    data["tokens_read"] = data.get("tokens_read", 0) + amount
    ses.save(a.session, data)
    add_tokens_read(v, n.key, amount)
    print(f"# budget: {data['query_bodies']}/{mode['bodies']} query · {data['total_bodies']}/{mode['session_bodies']} session · "
          f"{data['tokens_read']} tok read")
    Telemetry(v).record("read", [n.key], a.session)
    return 0


def cmd_credit(v: Vault, a) -> int:
    keys, missing = [], []
    for t in a.used:
        try:
            keys.append(v.node(t).key)
        except BrainError:
            missing.append(t)
    keys = list(dict.fromkeys(keys))
    ses = Sessions(v)
    if not a.session:
        a.session, _ = ses.auto(touch=False, by=a.by)
    try:
        data = ses.load(a.session)
        data["credited"] = True
        ses.save(a.session, data)
    except BrainError:
        pass
    tel = Telemetry(v)
    tel.record("used", keys, a.session, group=os.urandom(4).hex())
    with v.lock():
        write_hot(v, tel)
    print(f"credited {len(keys)} nodes" + (f" · not found: {', '.join(missing)}" if missing else ""))
    for title in confirm_provisional(v, keys):
        print(f'confirmed "{title}": provisional → seed')
    return 0


def confirm_provisional(v: Vault, keys) -> list[str]:
    """A provisional node becomes a seed once it helped in confirm_sessions distinct sessions, is confirm_min_days
    old and is in no contradiction either way. The curator samples what was confirmed (consolidate lists it)."""
    need = int(v.cfg.get("confirm_sessions", CONFIRM_SESSIONS))
    min_days = int(v.cfg.get("confirm_min_days", CONFIRM_MIN_DAYS))
    by_key, events, done = v.by_key(), Telemetry(v).events(), []
    for k in keys:
        n = by_key.get(k)
        if not n or n.status != "provisional" or age_days(v, n) < min_days:
            continue
        if any(s.type == "contradicts" for s in n.synapses) or any(t == "contradicts" for t, _ in v.incoming.get(n.k, [])):
            continue
        if len({e.get("s") for e in events if e.get("e") == "used" and e.get("k") == k and e.get("s")}) < need:
            continue
        n.fm["status"], n.fm["confirmed"] = "seed", today()
        with v.lock():
            save_node(v.root, n, touch=False)
        done.append(n.title)
    return done


def sensitive(text: str, allowed: set) -> str | None:
    """What in the text looks like a credential or a person's address, or None (the audit's checks, for one text)."""
    for line in text.splitlines():
        if not SENSITIVE_PREFILTER.search(line):
            continue
        for name, pattern in SECRETS:
            if pattern.search(line):
                return name
        m = GENERIC_SECRET.search(line)
        if m and re.search(r"\d", m.group(1)) and re.search(r"[A-Za-z]", m.group(1)):
            return "token or key"
        if any(m.group(0).lower() not in allowed for m in EMAIL.finditer(line)):
            return "email address"
    return None


def cmd_learn(v: Vault, a) -> int:
    """An agent's own learning: one provisional L3 under an existing concept, checked before anything is written.
    Agents never edit nodes: a node they find wrong is answered with a learning that contradicts it."""
    typ = a.type or "fact"
    if typ not in TYPES_BY_LAYER[3]:
        raise BrainError(f'type "{typ}" not valid: {", ".join(sorted(TYPES_BY_LAYER[3]))}')
    by = a.by or v.cfg.get("learner") or "agent"
    if not re.fullmatch(r"[\w-]{1,32}", by):
        raise BrainError("--by is a short agent name")
    validate_title(v, a.title)
    validate_summary(a.summary)
    try:
        parent = v.node(a.up)
        validate_parent(3, parent)
        missing = None
    except BrainError as e:   # no concept to hang it from (a new vault has none): it goes to _inbox, not lost
        parent, missing = None, str(e)
    content, evidence = (a.content or "").strip(), (a.evidence or "").strip()
    if not content or not evidence:
        raise BrainError("--content and --evidence must not be empty: say what happened and how it is known")
    if "[[" in content + evidence:
        raise BrainError("no links in a learning: the curator links nodes (use --contradicts for a node it corrects)")
    words = len(re.findall(r"\w+", content + "\n" + evidence))
    if words > MAX_WORDS[3]:
        raise BrainError(f"{words} words (max {MAX_WORDS[3]}): one fact per learning")
    spanish = looks_spanish(f"{a.title}\n{a.summary}")
    if spanish:
        raise BrainError(f"title or summary looks Spanish ({spanish}): the vault is written in English")
    if a.truth == "current" and not a.verify_intent:
        raise BrainError("a current learning needs its verify contract: --verify-intent (" + ", ".join(VERIFY_INTENTS)
                         + "), --verify-keywords and, when something can be checked, --verify lines such as "
                         '"key a.b in <file> = <value>", "path <path>" or "grep <regex> in <file>" '
                         "(use --truth historical for a dated record)")
    contract = contract_errors(a.verify_intent, a.verify_keywords or [], a.verify or [])
    if contract:
        raise BrainError("; ".join(contract))
    found = sensitive("\n".join((a.title, a.summary, content, evidence, *(a.verify or []))),
                      {x.lower() for x in v.cfg.get("allowed_addresses", {})})
    if found:
        raise BrainError(f"possible {found}: never store credentials or personal data")
    if parent is None:
        return capture_learning(v, a, by, content, evidence, missing)
    target = v.node(a.contradicts) if a.contradicts else None
    cap = int(v.cfg.get("max_provisional", MAX_PROVISIONAL))
    if sum(n.status == "provisional" for n in v.net.values()) >= cap:
        raise BrainError(f"{cap} provisional nodes await confirmation: write this one to _inbox instead", 2)
    sig = tokens(a.title) | tokens(a.summary)
    for m in v.net.values():
        other = tokens(m.title) | tokens(m.summary)
        if m is not target and sig and other and len(sig & other) / len(sig | other) >= 0.6:
            raise BrainError(f'"{m.title}" already says something close: credit it if it holds, '
                             f'or learn with --contradicts "{m.title}" if it is wrong')
    rel = expected_path(3, a.title, parent)
    if (v.root / rel).exists():
        raise BrainError(f"{rel.as_posix()} already exists")
    fm = {"id": new_id(), "layer": 3, "type": typ, "summary": a.summary.strip(), "up": f"[[{parent.title}]]",
          "aliases": [], "status": "provisional", "source": f"learned by {by} {today()}", "external": [],
          "created": today(), "updated": today(), "reviewed": None, "truth": a.truth, "learned_by": by}
    if a.verify_intent:
        fm.update(verify_intent=a.verify_intent, verify_keywords=a.verify_keywords or [], verify=a.verify or [])
    if a.system:
        fm["system"] = [x.strip().lower() for x in a.system]
    body = (f"\n## Content\n{content}\n\n## Evidence\n{evidence}\n\n## Connections\n"
            "%% Format: - type:: link to target — reason. Types: depends_on, contradicts, causes, example_of, related. Max 7. %%\n")
    if target:
        body += f"- contradicts:: [[{target.title}]] — learned by {by}: see Evidence\n"
    n = Node(rel, a.title, fm, body, [])
    with v.lock():
        save_node(v.root, n, touch=False)
        parent.body = add_to_section(parent.body, CHILD_SECTION[2], f"- [[{a.title}]] — {a.summary.strip()}")
        save_node(v.root, parent, touch=False)
    Telemetry(v).record("planted", [n.key], extra={"st": "provisional"})
    need = int(v.cfg.get("confirm_sessions", CONFIRM_SESSIONS))
    print(f"learned {rel.as_posix()} (L3 {typ}, provisional): it becomes a seed once it helps in {need} sessions"
          + (f"; it contradicts \"{target.title}\", so the curator decides" if target else ""))
    return 0


def capture_learning(v: Vault, a, by: str, content: str, evidence: str, why: str) -> int:
    """A learning with no L2 concept to hang from (a new vault has none yet) is not lost: it is captured in _inbox,
    already checked, for the curator to place (the atomize skill), the way a capture is."""
    rel = Path("_inbox") / f"{now().strftime('%Y-%m-%d %H%M')} {a.title}.md"
    if (v.root / rel).exists():
        raise BrainError(f"{rel.as_posix()} already exists")
    lines = ["---", f"captured: {today()}", f"source: learned by {by}", f"learned_by: {by}",
             f"wanted_up: {json.dumps(a.up or '')}", f"type: {a.type or 'fact'}", f"truth: {a.truth}",
             f"summary: {json.dumps(a.summary.strip())}", "---", "", f"# {a.title}", "",
             "## Content", content, "", "## Evidence", evidence, ""]
    with v.lock():
        (v.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (v.root / rel).write_text("\n".join(lines), encoding="utf-8")
    print(f"captured {rel.as_posix()} in _inbox: {why.rstrip('.')}, so there is no concept to hang it from yet. "
          "The curator places it (atomize); to learn straight into the graph next time, create the L1 domain and "
          "the L2 concept first (cortex_curate verb=create, or `new --layer 1/2`)")
    return 0


# --------------------------------------------------------------------------- write commands
def cmd_new(v: Vault, a) -> int:
    layer = a.layer
    typ = a.type or DEFAULT_TYPE[layer]
    if typ not in TYPES_BY_LAYER[layer]:
        raise BrainError(f'type "{typ}" not valid for L{layer}: {", ".join(sorted(TYPES_BY_LAYER[layer]))}')
    validate_title(v, a.title)
    validate_summary(a.summary)
    up = a.up or ("MAP" if layer == 1 else None)
    if not up:
        raise BrainError("--up is required for L2 and L3")
    parent = None if norm(up) == "map" else v.node(up)
    validate_parent(layer, parent)
    rel = expected_path(layer, a.title, parent)
    if (v.root / rel).exists():
        raise BrainError(f"{rel.as_posix()} already exists")
    fm = {"id": new_id(), "layer": layer, "type": typ, "summary": a.summary.strip(),
          "up": f"[[{parent.title if parent else 'MAP'}]]", "aliases": a.aliases or [], "status": "seed",
          "source": a.source or None, "external": [], "created": today(), "updated": today(), "reviewed": None}
    if a.verify_intent or a.verify:
        if layer == 1:
            raise BrainError("a verify contract belongs on an L2 or L3 note")
        contract = contract_errors(a.verify_intent, a.verify_keywords or [], a.verify or [])
        if contract:
            raise BrainError("; ".join(contract))
        fm.update(verify_intent=a.verify_intent, verify_keywords=a.verify_keywords or [], verify=a.verify or [])
    if a.system:
        fm["system"] = [x.strip().lower() for x in a.system]
    n = Node(rel, a.title, fm, template_body(v, layer, a.title), [])
    with v.lock():
        save_node(v.root, n, touch=False)
        if parent:
            parent.body = add_to_section(parent.body, CHILD_SECTION[parent.layer], f"- [[{a.title}]] — {a.summary.strip()}")
            save_node(v.root, parent)
    Telemetry(v).record("planted", [n.key], extra={"st": "seed"})
    print(f"created {rel.as_posix()} (L{layer} {typ}, seed)")
    return 0


def cmd_link(v: Vault, a) -> int:
    if a.type not in SYNAPSES:
        raise BrainError(f"invalid synapse type: {', '.join(SYNAPSES)}")
    if not a.reason.strip():
        raise BrainError("--reason is required")
    n, m = v.node(a.source), v.node(a.target)
    if n.k == m.k:
        raise BrainError("a node cannot be connected to itself")
    if any(norm(s.target) == m.k for s in n.synapses):
        raise BrainError(f'"{n.title}" already has a synapse to "{m.title}"')
    if len(n.synapses) >= MAX_SYNAPSES:
        co = Telemetry(v).coactivation()
        weakest = min(n.synapses, key=lambda s: (co.get(pair(n.key, v.net[norm(s.target)].key), 0.0) if norm(s.target) in v.net else -1))
        raise BrainError(f'"{n.title}" already has {MAX_SYNAPSES} synapses. The weakest is "{weakest.target}": '
                         f"remove it with brain.py unlink if the new one matters more")
    n.body = add_to_section(n.body, "Connections", f"- {a.type}:: [[{m.title}]] — {a.reason.strip()}")
    with v.lock():
        save_node(v.root, n)
    print(f"synapse {a.type}: {n.title} → {m.title}")
    return 0


def cmd_unlink(v: Vault, a) -> int:
    n, m = v.node(a.source, archive=True), a.target
    new = remove_from_section(n.body, "Connections", m)
    if new == n.body:
        raise BrainError(f'"{n.title}" has no synapse to "{m}"')
    n.body = new
    with v.lock():
        save_node(v.root, n)
    print(f"synapse removed: {n.title} → {m}")
    return 0


def _move_paths(v: Vault, n: Node, target: Path) -> None:
    src = v.root / n.path
    dest = v.root / target
    if n.layer == 1 and n.path.parent.name == n.title and n.path.parent != Path():
        dest_folder = dest.parent
        if dest_folder.exists() and dest_folder != src.parent:
            raise BrainError(f"folder {target.parent.as_posix()} already exists")
        dest_folder.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src.parent, dest_folder)
        current_name = dest_folder / src.name
        if current_name != dest:
            os.replace(current_name, dest)
        return
    if dest.exists() and dest != src:
        raise BrainError(f"{target.as_posix()} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    details = src.parent / n.title
    if n.layer == 2 and details.is_dir():
        os.replace(details, dest.parent / dest.stem)
    try:
        if not any(src.parent.iterdir()):
            src.parent.rmdir()
    except OSError:
        pass


def cmd_move(v: Vault, a) -> int:
    n = v.node(a.title)
    new = None if norm(a.up) == "map" else v.node(a.up)
    validate_parent(n.layer, new)
    if new and (new.k == n.k or any(c.k == n.k for c in v.chain(new))):
        raise BrainError("a node cannot be moved under itself")
    target = expected_path(n.layer, n.title, new)
    old = v.parent(n)
    with v.lock():
        if old and (not new or old.k != new.k):
            old.body = remove_from_section(old.body, CHILD_SECTION[old.layer], n.title)
            save_node(v.root, old)
        if target != n.path:
            _move_paths(v, n, target)
        n.path = target
        n.fm["up"] = f"[[{new.title if new else 'MAP'}]]"
        save_node(v.root, n)
        if new and (not old or old.k != new.k):
            nv = Vault(v.root).node(new.title)
            nv.body = add_to_section(nv.body, CHILD_SECTION[nv.layer], f"- [[{n.title}]] — {n.summary}")
            save_node(v.root, nv)
    print(f"moved to {target.as_posix()}")
    return 0


def _rename_section(body: str, old: str, new: str) -> str:
    return re.sub(rf"(?m)^##\s+{re.escape(old)}\s*$", f"## {new}", body)


def _remove_empty_folder(path: Path) -> None:
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def cmd_promote(v: Vault, a) -> int:
    """L3 → L2 under its concept's L1 · L2 → L1 subdomain (its details become concepts)."""
    n = v.node(a.title)
    parent = v.parent(n)
    if n.layer == 3:
        grandparent = v.parent(parent) if parent else None
        if not grandparent or grandparent.layer != 1:
            raise BrainError("the parent concept does not hang from an L1")
        target = expected_path(2, n.title, grandparent)
        with v.lock():
            parent.body = remove_from_section(parent.body, CHILD_SECTION[2], n.title)
            save_node(v.root, parent)
            (v.root / target).parent.mkdir(parents=True, exist_ok=True)
            os.replace(v.root / n.path, v.root / target)
            _remove_empty_folder((v.root / n.path).parent)
            n.path = target
            n.fm.update(layer=2, type="concept", up=f"[[{grandparent.title}]]")
            save_node(v.root, n)
            grandparent.body = add_to_section(grandparent.body, CHILD_SECTION[1], f"- [[{n.title}]] — {n.summary}")
            save_node(v.root, grandparent)
        print(f'"{n.title}" promoted to L2 under "{grandparent.title}" → {target.as_posix()}')
        return 0
    if n.layer == 2:
        if not parent or parent.layer != 1:
            raise BrainError("the concept does not hang from an L1")
        target = expected_path(1, n.title, parent)
        children = v.children.get(n.k, [])
        with v.lock():
            (v.root / target).parent.mkdir(parents=True, exist_ok=True)
            os.replace(v.root / n.path, v.root / target)
            n.path = target
            n.fm.update(layer=1, type="domain")
            n.body = _rename_section(n.body, CHILD_SECTION[2], CHILD_SECTION[1])
            save_node(v.root, n)
            for h in children:
                h.fm.update(layer=2, type="concept")
                save_node(v.root, h)
        print(f'"{n.title}" promoted to L1 (subdomain of "{parent.title}") · {len(children)} details become concepts')
        return 0
    raise BrainError("only L2 and L3 nodes can be promoted")


def cmd_demote(v: Vault, a) -> int:
    """L2 → L3 under another concept (only if it has no details)."""
    n = v.node(a.title)
    if n.layer != 2:
        raise BrainError("only an L2 can be demoted to L3")
    if v.children.get(n.k):
        raise BrainError("it has details: move them to another concept before demoting")
    new = v.node(a.up)
    if new.layer != 2 or new.k == n.k:
        raise BrainError("the new parent must be another L2")
    typ = a.type or "fact"
    if typ not in TYPES_BY_LAYER[3]:
        raise BrainError(f"invalid L3 type: {', '.join(sorted(TYPES_BY_LAYER[3]))}")
    target = expected_path(3, n.title, new)
    old = v.parent(n)
    with v.lock():
        if old:
            old.body = remove_from_section(old.body, CHILD_SECTION[old.layer], n.title)
            save_node(v.root, old)
        (v.root / target).parent.mkdir(parents=True, exist_ok=True)
        os.replace(v.root / n.path, v.root / target)
        _remove_empty_folder((v.root / n.path).parent / n.title)
        n.path = target
        n.fm.update(layer=3, type=typ, up=f"[[{new.title}]]")
        save_node(v.root, n)
        new.body = add_to_section(new.body, CHILD_SECTION[2], f"- [[{n.title}]] — {n.summary}")
        save_node(v.root, new)
    print(f'"{n.title}" demoted to L3 ({typ}) under "{new.title}" → {target.as_posix()}')
    return 0


def cmd_rename(v: Vault, a) -> int:
    n = v.node(a.old, archive=True)
    validate_title(v, a.new, except_node=n)
    old_t = n.title
    with v.lock():
        src = v.root / n.path
        if n.layer == 1 and n.path.parent.name == old_t and not n.cold:
            folder = src.parent.with_name(a.new)
            os.replace(src.parent, folder)
            os.replace(folder / src.name, folder / f"{a.new}.md")
        else:
            os.replace(src, src.with_name(f"{a.new}.md"))
            details = src.parent / old_t
            if n.layer == 2 and details.is_dir():
                os.replace(details, details.with_name(a.new))
        changed = 0
        for rel in v.md_files(include_brain=True):
            path = v.root / rel
            if not path.exists() or "generated" in rel.parts:
                continue
            text = path.read_text(encoding="utf-8")
            new = replace_links(text, old_t, a.new)
            if new != text:
                write_atomic(path, new)
                changed += 1
    external = 0
    for name in v.cfg.get("siblings", {}):
        try:
            h = v.sibling(name)
        except BrainError:
            continue
        for rel in h.md_files():
            path = h.root / rel
            text = path.read_text(encoding="utf-8")

            def sub(m):
                ref = parse_uri(m.group(0))
                if ref and ref[0] == v.name and norm(ref[1]) == norm(old_t):
                    return obsidian_uri(v.name, a.new)
                return m.group(0)

            new = URI_RE.sub(sub, text)
            if new != text:
                write_atomic(path, new)
                external += 1
    print(f'renamed "{old_t}" → "{a.new}" · {changed} files with updated links · {external} in sibling vaults')
    return 0


def cmd_status(v: Vault, a) -> int:
    n = v.node(a.title)
    n.fm["status"] = a.status
    if a.status == "consolidated":
        n.fm["reviewed"] = today()
    with v.lock():
        save_node(v.root, n)
    if a.status == "consolidated":
        Telemetry(v).record("validated", [n.key])
    print(f'"{n.title}" → {a.status}')
    return 0


PHASES = ("dreamed", "rested", "measured")


def cmd_activity(v: Vault, a) -> int:
    """A life-cycle phase for the panel's live feed only (no telemetry): REM dreamed these nodes' questions, a nap
    rested the vault, the night measured it. Nodes are given by id, title or alias, one per line, from --nodes."""
    keys = []
    if a.nodes:
        text = sys.stdin.read() if a.nodes == "-" else Path(a.nodes).read_text(encoding="utf-8")
        by_key = v.by_key()
        for line in text.splitlines():
            if line.strip():
                n = by_key.get(line.strip()) or v.net.get(norm(line))
                if n:
                    keys.append(n.key)
    phase = {k: x for k, x in (("note", (a.note or "")[:160] or None), ("alarm", a.alarm or None)) if x}
    with v.lock():
        log_activity(v, a.event, keys, None, phase=phase)
    print(f"activity {a.event}" + (f" · {len(keys)} nodes" if keys else ""))
    return 0


QUESTIONS_MAX = 10  # per language and node
QUESTIONS_PROMPT = """# For each note below, write 5 English and 5 Spanish questions that a person or an agent would type to
# find it and that it specifically answers. Vary the wording (synonyms, everyday words; at most one question reuses
# the title), include one 3-5 word keyword query, one question about a concrete fact (name, number, command, date)
# and one how/why question. Spanish ones are natural Latin American Spanish written on their own, not translations;
# names, commands and identifiers stay as they are. Output JSON Lines, one per note:
# {"id": "<ID as given>", "en": [5 questions], "es": [5 questions]}
# then: brain.py questions import <file.jsonl>"""


def cmd_questions(v: Vault, a) -> int:
    """Generated questions (doc2query): the engine writes none itself. `todo` prints the nodes whose questions are
    missing or stale with the instructions for an LLM, `import` checks and stores its JSON Lines answer."""
    qs = questions(v)
    ranked = [n for n in v.net.values() if n.layer in (1, 2, 3) and STATUS_FACTOR.get(n.status, 0.8)]
    missing = [n for n in ranked if n.key not in qs]
    stale = [n for n in ranked if n.key in qs and qs[n.key].get("hash") != content_hash(n)]
    if a.action == "status":
        print(f"questions: {len(ranked) - len(missing)} of {len(ranked)} nodes · {len(stale)} stale · {len(missing)} missing")
        return 0
    if a.action == "todo":
        batch = (missing + stale)[: max(a.limit, 1)]
        if not batch:
            print("# every node has current questions")
            return 0
        print(QUESTIONS_PROMPT + "\n")
        for n in batch:
            body = re.sub(r"\s+", " ", own_text(n.body)).strip()[:1100]
            print(f"### ID: {n.key}\nTITLE: {n.title}\nSUMMARY: {n.summary}\nTEXT: {body}\n")
        print(f"# {len(batch)} of {len(missing) + len(stale)} nodes to do")
        return 0
    if not a.file:
        raise BrainError("questions import needs a JSON Lines file")
    by_key = {n.key: n for n in v.net.values()}
    allowed = {x.lower() for x in v.cfg.get("allowed_addresses", {})}
    added, problems = 0, []
    for i, line in enumerate(Path(a.file).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"line {i}: not JSON"); continue
        n = by_key.get(str(d.get("id")))
        if n is None:
            problems.append(f"line {i}: unknown id {d.get('id')!r}"); continue
        langs = {lang: [str(q).strip() for q in (d.get(lang) or []) if str(q).strip()][:QUESTIONS_MAX] for lang in ("en", "es")}
        if not langs["en"] and not langs["es"]:
            problems.append(f"line {i}: no questions"); continue
        found = sensitive("\n".join(langs["en"] + langs["es"]), allowed)
        if found:
            problems.append(f"line {i}: looks like a {found}, skipped"); continue
        qs[n.key] = {"key": n.key, "hash": content_hash(n), **langs}
        added += 1
    keep = {n.key for n in v.net.values()}
    lines = [json.dumps(qs[k], ensure_ascii=False) for k in sorted(qs) if k in keep]
    with v.lock():
        write_atomic(v.root / "_brain" / QUESTIONS_FILE, "\n".join(lines) + "\n")
    for p in problems:
        print(f"✖ {p}")
    print(f"questions: {added} nodes imported · {len(lines)} stored in _brain/{QUESTIONS_FILE}")
    return 1 if problems and not added else 0


def cmd_lint(v: Vault, a) -> int:
    if a.fix:
        fixed = 0
        with v.lock():
            for n in v.net.values():
                if not n.has_fm:
                    continue
                before = dict(n.fm)
                n.fm.setdefault("id", None)
                if not n.fm.get("id"):
                    n.fm["id"] = new_id()
                if not n.fm.get("created"):
                    n.fm["created"] = dt.date.fromtimestamp((v.root / n.path).stat().st_mtime).isoformat()
                if not n.fm.get("updated"):
                    n.fm["updated"] = n.fm["created"]
                if not n.fm.get("status"):
                    n.fm["status"] = "seed"
                if n.layer in DEFAULT_TYPE and not n.fm.get("type"):
                    n.fm["type"] = DEFAULT_TYPE[n.layer]
                for key in ("aliases", "external"):
                    if n.fm.get(key) is None:
                        n.fm[key] = []
                if n.fm != before:
                    save_node(v.root, n, touch=False)
                    fixed += 1
        print(f"fixed {fixed} nodes")
        v = Vault(v.root)
    findings = audit(v, warnings=not a.quiet)
    errors = sum(1 for h in findings if h[0] == "ERROR")
    for level, path, msg in findings[:200]:
        print(f"{level:<5}  {path}: {msg}")
    if len(findings) > 200:
        print(f"… {len(findings) - 200} more findings")
    print(f"— {errors} errors · {len(findings) - errors} warnings · {len(v.net)} nodes")
    return 1 if errors else 0


# --------------------------------------------------------------------------- consolidation
def age_days(v: Vault, n: Node) -> int:
    f = parse_date(n.fm.get("created"))
    if not f:
        f = dt.date.fromtimestamp((v.root / n.path).stat().st_mtime)
    return (dt.date.today() - f).days


def compute_proposals(v: Vault) -> dict[str, list[str]]:
    tel = Telemetry(v)
    weights, co, uses = tel.weights(), tel.coactivation(), tel.count("used")
    keys = v.by_key()
    P: dict[str, list[str]] = {k: [] for k in ("synapse", "prune", "archive", "atomize", "subdomain", "promote",
                                                "demote", "merge", "synthesis", "validate", "learned", "inbox")}
    connected = set()
    for n in v.net.values():
        if n.up and norm(n.up) in v.net:
            connected.add(pair(n.key, v.net[norm(n.up)].key))
        for s in n.synapses:
            if norm(s.target) in v.net:
                connected.add(pair(n.key, v.net[norm(s.target)].key))
    for (a, b), val in sorted(co.items(), key=lambda x: -x[1]):
        if val >= HEBB_THRESHOLD and (a, b) not in connected and a in keys and b in keys:
            P["synapse"].append(f"- [ ] synapse :: [[{keys[a].title}]] -> [[{keys[b].title}]] — co-activation {val:.1f}; write the reason with brain.py link")
    vals = sorted(weights.values())
    p90 = vals[int(len(vals) * 0.9)] if vals else 1.0
    for n in sorted(v.net.values(), key=lambda x: x.k):
        if n.layer not in (1, 2, 3):
            continue
        wn, age = weights.get(n.key, 0.0), age_days(v, n)
        children = v.children.get(n.k, [])
        for s in n.synapses:
            m = v.net.get(norm(s.target))
            if m and co.get(pair(n.key, m.key), 0.0) < 0.05 and wn < 0.1 and weights.get(m.key, 0) < 0.1 and age > 90 and age_days(v, m) > 90:
                P["prune"].append(f"- [ ] prune :: [[{n.title}]] -> [[{m.title}]] — no joint use in 90 days")
        if n.status == "seed" and wn < 0.05 and age > 60 and not children:
            P["archive"].append(f"- [ ] archive :: [[{n.title}]] — seed unused for {age} days")
        if over_limit(n):
            P["atomize"].append(f"- [ ] atomize :: [[{n.title}]] — {n.words} words (max {MAX_WORDS[n.layer]})")
        if n.layer == 1 and len([h for h in children if h.layer == 2]) > MAX_L1_CHILDREN:
            P["subdomain"].append(f"- [ ] subdomain :: [[{n.title}]] — {len(children)} branches: group them into subdomains")
        if n.layer == 2 and wn >= p90 and wn > 0.5 and len(children) >= 5:
            P["promote"].append(f"- [ ] promote :: [[{n.title}]] — central L2 with {len(children)} details: candidate for an L1 subdomain")
        if n.layer == 3 and wn >= p90 and wn > 0.5 and uses.get(n.key, 0) >= 5:
            P["promote"].append(f"- [ ] promote :: [[{n.title}]] — L3 used {uses[n.key]} times: candidate for an L2 concept")
        if n.layer == 2 and not children and wn < 0.1 and age > 60:
            P["demote"].append(f"- [ ] demote :: [[{n.title}]] — concept without details and rarely used: is it a detail of another concept?")
        if n.layer == 1:
            desc = [h for h in children] + [d for h in children for d in v.children.get(h.k, [])]
            latest = max((str(d.fm.get("updated") or "") for d in desc), default="")
            if latest and latest > str(n.fm.get("updated") or ""):
                P["synthesis"].append(f"- [ ] synthesis :: [[{n.title}]] — its branches changed ({latest}): rewrite the synthesis from the summaries")
        if n.status == "provisional":
            by, used = n.fm.get("learned_by") or "an agent", len({e.get("s") for e in tel.events() if e.get("e") == "used" and e.get("k") == n.key})
            if any(s.type == "contradicts" for s in n.synapses) or any(t == "contradicts" for t, _ in v.incoming.get(n.k, [])):
                P["learned"].append(f"- [ ] conflict :: [[{n.title}]] — learned by {by}, in a contradiction: decide which node holds")
            elif used == 0 and age > 30:
                P["archive"].append(f"- [ ] archive :: [[{n.title}]] — provisional by {by}, never used in {age} days")
            else:
                P["learned"].append(f"- [ ] pending :: [[{n.title}]] — provisional by {by}, helped in {used} sessions")
        if n.fm.get("confirmed") and not n.fm.get("reviewed"):
            P["learned"].append(f"- [ ] sample :: [[{n.title}]] — confirmed by use on {n.fm['confirmed']}: spot-check it")
        if n.status == "seed" and not n.fm.get("reviewed") and age > 30:
            P["validate"].append(f"- [ ] validate :: [[{n.title}]] — seed {age} days old without human review")
    nodes = [n for n in v.net.values() if n.layer in (1, 2, 3)]
    sigs = {n.k: tokens(n.title) | tokens(n.summary) for n in nodes}
    for i, n in enumerate(nodes):
        for m in nodes[i + 1:]:
            if n.layer != m.layer:
                continue
            fa, fb = sigs[n.k], sigs[m.k]
            if fa and fb and len(fa & fb) / len(fa | fb) >= 0.75:
                P["merge"].append(f"- [ ] merge :: [[{n.title}]] -> [[{m.title}]] — nearly identical titles and summaries")
    for rel in v.cold["_inbox"]:
        P["inbox"].append(f"- [ ] process :: [[{rel.stem}]] — capture waiting to be atomized")
    return P


PROPOSAL_TITLES = {
    "synapse": "Hebbian synapses (need a reason: brain.py link)",
    "prune": "Prune synapses (mechanical: --apply)",
    "archive": "Archive (mechanical: --apply)",
    "atomize": "Atomize large nodes (/atomize)",
    "subdomain": "Split domains into subdomains",
    "promote": "Promote a layer up",
    "demote": "Demote a layer down",
    "merge": "Possible duplicates (merge)",
    "synthesis": "Outdated L1 syntheses (bottom-up)",
    "validate": "Seeds awaiting your validation (brain.py status … consolidated)",
    "learned": "Agent learnings (provisional, conflicts, confirmed ones to sample)",
    "inbox": "Pending inbox",
}


def cmd_consolidate(v: Vault, a) -> int:
    path = v.root / "_brain" / "proposals.md"
    if a.apply:
        if not path.exists():
            raise BrainError("there are no proposals: run brain.py consolidate first")
        applied = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^- \[[xX]\] (archive|prune) :: \[\[(.+?)\]\](?: -> \[\[(.+?)\]\])?", line)
            if not m:
                continue
            v = Vault(v.root)
            action, t1, t2 = m.groups()
            if action == "prune":
                n = v.node(t1)
                n.body = remove_from_section(n.body, "Connections", t2)
                with v.lock():
                    save_node(v.root, n)
                print(f"pruned {t1} → {t2}")
            else:
                n = v.node(t1)
                if v.children.get(n.k):
                    print(f"skipped {t1}: it has children")
                    continue
                parent = v.parent(n)
                with v.lock():
                    if parent:
                        parent.body = remove_from_section(parent.body, CHILD_SECTION[parent.layer], n.title)
                        save_node(v.root, parent)
                    n.fm["archived"] = today()
                    save_node(v.root, n)
                    target = v.root / "_archive" / n.path.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(v.root / n.path, target)
                print(f"archived {t1}")
            applied += 1
        print(f"— {applied} proposals applied")
        v = Vault(v.root)
    P = compute_proposals(v)
    total = sum(len(x) for x in P.values())
    parts = ["# Consolidation proposals", "",
             f"%% Generated by brain.py consolidate on {now().isoformat(' ')}. Mark [x] what you approve. "
             "`brain.py consolidate --apply` runs the mechanical ones (archive, prune); the agent does the rest with /consolidate. %%", "",
             f"{total} proposals · vault {v.name}", ""]
    for key, lines in P.items():
        if lines:
            parts += [f"## {PROPOSAL_TITLES[key]}", *lines[:40], ""]
    write_atomic(path, "\n".join(parts))
    print(f"{total} proposals in _brain/proposals.md · " + " · ".join(f"{k} {len(x)}" for k, x in P.items() if x))
    return 0


# --------------------------------------------------------------------------- external references
def cmd_external(v: Vault, a) -> int:
    n = v.node(a.title)
    h = v.sibling(a.in_vault)
    target = h.node(a.note)
    uri = obsidian_uri(h.name, target.title)
    refs = n.external
    if uri in refs:
        raise BrainError("that external reference already exists")
    n.fm["external"] = refs + [uri]
    with v.lock():
        save_node(v.root, n)
    print(f"external reference added: {n.title} → {h.name}::{target.title}")
    return 0


def cmd_externals(v: Vault, a) -> int:
    cache: dict[str, Vault | None] = {}

    def vault_of(name: str) -> Vault | None:
        if name not in cache:
            try:
                cache[name] = v.sibling(name)
            except BrainError:
                cache[name] = None
        return cache[name]

    removed, total = 0, 0
    for n in sorted(v.net.values(), key=lambda x: x.k):
        keep = []
        for uri in n.external:
            total += 1
            ref = parse_uri(uri)
            h = vault_of(ref[0]) if ref else None
            ok = bool(ref and h and norm(ref[1]) in h.net)
            if not a.clean:
                print(f"{'ok    ' if ok else 'BROKEN'} {n.title} → {ref[0] + '::' + ref[1] if ref else uri}")
            if ok or not a.clean:
                keep.append(uri)
            else:
                removed += 1
        if a.clean and len(keep) != len(n.external):
            n.fm["external"] = keep
            save_node(v.root, n)
    if a.clean:
        missing = [k for k, r in v.cfg.get("siblings", {}).items() if not (v.root / r).resolve().is_dir()]
        for k in missing:
            v.cfg["siblings"].pop(k)
            if k in v.cfg.get("warn_dependency_on", []):
                v.cfg["warn_dependency_on"].remove(k)
        if missing:
            v.save_cfg()
        print(f"cleanup: {removed} broken references removed · siblings removed: {', '.join(missing) or 'none'}")
    else:
        print(f"— {total} external references")
    return 0


# --------------------------------------------------------------------------- visualization
def layout(v: Vault, weights: dict, max_l3: int = 150):
    net = v.net
    nodes = [n for n in net.values() if n.layer in (1, 2, 3)]
    roots = sorted([n for n in nodes if not (n.up and norm(n.up) in net)], key=lambda n: (n.layer, n.k))
    leaves: dict[str, int] = {}

    def count(n: Node, path: set) -> int:
        if n.k in leaves:
            return leaves[n.k]
        if n.k in path:
            return 1
        path = path | {n.k}
        leaves[n.k] = max(1, sum(count(h, path) for h in v.children.get(n.k, [])))
        return leaves[n.k]

    for r in roots:
        count(r, set())
    angle: dict[str, float] = {}

    def assign(group: list[Node], a0: float, a1: float) -> None:
        total = sum(leaves.get(n.k, 1) for n in group) or 1
        a = a0
        for n in group:
            if n.k in angle:
                continue
            span = (a1 - a0) * leaves.get(n.k, 1) / total
            angle[n.k] = a + span / 2
            assign(v.children.get(n.k, []), a, a + span)
            a += span

    assign(roots, -math.pi / 2, 1.5 * math.pi)
    for i, n in enumerate(x for x in nodes if x.k not in angle):
        angle[n.k] = -math.pi / 2 + 0.05 * (i + 1)
    l3 = sorted((n for n in nodes if n.layer == 3), key=lambda n: (-weights.get(n.key, 0.0), n.k))
    visible = {n.k for n in nodes if n.layer != 3} | {n.k for n in l3[:max_l3]}
    counts = {c: sum(1 for n in nodes if n.layer == c) for c in (1, 2, 3)}
    r1 = max(700, counts[1] * 320 / (2 * math.pi))
    r2 = max(r1 + 800, counts[2] * 260 / (2 * math.pi))
    r3 = max(r2 + 700, min(counts[3], max_l3) * 220 / (2 * math.pi))
    radii = {"L1": r1, "SUB": (r1 + r2) / 2, "L2": r2, "L3": r3, "COLD": r3 + 700}

    def radius(n: Node) -> float:
        if n.layer == 1:
            return radii["SUB"] if n.up and norm(n.up) in net else radii["L1"]
        return radii["L2"] if n.layer == 2 else radii["L3"]

    pos = {n.k: (radius(n) * math.cos(angle[n.k]), radius(n) * math.sin(angle[n.k])) for n in nodes if n.k in visible}
    return pos, radii, visible


# same layer colors as Panel.html and the Obsidian graph groups
COLORS = {0: "#EF9F27", 1: "#2a78d6", 2: "#eb6834", 3: "#1baf7a", "synapse": "#898781", "contradicts": "#d03b3b"}


def render_panel_svg(v: Vault, weights: dict) -> str:
    pos, radii, _ = layout(v, weights, max_l3=400)
    scale = 270 / radii["COLD"]
    cx = cy = 300
    p = ['<svg xmlns="http://www.w3.org/2000/svg" width="600" height="640" viewBox="0 0 600 640" font-family="sans-serif">']
    for key in ("L1", "L2", "L3", "COLD"):
        dash = ' stroke-dasharray="4 6"' if key == "COLD" else ""
        p.append(f'<circle cx="{cx}" cy="{cy}" r="{radii[key] * scale:.1f}" fill="none" stroke="#888780" stroke-opacity="0.5" stroke-width="1"{dash}/>')
    for n in v.net.values():
        if n.k not in pos:
            continue
        x, y = pos[n.k]
        parent = v.parent(n)
        if parent and parent.k in pos:
            px, py = pos[parent.k]
        elif n.layer == 1:
            px = py = 0
        else:
            continue
        p.append(f'<line x1="{cx + x * scale:.1f}" y1="{cy + y * scale:.1f}" x2="{cx + px * scale:.1f}" y2="{cy + py * scale:.1f}" stroke="#888780" stroke-opacity="0.35" stroke-width="0.8"/>')
    for n in v.net.values():
        for s in n.synapses:
            m = v.net.get(norm(s.target))
            if n.k in pos and m and m.k in pos:
                (x1, y1), (x2, y2) = pos[n.k], pos[m.k]
                color = COLORS["contradicts" if s.type == "contradicts" else "synapse"]
                p.append(f'<line x1="{cx + x1 * scale:.1f}" y1="{cy + y1 * scale:.1f}" x2="{cx + x2 * scale:.1f}" y2="{cy + y2 * scale:.1f}" stroke="{color}" stroke-opacity="0.7" stroke-width="1.2"/>')
    p.append(f'<circle cx="{cx}" cy="{cy}" r="12" fill="{COLORS[0]}"/>')
    base = {1: 6, 2: 4, 3: 2.5}
    for n in sorted(v.net.values(), key=lambda x: -(x.layer or 0)):
        if n.k not in pos:
            continue
        x, y = pos[n.k]
        r = base[n.layer] * (1 + 1.2 * weights.get(n.key, 0.0))
        border = ' fill-opacity="0.25" stroke="#898781" stroke-width="1.2"' if n.status == "seed" else ""
        p.append(f'<circle cx="{cx + x * scale:.1f}" cy="{cy + y * scale:.1f}" r="{r:.1f}" fill="{COLORS[n.layer]}"{border}/>')
    counts = {c: sum(1 for n in v.net.values() if n.layer == c) for c in (1, 2, 3)}
    cold = sum(len(x) for x in v.cold.values())
    p.append(f'<text x="300" y="618" text-anchor="middle" font-size="14" fill="#888780">L1 {counts[1]} · L2 {counts[2]} · L3 {counts[3]} · cold {cold} · faded with gray ring = seed</text>')
    p.append("</svg>")
    return "\n".join(p) + "\n"


def hot_nodes(v: Vault, tel: Telemetry) -> list[Node]:
    """The HOT_MAX most active concepts and details above HOT_MIN, hottest first."""
    act, by_key = tel.activation(), v.by_key()
    hot = [(b, by_key[k]) for k, b in act.items() if b >= HOT_MIN and k in by_key]
    hot = [(b, n) for b, n in hot if n.layer in (2, 3) and n.status not in ("obsolete", "provisional")]
    return [n for _, n in sorted(hot, key=lambda x: (-x[0], x[1].k))[:HOT_MAX]]


def record_hot(v: Vault, hot: list[Node]) -> None:
    """Appends the hot set to state/hot.jsonl when it changed, so hot_stats can tell what MAP.md held at any time."""
    path, keys = v.state_dir / "hot.jsonl", [n.key for n in hot]
    last = None
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        last = json.loads(lines[-1])["keys"] if lines else None
    if keys != last:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"t": now().isoformat(), "keys": keys}) + "\n")


def hot_stats(v: Vault, tel: Telemetry) -> tuple[str, int, int, int] | None:
    """Since hot memory started: (since, reads, reads of a hot node, credits of a hot node its session never read).
    The last count is the questions answered from MAP.md with no recall or read; the second, those that could have been."""
    path = v.state_dir / "hot.jsonl"
    if not path.exists():
        return None
    sets = [(parse_instant(x["t"]), set(x["keys"])) for x in map(json.loads, path.read_text(encoding="utf-8").splitlines())]
    sets = [(t, ks) for t, ks in sets if t]
    if not sets:
        return None

    def hot_at(t):
        cur = None
        for ts, ks in sets:  # strictly before: the set a credit itself produced was not on show when it was made
            if ts >= t:
                break
            cur = ks
        return cur or set()

    read_in = {(e.get("s"), e["k"]) for e in tel.events() if e.get("e") == "read"}
    reads = hot_reads = zero = 0
    for e in tel.events():
        if e["_t"] < sets[0][0]:
            continue
        if e.get("e") == "read":
            reads += 1
            hot_reads += e["k"] in hot_at(e["_t"])
        elif e.get("e") == "used" and e["k"] in hot_at(e["_t"]) and (e.get("s"), e["k"]) not in read_in:
            zero += 1
    return sets[0][0].date().isoformat(), reads, hot_reads, zero


HOT_HOW = ("When a summary here answers the question, answer from it with no recall, then "
           "`brain.py credit --used \"<title>\"`; otherwise recall.")


def render_hot(v: Vault, hot: list[Node]) -> str:
    """_brain/generated/hot.md: the Hot list alone, for an agent that loads it at the start of a conversation
    (an agent's instruction file or first-turn hook) without the rest of MAP.md."""
    out = [f"# Hot memory: {v.name}", "", "The nodes of your cortex needed most lately. " + HOT_HOW, ""]
    out += [f"- [[{n.title}]] — {n.summary}" for n in hot] or ["- (none yet: recall when your cortex may know)"]
    return "\n".join(out) + "\n"


def write_hot(v: Vault, tel: Telemetry) -> list[Node]:
    """Recomputes the hot set, writes hot.md when it changed and records it for hot_stats. build and credit call it,
    so what an agent loads tracks its use without a build."""
    hot = hot_nodes(v, tel)
    text, path = render_hot(v, hot), v.gen_dir / "hot.md"
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        write_atomic(path, text)
    record_hot(v, hot)
    return hot


def render_map(v: Vault, weights: dict, hot: list[Node] | None = None) -> str:
    l1 = [n for n in v.net.values() if n.layer == 1 and (not n.up or norm(n.up) == "map")]
    l1.sort(key=lambda n: (-weights.get(n.key, 0.0), n.k))
    counts = {c: sum(1 for n in v.net.values() if n.layer == c) for c in (1, 2, 3)}
    out = ["# MAP", "", "%% L0 core. Generated by brain.py build — do not edit by hand. %%", "",
           f"Vault **{v.name}** · {counts[1]} domains · {counts[2]} concepts · {counts[3]} details · updated {today()}",
           "", "## Domains"]
    lines = 0
    for n in l1:
        if lines >= 32:
            out.append(f"- … {len(l1) - l1.index(n)} more domains (see _brain/generated/index.tsv)")
            break
        branches = [h for h in v.children.get(n.k, [])]
        out.append(f"- [[{n.title}]] — {n.summary} · {len(branches)} branches")
        lines += 1
        subs = [h for h in branches if h.layer == 1]
        for s in subs[:3]:
            out.append(f"    - [[{s.title}]] — {s.summary}")
            lines += 1
        if len(subs) > 3:
            out.append(f"    - … +{len(subs) - 3} subdomains")
    if hot:
        out += ["", "## Hot", "What was needed most lately. " + HOT_HOW]
        out += [f"- [[{n.title}]] — {n.summary}" for n in hot]
    out += ["", "## How to read", "Use `brain.py recall \"<query>\"`; go down from L1 to L2 to L3 only along the relevant branch."]
    return "\n".join(out) + "\n"


def render_index(v: Vault, weights: dict) -> str:
    def clean(s):
        return re.sub(r"[\t\r\n]+", " ", str(s or ""))

    rows = ["layer\ttitle\tsummary\taliases\tup\tstatus\tweight\tpath\tid\ttokens"]
    for n in sorted(v.net.values(), key=lambda n: (n.layer or 9, -weights.get(n.key, 0.0), n.k)):
        rows.append("\t".join(clean(x) for x in (f"L{n.layer}", n.title, n.summary, ", ".join(n.aliases), n.up,
                                                 n.status, f"{weights.get(n.key, 0.0):.3f}", n.path.as_posix(), n.fm.get("id"), n.tokens)))
    return "\n".join(rows) + "\n"


def render_graph(v: Vault, weights: dict) -> str:
    nodes = [{"id": n.key, "title": n.title, "layer": n.layer, "type": n.type, "status": n.status, "up": n.up,
              "path": n.path.as_posix(), "weight": round(weights.get(n.key, 0.0), 4), "words": n.words,
              "tokens": n.tokens}
             for n in sorted(v.net.values(), key=lambda x: x.k)]
    edges = []
    for n in v.net.values():
        if n.up:
            edges.append({"from": n.title, "to": n.up, "type": "up"})
        for s in n.synapses:
            edges.append({"from": n.title, "to": s.target, "type": s.type, "reason": s.reason})
    return json.dumps({"version": VERSION, "vault": v.name, "nodes": nodes, "edges": edges,
                       "cold": {k: len(x) for k, x in v.cold.items()}}, ensure_ascii=False, indent=1) + "\n"


def record_history(v: Vault) -> list[dict]:
    path = v.state_dir / "history.csv"
    head = ["date", "l1", "l2", "l3", "seeds", "consolidated", "obsolete", "synapses", "cold"]
    rows = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) == len(head):
                rows.append(dict(zip(head, parts)))
    net = [n for n in v.net.values() if n.layer in (1, 2, 3)]
    current = {"date": today(), "l1": sum(n.layer == 1 for n in net), "l2": sum(n.layer == 2 for n in net),
               "l3": sum(n.layer == 3 for n in net), "seeds": sum(n.status == "seed" for n in net),
               "consolidated": sum(n.status == "consolidated" for n in net), "obsolete": sum(n.status == "obsolete" for n in net),
               "synapses": sum(len(n.synapses) for n in net), "cold": sum(len(x) for x in v.cold.values())}
    rows = [f for f in rows if f["date"] != today()] + [{k: str(x) for k, x in current.items()}]
    write_atomic(path, ",".join(head) + "\n" + "\n".join(",".join(f[c] for c in head) for f in rows) + "\n")
    return rows


def render_panel(v: Vault, weights: dict, history: list[dict], findings: list, co: dict, commits: list) -> str:
    keys = v.by_key()
    net = [n for n in v.net.values() if n.layer in (1, 2, 3)]
    out = [f"# {v.name} panel", "", f"%% Generated by brain.py build on {now().isoformat(' ')} — do not edit. %%", "",
           f"Vault **{v.name}** · engine v{VERSION} · interactive panel with 2D orbit and 3D sphere views: [[_brain/Panel.html|Panel.html]] "
           "(open it in a browser, or inside Obsidian with an HTML viewer plugin)", "",
           "![[_brain/generated/orbit.svg]]", "", "## Layers", "",
           "| Layer | Nodes | Seeds | Consolidated | Obsolete |", "|---|---|---|---|---|"]
    names = {1: "L1 Domains", 2: "L2 Concepts", 3: "L3 Details"}
    for c in (1, 2, 3):
        g = [n for n in net if n.layer == c]
        out.append(f"| {names[c]} | {len(g)} | {sum(n.status == 'seed' for n in g)} | "
                   f"{sum(n.status == 'consolidated' for n in g)} | {sum(n.status == 'obsolete' for n in g)} |")
    cold = {k: len(x) for k, x in v.cold.items()}
    out.append(f"| Cold ring | {sum(cold.values())} | — | — | — |")
    data = [(names[c], sum(n.layer == c for n in net)) for c in (1, 2, 3)]
    if any(x for _, x in data):
        out += ["", "```mermaid", "pie showData", "    title Nodes by layer"]
        out += [f'    "{e}" : {x}' for e, x in data if x]
        out += ["```"]
    out += ["", "## Growth (last 8 weeks)", "", "| Week | L1 | L2 | L3 | Total | Δ |", "|---|---|---|---|---|---|"]
    weeks: dict[str, dict] = {}
    for f in history:
        date = parse_date(f["date"])
        if date:
            iso = date.isocalendar()
            weeks[f"{iso[0]}-W{iso[1]:02d}"] = f
    prev = None
    rows = []
    for wk, f in sorted(weeks.items())[-9:]:
        total = int(f["l1"]) + int(f["l2"]) + int(f["l3"])
        if prev is not None:
            rows.append(f"| {wk} | {f['l1']} | {f['l2']} | {f['l3']} | {total} | {total - prev:+d} |")
        elif len(weeks) == 1:
            rows.append(f"| {wk} | {f['l1']} | {f['l2']} | {f['l3']} | {total} | — |")
        prev = total
    out += rows[-8:]
    errors = [h for h in findings if h[0] == "ERROR"]
    warns = [h for h in findings if h[0] == "WARN"]

    def count(text):
        return sum(1 for h in warns if text in h[2])

    stale = [n for n in net if n.status == "seed" and not n.fm.get("reviewed") and age_days(v, n) > 30]
    out += ["", "## Health", "",
            f"- Lint errors: **{len(errors)}** · warnings: {len(warns)}",
            f"- Orphans: {count('orphan')} · links without target: {count('link without target')} · misplaced: {count('expected location')}",
            f"- Oversized nodes: {count('words (max')} · not in English: {count('not_english')}",
            f"- Seeds > 30 days without review: {len(stale)}",
            f"- Nodes at the cap of {MAX_SYNAPSES} synapses: {sum(len(n.synapses) >= MAX_SYNAPSES for n in net)}",
            f"- Pending inbox: {cold['_inbox']}"]
    for level, path, msg in errors[:10]:
        out.append(f"    - ERROR `{path}`: {msg}")
    out += ["", "## Top 10 by weight", "", "| # | Node | Layer | Weight |", "|---|---|---|---|"]
    for i, n in enumerate(sorted(net, key=lambda n: (-weights.get(n.key, 0.0), n.k))[:10], 1):
        out.append(f"| {i} | [[{n.title}]] | L{n.layer} | {weights.get(n.key, 0.0):.2f} |")
    strongest = sorted(((val, a, b) for (a, b), val in co.items() if a in keys and b in keys), reverse=True)[:5]
    out += ["", "## Strongest co-activations", ""]
    out += [f"- [[{keys[a].title}]] ↔ [[{keys[b].title}]] · {val:.1f}" for val, a, b in strongest] or ["- (no usage data yet)"]
    out += ["", "## Latest learnings", ""]
    out += [f"- `{h}` {d} · {s}" for h, d, s in commits] or ["- (no commits yet)"]
    ext = [(n, parse_uri(u)) for n in net for u in n.external]
    out += ["", "## External references", ""]
    out += [f"- [[{n.title}]] → {r[0]}::{r[1]}" for n, r in ext if r] or ["- (none)"]
    out += ["", "## Cold ring", "", "| Folder | Files |", "|---|---|"]
    out += [f"| {k} | {x} |" for k, x in cold.items()]
    return "\n".join(out) + "\n"


PANEL_TEMPLATE = Path(__file__).resolve().parent / "panel.html"


def recent_commits(v: Vault, limit: int = 10) -> list[tuple[str, str, str]]:
    try:
        import subprocess
        r = subprocess.run(["git", "-C", str(v.root), "log", f"-{limit}", "--date=short", "--pretty=format:%h\t%ad\t%s"],
                           capture_output=True, text=True, encoding="utf-8", timeout=10)
        return [tuple(l.split("\t")) for l in r.stdout.splitlines() if l.count("\t") == 2] if r.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        return []


def render_panel_html(v: Vault, weights: dict, history: list[dict], findings: list, co: dict, commits: list) -> str:
    """Interactive panel, one page with both views (2D orbit and 3D sphere): panel.html with this vault's data inlined (works offline)."""
    net = sorted((n for n in v.net.values() if n.layer in (1, 2, 3)), key=lambda x: x.k)
    keys = v.by_key()
    warns = [h for h in findings if h[0] == "WARN"]

    def count(text):
        return sum(1 for h in warns if text in h[2])

    def number(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return 0

    cold = {k: len(x) for k, x in v.cold.items()}
    planted = Telemetry(v).planted()
    strongest = sorted(((val, a, b) for (a, b), val in co.items() if a in keys and b in keys), reverse=True)[:5]
    data = {
        "vault": v.name, "version": VERSION, "generated": now().isoformat(" ", "seconds"),
        "siblings": sorted(v.cfg.get("siblings", {})),
        "nodes": [{"id": n.fm.get("id"), "title": n.title, "layer": n.layer, "type": n.type, "status": n.status,
                   "weight": round(weights.get(n.key, 0.0), 3), "summary": n.summary, "up": n.up,
                   "path": n.path.with_suffix("").as_posix(), "updated": str(n.fm.get("updated") or ""),
                   "tokens": n.tokens,
                   "aliases": n.aliases, "external": n.external,
                   **({"planted": planted[n.key]["t"][:19]} if n.key in planted else {})} for n in net],
        "synapses": [{"source": n.title, "target": s.target, "type": s.type, "reason": s.reason}
                     for n in net for s in n.synapses],
        "history": [{k: (x if k == "date" else number(x)) for k, x in f.items()} for f in history[-180:]],
        "health": {"errors": sum(h[0] == "ERROR" for h in findings), "warnings": len(warns),
                   "orphans": count("orphan"), "broken": count("link without target"),
                   "misplaced": count("expected location"), "oversized": count("words (max"),
                   "not_english": count("not_english"),
                   "stale_seeds": sum(n.status == "seed" and not n.fm.get("reviewed") and age_days(v, n) > 30 for n in net),
                   "full_synapses": sum(len(n.synapses) >= MAX_SYNAPSES for n in net), "inbox": cold.get("_inbox", 0)},
        "findings": [{"level": "error" if level == "ERROR" else "warning", "node": path, "message": msg}
                     for level, path, msg in findings[:40]],
        "coactivations": [{"a": keys[a].title, "b": keys[b].title, "score": round(val, 2)} for val, a, b in strongest],
        "commits": [{"hash": h, "date": d, "subject": s} for h, d, s in commits],
        "cold": cold,
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return PANEL_TEMPLATE.read_text(encoding="utf-8").replace("__BRAIN_DATA__", payload, 1)


def cmd_precommit(v: Vault, a) -> int:
    """Lint (blocking) + build on a single vault load: run by the Git hook."""
    findings = audit(v)
    errors = [h for h in findings if h[0] == "ERROR"]
    if errors:
        for level, path, msg in errors[:50]:
            print(f"{level:<5}  {path}: {msg}")
        print(f"— {len(errors)} errors: commit blocked (fix them and try again)")
        return 1
    a.quiet = True
    return cmd_build(v, a, findings)


def cmd_build(v: Vault, a, findings: list | None = None) -> int:
    tel = Telemetry(v)
    weights = tel.weights()
    if findings is None:
        findings = audit(v)
    with v.lock():
        history = record_history(v)
        co = tel.coactivation()
        commits = recent_commits(v)
        write_atomic(v.gen_dir / "index.tsv", render_index(v, weights))
        write_atomic(v.gen_dir / "graph.json", render_graph(v, weights))
        write_atomic(v.gen_dir / "orbit.svg", render_panel_svg(v, weights))
        write_atomic(v.root / "_brain" / "Panel.md", render_panel(v, weights, history, findings, co, commits))
        write_atomic(v.root / "_brain" / "Panel.html", render_panel_html(v, weights, history, findings, co, commits))
        hot = write_hot(v, tel)
        write_atomic(v.root / "MAP.md", render_map(v, weights, hot))
    Sessions(v).clean()
    if not a.quiet:
        errors = sum(1 for h in findings if h[0] == "ERROR")
        print(f"build ok · {len(v.net)} nodes · {errors} lint errors · MAP, Panel.md, Panel.html, index and graph regenerated")
    return 0


# --------------------------------------------------------------------------- privacy: a vault is never public
# A cortex holds what its agent learned about a person, atomized and linked: published, the graph exposes far more
# than any single note. The pre-push hook refuses any remote that a public forge serves to anyone without a login.
PUBLIC_FORGES = {
    "github.com": "https://api.github.com/repos/{path}",
    "gitlab.com": "https://gitlab.com/api/v4/projects/{quoted}",
    "codeberg.org": "https://codeberg.org/api/v1/repos/{path}",
    "bitbucket.org": "https://api.bitbucket.org/2.0/repositories/{path}",
}


def parse_remote(url: str) -> tuple[str, str] | None:
    """(host, owner/name) of a git remote URL on any host, or None for a local path."""
    url = url.strip()
    m = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", url, re.I)
    if not m or url.startswith(("/", ".", "file:")) or re.match(r"^[A-Za-z]:[\\/]", url):
        return None
    return m.group(1).lower(), m.group(2).strip("/")


def remote_visibility(url: str, fetch=None) -> str:
    """public, private, unknown (a forge we could not ask) or self-hosted (no public forge)."""
    parsed = parse_remote(url)
    if not parsed:
        return "self-hosted"
    host, path = parsed
    if host not in PUBLIC_FORGES:
        return "self-hosted"
    import urllib.parse
    api = PUBLIC_FORGES[host].format(path=path, quoted=urllib.parse.quote(path, safe=""))
    if fetch is None:
        import urllib.error
        import urllib.request

        def fetch(u):
            req = urllib.request.Request(u, method="GET", headers={"User-Agent": "cortex-privacy-guard"})
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
    try:
        status = fetch(api)
    except Exception:
        return "unknown"
    # asked without credentials: 200 means anyone can read it; 404 (or 401) means only its members can
    return "public" if status == 200 else "private" if status in (401, 404) else "unknown"


def cmd_prepush(v: Vault, a) -> int:
    """Run by the pre-push hook: refuses to push the vault to a public repository."""
    unverified_ok = os.environ.get("CORTEX_ALLOW_UNVERIFIED_PUSH") == "1"
    seen = remote_visibility(a.url)
    if seen == "public":
        print(f"✖ push refused: {a.url} is public. A cortex is never published: its graph exposes personal "
              "information its agents learned. Make the repository private, then push.", file=sys.stderr)
        return 1
    if seen == "unknown" and not unverified_ok:
        print(f"✖ push refused: could not confirm that {a.url} is private (no answer from the forge). Try again online, "
              "or set CORTEX_ALLOW_UNVERIFIED_PUSH=1 for this push once you have checked it yourself.", file=sys.stderr)
        return 1
    if seen == "self-hosted":
        print(f"privacy: {a.url} is not on a public forge; keep it private (a cortex is never published)")
    return 0


# --------------------------------------------------------------------------- doctor
# --------------------------------------------------------------------------- memory import
# Claude Code keeps a memory folder per project: MEMORY.md (an index of "- [Title](file.md) — hook" lines) and one
# note per memory with frontmatter name, description and metadata.type (user, feedback, project, reference).
MEMORY_DOMAINS = {
    "user": ("Owner profile", "Who the owner is: role, expertise and preferences."),
    "feedback": ("Working agreements", "How the owner wants the work done: rules, corrections and confirmed approaches."),
    "project": ("Projects", "Ongoing work, goals and constraints that the code alone does not show."),
    "reference": ("References", "Where things live outside the vault: links, dashboards and tickets."),
}
OTHER_DOMAIN = ("Imported notes", "Notes imported from another memory that fit no other domain.")
MEMORY_INDEX = re.compile(r"^\s*-\s*\[([^\]]+)\]\(([^)]+\.md)\)\s*(?:[—–-]\s*(.*))?$")


def read_memory(path: Path) -> tuple[dict, str]:
    """A memory note's frontmatter (flat: name, description and type, wherever type is nested) and its body."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    meta, body = {}, text
    if text.startswith("---\n") and "\n---" in text[4:]:
        end = text.index("\n---", 4)
        for line in text[4:end].splitlines():
            k, sep, val = line.strip().partition(":")
            if sep and val.strip() and k in ("name", "description", "type"):
                meta.setdefault(k, val.strip().strip('"').strip("'").replace('\\"', '"'))
        body = text[end + 4:].split("\n", 1)[1] if "\n" in text[end + 4:] else ""
    return meta, body.strip()


def memory_label(folder: str) -> str:
    """A project's short name from Claude Code's folder name (its path with dashes for separators): the part after
    the last of Users/<name>, Dev, Documents, OfficialRepos, Repos, Projects or src."""
    parts = [x for x in re.split(r"[-_]+", folder) if x]
    cut = 0
    for i, x in enumerate(parts):
        if x.lower() == "users":
            cut = i + 2
        elif x.lower() in ("dev", "documents", "officialrepos", "repos", "projects", "src"):
            cut = i + 1
    return " ".join(parts[cut:]) or folder


def memory_title(raw: str, taken: set, label: str) -> str:
    """A memory's title as a valid, unique node title: forbidden characters become dashes, then the project label
    tells two memories of the same name apart."""
    t = re.sub(r"\s+", " ", "".join(" -" if ch == ":" else "-" if ch in FORBIDDEN else ch for ch in raw)).strip(" -")
    t = t or "Memory"
    t = t[:MAX_TITLE].strip()
    if norm(t) in ("map", "claude", "panel", "proposals"):
        t += " (memory)"
    base, i = t, 1
    while norm(t) in taken:
        i += 1
        t = f"{base[:MAX_TITLE - 20]} ({label})" if i == 2 else f"{base[:MAX_TITLE - 20]} ({label} {i})"
    return t


def cmd_import_memory(v: Vault, a) -> int:
    """Claude Code's memory folders into the graph, by rule and with no model: one L1 domain per memory type (Owner
    profile, Working agreements, Projects, References), one L2 concept per memory with the index's title, the note's
    description as summary and its body under Idea, and [[links]] between memories turned into links between the
    imported nodes. A memory with something that looks like a credential or an address is skipped; one over the word
    limit is captured in _inbox for the curator. Each node remembers where it came from (source), so a second import
    of the same folder adds only what is new. --commit: one commit of what the import added."""
    import subprocess
    root = v.root
    by = (a.by or "owner").strip()
    allowed = {x.lower() for x in v.cfg.get("allowed_addresses", {})}
    plans = []                                           # (folder, label, file, index title, hook)
    for folder in a.folders:
        f = Path(folder)
        if f.name != "memory" and (f / "memory").is_dir():
            f = f / "memory"
        if not f.is_dir():
            raise BrainError(f"no memory folder at {folder}")
        label = a.label or memory_label(f.parent.name)
        listed = {}
        if (f / "MEMORY.md").exists():
            for line in (f / "MEMORY.md").read_text(encoding="utf-8").splitlines():
                m = MEMORY_INDEX.match(line)
                if m and (f / m.group(2)).is_file():
                    listed[m.group(2)] = (m.group(1).strip(), (m.group(3) or "").strip())
        for note in sorted(f.glob("*.md")):
            if note.name != "MEMORY.md":
                title, hook = listed.get(note.name, ("", ""))
                plans.append((f, label, note, title, hook))
    before_paths = None
    if a.commit and (root / ".git").exists():
        r = subprocess.run(["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        before_paths = {x[3:] for x in r.stdout.splitlines()}
    have = {str(n.fm.get("source") or "") for n in v.net.values()}
    for cap in (root / "_inbox").glob("*.md") if (root / "_inbox").is_dir() else []:   # earlier captures count too
        m = re.search(r"^source: (claude-code memory .+)$", cap.read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            have.add(m.group(1).strip())
    taken = set(v.titles) | {norm(d) for d, _ in (*MEMORY_DOMAINS.values(), OTHER_DOMAIN)}   # the domains' own names
    chosen = []                                          # (source, title, domain, summary, body, type)
    out = {"imported": 0, "skipped": 0, "inbox": 0, "known": 0}
    slug_title = {}
    for f, label, note, idx_title, hook in plans:
        source = f"claude-code memory {f.parent.name}/{note.name}"
        meta, body = read_memory(note)
        if source in have:
            out["known"] += 1
            continue
        found = sensitive("\n".join((meta.get("description", ""), hook, body)), allowed)
        if found:
            print(f"skipped {note.name}: possible {found} (allow an address in config allowed_addresses to import it)")
            out["skipped"] += 1
            continue
        title = memory_title(idx_title or meta.get("name", "").replace("-", " ").strip().capitalize() or note.stem,
                             taken, label)
        taken.add(norm(title))
        summary = re.sub(r"\s+", " ", (meta.get("description") or hook or title).replace("[[", "").replace("]]", ""))
        if len(summary) > MAX_SUMMARY:
            summary = summary[:MAX_SUMMARY - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
        if len(re.findall(r"\w+", body)) <= MAX_WORDS[2]:   # only what lands in the graph can be linked to
            slug_title[note.stem] = title
            if meta.get("name"):
                slug_title[meta["name"]] = title
        chosen.append((source, title, MEMORY_DOMAINS.get(meta.get("type", ""), OTHER_DOMAIN), summary, body, label))

    def links(text: str) -> str:   # [[slug]] between memories -> [[Title]] of the imported node; anything else: plain
        return re.sub(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]",
                      lambda m: f"[[{slug_title[m.group(1).strip()]}]]" if m.group(1).strip() in slug_title
                      else m.group(1).strip(), text)

    for source, title, (domain, dsum), summary, body, label in chosen:
        body = links(body)
        if len(re.findall(r"\w+", body)) > MAX_WORDS[2]:
            rel = Path("_inbox") / f"{today()} import {title}.md"
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(f"---\ncaptured: {today()}\nsource: {source}\n---\n\n# {title}\n\n{body}\n",
                                    encoding="utf-8")
            print(f"captured {rel.as_posix()}: over {MAX_WORDS[2]} words, for the curator to atomize")
            out["inbox"] += 1
            continue
        vv = Vault(root)
        if not vv.exists(domain):
            cmd_new(vv, argparse.Namespace(layer=1, type=None, title=domain, summary=dsum, up=None, aliases=[],
                                           source=None, verify_intent=None, verify=None, verify_keywords=None,
                                           system=None))
            vv = Vault(root)
        elif vv.node(domain).layer != 1:
            raise BrainError(f'"{domain}" exists and is not an L1: rename it, then import again')
        cmd_new(vv, argparse.Namespace(layer=2, type=None, title=title, summary=summary, up=domain, aliases=[],
                                       source=source, verify_intent=None, verify=None, verify_keywords=None,
                                       system=None))
        vv = Vault(root)
        n = vv.node(title)
        placeholder = "## Idea\n%% The idea in 2 to 5 sentences. %%"
        n.body = n.body.replace(placeholder, f"## Idea\n{body}", 1) if placeholder in n.body else \
            n.body.replace(f"# {title}\n", f"# {title}\n\n{body}\n", 1) if f"# {title}\n" in n.body else \
            f"\n{body}\n" + n.body
        n.fm["learned_by"] = by
        save_node(root, n, touch=False)
        out["imported"] += 1
    print(f"imported {out['imported']} memories · {out['known']} already here · {out['inbox']} to _inbox · "
          f"{out['skipped']} skipped")
    if before_paths is not None and (out["imported"] or out["inbox"]):
        r = subprocess.run(["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        mine = sorted({x[3:] for x in r.stdout.splitlines()} - before_paths)
        staged = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--name-only"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
        if staged:
            print("not committed: something else is staged in this vault")
        elif mine:
            subprocess.run(["git", "-C", str(root), "add", "--", *[p.strip('"') for p in mine]],
                           capture_output=True)
            labels = ", ".join(sorted({c[5] for c in chosen})) or "memory"
            r = subprocess.run(["git", "-C", str(root), "commit", "-q", "-m",
                                f"import: {out['imported'] + out['inbox']} memories from Claude Code ({labels})"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            print("committed" if r.returncode == 0 else f"not committed: {(r.stderr or r.stdout).strip()[-300:]}")
    return 0


# --------------------------------------------------------------------------- migrations
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def load_migrations(folder: Path | None = None) -> list:
    """The migrations this engine ships (migrations/m0001_*.py ...), in order. Each has ID, TITLE, plan(root) and
    up(root): see migrations/m0001_baseline.py for the contract."""
    import importlib.util
    out = []
    for f in sorted((folder or MIGRATIONS_DIR).glob("m[0-9][0-9][0-9][0-9]_*.py")):
        spec = importlib.util.spec_from_file_location(f"cortex_migration_{f.stem}", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out.append(mod)
    ids = [m.ID for m in out]
    if ids != sorted(set(ids)):
        raise BrainError(f"migration ids are not unique and in order: {ids}")
    return out


def vault_schema(v: Vault) -> int:
    """The vault's schema: what its data has been migrated to. A vault from before migrations has none: 0."""
    try:
        return int(v.cfg.get("schema") or 0)
    except (TypeError, ValueError):
        return 0


def engine_schema(folder: Path | None = None) -> int:
    return max((m.ID for m in load_migrations(folder)), default=0)


def set_schema(root: Path, n: int) -> None:
    path = root / "_brain" / "config.json"
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg["schema"] = n
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def cmd_migrate(v: Vault, a) -> int:
    """Brings the vault's data up to this engine's schema, without a model: the pending migrations in order, each
    under the vault lock, checked by lint (no new errors), undone on any failure, and with --commit one commit each
    (`migration: 0001 ...`, by whoever the environment names: the Cortex hub when it runs them). A migration whose
    files hold uncommitted changes waits; a vault on a newer schema than this engine is left alone."""
    import subprocess
    root = v.root
    migs = load_migrations(Path(a.dir) if a.dir else None)
    cur, top = vault_schema(v), max((m.ID for m in migs), default=0)
    if cur > top:
        raise BrainError(f"schema {cur} is newer than this engine knows ({top}): upgrade the engine; nothing changed")
    pending = [m for m in migs if m.ID > cur]
    if not pending:
        print(f"schema {cur}: up to date")
        return 0

    def git(*args):
        try:
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=120)
        except (OSError, subprocess.SubprocessError):
            return None

    in_git = (root / ".git").exists()
    for m in pending:
        label = f"{m.ID:04d} {m.TITLE}"
        paths = sorted(set(m.plan(root)) | {"_brain/config.json"})
        if a.dry_run:
            print(f"would apply {label}: {', '.join(paths)}")
            continue
        if in_git:
            r = git("status", "--porcelain=v1", "--", *paths)
            dirty = [line[3:] for line in (r.stdout if r else "").splitlines() if line[:2] != "??"]
            if dirty:
                print(f"✖ {label} waits: uncommitted changes in {', '.join(dirty)}: commit or discard them, then migrate again")
                return 1
        before = {p: ((root / p).read_bytes() if (root / p).exists() else None) for p in paths}
        errors_before = sum(1 for h in audit(Vault(root), warnings=False) if h[0] == "ERROR")
        try:
            with v.lock():
                m.up(root)
                set_schema(root, m.ID)
            errors = sum(1 for h in audit(Vault(root), warnings=False) if h[0] == "ERROR")
            if errors > errors_before:
                raise BrainError(f"lint found {errors - errors_before} new errors after it")
        except Exception as e:   # noqa: BLE001 — whatever went wrong, the vault goes back to how it was
            for p, data in before.items():
                if data is None:
                    (root / p).unlink(missing_ok=True)
                else:
                    (root / p).write_bytes(data)
            print(f"✖ {label} failed and was undone: {e}")
            return 1
        print(f"applied {label}")
        if a.commit and in_git:
            staged = git("diff", "--cached", "--name-only")
            if staged and staged.stdout.strip():
                print(f"  not committed: something else is staged in this vault ({staged.stdout.split()[0]} ...)")
                continue
            git("add", "--", *paths)
            r = git("commit", "-q", "-m", f"migration: {label}")
            print("  committed" if r and r.returncode == 0 else
                  f"  not committed: {((r.stderr or r.stdout) if r else 'git not found').strip()[-300:]}")
    if not a.dry_run:
        print(f"schema {vault_schema(Vault(root))}: up to date")
    return 0


def cmd_doctor(v: Vault, a) -> int:
    failures = 0

    def check(ok: bool, text: str) -> None:
        nonlocal failures
        print(("✔ " if ok else "✖ ") + text)
        failures += 0 if ok else 1

    check(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}")

    def git(*args):
        try:
            import subprocess
            r = subprocess.run(["git", "-C", str(v.root), *args], capture_output=True, text=True, encoding="utf-8", timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    check(git("rev-parse", "--is-inside-work-tree") == "true", "Git repository")
    check(bool(git("config", "user.email")), f"Git identity: {git('config', 'user.name')} <{git('config', 'user.email')}>")
    check(git("config", "core.hooksPath") == ".githooks", "core.hooksPath = .githooks")
    check((v.root / ".githooks" / "pre-commit").exists(), "pre-commit hook present")
    check((v.root / ".githooks" / "pre-push").exists(), "pre-push hook present (a cortex is never pushed to a public repository)")
    for line in (git("remote", "-v") or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "(push)":
            seen = remote_visibility(parts[1])
            if seen != "unknown":
                check(seen != "public", f"remote {parts[0]} is {seen} (a cortex is never public)")
    check(v.cfg_path.exists(), "_brain/config.json")
    have, want = vault_schema(v), engine_schema()
    check(have == want, f"vault schema {have} (this engine's: {want})" +
          ("" if have == want else ": run `brain.py migrate`" if have < want else ": the engine is older than the vault"))
    check((v.root / "CLAUDE.md").exists(), "CLAUDE.md (L0)")
    check((v.root / ".obsidian").is_dir(), "Obsidian configuration")
    here = Path(__file__).resolve().parent
    check(PANEL_TEMPLATE.exists(), "panel template _brain/scripts/panel.html")
    for name, rel in v.cfg.get("siblings", {}).items():
        other = (v.root / rel / "_brain" / "scripts").resolve()
        if (other / "brain.py").exists():
            # siblings must run the same engine version; byte differences within one version only warn
            src = other / "brain_core.py" if (other / "brain_core.py").exists() else other / "brain.py"
            m = re.search(r'^VERSION = "([^"]+)"', src.read_text(encoding="utf-8"), re.M)
            theirs = m.group(1) if m else "?"
            check(theirs == VERSION, f'same engine version in "{name}" (v{theirs})')
            files = ("brain.py", "brain_core.py", "panel.html")
            if theirs == VERSION and not all((other / f).exists() and (other / f).read_bytes() == (here / f).read_bytes()
                                             for f in files):
                print(f'! "{name}" runs v{VERSION} with different files: reinstall the engine there')
        else:
            check(False, f'sibling vault "{name}" not found at {other.parent.parent}')
    st = hot_stats(v, Telemetry(v))
    if st:
        since, reads, hot_reads, zero = st
        print(f"· hot memory since {since}: {zero} answers from the Hot list with no read · {hot_reads} of {reads} reads "
              "were of a node the Hot list already held")
    print(f"— engine v{VERSION} · {len(v.net)} nodes · {failures} failures")
    return 1 if failures else 0


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="brain.py", description="Engine of the orbital second brain")
    ap.add_argument("--vault", help="vault root (default: the script's own vault)")
    ap.add_argument("--version", action="version", version=f"brain.py {VERSION}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("session", help="creates or inspects a read session (budgets)")
    p.add_argument("action", choices=["new", "status", "close"])
    p.add_argument("--mode", choices=list(MODES), default="normal")
    p.add_argument("--session", help="status, close: default the agent's session (--by), else the automatic one")
    p.add_argument("--by", help="the agent: new opens its task session, which its calls without --session use")
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("stats", help="token sizes and cumulative tokens read")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("recall", help="read funnel: signatures by relevance × weight")
    p.add_argument("query")
    p.add_argument("--mode", choices=list(MODES))
    p.add_argument("--session")
    p.add_argument("--by", help="the agent: without --session, its task session or its own fallback session")
    p.add_argument("--all", action="store_true", help="include the sibling vaults")
    p.add_argument("--no-body", action="store_true", help="signatures only, even when one answer is clear")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("read", help="reads a node's body within the budget")
    p.add_argument("title")
    p.add_argument("--session", help="default: the agent's session (--by), else the automatic one")
    p.add_argument("--by", help="the agent: without --session, its task session or its own fallback session")
    p.add_argument("--cold", help="reason to read a cold-ring note")
    p.add_argument("--full", action="store_true", help="the whole body, not only the part matching the last recall")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("credit", help="credits the nodes that were useful (weight and co-activation)")
    # extend, not store: agents repeat the flag (--used A --used B), and store kept only the last title
    p.add_argument("--used", nargs="+", action="extend", required=True)
    p.add_argument("--session")
    p.add_argument("--by", help="the agent: without --session, its task session or its own fallback session")
    p.set_defaults(func=cmd_credit)

    p = sub.add_parser("new", help="creates a node from its template in its right place")
    p.add_argument("--layer", type=int, choices=[1, 2, 3], required=True)
    p.add_argument("--type")
    p.add_argument("--up")
    p.add_argument("--title", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--aliases", nargs="*")
    p.add_argument("--source")
    p.add_argument("--system", nargs="*", help="the systems the note holds on, such as ubuntu or windows (default: all)")
    p.add_argument("--verify-intent", choices=VERIFY_INTENTS, help="what kind of claim renewal will check")
    p.add_argument("--verify-keywords", nargs="*", help="3 to 6 words a search for this note would use")
    p.add_argument("--verify", action="append", metavar="CHECK", help="one read-only check (repeatable): "
                   "key|nokey <a.b> in <file> [= value] · path|nopath <path> · grep <regex> in <file> · "
                   "url <http://localhost..> <field> = value · version <prog> contains <text> · service <unit> active")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("learn", help="an agent's own learning: one provisional L3, checked before it is written")
    p.add_argument("--up", required=True, help="the L2 concept it belongs under")
    p.add_argument("--title", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--type", choices=sorted(TYPES_BY_LAYER[3]))
    p.add_argument("--content", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--truth", choices=["current", "historical"], default="current")
    p.add_argument("--contradicts", metavar="TITLE", help="an existing node this learning corrects")
    p.add_argument("--by", help="the learning agent (default: config learner)")
    p.add_argument("--system", nargs="*", help="the systems the note holds on, such as ubuntu or windows (default: all)")
    p.add_argument("--verify-intent", choices=VERIFY_INTENTS, help="what kind of claim renewal will check")
    p.add_argument("--verify-keywords", nargs="*", help="3 to 6 words a search for this note would use")
    p.add_argument("--verify", action="append", metavar="CHECK", help="one read-only check (repeatable): "
                   "key|nokey <a.b> in <file> [= value] · path|nopath <path> · grep <regex> in <file> · "
                   "url <http://localhost..> <field> = value · version <prog> contains <text> · service <unit> active")
    p.set_defaults(func=cmd_learn)

    p = sub.add_parser("link", help="adds a typed synapse with a reason")
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("--type", required=True, choices=list(SYNAPSES))
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", help="removes a synapse")
    p.add_argument("source")
    p.add_argument("target")
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser("move", help="moves a node under another parent (folder and up)")
    p.add_argument("title")
    p.add_argument("--up", required=True)
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("promote", help="moves a node up a layer (L3→L2, L2→L1 subdomain)")
    p.add_argument("title")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("demote", help="moves an L2 without details down to L3 under another concept")
    p.add_argument("title")
    p.add_argument("--up", required=True)
    p.add_argument("--type")
    p.set_defaults(func=cmd_demote)

    p = sub.add_parser("rename", help="renames a node and updates every link")
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("status", help="changes the status (consolidated records your validation)")
    p.add_argument("title")
    p.add_argument("status", choices=list(STATUS_FACTOR))
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("activity", help="a life-cycle phase for the panel's live feed (dreamed, rested, measured)")
    p.add_argument("event", choices=PHASES)
    p.add_argument("--nodes", help="a file (or - for stdin) with one node id, title or alias per line")
    p.add_argument("--note", help="a short line for the panel's ticker")
    p.add_argument("--alarm", action="store_true", help="measured: the bench fell")
    p.set_defaults(func=cmd_activity)

    p = sub.add_parser("questions", help="generated questions per node (doc2query): status, todo batch, import")
    p.add_argument("action", choices=["status", "todo", "import"])
    p.add_argument("file", nargs="?", help="import: a JSON Lines file of {id, en, es}")
    p.add_argument("--limit", type=int, default=100, help="todo: nodes per batch")
    p.set_defaults(func=cmd_questions)

    p = sub.add_parser("lint", help="validates the vault (blocks commits with errors)")
    p.add_argument("--fix", action="store_true", help="fills in id, dates and empty fields")
    p.add_argument("--quiet", action="store_true", help="errors only")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("build", help="regenerates index, graph, MAP, Panel.md and Panel.html")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("precommit", help="blocking lint + build (used by the Git hook)")
    p.set_defaults(func=cmd_precommit)

    p = sub.add_parser("prepush", help="refuses a push to a public repository (used by the Git hook)")
    p.add_argument("remote")
    p.add_argument("url")
    p.set_defaults(func=cmd_prepush)

    p = sub.add_parser("consolidate", help="sleep-cycle proposals / applies the marked mechanical ones")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_consolidate)

    p = sub.add_parser("external", help="adds a reference to a note in a sibling vault")
    p.add_argument("action", choices=["add"])
    p.add_argument("title")
    p.add_argument("--in", dest="in_vault", required=True, metavar="VAULT", help="name of the sibling vault")
    p.add_argument("--note", required=True)
    p.set_defaults(func=cmd_external)

    p = sub.add_parser("externals", help="lists or cleans references to sibling vaults")
    p.add_argument("--clean", action="store_true")
    p.set_defaults(func=cmd_externals)

    p = sub.add_parser("import-memory", help="Claude Code memory folders into the graph, by rule (no model involved)")
    p.add_argument("folders", nargs="+", help="memory folders (~/.claude/projects/<project>/memory) or their parents")
    p.add_argument("--label", help="the project's short name, to tell two memories of the same title apart")
    p.add_argument("--by", help="who the memories come from (default: the owner)")
    p.add_argument("--commit", action="store_true", help="one git commit of what the import added")
    p.set_defaults(func=cmd_import_memory)

    p = sub.add_parser("migrate", help="brings the vault's data up to this engine's schema (no model involved)")
    p.add_argument("--dry-run", action="store_true", help="say what would change, change nothing")
    p.add_argument("--commit", action="store_true", help="one git commit per migration")
    p.add_argument("--dir", help=argparse.SUPPRESS)   # tests: another migrations folder
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("doctor", help="checks environment, Git, hooks and engine")
    p.set_defaults(func=cmd_doctor)
    return ap


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    a = build_parser().parse_args(argv)
    root = Path(a.vault) if a.vault else Path(__file__).resolve().parents[2]
    try:
        return a.func(Vault(root), a) or 0
    except BrainError as e:
        print(f"✖ {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
