#!/usr/bin/env python3
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""What one engine call costs: a new process per call (what cortex_mcp.py does today) against an engine imported once.

    python3 scripts/bench_calls.py --dir /tmp/bench            # inside the container: docker compose run --rm bench
    python3 scripts/bench_calls.py --dir /vaults/.bench -n 30   # on the bind-mounted vault folder

Builds a 1,000-node vault (10 domains · 100 concepts · 900 details, the shape of the engine's performance test) from
template-vault/ in --dir, then times recall and read both ways. Each timed call gets its own fresh session, made
outside the timing, so no call hits a budget. Prints median and p95 in milliseconds."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "template-vault"
QUERIES = ["roses farms inventory", "carnations from the farm", "inventory of domain 3",
           "which concept covers flowers", "detail about roses"]


def make_vault(v: Path) -> None:
    shutil.copytree(TEMPLATE / "_brain", v / "_brain")
    shutil.copytree(TEMPLATE / "_templates", v / "_templates")
    cfg = json.loads((v / "_brain/config.json").read_text(encoding="utf-8"))
    cfg["vault"] = "Bench"
    (v / "_brain/config.json").write_text(json.dumps(cfg), encoding="utf-8")


def write_node(brain, root: Path, rel: str, layer: int, title: str, up: str, summary: str, body: str) -> None:
    fm = {"id": brain.new_id(), "layer": layer, "type": brain.DEFAULT_TYPE[layer], "summary": summary,
          "up": f"[[{up}]]", "aliases": [], "status": "seed", "external": [], "created": brain.today(),
          "updated": brain.today()}
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brain.serialize_fm(fm) + "\n" + body + "\n\n## Connections\n", encoding="utf-8")


def fill(brain, v: Path) -> None:
    for d in range(10):
        write_node(brain, v, f"Domain {d}/Domain {d}.md", 1, f"Domain {d}", "MAP", f"Domain number {d}.",
                   "## Branches\n" + "".join(f"- [[Concept {d}-{c}]] — x\n" for c in range(10)))
        for c in range(10):
            write_node(brain, v, f"Domain {d}/Concept {d}-{c}.md", 2, f"Concept {d}-{c}", f"Domain {d}",
                       f"Concept {c} of domain {d} about inventory and flowers.",
                       "## Details\n" + "".join(f"- [[Detail {d}-{c}-{k}]] — x\n" for k in range(9)))
            for k in range(9):
                write_node(brain, v, f"Domain {d}/Concept {d}-{c}/Detail {d}-{c}-{k}.md", 3, f"Detail {d}-{c}-{k}",
                           f"Concept {d}-{c}", f"Detail {k} of concept {c}: roses, carnations and farms.", "")


def call(brain, argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        try:
            code = brain.main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code or 0, out.getvalue()


def stats(ms: list[float]) -> str:
    ms = sorted(ms)
    p95 = ms[min(len(ms) - 1, round(0.95 * (len(ms) - 1)))]
    return f"median {statistics.median(ms):7.1f} ms · p95 {p95:7.1f} ms"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", required=True, help="an empty or missing folder for the bench vault (it is replaced)")
    ap.add_argument("-n", type=int, default=20, help="timed calls per case")
    a = ap.parse_args()

    v = Path(a.dir).resolve() / "Bench"
    shutil.rmtree(v, ignore_errors=True)
    v.parent.mkdir(parents=True, exist_ok=True)
    make_vault(v)
    scripts = v / "_brain/scripts"
    sys.path.insert(0, str(scripts))
    import brain_core as brain  # the vault's own engine, as cortex_mcp.py runs it
    fill(brain, v)
    t0 = time.perf_counter()
    code, _ = call(brain, ["--vault", str(v), "build", "--quiet"])
    print(f"vault {v} · 1000 nodes · first build {time.perf_counter() - t0:.2f}s (exit {code})")

    def session() -> str:
        return call(brain, ["--vault", str(v), "session", "new"])[1].splitlines()[0].strip()

    def cases():
        for i in range(a.n):
            yield "recall", [QUERIES[i % len(QUERIES)]]
            yield "read", [f"Detail {i % 10}-{(i // 10) % 10}-{i % 9}"]

    times = {(mode, kind): [] for mode in ("process", "resident") for kind in ("recall", "read")}
    for kind, rest in cases():
        for mode in ("process", "resident"):
            argv = ["--vault", str(v), kind, *rest, "--session", session()]
            t0 = time.perf_counter()
            if mode == "process":
                code = subprocess.run([sys.executable, str(scripts / "brain.py"), *argv],
                                      capture_output=True, cwd=str(v.parent)).returncode
            else:
                code, _ = call(brain, argv)
            times[(mode, kind)].append((time.perf_counter() - t0) * 1000)
            if code:
                print(f"! {mode} {kind} {rest} exited {code}")

    for kind in ("recall", "read"):
        p, r = times[("process", kind)], times[("resident", kind)]
        print(f"{kind:6} process  {stats(p)}")
        print(f"{kind:6} resident {stats(r)} · {statistics.median(p) / statistics.median(r):.1f}x faster")
    return 0


if __name__ == "__main__":
    sys.exit(main())
