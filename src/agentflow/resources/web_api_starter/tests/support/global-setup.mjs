import {spawn} from 'node:child_process';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const here = path.dirname(fileURLToPath(import.meta.url));

export default async function startFrozenProduct() {
  const directory = await mkdtemp(path.join(tmpdir(), 'agentflow-product-test-'));
  const readyPath = path.join(directory, 'ready.json');
  const assigned = process.env.AGENTFLOW_TEST_PORT || process.env.AGENTFLOW_WEB_PORT;
  const port = assigned === undefined ? 0 : Number(assigned);
  if (!Number.isInteger(port) || port < 0 || port > 65535 || (assigned !== undefined && port === 0)) {
    await rm(directory, {recursive: true, force: true});
    throw new Error('The executor-assigned test port is invalid');
  }
  const inherited = Object.fromEntries(['PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL']
    .filter(name => process.env[name] !== undefined).map(name => [name, process.env[name]]));
  const child = spawn(process.execPath, [path.resolve(here, '../../product/server.mjs')], {
    cwd: directory, env: {...inherited, HOST: '127.0.0.1', PORT: String(port),
      AGENTFLOW_DATA_DIR: path.join(directory, 'data'), AGENTFLOW_READY_FILE: readyPath},
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = ''; let startupError;
  child.on('error', error => { startupError = error; });
  child.stdout.on('data', data => { output = (output + data).slice(-8192); });
  child.stderr.on('data', data => { output = (output + data).slice(-8192); });
  async function cleanup() {
    if (child.exitCode === null && child.signalCode === null) {
      const exited = new Promise(resolve => child.once('exit', resolve));
      child.kill('SIGTERM');
      await Promise.race([exited, delay(2500)]);
      if (child.exitCode === null && child.signalCode === null) { child.kill('SIGKILL'); await exited; }
    }
    await rm(directory, {recursive: true, force: true});
  }
  try {
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
      if (startupError) throw startupError;
      if (child.exitCode !== null || child.signalCode !== null) throw new Error(`Frozen product exited before readiness: ${output}`);
      try {
        const ready = JSON.parse(await readFile(readyPath, 'utf8'));
        if (ready.pid !== child.pid || ready.host !== '127.0.0.1' || !Number.isInteger(ready.port) || ready.port < 1
            || (port !== 0 && ready.port !== port)) {
          throw new Error('Product readiness identity is invalid');
        }
        const url = `http://127.0.0.1:${ready.port}`;
        const response = await fetch(url + '/health', {signal: AbortSignal.timeout(2000)});
        if (!response.ok) throw new Error('Product health check failed');
        process.env.AGENTFLOW_TEST_BASE_URL = url;
        process.env.AGENTFLOW_TEST_DATA_DIR = path.join(directory, 'data');
        return cleanup;
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
      await delay(25);
    }
    throw new Error(`Frozen product did not become ready: ${output}`);
  } catch (error) {
    await cleanup();
    throw error;
  }
}
