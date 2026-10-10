#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');

function findPython(env = process.env, platform = process.platform, run = spawnSync) {
  const candidates = env.TITAN_PYTHON
    ? [[env.TITAN_PYTHON, []]]
    : platform === 'win32'
      ? [['py', ['-3']], ['python', []], ['python3', []]]
      : [['python3', []], ['python', []]];
  for (const [command, prefix] of candidates) {
    const result = run(command, [...prefix, '-c',
      'import sys; assert sys.version_info >= (3,11); print(sys.executable)'],
    { encoding: 'utf8', timeout: 10000, windowsHide: true, env });
    if (!result.error && result.status === 0 && result.stdout.trim()) {
      return result.stdout.trim();
    }
  }
  throw new Error('Titan needs Python 3.11 or newer on this platform. Install Python with venv support, or set TITAN_PYTHON to its executable.');
}

function resolveRuntime(root, options = {}) {
  const platform = options.platform || process.platform;
  const arch = options.arch || process.arch;
  const env = options.env || process.env;
  const run = options.run || spawnSync;
  if (!['win32', 'linux'].includes(platform)) {
    throw new Error('Titan supports Windows and Linux.');
  }
  const executable = path.join(root, 'native', `${platform}-${arch}`,
    platform === 'win32' ? 'titan.exe' : 'titan');
  if (fs.existsSync(executable)) return { command: executable, prefix: [] };
  const wheelDir = path.join(root, 'wheel');
  const wheels = fs.existsSync(wheelDir)
    ? fs.readdirSync(wheelDir).filter(name => name.endsWith('.whl')) : [];
  if (wheels.length !== 1) throw new Error('Titan package is incomplete: expected one Python wheel. Reinstall the package.');
  const python = findPython(env, platform, run);
  const result = run(python, [path.join(root, 'bootstrap.py'), '--wheel', path.join(wheelDir, wheels[0])],
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'], windowsHide: true, env });
  if (result.error || result.status !== 0 || !result.stdout.trim()) {
    throw new Error('Titan could not prepare its private Python environment. See the installation message above; the next launch can retry.');
  }
  const runtime = result.stdout.trim();
  if (!fs.existsSync(runtime)) throw new Error('Titan runtime is missing. Reinstall the package.');
  return { command: runtime, prefix: ['-m', 'titan'] };
}

function main() {
  let runtime;
  try { runtime = resolveRuntime(path.resolve(__dirname, '..')); }
  catch (error) { console.error(`Titan: ${error.message}`); process.exitCode = 1; return; }
  const child = spawn(runtime.command, [...runtime.prefix, ...process.argv.slice(2)],
    { stdio: 'inherit', shell: false, windowsHide: false });
  // Both receive terminal Ctrl+C. Keep the parent alive until Titan closes.
  const interrupt = () => {};
  const terminate = () => child.kill('SIGTERM');
  process.on('SIGINT', interrupt);
  process.on('SIGTERM', terminate);
  child.on('error', error => { console.error(`Titan: ${error.message}`); process.exitCode = 1; });
  child.on('close', (code, signal) => {
    process.removeListener('SIGINT', interrupt);
    process.removeListener('SIGTERM', terminate);
    process.exitCode = code === null ? (signal === 'SIGINT' ? 130 : 143) : code;
  });
}

module.exports = { findPython, resolveRuntime };
if (require.main === module) main();
