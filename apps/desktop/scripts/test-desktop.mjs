import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn, spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { listPackage } from '@electron/asar'

import PACKAGE_JSON from '../package.json' with { type: 'json' }

const MODE = process.argv[2] || 'help'
const ARCH = process.arch === 'arm64' ? 'arm64' : 'x64'
const DESKTOP_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const RELEASE_ROOT = path.join(DESKTOP_ROOT, 'release')
const PLATFORM = process.platform

// Platform-specific packaged-app layout. The bundled app ships an Electron
// shell and the PM payload under resources/agent-payload.
const APP = (() => {
  if (PLATFORM === 'darwin') {
    const appPath = path.join(RELEASE_ROOT, `mac-${ARCH}`, 'Hermes.app')
    return {
      appPath,
      binary: path.join(appPath, 'Contents', 'MacOS', 'Hermes'),
      resourcesPath: path.join(appPath, 'Contents', 'Resources'),
      asarPath: path.join(appPath, 'Contents', 'Resources', 'app.asar'),
      unpackedDistIndex: path.join(appPath, 'Contents', 'Resources', 'app.asar.unpacked', 'dist', 'index.html')
    }
  }
  if (PLATFORM === 'win32') {
    // electron-builder names the unpacked output per-arch: win-arm64-unpacked
    // on arm64, plain win-unpacked on x64. Accept either so the harness works
    // on both hosts instead of hardcoding the x64 layout.
    const unpacked = ['win-unpacked', `win-${ARCH}-unpacked`].map(name => path.join(RELEASE_ROOT, name)).find(exists)
    return {
      appPath: unpacked,
      binary: unpacked ? path.join(unpacked, 'Hermes.exe') : path.join(RELEASE_ROOT, 'win-unpacked', 'Hermes.exe'),
      resourcesPath: unpacked ? path.join(unpacked, 'resources') : path.join(RELEASE_ROOT, 'win-unpacked', 'resources'),
      asarPath: unpacked
        ? path.join(unpacked, 'resources', 'app.asar')
        : path.join(RELEASE_ROOT, 'win-unpacked', 'resources', 'app.asar'),
      unpackedDistIndex: unpacked
        ? path.join(unpacked, 'resources', 'app.asar.unpacked', 'dist', 'index.html')
        : path.join(RELEASE_ROOT, 'win-unpacked', 'resources', 'app.asar.unpacked', 'dist', 'index.html')
    }
  }
  // linux unpacked layout matches windows but with different binary name
  const unpacked = path.join(RELEASE_ROOT, 'linux-unpacked')
  return {
    appPath: unpacked,
    binary: path.join(unpacked, 'Hermes'),
    resourcesPath: path.join(unpacked, 'resources'),
    asarPath: path.join(unpacked, 'resources', 'app.asar'),
    unpackedDistIndex: path.join(unpacked, 'resources', 'app.asar.unpacked', 'dist', 'index.html')
  }
})()

const FRESH_SANDBOX_ROOT = path.join(os.tmpdir(), 'hermes-desktop-fresh-install')

function die(message) {
  console.error(`\n${message}`)
  process.exit(1)
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd || DESKTOP_ROOT,
    env: options.env || process.env,
    shell: Boolean(options.shell) || PLATFORM === 'win32',
    stdio: 'inherit'
  })

  if (result.status !== 0) {
    die(`${command} ${args.join(' ')} failed`)
  }
}

function exists(target) {
  return fs.existsSync(target)
}

// Match node-pty native binding location to what the bundled electron-main.cjs
// resolves at runtime. stage-native-deps.mjs stages node-pty into
// dist/node_modules/node-pty, and dist/** is asarUnpacked (see package.json
// build.asarUnpack), so in a packaged build it lands under
// resources/app.asar.unpacked/dist/node_modules/node-pty — reachable by a bare
// require('node-pty') from the bundle. Upstream node-pty 1.x is N-API based and
// ships per-arch prebuilts under prebuilds/<platform>-<arch>/; nix/local builds
// instead compile from source into build/Release/. The stage script copies
// whichever is present, so we accept either as the native payload.
function expectedNativeDepPaths() {
  const root = path.join(APP.resourcesPath, 'app.asar.unpacked', 'dist', 'node_modules', 'node-pty')
  const prebuildsDir = path.join(root, 'prebuilds', `${PLATFORM}-${ARCH}`)
  const buildReleaseDir = path.join(root, 'build', 'Release')
  return {
    packageJson: path.join(root, 'package.json'),
    prebuildsDir,
    buildReleaseDir,
    libIndex: path.join(root, 'lib', 'index.js')
  }
}

