# -*- coding: utf-8 -*-
"""Golden output of the engine on vaults that live inside this repository, to prove a refactor changes nothing.

    python golden_layers.py capture OUTDIR     # writes every command's output as files, to diff by hand
    python golden_layers.py digest             # prints the JSON that test_golden.py compares (golden_layers.json)

Vaults: the example vaults, the empty template vault, a "rich" vault built here (every layer, every consolidate rule and
many lint errors, with a usage history made by recall and credit) and the 1,000-node performance vault. Time, dates and
ids are fixed, so a run is the same on any day. Commands: lint, build (and the files it generates), recall of a fixed set
of queries, read, consolidate, ideas --dry-run, stats, doctor."""
from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
import time as _time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import brain  # noqa: E402

REPO = HERE.parents[3]
TEMPLATE = HERE.parents[2]
FIXED_DAY = _dt.date(2026, 11, 20)
FIXED_AT = _dt.datetime(2026, 11, 20, 12, 0, 0)
FIXED_TS = FIXED_AT.timestamp()

QUERIES = [
    "soil acidity", "when is the last frost", "slugs on lettuce", "water the garden", "who buys supplies",
    "tomato chutney", "cilantro", "orders cutoff time", "late orders wrong wave", "shipping window",
    "roses farms inventory", "cuándo es la última helada", "qué plaga hay", "nothing matches this zzzq",
    "decision about domains", "concept with many details",
]


class _Date(_dt.date):
    @classmethod
    def today(cls):
        return cls(FIXED_DAY.year, FIXED_DAY.month, FIXED_DAY.day)


class _DateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 20, 12, 0, 0)


def _shim_dt():
    ns = {k: getattr(_dt, k) for k in dir(_dt) if not k.startswith("__")}
    ns.update(date=_Date, datetime=_DateTime)
    return types.SimpleNamespace(**ns)


def _shim_time():
    ns = {k: getattr(_time, k) for k in dir(_time) if not k.startswith("__")}
    ns["time"] = lambda: FIXED_TS
    return types.SimpleNamespace(**ns)


@contextlib.contextmanager
def fixed_world():
    saved = {k: getattr(brain, k) for k in ("dt", "time", "new_id")}
    counter = [0]

    def new_id():
        counter[0] += 1
        return f"20261120-120000-{counter[0]:08x}"

    brain.dt, brain.time, brain.new_id = _shim_dt(), _shim_time(), new_id
    try:
        yield
    finally:
        for k, val in saved.items():
            setattr(brain, k, val)
        brain.WARM.clear() if hasattr(brain, "WARM") else None


def run(root, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = brain.main(["--vault", str(root), *args])
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def write_node(root: Path, rel: str, layer: int, title: str, up: str, summary: str, body: str = "", type=None,
               status="seed", date="2026-11-20", extra=None):
    fm = {"id": brain.new_id(), "layer": layer, "type": type or brain.DEFAULT_TYPE[layer], "summary": summary,
          "up": f"[[{up}]]", "aliases": [], "status": status, "external": [], "created": date, "updated": date}
    fm.update(extra or {})
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brain.serialize_fm(fm) + "\n" + body + "\n\n## Connections\n", encoding="utf-8")


def new_vault(path: Path, name: str):
    (path / "_brain").mkdir(parents=True)
    (path / "_brain" / "config.json").write_text(json.dumps({"vault": name, "siblings": {}, "warn_dependency_on": []}),
                                                 encoding="utf-8")
    shutil.copytree(TEMPLATE / "_templates", path / "_templates")
    for c in brain.COLD_FOLDERS:
        (path / c).mkdir()


