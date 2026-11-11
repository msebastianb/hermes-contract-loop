#!/usr/bin/env python3
"""contract-loop driver.

The orchestrating (expensive) model writes a contract: small tasks, each with
the files it may touch and the commands that prove it done. This script then
runs the loop without it: a cheap coder model implements each task, the checks
decide, a decision model gates what checks cannot see, and the orchestrator is
only called back with a short packet when something needs judgment.

Stdlib only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
EXIT_USAGE, EXIT_ESCALATED, EXIT_FAILED = 2, 3, 4
JUDGE_TEXT_BYTES = 24000  # Jev degrades above ~32 kB of text; stay under it.


def say(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = EXIT_USAGE) -> None:
    print("error: " + msg, file=sys.stderr)
    sys.exit(code)


def tail(text: str, lines: int) -> str:
    rows = text.rstrip().splitlines()
    if len(rows) <= lines:
        return "\n".join(rows)
    return "[... %d lines omitted ...]\n" % (len(rows) - lines) + "\n".join(rows[-lines:])


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[... clipped, %d more chars ...]" % (len(text) - limit)


# ---------------------------------------------------------------- config

def load_roles() -> dict:
    roles = json.loads((SKILL_DIR / "roles.json").read_text())
    override = os.environ.get("CONTRACT_LOOP_ROLES")
    path = Path(override) if override else Path.home() / ".config/contract-loop/roles.json"
    if path.is_file():
        for key, val in json.loads(path.read_text()).items():
            if isinstance(val, dict) and isinstance(roles.get(key), dict):
                roles[key].update(val)
            else:
                roles[key] = val
    return roles


def api_key(roles: dict):
    """Environment first; a key file only if the user opted in via roles.key_file."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    key_file = roles.get("key_file")
    if not key_file:
        return None
    try:
        for line in Path(key_file).expanduser().read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.+)", line)
            if m:
                return m.group(1).strip().strip("'\"")
    except OSError:
        return None
    return None


def state_dir(repo: Path) -> Path:
    root = os.environ.get("CONTRACT_LOOP_STATE")
    if not root:
        root = os.path.join(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state"), "contract-loop")
    return Path(root) / ("%s-%s" % (repo.name, hashlib.sha1(str(repo).encode()).hexdigest()[:8]))


class Run:
    """One contract run against one repository."""

    def __init__(self, repo: Path, must_exist: bool = True):
        self.repo = repo
        self.dir = state_dir(repo)
        self.roles = load_roles()
        self.state_path = self.dir / "state.json"
        if must_exist and not self.state_path.is_file():
            die("no run for %s; start with: cl.py init" % repo)
        self.st = json.loads(self.state_path.read_text()) if self.state_path.is_file() else {}

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, indent=1))
        tmp.replace(self.state_path)

    def contract(self) -> dict:
        path = self.dir / "contract.json"
        if not path.is_file():
            die("missing %s" % path)
        try:
            c = json.loads(path.read_text())
        except ValueError as e:
            die("contract.json is not valid JSON: %s" % e)
        errs = validate_contract(c)
        if errs:
            die("contract.json invalid:\n  - " + "\n  - ".join(errs))
        return c

    def log(self, name: str) -> Path:
        d = self.dir / "log"
        d.mkdir(parents=True, exist_ok=True)
        return d / name

    def spend(self, kind: str, usage) -> None:
        cost = (usage or {}).get("cost")
        if isinstance(cost, (int, float)):
            sp = self.st.setdefault("spend", {})
            sp[kind] = round(sp.get(kind, 0) + cost, 6)


def validate_contract(c: dict) -> list:
    errs = []
    for key in ("goal", "stack", "tasks", "launch"):
        if not c.get(key):
            errs.append("missing '%s'" % key)
    stack = c.get("stack") or {}
    if not (isinstance(stack, dict) and stack.get("decision") and stack.get("why")):
        errs.append("stack needs 'decision' and 'why' (decide it, do not leave it to the coder)")
    launch = c.get("launch") or {}
    if not (isinstance(launch, dict) and launch.get("cmd") and isinstance(launch.get("ready"), dict)
            and set(launch["ready"]) & {"http", "stdout", "exit"}):
        errs.append("launch needs 'cmd' and 'ready' with one of http / stdout / exit")
    if not isinstance(c.get("protected", []), list):
        errs.append("'protected' must be a list of globs")
    seen = set()
    for i, t in enumerate(c.get("tasks") or []):
        tid = t.get("id") or "#%d" % i
        if not re.match(r"^[A-Za-z0-9_.-]+$", str(t.get("id", ""))):
            errs.append("task %s: id must match [A-Za-z0-9_.-]+" % tid)
        if tid in seen:
            errs.append("task %s: duplicate id" % tid)
        seen.add(tid)
        if not t.get("spec"):
            errs.append("task %s: missing spec" % tid)
        for key in ("allowed", "checks"):
            v = t.get(key)
            if not (isinstance(v, list) and v and all(isinstance(x, str) and x for x in v)):
                errs.append("task %s: '%s' must be a non-empty list of strings" % (tid, key))
        ui = t.get("ui")
        if ui is not None and not (isinstance(ui, dict) and ui.get("url") and ui.get("criterion")):
            errs.append("task %s: ui needs 'url' and 'criterion'" % tid)
    return errs


