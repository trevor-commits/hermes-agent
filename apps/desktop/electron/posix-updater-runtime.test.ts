import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { test, vi } from 'vitest'

import {
  POSIX_UPDATER_RUNTIME_ASSETS,
  publishPosixUpdaterRuntime,
  validatePublishedUpdaterRuntime
} from './posix-updater-runtime'

function fixture() {
  const root = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'hermes-atomic-runtime-'))
  const sourceRoot = path.join(root, 'source')
  const runtimeRoot = path.join(root, 'runtime')

  fs.mkdirSync(sourceRoot, { mode: 0o700 })
  for (const asset of POSIX_UPDATER_RUNTIME_ASSETS) {
    const body = asset.target === 'posix.sh' ? '#!/bin/bash\nexit 0\n' : `${asset.target}\n`
    fs.writeFileSync(path.join(sourceRoot, asset.source), body, { mode: asset.executable ? 0o755 : 0o644 })
  }

  return { root, runtimeRoot, sourceRoot }
}

test('publishes the canonical runtime with an inode and manifest trust anchor', () => {
  const { runtimeRoot, sourceRoot } = fixture()
  const result = publishPosixUpdaterRuntime({
    runtimeRoot,
    sourceRoot,
    transactionId: 'tx-runtime-123'
  })

  const directoryStat = fs.lstatSync(result.directory, { bigint: true })
  const rawManifest = fs.readFileSync(path.join(result.directory, 'manifest.json'))
  const expectedPaths = [...POSIX_UPDATER_RUNTIME_ASSETS]
    .map(asset => asset.target)
    .sort((left, right) => (left < right ? -1 : left > right ? 1 : 0))

  assert.equal(result.directory, path.join(runtimeRoot, 'tx-runtime-123'))
  assert.equal(result.entrypoint, path.join(result.directory, 'posix.sh'))
  assert.equal(fs.existsSync(result.entrypoint), true)
  assert.equal(result.directoryDevice, directoryStat.dev.toString(10))
  assert.equal(result.directoryInode, directoryStat.ino.toString(10))
  assert.equal(result.manifestSha256, createHash('sha256').update(rawManifest).digest('hex'))
  assert.equal(fs.statSync(runtimeRoot).mode & 0o777, 0o700)
  assert.equal(Number(directoryStat.mode & 0o777n), 0o700)
  assert.equal(fs.statSync(result.entrypoint).mode & 0o777, 0o500)
  assert.deepEqual(
    result.manifest.files.map(item => item.path),
    expectedPaths
  )

  for (const item of result.manifest.files) {
    const bytes = fs.readFileSync(path.join(result.directory, item.path))
    assert.equal(item.sha256, createHash('sha256').update(bytes).digest('hex'))
    assert.equal(item.size, bytes.byteLength)
  }

  assert.deepEqual(
    validatePublishedUpdaterRuntime(result.directory, {
      directoryDevice: result.directoryDevice,
      directoryInode: result.directoryInode,
      manifestSha256: result.manifestSha256,
      transactionId: 'tx-runtime-123'
    }),
    result.manifest
  )
})

test('canonical assets are deeply immutable and identity bindings are exact decimal strings', () => {
  const originalTarget = POSIX_UPDATER_RUNTIME_ASSETS[0].target

  assert.throws(() => {
    ;(POSIX_UPDATER_RUNTIME_ASSETS[0] as any).target = 'redefined.sh'
  }, TypeError)
  assert.equal(POSIX_UPDATER_RUNTIME_ASSETS[0].target, originalTarget)

  const { runtimeRoot, sourceRoot } = fixture()
  const result = publishPosixUpdaterRuntime({
    runtimeRoot,
    sourceRoot,
    transactionId: 'tx-bigint-binding'
  })
  const unsafeRoundedIdentity = (BigInt(result.directoryInode) + 2n ** 54n).toString(10)

  assert.match(result.directoryDevice, /^[0-9]+$/)
  assert.match(result.directoryInode, /^[0-9]+$/)
  assert.throws(
    () =>
      validatePublishedUpdaterRuntime(result.directory, {
        directoryDevice: result.directoryDevice,
        directoryInode: unsafeRoundedIdentity,
        manifestSha256: result.manifestSha256,
        transactionId: 'tx-bigint-binding'
      }),
    /trusted binding/i
  )
})

test('first runtime-root creation fsyncs its parent before transaction publication', () => {
  const { runtimeRoot, sourceRoot } = fixture()
  const parent = path.dirname(runtimeRoot)
  const realFsync = fs.fsyncSync
  const events: Array<{ dev: bigint; ino: bigint }> = []

  const spy = vi.spyOn(fs, 'fsyncSync').mockImplementation(descriptor => {
    const observed = fs.fstatSync(descriptor, { bigint: true })
    events.push({ dev: observed.dev, ino: observed.ino })
    return realFsync(descriptor)
  })
  try {
    publishPosixUpdaterRuntime({ runtimeRoot, sourceRoot, transactionId: 'tx-root-fsync' })
  } finally {
    spy.mockRestore()
  }

  const parentStat = fs.lstatSync(parent, { bigint: true })
  assert.equal(
    events.some(event => event.dev === parentStat.dev && event.ino === parentStat.ino),
    true
  )
})

