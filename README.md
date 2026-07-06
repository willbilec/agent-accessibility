# agentDesktopAccessibility

> **Status: WORK IN PROGRESS.** This add-on is functional but still being polished. Hotkeys, foreground routing, and the Hermes `app.asar` patcher all work, but expect rough edges: the session picker dialog is unstyled, the diagnostic dump is verbose, the speech filter regex set is not exhaustive, and the auto-read behavior in OpenCode may stutter on long messages. Do not rely on this for production screen-reader use without testing your specific workflow first. Please file issues for anything that gets in your way.

An [NVDA](https://www.nvaccess.org/) screen-reader add-on that improves accessibility of two desktop apps:

- **Hermes Agent** (Electron)
- **OpenCode Desktop** (Electron)

The add-on is **foreground-aware**: the same hotkey does the right thing in each app, with no double-bindings and no conflicts. It is a merge of the previous `hermesAccessibility` and `opencodeAccessibility` add-ons.

## Download

Grab the latest `.nvda-addon` from the [**Releases**](../../releases) page. The current build is **v2.1.0**.

## Install

1. Download `agentDesktopAccessibility-2.1.0.nvda-addon` from the latest release.
2. In NVDA: <kbd>NVDA</kbd>+<kbd>N</kbd> → **Tools** → **Manage Add-ons** → **Install**.
3. Select the downloaded `.nvda-addon` file.
4. Restart NVDA when prompted.

> **Upgrade note:** if you have the legacy `hermesAccessibility` or `opencodeAccessibility` add-ons installed, disable them first. Both can be uninstalled once you've confirmed the merged add-on works.

## How foreground routing works

Every shared gesture in the tables below checks which app is currently focused:

- **Hermes focused** — calls the Hermes backend (`state.db`, `hermes://session/<id>` deep links, status suppression, `@` picker).
- **OpenCode focused** — calls the OpenCode backend (`opencode.db`, `opencode://open-project?directory=...` deep links, auto-read, thinking trace).
- **Neither focused** — the gesture passes through (`gesture.send()`), so it can be handled by other apps or NVDA itself.

App-specific gestures (e.g. <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>T</kbd> for the OpenCode thinking trace) only fire when that app is the foreground. They pass through everywhere else.

## Hotkeys (shared — Hermes or OpenCode)

| Gesture | Action |
| --- | --- |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Down</kbd> | Next message |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Up</kbd> | Previous message |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Home</kbd> | First message (force refresh) |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>End</kbd> | Last message (force refresh) |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>R</kbd> | Re-read current message |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>S</kbd> | Open session switcher dialog |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>N</kbd> | Next session (cycle) |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>P</kbd> | Previous session (cycle) |
| <kbd>Ctrl</kbd>+<kbd>N</kbd> | New session. In **OpenCode** this fires the add-on's 5-method fallback chain (button → API → bridge → clipboard → keystroke). In **Hermes** it passes through to the OS — Hermes handles <kbd>Ctrl</kbd>+<kbd>N</kbd> natively. In any other app it passes through normally. |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>D</kbd> | Diagnostic dump |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>D</kbd> | Foreground window metadata (always on) |

## Hotkeys (OpenCode only)

These pass through when Hermes is the foreground app.

| Gesture | Action |
| --- | --- |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>T</kbd> | Read thinking trace for current assistant message |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>A</kbd> | Toggle auto-read of new assistant messages |

## Hotkeys (Hermes only)

These pass through when OpenCode is the foreground app. Preserved from `hermesAccessibility` 1.7.2.

| Gesture | Action |
| --- | --- |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Space</kbd> | Open `@` reference picker dialog |
| <kbd>NVDA</kbd>+<kbd>Shift</kbd>+<kbd>H</kbd> | Toggle Hermes speech filter (silence status spam) |
| <kbd>NVDA</kbd>+<kbd>Shift</kbd>+<kbd>J</kbd> | Hermes speech filter status + suppression count |

## Features in detail

### Hermes `@` reference picker

Press <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Space</kbd> when focused in Hermes to open a two-pane dialog. The **left pane** lists reference types — *Folder* is first (most common). The **right pane** shows recent entries formatted as `name — full path` so two folders with the same basename are easy to tell apart.

- **`@folder:`** — browse for a folder, or pick from recent. Inserts `@folder:full/path/to/folder`.
- **`@file:`** — browse for a file, or pick from recent. Inserts `@file:full/path/to/file`.
- **`@url:`** — type or pick a URL. Inserts `@url:https://...`.
- **`@diff`** — inserts immediately (Git working-tree diff).
- **`@staged`** — inserts immediately (Git staged diff).
- **`@git:5` / `@git:10` / `@git:20`** — prompts for commit count, then inserts.

Paths with whitespace are automatically wrapped in backticks on the wire, mirroring the desktop's `formatRefValue` cascade, so a folder like `C:/Users/willb/programs/Hermes accessibility` arrives intact and the agent's filesystem lookup succeeds.

### Hermes speech filter

Hermes repeatedly announces *thinking* / *running* / spinner characters / timers (`1:13`, `5m 30s`) while the agent is working. The add-on hooks the synth driver's `speak()` method (the only Python-level interception point that catches Electron IA2 live-region announcements) and drops any utterance that matches a known status pattern.

- Toggle with <kbd>NVDA</kbd>+<kbd>Shift</kbd>+<kbd>H</kbd>.
- Check the current state and suppression count with <kbd>NVDA</kbd>+<kbd>Shift</kbd>+<kbd>J</kbd>.

### Session switching

- **Hermes** — uses the `hermes://session/<id>` deep-link protocol, auto-patched into `app.asar` the first time you use it, and re-applied automatically (with audible failure announcements) if Hermes updates and overwrites the patch.
- **OpenCode** — uses the `opencode://open-project?directory=...` deep link.

Both protocols route through the running app's existing IPC — no second process is spawned, no keystroke simulation is needed.

### Self-healing Hermes `app.asar` patcher (2.1.0)

The Hermes desktop app's built-in deep-link handler only routes `kind=blueprint` links to the renderer. The add-on's `patch_app_asar.js` injects a 3-line branch that also routes `kind=session` to the renderer's existing `hermes:focus-session` listener.

- **Self-healing** — re-checks the patch on every session-pick (60s TTL cache), and re-applies if a Hermes update overwrote it.
- **Audible failure** — if the patch cannot be applied, NVDA announces *"Hermes session patch failed: \<reason\>. Session picker will not work."* — you'll never be left wondering why picking does nothing.
- **Pattern-based matching** — the patcher locates the target line by *function structure*, not exact text, so cosmetic upstream reformatting doesn't break it.
- **Diagnostic** — pressing <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>D</kbd> while Hermes is foreground reports "Patcher: asar OK/MISSING, marker found/not-found".
- **Manual audit** — run `node patch_app_asar.js --audit` for a JSON status report with no side effects.

The proper long-term fix is for Hermes' `handleDeepLink` to route `kind=session` natively — a one-line change. Until that lands upstream, the patcher is the binding solution.

## Compatibility

- **NVDA** 2024.1 or later (tested on 2026.1)
- **Hermes Agent** desktop app (Electron) — speech filter, message nav, session switching, `@` picker
- **OpenCode Desktop** — message nav, session switching, auto-read, thinking trace

## Repository layout

```
addon/                  # NVDA add-on source (manifest.ini + Python modules)
  manifest.ini
  appModules/Hermes.py
  globalPlugins/agentDesktopAccessibility.py
  globalPlugins/addtl/  # backends, router, completion, speech filter
buildVars.py            # build metadata (name + version)
build_addon.py          # builds the .nvda-addon zip from addon/
patch_app_asar.js       # Hermes app.asar patcher (bundled in the .nvda-addon)
readme.html             # in-NVDA documentation (referenced by manifest.ini)
COPYING                 # GPL v2+
```

`build_addon.py` reads `buildVars.py`, walks `addon/`, drops `__pycache__` and `.pyc`, and produces `agentDesktopAccessibility-<version>.nvda-addon` in the repo root. Built artifacts are `.gitignore`d; releases are attached via GitHub Releases.

## License

Free software. Modify and redistribute under the terms of the GNU GPL v2 or later. See [`COPYING`](COPYING).