# ---------------------------------------------------------------- git

def git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, text=True, errors="replace")
    if check and r.returncode:
        die("git %s: %s" % (" ".join(args), (r.stderr or r.stdout).strip()), EXIT_FAILED)
    return r.stdout


def git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True).returncode == 0


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def changed(repo: Path) -> list:
    """Paths that differ from HEAD, tracked or not. Unstages first so renames show as add+delete."""
    git(repo, "reset", "-q")
    out = git(repo, "status", "--porcelain", "-z", "--untracked-files=all")
    return [e[3:] for e in out.split("\0") if e]


def revert(repo: Path, path: str) -> None:
    if git_ok(repo, "cat-file", "-e", "HEAD:" + path):
        git(repo, "checkout", "HEAD", "--", path)
    else:
        try:
            (repo / path).unlink()
        except OSError:
            pass


def matches(path: str, globs: list) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


def work_diff(repo: Path) -> str:
    """Diff of the working tree against HEAD, untracked files included."""
    git(repo, "add", "-A")
    diff = git(repo, "diff", "--cached", "HEAD")
    git(repo, "reset", "-q")
    return diff


def commit(repo: Path, message: str):
    git(repo, "add", "-A")
    r = subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message],
                       capture_output=True, text=True, errors="replace")
    if r.returncode:
        git(repo, "reset", "-q")
        return None, (r.stdout + r.stderr).strip()
    return head(repo), ""


# ---------------------------------------------------------------- checks

def run_checks(repo: Path, cmds: list, timeout: int, logp: Path):
    """Run commands in order; stop at the first failure. Returns (ok, failure_text)."""
    with open(logp, "a") as log:
        for cmd in cmds:
            try:
                r = subprocess.run(cmd, shell=True, cwd=str(repo), capture_output=True, text=True,
                                   errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
                out, rc = r.stdout + r.stderr, r.returncode
            except subprocess.TimeoutExpired as e:
                raw = (e.stdout or b"") + (e.stderr or b"")
                out = (raw.decode(errors="replace") if isinstance(raw, bytes) else raw) + "\n[timed out after %ds]" % timeout
                rc = 124
            log.write("$ %s\n[exit %d]\n%s\n\n" % (cmd, rc, out))
            if rc:
                return False, "$ %s   (exit %d)\n%s" % (cmd, rc, tail(out, 60))
    return True, ""


def fingerprint(text: str) -> str:
    """Stable id of a failure: numbers, addresses and timings stripped."""
    norm = re.sub(r"0x[0-9a-fA-F]+|\d+(?:\.\d+)?", "N", text)
    return hashlib.sha1(norm.encode()).hexdigest()


# ---------------------------------------------------------------- models

def agent(role: dict, prompt: str, repo: Path, logp: Path, image: Path = None):
    """One-shot Hermes child on the given model. Returns (exit_code, stdout)."""
    pf = logp.with_suffix(".prompt.md")
    pf.write_text(prompt)
    budget = int(role.get("budget_s", 900))
    cmd = shlex.split(os.environ.get("CONTRACT_LOOP_HERMES", "hermes")) + [
        "chat", "--query-file", str(pf), "-Q", "--oneshot",
        "-m", role["model"], "--provider", role.get("provider", "openrouter"),
        "--max-turns", str(role.get("max_turns", 60)), "--run-budget", str(budget),
        "-t", role.get("toolsets", "terminal,file"),
        "--source", "tool",
        "--ignore-rules",  # no memory, no AGENTS.md, no skills: smaller prompt, nothing personal leaves
    ]
    if image and Path(image).is_file():
        cmd += ["--image", str(image)]  # the coder looks at the mockup itself, not at a description of it
    try:
        r = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, errors="replace",
                           timeout=budget + 180, stdin=subprocess.DEVNULL)
        rc, out = r.returncode, r.stdout + ("\n[stderr]\n" + r.stderr if r.stderr.strip() else "")
    except subprocess.TimeoutExpired:
        rc, out = 124, "[agent timed out]"
    except OSError as e:
        rc, out = 127, "[cannot start agent: %s]" % e
    logp.write_text(out)
    return rc, out


def api(run: Run, path: str, body: dict, timeout: int = 180):
    """POST to OpenRouter. Returns (json, None) or (None, reason). Never raises, never prints the key."""
    key = api_key(run.roles)
    if not key:
        return None, "no API key (set OPENROUTER_API_KEY or roles.key_file)"
    base = os.environ.get("CONTRACT_LOOP_API_BASE", "https://openrouter.ai")
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json", "X-Title": "contract-loop"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %d: %s" % (e.code, e.read()[:300].decode(errors="replace"))
    except Exception as e:  # network, JSON, timeout: the judge is optional by design
        return None, "%s: %s" % (type(e).__name__, e)


