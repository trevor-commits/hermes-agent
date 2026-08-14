import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

const SAFE_LEAF = /^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$/
const SAFE_TRANSACTION_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/

export interface RuntimeAsset {
  readonly source: string
  readonly target: string
  readonly executable: boolean
}

export const POSIX_UPDATER_RUNTIME_ASSETS: readonly RuntimeAsset[] = Object.freeze([
  Object.freeze({ source: 'posix.sh', target: 'posix.sh', executable: true }),
  Object.freeze({ source: 'atomic_macos.py', target: 'atomic_macos.py', executable: true }),
  Object.freeze({ source: '_atomic_macos_cli.py', target: '_atomic_macos_cli.py', executable: false }),
  Object.freeze({
    source: '_atomic_macos_transaction.py',
    target: '_atomic_macos_transaction.py',
    executable: false
  }),
  Object.freeze({ source: '_atomic_macos_git.py', target: '_atomic_macos_git.py', executable: false }),
  Object.freeze({
    source: '_atomic_macos_candidate.py',
    target: '_atomic_macos_candidate.py',
    executable: false
  }),
  Object.freeze({ source: 'serve-ui.py', target: 'serve-ui.py', executable: false }),
  Object.freeze({ source: 'ui.html', target: 'ui.html', executable: false })
])

interface RuntimeManifestFile {
  path: string
  sha256: string
  size: number
  mode: number
}

export interface RuntimeManifest {
  schema_version: 1
  principal: 'hermes-atomic-update'
  transaction_id: string
  files: RuntimeManifestFile[]
}

export interface PublishPosixUpdaterRuntimeOptions {
  runtimeRoot: string
  sourceRoot: string
  transactionId: string
}

export interface PublishedPosixUpdaterRuntime {
  directory: string
  directoryDevice: string
  directoryInode: string
  entrypoint: string
  manifest: RuntimeManifest
  manifestSha256: string
}

export interface PublishedRuntimeBinding {
  directoryDevice: string
  directoryInode: string
  manifestSha256: string
  transactionId: string
}

export interface LaunchPublishedAtomicMacosOptions {
  binding: PublishedRuntimeBinding
  command: 'capabilities' | 'recover'
  directory: string
  environment?: NodeJS.ProcessEnv
  timeoutMs?: number
  transactionId?: string
  transactionsRoot: string
}

export interface PublishedAtomicMacosLaunchResult {
  status: 0 | 64 | 75
  stdout: string
  stderr: string
}

function expectedUid(): number | null {
  return typeof process.getuid === 'function' ? process.getuid() : null
}

function validateLeaf(value: string, label: string): string {
  if (!SAFE_LEAF.test(value) || value === '.' || value === '..') {
    throw new Error(`${label} must be one safe path component`)
  }

  return value
}

function validateTransactionId(value: string): string {
  if (!SAFE_TRANSACTION_ID.test(value) || value === '.' || value === '..') {
    throw new Error('transaction id must be one safe path component')
  }

  return value
}

function canonicalAssets(): RuntimeAsset[] {
  return POSIX_UPDATER_RUNTIME_ASSETS.map(asset => ({ ...asset }))
}

function assertOwnedRealDirectory(directory: string, requiredMode?: number): fs.BigIntStats {
  const observed = fs.lstatSync(directory, { bigint: true })
  const uid = expectedUid()

  if (observed.isSymbolicLink() || !observed.isDirectory()) {
    throw new Error(`updater runtime path must be a real directory: ${directory}`)
  }
  if (uid !== null && observed.uid !== BigInt(uid)) {
    throw new Error(`updater runtime path has an unexpected owner: ${directory}`)
  }
  if (
    requiredMode === undefined ? Boolean(observed.mode & 0o022n) : (observed.mode & 0o777n) !== BigInt(requiredMode)
  ) {
    throw new Error(`updater runtime path has unsafe mode: ${directory}`)
  }

  return observed
}

