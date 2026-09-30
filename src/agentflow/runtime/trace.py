"""Owner-visible, bounded execution traces. Never expose private model reasoning."""
from __future__ import annotations

import base64
import codecs
import json
import logging
import re
import time
import zlib
from datetime import datetime
from uuid import uuid4

from agentflow.common import DomainError, canonical_digest, utc_now

logger = logging.getLogger(__name__)
MAX_CONTENT = 16 * 1024
MAX_ATTEMPT = 64 * 1024 * 1024
KINDS = {'instruction', 'llm_request', 'llm_output', 'tool_call', 'tool_result', 'status', 'error'}
HIDDEN = {'reasoning', 'reasoning_content', 'reasoning_details', 'encrypted_content', 'chain_of_thought'}
SENSITIVE = re.compile(r'(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|passwd|secret|credential|cookie|bootstrap|(?:^|[_-])token(?:$|[_-]))', re.I)
# Start at a complete name boundary. A greedy name without this boundary retries
# at every character of long minified output and makes redaction quadratic.
ASSIGNMENT_KEY = re.compile(r'''(?<![\w.-])(?P<name>[\w.-]+)["']?\s*[:=]\s*''')
URI_CREDENTIAL = re.compile(r'(?<![\w+.-])([A-Za-z][A-Za-z0-9+.-]*://)[^/\s:@]*:[^/\s@]+@')
PRIVATE_KEY = re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)', re.S)


def _sensitive_ranges(text):
    """Yield bounded scans of labeled values, including unfinished quoted values."""
    consumed = 0
    for match in ASSIGNMENT_KEY.finditer(text):
        if match.start() < consumed or not SENSITIVE.search(match['name']):
            continue
        start = match.end()
        if start == len(text):
            yield match.start(), start, start, '[已隐藏]', False
            continue
        quote = text[start] if text[start] in {'"', "'"} else None
        if quote:
            end, closed = start + 1, False
            while end < len(text):
                if text[end] == '\\':
                    end += 2
                    continue
                if text[end] == quote:
                    end += 1
                    closed = True
                    break
                end += 1
            end = min(end, len(text))
            replacement = quote + '[已隐藏]' + quote
        else:
            query = match.start() > 0 and text[match.start() - 1] in '?&'
            delimiters = '&?#\r\n\t ' if query else '\r\n' if 'authorization' in match['name'].lower() else '\r\n\t ,;}]'
            end = start
            while end < len(text) and text[end] not in delimiters:
                end += 1
            closed = end < len(text)
            replacement = '[已隐藏]'
        consumed = end
        yield match.start(), start, end, replacement, closed


def _redact_assignments(text):
    parts, prior = [], 0
    for _, start, end, replacement, _ in _sensitive_ranges(text):
        parts.extend((text[prior:start], replacement))
        prior = end
    parts.append(text[prior:])
    return ''.join(parts)


def _safe_line_cut(value, proposed):
    """Never flush one half of a credential and later expose its continuation."""
    for begin, _, end, _, closed in _sensitive_ranges(value):
        if begin < proposed and (not closed or end > proposed):
            proposed = min(proposed, value.rfind('\n', 0, begin) + 1)
    for match in re.finditer('-----BEGIN ', value):
        # Retain complete private-key blocks in one redaction unit. A partial
        # header can become a recognized private-key header in a later chunk.
        if match.start() < proposed:
            block = PRIVATE_KEY.match(value, match.start())
            if not block or block.end() > proposed or '-----END ' not in block.group():
                proposed = min(proposed, value.rfind('\n', 0, match.start()) + 1)
    return proposed


