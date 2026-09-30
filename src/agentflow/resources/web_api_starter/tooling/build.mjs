import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';

const root = process.cwd();
function checkedFiles(directory) {
  const files = [];
  for (const item of fs.readdirSync(directory, {withFileTypes: true})) {
    const file = path.join(directory, item.name);
    if (item.isSymbolicLink()) throw new Error(`Source links cannot enter frozen output: ${file}`);
    if (item.isDirectory()) files.push(...checkedFiles(file));
    else if (item.isFile()) files.push(file);
    else throw new Error(`Unsupported source entry: ${file}`);
  }
  return files;
}
for (const name of ['src', 'public', 'tests']) {
  const directory = path.join(root, name);
  if (fs.lstatSync(directory).isSymbolicLink()) throw new Error(`Linked input directory: ${name}`);
  for (const file of checkedFiles(directory)) {
    if (!file.endsWith('.mjs')) continue;
    const result = spawnSync(process.execPath, ['--check', file], {stdio: 'inherit'});
    if (result.status !== 0) process.exit(result.status || 1);
  }
}
const modules = path.join(root, 'node_modules');
const lock = JSON.parse(fs.readFileSync(path.join(root, 'package-lock.json')));
const expected = lock.packages['node_modules/@playwright/test'].version;
const installed = JSON.parse(fs.readFileSync(path.join(modules, '@playwright/test/package.json'))).version;
if (installed !== expected) throw new Error('Installed Playwright differs from package-lock.json; run npm ci');
if (fs.lstatSync(modules).isSymbolicLink()) throw new Error('node_modules must be a real installed directory');

const build = path.join(root, 'build');
if (fs.existsSync(build) && fs.lstatSync(build).isSymbolicLink()) throw new Error('Linked build directory is not allowed');
fs.rmSync(build, {recursive: true, force: true});
fs.mkdirSync(path.join(build, 'product'), {recursive: true});
fs.cpSync(path.join(root, 'src'), path.join(build, 'product'), {recursive: true});
fs.cpSync(path.join(root, 'public'), path.join(build, 'product/public'), {recursive: true});
fs.cpSync(path.join(root, 'tests'), path.join(build, 'tests'), {recursive: true});
fs.cpSync(modules, path.join(build, 'tests/node_modules'), {recursive: true, dereference: true, filter: file => {
  if (file.split(path.sep).includes('.bin')) return false;
  const actual = fs.realpathSync(file);
  if (actual !== modules && !actual.startsWith(modules + path.sep)) throw new Error('A dependency link escapes node_modules');
  return true;
}});
fs.writeFileSync(path.join(build, 'product/package.json'), JSON.stringify({private: true, type: 'module',
  engines: {node: '>=22.13.0'}, scripts: {start: 'node server.mjs'}}, null, 2) + '\n');
fs.writeFileSync(path.join(build, 'tests/package.json'), JSON.stringify({private: true, type: 'module'}, null, 2) + '\n');
fs.mkdirSync(path.join(root, 'reports'), {recursive: true});
console.log(JSON.stringify({product: 'build/product', tests: 'build/tests', business_verified: false}));
