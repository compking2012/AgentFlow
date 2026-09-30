"""Real HTTP transport regression for the shared Node request-body helper."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / 'src/agentflow/resources/web_api_starter/src/http.mjs'


@pytest.mark.parametrize('declared', [True, False], ids=['content-length', 'chunked'])
@pytest.mark.parametrize('pooled', [True, False], ids=['keep-alive', 'close'])
@pytest.mark.parametrize('limit', [1024, 64 * 1024], ids=['1KiB', '64KiB'])
def test_oversize_body_returns_json_413_and_preserves_normal_requests(tmp_path, declared, pooled, limit):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node runtime required')
    script = tmp_path / 'http-limit.mjs'
    script.write_text('''
import http from 'node:http';
import assert from 'node:assert/strict';
import {setTimeout as delay} from 'node:timers/promises';
import {readJson, sendJson} from MODULE_URL;
const declared = DECLARED;
const limit = BODY_LIMIT;
const pooled = POOLED;
const server = http.createServer(async (request, response) => {
  try { sendJson(response, 200, await readJson(request, limit)); }
  catch (error) { sendJson(response, error.status || 500, {error: error.message}); }
});
await new Promise((resolve, reject) => {server.once('error', reject); server.listen(0, '127.0.0.1', resolve);});
const port = server.address().port;
const agent = new http.Agent({keepAlive: true});
async function request(chunks, {length = false, pooled = false, paced = false} = {}) {
  return await new Promise((resolve, reject) => {
    let replied = false;
    const headers = {'content-type': 'application/json'};
    if (length) headers['content-length'] = chunks.reduce((total, chunk) => total + Buffer.byteLength(chunk), 0);
    const req = http.request({host: '127.0.0.1', port, method: 'POST', headers, agent: pooled ? agent : false}, (res) => {
      replied = true;
      const socket = res.socket;
      let body = '';
      res.setEncoding('utf8');
      res.on('data', chunk => body += chunk);
      res.on('error', reject);
      res.on('end', () => resolve({status: res.statusCode, headers: res.headers, body: JSON.parse(body), socket}));
    });
    req.on('error', error => {if (!replied) reject(error);});
    (async () => {
      for (const chunk of chunks) {
        if (replied || req.destroyed) break;
        req.write(chunk);
        if (paced) await delay(30);
      }
      if (!req.destroyed) req.end();
    })().catch(reject);
  });
}
try {
  // The body is deliberately still arriving when the limit is exceeded.
  const rejected = await request(['{"value":"' + 'x'.repeat(limit + 20), 'tail"}'], {length: declared, paced: true, pooled});
  assert.equal(rejected.status, 413);
  assert.deepEqual(rejected.body, {error: 'request_too_large'});
  // A rejected oversized upload must not poison the server or disable ordinary keep-alive.
  const first = await request(['{"ok":1}'], {length: true, pooled: true});
  const second = await request(['{"ok":2}'], {length: true, pooled: true});
  assert.equal(first.status, 200);
  assert.equal(second.status, 200);
  assert.deepEqual(second.body, {ok: 2});
  assert.equal(first.socket, second.socket);
  const boundary = await request(['{"x":"' + 'a'.repeat(limit - 8) + '"}'], {length: true});
  assert.equal(boundary.status, 200);
  for (const invalid of ['[]', 'null', '1', '{']) {
    const response = await request([invalid], {length: true});
    assert.equal(response.status, 400);
    assert.deepEqual(response.body, {error: 'invalid_json'});
  }
} finally {
  agent.destroy();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
'''.replace('MODULE_URL', json.dumps(MODULE.as_uri())).replace('DECLARED', str(declared).lower())
        .replace('POOLED', str(pooled).lower()).replace('BODY_LIMIT', str(limit)))
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('chunked', [True, False], ids=['chunked', 'content-length'])
def test_large_rejected_upload_does_not_stall_next_pooled_request(tmp_path, chunked):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node runtime required')
    script = tmp_path / 'http-pool.mjs'
    script.write_text('''
import http from 'node:http';
import assert from 'node:assert/strict';
import {readJson, sendJson} from MODULE_URL;
const server = http.createServer(async (request, response) => {
  try { sendJson(response, 200, await readJson(request)); }
  catch (error) { sendJson(response, error.status || 500, {error: error.message}); }
});
await new Promise((resolve, reject) => {
  server.once('error', reject);
  server.listen(0, '127.0.0.1', resolve);
});
const agent = new http.Agent({keepAlive: true, maxSockets: 1});
async function post(body, chunked) {
  return await new Promise((resolve, reject) => {
    const request = http.request({host: '127.0.0.1', port: server.address().port,
      method: 'POST', agent, headers: chunked ? {} : {'content-length': Buffer.byteLength(body)}}, response => {
      let text = '';
      response.setEncoding('utf8');
      response.on('data', chunk => text += chunk);
      response.on('error', reject);
      response.on('end', () => resolve({status: response.statusCode, body: JSON.parse(text)}));
    });
    request.setTimeout(2000, () => request.destroy(new Error('next request stalled')));
    request.on('error', reject);
    request.end(body);
  });
}
try {
  const rejected = await post('x'.repeat(256 * 1024), CHUNKED);
  assert.deepEqual(rejected, {status: 413, body: {error: 'request_too_large'}});
  const next = await post('{"ok":true}', false);
  assert.deepEqual(next, {status: 200, body: {ok: true}});
} finally {
  agent.destroy();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
'''.replace('MODULE_URL', json.dumps(MODULE.as_uri())).replace('CHUNKED', str(chunked).lower()))
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