function ensurePlatformBuilds() {
  if (PLATFORM === 'darwin') return
  if (PLATFORM === 'win32') return
  if (PLATFORM === 'linux') return
  die(`Desktop bundle validation is only wired for darwin / win32 / linux; platform=${PLATFORM} is not supported.`)
}

function ensurePackagedApp() {
  if (process.env.HERMES_DESKTOP_SKIP_BUILD === '1' && exists(APP.binary)) {
    return
  }

  run('npm', ['run', 'pack'])
}

function resolveDmgPath() {
  if (!exists(RELEASE_ROOT)) {
    return path.join(RELEASE_ROOT, `Hermes-${PACKAGE_JSON.version}-${ARCH}.dmg`)
  }

  const prefix = `Hermes-${PACKAGE_JSON.version}`
  const candidates = fs
    .readdirSync(RELEASE_ROOT)
    .filter(name => name.endsWith('.dmg'))
    .filter(name => name.startsWith(prefix))
    .filter(name => name.includes(ARCH))
    .sort((a, b) => {
      const aMtime = fs.statSync(path.join(RELEASE_ROOT, a)).mtimeMs
      const bMtime = fs.statSync(path.join(RELEASE_ROOT, b)).mtimeMs
      return bMtime - aMtime
    })

  return candidates.length > 0
    ? path.join(RELEASE_ROOT, candidates[0])
    : path.join(RELEASE_ROOT, `Hermes-${PACKAGE_JSON.version}-${ARCH}.dmg`)
}

function resolveMsixPath() {
  if (!exists(RELEASE_ROOT)) return null
  const candidates = fs
    .readdirSync(RELEASE_ROOT)
    .filter(name => /\.msix$/i.test(name) && /win/i.test(name))
    .sort((a, b) => {
      const aMtime = fs.statSync(path.join(RELEASE_ROOT, a)).mtimeMs
      const bMtime = fs.statSync(path.join(RELEASE_ROOT, b)).mtimeMs
      return bMtime - aMtime
    })
  return candidates.length > 0 ? path.join(RELEASE_ROOT, candidates[0]) : null
}

function ensureDmg() {
  if (PLATFORM !== 'darwin') {
    die('DMG mode is macOS-only; on Windows use the `msix` mode instead.')
  }
  if (process.env.HERMES_DESKTOP_SKIP_BUILD === '1' && exists(resolveDmgPath())) {
    return
  }
  run('npm', ['run', 'dist:mac:dmg'])
}

function ensureMsix() {
  if (PLATFORM !== 'win32') {
    die('MSIX mode is win32-only; on macOS use the `dmg` mode instead.')
  }
  if (process.env.HERMES_DESKTOP_SKIP_BUILD === '1' && resolveMsixPath()) {
    return
  }
  run('npm', ['run', 'dist:win:msix'])
}

function openApp() {
  if (!exists(APP.binary)) {
    die(`Missing packaged app: ${APP.binary}`)
  }

  if (PLATFORM === 'darwin') {
    run('open', ['-n', APP.appPath])
  } else if (PLATFORM === 'win32') {
    // Spawn detached so the test script exits while the app keeps running.
    spawn(APP.binary, [], { detached: true, stdio: 'ignore' }).unref()
  } else {
    spawn(APP.binary, [], { detached: true, stdio: 'ignore' }).unref()
  }
}

function openDmg() {
  if (PLATFORM !== 'darwin') {
    die('DMG mode is macOS-only.')
  }
  const dmgPath = resolveDmgPath()
  if (!exists(dmgPath)) {
    die(`Missing DMG: ${dmgPath}`)
  }
  run('open', [dmgPath])
}

const CREDENTIAL_ENV_SUFFIXES = [
  '_API_KEY',
  '_TOKEN',
  '_SECRET',
  '_PASSWORD',
  '_CREDENTIALS',
  '_ACCESS_KEY',
  '_PRIVATE_KEY',
  '_OAUTH_TOKEN'
]

