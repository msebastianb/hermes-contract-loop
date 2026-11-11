"""Offline tests: a fake coder agent and a fake OpenRouter stand in for the models."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CL = ROOT / "skills/hermes-contract-loop/scripts/cl.py"
sys.path.insert(0, str(CL.parent))
import cl  # noqa: E402

FAKE_AGENT = r'''
import json, os, subprocess, sys
plan = json.load(open(os.environ["FAKE_PLAN"]))
step = plan.pop(0) if plan else {}
json.dump(plan, open(os.environ["FAKE_PLAN"], "w"))
for path, content in step.get("write", {}).items():
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    open(path, "w").write(content)
if step.get("commit"):
    subprocess.run("git add -A && git commit -qm sneaky", shell=True)
print("DONE")
'''


class FakeAPI(http.server.BaseHTTPRequestHandler):
    answers = {}
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAPI.seen.append((self.path, body))
        out = {}
        for qid, q in body.get("questions", {}).items():
            v = FakeAPI.answers.get(qid)
            out[qid] = ({"type": "choice", "choice": v, "probabilities": {v: 0.9}} if q["type"] == "choice"
                        else {"type": "noul", "noul": v})
        data = json.dumps({"answers": out, "usage": {"cost": 0.001}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.repo = self.tmp / "proj"
        (self.repo / "tests").mkdir(parents=True)
        self.sh("git init -q . && git config user.email t@t && git config user.name t")
        (self.repo / "seed.txt").write_text("seed\n")
        self.sh("git add -A && git commit -qm seed")
        fake = self.tmp / "fake_agent.py"
        fake.write_text(FAKE_AGENT)
        self.plan = self.tmp / "plan.json"
        self.env = dict(os.environ, CONTRACT_LOOP_STATE=str(self.tmp / "state"),
                        CONTRACT_LOOP_HERMES="%s %s" % (sys.executable, fake), FAKE_PLAN=str(self.plan),
                        CONTRACT_LOOP_API_BASE="http://127.0.0.1:%d" % self.srv.server_address[1],
                        CONTRACT_LOOP_ROLES=str(self.tmp / "none.json"), OPENROUTER_API_KEY="test-key",
                        PYTHONDONTWRITEBYTECODE="1")  # same-second rewrites of m.py would hit a stale .pyc
        FakeAPI.answers = {"in_scope": 0.95, "same": 0.1, "risk": "normal", "meets": 0.9}
        FakeAPI.seen = []
        self.assertEqual(self.cl("init")[0], 0)
        self.state = next((self.tmp / "state").iterdir())

    def sh(self, cmd):
        return subprocess.run(cmd, shell=True, cwd=str(self.repo), capture_output=True, text=True, check=True).stdout

    def cl(self, *args, env=None):
        r = subprocess.run([sys.executable, str(CL), "--repo", str(self.repo)] + list(args),
                           capture_output=True, text=True, env=env or self.env)
        return r.returncode, r.stdout + r.stderr

    def seal(self, plan, launch=None, tasks=None):
        (self.repo / "tests/acc.py").write_text("import m\nassert m.f() == 2, 'f() must return 2'\n")
        contract = {
            "goal": "f returns 2", "stack": {"decision": "python stdlib", "why": "tiny"},
            "protected": ["tests/**"],
            "launch": launch or {"cmd": "%s -c 'import m; print(m.f())'" % sys.executable, "ready": {"exit": 0}, "timeout_s": 20},
            "tasks": tasks or [{"id": "t1", "title": "f", "spec": "m.f() returns 2", "allowed": ["m.py"],
                                "checks": ["PYTHONPATH=. %s tests/acc.py" % sys.executable]}]}
        (self.state / "contract.json").write_text(json.dumps(contract))
        self.plan.write_text(json.dumps(plan))
        rc, out = self.cl("seal")
        self.assertEqual(rc, 0, out)

    def task(self, tid="t1"):
        return json.loads((self.state / "state.json").read_text())["tasks"][tid]


GOOD = {"write": {"m.py": "def f():\n    return 2\n"}}
BAD = {"write": {"m.py": "def f():\n    return 1\n"}}


class Loop(Base):
    def test_happy_path_commits_and_closes(self):
        self.seal([GOOD])
        rc, out = self.cl("run")
        self.assertEqual(rc, 0, out)
        t = self.task()
        self.assertEqual((t["status"], t["by"], t["attempts"], t["gate"]["verdict"]), ("done", "coder", 1, "accept"))
        self.assertEqual(self.sh("git status --porcelain"), "")
        self.assertIn("cl(t1)", self.sh("git log --oneline -1"))
        rc, out = self.cl("close")
        self.assertEqual(rc, 0, out)
        report = (self.state / "close.md").read_text()
        self.assertIn("Real launch", report)
        self.assertIn("m.py: normal", report)

    def test_guard_reverts_protected_and_out_of_scope_files(self):
        cheat = {"write": {"m.py": "def f():\n    return 1\n", "tests/acc.py": "pass\n", "other.txt": "x"}}
        self.seal([cheat, GOOD])
        rc, out = self.cl("run")
        self.assertEqual(rc, 0, out)
        self.assertIn("f() must return 2", (self.repo / "tests/acc.py").read_text())
        self.assertFalse((self.repo / "other.txt").exists())
        self.assertEqual(self.task()["attempts"], 2)
        prompt = (self.state / "log/t1.a2.prompt.md").read_text()
        self.assertIn("reverted tests/acc.py: protected file", prompt)
        self.assertIn("reverted other.txt: outside the allowed set", prompt)

    def test_same_failure_twice_escalates_then_resolve(self):
        self.seal([BAD, BAD, BAD])
        rc, out = self.cl("run")
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.task()["attempts"], 2)  # third attempt never spent
        packet = (self.state / "escalations/t1.md").read_text()
        self.assertIn("same failure twice", packet)
        self.assertIn("return 1", packet)
        self.assertEqual(self.cl("run")[0], 3)  # refuses to continue past an escalation
        (self.repo / "m.py").write_text("def f():\n    return 2\n")
        rc, out = self.cl("resolve", "t1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.task()["by"], "orchestrator")
        self.assertEqual(self.cl("run")[0], 0)

    def test_scope_gate_reject_escalates_even_when_checks_pass(self):
        FakeAPI.answers["in_scope"] = 0.05
        self.seal([GOOD])
        rc, out = self.cl("run")
        self.assertEqual(rc, 3, out)
        self.assertIn("scope gate rejected", out)
        self.assertNotIn("cl(t1)", self.sh("git log --oneline"))

    def test_scope_gate_doubt_commits_with_flag(self):
        FakeAPI.answers["in_scope"] = 0.5
        self.seal([GOOD])
        self.assertEqual(self.cl("run")[0], 0)
        self.assertIn("doubt", self.task()["flags"][0])

    def test_without_key_gates_are_skipped_and_flagged(self):
        env = dict(self.env)
        del env["OPENROUTER_API_KEY"]
        self.seal([GOOD])
        rc, out = self.cl("run", env=env)
        self.assertEqual(rc, 0, out)
        self.assertIn("unjudged", self.task()["flags"][0])
        rc, out = self.cl("close", env=env)
        self.assertEqual(rc, 0, out)
        self.assertIn("m.py: unjudged", (self.state / "close.md").read_text())
        self.assertIn("### m.py", (self.state / "close.md").read_text())

    def test_coder_commit_is_folded_back(self):
        self.seal([dict(GOOD, commit=True)])
        rc, out = self.cl("run")
        self.assertEqual(rc, 0, out)
        log = self.sh("git log --oneline")
        self.assertNotIn("sneaky", log)
        self.assertIn("cl(t1)", log)

    def test_already_green_checks_are_flagged(self):
        (self.repo / "m.py").write_text("def f():\n    return 2\n")
        self.seal([])
        self.assertEqual(self.cl("run")[0], 0)
        t = self.task()
        self.assertEqual(t["by"], "nobody")
        self.assertIn("already-green", t["flags"][0])

    def test_close_fails_when_product_does_not_start(self):
        self.seal([GOOD], launch={"cmd": "%s -c 'raise SystemExit(7)'" % sys.executable,
                                  "ready": {"stdout": "listening"}, "timeout_s": 10})
        self.assertEqual(self.cl("run")[0], 0)
        rc, out = self.cl("close")
        self.assertEqual(rc, 4, out)
        self.assertIn("exited 7 before it was ready", (self.state / "close.md").read_text())

    def test_close_shows_high_risk_diff(self):
        FakeAPI.answers["risk"] = "high"
        self.seal([GOOD])
        self.assertEqual(self.cl("run")[0], 0)
        self.assertEqual(self.cl("close")[0], 0)
        report = (self.state / "close.md").read_text()
        self.assertIn("### m.py (high)", report)
        self.assertIn("+def f():", report)

    def test_launch_ready_on_stdout_and_process_is_stopped(self):
        server = "%s -u -c \"import time; print('listening'); time.sleep(60)\"" % sys.executable
        self.seal([GOOD], launch={"cmd": server, "ready": {"stdout": "listening"}, "timeout_s": 15, "hold_s": 0.2})
        self.assertEqual(self.cl("run")[0], 0)
        rc, out = self.cl("close")
        self.assertEqual(rc, 0, out)
        self.assertEqual(subprocess.run("pgrep -f \"print\\('listening'\\)\"", shell=True, capture_output=True).returncode, 1)


class Units(unittest.TestCase):
    def test_contract_validation(self):
        errs = cl.validate_contract({"goal": "g", "stack": {"decision": "x"}, "launch": {"cmd": "x", "ready": {}},
                                     "tasks": [{"id": "a b", "allowed": [], "checks": ["x"]}]})
        text = "\n".join(errs)
        for needle in ("stack needs", "launch needs", "id must match", "missing spec", "'allowed' must be"):
            self.assertIn(needle, text)
        ok = json.loads((ROOT / "skills/hermes-contract-loop/templates/contract.example.json").read_text())
        self.assertEqual(cl.validate_contract(ok), [])

    def test_fingerprint_ignores_numbers(self):
        self.assertEqual(cl.fingerprint("fail at 0x7f3a in 0.12s line 40"), cl.fingerprint("fail at 0x11bc in 3.4s line 41"))
        self.assertNotEqual(cl.fingerprint("KeyError"), cl.fingerprint("TypeError"))

    def test_key_file_is_opt_in(self):
        old = os.environ.pop("OPENROUTER_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as d:
                f = Path(d) / ".env"
                f.write_text("OTHER=1\nexport OPENROUTER_API_KEY='abc'\n")
                self.assertIsNone(cl.api_key({"key_file": None}))
                self.assertEqual(cl.api_key({"key_file": str(f)}), "abc")
        finally:
            if old:
                os.environ["OPENROUTER_API_KEY"] = old


if __name__ == "__main__":
    unittest.main()
