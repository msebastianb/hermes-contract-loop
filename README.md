# hermes-contract-loop

A Hermes skill for building software with a strong model's judgment at a cheap model's price.

The strong model writes a **contract**: the stack decision, small tasks, the files each task
may touch, and the acceptance checks that prove it done. A script then runs the loop without it:

| Role | Default | Job |
|---|---|---|
| Architect | your session model | Contract, escalations, final verdict |
| Scout and coder | `deepseek/deepseek-v4.1-flash` | One-page repo brief; implements each task |
| Judge | `typesafe/jev-1.14` | Typed yes/no gates: scope, repeated failure, screenshot, risk per file |
| Mockups | `qwen/qwen-image-2.1-turbo` | Optional visual target for UI tasks |
| Checks | your commands | The only source of truth for "done" |

The coder cannot edit the checks or stray outside its files (changes are reverted), repeated
failures stop early, and the final report shows the architect only the diffs judged risky.
The product must actually start before a run can close.

## Install

```sh
./install.sh            # copies the skill into ~/.hermes/skills/software-development/
```

## Use

In Hermes: "use hermes-contract-loop to build X". The procedure is in
[`skills/hermes-contract-loop/SKILL.md`](skills/hermes-contract-loop/SKILL.md).

The judge and mockups call OpenRouter directly and need the key: export `OPENROUTER_API_KEY`,
or opt in to a key file in `~/.config/contract-loop/roles.json`:

```json
{ "key_file": "~/.config/openrouter/key" }
```

Without a key the loop still runs; gates are skipped and flagged for the architect.

## Requirements

`hermes` on PATH (the coder runs as `hermes chat --oneshot`) and Python 3.9+ — nothing else,
the script is standard library only.

## Test

```sh
python3 -m unittest discover -s tests
```

Offline: a fake agent and a fake API stand in for the models.

## License

MIT
