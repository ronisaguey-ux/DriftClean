# Wiring `/clean` into each agent

`/clean` runs the sweep in `examples/clean_everything.py`. The files here are the
integration for each agent — the part that makes `/clean` a **hook or a local UI
command rather than a prompt**.

That distinction is not cosmetic. A slash command written as a markdown file is
expanded into a prompt and handed to the model, which means a drifted agent gets
a chance to refuse, restate, or half-do the job — exactly when you least want it
deciding anything. Everything here runs *before* or entirely *outside* the model,
so the sweep has already finished by the time the next turn exists.

| Agent | File | Extension point |
|---|---|---|
| [Claude Code](#claude-code) | `claude_clean_hook.py` | `UserPromptSubmit` hook |
| [Codex CLI](#codex-cli) | `codex_clean_hook.py` | `UserPromptSubmit` hook |
| [Antigravity](#antigravity) | `agy_slash_clean.py` + `agy_clean_skill.md` | `PreInvocation` hook |
| [Aider](#aider) | `aider_clean_launcher.py` | a `cmd_clean` method on its `Commands` class |
| [Hermes Agent](#hermes-agent) | `hermes_driftclean/` | a directory plugin |
| [opencode](#opencode) | `opencode_driftclean.tsx` | TUI plugin |

All of them read `DRIFTCLEAN_HOME` for the path to this repo, falling back to
`~/DriftClean`:

```bash
export DRIFTCLEAN_HOME=/path/to/DriftClean
```

## Diff mode

Every wiring has one, and it is the same question everywhere: *what would this
have written?* `--diff` runs the identical pipeline and throws the result away,
printing a unified diff instead. It implies a dry run — a diff is a question, not
an action, so nothing is written and no backup is taken. That is enforced inside
the sweep, not by each caller, so a new wiring cannot get it wrong by forgetting.

```bash
/clean --diff                  # one session
/clean --all --diff            # every live session on the machine
python3 examples/clean_everything.py --diff
```

Two agents cannot take a flag the same way, and both are handled rather than
worked around:

- **opencode** — its keymap dispatches `run()` with no arguments, so a flag
  cannot ride along with the command. Diff mode is its own command, `/cleandiff`
  (aliased `/driftdiff`), which shows the diff in a scrollable dialog.
- **Antigravity** — a `PreInvocation` hook has exactly one way to speak: the
  `injectSteps` it returns. So `/clean --diff` injects the head of the diff and
  writes the whole thing to `$XDG_RUNTIME_DIR/driftclean/last-diff.patch`, naming
  that file in the same message. Use `--scope` to keep a diff run small.

## Claude Code

A `UserPromptSubmit` hook. Exit code `2` tells Claude Code to swallow the prompt
it just received, so `/clean` never reaches the model and costs no turn.

Copy the hook somewhere on `PATH` and register it:

```bash
install -m 755 wiring/claude_clean_hook.py ~/.local/bin/claude_clean_hook.py
```

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "claude_clean_hook.py" }
        ]
      }
    ]
  }
}
```

`/clean --all` sweeps every live session; a bare `/clean` cleans the transcript of
the session you typed it in.

## Codex CLI

Same hook event name as Claude Code, different payload: Codex hands the hook the
whole context on stdin, including `transcript_path` — the rollout JSONL of the
session being used. So a bare `/clean` cleans the right session without guessing.

Codex keeps every turn in the rollout twice, as a canonical `response_item` and
as an `event_msg` display mirror. The adapter rewrites both from one source of
truth; see `src/sanitizer/adapters/codex.py`.

```bash
install -m 755 wiring/codex_clean_hook.py ~/.local/bin/codex_clean_hook.py
```

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "codex_clean_hook.py", "timeout": 600 }] }
    ]
  }
}
```

Save that as `~/.codex/hooks.json`. The project-level equivalent is
`<repo>/.codex/hooks.json`, and it only loads when that layer is trusted. Don't
put hooks in `config.toml` *and* `hooks.json` — Codex loads both and warns.

Two things about Codex specifically:

- **Blocking needs non-empty stderr.** Codex reads exit `2` as a *block* only
  when `stderr` carries something; exit 2 with an empty stderr is a failed hook,
  and a failed hook lets the prompt through to the model. The relay here always
  says something, even when the sweep says nothing — a `/clean` that reaches the
  model is the one thing this file exists to prevent.
- **Hooks must be trusted.** Codex silently skips untrusted hooks: run `/hooks`
  once after installing, or `/clean` will appear to do nothing. `matcher` is not
  supported on `UserPromptSubmit`, so the hook is called for every prompt and
  ignores the ones that are not `/clean`.

## Antigravity

A `PreInvocation` hook — it fires before the model is called, which is the only
place in Antigravity where an invocation can be intercepted. It runs the sweep
itself and injects one line telling the agent what already happened, so the agent
acknowledges rather than re-does.

