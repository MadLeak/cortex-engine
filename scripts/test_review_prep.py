"""Review request tests. Run: python3 -m unittest scripts/test_review_prep.py"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_prep  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent.parent / "template-vault"


def note(layer: int, up: str, summary: str, truth: str = "", body: str = "A fact.", links: str = "") -> str:
    return (f"---\nid: x\nlayer: {layer}\ntype: fact\nsummary: \"{summary}\"\nup: \"{'[[' + up + ']]' if up else ''}\"\n"
            f"status: seed\n{'truth: ' + truth if truth else ''}\ncreated: 2026-10-31\n---\n\n## Content\n{body}\n\n"
            f"## Connections\n{links}\n")


class ReviewPrepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "Fern Cortex"
        shutil.copytree(TEMPLATE, self.vault)
        d = self.vault / "Garden"
        d.mkdir()
        (d / "Garden.md").write_text(note(1, "", "The garden domain.", "current"))
        (d / "Watering.md").write_text(note(2, "Garden", "How the beds are watered.", "current"))
        (d / "Soil.md").write_text(note(2, "Garden", "What the soil is.", "current"))
        (d / "Water at dawn.md").write_text(note(3, "Watering", "Beds are watered at 6:00.", "historical",
                                                 links="- depends_on:: [[Soil]] — clay holds water"))
        (d / "Hose length.md").write_text(note(3, "Watering", "The hose is 20 m."))
        self.items = {it["title"]: it for it in review_prep.items(self.vault, "Fern")}

    def tearDown(self):
        self.tmp.cleanup()

    def test_request_shows_ancestors_links_and_sibling_titles(self):
        p = self.items["Water at dawn"]["prompt"]
        anc = p.split("ANCESTORS")[1].split("LINKED NOTES")[0]
        self.assertLess(anc.index("L1 Garden: The garden domain."), anc.index("L2 Watering: How the beds"))
        self.assertIn("depends_on → Soil (L2, truth current): What the soil is.", p)
        sibs = p.split("SIBLINGS")[1]
        self.assertIn("- Hose length\n", sibs + "\n")
        self.assertNotIn("The hose is 20 m.", sibs)   # titles only

    def test_top_domain_has_no_ancestors(self):
        self.assertIn("(none: a top domain)", self.items["Garden"]["prompt"])

    def test_missing_truth_is_settled_by_rule(self):
        v = review_prep.rule(self.items["Hose length"])
        self.assertEqual(v["action"], "fix")
        self.assertIn("truth is '-'", v["reason"])
        self.assertIsNone(review_prep.rule(self.items["Water at dawn"]))

    def test_superseded_stays_obsolete(self):
        it = {**self.items["Hose length"], "truth": "superseded"}
        self.assertEqual(review_prep.rule(it)["action"], "obsolete")


if __name__ == "__main__":
    unittest.main()