def make_rich(path: Path):
    new_vault(path, "Rich")
    write_node(path, "Orders/Orders.md", 1, "Orders", "MAP", "Daily order cycle: intake, cutoff and dispatch.",
               "## Branches\n- [[Order cutoff is at 2pm EST]] — x\n- [[The shipping window limits the cutoff]] — x\n")
    write_node(path, "Orders/Order cutoff is at 2pm EST.md", 2, "Order cutoff is at 2pm EST", "Orders",
               "The daily cutoff decides which orders enter the dispatch wave.",
               "## Details\n- [[Late orders went to the wrong wave]] — x\n", extra={"aliases": ["cutoff time", "deadline"]})
    write_node(path, "Orders/Order cutoff is at 2pm EST/Late orders went to the wrong wave.md", 3,
               "Late orders went to the wrong wave", "Order cutoff is at 2pm EST",
               "Fixed error: the server clock was in UTC.", "The wave closed at 2pm UTC. " * 3, type="error")
    write_node(path, "Orders/The shipping window limits the cutoff.md", 2, "The shipping window limits the cutoff",
               "Orders", "The carrier's shipping window sets the latest possible cutoff.")
    write_node(path, "Orders/Empty old concept.md", 2, "Empty old concept", "Orders",
               "A concept nobody uses and that holds no details.", date="2026-01-01")
    write_node(path, "Orders/Old seed.md", 2, "Old seed", "Orders", "An old unused seed concept on carriers.",
               date="2026-01-02")
    # a domain with too many concepts, a central concept with many details
    write_node(path, "Flowers/Flowers.md", 1, "Flowers", "MAP", "Flowers grown, cut and sold by the farm.",
               "## Branches\n" + "".join(f"- [[Flower concept {i}]] — x\n" for i in range(14)))
    for i in range(14):
        write_node(path, f"Flowers/Flower concept {i}.md", 2, f"Flower concept {i}", "Flowers",
                   f"Concept {i} about roses, carnations and inventory.",
                   "## Details\n" + "".join(f"- [[Flower detail {i}-{k}]] — x\n" for k in range(6 if i == 0 else 1)))
        for k in range(6 if i == 0 else 1):
            write_node(path, f"Flowers/Flower concept {i}/Flower detail {i}-{k}.md", 3, f"Flower detail {i}-{k}",
                       f"Flower concept {i}", f"Detail {k} of concept {i}: roses are sold in dozens.",
                       "word " * (320 if (i, k) == (1, 0) else 20))
    write_node(path, "Flowers/Flower concept 0/Historical order.md", 3, "Historical order", "Flower concept 0",
               "Example of an order from 2025 that went wrong.", "word " * 400, type="example",
               extra={"truth": "historical"})
    write_node(path, "Flowers/Flower concept 0/Superseded rule.md", 3, "Superseded rule", "Flower concept 0",
               "A rule about roses that no longer holds.", "x " * 10, extra={"truth": "superseded"})
    write_node(path, "Flowers/Flower concept 0/Obsolete rule.md", 3, "Obsolete rule", "Flower concept 0",
               "A rule about carnations that was dropped.", "x " * 10, status="obsolete")
    write_node(path, "Flowers/Provisional lesson.md", 3, "Provisional lesson", "Flower concept 1",
               "A learned lesson about roses.", "x " * 10, status="provisional", extra={"learned_by": "agent"})
    # lint errors
    write_node(path, "Bad/Wrong type.md", 2, "Wrong type", "Flowers", "A concept with a type of another layer.",
               type="fact")
    write_node(path, "Bad/Misplaced.md", 3, "Misplaced", "Flowers", "A detail directly under a domain.")
    write_node(path, "Bad/Layer seven.md", 7, "Layer seven", "Flowers", "A node with a layer that does not exist.",
               type="fact")
    write_node(path, "Bad/Under concept.md", 2, "Under concept", "Flower concept 0", "A concept under a concept.")
    write_node(path, "Bad/Long summary.md", 3, "Long summary", "Flower concept 2", "word " * 40)
    write_node(path, "Bad/Orphan.md", 3, "Orphan", "Nonexistent parent", "A detail whose parent does not exist.")
    write_node(path, "_inbox/Pending note.md", 3, "Pending note", "Flower concept 3", "A note waiting in the inbox.")
    (path / "Flowers" / "Stray.md").write_text("no front matter here\n", encoding="utf-8")
    for q in ("roses carnations inventory", "cutoff deadline", "roses sold in dozens", "carnations dropped"):
        for _ in range(3):
            run(path, "recall", q)
            run(path, "credit", "--used", "Flower concept 0", "Order cutoff is at 2pm EST")
    run(path, "credit", "--used", "Flower detail 0-0", "Flower detail 0-1")


def make_perf(path: Path):
    new_vault(path, "Perf")
    for d in range(10):
        write_node(path, f"Domain {d}/Domain {d}.md", 1, f"Domain {d}", "MAP", f"Domain number {d}.",
                   "## Branches\n" + "".join(f"- [[Concept {d}-{c}]] — x\n" for c in range(10)))
        for c in range(10):
            write_node(path, f"Domain {d}/Concept {d}-{c}.md", 2, f"Concept {d}-{c}", f"Domain {d}",
                       f"Concept {c} of domain {d} about inventory and flowers.",
                       "## Details\n" + "".join(f"- [[Detail {d}-{c}-{k}]] — x\n" for k in range(9)))
            for k in range(9):
                write_node(path, f"Domain {d}/Concept {d}-{c}/Detail {d}-{c}-{k}.md", 3, f"Detail {d}-{c}-{k}",
                           f"Concept {d}-{c}", f"Detail {k} of concept {c}: roses, carnations and farms.")


