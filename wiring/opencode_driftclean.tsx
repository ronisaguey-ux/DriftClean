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
 * The heavy lifting lives in the repo (examples/clean_everything.py) so this
 * stays a thin, disposable front-end: a spawn, a toast, done.
 *
 * Registered on the keymap layer rather than as a commands/*.md file — a
 * markdown command is expanded into a prompt and sent to the model, which is
 * exactly what we are avoiding.
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

async function sweep(): Promise<string> {
  try {
    const proc = Bun.spawn(["python3", CLEANER], {
      cwd: WORKDIR,
      stdout: "pipe",
      stderr: "pipe",
    })

    const [out, err] = await Promise.all([
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
    ])
    const code = await proc.exited

    const line = lastLine(out)
    if (line) return line
    if (code !== 0 && err.trim()) return `✗ DriftClean: ${lastLine(err).slice(0, 200)}`
    return "✓ DriftClean: nothing to clean"
  } catch (error) {
    // A plugin that throws takes the TUI with it. Report instead.
    return `✗ DriftClean: ${String((error as Error)?.message || error).slice(0, 200)}`
  }
}

const tui: TuiPlugin = async (api: TuiPluginApi) => {
  api.keymap.registerLayer({
    commands: [
      {
        namespace: "palette",
        name: "driftclean.clean",
        title: "DriftClean — clean every session",
        desc: "Sanitize every live session (Claude, Antigravity, opencode) without involving the model.",
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

          const line = await sweep()

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
    ],
    bindings: [],
  })

  log(`registered /clean (plugin ${api.app.version})`)
}

const plugin: TuiPluginModule & { id: string } = {
  id: PLUGIN_ID,
  tui,
}

export default plugin