const CREDENTIAL_ENV_NAMES = new Set([
  'ANTHROPIC_BASE_URL',
  'ANTHROPIC_TOKEN',
  'AWS_ACCESS_KEY_ID',
  'AWS_SECRET_ACCESS_KEY',
  'AWS_SESSION_TOKEN',
  'CUSTOM_API_KEY',
  'GEMINI_BASE_URL',
  'OPENAI_BASE_URL',
  'OPENROUTER_BASE_URL',
  'OLLAMA_BASE_URL',
  'GROQ_BASE_URL',
  'XAI_BASE_URL'
])

function isCredentialEnvVar(name) {
  if (CREDENTIAL_ENV_NAMES.has(name)) return true
  return CREDENTIAL_ENV_SUFFIXES.some(suffix => name.endsWith(suffix))
}

function launchFresh() {
  if (!exists(APP.binary)) {
    die(`Missing app executable: ${APP.binary}`)
  }

  const sandbox = fs.mkdtempSync(`${FRESH_SANDBOX_ROOT}-`)
  const userDataDir = path.join(sandbox, 'electron-user-data')
  const hermesHome = path.join(sandbox, 'hermes-home')
  const cwd = path.join(sandbox, 'workspace')

  fs.mkdirSync(userDataDir, { recursive: true })
  fs.mkdirSync(hermesHome, { recursive: true })
  fs.mkdirSync(cwd, { recursive: true })

  // Strip every credential-shaped env var so the sandbox is actually fresh.
  const env = {}
  for (const [key, value] of Object.entries(process.env)) {
    if (isCredentialEnvVar(key)) continue
    env[key] = value
  }

  env.HERMES_DESKTOP_CWD = cwd
  env.HERMES_DESKTOP_IGNORE_EXISTING = '1'
  env.HERMES_DESKTOP_TEST_MODE = 'fresh-install'
  env.HERMES_DESKTOP_USER_DATA_DIR = userDataDir
  env.HERMES_HOME = hermesHome
  delete env.HERMES_DESKTOP_HERMES
  delete env.HERMES_DESKTOP_HERMES_ROOT

  const child = spawn(APP.binary, [], {
    cwd: os.homedir(),
    detached: true,
    env,
    stdio: 'ignore'
  })
  child.unref()

  console.log('\nFresh install sandbox:')
  console.log(`  root: ${sandbox}`)
  console.log(`  electron userData: ${userDataDir}`)
  console.log(`  HERMES_HOME: ${hermesHome}`)
  console.log(`  cwd: ${cwd}`)
}
// ── lifecycle: packaged app → canonical gateway ready → relaunch → owned teardown ──
// Electron owns its viewer; the canonical gateway survives app quit. Use the
// real preload descriptor and native control identity, then retire only the
// gateway created under this fresh sandbox after both viewer sessions finish.
const LIFECYCLE_BACKEND_ROOT = process.env.HERMES_DESKTOP_LIFECYCLE_BACKEND_ROOT
const LIFECYCLE_TIMEOUT_MS = Number(process.env.HERMES_DESKTOP_LIFECYCLE_TIMEOUT_MS) || 150_000
const LIFECYCLE_KEEP = process.env.HERMES_DESKTOP_LIFECYCLE_KEEP === '1'

function lifecycleEnv(sandbox) {
  const userDataDir = path.join(sandbox, 'electron-user-data')
  const hermesHome = path.join(sandbox, 'hermes-home')
  const cwd = path.join(sandbox, 'workspace')
  for (const dir of [userDataDir, hermesHome, cwd]) fs.mkdirSync(dir, { recursive: true })

  const env = {}
  for (const [key, value] of Object.entries(process.env)) {
    if (isCredentialEnvVar(key)) continue
    env[key] = value
  }
  env.HERMES_DESKTOP_CWD = cwd
  env.HERMES_DESKTOP_USER_DATA_DIR = userDataDir
  env.HERMES_HOME = hermesHome
  env.HERMES_DESKTOP_SKIP_QUIT_CONFIRM = '1'
  // Window-title label only — NOT package identity; package identity is build-
  // time. Identity isolation here is the sandboxed userData (single-instance
  // lock is scoped to it), so a live Hermes instance can never be contacted.
  env.HERMES_DESKTOP_APP_NAME = 'HermesLifecycleProbe'
  // REQUIRED: with a thin (external-payload) build there is no sealed runtime,
  // and an unresolved backend would fall through to first-run bootstrap —
  // install.ps1, which writes User PATH, Start-Menu shortcuts and ACLs on a
  // real host. This harness must never let that happen.
  if (LIFECYCLE_BACKEND_ROOT) {
    env.HERMES_DESKTOP_HERMES_ROOT = path.resolve(LIFECYCLE_BACKEND_ROOT)
  }
  delete env.HERMES_DESKTOP_HERMES
  delete env.HERMES_DESKTOP_TEST_MODE
  return { env, userDataDir, hermesHome, cwd }
}

