#!/usr/bin/env node
const { spawn } = require('child_process');
const path = require('path');

const script = path.join(__dirname, '..', 'taldocker.py');
const candidates = process.platform === 'win32'
  ? ['python', 'py', 'python3']
  : ['python3', 'python'];

function tryRun(i) {
  if (i >= candidates.length) {
    console.error('[taldocker] Python 3.8+ not found in PATH. Install Python and retry.');
    process.exit(127);
  }
  const child = spawn(candidates[i], [script, ...process.argv.slice(2)], { stdio: 'inherit' });
  child.on('error', (err) => {
    if (err.code === 'ENOENT') return tryRun(i + 1);
    console.error(`[taldocker] Failed to launch ${candidates[i]}: ${err.message}`);
    process.exit(1);
  });
  child.on('exit', (code, signal) => {
    if (signal) process.kill(process.pid, signal);
    else process.exit(code ?? 0);
  });
}

tryRun(0);
