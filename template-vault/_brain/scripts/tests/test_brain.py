# -*- coding: utf-8 -*-
"""Engine tests. Run: python -m unittest discover -s _brain/scripts/tests -v"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import brain  # noqa: E402

TEMPLATES = HERE.parents[2] / "_templates"


def run(root, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = brain.main(["--vault", str(root), *args])
    return code, out.getvalue(), err.getvalue()


def make_vault(path: Path, name: str, siblings: dict | None = None):
    (path / "_brain").mkdir(parents=True)
    cfg = {"vault": name, "siblings": siblings or {}, "warn_dependency_on": list(siblings or {})}
    (path / "_brain" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    if TEMPLATES.exists():
        shutil.copytree(TEMPLATES, path / "_templates")
    for c in brain.COLD_FOLDERS:
        (path / c).mkdir()


def write_node(root: Path, rel: str, layer: int, title: str, up: str, summary: str, body: str = "", type=None):
    fm = {"id": brain.new_id(), "layer": layer, "type": type or brain.DEFAULT_TYPE[layer], "summary": summary,
          "up": f"[[{up}]]", "aliases": [], "status": "seed", "external": [], "created": brain.today(),
          "updated": brain.today()}
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brain.serialize_fm(fm) + "\n" + body + "\n\n## Connections\n", encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="brain-"))
        self.v = self.tmp / "Test"
        make_vault(self.v, "Test", {"Other": "../Other"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ok(self, *args, vault=None):
        c, o, e = run(vault or self.v, *args)
        self.assertEqual(c, 0, f"{args}\n{o}\n{e}")
        return o

    def code(self, *args):
        return run(self.v, *args)[0]

    def seed(self):
        self.ok("new", "--layer", "1", "--title", "Orders", "--summary", "Daily order cycle: intake, cutoff and dispatch.")
        self.ok("new", "--layer", "2", "--up", "Orders", "--title", "Order cutoff is at 2pm EST",
                "--summary", "The daily cutoff decides which orders enter the dispatch wave.", "--aliases", "cutoff time", "deadline")
        self.ok("new", "--layer", "3", "--up", "Order cutoff is at 2pm EST", "--type", "error",
                "--title", "Late orders went to the wrong wave", "--summary", "Fixed error: the server clock was in UTC.")
        self.ok("new", "--layer", "2", "--up", "Orders", "--title", "The shipping window limits the cutoff",
                "--summary", "The carrier's shipping window sets the latest possible cutoff.")


class StructureTests(Base):
    def test_count_tokens_is_deterministic_estimate(self):
        self.assertEqual(brain.count_tokens(""), 0)
        self.assertEqual(brain.count_tokens("abcd"), 2)
        self.assertEqual(brain.count_tokens("one two three four"), 6)

    def test_token_index_graph_and_stats(self):
        self.seed()
        self.ok("build")
        lines = (self.v / "_brain" / "generated" / "index.tsv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0].split("\t")[-1], "tokens")
        self.assertTrue(all(len(line.split("\t")) == len(lines[0].split("\t")) for line in lines))
        graph = json.loads((self.v / "_brain" / "generated" / "graph.json").read_text(encoding="utf-8"))
        self.assertTrue(all("tokens" in n for n in graph["nodes"]))
        self.assertIn("tokens:", self.ok("stats"))

    def test_new_places_and_links(self):
        self.seed()
        self.assertTrue((self.v / "Orders" / "Orders.md").exists())
        self.assertTrue((self.v / "Orders" / "Order cutoff is at 2pm EST.md").exists())
        self.assertTrue((self.v / "Orders" / "Order cutoff is at 2pm EST" / "Late orders went to the wrong wave.md").exists())
        parent = (self.v / "Orders" / "Orders.md").read_text(encoding="utf-8")
        self.assertIn("- [[Order cutoff is at 2pm EST]] — ", parent)
        self.assertEqual(self.code("lint"), 0)

    def test_learn_needs_a_contract_when_current(self):
        self.seed()
        base = ("learn", "--up", "Order cutoff is at 2pm EST", "--title", "Cutoff moved to 3pm on Fridays",
                "--summary", "Friday orders close one hour later.", "--content", "On Fridays the cutoff is 3pm.",
                "--evidence", "Seen in the Friday wave config.")
        self.assertEqual(self.code(*base), 1)
        self.assertEqual(self.code(*base, "--verify-intent", "config", "--verify", "chmod 777 /"), 1)
        self.assertEqual(self.code(*base, "--verify-intent", "config", "--verify-keywords", "cutoff", "friday",
                                   "--verify", "key waves.friday.cutoff in ~/waves.yaml = 15:00", "--system", "ubuntu"), 0)
        text = next(self.v.rglob("Cutoff moved to 3pm on Fridays.md")).read_text(encoding="utf-8")
        self.assertIn("verify_intent: config", text)
        self.assertIn('  - "key waves.friday.cutoff in ~/waves.yaml = 15:00"', text)
        self.assertIn("system:\n  - ubuntu", text)
        self.assertEqual(self.code("lint"), 0)

    def test_a_learning_with_no_concept_is_captured_in_the_inbox(self):
        """A new vault has no L2 to hang a learning from: it goes to _inbox, checked, instead of being lost."""
        base = ("learn", "--up", "Preferences", "--title", "The owner prefers VS Code",
                "--summary", "VS Code is the owner's editor.", "--content", "The owner edits everything in VS Code.",
                "--evidence", "The owner said so on 2026-11-19.", "--truth", "historical", "--by", "claude")
        out = self.ok(*base)
        self.assertIn("captured _inbox/", out)
        self.assertIn("cortex_curate verb=create", out)
        (note,) = list((self.v / "_inbox").glob("* The owner prefers VS Code.md"))
        text = note.read_text(encoding="utf-8")
        for part in ("learned_by: claude", 'wanted_up: "Preferences"', "## Content", "## Evidence", "truth: historical"):
            self.assertIn(part, text)
        self.assertEqual(self.code("lint"), 0)
        self.assertEqual(self.code("learn", "--up", "Preferences", "--title", "A key", "--summary", "The api key.",
                                   "--content", "api_key = sk-abcdefghijklmnopqrstuvwxyz123456",
                                   "--evidence", "config file", "--truth", "historical"), 1, "secrets still refused")
        self.seed()   # with an L1 under the hand: an L1 is not a concept to learn under either
        self.assertIn("captured _inbox/", self.ok("learn", "--up", "Orders", "--title", "Orders spike on Mondays",
                                                  "--summary", "Monday has the most orders.", "--content", "x y",
                                                  "--evidence", "the dashboard", "--truth", "historical"))

    def test_lint_rejects_a_malformed_contract(self):
        self.seed()
        write_node(self.v, "Orders/Ops.md", 2, "Ops", "Orders", "Ops runbook.")
        path = self.v / "Orders" / "Ops.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("\n---", '\nverify_intent: guess\nverify:\n  - "ask someone"\n---', 1), encoding="utf-8")
        out = run(self.v, "lint")[1]
        self.assertIn('verify_intent "guess"', out)
        self.assertIn("verify check in no known form", out)

    def test_recall_marks_and_ranks_another_systems_note(self):
        self.seed()
        write_node(self.v, "Orders/Windows label printer.md", 2, "Windows label printer", "Orders",
                   "The label printer prints order labels from the spooler.", "Order labels print through the spooler.")
        write_node(self.v, "Orders/Ubuntu label printer.md", 2, "Ubuntu label printer", "Orders",
                   "The label printer prints order labels through CUPS.", "Order labels print through CUPS.")
        path = self.v / "Orders" / "Windows label printer.md"
        path.write_text(path.read_text(encoding="utf-8").replace("\n---", "\nsystem: [windows]\n---", 1), encoding="utf-8")
        import os
        os.environ["CORTEX_SYSTEM"] = "ubuntu"
        try:   # with no twin here, the windows note keeps its rank: it is still the best answer, marked
            out = run(self.v, "recall", "label printer order labels spooler", "--no-body")[1]
        finally:
            del os.environ["CORTEX_SYSTEM"]
        self.assertLess(out.index("Windows label printer"), out.index("Ubuntu label printer"))
        twin = self.v / "Orders" / "Ubuntu label printer.md"
        twin.write_text(twin.read_text(encoding="utf-8").replace("\n---", "\nsystem: [ubuntu]\n---", 1), encoding="utf-8")
        self.ok("link", "Ubuntu label printer", "Windows label printer", "--type", "related", "--reason", "its twin on windows")
        os.environ["CORTEX_SYSTEM"] = "ubuntu"
        try:
            out = run(self.v, "recall", "label printer order labels", "--no-body")[1]
        finally:
            del os.environ["CORTEX_SYSTEM"]
        self.assertIn("system ubuntu", out)
        self.assertLess(out.index("Ubuntu label printer"), out.index("Windows label printer"))
        self.assertIn("Windows label printer (WINDOWS ONLY)", out)
        self.assertEqual(self.code("lint"), 0)

    def test_forbidden_and_duplicate_title(self):
        self.seed()
        self.assertEqual(self.code("new", "--layer", "1", "--title", "What is it?", "--summary", "x"), 1)
        self.assertEqual(self.code("new", "--layer", "1", "--title", "orders", "--summary", "duplicate ignoring case"), 1)
        self.assertEqual(self.code("new", "--layer", "3", "--up", "Orders", "--title", "Detail hung in the wrong place", "--summary", "x"), 1)

    def test_address_check_skips_hosts_and_allowed(self):
        self.seed()
        write_node(self.v, "Orders/Ops.md", 2, "Ops", "Orders", "Ops runbook.",
                   "Run ssh root@server.local or root@192.0.2.10 with uv@0.13.0; invoices come from bills@example.com.")
        self.assertIn("possible personal data", run(self.v, "lint")[1])
        cfg = json.loads((self.v / "_brain" / "config.json").read_text(encoding="utf-8"))
        cfg["allowed_addresses"] = {"bills@example.com": "a company's invoice sender"}
        (self.v / "_brain" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.assertNotIn("possible personal data", run(self.v, "lint")[1])

    def test_lint_blocks(self):
        self.seed()
        write_node(self.v, "Orders/No summary.md", 2, "No summary", "Orders", "")
        self.assertEqual(self.code("lint"), 1)
        (self.v / "Orders" / "No summary.md").unlink()
        self.assertEqual(self.code("lint"), 0)
        path = self.v / "Orders" / "The shipping window limits the cutoff.md"
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "\nconnection: Server=db01;User Id=app;Password=Sup3r!\n", encoding="utf-8")
        self.assertEqual(self.code("lint"), 1, "a secret must block")
        path.write_text(original + "\nla contraseña: Sup3r!x\n", encoding="utf-8")
        self.assertEqual(self.code("lint"), 1, "a secret written in Spanish must block too")
        path.write_text(original + "- related:: [[Orders]]\n", encoding="utf-8")
        self.assertEqual(self.code("lint"), 1, "a synapse without a reason must block")
        path.write_text(original, encoding="utf-8")
        for i in range(8):
            write_node(self.v, f"Orders/Target {i}.md", 2, f"Target {i}", "Orders", f"Target number {i}.")
        lines = "".join(f"- related:: [[Target {i}]] — cap test\n" for i in range(8))
        path.write_text(original + lines, encoding="utf-8")
        _, output, _ = run(self.v, "lint")
        self.assertIn("8 synapses (max 7)", output)
        self.assertEqual(self.code("link", "The shipping window limits the cutoff", "Orders", "--type", "causes", "--reason", "x"), 1)

    def test_tools_region(self):
        self.seed()
        self.ok("new", "--layer", "1", "--type", "tools", "--title", "Test Tools",
                "--summary", "The agent's limbs: one concept per tool it uses.")
        self.ok("new", "--layer", "2", "--type", "tool", "--up", "Test Tools", "--title", "Shell tool",
                "--summary", "Runs commands; its lessons hang below it.")
        self.ok("new", "--layer", "3", "--type", "error", "--up", "Shell tool", "--title", "Quotes broke a heredoc",
                "--summary", "An escaped quote in a heredoc ended it early; write the script to a file.")
        self.ok("new", "--layer", "2", "--type", "tool", "--up", "Orders", "--title", "Order scanner",
                "--summary", "Scans the order labels at the dock.")
        code, output, _ = run(self.v, "lint")
        self.assertEqual(code, 0, output)
        flagged = [l for l in output.splitlines() if "Tools domain" in l]
        self.assertEqual(len(flagged), 1, output)
        self.assertIn("Order scanner", flagged[0])
        self.ok("build")
        graph = json.loads((self.v / "_brain" / "generated" / "graph.json").read_text(encoding="utf-8"))
        types = {n["title"]: n["type"] for n in graph["nodes"]}
        self.assertEqual((types["Test Tools"], types["Shell tool"]), ("tools", "tool"))

    def test_lint_warns_not_english(self):
        self.seed()
        self.ok("new", "--layer", "2", "--up", "Orders", "--title", "El corte de pedidos es a las 14h EST",
                "--summary", "El corte diario define qué pedidos entran en la ola de despacho.")
        self.ok("new", "--layer", "2", "--up", "Orders", "--title", "PicaflorDB stores Sunshine Bouquet orders",
                "--summary", "Sunshine Bouquet keeps every order in PicaflorDB, its order database.")
        self.ok("new", "--layer", "2", "--up", "Orders", "--title", "Peñalosa farm ships on Tuesdays",
                "--summary", "Peñalosa only ships to the hub on Tuesdays.", "--aliases", "Peñalosa")
        code, output, _ = run(self.v, "lint")
        self.assertEqual(code, 0, "not_english is a warning, never an error")
        flagged = [l for l in output.splitlines() if "not_english" in l]
        self.assertEqual(len(flagged), 1, output)
        self.assertTrue(flagged[0].startswith("WARN"), flagged[0])
        self.assertIn("El corte de pedidos es a las 14h EST.md", flagged[0])
        _, quiet, _ = run(self.v, "lint", "--quiet")
        self.assertNotIn("not_english", quiet)

    def test_link_unlink(self):
        self.seed()
        self.ok("link", "Order cutoff is at 2pm EST", "The shipping window limits the cutoff",
                "--type", "depends_on", "--reason", "the cutoff is computed backwards from the window")
        self.assertEqual(self.code("link", "Order cutoff is at 2pm EST", "The shipping window limits the cutoff",
                                   "--type", "causes", "--reason", "duplicate"), 1)
        self.assertEqual(self.code("lint"), 0)
        self.ok("unlink", "Order cutoff is at 2pm EST", "The shipping window limits the cutoff")

    def test_rename_and_move(self):
        self.seed()
        self.ok("rename", "Order cutoff is at 2pm EST", "The daily cutoff is at 2pm EST")
        self.assertTrue((self.v / "Orders" / "The daily cutoff is at 2pm EST.md").exists())
        self.assertTrue((self.v / "Orders" / "The daily cutoff is at 2pm EST").is_dir())
        detail = (self.v / "Orders" / "The daily cutoff is at 2pm EST" / "Late orders went to the wrong wave.md").read_text(encoding="utf-8")
        self.assertIn('up: "[[The daily cutoff is at 2pm EST]]"', detail)
        self.ok("new", "--layer", "1", "--title", "Dispatch", "--summary", "Physical departure of orders to the carriers.")
        self.ok("move", "The shipping window limits the cutoff", "--up", "Dispatch")
        self.assertTrue((self.v / "Dispatch" / "The shipping window limits the cutoff.md").exists())
        self.assertNotIn("The shipping window", (self.v / "Orders" / "Orders.md").read_text(encoding="utf-8"))
        self.assertIn("The shipping window", (self.v / "Dispatch" / "Dispatch.md").read_text(encoding="utf-8"))
        self.ok("move", "Dispatch", "--up", "Orders")
        self.assertTrue((self.v / "Orders" / "Dispatch" / "Dispatch.md").exists())
        self.assertEqual(self.code("lint"), 0)


class MobilityTests(Base):
    def test_promote_and_demote(self):
        self.seed()
        self.ok("promote", "Late orders went to the wrong wave")
        self.assertTrue((self.v / "Orders" / "Late orders went to the wrong wave.md").exists())
        self.assertEqual(self.code("lint"), 0)
        self.ok("demote", "Late orders went to the wrong wave", "--up", "Order cutoff is at 2pm EST", "--type", "error")
        self.assertEqual(self.code("lint"), 0)
        self.ok("promote", "Order cutoff is at 2pm EST")
        sub = self.v / "Orders" / "Order cutoff is at 2pm EST"
        self.assertTrue((sub / "Order cutoff is at 2pm EST.md").exists())
        detail = (sub / "Late orders went to the wrong wave.md").read_text(encoding="utf-8")
        self.assertIn("layer: 2", detail)
        self.assertIn("## Branches", (sub / "Order cutoff is at 2pm EST.md").read_text(encoding="utf-8"))
        self.assertEqual(self.code("lint"), 0)


class ReadTests(Base):
    def populate(self, n=30):
        self.seed()
        for i in range(n):
            self.ok("new", "--layer", "3", "--up", "Order cutoff is at 2pm EST", "--title", f"Cutoff case number {i}",
                    "--summary", f"Example {i} of orders that arrived close to the dispatch cutoff.")

    def test_recall_respects_budget(self):
        self.populate()
        _, output, _ = run(self.v, "recall", "order cutoff dispatch")
        body = [l for l in output.splitlines() if not l.startswith("#")]
        self.assertLessEqual(len(body), brain.MODES["normal"]["lines"])
        self.assertLessEqual(sum(len(l) + 1 for l in body), brain.MODES["normal"]["chars"])
        self.assertIn("[L2] Order cutoff is at 2pm EST", output)

    def test_recall_alias_and_accents(self):
        self.seed()
        _, output, _ = run(self.v, "recall", "deadline", "--mode", "quick")
        self.assertIn("Order cutoff", output)
        _, output, _ = run(self.v, "recall", "SHIPPÍNG WÍNDOW")
        self.assertIn("The shipping window", output)

    def test_concept_inherits_part_of_its_detail_match(self):
        self.seed()
        ranked = [n.title for _, n in brain.score(brain.Vault(self.v), "server clock UTC", {})]
        self.assertEqual(ranked[:2], ["Late orders went to the wrong wave", "Order cutoff is at 2pm EST"])
        self.assertNotIn("Orders", ranked, "a hub inherits nothing: it would outrank every concept")

    def test_spanish_query_is_translated(self):
        self.seed()
        (self.v / "_brain" / "glossary.json").write_text(json.dumps(
            {"stop": "el la de que", "es": {"hora de corte": "cutoff", "pedidos": "orders"}}), encoding="utf-8")
        v = brain.Vault(self.v)
        self.assertEqual(brain.translate_query(v, "¿Cuál es la hora de corte de los pedidos del 6 de octubre?"),
                         "cutoff orders 10-06", "Spanish function words drop even with no vault stop list")
        self.assertEqual(brain.translate_query(v, "order cutoff"), "order cutoff", "English passes untouched")
        _, output, _ = run(self.v, "recall", "¿Cuál es la hora de corte de los pedidos?")
        self.assertIn("# searched as:", output)
        self.assertIn("[L2] Order cutoff is at 2pm EST", output)

    def test_typo_and_spanish_stem(self):
        self.seed()
        v = brain.Vault(self.v)
        self.assertEqual(brain.score(v, "shiping windoow", {})[0][1].title, "The shipping window limits the cutoff",
                         "a word the vault never uses is corrected to the nearest one it does")
        self.assertEqual(brain.es_stem("ejecutando"), brain.es_stem("ejecutar"))
        self.assertIn("order", brain.translate_query(v, "¿Qué pasa con los pedidos atrasados?").split(),
                      "a Spanish word matches the base lexicon by its stem")

    def test_snowball_stems_plurals_and_inflections(self):
        self.assertEqual(brain.word_stem("deploys"), brain.word_stem("deploy"))
        self.assertEqual(brain.word_stem("delegating"), brain.word_stem("delegated"))
        self.assertEqual(brain.es_stem("configuraciones"), brain.es_stem("configuración".replace("ó", "o")))

    def test_generated_questions_import_and_rank(self):
        self.seed()
        v = brain.Vault(self.v)
        late = next(n for n in v.net.values() if n.title == "Late orders went to the wrong wave")
        self.assertIn("0 of 4 nodes · 0 stale · 4 missing", self.ok("questions", "status"))
        todo = self.ok("questions", "todo", "--limit", "2")
        self.assertIn("### ID:", todo)
        self.assertIn("2 of 4 nodes to do", todo)
        answer = self.tmp / "answer.jsonl"
        answer.write_text("\n".join([
            json.dumps({"id": late.key, "en": ["why did parcels miss the truck"], "es": ["por qué los paquetes perdieron el camión"]}),
            json.dumps({"id": "no-such-node", "en": ["x"]}),
            json.dumps({"id": late.key, "en": ["key ghp_" + "a1" * 15]}),
        ]) + "\n", encoding="utf-8")
        out = self.ok("questions", "import", str(answer))
        self.assertIn("unknown id", out)
        self.assertIn("looks like a GitHub token", out)
        self.assertIn("1 nodes imported", out)
        self.assertIn("1 of 4 nodes", self.ok("questions", "status"))
        v = brain.Vault(self.v)
        self.assertEqual(brain.score(v, "parcels missed the truck", {})[0][1].title, late.title,
                         "the generated questions are searched, and a new questions file rebuilds the cached index")
        self.assertEqual(brain.score(v, "¿Por qué los paquetes perdieron el camión?", {})[0][1].title, late.title,
                         "a Spanish query meets the Spanish questions untranslated")
        path = self.v / "Orders" / "Order cutoff is at 2pm EST" / "Late orders went to the wrong wave.md"
        path.write_text(path.read_text(encoding="utf-8").replace("\n## Connections", "The clock now runs on EST.\n\n## Connections"),
                        encoding="utf-8")
        self.assertIn("1 stale", self.ok("questions", "status"), "an edited node's questions are marked stale")

    def test_detail_over_parent_on_equal_match(self):
        self.seed()
        ranked = [n.title for _, n in brain.score(brain.Vault(self.v), "late orders wrong wave", {})]
        self.assertEqual(ranked[0], "Late orders went to the wrong wave")

    def test_superseded_detail_stays_below_its_concept(self):
        self.seed()
        self.ok("new", "--layer", "3", "--up", "Order cutoff is at 2pm EST", "--type", "fact",
                "--title", "The cutoff was at noon EST", "--summary", "Until spring the order cutoff was at noon EST.")
        self.ok("link", "--type", "supersedes", "--reason", "the cutoff moved to 2pm",
                "Order cutoff is at 2pm EST", "The cutoff was at noon EST")
        ranked = [n.title for _, n in brain.score(brain.Vault(self.v), "order cutoff EST", {})]
        self.assertEqual(ranked[0], "Order cutoff is at 2pm EST", "the fact that holds today answers first")
        self.assertLess(ranked.index("Order cutoff is at 2pm EST"), ranked.index("The cutoff was at noon EST"))

    def test_a_historical_node_keeps_its_weight_when_superseded(self):
        self.seed()
        self.ok("new", "--layer", "3", "--up", "Order cutoff is at 2pm EST", "--type", "fact",
                "--title", "The cutoff was at noon EST", "--summary", "Until spring the order cutoff was at noon EST.")
        self.ok("link", "--type", "supersedes", "--reason", "the cutoff moved to 2pm",
                "Order cutoff is at 2pm EST", "The cutoff was at noon EST")
        v = brain.Vault(self.v)
        old = next(n for n in v.net.values() if n.title == "The cutoff was at noon EST")
        self.assertEqual(brain.truth_factor(v, old), brain.TRUTH_FACTOR["superseded"])
        old.fm["truth"] = "historical"
        self.assertEqual(brain.truth_factor(v, old), 1.0, "a dated record answers questions about its date")

    def test_gate_hands_the_body_only_when_covered(self):
        self.seed()
        _, output, _ = run(self.v, "recall", "shipping window cutoff")
        self.assertIn("# answer: [L2] The shipping window limits the cutoff", output)
        self.assertIn("likely the answer", output)
        _, output, _ = run(self.v, "recall", "dispatch")
        self.assertNotIn("# answer:", output, "one matched word is not enough to hand over a body")
        v = brain.Vault(self.v)
        sure, _ = brain.gate_probability(v, "shipping window cutoff", brain.score(v, "shipping window cutoff", {}))
        unsure, _ = brain.gate_probability(v, "dispatch", brain.score(v, "dispatch", {}))
        self.assertGreater(sure, unsure)
        self.assertTrue(0 < unsure < brain.GATE["threshold"] <= sure < 1)

    def test_read_hands_over_the_matching_part(self):
        self.seed()
        filler = " ".join(f"Routine line {i} about intake paperwork and label printing." for i in range(12))
        write_node(self.v, "Orders/Carrier pickup.md", 2, "Carrier pickup", "Orders", "When the carrier collects.",
                   "The carrier collects at 4pm from dock B.\n\n" + filler + "\n\nA missed pickup is rebooked by phone before 5pm.")
        sid = run(self.v, "session", "new")[1].splitlines()[0]
        self.ok("recall", "missed pickup rebooked", "--session", sid, "--no-body")
        out = self.ok("read", "Carrier pickup", "--session", sid)
        self.assertIn("rebooked by phone", out)
        self.assertIn("collects at 4pm", out, "the lead sentence always stays")
        self.assertNotIn("Routine line 5", out)
        self.assertIn("# excerpt:", out)
        full = self.ok("read", "Carrier pickup", "--full", "--session", sid)
        self.assertIn("Routine line 5", full, "the rest of an excerpt comes on request, outside the budget")
        self.assertIn("tok", full.splitlines()[0])
        session = brain.Sessions(brain.Vault(self.v)).load(sid)
        self.assertEqual(session["tokens_read"], brain.count_tokens("\n".join(brain.body_lines(brain.Vault(self.v).node("Carrier pickup")))))
        self.assertEqual(self.code("read", "Carrier pickup", "--full", "--session", sid), 2, "but only once")

    def test_tokens_read_accumulate_in_session_and_state(self):
        self.seed()
        sid = run(self.v, "session", "new")[1].splitlines()[0]
        out = self.ok("read", "Order cutoff is at 2pm EST", "--session", sid)
        self.assertRegex(out, r"# budget: .* · \d+ tok read")
        key = brain.Vault(self.v).node("Order cutoff is at 2pm EST").key
        state = json.loads((self.v / "_brain" / "state" / "tokens_read.json").read_text(encoding="utf-8"))
        self.assertGreater(state[key], 0)
        self.assertEqual(brain.Sessions(brain.Vault(self.v)).load(sid)["tokens_read"], state[key])

    def test_read_budget_and_visited(self):
        self.populate(8)
        sid = run(self.v, "session", "new")[1].splitlines()[0]
        self.ok("recall", "cutoff case", "--session", sid, "--no-body")
        for i in range(5):
            self.ok("read", f"Cutoff case number {i}", "--session", sid)
        self.assertEqual(self.code("read", "Cutoff case number 5", "--session", sid), 2, "the 6th body must be refused")
        self.assertEqual(self.code("read", "Cutoff case number 0", "--session", sid), 2, "a re-read must be refused")
        self.ok("recall", "another question about the cutoff", "--session", sid)
        self.ok("read", "Cutoff case number 5", "--session", sid)
        self.assertEqual(self.code("read", "Orders", "--session", "s-made-up"), 1)

    def test_cold_needs_reason(self):
        self.seed()
        (self.v / "_sources" / "Long manual.md").write_text("source text\n", encoding="utf-8")
        sid = run(self.v, "session", "new")[1].splitlines()[0]
        self.assertEqual(self.code("read", "Long manual", "--session", sid), 1)
        self.ok("read", "Long manual", "--session", sid, "--cold", "verify the original source")
        _, output, _ = run(self.v, "recall", "long manual")
        self.assertNotIn("Long manual —", output)


class LearningTests(Base):
    def test_hebbian_and_weights(self):
        self.seed()
        for _ in range(3):
            self.ok("credit", "--used", "Order cutoff is at 2pm EST", "Late orders went to the wrong wave",
                    "The shipping window limits the cutoff")
        self.ok("consolidate")
        proposals = (self.v / "_brain" / "proposals.md").read_text(encoding="utf-8")
        self.assertIn("synapse :: ", proposals)
        weights = brain.Telemetry(brain.Vault(self.v)).weights()
        self.assertAlmostEqual(max(weights.values()), 1.0)

    def test_repeated_used_flag_credits_every_title(self):
        self.seed()
        out = self.ok("credit", "--used", "Order cutoff is at 2pm EST", "--used", "Late orders went to the wrong wave")
        self.assertIn("credited 2 nodes", out)

    def test_incremental_weights_match_the_log(self):
        self.seed()
        tel = brain.Telemetry(brain.Vault(self.v))
        tel.weights()  # the running sums start here; the next calls only advance over the new lines
        for _ in range(2):
            self.ok("credit", "--used", "Order cutoff is at 2pm EST", "Late orders went to the wrong wave")
        self.ok("recall", "shipping window")
        tel = brain.Telemetry(brain.Vault(self.v))
        fast, full = tel.weights(), tel.weights_from_log()
        for k in full:
            self.assertAlmostEqual(fast[k], full[k], places=6)
        co, co_full = tel.coactivation(), tel.coactivation_from_log()
        self.assertEqual(set(co), set(co_full))

    def test_automatic_session_and_credit(self):
        self.seed()
        out = self.ok("recall", "cutoff")
        self.assertNotIn("--session", out.splitlines()[-1], "no session to pass any more")
        self.ok("read", "Late orders went to the wrong wave")
        ptr = self.v / "_brain" / "state" / "sessions" / "auto.json"
        cur = json.loads(ptr.read_text(encoding="utf-8"))
        cur["touched"] -= brain.AUTO_IDLE + 1
        ptr.write_text(json.dumps(cur), encoding="utf-8")
        out = self.ok("recall", "shipping")
        self.assertIn('credited automatically: "Late orders went to the wrong wave"', out)
        self.assertGreater(brain.Telemetry(brain.Vault(self.v)).count("used").get(
            brain.Vault(self.v).node("Late orders went to the wrong wave").key, 0), 0)

    def test_session_status_shows_the_automatic_session_without_keeping_it_alive(self):
        self.seed()
        self.assertIn("no session open", self.ok("session", "status"))
        self.ok("recall", "cutoff")
        ptr = self.v / "_brain" / "state" / "sessions" / "auto.json"
        before = json.loads(ptr.read_text(encoding="utf-8"))
        out = self.ok("session", "status")
        self.assertIn(f"session {before['sid']}", out)
        self.assertIn("queries 1/", out)
        self.assertEqual(json.loads(ptr.read_text(encoding="utf-8")), before, "a look does not touch the pointer")
        before["touched"] -= brain.AUTO_IDLE + 1
        ptr.write_text(json.dumps(before), encoding="utf-8")
        self.assertIn("no session open", self.ok("session", "status"))

    def test_a_task_session_lasts_until_it_is_closed(self):
        """session new --by opens the agent's task session: its calls without --session use it, with no idle, and
        session close (or the agent's next session new) ends it with the automatic credit."""
        self.seed()
        sid = self.ok("session", "new", "--by", "Claude Code").splitlines()[0]
        self.ok("recall", "cutoff", "--by", "Claude Code")
        self.ok("read", "Late orders went to the wrong wave", "--by", "Claude Code")
        ses = self.v / "_brain" / "state" / "sessions"
        data = json.loads((ses / f"{sid}.json").read_text(encoding="utf-8"))
        self.assertEqual((data["queries"], data["by"]), (1, "Claude Code"))
        self.assertTrue((ses / "task-claude-code.json").exists())
        self.assertIn(f"session {sid}", self.ok("session", "status", "--by", "Claude Code"))
        other = self.ok("recall", "shipping", "--by", "codex")          # another agent: its own fallback session
        self.assertTrue((ses / "auto-codex.json").exists())
        self.assertEqual(json.loads((ses / f"{sid}.json").read_text(encoding="utf-8"))["queries"], 1, other)
        out = self.ok("session", "close", "--by", "Claude Code")
        self.assertIn('credited automatically: "Late orders went to the wrong wave"', out)
        self.assertFalse((ses / "task-claude-code.json").exists())
        self.assertIn("closed", self.ok("session", "status", "--session", sid))
        self.assertIn("no task session open", self.ok("session", "close", "--by", "Claude Code"))

    def test_the_next_task_closes_the_previous_one(self):
        self.seed()
        first = self.ok("session", "new", "--by", "codex").splitlines()[0]
        self.ok("read", "Late orders went to the wrong wave", "--by", "codex")
        out = self.ok("session", "new", "--by", "codex")
        second = out.splitlines()[0]
        self.assertNotEqual(first, second)
        self.assertIn("credited automatically", out)
        self.assertIn(f"session {second}", self.ok("session", "status", "--by", "codex"))

    def test_a_spent_fallback_session_says_stop_instead_of_renewing(self):
        self.seed()
        cfg = self.v / "_brain" / "config.json"
        cfg.write_text(json.dumps(dict(json.loads(cfg.read_text(encoding="utf-8")), auto_idle_minutes=60)),
                       encoding="utf-8")
        limit = brain.MODES["normal"]["queries"]
        for i in range(limit):
            self.ok("recall", f"cutoff {i}", "--by", "codex")
        code, out, err = run(self.v, "recall", "cutoff again", "--by", "codex")
        self.assertEqual(code, 2, out + err)
        self.assertIn(f"limit of {limit} queries", err)
        self.assertEqual(brain.Sessions(brain.Vault(self.v)).idle_seconds(), 3600)

    def test_snapshot_sees_an_edited_node(self):
        self.seed()
        self.ok("recall", "cutoff")  # writes the snapshot and the index
        path = self.v / "Orders" / "Order cutoff is at 2pm EST" / "Late orders went to the wrong wave.md"
        time.sleep(0.05)
        path.write_text(path.read_text(encoding="utf-8").replace("server clock", "zanzibar clock"), encoding="utf-8")
        out = self.ok("recall", "zanzibar", "--no-body")
        self.assertIn("Late orders went to the wrong wave", out)

    def test_consolidate_applies_archive(self):
        self.seed()
        path = self.v / "_brain" / "proposals.md"
        path.write_text("- [x] archive :: [[The shipping window limits the cutoff]] — test\n", encoding="utf-8")
        self.ok("consolidate", "--apply")
        self.assertTrue((self.v / "_archive" / "The shipping window limits the cutoff.md").exists())
        self.assertNotIn("The shipping window", (self.v / "Orders" / "Orders.md").read_text(encoding="utf-8"))
        self.assertEqual(self.code("lint"), 0)


class ExternalTests(Base):
    def test_leaving_a_vault(self):
        self.seed()
        other = self.tmp / "Other"
        make_vault(other, "Other", {"Test": "../Test"})
        self.ok("new", "--layer", "1", "--title", "Customers", "--summary", "Who buys and how.", vault=other)
        self.ok("external", "add", "Orders", "--in", "Other", "--note", "Customers")
        self.assertIn("obsidian://open?vault=Other&file=Customers", (self.v / "Orders" / "Orders.md").read_text(encoding="utf-8"))
        _, output, _ = run(self.v, "lint")
        self.assertIn("external dependency", output)
        _, output, _ = run(self.v, "recall", "customers", "--all")
        self.assertIn("{Other} [L1] Customers", output)
        shutil.rmtree(other)
        self.ok("externals", "--clean")
        self.assertNotIn("obsidian://", (self.v / "Orders" / "Orders.md").read_text(encoding="utf-8"))
        cfg = json.loads((self.v / "_brain" / "config.json").read_text(encoding="utf-8"))
        self.assertNotIn("Other", cfg["siblings"])
        self.assertEqual(self.code("lint"), 0)


class VisualizationTests(Base):
    def test_build_generates_everything(self):
        self.seed()
        self.ok("build")
        html = (self.v / "_brain" / "Panel.html").read_text(encoding="utf-8")
        self.assertNotIn("__BRAIN_DATA__", html)
        start = html.index('type="application/json">') + len('type="application/json">')
        data = json.loads(html[start:html.index("</script>", start)])
        self.assertEqual(data["vault"], "Test")
        titles = {n["title"] for n in data["nodes"]}
        self.assertIn("Orders", titles)
        for s in data["synapses"]:
            self.assertIn(s["source"], titles)
        self.assertFalse((self.v / "_brain" / "Orbit.canvas").exists())
        self.assertIn("[[Orders]]", (self.v / "MAP.md").read_text(encoding="utf-8"))
        self.assertIn("## Layers", (self.v / "_brain" / "Panel.md").read_text(encoding="utf-8"))
        self.assertTrue((self.v / "_brain" / "generated" / "orbit.svg").exists())
        first = (self.v / "_brain" / "generated" / "orbit.svg").read_text(encoding="utf-8")
        self.ok("build")
        self.assertEqual(first, (self.v / "_brain" / "generated" / "orbit.svg").read_text(encoding="utf-8"), "deterministic positions")

    def test_planted_nodes_glow_until_their_status_changes(self):
        self.seed()
        self.ok("learn", "--up", "Order cutoff is at 2pm EST", "--title", "Friday cutoff moves to noon",
                "--summary", "On Fridays the cutoff is at noon EST.", "--content", "The Friday wave leaves early.",
                "--evidence", "Seen in the dispatch log.", "--by", "tester",
                "--verify-intent", "behavior", "--verify-keywords", "friday", "cutoff")

        def planted():
            self.ok("build")
            html = (self.v / "_brain" / "Panel.html").read_text(encoding="utf-8")
            start = html.index('type="application/json">') + len('type="application/json">')
            return {n["title"] for n in json.loads(html[start:html.index("</script>", start)])["nodes"] if n.get("planted")}

        self.assertIn("Orders", planted(), "a node created with new is planted as a seed")
        self.assertIn("Friday cutoff moves to noon", planted(), "a learning is planted as provisional")
        self.ok("status", "Orders", "consolidated")
        self.assertNotIn("Orders", planted(), "its status changed, so it is no longer drawn as planted")
        self.assertIn("Friday cutoff moves to noon", planted())

    def test_life_cycle_phases_reach_the_panel_feed_only(self):
        self.seed()
        nodes = self.v / "dreamed.txt"
        nodes.write_text("Order cutoff is at 2pm EST\nNo such node\n", encoding="utf-8")
        self.ok("activity", "dreamed", "--nodes", str(nodes), "--note", "REM 1 note")
        self.ok("activity", "measured", "--note", "hit@1 9/10", "--alarm")
        raw = (self.v / "_brain" / "state" / "activity.js").read_text(encoding="utf-8")
        events = json.loads(raw[len(brain.ACTIVITY_PREFIX):].rstrip().rstrip(";"))["events"]
        dreamed, measured = events[-2], events[-1]
        self.assertEqual((dreamed["e"], dreamed["titles"], dreamed["n"], dreamed["note"]), ("dreamed", ["Order cutoff is at 2pm EST"], 1, "REM 1 note"))
        self.assertEqual((measured["e"], measured["alarm"]), ("measured", True))
        self.assertNotIn("titles", measured)
        usage = self.v / "_brain" / "state" / "usage.jsonl"
        self.assertFalse(usage.exists() and "dreamed" in usage.read_text(encoding="utf-8"), "a phase is not telemetry")

    def test_activity_feed_for_live_signals(self):
        self.seed()
        sid = run(self.v, "session", "new")[1].splitlines()[0]
        self.ok("recall", "cutoff", "--session", sid)
        self.ok("recall", "zebra migration", "--session", sid)
        self.ok("read", "Order cutoff is at 2pm EST", "--session", sid)
        self.ok("credit", "--session", sid, "--used", "Order cutoff is at 2pm EST")
        raw = (self.v / "_brain" / "state" / "activity.js").read_text(encoding="utf-8")
        self.assertTrue(raw.startswith(brain.ACTIVITY_PREFIX))
        events = json.loads(raw[len(brain.ACTIVITY_PREFIX):].rstrip().rstrip(";"))["events"]
        kinds = [e["e"] for e in events]
        self.assertEqual(kinds[0], "recall")
        self.assertIn("activated", kinds)
        self.assertEqual(kinds[-2:], ["read", "used"])
        self.assertIn({"cutoff", "zebra migration"}, [{e["q"] for e in events if e["e"] == "recall"}])
        self.assertEqual([e["i"] for e in events], list(range(1, len(events) + 1)), "monotonic sequence for the poller")
        self.assertEqual(events[-1]["title"], "Order cutoff is at 2pm EST")

    def test_performance_1000_nodes(self):
        for d in range(10):
            write_node(self.v, f"Domain {d}/Domain {d}.md", 1, f"Domain {d}", "MAP", f"Domain number {d}.",
                       "## Branches\n" + "".join(f"- [[Concept {d}-{c}]] — x\n" for c in range(10)))
            for c in range(10):
                write_node(self.v, f"Domain {d}/Concept {d}-{c}.md", 2, f"Concept {d}-{c}", f"Domain {d}",
                           f"Concept {c} of domain {d} about inventory and flowers.",
                           "## Details\n" + "".join(f"- [[Detail {d}-{c}-{k}]] — x\n" for k in range(9)))
                for k in range(9):
                    write_node(self.v, f"Domain {d}/Concept {d}-{c}/Detail {d}-{c}-{k}.md", 3, f"Detail {d}-{c}-{k}",
                               f"Concept {d}-{c}", f"Detail {k} of concept {c}: roses, carnations and farms.")
        t0 = time.perf_counter()
        self.ok("build", "--quiet")
        t_cold = time.perf_counter() - t0
        t0 = time.perf_counter()
        self.ok("precommit")
        t_build = time.perf_counter() - t0
        t0 = time.perf_counter()
        _, output, _ = run(self.v, "recall", "roses farms inventory")
        t_recall = time.perf_counter() - t0
        print(f"\n    1000 nodes · build without cache {t_cold:.2f}s · precommit with cache {t_build:.2f}s · "
              f"recall {t_recall:.2f}s · output {len(output)} characters")
        self.assertLess(t_build, 3.0)
        self.assertLess(t_recall, 2.0)
        self.assertLess(len(output), brain.MODES["normal"]["chars"] + 600)


class LayerRoleTests(Base):
    """The levels are one table of roles; the default is the paper's three (golden output proves nothing else moved)."""

    def test_the_default_table_is_the_three_levels(self):
        self.assertEqual([(d["n"], d["role"]) for d in brain.layers_for({})], [(1, "domain"), (2, "concept"), (3, "leaf")])
        self.assertEqual([d["n"] for d in brain.layers_for({"layers": 5})], [1, 2, 3, 4, 5])
        self.assertEqual(brain.LAYER_ROLE, {1: brain.DOMAIN, 2: brain.CONCEPT, 3: brain.LEAF, 4: brain.LEAF, 5: brain.LEAF})
        self.assertEqual(brain.ROLE_LAYER[brain.LEAF], 3, "a role's layer is its first")
        self.assertEqual([brain.layer_of(r) for r in (brain.DOMAIN, brain.CONCEPT, brain.LEAF)], [1, 2, 3])
        self.assertEqual(brain.MAX_WORDS, {1: 250, 2: 300, 3: 300, 4: 300, 5: 300})
        self.assertEqual(brain.CHILD_SECTION, {1: "Branches", 2: "Details"})
        self.assertEqual(brain.number_list(sorted(brain.Vault(self.v).layer_set)), "1, 2 or 3")

    def test_a_node_knows_its_role(self):
        self.seed()
        v = brain.Vault(self.v)
        self.assertEqual([v.node(t).role for t in ("Orders", "Order cutoff is at 2pm EST", "Late orders went to the wrong wave")],
                         [brain.DOMAIN, brain.CONCEPT, brain.LEAF])
        self.assertIsNone(brain.Node(Path("Odd.md"), "Odd one", {"layer": 9}, "", []).role)


class MoreLayersTests(Base):
    """Three to five layers: the levels beyond the third refine a detail, and adding one moves no note."""

    DETAIL = "Late orders went to the wrong wave"
    CONCEPT = "Order cutoff is at 2pm EST"

    def layered(self, to=None):
        self.seed()
        (self.v / ".obsidian").mkdir()
        (self.v / ".obsidian" / "graph.json").write_text(json.dumps({"colorGroups": [{"query": "[layer:3]", "color": {"a": 1, "rgb": 1}}]}), encoding="utf-8")
        (self.v / "AGENTS.md").write_text("# A\n\n## Layers\n| Layer | What | Max words |\n|---|---|---|\n| L0 | x | - |\n"
                                          "| L3 | Detail | 300 |\n| Cold | `_inbox` | - |\n\n## Truth\n", encoding="utf-8")
        (self.v / "_brain" / "Layers.base").write_text("views:\n  - type: table\n    name: Seeds to review\n", encoding="utf-8")
        if to:
            self.ok("layers", "--to", str(to))

    def notes(self):
        return {p.relative_to(self.v).as_posix(): p.read_bytes() for p in self.v.rglob("*.md")
                if "_templates" not in p.parts and p.name != "AGENTS.md"}

    def cfg(self):
        return json.loads((self.v / "_brain" / "config.json").read_text(encoding="utf-8"))

    def test_layers_only_go_up_and_only_by_the_command(self):
        self.layered()
        self.assertIn("3 layers", self.ok("layers"))
        for bad, text in (("3", "already has 3"), ("2", "never removed"), ("6", "3 to 5 layers")):
            code, _, err = run(self.v, "layers", "--to", bad)
            self.assertEqual(code, 1, bad)
            self.assertIn(text, err)
        self.assertNotIn("layers", self.cfg())
        self.ok("layers", "--to", "5")
        code, _, err = run(self.v, "layers", "--to", "4")
        self.assertIn("never removed", err)
        self.assertEqual(self.cfg()["layers"], 5)

    def test_adding_a_layer_moves_no_note_and_says_so_everywhere(self):
        self.layered()
        before = self.notes()
        self.ok("layers", "--to", "5")
        self.assertEqual(self.notes(), before)
        self.assertEqual(self.cfg()["layers"], 5)
        for k in (4, 5):
            tpl = (self.v / "_templates" / f"L{k} Refinement.md").read_text(encoding="utf-8")
            self.assertIn(f"layer: {k}", tpl)
        agents = (self.v / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("| L4 | Refinement of an L3 detail", agents)
        self.assertIn("| L5 | Refinement of an L4 detail", agents)
        self.assertIn("This vault has 5 layers", agents)
        self.assertEqual(agents.count("| L3 |"), 1)
        graph = json.loads((self.v / ".obsidian" / "graph.json").read_text(encoding="utf-8"))
        self.assertEqual([g["query"] for g in graph["colorGroups"]], ["[layer:3]", "[layer:4]", "[layer:5]"])
        base = (self.v / "_brain" / "Layers.base").read_text(encoding="utf-8")
        self.assertIn("name: L4 Refinements", base)
        self.assertLess(base.index("L5 Refinements"), base.index("Seeds to review"))
        self.assertEqual(self.code("lint"), 0)

    def test_a_refinement_nests_under_its_detail_and_lint_accepts_it(self):
        self.layered(4)
        self.ok("new", "--layer", "4", "--up", self.DETAIL, "--type", "fact", "--title", "The clock fix was in the sync job",
                "--summary", "The UTC clock came from the sync job that restarts the server.")
        path = self.v / "Orders" / self.CONCEPT / self.DETAIL / "The clock fix was in the sync job.md"
        self.assertTrue(path.exists(), "folder named for the parent")
        parent = (self.v / "Orders" / self.CONCEPT / f"{self.DETAIL}.md").read_text(encoding="utf-8")
        self.assertIn("## Refinements", parent)
        self.assertIn("[[The clock fix was in the sync job]]", parent)
        code, out, _ = run(self.v, "lint")
        self.assertEqual(code, 0, out)
        self.assertNotIn("expected location", out)
        self.assertNotIn("does not link", out)

    def test_layer_five_nests_one_folder_deeper_and_a_sixth_does_not_exist(self):
        self.layered(5)
        self.ok("new", "--layer", "4", "--up", self.DETAIL, "--title", "First refinement",
                "--summary", "A refinement of the late orders detail.")
        self.ok("new", "--layer", "5", "--up", "First refinement", "--title", "Second refinement",
                "--summary", "A refinement of the refinement of the late orders detail.")
        self.assertTrue((self.v / "Orders" / self.CONCEPT / self.DETAIL / "First refinement" / "Second refinement.md").exists())
        self.assertEqual(self.code("lint"), 0)
        self.assertNotIn(6, brain.Vault(self.v).layer_set)

    def test_a_layer_the_vault_does_not_have_is_refused_and_linted(self):
        self.layered()
        code, _, err = run(self.v, "new", "--layer", "4", "--up", self.DETAIL, "--title", "Too soon", "--summary", "Not yet.")
        self.assertEqual(code, 1)
        self.assertIn("this vault has 3 layers, not 4", err)
        write_node(self.v, "Orders/Stray.md", 4, "Stray refinement", self.DETAIL, "A refinement in a three-layer vault.", type="fact")
        code, out, _ = run(self.v, "lint")
        self.assertEqual(code, 1)
        self.assertIn("layer must be 1, 2 or 3", out)

    def test_a_refinement_hangs_only_from_the_layer_above(self):
        self.layered(4)
        code, _, err = run(self.v, "new", "--layer", "4", "--up", self.CONCEPT, "--title", "Wrong parent", "--summary", "Under a concept.")
        self.assertEqual(code, 1)
        self.assertIn("an L4 must hang from an L3", err)
        code, _, err = run(self.v, "new", "--layer", "3", "--up", self.DETAIL, "--title", "Wrong again", "--summary", "A detail under a detail.")
        self.assertIn("an L3 must hang from an L2", err)

    def test_recall_hot_consolidate_and_learn_work_with_refinements(self):
        self.layered(4)
        self.ok("new", "--layer", "4", "--up", self.DETAIL, "--title", "The sync job restarts the server",
                "--summary", "The nightly sync job restarts the server and resets its clock to UTC.")
        out = self.ok("recall", "sync job restarts clock")
        self.assertIn("[L4] The sync job restarts the server", out)
        self.assertIn("consolidate", self.ok("consolidate") + "consolidate")
        self.ok("learn", "--up", self.CONCEPT, "--title", "Cutoffs follow the carrier", "--summary", "A learned fact about the carrier cutoff.",
                "--content", "The carrier sets the cutoff.", "--evidence", "test", "--truth", "historical")
        learned = [p for p in self.v.rglob("Cutoffs follow the carrier.md")]
        self.assertEqual(len(learned), 1)
        self.assertIn("layer: 3", learned[0].read_text(encoding="utf-8"))
        self.assertEqual(self.code("lint"), 0)

    def test_the_folder_of_refinements_travels_with_its_detail(self):
        self.layered(4)
        self.ok("new", "--layer", "4", "--up", self.DETAIL, "--title", "A refinement", "--summary", "A refinement of the late orders detail.")
        self.ok("move", self.DETAIL, "--up", "The shipping window limits the cutoff")
        moved = self.v / "Orders" / "The shipping window limits the cutoff" / self.DETAIL
        self.assertTrue((moved / "A refinement.md").exists())
        self.assertFalse((self.v / "Orders" / self.CONCEPT / self.DETAIL).exists())
        self.ok("rename", self.DETAIL, "Late orders joined the wrong wave")
        self.assertTrue((self.v / "Orders" / "The shipping window limits the cutoff" / "Late orders joined the wrong wave" / "A refinement.md").exists())
        self.assertEqual(self.code("lint"), 0)
        code, _, err = run(self.v, "promote", "Late orders joined the wrong wave")
        self.assertEqual(code, 1)
        self.assertIn("promoted by the curator", err)

    def test_build_draws_every_layer(self):
        self.layered(5)
        self.ok("new", "--layer", "4", "--up", self.DETAIL, "--title", "First refinement", "--summary", "A refinement of the late orders detail.")
        self.ok("new", "--layer", "5", "--up", "First refinement", "--title", "Second refinement", "--summary", "A refinement of the refinement.")
        self.ok("build")
        svg = (self.v / "_brain" / "generated" / "orbit.svg").read_text(encoding="utf-8")
        self.assertIn("L4 1 · L5 1", svg)
        self.assertEqual(svg.count('fill="none" stroke="#888780"'), 4 + 2, "a ring per layer (L1, L2, L3, L4, L5) and the cold ring")
        self.assertIn('fill="#d1a21c"', svg)
        self.assertIn('fill="#d6527e"', svg)
        md = (self.v / "_brain" / "Panel.md").read_text(encoding="utf-8")
        self.assertIn("| L4 Refinements | 1 |", md)
        self.assertIn("| L5 Refinements | 1 |", md)
        html = (self.v / "_brain" / "Panel.html").read_text(encoding="utf-8")
        data = json.loads(html.split('<script id="brain-data" type="application/json">')[1].split("</script>")[0])
        self.assertEqual(data["layers"], 5)
        self.assertEqual(sorted({n["layer"] for n in data["nodes"]}), [1, 2, 3, 4, 5])
        self.assertIn("layerTable(DATA.layers)", html)

    def test_a_default_vault_gives_the_panel_no_layer_count(self):
        self.seed()
        self.ok("build")
        html = (self.v / "_brain" / "Panel.html").read_text(encoding="utf-8")
        data = json.loads(html.split('<script id="brain-data" type="application/json">')[1].split("</script>")[0])
        self.assertNotIn("layers", data)

    def test_doctor_accepts_three_to_five_and_refuses_the_rest(self):
        for n, ok in ((3, True), (4, True), (5, True), (6, False), (2, False)):
            cfg = self.cfg()
            cfg["layers"] = n
            (self.v / "_brain" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
            self.assertEqual("layers; this engine builds 3 to 5" in run(self.v, "doctor")[1], not ok, n)


class IdeasTests(Base):
    """The ideas miner: deterministic candidates, written to _inbox for the curator, never into the graph."""

    def garden(self):
        write_node(self.v, "Farms/Farms.md", 1, "Farms", "MAP", "Where the flowers grow.")
        write_node(self.v, "Dispatch/Dispatch.md", 1, "Dispatch", "MAP", "How orders leave the dock.")
        for up, dom, title, summary in (
                ("Roses farm", "Farms", None, "Roses from the north farm."),
                ("Carnation farm", "Farms", None, "Carnations from the south farm."),
                ("Wave planning", "Dispatch", None, "Which orders ship in which wave.")):
            write_node(self.v, f"{dom}/{up}.md", 2, up, dom, summary)
        for up, dom, title, summary in (
                ("Roses farm", "Farms", "Pallets leave at dawn", "Pallets of roses leave the farm at dawn."),
                ("Carnation farm", "Farms", "Pallets need cooling", "Carnation pallets are cooled before loading."),
                ("Wave planning", "Dispatch", "Pallet labels print late", "Pallet labels are printed after the wave closes."),
                ("Wave planning", "Dispatch", "Pallets wait at dock three", "Loaded pallets wait at dock three."),
                ("Roses farm", "Farms", "Cold chain truck fails at dusk", "The cold chain truck fails at dusk on hot days."),
                ("Wave planning", "Dispatch", "Dusk repairs of the cold chain truck", "Repairs of the cold chain truck happen at dusk.")):
            write_node(self.v, f"{dom}/{up}/{title}.md", 3, title, up, summary)

    def test_a_word_that_recurs_under_different_parents_is_proposed_as_a_concept(self):
        self.garden()
        self.ok("build")
        ideas = brain.mine_ideas(brain.Vault(self.v), 4)
        concepts = {i["word"]: i for i in ideas if i["kind"] == "concept"}
        self.assertIn("pallets", concepts)
        self.assertEqual(concepts["pallets"]["count"], 4)
        self.assertEqual(concepts["pallets"]["up"] in ("Farms", "Dispatch"), True)
        self.assertNotIn("roses", concepts, "a word in a concept's title is already a concept")
        self.assertNotIn("dusk", concepts, "only two nodes carry it: below the threshold")
        self.assertEqual(ideas, brain.mine_ideas(brain.Vault(self.v), 4), "the same vault gives the same list")

    def test_two_unlinked_nodes_that_share_rare_words_are_proposed_as_a_link(self):
        self.garden()
        self.ok("build")
        links = [i for i in brain.mine_ideas(brain.Vault(self.v), 4) if i["kind"] == "link"]
        pairs = {frozenset((i["a"], i["b"])) for i in links}
        self.assertIn(frozenset(("Cold chain truck fails at dusk", "Dusk repairs of the cold chain truck")), pairs)
        self.ok("link", "Cold chain truck fails at dusk", "Dusk repairs of the cold chain truck", "--type", "related",
                "--reason", "the same truck")
        links = [i for i in brain.mine_ideas(brain.Vault(self.v), 4) if i["kind"] == "link"]
        self.assertNotIn(frozenset(("Cold chain truck fails at dusk", "Dusk repairs of the cold chain truck")),
                         {frozenset((i["a"], i["b"])) for i in links}, "linked: nothing left to propose")

    def test_nodes_used_together_are_proposed_as_a_link(self):
        self.garden()
        self.ok("build")
        v = brain.Vault(self.v)
        a, b = v.node("Pallets leave at dawn").key, v.node("Pallet labels print late").key
        path = v.state_dir / "usage.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for g in ("g1", "g2"):
                for k in (a, b):
                    f.write(json.dumps({"t": brain.now().isoformat(), "s": g, "e": "used", "k": k, "g": g}) + "\n")
        links = [i for i in brain.mine_ideas(brain.Vault(self.v), 4) if i["kind"] == "link"]
        hit = [i for i in links if {i["a"], i["b"]} == {"Pallets leave at dawn", "Pallet labels print late"}]
        self.assertEqual(len(hit), 1)
        self.assertGreaterEqual(hit[0]["co"], brain.IDEAS_CO_MIN)

    def test_it_writes_notes_to_the_inbox_and_never_touches_the_graph(self):
        self.garden()
        self.ok("build")
        before = sorted(brain.Vault(self.v).net)
        files = {p.relative_to(self.v).as_posix(): p.read_bytes() for p in self.v.rglob("*.md") if "_inbox" not in p.parts}
        out = self.ok("ideas")
        self.assertIn("written to _inbox", out)
        notes = sorted((self.v / "_inbox").glob("*.md"))
        self.assertTrue(notes)
        text = "\n".join(n.read_text(encoding="utf-8") for n in notes)
        self.assertIn('idea: "concept:', text)
        self.assertIn("ideas miner (deterministic, no model)", text)
        self.assertEqual(sorted(brain.Vault(self.v).net), before, "the graph has the same nodes")
        after = {p.relative_to(self.v).as_posix(): p.read_bytes() for p in self.v.rglob("*.md") if "_inbox" not in p.parts}
        self.assertEqual(files, after, "no note outside _inbox changed")
        self.assertEqual(self.code("lint"), 0)
        self.assertTrue((self.v / "_log" / "ideas.jsonl").exists())

    def test_an_idea_is_proposed_once_even_when_its_note_is_deleted(self):
        self.garden()
        self.ok("build")
        self.assertIn("would be written", self.ok("ideas", "--dry-run"))
        self.assertFalse(list((self.v / "_inbox").glob("*.md")), "a dry run writes nothing")
        self.ok("ideas")
        n = len(list((self.v / "_inbox").glob("*.md")))
        self.assertIn("0 written", self.ok("ideas"))
        for f in (self.v / "_inbox").glob("*.md"):
            f.unlink()                       # the curator dismissed them all
        self.assertIn("0 written", self.ok("ideas"))
        self.assertEqual(len(list((self.v / "_inbox").glob("*.md"))), 0)
        self.assertGreater(n, 0)

    def test_limit_and_threshold(self):
        self.garden()
        self.ok("build")
        self.assertIn("0 concepts", self.ok("ideas", "--dry-run", "--min-count", "5").splitlines()[0])
        self.ok("ideas", "--limit", "1")
        self.assertEqual(len(list((self.v / "_inbox").glob("*.md"))), 2, "one concept and one link")


class WarmMemoryTests(Base):
    """A process that makes many calls (the hub's resident engine) keeps the graph, the index, the glossary and the
    usage sums in memory; none of it may outlive the files it was built from."""

    def test_the_graph_and_the_index_follow_an_edited_note(self):
        self.seed()
        self.ok("build")
        self.assertIn("carrier's shipping window sets", self.ok("recall", "cutoff", "--no-body"))
        v = brain.Vault(self.v)
        self.assertTrue(brain.warm(v.root).get("snapshot") and brain.warm(v.root).get("bm25"))
        note = self.v / "Orders" / "The shipping window limits the cutoff.md"
        note.write_text(note.read_text(encoding="utf-8").replace(
            "The carrier's shipping window sets the latest possible cutoff.",
            "Pallets leave the dock only on Fridays, an entirely different summary."), encoding="utf-8")
        got = self.ok("recall", "pallets dock fridays", "--no-body")
        self.assertIn("Pallets leave the dock", got)
        self.assertNotIn("carrier's shipping window sets", got)

    def test_the_memory_is_not_used_when_a_file_is_added_or_removed(self):
        self.seed()
        self.ok("build")
        self.ok("recall", "cutoff", "--no-body")
        self.ok("new", "--layer", "2", "--up", "Orders", "--title", "Pallets leave on Fridays",
                "--summary", "The dock ships pallets only on Fridays.")
        self.assertIn("Pallets leave on Fridays", self.ok("recall", "pallets fridays", "--no-body"))
        (self.v / "Orders" / "Pallets leave on Fridays.md").unlink()
        self.assertNotIn("Pallets leave on Fridays", self.ok("recall", "pallets fridays", "--no-body"))

    def test_the_glossary_follows_its_file(self):
        self.seed()
        self.ok("build")
        before = brain.translate_query(brain.Vault(self.v), "cuando es el corte de zxqv")
        path = self.v / "_brain" / "glossary.json"
        path.write_text(json.dumps({"es": {"zxqv": "pallets"}}), encoding="utf-8")
        after = brain.translate_query(brain.Vault(self.v), "cuando es el corte de zxqv")
        self.assertNotIn("pallets", before)
        self.assertIn("pallets", after)
        path.unlink()
        self.assertNotIn("pallets", brain.translate_query(brain.Vault(self.v), "cuando es el corte de zxqv"))

    def test_the_usage_sums_follow_the_log_and_survive_a_restart(self):
        self.seed()
        self.ok("build")
        title = "The shipping window limits the cutoff"
        key = brain.Vault(self.v).node(title).key
        w0 = brain.Telemetry(brain.Vault(self.v)).weights()[key]
        sid = self.ok("session", "new").splitlines()[0].strip()
        self.ok("read", title, "--session", sid)
        self.ok("credit", "--used", title, "--session", sid)
        w1 = brain.Telemetry(brain.Vault(self.v)).weights()[key]
        self.assertGreater(w1, w0)
        # the sums kept in memory equal the sums rebuilt from the whole log
        tel = brain.Telemetry(brain.Vault(self.v))
        full = tel.weights_from_log()
        for k, w in tel.weights().items():
            self.assertAlmostEqual(w, full[k], places=6)
        # a process with no memory (a new brain.py) reads the same sums from the log and the saved pickle
        brain.WARM.clear()
        w2 = brain.Telemetry(brain.Vault(self.v)).weights()[key]
        self.assertAlmostEqual(w2, w1, places=6)

    def test_only_a_few_vaults_are_kept(self):
        for i in range(brain.WARM_VAULTS + 3):
            brain.warm(f"/nowhere/{i}")["x"] = 1
        self.assertLessEqual(len(brain.WARM), brain.WARM_VAULTS)


class HotMemoryTests(Base):
    def log(self, *events):
        v = brain.Vault(self.v)
        path = v.state_dir / "usage.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for title, event, hours_ago, session in events:
                t = (brain.now() - brain.dt.timedelta(hours=hours_ago)).isoformat()
                f.write(json.dumps({"t": t, "s": session, "e": event, "k": v.node(title).key}) + "\n")

    def hot_section(self):
        text = (self.v / "MAP.md").read_text(encoding="utf-8")
        return text.split("## Hot", 1)[1].split("## How to read", 1)[0] if "## Hot" in text else ""

    def test_recent_and_frequent_uses_heat_a_node(self):
        self.seed()
        self.ok("build")
        self.assertEqual(self.hot_section(), "", "no use, no hot list")
        self.log(("The shipping window limits the cutoff", "read", 2, "a"),
                 ("Late orders went to the wrong wave", "read", 24 * 60, "b"),
                 ("Orders", "used", 1, "c"),
                 ("Order cutoff is at 2pm EST", "activated", 1, "d"))
        self.ok("build")
        hot = self.hot_section()
        self.assertIn("[[The shipping window limits the cutoff]]", hot)
        self.assertNotIn("Late orders went to the wrong wave", hot, "a read two months ago has cooled below the floor")
        self.assertNotIn("[[Orders]]", hot, "a domain is already listed under Domains")
        self.assertNotIn("Order cutoff is at 2pm EST", hot, "being listed by recall is not being needed")
        act = brain.Telemetry(brain.Vault(self.v)).activation()
        self.log(("Late orders went to the wrong wave", "used", 3, "e"), ("Late orders went to the wrong wave", "used", 5, "f"))
        self.ok("build")
        hot = self.hot_section()
        self.assertLess(hot.index("Late orders"), hot.index("The shipping window"), "three uses outrank one read")
        self.assertTrue(act)

    def test_answers_from_map_are_counted(self):
        self.seed()
        self.log(("The shipping window limits the cutoff", "read", 2, "a"))
        self.ok("build")
        time.sleep(1.1)  # telemetry has one-second resolution: the credit must come after the build that showed the list
        self.ok("credit", "--used", "The shipping window limits the cutoff")
        hot = (self.v / "_brain" / "generated" / "hot.md").read_text(encoding="utf-8")
        self.assertIn("[[The shipping window limits the cutoff]]", hot, "credit refreshes hot.md without a build")
        self.ok("credit", "--used", "Late orders went to the wrong wave")
        doctor = run(self.v, "doctor")[1]
        self.assertIn("1 answers from the Hot list with no read", doctor,
                      "a credit that makes a node hot was not answered from the list: it was not on show yet")


def fake_memory(folder: Path) -> Path:
    """A Claude Code memory folder as it looks on disk: MEMORY.md and one note per memory."""
    m = folder / "C--Users-me-Dev-OfficialRepos-Demo-App" / "memory"
    m.mkdir(parents=True)
    note = lambda name, typ, desc, body: (m / f"{name}.md").write_text(   # noqa: E731
        f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: {typ}\n---\n\n{body}\n", encoding="utf-8")
    note("owner-profile", "user", "Engineer who prefers short answers", "Works on data pipelines.")
    note("plan-first", "feedback", "Plan first, act only on an explicit OK",
         "Propose a plan before editing. **Why:** surprises cost a day. See [[owner-profile]] and [[gone-note]].")
    note("mail-rule", "feedback", "Write to me at me@example.com", "Contact: me@example.com")
    note("long-call", "project", "A long call summary", " ".join(["word"] * 400))
    note("qa-dev", "project", "The SQL server is QA, not PROD", "Downloads are allowed, read-only.")
    (m / "MEMORY.md").write_text(
        "- [Owner profile](owner-profile.md) — who\n- [Plan first](plan-first.md) — rule\n"
        "- [Mail rule](mail-rule.md) — mail\n- [Long call](long-call.md) — call\n"
        "- [SQL Server is QA/DEV](qa-dev.md) — server\n", encoding="utf-8")
    return m


class MemoryImportTests(Base):
    """Claude Code memory into the graph by rule: domains by type, concepts by memory, links kept, nothing twice."""

    def test_memories_land_in_the_graph_by_rule_once(self):
        mem = fake_memory(self.tmp / "projects")
        out = self.ok("import-memory", str(mem))
        self.assertIn("imported 3 memories · 0 already here · 1 to _inbox · 1 skipped", out)
        self.assertIn("skipped mail-rule.md: possible email address", out)
        v = brain.Vault(self.v)
        self.assertEqual(v.node("Owner profile").layer, 1)
        self.assertEqual(v.node("Working agreements").layer, 1)
        plan = v.node("Plan first")
        self.assertEqual((plan.layer, plan.fm["summary"], plan.fm["source"]),
                         (2, "Plan first, act only on an explicit OK",
                          "claude-code memory C--Users-me-Dev-OfficialRepos-Demo-App/plan-first.md"))
        body = (self.v / plan.path).read_text(encoding="utf-8")
        self.assertIn("See [[Owner profile (Demo App)]] and gone-note.", body, "links between memories kept, others plain")
        self.assertTrue(v.exists("SQL Server is QA-DEV"))
        self.assertEqual(v.node("Owner profile (Demo App)").layer, 2, "a memory named like a domain gets its project")
        self.assertEqual(len(list((self.v / "_inbox").glob("* import Long call.md"))), 1)
        self.assertIn("imported 0 memories · 4 already here · 0 to _inbox · 1 skipped", self.ok("import-memory", str(mem)))
        self.assertEqual(self.code("lint"), 0)
        self.assertNotIn("WARN", self.ok("lint"))

    def test_the_import_is_one_commit(self):
        mem = fake_memory(self.tmp / "projects")
        env = {**os.environ, **GIT_ID}
        subprocess.run(["git", "-C", str(self.v), "init", "-q", "-b", "main"], env=env)
        subprocess.run(["git", "-C", str(self.v), "add", "-A"], env=env)
        subprocess.run(["git", "-C", str(self.v), "commit", "-q", "-m", "seed"], env=env)
        from unittest import mock
        with mock.patch.dict(os.environ, GIT_ID):
            self.assertIn("committed", self.ok("import-memory", str(mem), "--commit"))
        log = subprocess.run(["git", "-C", str(self.v), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout
        self.assertEqual(log.strip(), "import: 4 memories from Claude Code (Demo App)")


GIT_ID = {"GIT_AUTHOR_NAME": "Cortex hub", "GIT_AUTHOR_EMAIL": "hub@cortex.local",
          "GIT_COMMITTER_NAME": "Cortex hub", "GIT_COMMITTER_EMAIL": "hub@cortex.local"}


class MigrationTests(Base):
    """A vault from before migrations (no schema: a fossil) reaches this engine's schema by code alone, once."""

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.v), *args], capture_output=True, text=True,
                              env={**os.environ, **GIT_ID})

    def init_git(self):
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")

    def schema(self):
        return brain.vault_schema(brain.Vault(self.v))

    def test_a_fossil_vault_reaches_the_current_schema_once(self):
        self.seed()
        gi = self.v / ".gitignore"
        gi.write_text("_brain/state/sessions/\n", encoding="utf-8")
        self.assertEqual(self.schema(), 0)
        self.assertIn("would apply 0001", self.ok("migrate", "--dry-run"))
        self.assertEqual((self.schema(), gi.read_text(encoding="utf-8")), (0, "_brain/state/sessions/\n"), "dry run")
        out = self.ok("migrate")
        self.assertIn("applied 0001", out)
        text = gi.read_text(encoding="utf-8")
        self.assertEqual((self.schema(), text.count("_brain/state/mcp.token"), text.count("_brain/state/sessions/"),
                          text.count("_brain/state/remote.token")), (brain.engine_schema(), 1, 1, 1))
        self.assertIn(f"schema {brain.engine_schema()}: up to date", self.ok("migrate"))
        self.assertEqual(gi.read_text(encoding="utf-8"), text, "a second run changes nothing")

    def test_uncommitted_changes_make_a_migration_wait(self):
        self.seed()
        self.init_git()
        (self.v / ".gitignore").write_text("my own edit\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-q", "-m", "ignore")
        (self.v / ".gitignore").write_text("my own edit, not committed\n", encoding="utf-8")
        code, out, _ = run(self.v, "migrate")
        self.assertEqual(code, 1)
        self.assertIn("waits: uncommitted changes in .gitignore", out)
        self.assertEqual(self.schema(), 0)
        self.assertEqual((self.v / ".gitignore").read_text(encoding="utf-8"), "my own edit, not committed\n")

    def test_a_failing_migration_is_undone(self):
        self.seed()
        folder = self.tmp / "migrations"
        folder.mkdir()
        (folder / "m0001_breaks.py").write_text(
            "ID = 1\nTITLE = 'writes, then breaks'\n"
            "def plan(root):\n    return ['.gitignore', 'New note.md']\n"
            "def up(root):\n    (root / '.gitignore').write_text('changed\\n')\n"
            "    (root / 'New note.md').write_text('x')\n    raise RuntimeError('boom')\n", encoding="utf-8")
        (self.v / ".gitignore").write_text("original\n", encoding="utf-8")
        cfg = (self.v / "_brain" / "config.json").read_bytes()
        code, out, _ = run(self.v, "migrate", "--dir", str(folder))
        self.assertEqual(code, 1)
        self.assertIn("failed and was undone: boom", out)
        self.assertEqual((self.v / ".gitignore").read_text(encoding="utf-8"), "original\n")
        self.assertFalse((self.v / "New note.md").exists())
        self.assertEqual((self.v / "_brain" / "config.json").read_bytes(), cfg)

    def test_a_vault_on_a_newer_schema_is_left_alone(self):
        brain.set_schema(self.v, 99)
        code, _, err = run(self.v, "migrate")
        self.assertEqual(code, 1)
        self.assertIn("newer than this engine knows", err)

    def test_each_migration_is_its_own_commit(self):
        self.seed()
        self.init_git()
        from unittest import mock
        with mock.patch.dict(os.environ, GIT_ID):
            self.assertIn("committed", self.ok("migrate", "--commit"))
        log = self.git("log", "-2", "--format=%an|%s").stdout.strip().splitlines()
        log = self.git("log", "-3", "--format=%an|%s").stdout.strip().splitlines()
        self.assertTrue(log[2].startswith("Cortex hub|migration: 0001 baseline"), log)
        self.assertTrue(log[1].startswith("Cortex hub|migration: 0002 the push token"), log)
        self.assertTrue(log[0].startswith("Cortex hub|migration: 0003 the vault states"), log)
        self.assertEqual(self.git("status", "--porcelain", "--", ".gitignore", "_brain/config.json").stdout, "")

    def test_new_vaults_start_on_the_current_schema(self):
        cfg = json.loads((HERE.parents[1] / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["schema"], brain.engine_schema())
        self.assertEqual(cfg["layers"], 3, "a new vault states its layers")

    def test_a_schema_2_vault_gains_its_layer_count_and_nothing_else(self):
        """Fossil of engine 2.24: schema 2, no `layers`. Migration 0003 writes `layers: 3` and moves nothing."""
        self.seed()
        brain.set_schema(self.v, 2)
        (self.v / ".gitignore").write_text("_brain/state/remote.token" + chr(10), encoding="utf-8")
        cfg_path = self.v / "_brain" / "config.json"
        before = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertNotIn("layers", before)
        notes = sorted(p.relative_to(self.v).as_posix() for p in self.v.rglob("*.md") if "_templates" not in p.parts)
        self.assertIn("would apply 0003", self.ok("migrate", "--dry-run"))
        self.assertEqual(json.loads(cfg_path.read_text(encoding="utf-8")), before, "dry run")
        self.assertIn("applied 0003", self.ok("migrate"))
        after = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(after, {**before, "layers": 3, "schema": 3})
        self.assertEqual(sorted(p.relative_to(self.v).as_posix() for p in self.v.rglob("*.md") if "_templates" not in p.parts), notes)
        raw = cfg_path.read_bytes()
        self.assertIn("up to date", self.ok("migrate"))
        self.assertEqual(cfg_path.read_bytes(), raw, "a second run changes nothing")

    def test_a_vault_that_already_states_its_layers_keeps_them(self):
        brain.set_schema(self.v, 2)
        cfg_path = self.v / "_brain" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["layers"] = 4
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        self.ok("migrate")
        self.assertEqual(json.loads(cfg_path.read_text(encoding="utf-8"))["layers"], 4)

    def test_doctor_refuses_a_layer_count_this_engine_does_not_build(self):
        cfg_path = self.v / "_brain" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["layers"] = 7
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        code, out, _ = run(self.v, "doctor")
        self.assertIn("this vault has 7 layers; this engine builds 3 to 5", out)
        self.assertEqual(brain.layer_count({"layers": "x"}), 3)


class ConcurrencyTests(Base):
    """What the reads write (the automatic session pointer, the telemetry) is not under the vault lock, so it has
    to survive two agents — or two threads here — touching one vault in the same moment."""

    def test_parallel_writes_of_one_path_keep_their_own_tmp(self):
        path = self.v / "_brain" / "state" / "sessions" / "auto.json"
        texts = [f'{{"n": {i}}}' for i in range(24)]
        errs = []

        def write(t):
            try:
                brain.write_atomic(path, t)
            except Exception as e:  # noqa: BLE001 — the whole point is that no writer raises
                errs.append(repr(e))

        threads = [threading.Thread(target=write, args=(t,)) for t in texts]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errs, [], "two writers fought over one tmp file")
        self.assertIn(path.read_text(encoding="utf-8"), texts, "a write landed as somebody else's content")
        self.assertEqual(list(path.parent.glob("*.tmp")), [], "a writer left its tmp behind")

    def test_two_agents_reading_one_vault_finish_without_crashing(self):
        self.seed()
        self.ok("recall", "cutoff")                      # the first call makes the session pointer exist
        brain_py = HERE.parent / "brain.py"
        done = []

        def read(i):
            r = subprocess.run([sys.executable, str(brain_py), "--vault", str(self.v), "recall", f"cutoff wave {i}"],
                               capture_output=True, text=True, timeout=60)
            done.append((r.returncode, r.stderr))

        threads = [threading.Thread(target=read, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(done), 6)
        for code, err in done:
            self.assertEqual(code, 0, f"a concurrent read exited {code}: {err.strip()[:500]}")


class PrivacyTests(unittest.TestCase):
    """A vault is never public: the pre-push guard reads the forge's anonymous answer."""

    def test_parse_remote_forms(self):
        self.assertEqual(brain.parse_remote("git@github.com:MadLeak/claude-cortex.git"), ("github.com", "MadLeak/claude-cortex"))
        self.assertEqual(brain.parse_remote("https://github.com/MadLeak/claude-cortex.git"), ("github.com", "MadLeak/claude-cortex"))
        self.assertEqual(brain.parse_remote("ssh://git@gitlab.com:22/group/sub/vault"), ("gitlab.com", "group/sub/vault"))
        self.assertIsNone(brain.parse_remote("/srv/git/vault.git"))
        self.assertIsNone(brain.parse_remote("C:\\backups\\vault"))

    def test_visibility_from_status(self):
        url = "https://github.com/someone/vault.git"
        self.assertEqual(brain.remote_visibility(url, fetch=lambda u: 200), "public")
        self.assertEqual(brain.remote_visibility(url, fetch=lambda u: 404), "private")
        self.assertEqual(brain.remote_visibility(url, fetch=lambda u: 403), "unknown")   # rate limit, not proof
        def down(u):
            raise OSError("offline")
        self.assertEqual(brain.remote_visibility(url, fetch=down), "unknown")
        self.assertEqual(brain.remote_visibility("git@git.home.lan:me/vault.git", fetch=lambda u: 200), "self-hosted")
        self.assertEqual(brain.remote_visibility("/srv/git/vault.git"), "self-hosted")

    def test_gitlab_path_is_quoted(self):
        asked = []
        brain.remote_visibility("https://gitlab.com/group/sub/vault.git", fetch=lambda u: asked.append(u) or 404)
        self.assertEqual(asked, ["https://gitlab.com/api/v4/projects/group%2Fsub%2Fvault"])

    def test_prepush_policy(self):
        import types
        from unittest import mock
        a = types.SimpleNamespace(remote="origin", url="https://github.com/someone/vault.git")
        for seen, env, expected in (("public", "1", 1), ("private", "", 0), ("unknown", "", 1), ("unknown", "1", 0), ("self-hosted", "", 0)):
            with mock.patch.object(brain, "remote_visibility", return_value=seen), \
                 mock.patch.dict(os.environ, {"CORTEX_ALLOW_UNVERIFIED_PUSH": env}), \
                 mock.patch("sys.stdout", new=io.StringIO()), mock.patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(brain.cmd_prepush(None, a), expected, (seen, env))


if __name__ == "__main__":
    unittest.main(verbosity=2)