function lifecycleControl(operation, context) {
  const { env, hermesHome, ...request } = context
  const root = env.HERMES_DESKTOP_HERMES_ROOT
  const python =
    env.HERMES_PYTHON ||
    [
      path.join(root, '.venv', PLATFORM === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
      path.join(root, 'venv', PLATFORM === 'win32' ? 'Scripts/python.exe' : 'bin/python')
    ].find(exists)
  if (!python || !exists(python)) throw new Error('Lifecycle requires an existing backend Python interpreter')
  const result = spawnSync(
    python,
    [path.join(DESKTOP_ROOT, 'scripts', 'lifecycle_gateway.py'), operation, hermesHome],
    {
      cwd: root,
      env: { ...env, PYTHONPATH: root },
      input: JSON.stringify(request),
      encoding: 'utf8',
      timeout: operation === 'stop' ? 40_000 : 12_000
    }
  )
  if (result.error || result.status !== 0) {
    throw new Error(
      `Lifecycle gateway ${operation} refused: ${result.error?.message || result.stderr?.trim() || result.status}`
    )
  }
  return JSON.parse(result.stdout)
}

async function gatewayBackendReady(window, context, label, recordOwner) {
  const deadline = Date.now() + LIFECYCLE_TIMEOUT_MS
  let timer
  const descriptor = await Promise.race([
    window.evaluate(async () => {
      const connection = await window.hermesDesktop.getConnection()
      return {
        mode: connection.mode,
        source: connection.source,
        baseUrl: connection.baseUrl,
        gatewayEndpoint: connection.gatewayEndpoint
      }
    }),
    new Promise((_, reject) => {
      timer = setTimeout(
        () => reject(new Error(`[${label}] gateway descriptor timed out after ${LIFECYCLE_TIMEOUT_MS}ms`)),
        LIFECYCLE_TIMEOUT_MS
      )
    })
  ]).finally(() => clearTimeout(timer))
  const owner = lifecycleControl('inspect', { ...context, connection: descriptor })
  recordOwner(owner)
  let lastProbe = 'not answered'
  while (Date.now() < deadline) {
    let response
    try {
      response = await fetch(`${owner.api_origin}/api/health`, {
        signal: AbortSignal.timeout(5_000),
        redirect: 'error'
      })
      lastProbe = `HTTP ${response.status}`
    } catch (error) {
      lastProbe = error?.name || 'connection failed'
    }
    if (response?.status === 200) {
      const health = await response.json()
      if (health.ok !== true || health.commit !== owner.code_sha) {
        throw new Error(`[${label}] gateway health status/revision differs from verified owner`)
      }
      return owner
    }
    await new Promise(resolve => setTimeout(resolve, 500))
  }
  throw new Error(
    `[${label}] verified gateway did not answer /api/health within ${LIFECYCLE_TIMEOUT_MS}ms (${lastProbe})`
  )
}

// Everything the first session created in the isolated home must survive the
// quit + relaunch cycle. Transient scratch (*.tmp/.lock/.part) is excluded:
// a healthy later session may clean up stale temp files — that is not loss.
function snapshotHome(root) {
  const out = []
  const walk = (dir, depth) => {
    if (depth > 2) return
    let entries
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const entry of entries) {
      if (/\.(tmp|lock|part)$/.test(entry.name)) continue
      const rel = path.relative(root, path.join(dir, entry.name))
      out.push(entry.isDirectory() ? `dir ${rel}` : `file ${rel}`)
      if (entry.isDirectory()) walk(path.join(dir, entry.name), depth + 1)
    }
  }
  walk(root, 0)
  return out
}

