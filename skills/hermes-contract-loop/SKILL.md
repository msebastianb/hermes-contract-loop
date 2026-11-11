---
name: hermes-contract-loop
description: Use when building code with cheap models under a contract.
version: 0.2.0
author: msebastianb, Hermes Agent
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [coding, multi-model, orchestration, cost]
    related_skills: []
---

# Contract Loop

You (the strong, expensive model) write a contract and judge the result. A script does the
rest: a cheap model scouts and codes, executable checks decide, a decision model (Jev) gates
what checks cannot see. You are called back only with short packets. Your budget is two turns
of real thinking, plus escalations.

## When to Use

- A coding task big enough to split into 2+ small tasks with executable acceptance checks.
- Don't use for: one-line fixes, pure exploration, or work with no way to check it by command.

## Prerequisites

- `hermes` on PATH with an OpenRouter key (the coder runs as `hermes chat --oneshot`).
- Optional judge and mockups: the script needs the key itself, from `OPENROUTER_API_KEY` or
  from the file named in `key_file` of `~/.config/contract-loop/roles.json`. Without it the
  gates are skipped and every task is flagged `unjudged` for you to read.
- Models and limits live in `roles.json` (override in `~/.config/contract-loop/roles.json`).
- The skill is called `hermes-contract-loop`, but its runtime artefacts keep the short name:
  `~/.config/contract-loop/`, `~/.local/state/contract-loop/`, `CONTRACT_LOOP_*` and `cl.py`.
  Do not rename them: existing runs lose their state and the key override stops being found.

## Quick Reference

Run with `terminal`, from the target repo. Define a shell function, not a variable:
`cl() { python3 "<skill_dir>/scripts/cl.py" "$@"; }` — zsh does not word-split `$CL`, so a
variable holding `python3 path` fails with "no such file or directory". `$CL` below means that function.

| Command | Does |
|---|---|
| `$CL init` | Clean-tree check, branch `cl/<stamp>`, state dir outside the repo |
| `$CL scout --goal "..."` | Cheap model writes `brief.md` (≤ 60 lines) |
| `$CL seal` | Validates `contract.json`, commits your acceptance checks |
| `$CL run` | Implements tasks in order. Exit 3 = escalation packet waiting |
| `$CL resolve <task>` | Accepts your own fix for an escalated task |
| `$CL run --retry <task>` | Discards the failed attempt, starts the task again |
| `$CL mockup <task>` | Reference image for a task with a `ui` block |
| `$CL close` | Reruns all checks, launches the product, writes `close.md`. Exit 4 = failed |
| `$CL status` | One line per task, spend so far |

## Procedure

1. **Init and scout.** Run `init` FIRST, before writing any file in the repo: it refuses a
   dirty tree, and an acceptance check written beforehand counts as dirt. On a new directory
   it runs `git init` and an empty commit itself. `init` prints the state dir; capture it and
   stop if it is empty, or the contract gets written to `/contract.json`. Then `scout`; skip
   it on an empty repo (nothing to brief). Read `brief.md` only; do not open repo files
   unless the brief is clearly insufficient. Done when: you can name the stack and the test command.
2. **Write the contract** to `<state>/contract.json` (shape: `contract.example.json` there).
   - Decide the stack yourself, with a reason. On an empty repo this is your decision, not the coder's.
   - Split into tasks a mid-level developer finishes in under an hour. Each task: `spec`,
     `allowed` globs, `checks`.
   - Write the acceptance checks yourself, as files under a `protected` glob. They must fail
     now and pass only when the task is really done. This is where your tokens belong.
   - `launch` is the real start command of the product plus how to know it is up
     (`http`, `stdout` regex, or `exit` code). No launch, no close.
   - UI tasks get a `ui` block (`url`, `criterion`); run `mockup` if a visual target helps.
   Done when: `seal` prints the task count without errors.
3. **Run.** Foreground `terminal` caps at 600 s. A single small task finishes in well under a
   minute, so run it in the foreground with `timeout=600`; for several tasks start `run` with
   `background=true, notify=true` and end the turn. Read only its stdout.
   Done when: exit 0, or exit 3 with a packet path.
4. **On escalation** read the packet (spec, last failure, last diff) and nothing else. Either
   fix it in the working tree and `resolve <task>`, or rewrite the task, `seal`, and
   `run --retry <task>`. A task flagged `already-green` means your check was vacuous: fix the check.
   Done when: `run` exits 0.
