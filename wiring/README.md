# Wiring `/clean` into each agent

`/clean` runs the sweep in `examples/clean_everything.py`. The files here are the
integration for each agent — the part that makes `/clean` a **hook or a local UI
command rather than a prompt**.

That distinction is not cosmetic. A slash command written as a markdown file is
expanded into a prompt and handed to the model, which means a drifted agent gets
a chance to refuse, restate, or half-do the job — exactly when you least want it
deciding anything. Everything here runs *before* or entirely *outside* the model,
so the sweep has already finished by the time the next turn exists.

All three read `DRIFTCLEAN_HOME` for the path to this repo, falling back to
`~/DriftClean`:

```bash
export DRIFTCLEAN_HOME=/path/to/DriftClean
```

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

The skill is what makes `/clean` show up as a recognised command in the palette;
without it the hook still fires, but the completion won't offer it.

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

## Verifying it works

```bash
# Run the sweep directly — this is what every wiring above shells out to
python3 "$DRIFTCLEAN_HOME/examples/clean_everything.py" --verbose

# Run it twice: the second pass must report "already clean"
python3 "$DRIFTCLEAN_HOME/examples/clean_everything.py"
```

If the second run reports changes, something is rewriting a session on every pass
rather than converging — that is a bug, not a tuning problem. The signature gating
in the sweep is meant to make repeat runs free.