async function runLifecycle() {
  if (!['darwin', 'linux'].includes(PLATFORM))
    throw new Error('Lifecycle identity-bound gateway cleanup currently requires macOS or Linux')
  if (!LIFECYCLE_BACKEND_ROOT) throw new Error('Lifecycle requires HERMES_DESKTOP_LIFECYCLE_BACKEND_ROOT')
  const { _electron } = await import('@playwright/test')
  const sandbox = fs.mkdtempSync(`${FRESH_SANDBOX_ROOT}-lifecycle-`)
  const { env, userDataDir, hermesHome } = lifecycleEnv(sandbox)
  const sessions = []
  let preservedBefore = null
  let ownedGateway = null
  let failure = null
  const revision = spawnSync('git', ['-C', env.HERMES_DESKTOP_HERMES_ROOT, 'rev-parse', 'HEAD'], { encoding: 'utf8' })
  if (revision.status !== 0 || !/^[a-f0-9]{40}$/.test(revision.stdout.trim()))
    throw new Error('Cannot establish backend source revision')
  const context = { env, hermesHome, sandbox, code_sha: revision.stdout.trim(), started_after: Date.now() / 1000 }

  try {
    for (const label of ['session-1', 'session-2']) {
      const app = await _electron.launch({
        executablePath: APP.binary,
        env,
        cwd: sandbox,
        timeout: LIFECYCLE_TIMEOUT_MS
      })
      const proc = app.process()
      console.log(`[${label}] launched ${APP.binary} (pid ${proc.pid})`)
      try {
        const window = await app.firstWindow({ timeout: LIFECYCLE_TIMEOUT_MS })
        console.log(`[${label}] first window: ${window.url()}`)
        const ready = await gatewayBackendReady(window, { ...context, expected: ownedGateway }, label, owner => {
          ownedGateway = owner
          fs.writeFileSync(path.join(sandbox, 'lifecycle-gateway.json'), JSON.stringify(owner, null, 2), {
            mode: 0o600
          })
        })
        console.log(`[${label}] READY: owned gateway ${ready.pid} at ${ready.api_origin}, /api/health → 200`)
        sessions.push({ label, appPid: proc.pid, ...ready, windowUrl: window.url() })
        if (label === 'session-1') preservedBefore = snapshotHome(hermesHome)
      } finally {
        // Quit only this viewer. The canonical gateway stays available for relaunch.
        await app.close()
        await new Promise((resolve, reject) => {
          if (proc.exitCode !== null || proc.signalCode !== null) return resolve()
          const timer = setTimeout(
            () => reject(new Error(`[${label}] app process ${proc.pid} did not exit after close()`)),
            30_000
          )
          proc.once('exit', () => {
            clearTimeout(timer)
            resolve()
          })
        })
        console.log(`[${label}] exited cleanly (code ${proc.exitCode})`)
      }
    }

    const preservedAfter = snapshotHome(hermesHome)
    const lost = preservedBefore.filter(entry => !preservedAfter.includes(entry))
    console.log('\nLifecycle summary:')
    console.log(`  sessions: ${sessions.length}`)
    console.log(`  home entries before relaunch: ${preservedBefore.length}, after: ${preservedAfter.length}`)
    if (lost.length > 0) {
      throw new Error(`preserved-state check FAILED — entries missing after relaunch:\n  ${lost.join('\n  ')}`)
    }
    console.log('  preservation: all pre-relaunch home entries survived the quit + relaunch cycle')
    console.log(
      JSON.stringify(
        {
          sandbox,
          userDataDir,
          hermesHome,
          backendRoot: env.HERMES_DESKTOP_HERMES_ROOT || null,
          sessions,
          preserved: { before: preservedBefore.length, after: preservedAfter.length, lost: 0 }
        },
        null,
        2
      )
    )
  } catch (error) {
    failure = error
    throw error
  } finally {
    let cleaned = false
    try {
      const cleanup = lifecycleControl('stop', { ...context, expected: ownedGateway })
      fs.writeFileSync(path.join(sandbox, 'lifecycle-gateway-cleanup.json'), JSON.stringify(cleanup, null, 2), {
        mode: 0o600
      })
      console.log(`  owned gateway ${cleanup.pid} exited through native pause; control socket absent`)
      cleaned = true
    } catch (cleanupError) {
      console.error(`  gateway cleanup unverified; sandbox retained: ${sandbox}`)
      if (failure) throw new AggregateError([failure, cleanupError], `${failure.message}; ${cleanupError.message}`)
      throw cleanupError
    } finally {
      if (cleaned && !LIFECYCLE_KEEP) {
        fs.rmSync(sandbox, { recursive: true, force: true })
        console.log(`  sandbox removed: ${sandbox}`)
      } else {
        console.log(`  sandbox kept: ${sandbox}`)
      }
    }
  }
}
// The packaged app must contain the PM payload, node-pty, and renderer assets.
function validateBundle() {
  if (!exists(APP.binary)) {
    die(`Missing packaged app binary: ${APP.binary}`)
  }

  // The payload may be the real pm bundle (staged by scripts/bundles/desktop.py /
  // `hermes pm bundle --out build/agent-payload`) or the external stub
  // (plain `npm run pack` in the PR/JS lane — the app fetches the runtime at
  // first launch via the stage protocol). Validate the payload only when a
  // real one is present; the stub is the thin-installer contract.
  const payloadRoot = path.join(APP.resourcesPath, 'agent-payload')
  const payloadManifestPath = path.join(payloadRoot, 'manifest.json')
  let payloadManifest = null
  if (exists(payloadManifestPath)) {
    try {
      payloadManifest = JSON.parse(fs.readFileSync(payloadManifestPath, 'utf8'))
    } catch (err) {
      die(`Bundled payload manifest is not valid JSON: ${err.message}`)
    }
  }
  if (payloadManifest != null && payloadManifest.external !== true) {
    for (const key of ['repo', 'store', 'venv']) {
      if (typeof payloadManifest[key] !== 'string') {
        die(`Bundled payload manifest is missing ${key}: ${JSON.stringify(payloadManifest)}`)
      }
    }
    const payloadPython = path.join(
      payloadRoot,
      payloadManifest.venv,
      PLATFORM === 'win32' ? 'Scripts' : 'bin',
      PLATFORM === 'win32' ? 'python.exe' : 'python'
    )
    if (!exists(payloadPython)) {
      die(`Missing bundled payload Python: ${payloadPython}`)
    }
    if (PLATFORM === 'win32') {
      const payloadShim = path.join(payloadRoot, payloadManifest.venv, 'Scripts', 'hermes.exe')
      if (!exists(payloadShim)) {
        die(`Missing bundled payload shim: ${payloadShim}`)
      }
    }
  }

  // Positive assertion: node-pty native deps shipped
  const native = expectedNativeDepPaths()
  if (!exists(native.packageJson)) {
    die(`Missing node-pty package.json in app.asar.unpacked: ${native.packageJson}`)
  }
  if (!exists(native.libIndex)) {
    die(`Missing node-pty lib/index.js in app.asar.unpacked: ${native.libIndex}`)
  }
  // The native binary lands in prebuilds/<platform>-<arch>/ (downloaded prebuild)
  // OR build/Release/ (compiled from source). stage-native-deps.mjs copies
  // whichever is present, so accept either.
  const nativeBinaryDirs = [native.prebuildsDir, native.buildReleaseDir].filter(exists)
  if (nativeBinaryDirs.length === 0) {
    die(
      `Missing node-pty native binary dir for ${PLATFORM}-${ARCH}: neither ` +
        `${native.prebuildsDir} nor ${native.buildReleaseDir} exists`
    )
  }
  const nodeBinaries = nativeBinaryDirs.flatMap(dir => fs.readdirSync(dir).filter(name => name.endsWith('.node')))
  if (nodeBinaries.length === 0) {
    die(`No .node native binaries found in: ${nativeBinaryDirs.join(', ')}`)
  }
  // Darwin requires a runtime-execed spawn-helper alongside pty.node; missing
  // it manifests as "ENOENT: spawn-helper" on first pty.spawn() call.
  if (PLATFORM === 'darwin') {
    const spawnHelper = nativeBinaryDirs.map(dir => path.join(dir, 'spawn-helper')).find(exists)
    if (!spawnHelper) {
      die(`Missing node-pty spawn-helper (required on darwin) in: ${nativeBinaryDirs.join(', ')}`)
    }
  }

  // Renderer payload check (either unpacked or in the asar)
  if (exists(APP.unpackedDistIndex)) {
    return { payloadManifest, nodeBinaries }
  }
  if (!exists(APP.asarPath)) {
    die(`Missing renderer payload: neither ${APP.unpackedDistIndex} nor ${APP.asarPath} exists`)
  }
  const files = listPackage(APP.asarPath)
  // Normalize separators because @electron/asar's listPackage returns
  // backslash-prefixed entries on Windows ('\\dist\\index.html') and
  // forward-slash on Unix.
  const normalized = files.map(f => f.replace(/\\/g, '/').replace(/^\/+/, ''))
  if (!normalized.includes('dist/index.html')) {
    die(`Missing renderer payload file in app.asar: ${APP.asarPath} (expected dist/index.html)`)
  }
  return { payloadManifest, nodeBinaries }
}