function assertNoSymlinkAncestors(candidate: string): void {
  const absolute = path.resolve(candidate)
  const parsed = path.parse(absolute)
  let current = parsed.root

  for (const component of absolute.slice(parsed.root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, component)
    const observed = fs.lstatSync(current)

    if (observed.isSymbolicLink()) {
      throw new Error(`updater runtime path has a symlink ancestor: ${current}`)
    }
  }
}

function ensureRuntimeRoot(runtimeRoot: string): void {
  const parent = path.dirname(runtimeRoot)
  let created = false

  assertNoSymlinkAncestors(parent)
  assertOwnedRealDirectory(parent)

  try {
    fs.mkdirSync(runtimeRoot, { mode: 0o700 })
    created = true
  } catch (error: any) {
    if (error?.code !== 'EEXIST') {
      throw new Error(`could not create updater runtime root: ${String(error)}`)
    }
  }

  assertNoSymlinkAncestors(runtimeRoot)
  assertOwnedRealDirectory(runtimeRoot, 0o700)
  if (created) {
    fsyncDirectory(parent)
  }
}

function fsyncDirectory(directory: string): void {
  const descriptor = fs.openSync(directory, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0))

  try {
    fs.fsyncSync(descriptor)
  } finally {
    fs.closeSync(descriptor)
  }
}

function sameFileIdentity(left: fs.BigIntStats, right: fs.BigIntStats): boolean {
  return (
    left.dev === right.dev &&
    left.ino === right.ino &&
    left.mode === right.mode &&
    left.uid === right.uid &&
    left.size === right.size &&
    left.mtimeNs === right.mtimeNs &&
    left.ctimeNs === right.ctimeNs
  )
}

function readVerifiedRegularFile(file: string, mode?: number, maximumSize = 16 * 1024 * 1024): Buffer {
  const before = fs.lstatSync(file, { bigint: true })
  const uid = expectedUid()

  if (before.isSymbolicLink() || !before.isFile()) {
    throw new Error(`updater runtime member must be a regular file, not a symlink: ${file}`)
  }
  if (uid !== null && before.uid !== BigInt(uid)) {
    throw new Error(`updater runtime member has an unexpected owner: ${file}`)
  }
  if (mode === undefined ? Boolean(before.mode & 0o022n) : (before.mode & 0o777n) !== BigInt(mode)) {
    throw new Error(`updater runtime member has unsafe mode: ${file}`)
  }
  if (before.size < 0n || before.size > BigInt(maximumSize)) {
    throw new Error(`updater runtime member exceeds its size limit: ${file}`)
  }

  const descriptor = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0))

  try {
    const opened = fs.fstatSync(descriptor, { bigint: true })

    if (
      !opened.isFile() ||
      opened.dev !== before.dev ||
      opened.ino !== before.ino ||
      (uid !== null && opened.uid !== BigInt(uid)) ||
      (mode === undefined ? Boolean(opened.mode & 0o022n) : (opened.mode & 0o777n) !== BigInt(mode))
    ) {
      throw new Error(`updater runtime member changed while opening: ${file}`)
    }

    const bytes = fs.readFileSync(descriptor)
    const after = fs.fstatSync(descriptor, { bigint: true })

    if (!sameFileIdentity(opened, after) || BigInt(bytes.byteLength) !== opened.size) {
      throw new Error(`updater runtime member changed while reading: ${file}`)
    }

    return bytes
  } finally {
    fs.closeSync(descriptor)
  }
}

function writeDurableExclusiveFile(directory: string, leaf: string, bytes: Buffer, mode: number): void {
  const destination = path.join(directory, validateLeaf(leaf, 'updater runtime output'))
  const descriptor = fs.openSync(
    destination,
    fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | (fs.constants.O_NOFOLLOW ?? 0),
    mode
  )

  try {
    fs.writeFileSync(descriptor, bytes)
    fs.fchmodSync(descriptor, mode)
    fs.fsyncSync(descriptor)
  } finally {
    fs.closeSync(descriptor)
  }
}

