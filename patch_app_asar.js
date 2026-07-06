#!/usr/bin/env node
/**
 * patch_app_asar.js — Self-healing patch for the Hermes desktop app's app.asar.
 *
 * The bundled Hermes app's `handleDeepLink()` only forwards `kind=blueprint`
 * deep links to the renderer. Screen-reader accessibility (NVDA) needs
 * `hermes://session/<id>` deep links to also be routed to the renderer's
 * `hermes:focus-session` IPC handler so the user can switch sessions without
 * a working mouse.
 *
 * This patcher:
 *   1. Locates `electron/main.cjs` inside the Hermes app.asar
 *   2. Pattern-matches the `handleDeepLink` function body and inserts a
 *      `kind === 'session' && name` branch that routes to focus-session
 *   3. Repacks app.asar
 *
 * Pattern matching, NOT exact line matching — the upstream file has been
 * re-formatted twice in the last 6 months and we cannot afford to track
 * whitespace changes.
 *
 * IDEMPOTENT — checks for the patch marker before doing anything. Safe to
 * run as many times as you like.
 *
 * Exit codes:
 *   0  patched (or already patched)
 *   1  --check only and not patched
 *   2  prerequisites missing (asar/node/app.asar)
 *   3  pattern not found in main.cjs (Hermes changed structure)
 *   4  repack verification failed
 *
 * Usage:
 *   node patch_app_asar.js            # patch if needed
 *   node patch_app_asar.js --check    # exit 0 if patched, 1 if not
 *   node patch_app_asar.js --force    # re-patch even if marker is present
 *   node patch_app_asar.js --audit    # print JSON status, no side effects
 */
'use strict'

const fs = require('fs')
const path = require('path')
const os = require('os')
const { execFileSync } = require('child_process')

// Locate @electron/asar from the Hermes installation
const HERMES_ROOT = path.join(os.homedir(), 'AppData', 'Local', 'hermes', 'hermes-agent')
let asar
try {
  asar = require(path.join(HERMES_ROOT, 'node_modules', '@electron', 'asar'))
} catch {
  try {
    asar = require('@electron/asar')
  } catch {
    console.error('ERROR: Cannot find @electron/asar. Ensure Hermes is installed.')
    process.exit(2)
  }
}

const APP_ASAR = path.join(
  HERMES_ROOT, 'apps', 'desktop', 'release', 'win-unpacked', 'resources', 'app.asar'
)
const EXTRACT_DIR = path.join(os.tmpdir(), 'hermes-asar-patch')

// ═══════════════════════════════════════════════════════════════════
// Patch marker — a unique string that ONLY exists in patched versions.
// Keep this in sync with the Python side (_ensureAppAsarPatched).
// ═══════════════════════════════════════════════════════════════════
const PATCH_MARKER = "kind === 'session' && name"

// The replacement block we inject into handleDeepLink's try block.
// It must compile and behave exactly as the original line, but route
// `kind=session` to `hermes:focus-session` instead of `hermes:deep-link`.
const PATCH_BLOCK = [
  "    // NVDA accessibility patch: route hermes://session/<id> deep links",
  "    // through the existing focus-session IPC channel. The renderer's",
  "    // onFocusSession listener already calls sessionRoute(sessionId) when",
  "    // this fires. See patch_app_asar.js for context.",
  "    if (kind === 'session' && name) {",
  "      mainWindow.webContents.send('hermes:focus-session', name)",
  "      rememberLog(`[deeplink] delivered session/${name}`)",
  "    } else {",
  "      mainWindow.webContents.send('hermes:deep-link', payload)",
  "    }",
].join('\n')

