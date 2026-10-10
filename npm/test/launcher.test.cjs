'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { findPython, resolveRuntime } = require('../bin/titan.cjs');

test('native Windows runtime needs no Python and preserves paths with spaces', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'titan npm '));
  try {
    const dir = path.join(root, 'native', 'win32-x64');
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'titan.exe'), 'test fixture');
    assert.deepEqual(resolveRuntime(root, { platform: 'win32', arch: 'x64', run: () => { throw Error('unexpected Python'); } }),
      { command: path.join(dir, 'titan.exe'), prefix: [] });
  } finally { fs.rmSync(root, { recursive: true }); }
});
test('Python override is one executable argument, never a shell command', () => {
  const executable = '/a path/python';
  assert.equal(findPython({ TITAN_PYTHON: executable }, 'linux', (command, args, options) => {
    assert.equal(command, executable);
    assert.equal(args[0], '-c');
    assert.equal(options.env.TITAN_PYTHON, executable);
    assert.equal(options.shell, undefined);
    return { status: 0, stdout: executable + '\n' };
  }), executable);
});
test('missing Python and unsupported operating systems fail clearly', () => {
  assert.throws(() => findPython({}, 'linux', () => ({ status: 1, stdout: '' })), /Python 3.11/);
  assert.throws(() => resolveRuntime('.', { platform: 'darwin' }), /Windows and Linux/);
});
test('incomplete distributions fail before Python installation', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'titan-incomplete-'));
  try { assert.throws(() => resolveRuntime(root), /incomplete/); }
  finally { fs.rmSync(root, { recursive: true }); }
});
