# agentDesktopAccessibility

> **Status: WORK IN PROGRESS.** This add-on is functional but still being polished. Hotkeys, foreground routing, and the Hermes `app.asar` patcher all work, but expect rough edges: the session picker dialog is unstyled, the diagnostic dump is verbose, the speech filter regex set is not exhaustive, and the auto-read behavior in OpenCode may stutter on long messages. Do not rely on this for production screen-reader use without testing your specific workflow first. Please file issues for anything that gets in your way.

An [NVDA](https://www.nvaccess.org/) screen-reader add-on that improves accessibility of three desktop apps:

- **Hermes Agent** (Electron)
- **OpenCode Desktop** (Electron)
- **ChatGPT Desktop — consumer Chat and the Codex workspace**

The add-on routes shared commands to the focused agent app. Codex transcript reading and active-task cycling remain available from other applications through NVDA's local buffer. It merges the previous `hermesAccessibility`, `opencodeAccessibility`, and `codexAccessibility` add-ons.

## Download

Grab the latest `.nvda-addon` from the [**Releases**](../../releases) page. The current build is **v2.8.8**.

## Install

Two equivalent ways to install the add-on on NVDA 2024.1 or later:

**Option A — open the file directly**

Double-click `agentDesktopAccessibility-2.8.8.nvda-addon` in your file manager (or open it from your browser's downloads). NVDA will detect the add-on and offer to install it.

**Option B — from the Add-on Store**

1. In NVDA: <kbd>NVDA</kbd>+<kbd>N</kbd> → **Tools** → **Add-on Store**.
2. Open the add-on store menu and choose **Install from External Source**.
3. Select the downloaded `.nvda-addon` file.
4. Restart NVDA when prompted.

> **Upgrade note:** if you have the legacy `hermesAccessibility`, `opencodeAccessibility`, or `codexAccessibility` add-ons installed, disable them first. They can be uninstalled once you've confirmed the merged add-on works.

## How foreground routing works

Every shared gesture in the tables below checks which app is currently focused:

- **Hermes focused** — calls the Hermes backend (`state.db`, `hermes://session/<id>` deep links, status suppression, `@` picker).
- **OpenCode focused** — calls the OpenCode backend (`opencode.db`, `opencode://open-project?directory=<dir>` deep links, auto-read, thinking trace).
- **ChatGPT focused** — calls the Codex backend (`%USERPROFILE%\.codex`, `codex://` links, active-task and project/task pickers, transcript navigation, and usage limits).
- **Neither focused** — message-reading commands use the selected Codex transcript buffer. Left/right changes that NVDA buffer without changing or focusing ChatGPT. Unrelated foreground-specific gestures still pass through.

App-specific gestures are routed by foreground. For example, <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>T</kbd> opens active tasks in ChatGPT Codex and reads the thinking trace in OpenCode.

## Hotkeys (shared — Hermes, OpenCode, or ChatGPT Codex)

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

In ChatGPT, <kbd>Ctrl</kbd>+<kbd>N</kbd> passes through to the app's native new-task command. The shared session picker and session-cycle commands operate on Codex projects and tasks.

## Hotkeys (ChatGPT only)

These preserve the original Codex Accessibility shortcuts. Transcript reading and left/right task cycling work from any app. The project and active-task pickers are also available whether ChatGPT is open or closed. If it is open, choosing an item restores it before navigation. If it is closed, the add-on starts ChatGPT, waits for its verified desktop window and renderer, then applies the selected project/task link. Other foreground-specific commands pass through outside ChatGPT, and usage-limit reporting remains available from any app.

| Gesture | Action |
| --- | --- |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Enter</kbd> | In consumer **Chat**, open an accessible multiline prompt, send it through the signed-in ChatGPT app, wait for completion, and read the response. This deliberately refuses to run from Work or Codex. |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Right</kbd> | Next running or seven-day-recent Codex task. When ChatGPT is focused, synchronize from the task actually shown and open the next task; otherwise change only NVDA's message buffer. |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Left</kbd> | Previous running or seven-day-recent Codex task. When ChatGPT is focused, synchronize from the task actually shown and open the previous task; otherwise change only NVDA's message buffer. |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>C</kbd> | Open an accessible mirror of the ChatGPT application menus |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>P</kbd> | Open the Codex project and task picker (same result as the shared session-picker command) |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>T</kbd> | Open the active Codex task dialog |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>U</kbd> | Report current 5-hour and weekly Codex usage limits plus banked usage resets; press twice to offer to use one (available globally) |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Space</kbd> | Re-read the current Codex transcript message |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>A</kbd> | Toggle Auto-Read of collapsed activity summaries, commentary, and final responses from every active Codex task |

### Driving consumer Chat

Open **Quick chat** in the ChatGPT desktop app, then press <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Enter</kbd>. Type a message in NVDA's dialog and choose **OK**. The add-on finds the consumer composer by its accessible name, types without changing the clipboard, activates the accessible **Send** button, watches the rendered conversation while ChatGPT is responding, announces each new assistant activity card (for example, “Implementing deduplicated chat activity announcements”), and speaks the stable completed answer.

This uses the ordinary signed-in ChatGPT Chat surface and synced chat history. It does not call Codex, read `.codex` task logs, require an API key, or patch the ChatGPT application. If a draft already exists in the Chat composer, the add-on refuses to overwrite it.

### ChatGPT Codex usage and banked resets

Press <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>U</kbd> once to hear the current 5-hour and weekly limits and the number of banked usage resets available. Press it twice quickly to check the balance and open a confirmation dialog. A reset is never used merely by pressing the command twice: the dialog defaults to **No**, and the add-on redeems a reset only after you choose **Yes**. After redemption, the add-on refreshes and reports the limits again.

Codex always announces “Codex task finished” when a task reaches its final response, even when Auto-Read is off or ChatGPT is not focused. While Auto-Read is on, activity, commentary, and responses from every active top-level task are read globally; there is no foreground or remembered-window gate. Each update is queued as a separate NVDA-priority utterance so one task cannot hide another later in a combined message. It identifies every announcement by task. While Codex is working, Auto-Read speaks the text represented by its collapsed activity button as “Codex activity” without expanding or clicking the item. Archived tasks and internal subagent transcripts are excluded.

## Hotkeys (OpenCode, plus routed Codex Auto-Read)

These pass through when Hermes is the foreground app. <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>T</kbd> is foreground-routed and opens active tasks when ChatGPT is focused.

| Gesture | Action |
| --- | --- |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>T</kbd> | Read thinking trace for current assistant message |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>A</kbd> | Toggle auto-read of new assistant messages in OpenCode or ChatGPT Codex |

## Hotkeys (Hermes only)

These fire only when Hermes is the foreground app. In OpenCode (or any other app) they pass through to the OS or to NVDA's default handling. Preserved from `hermesAccessibility` 1.7.2.

| Gesture | Action |
| --- | --- |
| <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>Space</kbd> | Open the Hermes `@` reference picker |
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
- **OpenCode** — uses the native `opencode://open-project?directory=<dir>` deep-link protocol that OpenCode supports out of the box. The session picker lands on the picked **project**; within a multi-session project it lands on whatever the project auto-opens to (most-recent session), since the add-on no longer patches OpenCode's `app.asar` to add a per-session route. (A `patch_opencode_asar.js` script that used to do this was removed in 2.4.0 because it destabilized the OpenCode renderer.)
- **ChatGPT Codex** — reads projects and tasks from `%USERPROFILE%\.codex` and opens selections with the existing `codex://new` and `codex://threads/<id>` routes.

### ChatGPT Codex active tasks

Press <kbd>NVDA</kbd>+<kbd>Alt</kbd>+<kbd>T</kbd> while ChatGPT is focused to open a one-pane list of current Codex tasks, newest first. “Active” here means top-level tasks that have not been archived; internal subagent threads are excluded. Each entry includes its project name. Press <kbd>Enter</kbd> or choose **Open Task** to switch to it, or use **Refresh** while the dialog is open.

All three protocols route through the running app's existing IPC — no second process is spawned, no keystroke simulation is needed.

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
- **ChatGPT Desktop** (`ChatGPT.exe`, visible window title `ChatGPT`) — consumer Chat sending/response reading plus Codex menus, active tasks, projects/tasks, transcript nav, and usage limits. Consumer Chat support targets the current English accessible labels `Message ChatGPT`, `Send`, and `ChatGPT is responding`; other display languages may need additional label mappings. The Microsoft Store package and Codex integration surfaces still use the `OpenAI.Codex`, `.codex`, `codex`, and `codex://` names.
- **Codex CLI 0.141.0 or later, signed in with the same ChatGPT account**, for banked-reset counts and redemption. Version 0.144.0 or later is recommended so the add-on can select the earliest-expiring reset and describe its title and expiration.

## Repository layout

```
addon/                  # NVDA add-on source (manifest.ini + Python modules)
  manifest.ini
  appModules/Hermes.py
  globalPlugins/agentDesktopAccessibility.py
  globalPlugins/addtl/  # Hermes, OpenCode, and ChatGPT Codex backends plus router/helpers
buildVars.py            # build metadata (name + version)
build_addon.py          # builds the .nvda-addon zip from addon/
patch_app_asar.js       # Hermes app.asar patcher (bundled in the .nvda-addon)
readme.html             # in-NVDA documentation (referenced by manifest.ini)
COPYING                 # GPL v2+
```

`build_addon.py` reads `buildVars.py`, walks `addon/`, drops `__pycache__` and `.pyc`, and produces `agentDesktopAccessibility-<version>.nvda-addon` in the repo root. Built artifacts are `.gitignore`d; releases are attached via GitHub Releases.

## License

Free software. Modify and redistribute under the terms of the GNU GPL v2 or later. See [`COPYING`](COPYING).