function sha256(bytes: Buffer): string {
  return createHash('sha256').update(bytes).digest('hex')
}

function manifestBytes(manifest: RuntimeManifest): Buffer {
  return Buffer.from(`${JSON.stringify(manifest)}\n`, 'utf8')
}

export function publishPosixUpdaterRuntime(options: PublishPosixUpdaterRuntimeOptions): PublishedPosixUpdaterRuntime {
  const transactionId = validateTransactionId(options.transactionId)
  const runtimeRoot = path.resolve(options.runtimeRoot)
  const sourceRoot = path.resolve(options.sourceRoot)
  const finalDirectory = path.join(runtimeRoot, transactionId)
  const assets = canonicalAssets()

  ensureRuntimeRoot(runtimeRoot)
  assertNoSymlinkAncestors(sourceRoot)
  assertOwnedRealDirectory(sourceRoot)

  const preparedAssets = assets.map(asset => {
    validateLeaf(asset.source, 'updater runtime source')

    return {
      asset,
      bytes: readVerifiedRegularFile(path.join(sourceRoot, asset.source))
    }
  })

  try {
    fs.mkdirSync(finalDirectory, { mode: 0o700 })
  } catch (error: any) {
    if (error?.code === 'EEXIST') {
      throw new Error(`updater runtime transaction already exists: ${transactionId}`)
    }
    throw error
  }
  fsyncDirectory(runtimeRoot)

  const directoryIdentity = assertOwnedRealDirectory(finalDirectory, 0o700)
  const files: RuntimeManifestFile[] = []

  for (const { asset, bytes } of preparedAssets) {
    const mode = asset.executable ? 0o500 : 0o400

    writeDurableExclusiveFile(finalDirectory, asset.target, bytes, mode)
    files.push({ mode, path: asset.target, sha256: sha256(bytes), size: bytes.byteLength })
  }

  files.sort((left, right) => (left.path < right.path ? -1 : left.path > right.path ? 1 : 0))
  const manifest: RuntimeManifest = {
    files,
    principal: 'hermes-atomic-update',
    schema_version: 1,
    transaction_id: transactionId
  }
  const bytes = manifestBytes(manifest)

  writeDurableExclusiveFile(finalDirectory, 'manifest.json', bytes, 0o400)
  fsyncDirectory(finalDirectory)

  const binding: PublishedRuntimeBinding = {
    directoryDevice: directoryIdentity.dev.toString(10),
    directoryInode: directoryIdentity.ino.toString(10),
    manifestSha256: sha256(bytes),
    transactionId
  }
  const validated = validatePublishedUpdaterRuntime(finalDirectory, binding)

  return {
    directory: finalDirectory,
    directoryDevice: binding.directoryDevice,
    directoryInode: binding.directoryInode,
    entrypoint: path.join(finalDirectory, 'posix.sh'),
    manifest: validated,
    manifestSha256: sha256(bytes)
  }
}