5. **Close.** Run `close`, read `close.md`: verdict lines, task table, and only the diffs it
   shows (high-risk or unjudged files, flagged tasks). Give the user a verdict with what you
   read and what you did not. Done when: checks PASS, launch PASS, and you have read every diff listed.
6. **Hand over.** The work is on branch `cl/<stamp>`. Do not merge or push unless asked.

## Running it well

- Tokens are the cost of this skill: batch `init → contract → seal → run → close` into as few
  `terminal` calls as possible, read only what the step needs, and keep the final report to
  verdict, what was verified, what was not.
- The key belongs to the user: never set `key_file` or read a secrets file on your own. Run
  without it and say which gates were skipped.
- Decide stack, models and task split yourself and say why in one line: the loop exists to spend
  the strong model's turns on the contract and the checks, not on questions.
- Throwaway trials go in a scratch directory; real work stays on the `cl/<stamp>` branch, unpushed.
- After editing an installed copy of this skill, port the edit back to the repo *before* running
  `install.sh`: it copies repo → installed and silently drops edits that exist only in the
  installed copy.

## Building to a mockup

`mockup <task>` writes `mockups/<task>.<ext>`; the run attaches it to the coder with
`hermes chat --image` (needs a vision model — deepseek-v4.1-flash accepts images) and the judge
gets it too. The coder looks at the picture; your spec only carries what the checks demand.

- A generated mockup is almost always @2x: divide every pixel measurement by 2 to get CSS px,
  and compare the page at that CSS width (a 1822x1024 PNG is a 911x512 page).
- Measure the image, do not eyeball it: PIL in a script (`im.load()`, luminance thresholds) gives
  the card box, the bar height, the text line pitch. Then aim the page at those numbers.
- Any `rem` inside a fluid component defeats the scale: the card's paragraphs stayed at 16.8px
  while the card itself was clamped to 11px. Use `em` and `clamp()` throughout the component.
- Auto-hyphenation is not a design tool: `hyphens: auto` and `&shy;` moved the break when the
  column changed width. Set the type size so the natural break lands where the image has it.
- An element inside a hidden parent disappears: the mobile menu button sat inside `<nav>`, so
  hiding the nav took the button with it. Hide the links, not the container.


## Pitfalls

- The coder gets no memory, no AGENTS.md and no skills (`--ignore-rules`): everything it needs
  must be in the task `spec` or the brief.
- Globs use fnmatch: `*` also crosses `/`, so `src/*` covers the whole tree under `src`.
- Files outside `allowed` or inside `protected` are reverted after each attempt; gitignored
  files are invisible to that guard.
- Jev returns probabilities, never reasons. `doubt` and `unjudged` are for you to settle.
- Thresholds (0.8 / 0.2) are starting values; tune them in `roles.json` after real runs.
- Source code is sent to the coder model's provider. For private repos point `coder` at a local model.
- Static checks cannot see browser behaviour: a form with `required` and no `novalidate` lets native
  constraint validation swallow the submit, so a JS handler that shows feedback never runs while every
  structural check still passes. Demand `novalidate` in the check, or drive the page with a browser.
- Write `ui.criterion` about what fits in the first viewport. A criterion that mentions pricing tables
  and the footer fails a good page, because the screenshot is above the fold only.
- Judging the whole page height needs `captureBeyondViewport`, not the judge's own screenshot; verify
  full-page layout and mobile behaviour yourself before reporting PASS.
- The coder needs a vision model for mockup work: `hermes chat --image` is passed automatically when
  a `reference` or a `mockups/<task>` file exists. Check the model's input modalities first; with a
  text-only model the image is attached but silently ignored.
- With a mockup the visual gate keeps landing in `doubt` (0.2-0.4) even on a close page, because the
  model also compares text it cannot expect to match. Settle it yourself, side by side.
- `image_part` shrinks pictures over 700 KB with `sips`: a 1800px mockup is otherwise rejected with
  "Invalid image dimensions" and the gate reports NOT JUDGED.

## Verification

- `python3 -m unittest discover -s tests` in the hermes-contract-loop repo passes (offline, fake models).
- After a run: `git log` on the run branch shows one `cl(<task>)` commit per task and
  `close.md` reports checks PASS and launch PASS.
