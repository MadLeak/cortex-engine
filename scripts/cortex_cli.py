#!/usr/bin/env python3
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Cortex from the command line, inside the container: nothing but Docker on the host.

    ./cortex.sh <command> ...        (Windows: .\\cortex.ps1 <command> ...)
    docker compose run --rm cli <command> ...

    list                              the vaults under the vault folder, their engine and their token file
    new <folder> [--agent A] [--from EXAMPLE] [--name N]
                                      a new vault from the template (EXAMPLE: fern-cortex, kitchen-cortex)
    brain <vault> <engine args...>    one engine call: brain kitchen recall "what do we preserve"
    doctor [<vault>]                  the engine's doctor, on one vault or all of them
    upgrade                           every older vault on this engine, committed as the Cortex hub
    token <vault> [--rotate]          the vault's MCP token (a new one with --rotate: the old one stops working)
    commit <vault> -m MESSAGE         a commit of everything changed, as you (CORTEX_GIT_NAME / CORTEX_GIT_EMAIL)
    precommit <vault>                 the vault's pre-commit check (what its git hook runs)

A vault is named by its folder or by the folder's first word (kitchen-cortex or kitchen). The vault folder is
/vaults inside the container: ~/Vaults on the host (CORTEX_VAULTS in .env).
"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
sys.path.insert(0, str(HERE))
import cortex_mcp  # noqa: E402

ROOT = Path(os.environ.get("CORTEX_ROOT", "/vaults"))


def vaults() -> dict[str, Path]:
    out = {}
    if ROOT.is_dir():
        for v in sorted(ROOT.iterdir()):
            if (v / "_brain/scripts/brain.py").is_file() and not v.name.startswith("."):
                out.setdefault(v.name.split("-")[0].split()[0].lower(), v)
                out[v.name] = v
    return out


def vault(name: str) -> Path:
    v = vaults().get(name) or vaults().get(name.lower())
    if v is None:
        names = sorted({x.name for x in vaults().values()}) or ["none yet"]
        sys.exit(f"no vault {name} under {ROOT}; there are: {', '.join(names)}")
    return v


def owner_env() -> dict:
    """The human's identity for the commits this CLI makes on their behalf."""
    name, email = os.environ.get("CORTEX_GIT_NAME", ""), os.environ.get("CORTEX_GIT_EMAIL", "")
    if not name or not email:
        sys.exit("set CORTEX_GIT_NAME and CORTEX_GIT_EMAIL in .env: commits made here carry your name")
    return {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


def engine(v: Path, *args: str) -> int:
    return subprocess.run([sys.executable, str(v / "_brain/scripts/brain.py"), "--vault", str(v), *args],
                          cwd=str(v.parent)).returncode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cortex", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("new")
    p.add_argument("folder")
    p.add_argument("--name")
    p.add_argument("--agent", default="the agent")
    p.add_argument("--from", dest="overlay")
    p = sub.add_parser("brain")
    p.add_argument("vault")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p = sub.add_parser("doctor")
    p.add_argument("vault", nargs="?")
    sub.add_parser("upgrade")
    p = sub.add_parser("token")
    p.add_argument("vault")
    p.add_argument("--rotate", action="store_true")
    p = sub.add_parser("commit")
    p.add_argument("vault")
    p.add_argument("-m", "--message", required=True)
    p = sub.add_parser("precommit")
    p.add_argument("vault")
    a = ap.parse_args(argv)

    if a.cmd == "list":
        seen = set()
        for v in vaults().values():
            if v in seen:
                continue
            seen.add(v)
            m = cortex_mcp.VERSION_LINE.search((v / "_brain/scripts/brain_core.py").read_text(encoding="utf-8"))
            tok = "token" if cortex_mcp.token_path(v).is_file() else "no token yet"
            print(f"{v.name:28} engine v{m.group(1) if m else '?':8} {tok}")
        return 0
    if a.cmd == "new":
        dest = ROOT / a.folder
        if dest.exists():
            sys.exit(f"{dest} already exists")
        cmd = [sys.executable, str(ENGINE / "scripts/new_vault.py"), str(dest), "--agent", a.agent,
               "--owner", os.environ.get("CORTEX_OWNER", "the owner")]
        if a.name:
            cmd += ["--name", a.name]
        if a.overlay:
            src = ENGINE / "examples" / a.overlay
            if not src.is_dir():
                have = sorted(x.name for x in (ENGINE / "examples").iterdir()) if (ENGINE / "examples").is_dir() else []
                sys.exit(f"no example {a.overlay}; there are: {', '.join(have) or 'none'}")
            cmd += ["--from", str(src)]
        for flag, var in (("--git-name", "CORTEX_GIT_NAME"), ("--git-email", "CORTEX_GIT_EMAIL")):
            if os.environ.get(var):
                cmd += [flag, os.environ[var]]
        r = subprocess.run(cmd)
        if r.returncode == 0:
            cortex_mcp.ensure_token(dest)
            print(f"MCP token in {a.folder}/{cortex_mcp.TOKEN_IGNORE}")
        return r.returncode
    if a.cmd == "brain":
        return engine(vault(a.vault), *a.args)
    if a.cmd == "doctor":
        targets = [vault(a.vault)] if a.vault else sorted(set(vaults().values()))
        return max([engine(v, "doctor") for v in targets] or [0])
    if a.cmd == "upgrade":
        import cortex_hub
        want = cortex_mcp.VERSION_LINE.search(
            (ENGINE / "template-vault/_brain/scripts/brain_core.py").read_text(encoding="utf-8")).group(1)
        lines = cortex_hub.upgrade_engines(ROOT, want)
        print("\n".join(lines) or f"every vault already runs engine v{want}")
        return 0
    if a.cmd == "token":
        v = vault(a.vault)
        if a.rotate:
            cortex_mcp.token_path(v).parent.mkdir(parents=True, exist_ok=True)
            cortex_mcp.token_path(v).write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
        print(cortex_mcp.ensure_token(v))
        return 0
    if a.cmd == "commit":
        v, env = vault(a.vault), owner_env()
        if subprocess.run(["git", "-C", str(v), "rev-parse", "--git-dir"], capture_output=True).returncode != 0:
            sys.exit(f"{v.name} is not a git repository: create vaults with `new`, which makes one")
        cortex_mcp.fix_filemode(v)
        subprocess.run(["git", "-C", str(v), "add", "-A"], check=True)
        return subprocess.run(["git", "-C", str(v), "commit", "-m", a.message], env=env).returncode
    if a.cmd == "precommit":
        return engine(vault(a.vault), "precommit")
    return 2


if __name__ == "__main__":
    sys.exit(main())
