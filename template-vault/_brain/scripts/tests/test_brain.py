# -*- coding: utf-8 -*-
"""Engine tests. Run: python -m unittest discover -s _brain/scripts/tests -v"""
import contextlib
import io
import json
import math
import shutil
import sys
import tempfile
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
        self.assertEqual(brain.token_count_blend("one two three four"), 6)
        self.assertEqual(brain.token_count_script("hola, señor"), 3)
        self.assertEqual(brain.token_count_bytes("abcd"), 1)
        self.assertEqual(brain.token_count_pretok("hello world"), 3)
        self.assertEqual(brain.count_tokens("one two three four"), 6)
        self.assertEqual(brain.count_tokens("Hola, señor. ¿Cómo estás?"),
                         math.ceil(__import__("statistics").median((brain.token_count_blend("Hola, señor. ¿Cómo estás?"),
                         brain.token_count_script("Hola, señor. ¿Cómo estás?"), brain.token_count_bytes("Hola, señor. ¿Cómo estás?"),
                         brain.token_count_pretok("Hola, señor. ¿Cómo estás?")))))

    def test_counters_on_english_spanish_and_code(self):
        samples = {
            "english": ("The quick brown fox jumps over the lazy dog.", (12, 11, 11, 18), 12),
            "spanish": ("Hola, señor. ¿Cómo estás?", (7, 7, 8, 11), 8),
            "code": ("def greet(name): return f'Hola, {name}!'", (10, 10, 10, 17), 10),
        }
        for name, (text, expected, median) in samples.items():
            with self.subTest(sample=name):
                actual = (brain.token_count_blend(text), brain.token_count_script(text),
                          brain.token_count_bytes(text), brain.token_count_pretok(text))
                self.assertEqual(actual, expected)
                self.assertEqual(brain.count_tokens(text, tokenizer_url=""), median)

    def test_optional_tokenizers_do_not_change_base_median_when_unavailable(self):
        text = "def greet(name): return f'Hola, {name}!'"
        baseline = brain.count_tokens(text, tokenizer_url="")
        self.assertEqual(brain.count_tokens(text, tokenizer_url="http://127.0.0.1:1/tokenize"), baseline)

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
                "--evidence", "Seen in the dispatch log.", "--by", "tester")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