function readValidatedPublishedUpdaterRuntime(
  directory: string,
  binding: PublishedRuntimeBinding
): { files: Readonly<Record<string, Buffer>>; manifest: RuntimeManifest } {
  const root = path.resolve(directory)
  const assets = canonicalAssets()

  assertNoSymlinkAncestors(root)
  const parentIdentity = assertOwnedRealDirectory(path.dirname(root), 0o700)
  const rootIdentity = assertOwnedRealDirectory(root, 0o700)

  if (
    !/^[0-9]+$/.test(binding.directoryDevice) ||
    !/^[0-9]+$/.test(binding.directoryInode) ||
    rootIdentity.dev.toString(10) !== binding.directoryDevice ||
    rootIdentity.ino.toString(10) !== binding.directoryInode ||
    path.basename(root) !== binding.transactionId
  ) {
    throw new Error('published updater runtime directory does not match its trusted binding')
  }

  const expectedNames = new Set([...assets.map(asset => asset.target), 'manifest.json'])
  const observedNames = fs.readdirSync(root)

  if (observedNames.length !== expectedNames.size || observedNames.some(name => !expectedNames.has(name))) {
    throw new Error('published updater runtime contains an unexpected file set')
  }

  const rawManifest = readVerifiedRegularFile(path.join(root, 'manifest.json'), 0o400, 256 * 1024)

  if (!/^[0-9a-f]{64}$/.test(binding.manifestSha256) || sha256(rawManifest) !== binding.manifestSha256) {
    throw new Error('published updater runtime manifest does not match its trusted digest')
  }

  let manifest: RuntimeManifest

  try {
    manifest = JSON.parse(rawManifest.toString('utf8'))
  } catch {
    throw new Error('published updater runtime manifest is invalid JSON')
  }

  const manifestKeys = Object.keys(manifest as any).sort()
  const transactionId = path.basename(root)

  if (
    JSON.stringify(manifestKeys) !== JSON.stringify(['files', 'principal', 'schema_version', 'transaction_id']) ||
    manifest?.schema_version !== 1 ||
    manifest?.principal !== 'hermes-atomic-update' ||
    manifest?.transaction_id !== transactionId ||
    !Array.isArray(manifest.files)
  ) {
    throw new Error('published updater runtime manifest identity is invalid')
  }
  if (!rawManifest.equals(manifestBytes(manifest))) {
    throw new Error('published updater runtime manifest encoding is not canonical')
  }

  const expectedFiles = [...assets].sort((left, right) =>
    left.target < right.target ? -1 : left.target > right.target ? 1 : 0
  )

  if (manifest.files.length !== expectedFiles.length) {
    throw new Error('published updater runtime manifest file count is invalid')
  }

  const verifiedFiles: Record<string, Buffer> = {}

  for (let index = 0; index < expectedFiles.length; index += 1) {
    const asset = expectedFiles[index]
    const record = manifest.files[index]
    const recordKeys = Object.keys(record ?? {}).sort()
    const mode = asset.executable ? 0o500 : 0o400
    const bytes = readVerifiedRegularFile(path.join(root, asset.target), mode)

    if (
      JSON.stringify(recordKeys) !== JSON.stringify(['mode', 'path', 'sha256', 'size']) ||
      record.path !== asset.target ||
      record.mode !== mode ||
      record.size !== bytes.byteLength ||
      record.sha256 !== sha256(bytes)
    ) {
      throw new Error(`published updater runtime manifest mismatch: ${asset.target}`)
    }
    verifiedFiles[asset.target] = bytes
  }

  const rootAfter = fs.lstatSync(root, { bigint: true })
  const parentAfter = fs.lstatSync(path.dirname(root), { bigint: true })
  if (!sameFileIdentity(rootIdentity, rootAfter) || !sameFileIdentity(parentIdentity, parentAfter)) {
    throw new Error('published updater runtime directory changed during validation')
  }

  return { files: Object.freeze(verifiedFiles), manifest }
}

export function validatePublishedUpdaterRuntime(directory: string, binding: PublishedRuntimeBinding): RuntimeManifest {
  return readValidatedPublishedUpdaterRuntime(directory, binding).manifest
}