function printArtifacts(options = {}) {
  const payloadManifest = options.payloadManifest

  console.log('\nDesktop artifacts:')
  console.log(`  app: ${APP.appPath}`)
  if (PLATFORM === 'darwin') {
    console.log(`  dmg: ${resolveDmgPath()}`)
  } else if (PLATFORM === 'win32') {
    const msix = resolveMsixPath()
    if (msix) console.log(`  package: ${msix}`)
  }
  if (payloadManifest) {
    console.log(`  payload: ${payloadManifest.repo} + ${payloadManifest.venv}`)
  }
  if (options.nodeBinaries && options.nodeBinaries.length > 0) {
    console.log(`  node-pty binaries: ${options.nodeBinaries.join(', ')}`)
  }
}

function help() {
  console.log(`Usage:
  npm run test:desktop:existing  # build packaged app, launch with normal PATH/existing Hermes
  npm run test:desktop:fresh     # build packaged app, launch with temp userData + HERMES_HOME
  npm run test:desktop:dmg       # (macOS only) build DMG and open it
  npm run test:desktop:msix      # (win32 only) build MSIX package
  npm run test:desktop:all       # build the platform package and validate the payload

Fast rerun (skip rebuild if the packaged app already exists):
  HERMES_DESKTOP_SKIP_BUILD=1 npm run test:desktop:fresh

Automated packaged lifecycle (no host side effects; isolated userData + HERMES_HOME):
  npm run test:desktop:lifecycle
  # start packaged app → canonical gateway ready (/api/health 200) → graceful
  # quit → relaunch → preserved-home check → full teardown of our own processes.
  # Identity-bound gateway teardown currently supports macOS and Linux.
  # Requires a backend root the packaged app may use (backend-resolution rung 1):
  #   HERMES_DESKTOP_LIFECYCLE_BACKEND_ROOT=<hermes checkout with .venv|venv>
  # Knobs: HERMES_DESKTOP_LIFECYCLE_TIMEOUT_MS and
  # HERMES_DESKTOP_LIFECYCLE_KEEP=1 to keep the sandbox for inspection.
`)
}

ensurePlatformBuilds()

if (MODE === 'existing') {
  ensurePackagedApp()
  const result = validateBundle()
  openApp()
  printArtifacts(result)
} else if (MODE === 'fresh') {
  ensurePackagedApp()
  const result = validateBundle()
  printArtifacts({ ...launchFresh(), ...result })
} else if (MODE === 'dmg') {
  ensureDmg()
  openDmg()
  printArtifacts()
} else if (MODE === 'msix') {
  ensureMsix()
  printArtifacts(validateBundle())
} else if (MODE === 'all') {
  if (PLATFORM === 'darwin') {
    ensureDmg()
  } else if (PLATFORM === 'win32') {
    ensureMsix()
  } else {
    ensurePackagedApp()
  }
  printArtifacts(validateBundle())
} else if (MODE === 'lifecycle') {
  ensurePackagedApp()
  printArtifacts(validateBundle())
  runLifecycle().catch(err => {
    console.error(err && err.message ? err.message : err)
    process.exit(1)
  })
} else {
  help()
}