test('rejects a symlinked source asset and removes only its incomplete claim', () => {
  const { root, runtimeRoot, sourceRoot } = fixture()
  const outside = path.join(root, 'outside')

  fs.writeFileSync(outside, 'replacement\n')
  fs.unlinkSync(path.join(sourceRoot, 'atomic_macos.py'))
  fs.symlinkSync(outside, path.join(sourceRoot, 'atomic_macos.py'))

  assert.throws(
    () => publishPosixUpdaterRuntime({ runtimeRoot, sourceRoot, transactionId: 'tx-symlink' }),
    /regular file|symlink/i
  )
  assert.equal(fs.existsSync(path.join(runtimeRoot, 'tx-symlink')), false)
  assert.equal(fs.readFileSync(outside, 'utf8'), 'replacement\n')
})

test('rejects source-root and runtime-root symlink ancestors', () => {
  const { root, runtimeRoot, sourceRoot } = fixture()
  const sourceLink = path.join(root, 'source-link')
  const redirectedRuntime = path.join(root, 'redirected-runtime')
  const runtimeLink = path.join(root, 'runtime-link')

  fs.symlinkSync(sourceRoot, sourceLink)
  fs.mkdirSync(redirectedRuntime, { mode: 0o755 })
  fs.symlinkSync(redirectedRuntime, runtimeLink)

  assert.throws(
    () => publishPosixUpdaterRuntime({ runtimeRoot, sourceRoot: sourceLink, transactionId: 'tx-source-link' }),
    /symlink ancestor/i
  )
  assert.throws(
    () => publishPosixUpdaterRuntime({ runtimeRoot: runtimeLink, sourceRoot, transactionId: 'tx-root-link' }),
    /real directory|symlink ancestor/i
  )
  assert.equal(fs.statSync(redirectedRuntime).mode & 0o777, 0o755)
  assert.deepEqual(fs.readdirSync(redirectedRuntime), [])
})

test('exclusive final-directory claim preserves existing nonempty and empty collisions', () => {
  for (const nonempty of [false, true]) {
    const { runtimeRoot, sourceRoot } = fixture()
    const existing = path.join(runtimeRoot, nonempty ? 'tx-nonempty' : 'tx-empty')

    fs.mkdirSync(existing, { recursive: true, mode: 0o700 })
    if (nonempty) {
      fs.writeFileSync(path.join(existing, 'sentinel'), 'keep me\n')
    }
    const before = fs.lstatSync(existing)

    assert.throws(
      () =>
        publishPosixUpdaterRuntime({
          runtimeRoot,
          sourceRoot,
          transactionId: path.basename(existing)
        }),
      /already exists/i
    )
    const after = fs.lstatSync(existing)
    assert.deepEqual([after.dev, after.ino], [before.dev, before.ino])
    if (nonempty) {
      assert.equal(fs.readFileSync(path.join(existing, 'sentinel'), 'utf8'), 'keep me\n')
    }
  }
})

test('trusted validation rejects member, manifest, file-set, and directory replacement', () => {
  for (const mutation of ['member', 'manifest', 'extra', 'symlink', 'directory'] as const) {
    const { root, runtimeRoot, sourceRoot } = fixture()
    const result = publishPosixUpdaterRuntime({
      runtimeRoot,
      sourceRoot,
      transactionId: `tx-${mutation}`
    })
    const binding = {
      directoryDevice: result.directoryDevice,
      directoryInode: result.directoryInode,
      manifestSha256: result.manifestSha256,
      transactionId: `tx-${mutation}`
    }

    if (mutation === 'member') {
      const member = path.join(result.directory, 'atomic_macos.py')
      fs.chmodSync(member, 0o600)
      fs.appendFileSync(member, 'changed\n')
    } else if (mutation === 'manifest') {
      const manifest = path.join(result.directory, 'manifest.json')
      fs.chmodSync(manifest, 0o600)
      fs.appendFileSync(manifest, '{}\n')
    } else if (mutation === 'extra') {
      fs.writeFileSync(path.join(result.directory, 'extra.py'), 'unexpected\n')
    } else if (mutation === 'symlink') {
      const target = path.join(root, 'replacement.py')
      fs.writeFileSync(target, 'replacement\n')
      fs.unlinkSync(path.join(result.directory, 'atomic_macos.py'))
      fs.symlinkSync(target, path.join(result.directory, 'atomic_macos.py'))
    } else {
      fs.renameSync(result.directory, `${result.directory}-old`)
      fs.mkdirSync(result.directory, { mode: 0o700 })
    }

    assert.throws(
      () => validatePublishedUpdaterRuntime(result.directory, binding),
      /runtime|manifest|regular|symlink|binding/i
    )
  }
})

test('published canonical production modules import from the external directory', () => {
  const runtimeParent = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'hermes-runtime-import-'))
  const runtimeRoot = path.join(runtimeParent, 'runtime')
  const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../scripts/desktop-update')
  const result = publishPosixUpdaterRuntime({
    runtimeRoot,
    sourceRoot,
    transactionId: 'tx-production-import'
  })
  const probe = spawnSync(
    '/usr/bin/python3',
    [
      '-c',
      [
        'import importlib.util, pathlib, sys',
        'path = pathlib.Path(sys.argv[1])',
        'spec = importlib.util.spec_from_file_location("hermes_atomic_runtime", path)',
        'module = importlib.util.module_from_spec(spec)',
        'sys.modules[spec.name] = module',
        'spec.loader.exec_module(module)',
        'assert callable(module.recover_exchange)'
      ].join('; '),
      result.entrypoint.replace(/posix\.sh$/, 'atomic_macos.py')
    ],
    { encoding: 'utf8' }
  )

  assert.equal(probe.status, 0, probe.stderr || probe.stdout)
})