const TRUSTED_PYTHON_BOOTSTRAP = `
import base64
import json
import pathlib
import sys
import types

payload = json.load(sys.stdin)
runtime_directory = pathlib.Path(sys.argv[1])
verified = {
    leaf: base64.b64decode(encoded, validate=True)
    for leaf, encoded in payload["files"].items()
}
module = types.ModuleType("__main__")
namespace = module.__dict__
namespace.update({
    "__file__": str(runtime_directory / "atomic_macos.py"),
    "__name__": "__main__",
    "_HERMES_TRUSTED_RUNTIME_BOOTSTRAP": True,
    "_HERMES_VERIFIED_RUNTIME_BYTES": verified,
    "_HERMES_VERIFIED_RUNTIME_MODES": payload["modes"],
    "_HERMES_VERIFIED_RUNTIME_TRANSACTION_ID": payload["runtime_transaction_id"],
})
sys.modules["__main__"] = module
sys.argv = [namespace["__file__"], *payload["arguments"]]
exec(compile(verified["atomic_macos.py"], namespace["__file__"], "exec"), namespace, namespace)
`.trim()

function launchFailure(
  failureCode: 'runtime_validation_failed' | 'recovery_launcher_failed'
): PublishedAtomicMacosLaunchResult {
  const status = failureCode === 'runtime_validation_failed' ? 64 : 75
  const payload = {
    failure_code: failureCode,
    manual_recovery_required: false,
    ok: false,
    schema_version: 1,
    status: status === 64 ? 'unsafe' : 'unrecovered'
  }
  return { status, stderr: '', stdout: `${JSON.stringify(payload)}\n` }
}

export function launchPublishedAtomicMacos(
  options: LaunchPublishedAtomicMacosOptions
): PublishedAtomicMacosLaunchResult {
  let validated: ReturnType<typeof readValidatedPublishedUpdaterRuntime>

  try {
    validated = readValidatedPublishedUpdaterRuntime(options.directory, options.binding)
  } catch {
    return launchFailure('runtime_validation_failed')
  }

  let argumentsList: string[]
  try {
    if (
      !path.isAbsolute(options.transactionsRoot) ||
      path.normalize(options.transactionsRoot) !== options.transactionsRoot
    ) {
      throw new Error('transactions root must be canonical and absolute')
    }
    if (options.command === 'recover' && options.transactionId !== options.binding.transactionId) {
      throw new Error('recovery transaction does not match its runtime binding')
    }
    argumentsList = [
      ...(options.command === 'capabilities'
        ? ['--capabilities']
        : [
            'recover',
            '--transaction',
            validateTransactionId(options.transactionId ?? ''),
            '--transactions-root',
            options.transactionsRoot
          ]),
      '--runtime-device',
      options.binding.directoryDevice,
      '--runtime-inode',
      options.binding.directoryInode,
      '--manifest-sha256',
      options.binding.manifestSha256
    ]
  } catch {
    return launchFailure('runtime_validation_failed')
  }

  const files = Object.fromEntries(
    Object.entries(validated.files).map(([leaf, bytes]) => [leaf, bytes.toString('base64')])
  )
  const modes = Object.fromEntries(
    POSIX_UPDATER_RUNTIME_ASSETS.map(asset => [asset.target, asset.executable ? 0o500 : 0o400])
  )
  const launched = spawnSync('/usr/bin/python3', ['-c', TRUSTED_PYTHON_BOOTSTRAP, path.resolve(options.directory)], {
    encoding: 'utf8',
    env: options.environment,
    input: JSON.stringify({
      arguments: argumentsList,
      files,
      modes,
      runtime_transaction_id: options.binding.transactionId
    }),
    maxBuffer: 8192,
    timeout: options.timeoutMs ?? 5000
  })

  const stdout = typeof launched.stdout === 'string' ? launched.stdout : ''
  const stderr = typeof launched.stderr === 'string' ? launched.stderr : ''
  if (
    launched.error ||
    (launched.status !== 0 && launched.status !== 64 && launched.status !== 75) ||
    stderr !== '' ||
    Buffer.byteLength(stdout, 'utf8') > 4096
  ) {
    return launchFailure('recovery_launcher_failed')
  }
  try {
    const payload = JSON.parse(stdout)
    if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
      return launchFailure('recovery_launcher_failed')
    }
  } catch {
    return launchFailure('recovery_launcher_failed')
  }

  return { status: launched.status, stderr: '', stdout }
}