def vaults(tmp: Path) -> dict:
    """name -> path of a fresh copy (or build) of each in-repo vault."""
    out = {}
    for ex in ("fern-cortex", "kitchen-cortex"):
        src = REPO / "examples" / ex
        if src.is_dir():
            shutil.copytree(src, tmp / ex)
            out[ex] = tmp / ex
    for dest, src in (("template", TEMPLATE), ):
        shutil.copytree(src, tmp / dest, ignore=shutil.ignore_patterns("scripts", "state", "generated", "__pycache__"),
                        dirs_exist_ok=True)
        (tmp / dest / "_brain" / "scripts").mkdir(parents=True, exist_ok=True)
        out[dest] = tmp / dest
    make_rich(tmp / "rich")
    out["rich"] = tmp / "rich"
    make_perf(tmp / "perf")
    out["perf"] = tmp / "perf"
    return out


def normalize(text: str, tmp: Path) -> str:
    text = text.replace(str(tmp), "<TMP>").replace(str(tmp).replace("\\", "/"), "<TMP>")
    text = re.sub(r"(?<![\d.])2\.\d+\.\d+(?![\d.])", "<VERSION>", text)
    text = text.replace("<TMP>\\", "<TMP>/")
    text = re.sub(r"system (windows|debian|linux|darwin|macos)\b", "system <SYS>", text)
    text = re.sub(r"Python 3\.\d+\.\d+", "Python <PY>", text)
    text = re.sub(r"\d+\.\d+ ?ms|\d+ ?ms", "<MS>", text)
    return text


GENERATED = ["MAP.md", "_brain/Panel.md", "_brain/Panel.html", "_brain/generated/index.tsv",
             "_brain/generated/graph.json", "_brain/generated/orbit.svg", "_brain/proposals.md"]


def reduce(rel: str, text: str) -> str:
    """The panel page is the template's code plus the vault's data: only the data is compared here (what the engine
    computes); the template's script is a different kind of check (a browser renders it before and after a change)."""
    if rel.endswith("Panel.html"):
        m = re.search(r'<script id="brain-data" type="application/json">(.*?)</script>', text, re.S)
        return m.group(1) if m else text
    return text


def capture() -> dict:
    """{vault: {label: text}} for every vault and command, normalized."""
    results = {}
    with tempfile.TemporaryDirectory(prefix="golden-") as t, fixed_world():
        tmp = Path(t).resolve()
        for name, path in vaults(tmp).items():
            r = results[name] = {}

            def step(label, *args):
                if "--session" in args:   # a fresh session per call, so no call meets a spent budget
                    mode = args[args.index("--mode") + 1] if "--mode" in args else "normal"
                    sid = run(path, "session", "new", "--mode", mode)[1].splitlines()[0]
                    args = tuple(sid if x == "golden" else x for x in args)
                c, o, e = run(path, *args)
                text = normalize(f"exit {c}\n--- stdout\n{o}\n--- stderr\n{e}", tmp)
                r[label] = re.sub(r"\d{8}-\d{6}-[0-9a-f]{8}", "<ID>", text)

            step("lint", "lint")
            step("build", "build")
            for rel in GENERATED[:-1]:
                f = path / rel
                if f.exists():
                    r["file " + rel] = reduce(rel, normalize(f.read_text(encoding="utf-8"), tmp))
            qs = QUERIES if name != "perf" else QUERIES[10:11]
            for q in qs:
                step("recall " + q, "recall", q, "--session", "golden")
            step("recall all", "recall", "inventory", "--all", "--no-body", "--session", "golden")
            for q in qs[:3]:
                step("recall deep " + q, "recall", q, "--mode", "deep", "--session", "golden")
            step("stats", "stats")
            step("ideas", "ideas", "--dry-run")
            step("consolidate", "consolidate")
            f = path / "_brain" / "proposals.md"
            if f.exists():
                r["file proposals"] = normalize(f.read_text(encoding="utf-8"), tmp)
            step("doctor", "doctor")
    return results


def digest() -> dict:
    return {v: {k: hashlib.sha256(text.encode("utf-8")).hexdigest() for k, text in d.items()}
            for v, d in capture().items()}


if __name__ == "__main__":
    if sys.argv[1:2] == ["capture"]:
        out = Path(sys.argv[2])
        for v, d in capture().items():
            for label, text in d.items():
                f = out / v / (re.sub(r"[^\w.-]+", "_", label)[:100] + ".txt")
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text, encoding="utf-8", newline="\n")
        print("written", out)
    elif sys.argv[1:2] == ["digest"]:
        print(json.dumps(digest(), indent=1, sort_keys=True))
    else:
        sys.exit(__doc__)
