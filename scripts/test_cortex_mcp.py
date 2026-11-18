"""MCP tests. Run: python3 -m unittest scripts/test_cortex_mcp.py

Nothing here touches a real vault, an agent's configuration or the network. The engine is a stub that echoes the
argv it was given and fails the way brain.py fails (its own mark on stderr, its own exit code), so what gets
tested is the mapping in both directions: a tool call to an engine call, and an engine exit code back to a model.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cortex_mcp  # noqa: E402

NL = chr(10)
STUB = NL.join([
    'import sys',
    'a = sys.argv[3:]                              # past --vault <path>',
    'if "--version" in sys.argv:',
    '    print("brain.py 9.9.9")',
    '    sys.exit(0)',
    'for x in a:',
    '    if x.startswith("@"):',
    '        code, _, msg = x[1:].partition("|")',
    '        if msg.startswith("TB:"):',
    '            sys.stderr.write("Traceback (most recent call last):" + chr(10) + "  File x, line 1" + chr(10))',
    '            sys.stderr.write(msg[3:] + chr(10))',
    '        else:',
    '            print(msg, file=sys.stderr)',
    '        sys.exit(int(code))',
    'print("argv " + " ".join(a))',
    '',
]) + NL
SLEEP = 'import time' + NL + 'time.sleep(20)' + NL     # a call that will not answer inside the test's timeout
ENGINE = HERE.parent / "template-vault" / "_brain" / "scripts" / "brain_core.py"


def engine_mark() -> str:
    """The character brain.py puts in front of every error it raises itself, taken from the engine's own source."""
    m = re.search(r'print\(f"(.) \{e\}"', ENGINE.read_text(encoding="utf-8"))
    return m.group(1) if m else "✖"


MARK = engine_mark()


def fake_vault(tmp: Path, engine: str = STUB) -> Path:
    """A folder shaped like a vault: a config with its name, and an engine where brain.py would be."""
    v = tmp / "My Cortex"
    (v / "_brain" / "scripts").mkdir(parents=True)
    (v / "_brain" / "config.json").write_text(json.dumps({"vault": "My Cortex"}), encoding="utf-8")
    (v / "_brain" / "scripts" / "brain.py").write_text(engine, encoding="utf-8")
    return v


def call(action: str, **extra) -> dict:
    """A tools/call body for one action."""
    return {"action": action, **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cortex-mcp-"))
        self.v = fake_vault(self.tmp)
        self.st = cortex_mcp.Server(self.v, "claude", 10.0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ask(self, arguments):
        """One tools/call answered by the server object, with no pipe in the way."""
        msg = {"method": "tools/call", "id": 1, "params": {"name": "cortex", "arguments": arguments}}
        return cortex_mcp.answer(msg, self.st)

    def text_of(self, reply):
        return reply["result"]["content"][0]["text"]

    def ran(self, arguments):
        """The argv the stub got for this call, as one string."""
        return self.text_of(self.ask(arguments))

    def call_of(self, rid, arguments):
        """One tools/call line for the pipe."""
        return json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                           "params": {"name": "cortex", "arguments": arguments}})


