import { createHash } from 'node:crypto'
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

function readVerifiedRegularFile(file: string, mode?: number): Buffer {
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

    return fs.readFileSync(descriptor)
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

export function validatePublishedUpdaterRuntime(directory: string, binding: PublishedRuntimeBinding): RuntimeManifest {
  const root = path.resolve(directory)
  const assets = canonicalAssets()

  assertNoSymlinkAncestors(root)
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

  const rawManifest = readVerifiedRegularFile(path.join(root, 'manifest.json'), 0o400)

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

  const expectedFiles = [...assets].sort((left, right) =>
    left.target < right.target ? -1 : left.target > right.target ? 1 : 0
  )

  if (manifest.files.length !== expectedFiles.length) {
    throw new Error('published updater runtime manifest file count is invalid')
  }

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
  }

  return manifest
}