// ═══════════════════════════════════════════════════════════════════
// Pattern-based target locator.
// We match the structural shape of `handleDeepLink`'s delivery line, not
// its exact text. Two patterns are tried: the modern (post-2026) and
// legacy one. Both are unambiguous within the function body.
// ═══════════════════════════════════════════════════════════════════
//
// Capture group 1 is the EXACT whitespace prefix on the target line
// (so the replacement keeps the original indentation).
const TARGET_PATTERNS = [
  // Modern: mainWindow.webContents.send('hermes:deep-link', payload)
  /^( +)mainWindow\.webContents\.send\(['"]hermes:deep-link['"],\s*payload\s*\)\s*$/m,
  // Legacy: webContents.send('hermes:deep-link', payload)  (no mainWindow. prefix)
  /^( +)webContents\.send\(['"]hermes:deep-link['"],\s*payload\s*\)\s*$/m,
  // Modern with template literal
  /^( +)mainWindow\.webContents\.send\(['"]hermes:deep-link['"],\s*payload\s*\)\s*;?\s*$/m,
]

/**
 * Returns { matched: bool, indent: string|null, line: string|null, patternIndex: number }
 */
function findTargetLine(content) {
  for (let i = 0; i < TARGET_PATTERNS.length; i++) {
    const m = content.match(TARGET_PATTERNS[i])
    if (m) {
      return { matched: true, indent: m[1], line: m[0], patternIndex: i }
    }
  }
  return { matched: false, indent: null, line: null, patternIndex: -1 }
}

/**
 * Find the `function handleDeepLink` body bounds in main.cjs.
 * Returns { startLine, endLine, body } or null.
 *
 * The function in question has a known shape:
 *   function handleDeepLink(url) {
 *     ...if (!_rendererReadyForDeepLink || !mainWindow || ...) { _pendingDeepLink = payload; return }
 *     try { ... mainWindow.webContents.send('hermes:deep-link', payload) ... } catch (err) { ... }
 *   }
 *
 * We locate the function header, then walk braces to find its end.
 */
function findHandleDeepLinkBlock(content) {
  const lines = content.split('\n')
  const headerRe = /^(async\s+)?function\s+handleDeepLink\s*\(/
  let headerLine = -1
  for (let i = 0; i < lines.length; i++) {
    if (headerRe.test(lines[i])) {
      headerLine = i
      break
    }
  }
  if (headerLine < 0) return null

  // Walk braces from headerLine forward to find the function's closing `}`.
  // We use the *first* closing brace at the same indent level as `function`.
  // Simpler: count `{` and `}` from headerLine; stop when we hit 0 after opening.
  let depth = 0
  let opened = false
  let endLine = lines.length - 1
  for (let i = headerLine; i < lines.length; i++) {
    const line = lines[i]
    for (const ch of line) {
      if (ch === '{') { depth++; opened = true }
      else if (ch === '}') {
        depth--
        if (opened && depth === 0) { endLine = i; break }
      }
    }
    if (opened && depth === 0) break
  }
  if (endLine >= lines.length) return null
  return { startLine: headerLine, endLine, body: lines.slice(headerLine, endLine + 1).join('\n') }
}

// ═══════════════════════════════════════════════════════════════════
// Main entry points
// ═══════════════════════════════════════════════════════════════════
function audit() {
  const result = {
    asar: APP_ASAR,
    asarExists: fs.existsSync(APP_ASAR),
    asarMtime: null,
    asarSize: null,
    mainCjsFound: false,
    patched: false,
    patchMarker: PATCH_MARKER,
    handleDeepLinkFound: false,
    targetLineMatch: null,
    patternIndex: -1,
    nodeVersion: process.version,
    asarVersion: null,
  }
  if (result.asarExists) {
    const st = fs.statSync(APP_ASAR)
    result.asarMtime = st.mtime.toISOString()
    result.asarSize = st.size
    try {
      const pkg = asar.extractFile(APP_ASAR, 'electron/main.cjs').toString('utf8')
      result.mainCjsFound = true
      result.patched = pkg.includes(PATCH_MARKER)
      const block = findHandleDeepLinkBlock(pkg)
      if (block) {
        result.handleDeepLinkFound = true
        const target = findTargetLine(block.body)
        if (target.matched) {
          result.targetLineMatch = target.line.trim()
          result.patternIndex = target.patternIndex
        }
      }
    } catch (e) {
      result.error = e.message
    }
  }
  try {
    result.asarVersion = require(path.join(HERMES_ROOT, 'node_modules', '@electron', 'asar', 'package.json')).version
  } catch {}
  return result
}

function patch({ force = false } = {}) {
  if (!fs.existsSync(APP_ASAR)) {
    console.error('ERROR: app.asar not found at ' + APP_ASAR)
    process.exit(2)
  }

  // Extract
  try { fs.rmSync(EXTRACT_DIR, { recursive: true, force: true }) } catch {}
  asar.extractAll(APP_ASAR, EXTRACT_DIR)

  const mainCjsPath = path.join(EXTRACT_DIR, 'electron', 'main.cjs')
  if (!fs.existsSync(mainCjsPath)) {
    console.error('ERROR: electron/main.cjs not found in extracted asar')
    process.exit(2)
  }

  const original = fs.readFileSync(mainCjsPath, 'utf8')

  // Check if already patched (unless --force)
  if (!force && original.includes(PATCH_MARKER)) {
    console.log('PATCHED')
    cleanup()
    return
  }

  // Locate the function block
  const block = findHandleDeepLinkBlock(original)
  if (!block) {
    console.error('ERROR: function handleDeepLink not found in main.cjs. App structure may have changed.')
    process.exit(3)
  }

  // Locate the target line within the function block
  const target = findTargetLine(block.body)
  if (!target.matched) {
    console.error('ERROR: target line (hermes:deep-link send) not found in handleDeepLink. Hermes refactored again.')
    console.error('  Looked in lines ' + block.startLine + '..' + block.endLine + ' of main.cjs')
    process.exit(3)
  }

  // Build the replacement: keep original indent on each replacement line.
  const indent = target.indent
  const replacement = PATCH_BLOCK.split('\n').map(l => l ? indent + l.replace(/^ {4}/, '') : l).join('\n')
  // (The PATCH_BLOCK already has 4-space prefix to keep the source readable;
  //  we strip 4 spaces and re-add the original indent.)

  // Surgical replacement: only inside the handleDeepLink function body
  // so we don't accidentally hit a similar-looking line elsewhere.
  const bodyOriginal = block.body
  const bodyReplaced = bodyOriginal.replace(target.line, replacement)
  if (bodyReplaced === bodyOriginal) {
    console.error('ERROR: replacement had no effect — line may be subtly different')
    process.exit(3)
  }
  const patched = original.slice(0, block.startLine === 0 ? 0 : 0) // we'll just replace the substring
  // Compute the byte offset of the body in the original file.
  const bodyOffset = original.indexOf(bodyOriginal)
  if (bodyOffset < 0) {
    console.error('ERROR: could not locate body in original file (split mismatch?)')
    process.exit(3)
  }
  const newContent = original.slice(0, bodyOffset) + bodyReplaced + original.slice(bodyOffset + bodyOriginal.length)

  // Verify the patched file contains the marker
  if (!newContent.includes(PATCH_MARKER)) {
    console.error('ERROR: patched file does not contain marker after replacement')
    process.exit(4)
  }

  fs.writeFileSync(mainCjsPath, newContent, 'utf8')

  // Backup original (only if backup doesn't already exist)
  const bakPath = APP_ASAR + '.bak'
  if (!fs.existsSync(bakPath)) {
    fs.copyFileSync(APP_ASAR, bakPath)
  }

  // Repack
  const tempAsar = APP_ASAR + '.tmp'
  try { fs.unlinkSync(tempAsar) } catch {}
  return asar.createPackage(EXTRACT_DIR, tempAsar).then(() => {
    // Verify the temp asar
    const verifyContent = asar.extractFile(tempAsar, 'electron/main.cjs').toString('utf8')
    if (!verifyContent.includes(PATCH_MARKER)) {
      console.error('ERROR: repacked asar verification failed')
      try { fs.unlinkSync(tempAsar) } catch {}
      process.exit(5)
    }
    fs.copyFileSync(tempAsar, APP_ASAR)
    try { fs.unlinkSync(tempAsar) } catch {}
    cleanup()
    console.log('PATCHED_SUCCESS')
  }).catch(err => {
    console.error('ERROR: repack failed: ' + err.message)
    try { fs.unlinkSync(tempAsar) } catch {}
    process.exit(6)
  })
}

function check() {
  if (!fs.existsSync(APP_ASAR)) {
    console.error('NOT_PATCHED: app.asar not found at ' + APP_ASAR)
    process.exit(2)
  }
  try {
    const content = asar.extractFile(APP_ASAR, 'electron/main.cjs').toString('utf8')
    if (content.includes(PATCH_MARKER)) {
      console.log('PATCHED')
      process.exit(0)
    } else {
      console.log('NOT_PATCHED')
      process.exit(1)
    }
  } catch (e) {
    console.error('NOT_PATCHED: ' + e.message)
    process.exit(2)
  }
}

function cleanup() {
  try { fs.rmSync(EXTRACT_DIR, { recursive: true, force: true }) } catch {}
}

function main() {
  const args = process.argv.slice(2)
  if (args.includes('--audit')) {
    console.log(JSON.stringify(audit(), null, 2))
    return
  }
  if (args.includes('--check')) {
    return check()
  }
  const force = args.includes('--force')
  return patch({ force })
}

main()