class ArgvTests(Base):
    def test_each_action_becomes_the_engines_own_command(self):
        self.assertEqual(cortex_mcp.argv_for(call("recall", query="hub tls"), None), ["recall", "hub tls"])
        self.assertEqual(cortex_mcp.argv_for(call("recall", query="hub tls", all=True, no_body=True), None),
                         ["recall", "hub tls", "--all", "--no-body"])
        self.assertEqual(cortex_mcp.argv_for(call("read", title="Hub TLS", full=True), None),
                         ["read", "Hub TLS", "--full"])
        self.assertEqual(cortex_mcp.argv_for(call("read", title="Cold One", cold="need the date"), None),
                         ["read", "Cold One", "--cold", "need the date"])
        self.assertEqual(cortex_mcp.argv_for(call("credit", used=["A", "B"]), None), ["credit", "--used", "A", "B"])
        self.assertEqual(cortex_mcp.argv_for(call("stats"), None), ["stats"])

    def test_a_learning_carries_the_flags_the_engine_defines(self):
        cmd = cortex_mcp.argv_for(call("learn", up="Hub", title="TLS names", summary="One file lists them.",
                                       content="names.json holds the SANs.", evidence="read it 2026-11-03",
                                       type="fact", truth="current", contradicts="Old TLS Note",
                                       verify_intent="config", system=["ubuntu"], verify_keywords=["tls", "san"],
                                       verify=["path /x/names.json", "grep san in /x/names.json"]), "claude")
        self.assertEqual(cmd[:2], ["learn", "--up"])
        self.assertEqual(cmd[cmd.index("--verify-keywords") + 1:cmd.index("--verify")], ["tls", "san"])
        self.assertEqual(cmd.count("--verify"), 2, "each verify line is its own argument")
        self.assertEqual(cmd[-2:], ["--by", "claude"])

    def test_the_server_author_is_used_only_when_the_call_gives_none(self):
        base = call("learn", up="U", title="T", summary="S.", content="C.", evidence="E.")
        self.assertEqual(cortex_mcp.argv_for(base, "strata")[-2:], ["--by", "strata"])
        self.assertEqual(cortex_mcp.argv_for(dict(base, by="codex"), "strata")[-2:], ["--by", "codex"])

    def test_no_action_passes_a_session(self):
        """The automatic session belongs to the vault, so this process never asks for its own."""
        calls = [call("recall", query="q"), call("read", title="T"), call("credit", used=["T"]), call("stats"),
                 call("learn", up="U", title="T", summary="S.", content="C.", evidence="E.")]
        for a in calls:
            self.assertNotIn("--session", cortex_mcp.argv_for(a, "claude"), a["action"])

    def test_a_wrong_call_is_refused_before_the_engine_is_asked(self):
        wrong = [({"action": "consolidate"}, "action must be one of"),
                 ({"action": "recall"}, "needs query"),
                 ({"action": "recall", "query": "  "}, "needs query"),
                 ({"action": "recall", "query": "q", "sidewinder": True}, "does not take sidewinder"),
                 ({"action": "read"}, "needs title"),
                 ({"action": "credit"}, "needs used"),
                 ({"action": "credit", "used": "A"}, "needs used as a list"),
                 ({"action": "credit", "used": ["A", " "]}, "needs used as a list"),
                 ({"action": "learn", "up": "U"}, "needs title"),
                 ({"action": "learn", "up": "U", "title": "T", "summary": "S.", "content": "C.", "evidence": "E.",
                   "verify": [""]}, "verify takes read-only checks")]
        for a, message in wrong:
            with self.assertRaises(cortex_mcp.BadCall) as got:
                cortex_mcp.argv_for(a, None)
            self.assertIn(message, str(got.exception), a["action"])


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.props = cortex_mcp.tool_schema()["inputSchema"]["properties"]

    def test_one_tool_carries_the_whole_funnel(self):
        schema = cortex_mcp.tool_schema()
        self.assertEqual(schema["name"], "cortex")
        self.assertEqual(self.props["action"]["enum"], list(cortex_mcp.ACTIONS + cortex_mcp.COMBINED))
        self.assertEqual(schema["inputSchema"]["required"], ["action"])
        self.assertIs(schema["inputSchema"]["additionalProperties"], False)

    def test_the_schema_and_the_argument_check_agree(self):
        """A property the schema offers and argv_for refuses would strand a model in the middle of a call."""
        offered = set(self.props) - {"action"}
        self.assertEqual(offered, set().union(*(set(spec) for spec in cortex_mcp.ARGS.values())))
        for name, spec in self.props.items():
            self.assertTrue(spec["description"], f"{name} would reach the model with no description")

    def test_flags_are_flags_and_lists_are_lists(self):
        for n in cortex_mcp.BOOLS:
            self.assertEqual(self.props[n]["type"], "boolean", n)
        for n in cortex_mcp.LISTS:
            self.assertEqual(self.props[n]["items"]["type"], "string", n)
        self.assertEqual(self.props["query"]["type"], "string")

    def test_the_description_holds_what_a_small_model_has_to_remember(self):
        d = cortex_mcp.describe()
        for phrase in ("at most two per question", "budget or limit", "stop reading", "[[Title]]", "retry once",
                       "Never store secrets"):
            self.assertIn(phrase, d, phrase)


