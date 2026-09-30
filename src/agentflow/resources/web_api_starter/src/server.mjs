import http from 'node:http';
import {readFile, realpath, stat, mkdir, rename, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {sendJson} from './http.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const contentTypes = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon'};

export function createApplicationServer() {
  const publicRoot = path.join(here, 'public');
  return http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url || '/', 'http://127.0.0.1');
      if (url.pathname === '/health' && request.method === 'GET') {
        return sendJson(response, 200, {status: 'ok'});
      }
      // The implementation Agent replaces this branch with goal-specific APIs.
      if (url.pathname === '/api' || url.pathname.startsWith('/api/')) {
        return sendJson(response, 501, {error: 'business_not_implemented'});
      }
      if (!['GET', 'HEAD'].includes(request.method)) {
        return sendJson(response, 405, {error: 'method_not_allowed'});
      }
      let decoded;
      try { decoded = decodeURIComponent(url.pathname); }
      catch { return sendJson(response, 400, {error: 'invalid_path'}); }
      const requested = path.resolve(publicRoot, '.' + decoded);
      if (requested !== publicRoot && !requested.startsWith(publicRoot + path.sep)) {
        return sendJson(response, 403, {error: 'forbidden'});
      }
      let file = requested;
      try { if ((await stat(file)).isDirectory()) file = path.join(file, 'index.html'); }
      catch { if (!path.extname(decoded)) file = path.join(publicRoot, 'index.html'); }
      const actual = await realpath(file);
      if (!actual.startsWith(publicRoot + path.sep)) return sendJson(response, 403, {error: 'forbidden'});
      const content = await readFile(actual);
      response.writeHead(200, {'content-type': contentTypes[path.extname(actual)] || 'application/octet-stream',
        'content-length': content.length, 'cache-control': 'no-store', 'x-content-type-options': 'nosniff'});
      response.end(request.method === 'HEAD' ? undefined : content);
    } catch (error) {
      sendJson(response, error.code === 'ENOENT' ? 404 : error.status || 500,
        {error: error.code === 'ENOENT' ? 'not_found' : 'request_failed'});
    }
  });
}

export async function startApplication() {
  const host = process.env.HOST || '127.0.0.1';
  const port = Number(process.env.PORT ?? '3000');
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('PORT must be an integer from 0 to 65535');
  const server = createApplicationServer();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, host, resolve);
  });
  const address = server.address();
  const authority = host.includes(':') ? `[${host}]` : host;
  const ready = {event: 'product_ready', pid: process.pid, host, port: address.port,
    url: `http://${authority}:${address.port}`};
  if (process.env.AGENTFLOW_READY_FILE) {
    const destination = path.resolve(process.env.AGENTFLOW_READY_FILE);
    await mkdir(path.dirname(destination), {recursive: true});
    const temporary = destination + `.${process.pid}.tmp`;
    await writeFile(temporary, JSON.stringify(ready), {mode: 0o600});
    await rename(temporary, destination);
  }
  console.log(JSON.stringify(ready));
  let stopping = false;
  const stop = () => {
    if (stopping) return;
    stopping = true;
    server.close(() => process.exit(0));
    server.closeIdleConnections();
    setTimeout(() => server.closeAllConnections(), 1500).unref();
  };
  process.once('SIGTERM', stop);
  process.once('SIGINT', stop);
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  await startApplication();
}
