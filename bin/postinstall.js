#!/usr/bin/env node
// Friendly post-install hint. Detects whether the package was installed globally
// and prints a helpful message either way. Never fails the install.

const isGlobal =
  process.env.npm_config_global === 'true' ||
  (process.env.npm_config_prefix && __dirname.startsWith(process.env.npm_config_prefix));

const cyan = (s) => `\x1b[36m${s}\x1b[0m`;
const yellow = (s) => `\x1b[33m${s}\x1b[0m`;
const bold = (s) => `\x1b[1m${s}\x1b[0m`;

if (isGlobal) {
  console.log();
  console.log(bold('  Talentia Docker Viewer installed globally.'));
  console.log('  Run ' + cyan('taldocker') + ' from any directory to start.');
  console.log('  Run ' + cyan('taldocker --help') + ' to see options.');
  console.log();
} else {
  console.log();
  console.log(yellow('  Heads up: Talentia Docker Viewer is a CLI tool.'));
  console.log('  You installed it locally, so the ' + cyan('taldocker') + ' command');
  console.log('  is NOT available in your PATH.');
  console.log();
  console.log('  To use it globally, install with the ' + bold('-g') + ' flag:');
  console.log('    ' + cyan('npm install -g @talentiaoss/talentia-docker-viewer'));
  console.log();
  console.log('  Or run it once via npx:');
  console.log('    ' + cyan('npx @talentiaoss/talentia-docker-viewer'));
  console.log();
}