class ProtocolTests(Base):
    def reply(self, method, params=None, rid=7):
        return cortex_mcp.answer({"method": method, "id": rid, "params": params or {}}, self.st)

    def test_initialize_answers_with_the_revision_the_client_asked_for(self):
        got = self.reply("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(got["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(got["result"]["capabilities"], {"tools": {}})
        self.assertEqual(got["result"]["serverInfo"], {"name": "cortex", "version": "9.9.9"})

    def test_initialize_tells_the_agent_this_vault_is_its_memory(self):
        self.st.by = "claude-code"
        text = self.reply("initialize", {"protocolVersion": "2025-06-18"})["result"]["instructions"]
        self.assertIn('long-term memory: the Cortex vault "My Cortex"', text)
        self.assertIn("You are claude-code.", text)
        for step in ("action=brief", "action=ask", "action=finish", "commits the learnings under your name"):
            self.assertIn(step, text)
        self.st.role = "reader"
        text = self.reply("initialize", {"protocolVersion": "2025-06-18"})["result"]["instructions"]
        self.assertNotIn("learnings =", text)
        self.assertIn("action=finish", text)

    def test_a_revision_this_server_does_not_know_is_answered_with_its_own(self):
        got = self.reply("initialize", {"protocolVersion": "3141-15-92"})
        self.assertEqual(got["result"]["protocolVersion"], cortex_mcp.PROTOCOL)

    def test_tools_list_offers_the_one_tool(self):
        tools = self.reply("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["cortex"])
        self.assertEqual(tools[0]["inputSchema"]["properties"]["action"]["enum"], list(cortex_mcp.ACTIONS + cortex_mcp.COMBINED))

    def test_a_notification_gets_no_answer(self):
        for method in ("notifications/initialized", "notifications/cancelled"):
            self.assertIsNone(cortex_mcp.answer({"method": method, "params": {}}, self.st), method)

    def test_an_unknown_method_says_what_this_server_answers(self):
        got = self.reply("prompts/list")
        self.assertEqual(got["error"]["code"], -32601)
        self.assertNotIn("result", got)

    def test_a_tool_this_server_lacks_is_a_jsonrpc_error_not_a_tool_error(self):
        msg = {"method": "tools/call", "id": 5, "params": {"name": "vault_read", "arguments": {}}}
        got = cortex_mcp.answer(msg, self.st)
        self.assertEqual(got["error"]["code"], -32602)
        self.assertIn("cortex", got["error"]["message"])

    def test_ping_answers_an_empty_result(self):
        self.assertEqual(self.reply("ping")["result"], {})


class ExitTests(Base):
    """The engine's exit code is the contract: 0 the text is the answer, 1 the arguments were wrong, 2 the vault
    is spent. argparse exits 2 as well, so the mark on stderr is what tells a spent budget from a bad call."""

    def test_zero_hands_the_engines_text_over_as_the_answer(self):
        reply = self.ask(call("recall", query="hub tls"))
        self.assertIs(reply["result"]["isError"], False)
        self.assertEqual(self.text_of(reply), "argv recall hub tls")

    def test_the_flags_a_model_passes_reach_the_engine(self):
        self.assertEqual(self.ran(call("recall", query="hub tls", all=True, no_body=True)),
                         "argv recall hub tls --all --no-body")
        self.assertEqual(self.ran(call("read", title="Cold One", cold="need the date", full=True)),
                         "argv read Cold One --full --cold need the date")

    def test_a_question_that_read_nothing_is_still_an_answer(self):
        self.assertEqual(self.ran(call("recall", query="@0|")), "(the engine answered nothing)")

    def test_a_vault_that_is_spent_says_stop_reading(self):
        spent = "@2|" + MARK + " limit of 10 queries in this session: answer with what you have"
        reply = self.ask(call("recall", query=spent))
        self.assertIs(reply["result"]["isError"], True)
        text = self.text_of(reply)
        self.assertIn("answer with what you have", text, "the engine's own reason was dropped")
        self.assertIn("Budget or limit spent", text)

    def test_a_spent_provisional_limit_tells_a_learning_where_to_go_instead(self):
        full = "@2|" + MARK + " 41 provisional nodes await confirmation: write this one to _inbox instead"
        text = self.text_of(self.ask(call("learn", up="U", title=full, summary="S.", content="C.", evidence="E.")))
        self.assertIn("_inbox instead", text)
        self.assertNotIn("Budget", text, "a full pending queue is not a spent reading budget")


    def test_wrong_arguments_come_back_as_the_engines_own_no(self):
        text = self.text_of(self.ask(call("read", title="@1|" + MARK + " no such node: nope")))
        self.assertIn("no such node", text)
        self.assertIn(MARK, text, "the model has to see the engine's line, not a paraphrase of it")
        self.assertNotIn("Budget", text)

    def test_argparse_exits_2_too_and_that_one_means_ask_again(self):
        usage = "@2|usage: brain.py stats [-h] title {provisional,seed,consolidated,obsolete}"
        reply = self.ask(call("read", title=usage))
        self.assertIs(reply["result"]["isError"], True)
        text = self.text_of(reply)
        self.assertIn("The arguments were wrong: fix them and retry", text)
        self.assertNotIn("Budget or limit", text, "a malformed call is not a spent budget")

    def test_an_engine_that_crashes_is_not_blamed_on_the_model(self):
        boom = "@1|TB:FileNotFoundError: [Errno 2] No such file or directory: 'auto.json.tmp'"
        text = self.text_of(self.ask(call("recall", query=boom)))
        self.assertIn("crashed", text)
        self.assertIn("FileNotFoundError", text, "the last line of the traceback is the useful one")
        self.assertNotIn("Traceback", text, "six lines of traceback teach a small model nothing")
        self.assertIn("retry once", text)

    def test_a_call_that_never_answers_is_stopped_and_says_so(self):
        brain = self.v / "_brain" / "scripts" / "brain.py"
        brain.write_text(SLEEP, encoding="utf-8")
        ok, text = cortex_mcp.call(self.v, brain, "claude", 0.2, call("recall", query="q"))
        self.assertIs(ok, False)
        self.assertIn("stopped", text)
        self.assertIn("narrower", text)


class GateTests(Base):
    """One engine call at a time in this process: every read writes the automatic session and the telemetry, and
    the vault lock covers neither, so a second call waits rather than trampling the first one's bookkeeping."""

    def test_a_second_call_while_one_runs_says_try_again_instead_of_racing(self):
        self.st.gate.acquire()
        try:
            ok, text = cortex_mcp.call(self.v, self.st.brain, "claude", 0.1, call("stats"), self.st.gate)
        finally:
            self.st.gate.release()
        self.assertIs(ok, False)
        self.assertIn("another call", text)

    def test_the_gate_is_free_again_after_a_call(self):
        ok, _ = cortex_mcp.call(self.v, self.st.brain, "claude", 10.0, call("stats"), self.st.gate)
        self.assertIs(ok, True)
        self.assertTrue(self.st.gate.acquire(blocking=False), "the call kept the vault to itself")
        self.st.gate.release()

    def test_a_call_that_could_not_even_start_still_frees_the_gate(self):
        vault = self.v / "gone" / "deeper"                    # its folder is not there, so nothing can start
        ok, text = cortex_mcp.call(vault, self.st.brain, "claude", 10.0, call("stats"), self.st.gate)
        self.assertIs(ok, False)
        self.assertIn("cannot run", text)
        self.assertTrue(self.st.gate.acquire(blocking=False), "a failed call kept the vault to itself")
        self.st.gate.release()


class PipeTests(Base):
    def serve(self, *lines):
        """Feed the real server lines on stdin, close it, and get back every line it answered."""
        p = subprocess.run([sys.executable, str(HERE / "cortex_mcp.py"), "--vault", str(self.v)],
                           input="".join(x + NL for x in lines), capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return [json.loads(x) for x in p.stdout.splitlines() if x.strip()]

    def request(self, rid, method, params=None):
        return json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

    def test_a_client_over_stdio_gets_the_engines_answer(self):
        got = self.serve(
            self.request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "probe", "version": "0"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            self.request(2, "tools/list"),
            self.call_of(3, call("recall", query="hub tls", no_body=True)),
            self.call_of(4, call("consolidate")),
            "not json at all",
            json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}}),
            self.call_of(5, call("stats")))
        by = {r.get("id"): r for r in got}
        self.assertEqual(len(got), 6, "every request answers and no notification does")
        self.assertEqual(by[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(by[1]["result"]["serverInfo"]["version"], "9.9.9", "the vault's own engine, not ours")
        self.assertEqual([t["name"] for t in by[2]["result"]["tools"]], ["cortex"])
        self.assertEqual(by[3]["result"]["content"][0]["text"], "argv recall hub tls --no-body")
        self.assertIs(by[3]["result"]["isError"], False)
        self.assertIs(by[5]["result"]["isError"], False)
        self.assertEqual(by[5]["result"]["content"][0]["text"], "argv stats")
        self.assertIs(by[4]["result"]["isError"], True)
        self.assertIn("action must be one of", by[4]["result"]["content"][0]["text"])
        self.assertEqual(by[None]["error"]["code"], -32700, "a line that is not json is answered too")
        self.assertNotIn("--session", json.dumps(got), "the automatic session is the vault's, never ours")
        for r in got:
            self.assertEqual(r["jsonrpc"], "2.0")

    def test_a_vault_without_an_engine_refuses_to_start(self):
        empty = self.tmp / "NoEngine"
        (empty / "_brain").mkdir(parents=True)
        p = subprocess.run([sys.executable, str(HERE / "cortex_mcp.py"), "--vault", str(empty)],
                           input="", capture_output=True, text=True, timeout=30)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no _brain/scripts/brain.py", p.stderr)

    def test_check_prints_what_this_server_would_offer(self):
        p = subprocess.run([sys.executable, str(HERE / "cortex_mcp.py"), "--vault", str(self.v), "--check"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        offer = json.loads(p.stdout)
        self.assertEqual(offer["vault"], "My Cortex")
        self.assertEqual(offer["engine"], "9.9.9")
        self.assertEqual(offer["tools"][0]["name"], "cortex")


class MiscTests(Base):
    def test_the_vault_names_itself_from_its_config_and_the_engine_from_its_own_answer(self):
        self.assertEqual(self.st.name, "My Cortex")
        self.assertEqual(self.st.version, "9.9.9")

    def test_a_vault_with_an_unreadable_config_keeps_its_folder_name(self):
        v = self.tmp / "Other"
        (v / "_brain" / "scripts").mkdir(parents=True)
        (v / "_brain" / "config.json").write_text("{not json", encoding="utf-8")
        st = cortex_mcp.Server(v, None, 10.0)
        self.assertEqual(st.name, "Other")
        self.assertEqual(st.version, "unknown", "an engine that will not answer is no engine")

    def test_the_server_knows_the_mark_the_engine_marks_its_own_errors_with(self):
        self.assertEqual(cortex_mcp.ENGINE_MARK, MARK, "server and engine disagree about who was wrong")

    def test_a_long_answer_is_cut_saying_so_rather_than_silently(self):
        short = "x" * 500
        self.assertEqual(cortex_mcp.cap(short), short)
        long = cortex_mcp.cap("x" * 30_000)
        self.assertEqual(long.count("x"), cortex_mcp.MAX_TEXT, "it cuts at the cap, not somewhere else")
        self.assertIn("cut at", long)
        self.assertIn("ask for less", long)

    def test_two_threads_reading_one_vault_over_one_server_do_not_lose_a_reply(self):
        """A client may send several calls at once; each has to be answered exactly once."""
        lines = [self.call_of(i, call("stats")) for i in range(8)]
        by = {}
        p = subprocess.Popen([sys.executable, str(HERE / "cortex_mcp.py"), "--vault", str(self.v)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        for line in lines:
            p.stdin.write(line + NL)
        p.stdin.close()
        for line in iter(p.stdout.readline, ""):
            reply = json.loads(line)
            rid = reply["id"]
            self.assertNotIn(rid, by, f"id {rid} was answered twice")
            by[rid] = reply
        p.stdout.close()
        p.wait(timeout=30)
        self.assertEqual(sorted(by), list(range(8)), "every call got its one answer")
        self.assertTrue(all(r["result"]["content"][0]["text"] == "argv stats" for r in by.values()),
                        "a serialized call still answers with its own engine run")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CombinedTests(Base):
    """Combined actions: several engine calls in one round trip, against the stub that echoes its argv."""

    def test_ask_is_a_recall_then_the_titles_it_names(self):
        out = self.ran(call("ask", query="hub tls", titles=["Hub TLS"]))
        self.assertIn("## recall\nargv recall hub tls", out)
        self.assertIn("## read Hub TLS\nargv read Hub TLS", out)

    def test_finish_credits_then_learns(self):
        out = self.ran(call("finish", used=["A"], learnings=[{"up": "Hub", "title": "T", "summary": "S",
                                                              "content": "C", "evidence": "E"}]))
        self.assertLess(out.index("argv credit --used A"), out.index("argv learn --up Hub"))
        self.assertIn("--by claude", out)

    def test_health_runs_the_four_checks(self):
        out = self.ran(call("health"))
        for x in ("argv stats", "argv session status", "argv lint --quiet", "0 broken references"):
            self.assertIn(x, out)

    def test_a_batch_runs_in_order_and_stops_at_the_first_failure(self):
        out = self.ran(call("batch", steps=[call("stats"), call("read", title="@1|✖ no node"), call("stats")]))
        self.assertIn("## step 1: stats", out)
        self.assertIn("step 2: read (failed", out)
        self.assertNotIn("## step 3", out)

    def test_a_batch_refuses_a_batch_inside_it_and_too_many_steps(self):
        self.assertIn("never a batch", self.ran(call("batch", steps=[call("batch", steps=[])])))
        self.assertIn("at most", self.ran(call("batch", steps=[call("stats")] * 13)))

    def test_a_sibling_is_read_and_never_written(self):
        sib = fake_vault(self.tmp / "other")
        (self.tmp / "Sib Cortex").symlink_to(sib, target_is_directory=True)
        self.assertIn("argv stats", self.ran(call("stats", vault="Sib Cortex")))
        self.assertIn("runs only on this vault", self.ran(call("learn", vault="Sib Cortex", up="a", title="b",
                                                             summary="c", content="d", evidence="e")))
        self.assertIn("no vault named", self.ran(call("stats", vault="Nope")))
        self.assertIn("sibling vault's name", self.ran(call("stats", vault="../etc")))


class CurateTests(unittest.TestCase):
    """The curator verbs come from the engine's own parser: the template's engine is the reference."""

    def setUp(self):
        self.spec = cortex_mcp.engine_spec(ENGINE.parent / "brain.py")

    def test_every_engine_verb_but_the_funnel_and_the_hook_is_offered(self):
        self.assertIn("link", self.spec)
        self.assertIn("rename", self.spec)
        for v in ("recall", "read", "credit", "learn", "session", "precommit"):
            self.assertNotIn(v, self.spec)

    def test_args_become_the_engines_own_command_line(self):
        self.assertEqual(cortex_mcp.curate_argv(self.spec, "link", {"source": "A", "target": "B", "type": "related",
                                                                    "reason": "r"}),
                         ["link", "A", "B", "--type", "related", "--reason", "r"])
        self.assertEqual(cortex_mcp.curate_argv(self.spec, "lint", {"quiet": True}), ["lint", "--quiet"])
        self.assertEqual(cortex_mcp.curate_argv(self.spec, "external", {"action": "add", "title": "T",
                                                                        "in_vault": "S", "note": "N"}),
                         ["external", "add", "T", "--in", "S", "--note", "N"])

    def test_a_wrong_call_is_refused_before_the_engine(self):
        for verb, args in (("nope", {}), ("link", {"source": "A"}), ("link", {"bogus": 1}),
                           ("status", {"title": "T", "status": "golden"}), ("rename", {"old": ["a"], "new": "b"})):
            with self.assertRaises(cortex_mcp.BadCall, msg=verb):
                cortex_mcp.curate_argv(self.spec, verb, args)

    def test_only_the_owners_server_marks_a_node_consolidated(self):
        a = {"title": "T", "status": "consolidated"}
        with self.assertRaises(cortex_mcp.BadCall):
            cortex_mcp.curate_argv(self.spec, "status", a)
        self.assertEqual(cortex_mcp.curate_argv(self.spec, "status", a, True), ["status", "T", "consolidated"])

    def test_a_reader_server_offers_no_curate_tool(self):
        tmp = Path(tempfile.mkdtemp(prefix="cortex-mcp-"))
        try:
            st = cortex_mcp.Server(fake_vault(tmp), "x", 10.0, "reader")
            tools = cortex_mcp.answer({"method": "tools/list", "id": 1}, st)["result"]["tools"]
            self.assertEqual([t["name"] for t in tools], ["cortex"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class HttpTests(unittest.TestCase):
    """One service for every vault: a real HTTP server on a free localhost port."""

    def setUp(self):
        from http.server import ThreadingHTTPServer
        self.tmp = Path(tempfile.mkdtemp(prefix="cortex-mcp-"))
        fake_vault(self.tmp)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cortex_mcp.http_handler(cortex_mcp.Hub(self.tmp, 10.0)))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, path, body, headers=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.url + path, json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
                return r.status, json.loads(data) if data else None
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_a_vault_is_reached_by_its_slug_and_its_name(self):
        for path in ("/mcp/my", "/mcp/My%20Cortex"):
            code, got = self.post(path, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": "cortex", "arguments": {"action": "stats"}}})
            self.assertEqual(code, 200, path)
            self.assertIn("argv stats", got["result"]["content"][0]["text"])

    def test_by_and_role_come_from_the_query(self):
        code, got = self.post("/mcp/my?by=codex&role=reader", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([t["name"] for t in got["result"]["tools"]], ["cortex"])

    def test_notifications_are_accepted_with_no_body(self):
        self.assertEqual(self.post("/mcp/my", {"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)

    def test_unknown_vault_and_foreign_origin_are_refused(self):
        self.assertEqual(self.post("/mcp/nope", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0], 404)
        self.assertEqual(self.post("/mcp/my", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                   {"Origin": "http://evil.example"})[0], 403)

    def test_get_is_405_on_a_vault_path_and_404_elsewhere(self):
        import urllib.error
        import urllib.request
        for path, code in (("/mcp/my", 405), ("/.well-known/oauth-protected-resource", 404)):
            with self.assertRaises(urllib.error.HTTPError) as got:
                urllib.request.urlopen(self.url + path, timeout=10)
            self.assertEqual(got.exception.code, code, path)

    def test_two_agents_on_one_vault_share_one_gate(self):
        hub = cortex_mcp.Hub(self.tmp, 10.0)
        self.assertIs(hub.server("my", "codex", "curator").gate, hub.server("my", "hermes", "reader").gate)

    def test_a_cortex_localhost_origin_is_local(self):
        self.assertEqual(self.post("/mcp/my", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                   {"Origin": "http://claude.cortex.localhost:8790"})[0], 200)


class TokenTests(HttpTests):
    """--require-token: each vault answers only to the token kept in its own _brain/state/mcp.token."""

    def setUp(self):
        from http.server import ThreadingHTTPServer
        self.tmp = Path(tempfile.mkdtemp(prefix="cortex-mcp-"))
        self.vault = fake_vault(self.tmp)
        (self.vault / ".gitignore").write_text("_brain/state/sessions/\n", encoding="utf-8")
        self.token = cortex_mcp.ensure_token(self.vault)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         cortex_mcp.http_handler(cortex_mcp.Hub(self.tmp, 10.0), require_token=True))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def post(self, path, body, headers=None):
        return super().post(path, body, {"Authorization": f"Bearer {self.token}", **(headers or {})})

    def test_the_token_lives_in_the_vault_and_stays_out_of_git(self):
        self.assertEqual(cortex_mcp.token_path(self.vault).read_text(encoding="utf-8").strip(), self.token)
        self.assertGreaterEqual(len(self.token), 40)
        self.assertEqual(cortex_mcp.ensure_token(self.vault), self.token, "made once, then kept")
        gi = (self.vault / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(gi.count(cortex_mcp.TOKEN_IGNORE), 1)
        self.assertIn("_brain/state/sessions/", gi)

    def test_no_token_or_a_wrong_one_is_refused(self):
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(HttpTests.post(self, "/mcp/my", ping)[0], 401)
        self.assertEqual(HttpTests.post(self, "/mcp/my", ping, {"Authorization": "Bearer guess"})[0], 401)
        self.assertEqual(HttpTests.post(self, "/mcp/my", ping, {"Authorization": self.token})[0], 401, "Bearer only")
        self.assertEqual(self.post("/mcp/my", ping)[0], 200)

    def test_a_new_token_on_disk_replaces_the_cached_one(self):
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(self.post("/mcp/my", ping)[0], 200)
        cortex_mcp.token_path(self.vault).write_text("rotated-token\n", encoding="utf-8")
        self.assertEqual(self.post("/mcp/my", ping)[0], 401)
        self.assertEqual(HttpTests.post(self, "/mcp/my", ping, {"Authorization": "Bearer rotated-token"})[0], 200)


class BriefTests(Base):
    def test_brief_gives_the_hot_nodes_and_the_session_and_with_a_query_the_ask(self):
        (self.v / "MAP.md").write_text("# MAP\n\n## Hot\nWhat was needed.\n- [[A]] — first\n- [[B]] — second\n\n"
                                       "## How to read\n- [[C]] — not hot\n", encoding="utf-8")
        out = self.ran(call("brief"))
        self.assertIn("- [[A]] — first\n- [[B]] — second", out)
        self.assertNotIn("[[C]]", out)
        self.assertIn("argv session status", out)
        self.assertNotIn("## recall", out)
        out = self.ran(call("brief", query="hub tls", reads=0))
        self.assertIn("## recall\nargv recall hub tls", out)

    def test_brief_without_a_map_says_so(self):
        self.assertIn("no MAP.md yet", self.ran(call("brief")))


TEMPLATE = HERE.parent / "template-vault"


def real_vault(tmp: Path, name: str = "Real Cortex") -> Path:
    """A vault with the real engine from template-vault/ and three nodes, made through the engine itself."""
    v = tmp / name
    shutil.copytree(TEMPLATE / "_brain", v / "_brain", ignore=shutil.ignore_patterns("__pycache__", "tests"))
    shutil.copytree(TEMPLATE / "_templates", v / "_templates")
    shutil.copy(TEMPLATE / ".gitignore", v / ".gitignore")   # as a real vault: engine state stays out of git
    cfg = json.loads((v / "_brain" / "config.json").read_text(encoding="utf-8"))
    (v / "_brain" / "config.json").write_text(json.dumps(dict(cfg, vault=name)), encoding="utf-8")
    brain = v / "_brain" / "scripts" / "brain.py"
    for args in (["new", "--layer", "1", "--title", "Garden", "--summary", "The garden beds and their care."],
                 ["new", "--layer", "2", "--up", "Garden", "--title", "Water before 9 am",
                  "--summary", "Beds are watered in the morning only, so leaves dry before night."],
                 ["new", "--layer", "2", "--up", "Garden", "--title", "Slugs are the main pest",
                  "--summary", "Slugs eat the lettuce; copper tape keeps them off the beds."]):
        subprocess.run([sys.executable, str(brain), "--vault", str(v), *args], check=True, capture_output=True)
    return v


class ResidentTests(unittest.TestCase):
    """The engine imported once and called in this process gives what a new process gives, and its warm file list
    follows the vault: at once after a writing call, within scan_seconds after an edit from outside."""

    @classmethod
    def setUpClass(cls):
        cls.res = cortex_mcp.Resident(TEMPLATE / "_brain" / "scripts", scan_seconds=0.2, active_seconds=60)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cortex-res-"))
        self.v = real_vault(self.tmp)
        self.brain = self.v / "_brain" / "scripts" / "brain.py"

    def tearDown(self):
        cortex_mcp.RESIDENT = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def both(self, args):
        cortex_mcp.RESIDENT = None
        sub = cortex_mcp.run_engine(self.brain, self.v, args, 30)
        cortex_mcp.RESIDENT = self.res
        return sub, cortex_mcp.run_engine(self.brain, self.v, args, 30)

    def test_it_serves_only_its_own_engine_version(self):
        self.assertTrue(self.res.serves(self.v))
        core = self.v / "_brain" / "scripts" / "brain_core.py"
        core.write_text(core.read_text(encoding="utf-8").replace(f'VERSION = "{self.res.version}"', 'VERSION = "0.0.1"'),
                        encoding="utf-8")
        self.assertFalse(self.res.serves(self.v), "another version runs its own brain.py")

    def test_same_answers_as_a_new_process(self):
        for args in (["stats"], ["recall", "when are the beds watered", "--no-body"]):
            sub, res = self.both(args)
            self.assertTrue(sub[0] and res[0], (sub, res))
            self.assertEqual(res[1].splitlines()[0], sub[1].splitlines()[0], args)
        sub, res = self.both(["read", "No Such Node"])
        self.assertEqual((sub[0], res[0]), (False, False))
        sub, res = self.both(["recall"])           # argparse: a missing query
        self.assertEqual((sub[0], res[0]), (False, False))
        self.assertIn("arguments were wrong", res[1])

    def test_a_writing_call_refreshes_the_file_list_at_once(self):
        cortex_mcp.RESIDENT = self.res
        self.assertTrue(cortex_mcp.run_engine(self.brain, self.v, ["stats"], 30)[0])
        ok, got = cortex_mcp.run_engine(self.brain, self.v, ["new", "--layer", "2", "--up", "Garden", "--title",
                                                             "Mulch keeps the soil moist", "--summary",
                                                             "A layer of mulch halves the watering."], 30)
        self.assertTrue(ok, got)
        ok, got = cortex_mcp.run_engine(self.brain, self.v, ["recall", "mulch soil moist", "--no-body"], 30)
        self.assertIn("Mulch keeps the soil moist", got)

    def test_an_outside_edit_shows_up_within_scan_seconds(self):
        cortex_mcp.RESIDENT = self.res
        cortex_mcp.run_engine(self.brain, self.v, ["stats"], 30)
        key = str(self.v.resolve())
        before = len(self.res.lists[key][1])
        (self.v / "Garden" / "Compost is turned weekly.md").write_text("x", encoding="utf-8")
        deadline = time.time() + 5
        while time.time() < deadline and len(self.res.lists[key][1]) == before:
            time.sleep(0.1)
        self.assertEqual(len(self.res.lists[key][1]), before + 1)

    def test_output_is_captured_per_thread(self):
        cortex_mcp.RESIDENT = self.res
        seen = []
        stop = threading.Event()

        def chatter():                              # another thread writing while an engine call runs
            while not stop.is_set():
                seen.append(sys.stderr.write(""))
        t = threading.Thread(target=chatter)
        t.start()
        try:
            ok, got = cortex_mcp.run_engine(self.brain, self.v, ["stats"], 30)
        finally:
            stop.set()
            t.join()
        self.assertTrue(ok)
        self.assertIsNone(getattr(sys.stderr.local, "buf", None))


GIT_ID = {"GIT_AUTHOR_NAME": "Owner", "GIT_AUTHOR_EMAIL": "owner@example.com",
          "GIT_COMMITTER_NAME": "Owner", "GIT_COMMITTER_EMAIL": "owner@example.com"}


class TaskSessionTests(unittest.TestCase):
    """brief opens the agent's task session; finish credits, learns, closes it and commits only what the task
    changed, under the agent's name (engine 2.16+, a server that commits)."""

    def setUp(self):
        import os
        self.tmp = Path(tempfile.mkdtemp(prefix="cortex-task-"))
        self.v = real_vault(self.tmp)
        self.env = {**os.environ, **GIT_ID}
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")
        self.st = cortex_mcp.Server(self.v, "claude-code", 30.0)
        self.st.commit = True

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.v), *args], capture_output=True, text=True, env=self.env)

    def learning(self, title):
        return {"up": "Water before 9 am", "title": title, "summary": "Compost is turned every week to keep it warm.",
                "content": "Turn the compost every Sunday.", "evidence": "the owner said so on 2026-11-18",
                "truth": "historical"}

    def test_brief_opens_the_task_session_and_finish_closes_and_commits_it(self):
        ok, got = cortex_mcp.run(self.st, {"action": "brief", "query": "when are the beds watered"})
        self.assertTrue(ok, got)
        sid = re.search(r"This task's session is (s[\w-]+)", got).group(1)
        task = self.v / "_brain" / "state" / "sessions" / "task-claude-code.json"
        self.assertEqual(json.loads(task.read_text(encoding="utf-8"))["sid"], sid)
        (self.v / "Garden" / "Owner note.md").write_text("the owner's own edit\n", encoding="utf-8")
        mark = self.v / "_brain" / "state" / "bookkeeping.txt"   # engine bookkeeping that a vault tracks in git
        mark.write_text("1\n", encoding="utf-8")
        self.git("add", "-f", "_brain/state/bookkeeping.txt")
        self.git("commit", "-q", "-m", "track bookkeeping")
        mark.write_text("2\n", encoding="utf-8")                  # changed during the task, before finish
        ok, got = cortex_mcp.run(self.st, {"action": "finish", "used": ["Water before 9 am"],
                                           "learnings": [self.learning("Compost is turned weekly")]})
        self.assertTrue(ok, got)
        self.assertIn(f"session {sid} closed", got)
        self.assertFalse(task.exists())
        self.assertRegex(got, r"committed \w+ as claude-code <claude-code@cortex\.local>")
        self.assertIn("left alone, already changed before this task: Garden/Owner note.md", got)
        log = self.git("log", "-1", "--format=%an <%ae>|%s|%b").stdout.strip()
        self.assertTrue(log.startswith("claude-code <claude-code@cortex.local>|learning: Compost is turned weekly|"), log)
        self.assertNotIn("Co-Authored-By", log)
        files = self.git("show", "--name-only", "--format=", "HEAD").stdout.split("\n")
        self.assertNotIn("Garden/Owner note.md", files)
        self.assertIn("_brain/state/bookkeeping.txt", files, "the engine's bookkeeping goes with the task")
        self.assertIn('?? "Garden/Owner note.md"', self.git("status", "--porcelain").stdout, "the owner's edit stays")

    def test_nothing_is_committed_while_something_else_is_staged(self):
        (self.v / "staged.md").write_text("x\n", encoding="utf-8")
        self.git("add", "staged.md")
        ok, got = cortex_mcp.run(self.st, {"action": "finish", "learnings": [self.learning("Mulch is laid in May")]})
        self.assertTrue(ok, got)
        self.assertIn("something else is already staged", got)
        self.assertEqual(self.git("log", "--format=%s").stdout.split("\n")[0], "seed")

    def test_a_server_that_does_not_commit_leaves_git_alone(self):
        self.st.commit = False
        ok, got = cortex_mcp.run(self.st, {"action": "finish", "learnings": [self.learning("Beans climb the fence")]})
        self.assertTrue(ok, got)
        self.assertNotIn("## commit", got)
        self.assertEqual(self.git("log", "--format=%s").stdout.split("\n")[0], "seed")

    def test_session_is_refused_on_an_older_engine(self):
        core = self.v / "_brain" / "scripts" / "brain_core.py"
        core.write_text(re.sub(r'VERSION = "[^"]+"', 'VERSION = "2.15.0"', core.read_text(encoding="utf-8")),
                        encoding="utf-8")
        with self.assertRaises(cortex_mcp.BadCall):
            cortex_mcp.argv_for({"action": "recall", "query": "x", "session": "s1"}, "claude-code",
                                cortex_mcp.has_sessions(self.v))
        self.assertEqual(cortex_mcp.argv_for({"action": "recall", "query": "x"}, "claude-code", False), ["recall", "x"])


MODERN_META = {cortex_mcp.META_VERSION: cortex_mcp.MODERN, cortex_mcp.META_CAPS: {}}


class ModernTests(Base):
    """MCP 2026-07-28: no handshake, every request names its revision; the Skills extension serves the curator skills."""

    def ask(self, method, params=None, meta=MODERN_META, headers=None):
        return cortex_mcp.answer({"jsonrpc": "2.0", "id": 3, "method": method,
                                  "params": dict(params or {}, _meta=meta)}, self.st, headers)

    def test_discover_names_the_revisions_the_tools_and_the_skills(self):
        got = self.ask("server/discover")["result"]
        self.assertIn(cortex_mcp.MODERN, got["supportedVersions"])
        self.assertIn("2025-11-25", got["supportedVersions"])
        self.assertEqual(got["resultType"], "complete")
        self.assertEqual(got["_meta"][cortex_mcp.META_SERVER]["name"], "cortex")
        self.assertEqual(got["capabilities"]["extensions"], {cortex_mcp.SKILLS_EXT: {}})
        self.assertIn("resources", got["capabilities"])

    def test_tools_answer_without_initialize(self):
        got = self.ask("tools/list")["result"]
        self.assertEqual((got["resultType"], got["cacheScope"]), ("complete", "private"))
        self.assertGreater(got["ttlMs"], 0)
        got = self.ask("tools/call", {"name": "cortex", "arguments": {"action": "stats"}})["result"]
        self.assertEqual(got["resultType"], "complete")
        self.assertIn("argv stats", got["content"][0]["text"])

    def test_bad_requests_get_the_new_error_codes(self):
        self.assertEqual(self.ask("tools/list", meta={cortex_mcp.META_VERSION: cortex_mcp.MODERN})["error"]["code"],
                         -32602, "no client capabilities")
        err = self.ask("tools/list", meta={cortex_mcp.META_VERSION: "2099-01-01", cortex_mcp.META_CAPS: {}})["error"]
        self.assertEqual((err["code"], err["data"]["requested"]), (-32022, "2099-01-01"))
        self.assertEqual(self.ask("tools/list", headers={"MCP-Protocol-Version": "2025-06-18"})["error"]["code"], -32020)
        self.assertEqual(self.ask("ping")["error"]["code"], -32601, "ping is gone in 2026-07-28")

    def test_older_clients_keep_their_handshake(self):
        got = cortex_mcp.answer({"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}, self.st)
        self.assertEqual(got["result"]["protocolVersion"], "2025-11-25")
        self.assertNotIn("resultType", cortex_mcp.answer({"id": 2, "method": "tools/list"}, self.st)["result"])

    def test_skills_are_listed_got_and_read_with_matching_digests(self):
        import hashlib
        skills = self.ask("skills/list")["result"]["skills"]
        names = sorted(s["frontmatter"]["name"] for s in skills)
        self.assertEqual(names, ["atomize", "capture", "consolidate", "link", "review"])
        link = next(s for s in skills if s["frontmatter"]["name"] == "link")
        self.assertEqual(link["uri"], "skill://link/SKILL.md")
        self.assertTrue(link["frontmatter"]["description"])
        self.assertEqual(self.ask("skills/get", {"uri": link["uri"]})["result"]["skill"], link)
        text = self.ask("resources/read", {"uri": link["uri"]})["result"]["contents"][0]["text"]
        self.assertIn("cortex_curate", text, "the note on how B maps to the tools")
        res = next(r for r in link["resources"] if r["uri"] == link["uri"])
        self.assertEqual(res["digest"], "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(self.ask("resources/read", {"uri": "skill://link/../../etc/passwd"})["error"]["code"], -32602)

    def test_a_reader_gets_no_skills(self):
        self.st.role = "reader"
        self.assertEqual(self.ask("skills/list")["result"]["skills"], [])
        self.assertNotIn("extensions", self.ask("server/discover")["result"]["capabilities"])


class ModernHttpTests(HttpTests):
    def test_modern_errors_carry_their_http_status(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": MODERN_META}}
        self.assertEqual(self.post("/mcp/my", body, {"MCP-Protocol-Version": cortex_mcp.MODERN,
                                                     "Mcp-Method": "tools/list"})[0], 200)
        self.assertEqual(self.post("/mcp/my", body, {"MCP-Protocol-Version": "2025-06-18"})[0], 400)
        unknown = dict(body, method="nope/nothing")
        self.assertEqual(self.post("/mcp/my", unknown, {"MCP-Protocol-Version": cortex_mcp.MODERN})[0], 404)