def judge(run: Run, state, questions: dict):
    """Ask the decision model typed questions. Returns (answers, None) or (None, reason)."""
    if run.st.get("no_judge"):
        return None, "judge disabled"
    res, err = api(run, "/api/alpha/decisions",
                   {"model": run.roles["judge"]["model"], "state": state, "questions": questions})
    if err:
        return None, err
    run.spend("judge", res.get("usage"))
    answers = res.get("answers")
    if not isinstance(answers, dict):
        return None, "malformed decisions response"
    return answers, None


def image_part(path: Path) -> dict:
    kind = "jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "png"
    src = path
    # Un mockup grande (1800px, 1.4 MB) viene rifiutato dal giudice con
    # "Invalid image dimensions": si manda una copia ridotta. sips c'e' su macOS.
    if path.stat().st_size > 700_000 and shutil.which("sips"):
        small = path.with_name(path.stem + "-gate.jpg")
        if not small.is_file() or small.stat().st_mtime < path.stat().st_mtime:
            subprocess.run([shutil.which("sips"), "-Z", "1400", "-s", "format", "jpeg",
                            str(path), "--out", str(small)], capture_output=True)
        if small.is_file() and 0 < small.stat().st_size < path.stat().st_size:
            src, kind = small, "jpeg"
    data = base64.b64encode(src.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": "data:image/%s;base64,%s" % (kind, data)}}


# ---------------------------------------------------------------- prompts

SCOUT_PROMPT = """You are scouting the repository in the current directory for an architect who will not read the files.
READ ONLY: do not create, modify or delete anything, do not install anything, do not run builds or test suites.

Reply with a brief in Markdown, at most 60 lines, and nothing else:
- Stack: languages, frameworks and versions, taken from the manifests (say "none yet" if the repo is empty).
- Commands: exact install / build / test / run commands, and where each one is defined.
- Map: the directories and the 5-10 files that matter, one line each.
- Conventions: test layout, style, naming, anything a newcomer would get wrong.
- Surprises: anything broken, half-finished or unusual.

Goal the architect has in mind: {goal}
"""

CODER_PROMPT = """You are implementing ONE task in the repository in the current directory. Work only inside it.

# Task {id}: {title}
{spec}
{reference}
# Stack (already decided, do not change it)
{stack}

# Rules
- You may create or modify only files matching: {allowed}
- Never touch files matching: {protected}
  Any change outside the allowed set is reverted automatically after you finish.
- The task is done when every one of these commands exits 0 from the repository root:
{checks}
- Run them yourself before you finish. Make them pass by implementing the task,
  never by hardcoding expected answers, skipping tests or stubbing behaviour.
- Do not commit, stash, or switch branches.
- End with a summary of at most 3 lines. No other prose.

# Repository brief
{brief}
{feedback}"""


def coder_prompt(c: dict, t: dict, brief: str, feedback: str, reference: bool = False) -> str:
    return CODER_PROMPT.format(
        id=t["id"], title=t.get("title", ""), spec=t["spec"].strip(),
        reference=("\n# Visual reference\n"
                   "The image attached to this message is the mockup to reproduce: look at it before you write\n"
                   "any code, and match its layout, its colours, its type sizes and its spacing. Where the spec\n"
                   "and the image disagree about how something looks, the image wins; where the checks and the\n"
                   "image disagree about structure, the checks win.\n" if reference else ""),
        stack=c["stack"]["decision"],
        allowed=", ".join(t["allowed"]),
        protected=", ".join(c.get("protected") or ["(none)"]),
        checks="\n".join("    " + x for x in t["checks"]),
        brief=brief.strip() or "(no brief)",
        feedback=("\n# Your previous attempt was rejected\n" + feedback) if feedback else "")


# ---------------------------------------------------------------- commands

def cmd_init(args) -> None:
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        die("not a directory: %s" % repo)
    if not git_ok(repo, "rev-parse", "--git-dir"):
        git(repo, "init", "-q")
        say("git: initialised a new repository")
    if not git_ok(repo, "rev-parse", "--verify", "-q", "HEAD"):
        git(repo, "commit", "-q", "--allow-empty", "-m", "chore: initial commit")
        say("git: created an empty initial commit")
    if git(repo, "status", "--porcelain").strip():
        die("working tree is not clean; commit or stash first")
    run = Run(repo, must_exist=False)
    if run.st and not args.force:
        die("a run already exists in %s (use --force to archive it and start over)" % run.dir)
    if run.dir.exists():
        run.dir.rename(run.dir.with_name(run.dir.name + time.strftime(".old-%Y%m%d-%H%M%S")))
    base = head(repo)
    branch = "cl/" + time.strftime("%Y%m%d-%H%M%S")
    git(repo, "checkout", "-q", "-b", branch)
    run.st = {"repo": str(repo), "branch": branch, "base": base, "sealed": None, "tasks": {}, "spend": {}}
    run.save()
    shutil.copy(str(SKILL_DIR / "templates/contract.example.json"), str(run.dir / "contract.example.json"))
    say("branch: %s (from %s)" % (branch, base[:8]))
    say("state:  %s" % run.dir)
    say("next:   cl.py scout --goal '...'   then write %s" % (run.dir / "contract.json"))


def cmd_scout(args) -> None:
    run = Run(Path(args.repo).resolve())
    role = run.roles["scout"]
    rc, out = agent(role, SCOUT_PROMPT.format(goal=args.goal or "(not stated)"), run.repo, run.log("scout.out"))
    dirty = changed(run.repo)
    for p in dirty:
        revert(run.repo, p)
    if rc:
        die("scout failed (exit %d); see %s" % (rc, run.log("scout.out")), EXIT_FAILED)
    # -Q prints tool previews (box-drawing prefixed) before the final answer; keep the answer only.
    brief = "\n".join(l for l in out.splitlines() if not l.startswith(("│", "┊", "session_id:"))).strip()
    (run.dir / "brief.md").write_text(brief + "\n")
    say("brief: %s (%d lines)%s" % (run.dir / "brief.md", len(brief.splitlines()),
                                    "; reverted %d file(s) the scout touched" % len(dirty) if dirty else ""))


def cmd_seal(args) -> None:
    run = Run(Path(args.repo).resolve())
    c = run.contract()
    if git(run.repo, "rev-parse", "--abbrev-ref", "HEAD").strip() != run.st["branch"]:
        die("not on run branch %s" % run.st["branch"])
    if git(run.repo, "status", "--porcelain").strip():
        sha, err = commit(run.repo, "cl: contract - " + c["goal"][:60])
        if not sha:
            die("cannot commit the acceptance checks: %s" % err, EXIT_FAILED)
    run.st["sealed"] = head(run.repo)
    old = run.st.get("tasks", {})
    run.st["tasks"] = {t["id"]: old.get(t["id"], {"status": "pending", "attempts": 0, "flags": []}) for t in c["tasks"]}
    run.save()
    say("sealed at %s: %d task(s), launch = %s" % (run.st["sealed"][:8], len(c["tasks"]), c["launch"]["cmd"]))


def escalate(run: Run, t: dict, rec: dict, reason: str, failure: str) -> None:
    lines = int(run.roles["limits"].get("packet_lines", 200))
    diff = work_diff(run.repo)
    d = run.dir / "escalations"
    d.mkdir(exist_ok=True)
    path = d / (t["id"] + ".md")
    path.write_text("\n".join([
        "# Escalation: task %s" % t["id"], "",
        "Reason: %s" % reason,
        "Attempts by the coder: %d" % rec["attempts"], "",
        "## Spec", t["spec"].strip(), "",
        "## Checks", "\n".join("    " + x for x in t["checks"]), "",
        "## Last failure", "```", failure.strip() or "(checks passed)", "```", "",
        "## Coder's last diff (left in the working tree, uncommitted)", "```diff", tail(diff, lines) or "(empty)", "```", "",
        "## Your options",
        "- Fix it yourself in the working tree, then: cl.py resolve %s" % t["id"],
        "- Rewrite the task in contract.json, then: cl.py seal && cl.py run --retry %s" % t["id"], ""]))
    rec.update(status="escalated", reason=reason)
    run.save()
    say("ESCALATE %s: %s" % (t["id"], reason))
    say("packet: %s" % path)


def task_image(run: "Run", c: dict, t: dict):
    """The picture the coder must look at: the task's own, the contract's, or its generated mockup."""
    for cand in (t.get("reference"), c.get("reference")):
        if cand:
            p = Path(cand).expanduser()
            p = p if p.is_absolute() else (run.repo / p)
            if p.is_file():
                return p
    m = run.dir / "mockups"
    if m.is_dir():
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            p = m / (t["id"] + ext)
            if p.is_file():
                return p
    return None


def gate_scope(run: Run, c: dict, t: dict, diff: str):
    """Does the diff do the task and only the task? Returns (verdict, p). verdict: accept|doubt|reject|unjudged."""
    answers, err = judge(run, {
        "task": t["spec"], "allowed_files": t["allowed"], "diff": clip(diff, JUDGE_TEXT_BYTES)}, {
        "in_scope": {
            "type": "noul",
            "instructions": "Does `diff` implement what `task` asks, honestly and without unrelated changes?",
            "criteria": {
                "true": "Every change in the diff serves the task and implements real behaviour.",
                "false": "The diff has changes unrelated to the task, hardcodes expected test answers, "
                         "stubs or disables behaviour, or weakens a check.",
            }}})
    if err:
        return "unjudged", None, err
    p = (answers.get("in_scope") or {}).get("noul")
    if not isinstance(p, (int, float)):
        return "unjudged", None, "no noul in answer"
    j = run.roles["judge"]
    return ("accept" if p >= j["accept"] else "reject" if p <= j["reject"] else "doubt"), p, None


def same_failure(run: Run, prev: str, cur: str) -> bool:
    if fingerprint(prev) == fingerprint(cur):
        return True
    answers, err = judge(run, {"previous_failure": clip(prev, 8000), "current_failure": clip(cur, 8000)}, {
        "same": {"type": "noul",
                 "instructions": "Do `previous_failure` and `current_failure` show the same root failure?",
                 "criteria": {"true": "Same failing check for the same reason; the attempt made no real progress.",
                              "false": "A different check fails, or the same check fails for a different reason."}}})
    if err:
        return False
    p = (answers.get("same") or {}).get("noul")
    return isinstance(p, (int, float)) and p >= run.roles["judge"]["accept"]


def do_task(run: Run, c: dict, t: dict) -> bool:
    """Returns True when the task is committed, False when it was escalated."""
    repo, rec, limits = run.repo, run.st["tasks"][t["id"]], run.roles["limits"]
    protected = c.get("protected") or []
    timeout = int(limits.get("check_timeout_s", 600))
    brief_path = run.dir / "brief.md"
    brief = brief_path.read_text() if brief_path.is_file() else ""

    ok, _ = run_checks(repo, t["checks"], timeout, run.log("%s.pre.checks" % t["id"]))
    if ok:
        rec.update(status="done", by="nobody", commit=head(repo))
        rec["flags"].append("already-green: checks passed before any code was written")
        run.save()
        say("%s: checks already pass before the coder ran (vacuous checks?)" % t["id"])
        return True

    feedback, prev_failure = "", ""
    while rec["attempts"] < int(limits.get("attempts", 3)):
        rec["attempts"] += 1
        n = rec["attempts"]
        run.save()
        before = head(repo)
        img = task_image(run, c, t)
        if img:
            say("%s: mockup attached to the coder (%s)" % (t["id"], img.name))
        rc, _ = agent(run.roles["coder"], coder_prompt(c, t, brief, feedback, img is not None),
                      repo, run.log("%s.a%d.out" % (t["id"], n)), image=img)

        if git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() != run.st["branch"]:
            escalate(run, t, rec, "coder left the run branch", "")
            return False
        if head(repo) != before:  # it committed despite the rule: fold the commits back into the tree
            git(repo, "reset", "-q", "--soft", before)
        notes = []
        for p in changed(repo):
            if matches(p, protected) or not matches(p, t["allowed"]):
                revert(repo, p)
                notes.append("- reverted %s: %s" % (p, "protected file" if matches(p, protected) else "outside the allowed set"))
        if rc:
            notes.append("- the agent process ended abnormally (exit %d)" % rc)

        ok, failure = run_checks(repo, t["checks"], timeout, run.log("%s.a%d.checks" % (t["id"], n)))
        if not ok:
            say("%s attempt %d: checks fail%s" % (t["id"], n, " (%d path(s) reverted)" % len(notes) if notes else ""))
            if prev_failure and same_failure(run, prev_failure, failure):
                escalate(run, t, rec, "same failure twice; more attempts would not help", failure)
                return False
            prev_failure = failure
            feedback = "\n".join(notes + ["", "The checks still fail:", "```", failure, "```"])
            continue

        diff = work_diff(repo)
        if not diff.strip():
            escalate(run, t, rec, "checks pass but the coder changed nothing it was allowed to", "")
            return False
        verdict, p, err = gate_scope(run, c, t, diff)
        rec["gate"] = {"verdict": verdict, "p": p}
        if verdict == "reject":
            escalate(run, t, rec, "checks pass but the scope gate rejected the diff (p=%.2f)" % p, "")
            return False
        if verdict == "doubt":
            rec["flags"].append("doubt: scope gate p=%.2f" % p)
        if verdict == "unjudged":
            rec["flags"].append("unjudged: %s" % err)
        sha, cerr = commit(repo, "cl(%s): %s" % (t["id"], t.get("title") or t["spec"].strip().splitlines()[0][:60]))
        if not sha:
            escalate(run, t, rec, "commit failed: %s" % cerr[:200], "")
            return False
        rec.update(status="done", by="coder", commit=sha)
        run.save()
        say("%s: done in %d attempt(s), gate=%s%s" % (t["id"], n, verdict, "" if p is None else " p=%.2f" % p))
        return True

    escalate(run, t, rec, "attempt limit reached", prev_failure)
    return False


def cmd_run(args) -> None:
    run = Run(Path(args.repo).resolve())
    c = run.contract()
    if not run.st.get("sealed"):
        die("contract not sealed; run: cl.py seal")
    if git(run.repo, "rev-parse", "--abbrev-ref", "HEAD").strip() != run.st["branch"]:
        die("not on run branch %s" % run.st["branch"])
    if args.no_judge:
        run.st["no_judge"] = True
    if args.retry:
        rec = run.st["tasks"].get(args.retry)
        if not rec:
            die("unknown task %s" % args.retry)
        git(run.repo, "reset", "-q", "--hard", "HEAD")
        git(run.repo, "clean", "-fdq")
        rec.update(status="pending", attempts=0, flags=[])
        rec.pop("reason", None)
        run.save()
    for t in c["tasks"]:
        rec = run.st["tasks"].setdefault(t["id"], {"status": "pending", "attempts": 0, "flags": []})
        if rec["status"] == "done":
            continue
        if rec["status"] == "escalated":
            die("task %s is escalated; resolve it first (see %s)" % (t["id"], run.dir / "escalations" / (t["id"] + ".md")),
                EXIT_ESCALATED)
        if git(run.repo, "status", "--porcelain").strip():
            die("working tree is dirty before task %s" % t["id"], EXIT_FAILED)
        if not do_task(run, c, t):
            sys.exit(EXIT_ESCALATED)
    say("all %d task(s) done; next: cl.py close" % len(c["tasks"]))


def cmd_resolve(args) -> None:
    run = Run(Path(args.repo).resolve())
    c = run.contract()
    t = next((x for x in c["tasks"] if x["id"] == args.task), None)
    if not t:
        die("unknown task %s" % args.task)
    rec = run.st["tasks"][t["id"]]
    ok, failure = run_checks(run.repo, t["checks"], int(run.roles["limits"].get("check_timeout_s", 600)),
                             run.log("%s.resolve.checks" % t["id"]))
    if not ok:
        say(failure)
        die("checks still fail; task %s stays escalated" % t["id"], EXIT_FAILED)
    sha = head(run.repo)
    if git(run.repo, "status", "--porcelain").strip():
        sha, err = commit(run.repo, "cl(%s): %s (resolved by orchestrator)" % (t["id"], t.get("title") or "task"))
        if not sha:
            die("commit failed: %s" % err, EXIT_FAILED)
    rec.update(status="done", by="orchestrator", commit=sha)
    rec.pop("reason", None)
    run.save()
    say("%s: resolved and committed %s; continue with: cl.py run" % (t["id"], sha[:8]))


# ---------------------------------------------------------------- close

def launch(repo: Path, spec: dict, logp: Path):
    """Start the product for real. Returns (process, ok, why). Caller must stop() it."""
    logf = open(logp, "w")
    p = subprocess.Popen(spec["cmd"], shell=True, cwd=str(repo), stdout=logf, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    ready = spec["ready"]
    deadline = time.time() + float(spec.get("timeout_s", 60))
    while time.time() < deadline:
        rc = p.poll()
        if "exit" in ready:
            if rc is not None:
                return p, rc == ready["exit"], "exited %d (expected %d)" % (rc, ready["exit"])
        elif rc is not None:
            return p, False, "process exited %d before it was ready" % rc
        elif _ready(ready, logp):
            time.sleep(float(spec.get("hold_s", 2)))  # catch a crash right after startup
            if p.poll() is not None:
                return p, False, "process exited %d right after becoming ready" % p.returncode
            return p, True, "ready"
        time.sleep(0.4)
    return p, False, "not ready after %ss" % spec.get("timeout_s", 60)


def _ready(ready: dict, logp: Path) -> bool:
    if "http" in ready:
        try:
            with urllib.request.urlopen(ready["http"], timeout=3) as r:
                return r.status < 500
        except urllib.error.HTTPError as e:
            return e.code < 500
        except Exception:
            return False
    return re.search(ready["stdout"], logp.read_text(errors="replace")) is not None


def stop(p) -> None:
    if p.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
        except OSError:
            return
        try:
            p.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def find_chrome(roles: dict):
    for cand in [roles.get("chrome"),
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Chromium.app/Contents/MacOS/Chromium",
                 shutil.which("chromium"), shutil.which("google-chrome"), shutil.which("chromium-browser")]:
        if cand and os.path.exists(cand):
            return cand
    return None


def screenshot(roles: dict, url: str, out: Path):
    chrome = find_chrome(roles)
    if not chrome:
        return "no Chrome/Chromium found"
    profile = tempfile.mkdtemp(prefix="cl-chrome-")  # never the user's own profile
    try:
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                        "--window-size=1280,800", "--user-data-dir=" + profile, "--screenshot=" + str(out), url],
                       capture_output=True, timeout=90)
    except (subprocess.TimeoutExpired, OSError) as e:
        return "screenshot failed: %s" % type(e).__name__
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return None if out.is_file() else "screenshot produced no file"


