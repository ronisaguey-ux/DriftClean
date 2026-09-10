/**
 * driftclean.tsx — opencode 1.18.29 TUI plugin: `/clean` runs DriftClean here.
 *
 * `/clean` is a LOCAL command. Its handler runs in the TUI process and shells
 * straight out to the sanitizer; nothing is posted to the server, no prompt is
 * built, no message is appended, and the model is never consulted. That is the
 * whole point: the agent cannot refuse a job it was never given, cannot
 * restate it, and cannot half-do it — the sweep has already finished by the
 * time the next turn is even a thought.
 *
 *   /clean        rewrite every live session (Claude, Antigravity, opencode,
 *                 Codex, Aider, Hermes, DriftClean's own reports)
 *   /cleandiff    the same sweep, read-only — show exactly what would change
 *
 * The heavy lifting lives in the repo (examples/clean_everything.py) so this
 * stays a thin, disposable front-end: a spawn, a toast, done.
 *
 * Registered on the keymap layer rather than as a commands/*.md file — a
 * markdown command is expanded into a prompt and sent to the model, which is
 * exactly what we are avoiding. A keymap command's handler is called with no
 * arguments (the core dispatches by name), which is why diff mode is its own
 * slash command rather than a `--diff` flag on this one.
 */

import type { TuiPlugin, TuiPluginApi, TuiPluginModule } from "@opencode-ai/plugin/tui"

const PLUGIN_ID = "driftclean.clean"
const WORKDIR =
  process.env.DRIFTCLEAN_HOME ?? `${process.env.HOME}/DriftClean`
const CLEANER = `${WORKDIR}/examples/clean_everything.py`

function log(message: string) {
  try {
    console.log(`[driftclean] ${message}`)
  } catch (_) {
    /* a broken log must never take the TUI down */
  }
}

/** One line, always: the last non-empty line the sanitizer printed. */
function lastLine(text: string): string {
  const lines = text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
  return lines.length ? lines[lines.length - 1] : ""
}

/** The sweep's summary comes first, then a blank line, then the diff. */
function firstLine(text: string): string {
  for (const line of text.split("\n")) {
    if (line.trim()) return line.trim()
  }
  return ""
}

type Sweep = { code: number; out: string; err: string }

async function sweep(extraArgs: string[] = []): Promise<Sweep> {
  try {
    const proc = Bun.spawn(["python3", CLEANER, ...extraArgs], {
      cwd: WORKDIR,
      stdout: "pipe",
      stderr: "pipe",
    })

    const [out, err] = await Promise.all([
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
    ])
    const code = await proc.exited

    return { code, out, err }
  } catch (error) {
    // A plugin that throws takes the TUI with it. Report instead.
    return { code: 1, out: "", err: String((error as Error)?.message || error) }
  }
}

function summarize(result: Sweep): string {
  const line = lastLine(result.out)
  if (line) return line
  if (result.code !== 0 && result.err.trim()) return `✗ DriftClean: ${lastLine(result.err).slice(0, 200)}`
  return "✓ DriftClean: nothing to clean"
}

/** `+`/`-`/`@@`/context — a unified diff coloured the way opencode colours one. */
function diffColor(api: TuiPluginApi, line: string) {
  const theme = api.theme.current
  if (line.startsWith("+++") || line.startsWith("---")) return theme.diffHunkHeader
  if (line.startsWith("@@")) return theme.diffHunkHeader
  if (line.startsWith("+")) return theme.diffAdded
  if (line.startsWith("-")) return theme.diffRemoved
  return theme.diffContext
}

/** A whole diff is far taller than any terminal: scrollbox + the core's own
 *  Esc/ctrl+c "Close dialog" binding is the entire affordance. */
async function showDiff(api: TuiPluginApi, header: string, body: string[]): Promise<void> {
  await new Promise<void>((resolve) => {
    api.ui.dialog.replace(
      () => (
        <box
          flexDirection="column"
          width="90%"
          maxHeight="80%"
          borderStyle="rounded"
          borderColor={api.theme.current.border}
          paddingLeft={1}
          paddingRight={1}
          title="DriftClean — dry run"
        >
          <box flexDirection="column" flexShrink={0} marginBottom={1}>
            <text fg={api.theme.current.text}>{header}</text>
            <text fg={api.theme.current.textMuted}>
              {body.length} diff lines · Esc closes · nothing was written
            </text>
          </box>
          <scrollbox flexGrow={1} scrollbarOptions={{ visible: true }}>
            {body.map((line, index) => (
              <text key={index} fg={diffColor(api, line)}>
                {line || " "}
              </text>
            ))}
          </scrollbox>
        </box>
      ),
      () => resolve(),
    )
  })
}

const tui: TuiPlugin = async (api: TuiPluginApi) => {
  api.keymap.registerLayer({
    commands: [
      {
        namespace: "palette",
        name: "driftclean.clean",
        title: "DriftClean — clean every session",
        desc: "Sanitize every live session (Claude, Antigravity, opencode, Codex, Aider, Hermes) without involving the model.",
        category: "DriftClean",
        slashName: "clean",
        slashAliases: ["driftclean"],
        async run() {
          api.ui.toast({
            variant: "info",
            title: "DriftClean",
            message: "sweeping every session…",
            duration: 4000,
          })

          const line = summarize(await sweep())

          api.ui.toast({
            variant: line.startsWith("✓") ? "success" : "error",
            title: "DriftClean",
            message: line,
            duration: 8000,
          })

          log(line)
          return line
        },
      },
      {
        namespace: "palette",
        name: "driftclean.cleandiff",
        title: "DriftClean — show what /clean would change",
        desc: "Dry run: a unified diff of every live session, with nothing written to disk.",
        category: "DriftClean",
        slashName: "cleandiff",
        slashAliases: ["driftdiff"],
        async run() {
          const result = await sweep(["--diff"])
          const text = result.out.trim()

          // The sweep is read-only under --diff: summary line first, then a
          // blank line, then the diff. No diff at all means nothing to show.
          const lines = text ? text.split("\n") : []
          const header = firstLine(text) || summarize(result)
          const start = lines.findIndex((line) => line.trim()) + 1
          const body = lines.slice(start)

          if (!body.some((line) => line.trim())) {
            api.ui.toast({
              variant: header.startsWith("✓") ? "success" : "error",
              title: "DriftClean",
              message: header,
              duration: 8000,
            })
            log(header)
            return header
          }

          log(header)
          await showDiff(api, header, body)
          return header
        },
      },
    ],
    bindings: [],
  })

  log(`registered /clean and /cleandiff (plugin ${api.app.version})`)
}

const plugin: TuiPluginModule & { id: string } = {
  id: PLUGIN_ID,
  tui,
}

export default plugin