def public_value(value, secrets=()):
    if isinstance(value, dict):
        if str(value.get('type', '')).startswith('reasoning'):
            return None
        return {key: '[已隐藏]' if SENSITIVE.search(key) else public_value(item, secrets)
                for key, item in value.items() if key not in HIDDEN}
    if isinstance(value, list):
        return [public_value(item, secrets) for item in value
                if not isinstance(item, dict) or not str(item.get('type', '')).startswith('reasoning')]
    if not isinstance(value, str):
        return value
    result = value
    if result.startswith(('data:image/', 'data:audio/')):
        return f'[内嵌媒体，共 {len(result)} 字符]'
    for secret in sorted((s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        result = result.replace(secret, '[已隐藏]')
    result = PRIVATE_KEY.sub('[私钥已隐藏]', result)
    result = re.sub(r'(?i)\bBearer\s+[^\s"\'<>]+', 'Bearer [已隐藏]', result)
    result = re.sub(r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]+', '[已隐藏]', result)
    result = URI_CREDENTIAL.sub(r'\1[已隐藏]@', result)
    return _redact_assignments(result)


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def _chunks(text):
    # UTF-8 bytes bound each event; do not cut a multibyte character.
    raw = text.encode('utf-8')
    offset = 0
    if not raw:
        yield ''
    while offset < len(raw):
        end = min(offset + MAX_CONTENT, len(raw))
        while end < len(raw) and raw[end] & 0xc0 == 0x80:
            end -= 1
        yield raw[offset:end].decode('utf-8')
        offset = end


class ExecutionTrace:
    def __init__(self, store):
        self.store = store

    async def emit(self, attempt_id, kind, title, content='', *, key=None, secrets=(), **metadata):
        """Observability must not alter model accounting or execution outcomes."""
        try:
            if kind not in KINDS:
                raise ValueError('Unknown trace kind')
            clean = _text(public_value(content, secrets))
            parts = list(_chunks(clean))
            safe_title = public_value(str(title), secrets)[:200]
            allowed = {k: v for k, v in metadata.items() if k in {'call_id', 'model', 'status', 'duration_ms'}}
            allowed = public_value(allowed, secrets)
            def append(tx):
                attempt = tx.get('attempt', attempt_id)
                if not attempt:
                    return {'added': 0}
                previous = tx.get('trace_sequence', attempt_id)
                cursor = previous or {'next_seq': 1, 'bytes': 0, 'limited': False}
                if cursor['limited']:
                    return {'added': 0}
                seq, count = cursor['next_seq'], cursor['bytes']
                added, limited = 0, False
                for index, text in enumerate(parts):
                    size = len(text.encode('utf-8'))
                    limited = count + size > MAX_ATTEMPT
                    if limited:
                        text = '本次执行日志已达到 64 MiB 上限，后续正文停止记录；任务状态与正式产物仍会保留。'
                    identity = f'{attempt_id}/{seq:012d}'
                    compressed = base64.b64encode(zlib.compress(text.encode('utf-8'))).decode('ascii')
                    tx.put('execution_trace', identity, {'attempt_id': attempt_id,
                        'run_id': attempt['run_id'], 'work_item_id': attempt['work_item_id'], 'seq': seq,
                        'kind': 'status' if limited else kind,
                        'title': '日志容量提示' if limited else safe_title + (f'（{index + 1}/{len(parts)}）' if len(parts) > 1 else ''),
                        'content_zlib': compressed, 'created_at': utc_now(), 'truncated': limited,
                        'redacted': clean != _text(content), **allowed})
                    seq += 1
                    count += size
                    added += 1
                    if limited:
                        break
                tx.put('trace_sequence', attempt_id, {'next_seq': seq, 'bytes': count, 'limited': limited},
                       previous['revision'] if previous else None)
                return {'added': added}
            await self.store.command('trace.append', key or str(uuid4()),
                {'attempt_id': attempt_id, 'kind': kind, 'content_digest': canonical_digest(clean), 'title': safe_title,
                 'metadata': allowed}, append)
        except Exception:
            logger.warning('Execution trace unavailable; primary execution evidence retained', exc_info=False)

    async def attempts(self, run_id, work_id, *, before=None, limit=20):
        work = await self.store.read('work_item', work_id)
        if not work or work['run_id'] != run_id:
            raise DomainError('not_found', '找不到该任务。', 404)
        if not 1 <= limit <= 50:
            raise DomainError('invalid_cursor', '执行列表分页大小无效。', 422)
        rows = sorted((a for a in await self.store.list('attempt') if a.get('work_item_id') == work_id
                       and a.get('run_id') == run_id), key=lambda a: (a.get('generation', 0), a.get('started_at', ''), a['id']), reverse=True)
        if before is not None:
            ids = [a['id'] for a in rows]
            if before not in ids:
                raise DomainError('invalid_cursor', '执行列表游标无效。', 422)
            rows = rows[ids.index(before) + 1:]
        page = []
        for row in rows[:limit]:
            ended = row.get('finished_at') or row.get('ended_at')
            duration = None
            try:
                duration = int((datetime.fromisoformat(ended or utc_now()) - datetime.fromisoformat(row['started_at'])).total_seconds() * 1000)
            except (ValueError, KeyError, TypeError):
                pass
            page.append({k: row.get(k) for k in ('id', 'generation', 'status', 'started_at')} |
                        {'finished_at': ended, 'duration_ms': duration})
        return {'run_id': run_id, 'work_item_id': work_id, 'current_attempt_id': work.get('attempt_id'),
                'items': page, 'next_before': page[-1]['id'] if len(rows) > limit else None}

    async def page(self, attempt_id, *, after=None, before=None, limit=50):
        attempt = await self.store.read('attempt', attempt_id)
        if not attempt:
            raise DomainError('not_found', '找不到该次执行。', 404)
        if (type(limit) is not int or not 1 <= limit <= 100 or (after is not None and before is not None)
                or any(type(c) is not int or not 0 <= c < 10**12 for c in (after, before) if c is not None)):
            raise DomainError('invalid_cursor', '日志分页参数无效。', 422)
        prefix = attempt_id + '/'
        reverse = after is None
        rows = await self.store.record_page('execution_trace', prefix, limit=limit + 1, reverse=reverse,
            after=prefix + f'{after:012d}' if after is not None else None,
            before=prefix + f'{before:012d}' if before is not None else None)
        sequence = await self.store.read('trace_sequence', attempt_id)
        if ((rows and not sequence) or (sequence and (type(sequence.get('next_seq')) is not int
                or not 1 <= sequence['next_seq'] < 10**12))):
            raise DomainError('trace_corrupt', '日志游标无法核验。', 409)
        items, size = [], 0
        for row in rows[:limit]:
            seq = row.get('seq')
            if (type(seq) is not int or not 1 <= seq < sequence['next_seq']
                    or row['id'] != prefix + f'{seq:012d}' or row.get('attempt_id') != attempt_id
                    or row.get('run_id') != attempt['run_id'] or row.get('work_item_id') != attempt['work_item_id']):
                raise DomainError('trace_corrupt', '日志记录身份无法核验。', 409)
            try:
                encoded = row['content_zlib']
                if not isinstance(encoded, str) or len(encoded) > (MAX_CONTENT + 1024) * 2:
                    raise ValueError('invalid_trace_encoding')
                decoder = zlib.decompressobj()
                content = decoder.decompress(base64.b64decode(encoded, validate=True), MAX_CONTENT + 1)
                decoded = content.decode('utf-8')
            except (ValueError, KeyError, TypeError, zlib.error) as error:
                raise DomainError('trace_corrupt', '日志记录无法核验。', 409) from error
            if len(content) > MAX_CONTENT or not decoder.eof or decoder.unused_data:
                raise DomainError('trace_corrupt', '日志记录无法核验。', 409)
            item = {k: v for k, v in row.items() if k not in {'content_zlib', 'revision'}}
            item['content'] = decoded
            wire_size = len(json.dumps(item, ensure_ascii=False).encode('utf-8'))
            if size + wire_size > 500 * 1024:
                break
            items.append(item)
            size += wire_size
        items.sort(key=lambda row: row['seq'])
        total = sequence['next_seq'] - 1 if sequence else 0
        complete = attempt['status'] not in {'running', 'cancel_requested', 'waiting_execution'}
        if not total and after is None and before is None:
            items = [{'id': attempt_id + '/legacy', 'seq': 0, 'kind': 'status', 'title': '执行记录',
                'content': '该次历史执行未记录实时模型正文。新执行会显示请求、可见结果和工具信息。' if complete
                           else '执行刚开始，正在等待首条日志。',
                'created_at': attempt.get('started_at') or utc_now(), 'status': attempt['status']}]
        first, last = (items[0]['seq'], items[-1]['seq']) if items else (before or total + 1, after or total)
        return {'attempt_id': attempt_id, 'run_id': attempt['run_id'], 'work_item_id': attempt['work_item_id'],
                'items': items, 'next_after': last, 'next_before': first if first > 1 else None,
                'has_more_after': last < total, 'has_more_before': first > 1 and total > 0, 'complete': complete}


class ModelTrace:
    """Extract visible protocol fields; ignore reasoning/encrypted protocol items."""
    def __init__(self, traces, attempt_id, operation_id, model, protocol, secrets):
        self.traces, self.attempt_id, self.operation_id = traces, attempt_id, operation_id
        self.model, self.protocol, self.secrets = model, protocol, secrets
        self.decoder = codecs.getincrementaldecoder('utf-8')('strict')
        self.sse = ''
        self.buffers = {}
        self.received = set()
        self.started = time.monotonic()
        self.last_flush = self.started

    async def emit(self, kind, title, value):
        await self.traces.emit(self.attempt_id, kind, title, value, secrets=self.secrets,
            call_id=self.operation_id, model=self.model)

    async def request(self, body):
        await self.emit('llm_request', '发送给模型的请求', body)
        messages = body.get('messages', body.get('input', []))
        if isinstance(messages, list):
            latest = []
            for item in reversed(messages):
                if not isinstance(item, dict) or not (item.get('role') == 'tool' or item.get('type') in {
                        'function_call_output', 'custom_tool_call_output'}):
                    break
                latest.append(item)
            for item in reversed(latest):
                await self.emit('tool_result', '工具返回', item.get('content', item.get('output', '')))

    def text(self, identity, kind, title, delta):
        if isinstance(delta, str) and delta:
            self.received.add(identity)
            previous = self.buffers.get(identity, (kind, title, ''))
            self.buffers[identity] = (previous[0], previous[1], previous[2] + delta)

    def observe(self, value):
        if not isinstance(value, dict):
            return
        if self.protocol == 'chat_completions':
            for choice in value.get('choices', []):
                message = choice.get('delta') or choice.get('message') or {}
                self.text('message', 'llm_output', '模型返回', message.get('content'))
                self.text('refusal', 'llm_output', '模型返回', message.get('refusal'))
                for index, call in enumerate(message.get('tool_calls') or []):
                    function = call.get('function', {})
                    identity = 'tool:' + str(call.get('index', index))
                    if function.get('name'):
                        self.buffers.setdefault(identity, ('tool_call', '模型请求工具 · ' + function['name'], ''))
                    self.text(identity, 'tool_call', '模型请求工具 · ' + function.get('name', ''), function.get('arguments'))
            return
        kind = value.get('type', '')
        identity = value.get('item_id', 'output')
        if kind == 'response.output_text.delta':
            self.text(identity, 'llm_output', '模型返回', value.get('delta'))
        elif kind in {'response.function_call_arguments.delta', 'response.custom_tool_call_input.delta'}:
            self.text(identity, 'tool_call', '模型请求工具', value.get('delta'))
        elif kind == 'response.output_item.added':
            item = value.get('item', {})
            if item.get('type') in {'function_call', 'custom_tool_call'}:
                self.buffers.setdefault(item.get('id', identity), ('tool_call', '模型请求工具 · ' + str(item.get('name', '')), ''))
        elif (not kind and isinstance(value.get('output'), list)) or kind in {
                'response.completed', 'response.failed', 'response.incomplete'}:
            output = value.get('response', value).get('output', [])
            for item in output:
                if not isinstance(item, dict):
                    continue
                identity = item.get('id', 'message' if item.get('type') == 'message' else 'tool')
                if identity in self.received or 'output' in self.received:
                    continue
                if item.get('type') == 'message':
                    for part in item.get('content', []):
                        if part.get('type') == 'output_text':
                            self.text(identity, 'llm_output', '模型返回', part.get('text'))
                        elif part.get('type') == 'refusal':
                            self.text(identity, 'llm_output', '模型返回', part.get('refusal'))
                elif item.get('type') in {'function_call', 'custom_tool_call'}:
                    self.text(identity, 'tool_call', '模型请求工具 · ' + str(item.get('name', '')),
                              item.get('arguments') or item.get('input'))

    async def feed(self, chunk):
        self.sse += self.decoder.decode(chunk)
        self.sse = self.sse.replace('\r\n', '\n')
        while '\n\n' in self.sse:
            raw, self.sse = self.sse.split('\n\n', 1)
            text = '\n'.join(line[5:].lstrip() for line in raw.split('\n') if line.startswith('data:'))
            if text and text != '[DONE]':
                self.observe(json.loads(text))
        if time.monotonic() - self.last_flush >= .5:
            await self.flush()

    async def flush(self, final=False):
        # Send complete lines only. Partial credentials/JSON/tool arguments stay
        # private until their complete value can be redacted consistently.
        for identity, (kind, title, value) in list(self.buffers.items()):
            cut = len(value) if final else value.rfind('\n') + 1 if kind == 'llm_output' else 0
            if not final and cut:
                cut = _safe_line_cut(value, cut)
                if any(value[:cut].endswith(secret[:length]) for secret in self.secrets
                       for length in range(1, min(len(secret), 128))):
                    cut = 0
            if cut:
                await self.emit(kind, title, value[:cut])
                self.buffers[identity] = (kind, title, value[cut:])
        self.last_flush = time.monotonic()

    async def finish(self, status):
        await self.flush(final=True)
        await self.traces.emit(self.attempt_id, 'status' if status == 'completed' else 'error',
            '模型调用结束' if status == 'completed' else '模型调用未完成', status,
            call_id=self.operation_id, model=self.model, status=status,
            duration_ms=int((time.monotonic() - self.started) * 1000))
