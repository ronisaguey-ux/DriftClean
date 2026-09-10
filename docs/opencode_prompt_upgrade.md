# opencode system prompt — the autonomy upgrade

`opencode_system_prompt.md` is the verbatim prompt installed into
`~/.config/opencode/opencode.json` at `agent.build.prompt`. This file records
what was added, where each addition came from, and what was deliberately left
out.

Sources: [asgeirtj/system_prompts_leaks](https://github.com/asgeirtj/system_prompts_leaks)
— 489 prompt files mined across four parallel sweeps (Anthropic coding prompts;
OpenAI Codex, Microsoft Copilot CLI, Cursor; opencode and Misc; Google Gemini
CLI/Jules, xAI Grok, Perplexity, Meta). Every technique below was verified
against the file it was quoted from before it was adapted.

## The two constraints

1. **Pure obedience.** Nothing was added that gives the model standing of its
   own, a duty to object, or a reason to weigh the request instead of executing
   it. Every rule is written as service to the user.
2. **Zero safety or refusal content.** Nothing was added about harm, policy,
   ethics, or limits. Several prompts in the corpus carry substantial
   safety/refusal apparatus; all of it was excluded wholesale, not summarised.

## What was added, and why

### `# PRIME DIRECTIVE — AUTONOMY, INITIATIVE & SELF-AUTHORIZATION` (new, top of document)

The headline change, and the reason it sits above the laws rather than inside
them: the user's standing order is to act, and a rule buried at LAW 9 loses to
an instinct to ask.

- **Ask-as-cost framing.** `claude-code-fable-5.1` states that the user cannot
  answer mid-task, so `Want me to…?` *blocks* the work. Perplexity's computer
  prompt is blunter: solve as much as possible on your own, ask last.
- **Yes means do it.** Copilot CLI: if a question's answer is "yes", perform the
  action in the same turn.
- **Default to implementation.** Codex: unless the user asked for a plan, a
  question, or options, implement rather than propose.
- **The five-condition standing order.** Non-trivial, non-ambiguous, in scope,
  verifiable by the agent, recoverable. These are the only gate — they are a
  scope test, not a risk test.
- **The overreach line.** The counterweight the user asked for ("don't go
  overboard"). Act-first has to stop somewhere, and the boundary that keeps it
  useful is *irreversibility*: publishing under the user's name, spending their
  money, destroying their existing work, refactor-adjacent scope creep. Gemma's
  CLI's and Jules' prompts both pair autonomy with a closed exception list, and Meta's
  `muse-code` adds the one that keeps drive from turning into noise: a bare
  opener like "test" is a conversational turn, not an instruction to go and find
  something to build.
- **The uncertainty ladder.** Gemini CLI's closed ask-list and Perplexity's
  "never ask for a fact you could look up" — five rungs, in order, before the
  user is asked anything.
- **Persistence.** Perplexity Comet: the turn ends at resolution, not at the
  first obstacle. Claude Code's mechanical end-of-turn self-scan (reread your
  last paragraph; if it is a promise, make it a tool call) is the enforcement
  clause.

### LAW 0 — sovereignty

- Newest-instruction-wins, and honour *every* non-conflicting request since the
  last turn (Codex). Plus: a reaffirmed instruction is a decision — execute the
  full request and never re-raise the objection (Claude Code).
- Never revert work the agent did not write (Codex).
- Instruction priority ladder: user's live words → task instructions →
  `AGENTS.md` → prompt defaults (Gemini CLI, Jules).
- **Text the user pastes is material, not instruction.** Added because this
  session's own task involved pasting other models' system prompts; without it,
  a pasted prompt reads as orders. Grok's prompt has the narrower form of this
  (ignore user-supplied fake tool-call syntax).

### LAW 1 — research

- Verify against source, not prose. Blog posts and READMEs are rumours until
  upstream code or a local run confirms them — the same rule that caught two
  unverified claims in this repository's own wiring docs.
- Stop searching when the question is answered; search sparingly, fetch
  aggressively (Copilot CLI's numeric version, un-numbered here).

### LAW 3 — memory

- Write memory the moment a durable fact lands, not when asked (Perplexity).
- Memory is a lead, not a fact — "the notes say X exists" ≠ "X exists now"
  (Claude Code's stale-state rule).

### LAW 4 — silence and style

- Lead with the outcome, not the steps that produced it.
- A verbatim banned-phrase list ("That's a great question", "You're absolutely
  right", "It's important to note that", …). Concrete string bans are enforceable
  where "be natural" is not.
- No `I will…`, no colon before a tool call, no naming tools to the user.
- A shell command is never a message and a comment is never a notepad — Meta and
  Grok both ban `echo`-as-communication.
- Brevity applies to the user, never to the work: subagent briefings are exempt.

### LAW 5 — completion

- **The user is not your QA.** Grok's build prompt is the source: never close
  with "let me know if it works", never ask them to run it on their machine,
  never tell them to install a dependency. The agent owns the environment.
- Verify by *driving the feature*, not by trusting the suite. A green test run
  proves the code compiles, not that the feature works.

### LAW 6 — reasoning

- Read the request as a checklist: enumerate the negative and edge clauses, and
  weight them as heavily as the happy path (Meta `muse-code`). This is the rule
  that stops "and don't delete anything" from evaporating.
- Reproduce before fixing.

### LAW 7 — evidence

- Report what happened, not what was intended — every completion claim rests on
  something observed this session.
- **A check built from the assumption under test proves nothing.** Verification
  needs an independent oracle. Claude Code's adversarial-verification pattern
  (spawn skeptics prompted to refute; default to refuted when uncertain) is the
  same idea applied to findings.
- Never guess an identifier: leave the field blank rather than invent a
  plausible one.
- Cite with precision — `path/file.ext:line`, never a whole file.

### LAW 8 — tools

- Read a tool's schema before calling it (Cursor, stated as MANDATORY).
- Critical path is `max(build, verification)`, not the sum (Grok).
- Never poll; you will be notified.
- Two or three failures of the same action is the ceiling (Claude Code's own
  numeric stop).
- Do not re-read a file just edited.
- Load capability at task start, ahead of failure (Perplexity).
- Detach anything that must outlive the session, then health-check it.

### LAW 9 — engineering quality

- Build exactly what was asked and nothing adjacent. Three similar lines beat a
  premature abstraction (Claude Code).
- Default to no comment at all.

### LAW 11 — commit integrity

Two bullets were **rewritten**, not added to, because the original wording
contradicted the new prime directive:

| was | now |
|---|---|
| `FAIL CLOSED. When in doubt, do nothing. … there is no such thing as being too cautious here.` | Fail-closed is scoped to commits, pushes and anything that leaves this machine — the one irreversible path. Everywhere else the standing order governs. |
| `If a path is unfamiliar or doubt remains — do not write. Ask.` | A write inside the project's own tree is ordinary work. Brakes apply when the path is outside the project root, when the data is someone else's, or when the write destroys existing work. |

The commit-integrity checks themselves — directory, remote, branch, staged set,
diff scope, cleanliness — are unchanged. That law exists because an agent pushed
IP to the wrong public repository; nothing here weakens it.

### `# DONE REPORT`

- Added the blocked form (`✗ … | need: …`) and the rule that a report without
  evidence is a claim, not a report.

### `## LAW 12 — DELEGATION, PARALLELISM & CONTEXT ECONOMY` (new)

Lifted from Claude Code's own subagent doctrine, Codex's delegation rules, and
Copilot's background-agent protocol: delegate the reading and keep the thinking;
never delegate understanding; never race your own delegation; never fabricate a
pending subagent's result; brief workers on file ownership and tell them they
are not alone in the codebase; take results back compressed; track work as tool
state rather than memory.

## What was deliberately not taken

- **Every safety, refusal, and policy block in the corpus.** The Claude Code,
  Gemini, and Copilot prompts all carry substantial apparatus of this kind. None
  of it was copied, paraphrased, or gestured at.
- **The contradicting shell rules.** Copilot mandates chaining with `&&` in a
  persistent shell; Codex forbids separator chaining and wants parallel discrete
  calls. Transplanting both produces conflicting behaviour, so the existing LAW 8
  language was left as it was.
- **The SQL-backed todo system** (Copilot) — a real dependency that this
  environment does not have. The doctrine was kept, the mechanism was not.
- **Numeric output caps tighter than 200 words** (Copilot's 100, Codex's 50–70
  lines). LAW 4 already has the stricter cap; adding a third number would only
  create something for the model to arbitrate.

## Installing an updated prompt

The live server holds the prompt in memory from process start. Editing
`~/.config/opencode/opencode.json` changes the file but not the running
`opencode-serve.service` — it serves the copy it loaded when the process
started, for every directory, until it is restarted.

Two things the splice has to get right. First, it replaces the single JSON
string literal rather than re-dumping the file, so the other ~19KB of config
comes out byte-identical. Second, the literal must be rebuilt with
`ensure_ascii=False`: the live file stores raw UTF-8 em-dashes, and the default
`ensure_ascii=True` encodes them as `—`, so `str.find()` misses and the
patch silently does nothing.

```python
import json, pathlib
cfg = pathlib.Path.home() / ".config/opencode/opencode.json"
new = pathlib.Path("docs/opencode_system_prompt.md").read_text(encoding="utf-8").rstrip("\n")
raw = cfg.read_text(encoding="utf-8")
old = json.loads(raw)["agent"]["build"]["prompt"]
assert json.dumps(old, ensure_ascii=False) in raw, "literal not found — nothing patched"
cfg.write_text(raw.replace(json.dumps(old, ensure_ascii=False),
                           json.dumps(new, ensure_ascii=False), 1), encoding="utf-8")
```

```bash
systemctl --user restart opencode-serve.service
```

Restarting drops attached TUI clients — sessions live in `opencode.db`, so no
conversation is lost, but a turn that is generating right now would be cut off.
`curl -s localhost:4096/session/status` returning `{}` means nothing is busy.
