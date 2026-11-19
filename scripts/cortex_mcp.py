#!/usr/bin/env python3
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""The Cortex engine as an MCP server: standard library only, no SDK. Two ways to run it:

    python3 scripts/cortex_mcp.py --http 127.0.0.1:8777 [--vaults ~/Vaults]      one service for every agent
    python3 scripts/cortex_mcp.py --vault ~/vaults/my-cortex [--by myagent]       stdio, one vault per process

The service answers MCP's streamable HTTP (JSON replies, no event stream) at /mcp/<vault>, where <vault> is the
folder's first word in lower case (claude, hermes) or its full name; ?by=<agent> names who authors the learnings and
?role=reader hides the curator tool. It listens on localhost only and refuses a browser Origin that is not local.
One gate per vault is shared by every agent, so two agents on one vault queue instead of racing its bookkeeping.

Tools. `cortex`, the funnel: recall · read · credit · stats · learn · session, and the combined actions brief (the Hot nodes, the budget, and with a query an ask), ask (recall
plus the reads it points at), finish (credit plus learnings), health (stats, session, lint, broken references) and
batch (up to 12 calls in order). vault=<sibling> runs a read on a sibling vault. `cortex_curate`, every other verb
of the vault's own engine, read from its parser at start (a new engine verb appears with no change here), plus
create (new and its links). status consolidated needs --allow-consolidate: only the vault's owner consolidates.
Each call runs that vault's own `_brain/scripts/brain.py` and hands back its stdout as the answer, so the engine's
formatting and its exit codes stay the contract: 0 the text is the answer · 1 the arguments were wrong (the engine's
✖ line comes back as a tool error, so the model fixes them and retries) · 2 the read budget or the provisional
limit is spent (stop reading and answer with what you have).

Sessions (engine 2.16+): brief opens the agent's task session (session new --by <agent>) and returns its id; the
agent's calls use it with or without session=<id>, and finish credits, stores, closes it and, when the server was
started to (the Cortex hub), commits what the task learned under the agent's own name. A call with no task session
open uses the agent's fallback session, closed after the vault's auto_idle_minutes. Older engines keep the single
automatic session per vault. Only learn writes; the engine takes the vault's own lock and waits about 10 seconds
for it, because a curator or a consolidation timer can be writing at the same time.

Every read call also touches the automatic session and the telemetry, which the vault lock does not cover, so this
process runs one engine call at a time: a second call waits for the first instead of racing it. Two agents reading
the same vault from two processes still spend the same budget, and the last of them to finish is the one whose
bookkeeping survives. Running the engine fresh costs about 0.04 s and 17 MB per call (timed twice on Alastor,
2026-11-03), which is why nothing here keeps a long-lived engine around.

Test: python3 -m unittest scripts/test_cortex_mcp.py
"""
from __future__ import annotations

import argparse
import hmac
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

PROTOCOL = "2025-11-25"            # what initialize answers with when a client asks for a revision we do not speak
# echo a revision only when we speak it: promising one we cannot read invites requests we cannot answer
KNOWN = {"2024-11-05", "2025-03-26", "2025-06-18", PROTOCOL}   # the revisions with an initialize handshake
MODERN = "2026-07-28"              # no handshake: every request names its revision (_meta and MCP-Protocol-Version)
SUPPORTED = sorted(KNOWN) + [MODERN]
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"
SKILLS_EXT = "io.modelcontextprotocol/skills"
SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"   # the curator skills, served over MCP (Skills extension)
LIST_TTL_MS = 60_000
ENGINE_MARK = "✖"                  # what brain.py puts in front of every error it raises itself (main's handler)
ACTIONS = ("recall", "read", "credit", "stats", "learn", "session")
COMBINED = ("brief", "ask", "finish", "health", "batch")   # several engine calls in one round trip
SIBLING_OK = {"recall", "read", "credit", "stats", "session", "ask", "health", "brief"}   # what may target a sibling vault
MAX_STEPS = 12
MAX_TEXT = 20_000                  # a client caps its own results anyway; say so instead of silently cutting


class BadCall(Exception):
    """The call is wrong before the engine is even asked: bad action, missing argument, no vault."""


def describe() -> str:
    """What the model reads. The funnel's four steps live here, so a small model does not have to remember them."""
    return ("Cortex vault: graph memory, read through its budgeted funnel. "
            "action=recall(query): signatures by relevance and weight, and the body itself under '# answer:' when "
            "one node clearly answers. action=read(title): one node body by exact title, at most two per question. "
            "action=credit(used=[titles]): name the titles that helped; it is bookkeeping, do not mention it. "
            "action=stats: the vault's size and the tokens read from it so far. action=learn: store one verified "
            "fact you just learned (English title and summary; a current one needs verify_intent, verify_keywords "
            "and, when it can be rechecked, verify lines - or truth=historical). "
            "isError means fix the arguments, or - when it says budget or limit - stop reading and answer with what "
            "you already have. Anything else is the engine's own failure: retry once. "
            "A task starts with action=brief, which opens its session and gives its id, and ends with action=finish, "
            "which closes it: pass session=<id> on every call when you run tasks in parallel. Cite what you used as [[Title]]. "
            "Never store secrets, credentials or personal data. "
            "Combined actions save round trips: action=brief(query optional) at the start of a task gives the Hot "
            "nodes and the budget left, and with a query answers it like ask; action=ask(query, reads 0-2 or titles) is recall plus the reads it "
            "points at; action=finish(used, learnings=[learn objects]) credits and stores at the end of a task; "
            "action=health is stats, session, lint and broken sibling references at once; action=batch(steps) runs "
            "up to 12 calls in order (a step with verb and args is a cortex_curate call) and stops at the first "
            "failure. action=session shows the budget left. vault=<sibling name> runs a read on a sibling vault.")


def text(a: dict, key: str) -> str:
    v = a.get(key)
    if not isinstance(v, str) or not v.strip():
        raise BadCall(f"{a.get('action')} needs {key} as a non-empty string")
    return v


def words(a: dict, key: str) -> list[str]:
    v = a.get(key)
    if not isinstance(v, list) or not v:
        raise BadCall(f"{a.get('action')} needs {key} as a list of strings")
    out = []
    for x in v:
        if not isinstance(x, str) or not x.strip():
            raise BadCall(f"{a.get('action')} needs {key} as a list of strings")
        out.append(x)
    return out


# What each action takes, and what to tell the model about it. The schema and the argument check both come from
# this table, so they cannot drift apart: anything not listed here is refused before the engine is asked.
D = {"all": "also search the sibling vaults",
     "no_body": "signatures only, even when one answer looks clear",
     "full": "the whole body, not only the lines that match the last recall",
     "cold": "why you are reading a note in the cold ring",
     "by": "the short name of the agent authoring this learning",
     "system": "the systems it holds on, such as ubuntu or windows (leave out when it holds on any system)",
     "verify_keywords": "3 to 6 words a search for this note would use",
     "verify": "one read-only check a script could rerun without a model, such as 'path <p>', 'key a.b in <file> "
               "= <value>' or 'grep <regex> in <file>' (up to five, none when nothing can be checked)"}
D["session"] = "this task's session id, from brief (needed only when you run tasks in parallel)"
ARGS = {"recall": {"query": "what to look for, in English", "all": D["all"], "no_body": D["no_body"],
                   "mode": "the session's reading mode, when the engine offers more than normal",
                   "session": D["session"]},
        "read": {"title": "the exact title of the node", "full": D["full"], "cold": D["cold"], "session": D["session"]},
        "credit": {"used": "the titles that helped", "session": D["session"]},
        "stats": {},
        "session": {"session": D["session"]},
        "learn": {"up": "the L2 concept it belongs under", "title": "a short English title",
                  "summary": "one English line saying what is true", "content": "what is true",
                  "evidence": "how you know: a file, a command, a date",
                  "type": "fact, procedure, error, decision or example",
                  "truth": "current (verify anything measurable before stating it) or historical (a dated record)",
                  "contradicts": "the title of a node this learning corrects",
                  "verify_intent": "what a script will recheck later: config, file, service, endpoint, version, "
                                   "code, rule or behavior",
                  "system": D["system"], "verify_keywords": D["verify_keywords"], "verify": D["verify"],
                  "by": D["by"]}}
