export function sendJson(response, status, value) {
  const body = JSON.stringify(value);
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
  });
  response.end(body);
}

export async function readJson(request, maximumBytes = 64 * 1024) {
  const chunks = [];
  let size = 0;
  // 提前结束读取时保留连接，让错误响应可以完整发送。
  for await (const chunk of request.iterator({destroyOnReturn: false})) {
    size += chunk.length;
    if (size > maximumBytes) break;
    chunks.push(chunk);
  }
  if (size > maximumBytes) {
    // 丢弃剩余请求体，避免占住 keep-alive 连接；不继续缓存超限内容。
    request.resume();
    throw Object.assign(new Error('request_too_large'), {status: 413});
  }
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('object required');
    return value;
  } catch {
    throw Object.assign(new Error('invalid_json'), {status: 400});
  }
}