def visual_gate(run: Run, t: dict) -> str:
    shot = run.dir / "shots" / (t["id"] + ".png")
    shot.parent.mkdir(exist_ok=True)
    if shot.exists():
        shot.unlink()
    err = screenshot(run.roles, t["ui"]["url"], shot)
    if err:
        return "NOT RUN (%s)" % err
    state = ["Acceptance criterion: " + t["ui"]["criterion"], "Screenshot of the running product:", image_part(shot)]
    mock = next((m for m in (run.dir / "mockups").glob(t["id"] + ".*")), None) if (run.dir / "mockups").is_dir() else None
    if mock and mock.suffix.lower() in (".png", ".jpg", ".jpeg"):
        state += ["Reference mockup (layout intent only, not pixel-exact):", image_part(mock)]
    answers, jerr = judge(run, state, {"meets": {
        "type": "noul", "instructions": "Does the screenshot of the running product satisfy the acceptance criterion?",
        "criteria": {"true": "The criterion is visibly satisfied.",
                     "false": "The page is blank, shows an error, or does not satisfy the criterion."}}})
    if jerr:
        return "NOT JUDGED (%s); screenshot at %s" % (jerr, shot)
    p = (answers.get("meets") or {}).get("noul")
    if not isinstance(p, (int, float)):
        return "NOT JUDGED (no noul); screenshot at %s" % shot
    j = run.roles["judge"]
    word = "PASS" if p >= j["accept"] else "FAIL" if p <= j["reject"] else "UNSURE"
    return "%s p=%.2f; screenshot at %s" % (word, p, shot)