ARGS["ask"] = {"query": ARGS["recall"]["query"], "all": D["all"], "session": D["session"],
               "reads": "how many top signatures to read when recall gives no clear answer (0-2, default 1)",
               "titles": "exact titles to read after the recall, instead of the top signatures (at most 2)"}
ARGS["brief"] = {"query": "optional: the task's question, answered in the same call as an ask",
                 "all": D["all"], "reads": "with query: how many top signatures to read (0-2, default 1)"}
ARGS["finish"] = {"used": "the titles that helped (credited first)", "session": D["session"],
                  "learnings": "learn calls to run after the credit: each one an object with learn's fields"}
ARGS["health"] = {}
ARGS["batch"] = {"steps": "up to 12 calls run in order, each an object with its own action (or verb and args for "
                          "cortex_curate's verbs); it stops at the first one that fails"}
for _a in SIBLING_OK:
    ARGS[_a]["vault"] = "a sibling vault's name, such as Agents Cortex, to read there instead (reads only)"
LISTS = {"used", "system", "verify_keywords", "verify", "titles"}
INTS = {"reads"}
OBJS = {"learnings", "steps"}        # arrays of strings, not single strings
BOOLS = {"all", "no_body", "full"}                              # flags, not text
SHARED = {"vault": "a sibling vault's name, such as Agents Cortex, to read there instead (reads only)",
          "used": "the titles that helped",
          "query": "what to look for, in English",
          "all": D["all"],
          "title": "read: the exact title of the node. learn: a short English title for the node it creates"}


def argv_for(a: dict, by: str | None, sessions: bool = False) -> list[str]:
    """The engine's own command line for a tool call: one argument per title, query or line, never a shell.
    sessions: the vault's engine has task sessions (2.16+), so reads name their agent and may name a session."""
    act = a.get("action")
    if act not in ACTIONS:
        raise BadCall(f"action must be one of: {', '.join(ACTIONS)}")
    for key in a:
        if key not in ("action", "vault") and key not in ARGS[act]:
            raise BadCall(f"{act} does not take {key}")
    if act == "recall":
        cmd = ["recall", text(a, "query")]
        if a.get("all"):
            cmd.append("--all")
        if a.get("no_body"):
            cmd.append("--no-body")
        if a.get("mode"):
            cmd += ["--mode", text(a, "mode")]
    elif act == "read":
        cmd = ["read", text(a, "title")]
        if a.get("full"):
            cmd.append("--full")
        if a.get("cold"):
            cmd += ["--cold", text(a, "cold")]
    elif act == "credit":
        cmd = ["credit", "--used", *words(a, "used")]
    elif act == "stats":
        cmd = ["stats"]
    elif act == "session":
        cmd = ["session", "status"]
    if act in ("recall", "read", "credit", "session") and sessions:
        if a.get("session"):
            cmd += ["--session", text(a, "session")]
        if by:
            cmd += ["--by", by]
        return cmd
    if act in ("recall", "read", "credit", "session"):
        if a.get("session"):
            raise BadCall("this vault's engine predates task sessions: leave session out")
        return cmd
    if act == "learn":
        cmd = ["learn", "--up", text(a, "up"), "--title", text(a, "title"), "--summary", text(a, "summary"),
               "--content", text(a, "content"), "--evidence", text(a, "evidence")]
        for key, flag in (("type", "--type"), ("truth", "--truth"), ("contradicts", "--contradicts"),
                          ("verify_intent", "--verify-intent")):
            if a.get(key):
                cmd += [flag, text(a, key)]
        if a.get("system"):
            cmd += ["--system", *words(a, "system")]
        if a.get("verify_keywords"):
            cmd += ["--verify-keywords", *words(a, "verify_keywords")]
        for line in a.get("verify") or []:
            if not isinstance(line, str) or not line.strip():
                raise BadCall("learn's verify takes read-only checks as strings")
            cmd += ["--verify", line]
        if a.get("by") or by:
            cmd += ["--by", a.get("by") or by]
    return cmd



# ---- every other verb of the vault's own engine, read from its parser: a new verb appears with no change here
SPEC_CODE = r"""
import argparse, json, sys
sys.path.insert(0, sys.argv[1])
import brain_core as bc
p = bc.build_parser()
sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
helps = {c.dest: c.help for c in sub._choices_actions}
out = {}
for verb, sp in sub.choices.items():
    args = []
    for a in sp._actions:
        if a.dest in ("help", "session", "vault"):
            continue
        if isinstance(a, argparse._StoreTrueAction):
            kind = "bool"
        elif a.nargs in ("*", "+") or isinstance(a, argparse._AppendAction):
            kind = "list"
        elif a.type is int:
            kind = "int"
        else:
            kind = "str"
        args.append({"name": a.dest, "flag": a.option_strings[-1] if a.option_strings else None, "kind": kind,
                     "choices": [str(c) for c in a.choices] if a.choices else None,
                     "required": bool(a.required) if a.option_strings else a.nargs != "?",
                     "append": isinstance(a, argparse._AppendAction), "help": a.help or ""})
    out[verb] = {"help": helps.get(verb, ""), "args": args}
print(json.dumps(out))
"""
READER = set(ACTIONS) | {"session"}     # the funnel's verbs live on the cortex tool, never on cortex_curate
NOT_HERE = {"precommit"}                # the Git hook's verb: a model has no reason to call it
GUARDED = {("status", "consolidated")}  # only the owner marks a node consolidated: needs --allow-consolidate


def engine_spec(brain: Path) -> dict:
    """{verb: {help, args}} as this vault's engine defines them; {} when its parser cannot be read that way."""
    try:
        r = subprocess.run([sys.executable, "-I", "-c", SPEC_CODE, str(brain.parent)], capture_output=True,
                           text=True, timeout=30, encoding="utf-8")
        spec = json.loads(r.stdout) if r.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        spec = {}
    return {v: s for v, s in spec.items() if v not in READER | NOT_HERE}


def signature(verb: str, s: dict) -> str:
    parts = []
    for a in s["args"]:
        t = "|".join(a["choices"]) if a["choices"] else {"bool": "true", "list": "[..]", "int": "n"}.get(a["kind"], "..")
        parts.append(f"{a['name']}={t}" + ("" if a["required"] else "?"))
    return f"{verb}({', '.join(parts)}): {s['help']}"


def curate_argv(spec: dict, verb, args, allow_consolidate: bool = False) -> list[str]:
    """The engine's command line for a curator verb, checked against the engine's own parser spec."""
    if verb not in spec:
        raise BadCall(f"verb must be one of: {', '.join(sorted(spec))}")
    if not isinstance(args, dict):
        raise BadCall("args must be an object")
    known = {a["name"]: a for a in spec[verb]["args"]}
    for k in args:
        if k not in known:
            raise BadCall(f"{verb} does not take {k}; it takes: {', '.join(known) or 'nothing'}")
    pos, opt = [], []
    for name, a in known.items():
        v = args.get(name)
        if v is None or v is False or v == [] or v == "":
            if a["required"]:
                raise BadCall(f"{verb} needs {name}")
            continue
        if a["kind"] == "list" and not isinstance(v, list):
            v = [v]
        elif a["kind"] != "list" and isinstance(v, list):
            raise BadCall(f"{verb}'s {name} takes one value, not a list")
        vals = v if isinstance(v, list) else [v]
        for x in vals:
            if (isinstance(x, bool) and a["kind"] != "bool") or not isinstance(x, (str, int, bool)):
                raise BadCall(f"{verb}'s {name} takes {a['kind']}, not {type(x).__name__}")
            if a["choices"] and str(x) not in a["choices"]:
                raise BadCall(f"{verb}'s {name} must be one of: {', '.join(a['choices'])}")
        if a["flag"] is None:
            pos += [str(x) for x in vals]
        elif a["kind"] == "bool":
            opt.append(a["flag"])
        elif a["append"]:
            for x in vals:
                opt += [a["flag"], str(x)]
        else:
            opt += [a["flag"], *[str(x) for x in vals]]
    for name, value in GUARDED:
        if verb == name and value in pos and not allow_consolidate:
            raise BadCall(f"only the vault's owner marks a node {value}; this server was not started with "
                          "--allow-consolidate")
    if verb == "activity" and args.get("nodes") == "-":
        raise BadCall("activity's nodes takes a file path here, not stdin")
    return [verb, *pos, *opt]

