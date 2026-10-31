"""Hub tests. Run: python3 -m unittest scripts/test_cortex_hub.py"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cortex_hub  # noqa: E402


class HubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        v = root / "Fern Cortex" / "_brain"
        (v / "scripts").mkdir(parents=True)
        (v / "scripts" / "brain.py").write_text("")
        (v / "generated").mkdir()
        (v / "state").mkdir()
        (v / "Panel.html").write_text("<html>panel</html>")
        (v / "config.json").write_text("{}")
        (v / "generated" / "graph.json").write_text(json.dumps({"version": "2.11.0", "cold": {"_inbox": 2}, "nodes": [
            {"layer": 1, "status": "consolidated"}, {"layer": 3, "status": "seed"}, {"layer": 3, "status": "provisional"}]}))
        (v / "state" / "activity.js").write_text(cortex_hub.ACTIVITY_PREFIX + json.dumps({"vault": "Fern Cortex", "events": [
            {"t": "2020-01-01T10:00:00", "e": "recall", "q": "when to water", "i": 1},
            {"t": "2020-01-01T10:00:01", "e": "activated", "title": "Beds", "i": 2}]}) + ";\n")
        (root / "notes").mkdir()   # not a vault: no _brain/scripts/brain.py
        cortex_hub.Hub.root = root
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cortex_hub.Hub)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, ""

    def test_serves_only_the_panel_files(self):
        self.assertEqual(self.get("/v/Fern%20Cortex/Panel.html"), (200, "<html>panel</html>"))
        self.assertEqual(self.get("/v/Fern%20Cortex/state/activity.js")[0], 200)
        for path in ("/v/Fern%20Cortex/config.json", "/v/Fern%20Cortex/../config.json", "/v/Fern%20Cortex/%2e%2e/config.json",
                     "/v/notes/Panel.html", "/etc/passwd"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_cards_sum_layers_seeds_and_latest_activity(self):
        status, body = self.get("/api/vaults")
        (v,) = json.loads(body)["vaults"]
        self.assertEqual((status, v["name"], v["version"], v["nodes"], v["inbox"]), (200, "Fern Cortex", "2.11.0", 3, 2))
        self.assertEqual((v["layers"], v["status"]["seed"], v["status"]["provisional"]), ({"1": 1, "2": 0, "3": 2}, 1, 1))
        self.assertEqual([e["e"] for e in v["recent"]], ["recall"], "activated events are left out of the card")
        self.assertFalse(v["live"])
        self.assertIn("Cortex hub", self.get("/")[1])


if __name__ == "__main__":
    unittest.main()