def risk_of(run: Run, path: str, diff: str):
    answers, err = judge(run, {"file": path, "diff": clip(diff, 8000)}, {"risk": {
        "type": "choice", "instructions": "How much damage could a mistake in this `diff` cause?",
        "criteria": {
            "low": "Tests, docs, comments, formatting, static assets or config with no runtime effect.",
            "normal": "Ordinary application logic.",
            "high": "Authentication or authorisation, secrets, payments, deleting or migrating data, concurrency, "
                    "building shell or SQL commands, parsing untrusted input, or network exposure.",
        }}})
    if err:
        return None
    a = answers.get("risk") or {}
    if (a.get("probabilities") or {}).get("high", 0) >= 0.3:
        return "high"
    return a.get("choice")


def cmd_close(args) -> None:
    run = Run(Path(args.repo).resolve())
    c = run.contract()
    repo, st = run.repo, run.st
    pending = [tid for tid, r in st["tasks"].items() if r["status"] != "done"]
    if pending:
        die("tasks not done: %s" % ", ".join(pending), EXIT_FAILED)
    if git(repo, "status", "--porcelain").strip():
        die("working tree is dirty", EXIT_FAILED)

    cmds = []
    for t in c["tasks"]:
        cmds += [x for x in t["checks"] if x not in cmds]
    checks_ok, failure = run_checks(repo, cmds, int(run.roles["limits"].get("check_timeout_s", 600)), run.log("close.checks"))

    proc, launch_ok, why = launch(repo, c["launch"], run.log("launch.out"))
    visual = {}
    try:
        if launch_ok:
            for t in c["tasks"]:
                if t.get("ui"):
                    visual[t["id"]] = visual_gate(run, t)
    finally:
        stop(proc)
    launch_log = tail(run.log("launch.out").read_text(errors="replace"), 25)

    files = [f for f in git(repo, "diff", "--name-only", st["base"], "HEAD").splitlines() if f]
    risk = {}
    for f in files[:40]:
        risk[f] = risk_of(run, f, git(repo, "diff", st["base"], "HEAD", "--", f))
    show = [f for f in files if risk.get(f) in ("high", None)]
    flagged = [(tid, r) for tid, r in st["tasks"].items() if r.get("flags") or r.get("by") != "coder"]

    out = ["# Close report: %s" % c["goal"], "",
           "- Checks (all tasks, rerun together): %s" % ("PASS" if checks_ok else "FAIL"),
           "- Real launch `%s`: %s (%s)" % (c["launch"]["cmd"], "PASS" if launch_ok else "FAIL", why)]
    for tid, v in visual.items():
        out.append("- Visual %s: %s" % (tid, v))
    out += ["- Spend outside the coder (USD): %s" % json.dumps(st.get("spend", {})), "",
            "## Tasks", "| task | by | attempts | gate | flags |", "|---|---|---|---|---|"]
    for tid, r in st["tasks"].items():
        g = r.get("gate") or {}
        out.append("| %s | %s | %d | %s | %s |" % (
            tid, r.get("by", "?"), r.get("attempts", 0),
            g.get("verdict", "-") + ("" if g.get("p") is None else " %.2f" % g["p"]), "; ".join(r.get("flags", [])) or "-"))
    if not checks_ok:
        out += ["", "## Failing check", "```", failure, "```"]
    if not launch_ok:
        out += ["", "## Launch output", "```", launch_log, "```"]
    out += ["", "## Changed files (%s..HEAD)" % st["base"][:8], "```",
            git(repo, "diff", "--stat", st["base"], "HEAD").rstrip(), "```", "",
            "## Risk per file",
            "\n".join("- %s: %s" % (f, risk.get(f) or "unjudged") for f in files) or "(no files changed)", "",
            "## What you must read",
            "Only the diffs below: files judged high-risk or not judged at all. "
            "Tasks listed under 'flags' above deserve a look at their commit too."]
    budget = int(run.roles["limits"].get("close_diff_chars", 30000))
    for f in show:
        d = git(repo, "diff", st["base"], "HEAD", "--", f)
        piece = clip(d, min(budget, 8000))
        budget -= len(piece)
        out += ["", "### %s (%s)" % (f, risk.get(f) or "unjudged"), "```diff", piece.rstrip(), "```"]
        if budget <= 0:
            out.append("\n[diff budget exhausted; read the rest with git diff]")
            break
    if not show:
        out.append("\n(nothing: every changed file was judged low or normal risk)")
    for tid, r in flagged:
        out.append("\nFlagged task %s: git show %s" % (tid, (r.get("commit") or "?")[:8]))

    path = run.dir / "close.md"
    path.write_text("\n".join(out) + "\n")
    st["closed"] = {"checks": checks_ok, "launch": launch_ok, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    run.save()
    say("checks: %s | launch: %s (%s)%s" % ("PASS" if checks_ok else "FAIL", "PASS" if launch_ok else "FAIL", why,
                                           "".join(" | visual %s: %s" % (k, v.split(";")[0]) for k, v in visual.items())))
    say("report: %s (%d lines) - read it, then give your verdict" % (path, len(out)))
    if not (checks_ok and launch_ok):
        sys.exit(EXIT_FAILED)


def cmd_mockup(args) -> None:
    run = Run(Path(args.repo).resolve())
    t = next((x for x in run.contract()["tasks"] if x["id"] == args.task), None)
    if not t or not t.get("ui"):
        die("task %s has no 'ui' block" % args.task)
    prompt = t["ui"].get("mockup_prompt") or (
        "Clean, realistic UI mockup screenshot, flat design, legible text, no device frame. " + t["ui"]["criterion"])
    res, err = api(run, "/api/v1/images", {"model": run.roles["image"]["model"], "prompt": prompt,
                                           "aspect_ratio": run.roles["image"].get("aspect_ratio", "16:9")}, timeout=300)
    if err:
        die("mockup not generated: %s" % err, EXIT_FAILED)
    run.spend("image", res.get("usage"))
    item = (res.get("data") or [{}])[0]
    if not item.get("b64_json"):
        die("image API returned no image", EXIT_FAILED)
    ext = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(item.get("media_type"), ".png")
    d = run.dir / "mockups"
    d.mkdir(exist_ok=True)
    for old in d.glob(t["id"] + ".*"):
        old.unlink()
    out = d / (t["id"] + ext)
    out.write_bytes(base64.b64decode(item["b64_json"]))
    run.save()
    say("mockup: %s (cost %s USD)" % (out, (res.get("usage") or {}).get("cost", "?")))


def cmd_status(args) -> None:
    run = Run(Path(args.repo).resolve())
    st = run.st
    say("branch %s | sealed %s | state %s" % (st["branch"], (st.get("sealed") or "no")[:8], run.dir))
    for tid, r in st.get("tasks", {}).items():
        say("  %-12s %-10s attempts=%d by=%s %s" % (tid, r["status"], r.get("attempts", 0), r.get("by", "-"),
                                                  r.get("reason") or "; ".join(r.get("flags", []))))
    say("spend outside the coder (USD): %s | judge key: %s" % (
        json.dumps(st.get("spend", {})), "available" if api_key(run.roles) else "absent (gates will be skipped and flagged)"))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="cl.py", description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".", help="target repository (default: current directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="create the run branch and the state directory")
    p.add_argument("--force", action="store_true", help="archive an existing run and start over")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("scout", help="cheap model writes a one-page brief of the repo")
    p.add_argument("--goal", default="")
    p.set_defaults(fn=cmd_scout)
    sub.add_parser("seal", help="validate contract.json and commit the acceptance checks").set_defaults(fn=cmd_seal)
    p = sub.add_parser("run", help="implement the tasks; exits 3 on escalation")
    p.add_argument("--retry", metavar="TASK", help="discard the failed attempt of TASK and start it again")
    p.add_argument("--no-judge", action="store_true", help="skip the decision model for this run")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("resolve", help="accept the orchestrator's own fix for an escalated task")
    p.add_argument("task")
    p.set_defaults(fn=cmd_resolve)
    sub.add_parser("close", help="rerun all checks, launch the product, write close.md").set_defaults(fn=cmd_close)
    p = sub.add_parser("mockup", help="generate a reference mockup for a UI task")
    p.add_argument("task")
    p.set_defaults(fn=cmd_mockup)
    sub.add_parser("status", help="one line per task").set_defaults(fn=cmd_status)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