def cap(t: str) -> str:
    return t if len(t) <= MAX_TEXT else t[:MAX_TEXT] + f"\n… cut at {MAX_TEXT} characters: ask for less."


def crash(err: str) -> str:
    """An error the engine did not raise itself: the model needs the last line, not six lines of traceback."""
    lines = [x for x in err.splitlines() if x.strip()]
    return f"error: the engine crashed ({lines[-1].strip() if lines else 'no output'}). " \
           "That is not your arguments: retry once, then answer with what you already have."


TRACEBACK = "Traceback (most recent call last)"


def run_engine(brain: Path, vault: Path, args: list[str], timeout: float) -> tuple[bool, str]:
    """-> (ok, text). The engine's stdout is the answer; its exit code says who was wrong.

    brain.py prefixes with ✖ every error it raises itself and returns that error's code: 1 wrong arguments, 2 a
    budget or a limit spent. argparse exits 2 too, on a call it cannot parse, and argparse never prints the ✖ -
    so the mark is what separates "you asked wrong, fix it" from "the vault is spent, stop reading".

    With a resident engine (RESIDENT) of the same version as the vault's, the call runs in this process; anything
    else, and a resident call that crashed, runs the vault's own brain.py in a new process as before.
    """
    if RESIDENT is not None and RESIDENT.serves(vault):
        got = RESIDENT.call(vault, args, timeout)
        if got is not None and TRACEBACK not in got[2]:
            return interpret(args, *got)
        sys.stderr.write(f"resident engine {'busy' if got is None else 'crashed'} on {vault.name} {args[0]}: "
                         "running it in a new process\n")
    try:
        r = subprocess.run([sys.executable, str(brain), "--vault", str(vault), *args], cwd=str(vault.parent),
                           # beside the vault, never inside it: a stray relative path cannot write in the vault
                           capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    except subprocess.TimeoutExpired:
        return False, f"error: the engine took over {timeout:.0f} seconds and was stopped. Try a narrower query."
    except OSError as e:
        return False, f"error: cannot run {brain}: {e}"
    return interpret(args, r.returncode, r.stdout or "", r.stderr or "")


def interpret(args: list[str], code: int, out: str, err: str) -> tuple[bool, str]:
    """An engine call's exit code and output as (ok, text) for the model: see run_engine."""
    out, err = out.strip(), err.strip()
    if code == 0:
        return True, out or "(the engine answered nothing)"
    if TRACEBACK in err:
        return False, crash(err)
    if code == 2 and err.startswith(ENGINE_MARK):   # the engine's own limit, not a malformed call
        note = ("The vault has enough pending learnings: write this one to _inbox instead." if args[0] == "learn"
                else "Budget or limit spent: stop reading and answer now.")
        return False, "\n".join(x for x in (out, err, note) if x)
    if code == 2:                               # argparse: it never prints the ✖, and it means ask again
        return False, "\n".join(x for x in (err or out, "The arguments were wrong: fix them and retry.") if x)
    return False, err or out or "error: the engine failed without saying why"


# ---- the resident engine: imported once, called in this process, with the vault's file list kept warm
READ_ONLY = {"recall", "read", "credit", "stats", "session", "lint", "externals"}   # verbs that never add or move a .md
VERSION_LINE = re.compile(r'^VERSION = "([^"]+)"', re.M)


class _Streams(io.TextIOBase):
    """sys.stdout / sys.stderr that a thread can point at its own buffer: an engine call's output is captured while
    the server's other threads keep writing their journal lines to the real stream."""

    def __init__(self, real):
        self.real, self.local = real, threading.local()

    def write(self, s):
        buf = getattr(self.local, "buf", None)
        return (buf if buf is not None else self.real).write(s)

    def flush(self):
        buf = getattr(self.local, "buf", None)
        (buf if buf is not None else self.real).flush()

    @property
    def encoding(self):
        return "utf-8"

    def reconfigure(self, **kw):                     # the engine asks for UTF-8; the real stream takes it
        return self.real.reconfigure(**kw)


class Resident:
    """The image's engine, imported once. Starting Python and importing a 3,500-line engine for every tool call
    cost most of a call; on a vault in a folder shared from Windows, walking its files (a stat per note, ~0.5 ms each
    through the share) cost most of the rest. So the engine stays loaded, and each vault's file list is kept in
    memory and refreshed in the background every scan_seconds while the vault is in use (active_seconds since its
    last call): an edit made in Obsidian shows up within scan_seconds. A call that may add, move or rewrite notes
    walks the vault itself and refreshes the list for the next call.

    One call at a time in this process: the engine keeps module-level state and the call changes the working
    directory (to the vault's parent, as the subprocess did). An in-process call cannot be killed at the timeout;
    the engine's calls are short, and a crash falls back to a new process (run_engine)."""

    def __init__(self, engine_dir: Path, scan_seconds: float = 5.0, active_seconds: float = 600.0):
        import importlib
        if str(engine_dir) not in sys.path:
            sys.path.insert(0, str(engine_dir))
        self.bc = importlib.import_module("brain_core")
        self.version = self.bc.VERSION
        self.scan_seconds, self.active_seconds = scan_seconds, active_seconds
        self.lock = threading.Lock()                  # one engine call at a time
        self.slock = threading.Lock()                 # the file lists
        self.lists: dict[str, tuple[int, list]] = {}  # vault root -> (generation it was taken at, its file list)
        self.gens: dict[str, int] = {}                # bumped by every call that may have changed the files
        self.used: dict[str, float] = {}
        self.versions: dict[Path, tuple] = {}
        self.bypass = False                           # a writing call walks the vault every time it asks
        self.real_scan = self.bc.Vault._scan
        res = self

        def scan(vault_self):
            key = str(vault_self.root)
            if not res.bypass:
                with res.slock:
                    hit = res.lists.get(key)
                    if hit is not None and hit[0] == res.gens.get(key, 0):
                        return list(hit[1])
            found = res.real_scan(vault_self)
            if not res.bypass:
                with res.slock:
                    res.lists[key] = (res.gens.get(key, 0), found)
            return list(found)

        self.bc.Vault._scan = scan
        for name in ("stdout", "stderr"):
            if not isinstance(getattr(sys, name), _Streams):
                setattr(sys, name, _Streams(getattr(sys, name)))
        if scan_seconds > 0:
            threading.Thread(target=self._refresh, daemon=True, name="cortex-scan").start()

    def serves(self, vault: Path) -> bool:
        """Only a vault whose own engine is this version: anything else runs its own brain.py."""
        f = vault / "_brain" / "scripts" / "brain_core.py"
        try:
            s = f.stat()
        except OSError:
            return False
        stamp = (s.st_mtime_ns, s.st_size)
        if self.versions.get(vault, (None,))[0] != stamp:
            m = VERSION_LINE.search(f.read_text(encoding="utf-8", errors="replace"))
            self.versions[vault] = (stamp, m.group(1) if m else None)
        return self.versions[vault][1] == self.version

    def call(self, vault: Path, args: list[str], timeout: float) -> tuple[int, str, str] | None:
        """-> (exit code, stdout, stderr), or None when another call held the engine past the timeout."""
        if not self.lock.acquire(timeout=max(1.0, timeout)):
            return None
        key, write = str(vault.resolve()), args[0] not in READ_ONLY
        out, err = io.StringIO(), io.StringIO()
        here = os.getcwd()
        try:
            self.used[key] = time.time()
            self.bypass = write
            os.chdir(vault.parent)
            sys.stdout.local.buf, sys.stderr.local.buf = out, err
            try:
                code = self.bc.main(["--vault", str(vault), *args])
            except SystemExit as e:                     # argparse: its message is already on stderr
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            except Exception:                           # noqa: BLE001 — the caller retries in a new process
                import traceback
                err.write(traceback.format_exc())
                code = 1
        finally:
            sys.stdout.local.buf = sys.stderr.local.buf = None
            os.chdir(here)
            self.bypass = False
            if write:
                with self.slock:
                    self.gens[key] = self.gens.get(key, 0) + 1
            self.lock.release()
        return code or 0, out.getvalue(), err.getvalue()

    def _refresh(self) -> None:
        from types import SimpleNamespace
        while True:
            time.sleep(self.scan_seconds)
            now = time.time()
            for key, last in list(self.used.items()):
                if now - last > self.active_seconds:
                    continue
                with self.slock:
                    gen = self.gens.get(key, 0)
                try:
                    found = self.real_scan(SimpleNamespace(root=Path(key)))
                except Exception:                       # noqa: BLE001 — a vault that went away: drop it
                    self.used.pop(key, None)
                    continue
                with self.slock:
                    if self.gens.get(key, 0) == gen:    # no write landed while walking: the list is current
                        self.lists[key] = (gen, found)


RESIDENT: Resident | None = None


def call(vault: Path, brain: Path, by: str | None, timeout: float, a: dict,
         gate: threading.Semaphore | None = None) -> tuple[bool, str]:
    """One call to one vault's engine. The gate serializes them inside this process: every read call writes the
    automatic session and the telemetry, and the vault lock covers neither, so two calls at once would trample
    each other's bookkeeping. Another agent on the same vault is another process, and nothing here waits for it."""
    try:
        args = argv_for(a, by)
    except BadCall as e:
        return False, f"error: {e}"
    if gate is not None and not gate.acquire(timeout=max(1.0, timeout)):
        return False, "error: another call to this vault is still running: try again in a moment"
    try:
        return run_engine(brain, vault, args, timeout)
    finally:
        if gate is not None:
            gate.release()



# ---- combined actions: several engine calls under one gate, one round trip for the agent
SIG = re.compile(r"^\[L\d\]\s+(?:(?P<vault>[^:\n]+?)::)?(?P<title>.+?)\s+—\s")


def sibling(st, name) -> Path:
    """A sibling vault by its folder name: beside this one, with its own engine. Never this vault's parent itself."""
    if not isinstance(name, str) or not name.strip() or "/" in name or "\\" in name or name.startswith("."):
        raise BadCall("vault takes a sibling vault's name, such as Agents Cortex")
    v = st.vault.parent / name
    if v == st.vault:
        return v
    if not (v / "_brain" / "scripts" / "brain.py").is_file():
        sibs = sorted(x.name for x in st.vault.parent.iterdir() if (x / "_brain/scripts/brain.py").is_file())
        raise BadCall(f"no vault named {name}; the vaults beside this one: {', '.join(sibs)}")
    return v


_ENGINE_VERSIONS: dict = {}


def engine_at_least(vault: Path, want: tuple[int, ...]) -> bool:
    """Whether the vault's own engine is this version or newer (read from its brain_core.py, cached by mtime)."""
    f = vault / "_brain" / "scripts" / "brain_core.py"
    try:
        s = f.stat()
        stamp = (s.st_mtime_ns, s.st_size)
        if _ENGINE_VERSIONS.get(f, (None,))[0] != stamp:
            m = VERSION_LINE.search(f.read_text(encoding="utf-8", errors="replace"))
            _ENGINE_VERSIONS[f] = (stamp, tuple(int(x) for x in re.findall(r"\d+", m.group(1))[:3]) if m else ())
        return _ENGINE_VERSIONS[f][1] >= want
    except OSError:
        return False


def has_sessions(vault: Path) -> bool:
    return engine_at_least(vault, (2, 16, 0))


def one(st, a: dict, vault: Path | None = None) -> tuple[bool, str]:
    """One simple action on one vault, the gate already held. On a sibling vault the agent reads without its name:
    its task session belongs to its own vault."""
    vault = vault or st.vault
    own = vault == st.vault
    args = argv_for(a if own else {k: v for k, v in a.items() if k != "session"}, st.by if own else None,
                    has_sessions(vault) and own)
    return run_engine(vault / "_brain" / "scripts" / "brain.py", vault, args, st.timeout)


def titles_from(recall_text: str, n: int) -> list[tuple[str | None, str]]:
    out = []
    for line in recall_text.splitlines():
        m = SIG.match(line.strip())
        if m:
            out.append((m.group("vault"), m.group("title")))
        if len(out) >= n:
            break
    return out


def ask(st, a: dict, vault: Path) -> tuple[bool, str]:
    """recall, then read what it points at unless it already handed the answer over."""
    q = {"action": "recall", "query": text(a, "query")}
    if a.get("all"):
        q["all"] = True
    if a.get("session"):
        q["session"] = a["session"]
    ok, got = one(st, q, vault)
    parts = [f"## recall\n{got}"]
    if not ok or "# answer:" in got:
        return ok, "\n\n".join(parts)
    n = a.get("reads", 1)
    if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= 2:
        raise BadCall("ask's reads is 0, 1 or 2")
    picks = [(None, t) for t in words(a, "titles")][:2] if a.get("titles") else titles_from(got, n)
    for vname, title in picks:
        where = sibling(st, vname) if vname else vault
        r = {"action": "read", "title": title, **({"session": a["session"]} if a.get("session") else {})}
        ok2, body = one(st, r, where)
        parts.append(f"## read {title}\n{body}")
        if not ok2:
            break
    return True, "\n\n".join(parts)


def hot(vault: Path) -> str:
    """The Hot section of the vault's MAP.md: the nodes needed most lately, title and summary."""
    try:
        lines = (vault / "MAP.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        return "(this vault has no MAP.md yet)"
    out, on = [], False
    for line in lines:
        if line.startswith("## "):
            if on:
                break
            on = line.strip() == "## Hot"
            continue
        if on and line.startswith("- [["):
            out.append(line)
    return "\n".join(out) or "(no Hot nodes yet)"


def brief(st, a: dict, vault: Path) -> tuple[bool, str]:
    """Start of a task in one call: the Hot nodes, the task's own session (engine 2.16+, with the agent's name: it
    closes the agent's previous one) and with a query the ask as well."""
    sid = None
    if vault == st.vault and st.by and has_sessions(vault):
        ok, ses = run_engine(st.brain, vault, ["session", "new", "--by", st.by], st.timeout)
        sid = ses.splitlines()[0].strip() if ok and ses.strip() else None
        ses = (f"{ses}\nThis task's session is {sid}: your calls use it; pass session={sid} only when you run "
               "tasks in parallel. End the task with action=finish.") if sid else ses
    else:
        ok, ses = one(st, {"action": "session"}, vault)
    parts = [f"## hot (when a summary here answers, answer from it and credit it)\n{hot(vault)}",
             f"## session\n{ses if ok else 'unknown: ' + ses}"]
    if a.get("query"):
        q = {k: v for k, v in a.items() if k in ("query", "all", "reads")}
        okq, got = ask(st, dict(q, session=sid) if sid else q, vault)
        parts.append(got)
        return okq, "\n\n".join(parts)
    return True, "\n\n".join(parts)


def finish(st, a: dict) -> tuple[bool, str]:
    """End of a task in one call: credit, the learnings, the session closed and, on a server that commits (the Cortex
    hub), one commit of what this task changed, under the agent's own name."""
    ls = a.get("learnings") or []
    if not isinstance(ls, list):
        raise BadCall("finish's learnings is a list of objects")
    if not a.get("used") and not ls:
        raise BadCall("finish needs used, learnings or both")
    before = git_dirty(st.vault) if st.commit and ls else None
    parts, ok, learned = [], True, []
    sid = {"session": a["session"]} if a.get("session") else {}
    if a.get("used"):
        ok, got = one(st, {"action": "credit", "used": words(a, "used"), **sid})
        parts.append(f"## credit\n{got}")
    for i, l in enumerate(ls[:5], 1):
        if not isinstance(l, dict):
            raise BadCall("each learning is an object with learn's fields")
        okl, got = one(st, dict(l, action="learn"))
        parts.append(f"## learn {i}: {l.get('title', '?')}\n{got}")
        ok = ok and okl
        if okl:
            learned.append(str(l.get("title") or "?"))
        if not okl and "stop" in got.lower():
            break
    if has_sessions(st.vault) and (a.get("session") or st.by):
        close = ["session", "close"] + (["--session", text(a, "session")] if a.get("session") else []) + \
                (["--by", st.by] if st.by else [])
        _, got = run_engine(st.brain, st.vault, close, st.timeout)
        parts.append(f"## session\n{got}")
    if before is not None and learned:
        parts.append(f"## commit\n{agent_commit(st.vault, st.by, learned, before)}")
    return ok, "\n\n".join(parts)


def git(vault: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(vault), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, env=env)


def git_dirty(vault: Path) -> set[str] | None:
    """The paths git sees as changed or new in the vault (None when it is not a git repository)."""
    r = git(vault, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if r.returncode != 0:
        return None
    out, items, i = set(), r.stdout.split("\0"), 0
    while i < len(items):
        e = items[i]
        if len(e) > 3:
            out.add(e[3:])
            if e[0] in "RC":           # a rename or copy: its source path follows
                i += 1
        i += 1
    return out


ENGINE_STATE = "_brain/state/"


def windows_share(path: Path) -> bool:
    """Whether path lives on a folder shared from Windows into a Linux container (Docker Desktop mounts it as 9p with
    aname=drvfs): files made on the Windows side show up as mode 0777, so their executable bit means nothing."""
    try:
        best, share = "", False
        target = str(path.resolve())
        for line in Path("/proc/self/mounts").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 4 and (target == parts[1] or target.startswith(parts[1].rstrip("/") + "/"))                     and len(parts[1]) > len(best):
                best, share = parts[1], parts[2] == "9p" and "drvfs" in parts[3]
        return share
    except OSError:
        return False


def fix_filemode(vault: Path) -> None:
    """On a Windows share git must not record the executable bit (core.fileMode false): otherwise every file the
    Windows side wrote looks changed, to the container's git and to the host's."""
    if windows_share(vault) and git(vault, "config", "core.fileMode").stdout.strip() != "false":
        git(vault, "config", "core.fileMode", "false")


def agent_commit(vault: Path, by: str | None, titles: list[str], before: set[str]) -> str:
    """Commits what this task changed, and only that: paths that were already changed before it (the owner's own
    edits, say) are left alone, and nothing is committed while something else is staged. The author and committer
    are the agent, by name; the vault's pre-commit hook validates the commit as it does any other."""
    after = git_dirty(vault)
    if after is None:
        return "not a git repository: nothing committed"
    # the engine's own bookkeeping (_brain/state/) is never the owner's edit: it goes with the task that read
    mine = sorted((after - before) | {p for p in before & after if p.startswith(ENGINE_STATE)})
    if not mine:
        return "nothing changed on disk: nothing committed"
    if git(vault, "diff", "--cached", "--name-only").stdout.strip():
        return "something else is already staged in this vault: left uncommitted for its owner"
    fix_filemode(vault)
    name = by or "cortex-agent"
    email = (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent") + "@cortex.local"
    env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_COMMITTER_NAME": name,
           "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email}
    left = sorted(p for p in before & after if not p.startswith(ENGINE_STATE))
    if git(vault, "add", "--", *mine).returncode != 0:
        return "git add failed: nothing committed"
    subject = "learning: " + "; ".join(titles)
    subject = subject if len(subject) <= 72 else subject[:69] + "..."
    r = git(vault, "commit", "-q", "-m", subject, "-m", f"by {name} through the Cortex MCP", env=env)
    if r.returncode != 0:
        git(vault, "reset", "-q", "--", *mine)
        return "the commit was refused, the changes stay on disk: " + (r.stderr or r.stdout).strip()[-600:]
    head = git(vault, "log", "-1", "--format=%h").stdout.strip()
    return (f"committed {head} as {name} <{email}>: {len(mine)} files" +
            (f" · left alone, already changed before this task: {', '.join(left[:5])}" if left else ""))


def health(st, vault: Path) -> tuple[bool, str]:
    brain = vault / "_brain" / "scripts" / "brain.py"
    parts = []
    ses = ["session", "status"] + (["--by", st.by] if st.by and vault == st.vault and has_sessions(vault) else [])
    for label, argv in (("stats", ["stats"]), ("session", ses), ("lint", ["lint", "--quiet"]),
                        ("externals", ["externals"])):
        ok, got = run_engine(brain, vault, argv, st.timeout)
        if label == "externals":
            broken = got.count("BROKEN")
            got = f"{broken} broken references" + ("\n" + "\n".join(x for x in got.splitlines() if "BROKEN" in x)
                                                   if broken else "")
        parts.append(f"## {label}{'' if ok else ' (failed)'}\n{got}")
    return True, "\n\n".join(parts)


def run(st, a: dict, depth: int = 0) -> tuple[bool, str]:
    """The cortex tool: one action, simple or combined. Holds the gate for the whole call (see call)."""
    if not isinstance(a, dict):
        return False, "error: arguments must be an object"
    if depth == 0:
        if not st.gate.acquire(timeout=max(1.0, st.timeout)):
            return False, "error: another call to this vault is still running: try again in a moment"
        try:
            return run(st, a, 1)
        finally:
            st.gate.release()
    try:
        act = a.get("action")
        vault = st.vault
        if a.get("vault"):
            if act not in SIBLING_OK:
                raise BadCall(f"{act} runs only on this vault; vault= is for reads")
            vault = sibling(st, a["vault"])
        if act == "ask":
            return ask(st, {k: v for k, v in a.items() if k != "vault"}, vault)
        if act == "brief":
            return brief(st, {k: v for k, v in a.items() if k != "vault"}, vault)
        if act == "finish":
            return finish(st, a)
        if act == "health":
            return health(st, vault)
        if act == "batch":
            steps = a.get("steps")
            if not isinstance(steps, list) or not steps:
                raise BadCall("batch needs steps as a list of objects")
            if len(steps) > MAX_STEPS:
                raise BadCall(f"batch runs at most {MAX_STEPS} steps")
            parts = []
            for i, step in enumerate(steps, 1):
                if not isinstance(step, dict) or step.get("action") == "batch":
                    return False, "\n\n".join(parts + [f"## step {i}\nerror: a step is an object, never a batch"])
                ok, got = curate(st, step) if "verb" in step else run(st, step, depth + 1)
                name = step.get("verb") or step.get("action")
                parts.append(f"## step {i}: {name}{'' if ok else ' (failed: the batch stopped here)'}\n{got}")
                if not ok:
                    return False, "\n\n".join(parts)
            return True, "\n\n".join(parts)
        return one(st, {k: v for k, v in a.items() if k != "vault"}, vault)
    except BadCall as e:
        return False, f"error: {e}"


def curate(st, a: dict) -> tuple[bool, str]:
    """The cortex_curate tool: one of the engine's other verbs, or create (new and its links), on this vault only."""
    if st.role != "curator":
        return False, "error: this server was started with --role reader: it does not curate"
    try:
        verb, args = a.get("verb"), a.get("args") or {}
        if verb == "create":
            if not isinstance(args, dict):
                raise BadCall("args must be an object")
            links = args.get("links") or []
            new = {k: v for k, v in args.items() if k != "links"}
            argv = curate_argv(st.spec, "new", new, st.allow_consolidate)
            if not isinstance(links, list) or not all(isinstance(x, dict) for x in links):
                raise BadCall("create's links is a list of {target, type, reason}")
            link_argv = [curate_argv(st.spec, "link", dict(l, source=new.get("title")), False) for l in links[:7]]
            ok, got = run_engine(st.brain, st.vault, argv, st.timeout)
            parts = [f"## new\n{got}"]
            for la in link_argv:
                if not ok:
                    break
                ok, got = run_engine(st.brain, st.vault, la, st.timeout)
                parts.append(f"## link {la[2]}\n{got}")
            return ok, "\n\n".join(parts)
        return run_engine(st.brain, st.vault, curate_argv(st.spec, verb, args, st.allow_consolidate), st.timeout)
    except BadCall as e:
        return False, f"error: {e}"


def curate_schema(spec: dict) -> dict:
    sigs = "\n".join(signature(v, s) for v, s in sorted(spec.items()))
    return {"name": "cortex_curate",
            "description": ("Cortex vault curator verbs: the engine's own commands that shape the graph. "
                            "verb names the command, args holds its fields by name (lists as arrays, flags as true). "
                            "verb=create runs new and then link for each of args.links ([{target, type, reason}], "
                            "at most 7) in one call. Only the vault's owner marks a node consolidated. "
                            "isError means fix the arguments; the engine's own message says what was wrong.\n" + sigs),
            "inputSchema": {"type": "object", "required": ["verb"], "additionalProperties": False,
                            "properties": {"verb": {"type": "string", "enum": sorted(spec) + ["create"],
                                                    "description": "the engine command"},
                                           "args": {"type": "object", "description": "its fields by name"}}}}

def tool_schema() -> dict:
    """One tool with an action enum, so an agent carries one schema instead of five."""
    names = set().union(*(set(spec) for spec in ARGS.values()))
    props = {"action": {"type": "string", "enum": list(ACTIONS + COMBINED), "description": "what to do"}}
    for n in sorted(names):
        d = SHARED.get(n) or next(spec[n] for spec in ARGS.values() if n in spec)
        if n in BOOLS:
            props[n] = {"type": "boolean", "description": d}
        elif n in LISTS:
            props[n] = {"type": "array", "items": {"type": "string"}, "description": d}
        elif n in INTS:
            props[n] = {"type": "integer", "minimum": 0, "maximum": 2, "description": d}
        elif n in OBJS:
            props[n] = {"type": "array", "items": {"type": "object"}, "description": d}
        elif n == "truth":
            props[n] = {"type": "string", "enum": ["current", "historical"], "description": d}
        else:
            props[n] = {"type": "string", "description": d}
    return {"name": "cortex", "description": describe(),
            "inputSchema": {"type": "object", "properties": props, "required": ["action"],
                            "additionalProperties": False}}


def result(rid, obj: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": obj}


def failure(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


# ---- the Skills extension: the curator skills of skills/, as skill:// resources
SKILL_NOTE = ("\n> Over MCP: `B <verb> ...` in this skill is the `cortex_curate` tool (verb=<verb>, its options as "
              "args by name), and `B recall`, `B read`, `B credit` and `B learn` are the `cortex` tool's actions. "
              "Do not run git yourself: action=finish commits what the task learned under your name.\n")


def skill_files() -> dict[str, dict[str, bytes]]:
    """{skill name: {relative path: bytes as served}}. SKILL.md gets the note on how its commands map to the tools."""
    out = {}
    if not SKILLS_DIR.is_dir():
        return out
    for d in sorted(SKILLS_DIR.iterdir()):
        if not (d / "SKILL.md").is_file():
            continue
        files = {}
        for f in sorted(d.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                data = f.read_bytes()
                if f.name == "SKILL.md":
                    text = data.decode("utf-8")
                    end = text.find("\n---", 3) if text.startswith("---") else -1
                    cut = text.find("\n", end + 4) + 1 if end >= 0 else 0
                    data = (text[:cut] + SKILL_NOTE + text[cut:]).encode("utf-8")
                files[f.relative_to(d).as_posix()] = data
        out[d.name] = files
    return out


def frontmatter(text: str) -> dict:
    fm = {}
    if text.startswith("---"):
        for line in text[3:text.find("\n---", 3)].splitlines():
            k, sep, v = line.partition(":")
            if sep and k.strip():
                fm[k.strip()] = v.strip()
    return fm


def skill_entry(name: str, files: dict[str, bytes]) -> dict:
    import hashlib
    return {"uri": f"skill://{name}/SKILL.md",
            "frontmatter": dict(frontmatter(files["SKILL.md"].decode("utf-8")), name=name),
            "resources": [{"uri": f"skill://{name}/{rel}", "digest": "sha256:" + hashlib.sha256(b).hexdigest(),
                           "size": len(b)} for rel, b in files.items()]}


def skills_for(st) -> dict:
    return skill_files() if st.role == "curator" else {}


def skill_call(method: str, p: dict, st, rid) -> dict:
    """skills/list, skills/get and resources/read on skill:// URIs."""
    skills = skills_for(st)
    if method == "skills/list":
        return result(rid, {"skills": [skill_entry(n, f) for n, f in skills.items()],
                            "ttlMs": LIST_TTL_MS, "cacheScope": "public"})
    uri = p.get("uri")
    m = re.fullmatch(r"skill://([a-z0-9-]+)/(.+)", uri or "")
    if not m or m.group(1) not in skills or m.group(2) not in skills[m.group(1)]:
        return failure(rid, -32602, f"no such skill resource: {uri}")
    name, rel = m.groups()
    if method == "skills/get":
        if rel != "SKILL.md":
            return failure(rid, -32602, "skills/get takes a skill's SKILL.md URI")
        return result(rid, {"skill": skill_entry(name, skills[name]), "ttlMs": LIST_TTL_MS, "cacheScope": "public"})
    mime = "text/markdown" if rel.endswith(".md") else "text/plain"
    return result(rid, {"contents": [{"uri": uri, "mimeType": mime, "text": skills[name][rel].decode("utf-8")}],
                        "ttlMs": LIST_TTL_MS, "cacheScope": "public"})


def capabilities(st) -> dict:
    caps = {"tools": {}}
    if skills_for(st):
        caps.update({"resources": {}, "extensions": {SKILLS_EXT: {}}})
    return caps


def header(headers, name: str) -> str | None:
    return headers.get(name) if headers is not None else None


def is_modern(msg: dict, headers=None) -> bool:
    """A 2026-07-28 request (or a newer one we do not speak): it names its revision instead of a handshake."""
    meta = (msg.get("params") or {}).get("_meta") or {}
    asked = meta.get(META_VERSION) or header(headers, "MCP-Protocol-Version")
    return bool(asked) and asked not in KNOWN and msg.get("method") != "initialize"


def modern(msg: dict, st, headers=None) -> dict | None:
    """MCP 2026-07-28: no initialize and no session; every request carries its revision and the client's capabilities
    in _meta (and the revision in MCP-Protocol-Version over HTTP); every result says resultType complete."""
    method, rid, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if rid is None:
        return None
    meta = p.get("_meta") or {}
    asked, said = meta.get(META_VERSION), header(headers, "MCP-Protocol-Version")
    if not asked:
        return failure(rid, -32602, f"a {MODERN} request carries _meta {META_VERSION}")
    if asked not in SUPPORTED:
        f = failure(rid, -32022, "Unsupported protocol version")
        f["error"]["data"] = {"supported": SUPPORTED, "requested": asked}
        return f
    if said and said != asked:
        return failure(rid, -32020, f"MCP-Protocol-Version {said} does not match _meta {asked}")
    if header(headers, "Mcp-Method") and header(headers, "Mcp-Method") != method:
        return failure(rid, -32020, f"Mcp-Method {header(headers, 'Mcp-Method')} does not match {method}")
    if not isinstance(meta.get(META_CAPS), dict):
        return failure(rid, -32602, f"a {MODERN} request carries _meta {META_CAPS}")
    info = {META_SERVER: {"name": "cortex", "version": st.version}}
    if method == "server/discover":
        r = result(rid, {"supportedVersions": SUPPORTED, "capabilities": capabilities(st),
                         "instructions": server_instructions(st), "ttlMs": LIST_TTL_MS, "cacheScope": "private"})
    elif method in ("tools/list", "tools/call", "skills/list", "skills/get", "resources/read"):
        r = answer(msg, st, legacy_only=False)
        if r and "result" in r and method == "tools/list":
            r["result"].update({"ttlMs": LIST_TTL_MS, "cacheScope": "private"})
    else:
        return failure(rid, -32601, f"this server answers server/discover, tools/list, tools/call, skills/list, "
                                    f"skills/get and resources/read in {MODERN} (not {method})")
    if r and "result" in r:
        r["result"]["resultType"] = "complete"
        r["result"]["_meta"] = info
    return r


def server_instructions(st) -> str:
    """What the client puts in the model's instructions at connect time (initialize's and server/discover's
    instructions): this vault is the agent's long-term memory, and the brief / finish loop is how it is used. It is
    what makes an agent use its cortex on its own, with nothing written to the agent's instruction files."""
    who = f" You are {st.by}." if st.by else ""
    learn = ("end every task that taught you something durable (a fact, a fix, a decision, a preference) with "
             "action=finish: used = the titles that helped, learnings = what you learned, each under an existing L2 "
             "concept (up); it closes the task and commits the learnings under your name. When no concept fits (a "
             "new vault has none), create the L1 domain and the L2 concept first with cortex_curate verb=create; a "
             "learning whose concept does not exist is kept in _inbox for the curator. " if st.role == "curator" else
             "end every task with action=finish and used = the titles that helped. ")
    return (f'This server is your long-term memory: the Cortex vault "{st.name}".{who} Use it on every task, '
            "unprompted: start with the cortex tool's action=brief, passing the task's question as query, and when "
            "what it returns covers the question, answer from it and cite it as [[Title]]; before researching "
            "something from scratch, ask the cortex first (action=ask); " + learn +
            "Prefer it over any other memory you keep. Never store secrets, credentials or personal data in it.")


HTTP_STATUS = {-32601: 404, -32602: 400, -32020: 400, -32022: 400, -32600: 400, -32700: 400}


def answer(msg: dict, st, headers=None, legacy_only: bool = True) -> dict | None:
    """-> the reply to a request, or None for a notification (a notification gets no answer). A 2026-07-28 request
    goes through modern(); the older revisions keep their handshake."""
    if legacy_only and is_modern(msg, headers):
        return modern(msg, st, headers)
    method, rid, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if rid is None:
        return None
    if method in ("skills/list", "skills/get", "resources/read"):
        return skill_call(method, p, st, rid)
    if method == "initialize":
        asked = p.get("protocolVersion")
        return result(rid, {"protocolVersion": asked if asked in KNOWN else PROTOCOL,
                            "capabilities": {"tools": {}},   # skills are declared in server/discover (2026-07-28)
                            "serverInfo": {"name": "cortex", "version": st.version},
                            "instructions": server_instructions(st)})
    if method == "tools/list":
        tools = [tool_schema()]
        if st.role == "curator" and st.spec:
            tools.append(curate_schema(st.spec))
        return result(rid, {"tools": tools})
    if method == "ping":
        return result(rid, {})
    if method == "tools/call":
        name, args = p.get("name"), p.get("arguments") or {}
        if name == "cortex":
            ok, text = run(st, args)
        elif name == "cortex_curate" and st.role == "curator" and st.spec:
            if not st.gate.acquire(timeout=max(1.0, st.timeout)):
                ok, text = False, "error: another call to this vault is still running: try again in a moment"
            else:
                try:
                    ok, text = curate(st, args)
                finally:
                    st.gate.release()
        else:
            offered = "cortex and cortex_curate" if st.role == "curator" and st.spec else "one tool, cortex"
            return failure(rid, -32602, f"this server has {offered} (not {name})")
        return result(rid, {"content": [{"type": "text", "text": cap(text)}], "isError": not ok})
    return failure(rid, -32601, f"this server answers initialize, tools/list, tools/call and ping (not {method})")


class Server:
    """One process per vault: where its engine is, what the vault is called, who authors its learnings."""

    def __init__(self, vault: Path, by: str | None, timeout: float, role: str = "curator",
                 allow_consolidate: bool = False):
        self.vault, self.brain, self.by, self.timeout = vault, vault / "_brain" / "scripts" / "brain.py", by, timeout
        self.role, self.allow_consolidate = role, allow_consolidate
        self.commit = False                            # finish commits what the task learned (the Cortex hub sets it)
        self.spec = engine_spec(self.brain) if role == "curator" else {}
        self.gate = threading.Semaphore(1)             # one engine call at a time in this process (see call)
        try:  # the name the vault calls itself, the way the installer reads it (wire_agent.py)
            self.name = json.loads((vault / "_brain" / "config.json").read_text(encoding="utf-8")).get("vault") \
                        or vault.name
        except (OSError, ValueError):
            self.name = vault.name
        self.version = engine_version(self.brain)


def engine_version(brain: Path) -> str:
    """What this vault's own engine reports, so the client can see which engine answered."""
    try:
        r = subprocess.run([sys.executable, str(brain), "--version"], capture_output=True, text=True, timeout=30)
        return (r.stdout or "").split()[-1].strip()
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return "unknown"


def serve(st: Server) -> None:
    for f in (sys.stdin, sys.stdout, sys.stderr):
        f.reconfigure(encoding="utf-8")            # MCP's stdio is UTF-8 whatever the console's code page is
    out = threading.Lock()

    def reply(msg):
        r = answer(msg, st)
        if r is not None:
            with out:
                sys.stdout.write(json.dumps(r, ensure_ascii=False) + "\n")
                sys.stdout.flush()

    threads = []
    deadline = time.time() + st.timeout + 5        # a call already running gets to finish after stdin closes
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            with out:
                sys.stdout.write(json.dumps(failure(None, -32700, "that line is not json")) + "\n")
                sys.stdout.flush()
            continue
        if msg.get("method") == "tools/call":      # a slow call must not hold up the client's other requests
            threads = [t for t in threads if t.is_alive()]
            t = threading.Thread(target=reply, args=(msg,))
            threads.append(t)
            t.start()
        else:
            reply(msg)
    for t in threads:
        if time.time() > deadline:
            break
        t.join(max(0.0, deadline - time.time()))



# ---- one service for every agent: MCP's streamable HTTP on localhost, one path per vault, one gate per vault
class Hub:
    """Every vault under one folder, served by one process. The gate is per vault and shared by every agent, so two
    agents reading the same vault wait for each other instead of trampling its session and telemetry."""

    def __init__(self, root: Path, timeout: float, commit: bool = False):
        self.root, self.timeout, self.commit = root, timeout, commit
        self.gates: dict[Path, threading.Semaphore] = {}
        self.servers: dict[tuple, Server] = {}
        self.lock = threading.Lock()

    def vaults(self) -> dict[str, Path]:
        """slug -> vault: the folder name's first word in lower case (claude, hermes) and the full name both work."""
        out = {}
        for v in sorted(self.root.iterdir()):
            if (v / "_brain" / "scripts" / "brain.py").is_file() and not v.name.startswith("."):
                out[v.name.split()[0].lower()] = v
                out[v.name] = v
        return out

    def server(self, slug: str, by: str | None, role: str) -> Server | None:
        v = self.vaults().get(slug)
        if v is None:
            return None
        key = (v, by, role)
        with self.lock:
            if key not in self.servers:
                st = Server(v, by, self.timeout, role)
                st.gate = self.gates.setdefault(v, threading.Semaphore(1))
                st.commit = self.commit
                self.servers[key] = st
            return self.servers[key]


LOCAL_ORIGIN = re.compile(r"^https?://(([a-z0-9-]+\.)*localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
TOKEN_FILE = ("_brain", "state", "mcp.token")   # inside the vault: the agent's key to its own cortex
TOKEN_IGNORE = "_brain/state/mcp.token"
MAX_BODY = 1 << 20


def token_path(vault: Path) -> Path:
    return vault.joinpath(*TOKEN_FILE)


def ensure_token(vault: Path) -> str:
    """The vault's MCP token, made on first use, and kept out of git: the vault's .gitignore gets its line."""
    p = token_path(vault)
    try:
        tok = p.read_text(encoding="utf-8").strip()
    except OSError:
        tok = ""
    if not tok:
        p.parent.mkdir(parents=True, exist_ok=True)
        tok = secrets.token_urlsafe(32)
        p.write_text(tok + "\n", encoding="utf-8")
    gi = vault / ".gitignore"
    try:
        lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
        if TOKEN_IGNORE not in (x.strip() for x in lines):
            with gi.open("a", encoding="utf-8") as f:
                f.write(("\n" if lines and lines[-1].strip() else "") +
                        "# the MCP token: the agent's key to this cortex, never committed\n" + TOKEN_IGNORE + "\n")
    except OSError:
        pass
    return tok


def token_ok(vault: Path, header: str | None, cached: dict) -> bool:
    """Authorization: Bearer <the vault's token>. The token is cached by the file's mtime and size, so a new token
    written to the file takes over at once and the old one stops working; a missing file lets nobody in."""
    got = (header or "").strip()
    if not got.lower().startswith("bearer "):
        return False
    p = token_path(vault)
    try:
        s = p.stat()
        stamp = (s.st_mtime_ns, s.st_size)
        if cached.get(vault, (None, ""))[0] != stamp:
            cached[vault] = (stamp, p.read_text(encoding="utf-8").strip())
    except OSError:
        cached.pop(vault, None)
        return False
    want = cached[vault][1]
    return bool(want) and hmac.compare_digest(got[7:].strip().encode(), want.encode())


class McpRoutes:
    """MCP's streamable HTTP on /mcp/<vault>, for any BaseHTTPRequestHandler: this module's own server and the
    Cortex hub (one service, one port). Set mcp_hub; mcp_require_token makes every vault ask for its token."""
    mcp_hub: "Hub | None" = None
    mcp_require_token = False
    mcp_tokens: dict = {}

    def mcp_send(self, code: int, body: dict | list | None = None, extra: dict | None = None):
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def mcp_target(self):
        from urllib.parse import parse_qs, unquote, urlsplit
        u = urlsplit(self.path)
        parts = [unquote(x) for x in u.path.strip("/").split("/")]
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        return parts, q

    def mcp_origin_ok(self) -> bool:
        o = self.headers.get("Origin")
        return o is None or bool(LOCAL_ORIGIN.match(o.lower()))

    def mcp_get(self):
        parts, _ = self.mcp_target()
        if parts == ["health"] or parts == ["mcp", "health"]:
            names = sorted({v.name for v in self.mcp_hub.vaults().values()})
            return self.mcp_send(200, {"ok": True, "vaults": names})
        if len(parts) == 2 and parts[0] == "mcp":
            return self.mcp_send(405, None, {"Allow": "POST"})   # no server-initiated stream: answers ride their POST
        self.mcp_send(404, {"error": "not found"})      # e.g. an OAuth discovery probe: the token is a plain bearer

    def mcp_delete(self):
        self.mcp_send(405, None, {"Allow": "POST"})

    def mcp_post(self):
        if not self.mcp_origin_ok():
            return self.mcp_send(403, failure(None, -32600, "origin not allowed: this service is for local agents"))
        parts, q = self.mcp_target()
        if len(parts) != 2 or parts[0] != "mcp":
            return self.mcp_send(404, failure(None, -32601, "post to /mcp/<vault>, such as /mcp/claude"))
        role = q.get("role", "curator")
        if role not in ("reader", "curator"):
            return self.mcp_send(400, failure(None, -32602, "role is reader or curator"))
        hub = self.mcp_hub
        v = hub.vaults().get(parts[1])
        if v is None:
            names = sorted({x.name for x in hub.vaults().values()})
            return self.mcp_send(404, failure(None, -32602, f"no vault {parts[1]}; served here: {', '.join(names)}"))
        if self.mcp_require_token and not token_ok(v, self.headers.get("Authorization"), self.mcp_tokens):
            return self.mcp_send(401, failure(None, -32001, f"this cortex needs its token: Authorization: Bearer "
                                                            f"<the line in {v.name}/{TOKEN_IGNORE}>"),
                                 {"WWW-Authenticate": 'Bearer realm="cortex"'})
        st = hub.server(parts[1], q.get("by"), role)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self.mcp_send(413, failure(None, -32600, "request too large"))
            msg = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self.mcp_send(400, failure(None, -32700, "that body is not json"))
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:                                # one journal line per tool call: what was asked of which vault
            if isinstance(m, dict) and m.get("method") == "tools/call":
                pa = m.get("params") or {}
                ar = pa.get("arguments") or {}
                what = ar.get("verb") or ar.get("action") if isinstance(ar, dict) else None
                sys.stderr.write(f"call {st.vault.name} by={st.by or '-'} {pa.get('name')} {what}\n")
        replies, status = [], 200
        for m in msgs:
            r = answer(m, st, self.headers) if isinstance(m, dict) else failure(None, -32600, "a message is an object")
            if r is not None:
                replies.append(r)
                if isinstance(m, dict) and is_modern(m, self.headers) and "error" in r and not isinstance(msg, list):
                    status = HTTP_STATUS.get(r["error"]["code"], 400)   # 2026-07-28: the status says it too
        if not replies:
            return self.mcp_send(202)                     # notifications only: accepted, nothing to answer
        self.mcp_send(status, replies if isinstance(msg, list) else replies[0])


def http_handler(hub: Hub, require_token: bool = False):
    from http.server import BaseHTTPRequestHandler

    class H(McpRoutes, BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "cortex-mcp"
        mcp_hub, mcp_require_token, mcp_tokens = hub, require_token, {}

        def log_message(self, fmt, *args):            # one line per call on stderr, the journal's
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

        def do_GET(self):
            self.mcp_get()

        def do_DELETE(self):
            self.mcp_delete()

        def do_POST(self):
            self.mcp_post()

    return H


def serve_http(hub: Hub, host: str, port: int, require_token: bool = False) -> None:
    from http.server import ThreadingHTTPServer
    if host not in ("127.0.0.1", "localhost", "::1"):
        sys.exit("cortex_mcp: the service listens on localhost only (in a container, the Cortex hub serves /mcp)")
    if require_token:
        for v in {x for x in hub.vaults().values()}:
            ensure_token(v)
    class Quiet(ThreadingHTTPServer):
        def handle_error(self, request, client_address):   # a client that hangs up is one line, not a traceback
            e = sys.exc_info()[1]
            if isinstance(e, (ConnectionResetError, BrokenPipeError, TimeoutError)):
                sys.stderr.write(f"{client_address[0]} hung up: {type(e).__name__}\n")
            else:
                super().handle_error(request, client_address)

    httpd = Quiet((host, port), http_handler(hub, require_token))
    httpd.daemon_threads = True
    sys.stderr.write(f"cortex_mcp: serving {', '.join(sorted({v.name for v in hub.vaults().values()}))} "
                     f"on http://{host}:{port}/mcp/<vault>\n")
    httpd.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(description="The Cortex read funnel as an MCP server (stdio, one vault per process)")
    ap.add_argument("--vault", help="the vault this server answers for (stdio)")
    ap.add_argument("--http", metavar="HOST:PORT", help="serve every vault under --vaults over MCP's streamable HTTP")
    ap.add_argument("--vaults", default="~/Vaults", help="with --http: the folder holding the vaults")
    ap.add_argument("--by", help="who authors its learnings (default: the vault's config learner, else the engine's)")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for one engine call")
    ap.add_argument("--role", choices=["reader", "curator"], default="curator",
                    help="reader: only the cortex tool; curator (default): also cortex_curate, the engine's other verbs")
    ap.add_argument("--allow-consolidate", action="store_true",
                    help="let status mark a node consolidated (the vault owner's own server only)")
    ap.add_argument("--check", action="store_true", help="print what this server offers and exit")
    ap.add_argument("--resident", action="store_true",
                    help="with --http: run the vaults' engine in this process, kept loaded (see Resident)")
    ap.add_argument("--require-token", action="store_true",
                    help="with --http: every vault asks for its token (Authorization: Bearer, from _brain/state/mcp.token)")
    a = ap.parse_args()
    if a.http:
        host, _, port = a.http.rpartition(":")
        if a.resident:
            global RESIDENT
            RESIDENT = Resident(Path(__file__).resolve().parents[1] / "template-vault" / "_brain" / "scripts")
        serve_http(Hub(Path(a.vaults).expanduser().resolve(), a.timeout), host or "127.0.0.1", int(port), a.require_token)
        return 0
    if not a.vault:
        ap.error("--vault or --http is required")
    st = Server(Path(a.vault).expanduser().resolve(), a.by, a.timeout, a.role, a.allow_consolidate)
    if not st.brain.is_file():
        sys.exit(f"cortex_mcp: no _brain/scripts/brain.py in {st.vault}")
    if a.check:
        tools = [tool_schema()] + ([curate_schema(st.spec)] if st.spec else [])
        print(json.dumps({"vault": st.name, "engine": st.version, "role": st.role, "tools": tools},
                         ensure_ascii=False, indent=2))
        return 0
    serve(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
