import assert from 'node:assert/strict'
import { exec as execCallback, spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { promisify } from 'node:util'

import { test } from 'vitest'

import {
  fingerprintToken,
  LOCKFILE_SCHEMA_VERSION,
  pidIsOurDashboard,
  PROTOCOL_VERSION,
  remoteProcessCreationTime,
  spawnLogPath,
  spawnTokenPath,
  terminateOwnedDashboardForUpdate
} from './remote-lifecycle'

const exec = promisify(execCallback)
const ownershipId = '0123456789abcdef0123456789abcdef'
const nonce = '0123456789abcdef'

test.skipIf(process.platform === 'win32')(
  'remote ownership preserves quoted paths, empty arguments and the argv/environment boundary',
  async () => {
    const temp = await mkdtemp(path.join(os.tmpdir(), "hermes argv ' slash\\ "))
    const env = { ...process.env, HOME: temp, HERMES_HOME: temp }
    const token = path.join(temp, spawnTokenPath(ownershipId, nonce).replace(/^~\//, ''))
    const launcher = path.join(temp, "install dir/hermes' launcher\\")
    const python = (await exec('command -v python3', { shell: '/bin/bash', env })).stdout.trim()
    const children: ReturnType<typeof spawn>[] = []
    const exits: Promise<unknown>[] = []
    const ssh = { exec: async (command: string): Promise<string> => (await exec(command, { shell: '/bin/bash', env })).stdout }

    const start = async (ownerValue: string, extraArgs: string[] = [], extraEnv = {}): Promise<ReturnType<typeof spawn>> => {
      const child = spawn(
        python,
        [
          '-c', 'import time; time.sleep(30)', '--profile', 'ops', 'serve', '--isolated',
          '--ssh-session-token-file', token, '--ssh-owner-nonce', ownerValue, ...extraArgs
        ],
        { env: { ...env, ...extraEnv }, stdio: 'ignore' }
      )

      children.push(child)
      exits.push(once(child, 'exit'))
      await once(child, 'spawn')

      return child
    }

    try {
      const child = await start(nonce)
      assert.equal(await pidIsOurDashboard(ssh, child.pid, nonce, launcher, temp, ownershipId, 'ops'), true)
      assert.equal(await pidIsOurDashboard(ssh, child.pid, 'fedcba9876543210', launcher, temp, ownershipId, 'ops'), false)
      assert.equal(await pidIsOurDashboard(ssh, child.pid, nonce, launcher, temp, 'fedcba9876543210fedcba9876543210', 'ops'), false)
      assert.equal(await pidIsOurDashboard(ssh, child.pid, nonce, launcher, temp, ownershipId, 'foreign'), false)

      if (process.platform === 'darwin') {
        const lock = {
          schemaVersion: LOCKFILE_SCHEMA_VERSION,
          protocolVersion: PROTOCOL_VERSION,
          ownershipId,
          spawnNonce: nonce,
          pid: child.pid,
          port: 40000,
          profile: 'ops',
          hermesPath: launcher,
          hermesHome: temp,
          logPath: spawnLogPath(ownershipId, nonce),
          tokenFingerprint: fingerprintToken('synthetic-token'),
          startedAt: new Date().toISOString(),
          creationTime: await remoteProcessCreationTime(ssh, child.pid)
        }

        const lockedSsh = {
          exec: async (command: string): Promise<string> =>
            /cat .*lock\.json/.test(command) ? JSON.stringify(lock) : ssh.exec(command)
        }

        await assert.rejects(terminateOwnedDashboardForUpdate(lockedSsh, lock), /Darwin cannot atomically bind a signal/)
        assert.equal(child.exitCode, null, 'Darwin must still refuse an unbound signal against the exact owned process')
      }

      // Empty argv values must not disappear and make the following value look like the nonce.
      const emptyOwner = await start('', [nonce], { SPOOFED_ARGUMENTS: `--ssh-owner-nonce ${nonce}` })
      assert.equal(await pidIsOurDashboard(ssh, emptyOwner.pid, nonce, launcher, temp, ownershipId, 'ops'), false)
    } finally {
      for (const child of children) {
        if (child.exitCode === null && child.signalCode === null) {child.kill('SIGTERM')}
      }

      await Promise.all(exits)
      await rm(temp, { recursive: true, force: true })
    }
  }
)