```bash
install -m 755 wiring/agy_slash_clean.py ~/.gemini/config/scripts/driftclean_slash_clean.py
mkdir -p ~/.gemini/config/skills/clean
cp wiring/agy_clean_skill.md ~/.gemini/config/skills/clean/SKILL.md
```

```json
{
  "driftclean-slash-clean": {
    "PreInvocation": [
      { "type": "command", "command": "python3 ./scripts/driftclean_slash_clean.py", "timeout": 600 }
    ]
  }
}
```

Save that as `~/.gemini/config/hooks.json`. The hook's cwd is the directory the
hooks file lives in, which is why the command path is relative.

The hook only forwards flags it recognises, and validates the ones that take a
value — it parses text the user typed, and a hook must never hand arbitrary words
to a subprocess as arguments.

The skill is what makes `/clean` show up as a recognised command in the palette;
without it the hook still fires, but the completion won't offer it.

## Aider

Aider has no plugin API and its slash commands cannot be added from config, so
there is no hook to register. What it has is a `Commands` class that discovers
its own commands by reflecting over `dir(self)` for `cmd_` attributes. Attaching
a `cmd_clean` before the app starts *is* a real in-process `/clean`.

That is why this one is a launcher rather than a hook: `/clean` only exists in
aider processes started through it. The shebang has to point at the interpreter
aider itself runs under, which is what the first line of its own launcher
carries:

```bash
AIDER_PY="$(head -1 "$(command -v aider)" | sed 's|^#!||')"
install -m 755 wiring/aider_clean_launcher.py ~/.local/bin/aider-clean
sed -i "1s|.*|#!$AIDER_PY|" ~/.local/bin/aider-clean
```

```bash
aider-clean              # aider, with /clean
aider-clean --model ...  # every other aider flag passes straight through
```

Rewriting the history file is only half the job: aider holds the conversation in
memory and never reads it back, so the launcher re-derives the in-memory history
from the cleaned file with aider's own parser. If that parser has moved in the
version you have, it says so and tells you to restart instead of pretending the
context is fresh.

## Hermes Agent

Hermes exposes plugin slash commands through `ctx.register_command`, which
registers an **in-session** `/name` — distinct from `ctx.register_cli_command`,
which would add a shell-level `hermes <plugin> <subcommand>`. The handler is
`fn(raw_args: str) -> str | None`, and its return value is what the session
prints. That is the whole extension point this needs: no model in the loop, no
prompt built.

```bash
cp -r wiring/hermes_driftclean ~/.hermes/plugins/driftclean
hermes plugins enable driftclean
```

Plugins are discovered from `<hermes-repo>/plugins/` (bundled),
`~/.hermes/plugins/<name>/` (user), and `.hermes/plugins/<name>/` (project, opt-in
via `HERMES_ENABLE_PROJECT_PLUGINS=true`). Enable it, then `hermes plugins list`
to confirm it loaded.

Hermes keeps conversations in `state.db` and mirrors message text into an FTS5
index through `AFTER UPDATE` triggers. The adapter writes plain `UPDATE`s so the
triggers keep the index in step. The index is never touched directly; touching it
would corrupt it.

## opencode

A TUI plugin. Its handler runs **in the TUI process**, spawns the sweep, and
shows a toast — nothing is posted to the server, no prompt is built, and the
model is never consulted.

```bash
cp wiring/opencode_driftclean.tsx ~/.config/opencode/driftclean.tsx
```

Then list it in `~/.config/opencode/tui.json`:

```json
{ "plugin": ["./driftclean.tsx"] }
```

This is deliberately *not* a `~/.config/opencode/commands/clean.md` file.
Markdown commands are expanded into prompts — opencode's server-side
`command.execute.before` hook is no help either, because the source calls
`prompt(...)` unconditionally afterwards, so the model runs regardless of what the
hook decides. The TUI plugin is the only layer that can do this without the model.

`/cleandiff` opens its dialog with `api.ui.dialog.replace`, and the diff body is
a `scrollbox` — a whole diff is far taller than any terminal, and the core's own
`Esc`/`ctrl+c` binding is the entire affordance needed to close it.

## Verifying it works

```bash
# Run the sweep directly — this is what every wiring above shells out to
python3 "$DRIFTCLEAN_HOME/examples/clean_everything.py" --verbose

# Run it twice: the second pass must report "already clean"
python3 "$DRIFTCLEAN_HOME/examples/clean_everything.py"

# See what a pass would do, without doing it
python3 "$DRIFTCLEAN_HOME/examples/clean_everything.py" --diff
```

If the second run reports changes, something is rewriting a session on every pass
rather than converging — that is a bug, not a tuning problem. The signature gating
in the sweep is meant to make repeat runs free.

Because a `--diff` run writes nothing, it also earns no signature: re-running it
gives the same diff every time, which is what you want while you are reading it.
