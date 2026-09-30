"""Strict Codex final JSON with narrowly recognized, unambiguous wrappers."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator

from agentflow.runtime.coding_receipt import output_schema as coding_schema

MAX_JSON_WRAPPER_CANDIDATES = 64
_CODING_DSML_CLOSE = '</｜｜DSML｜｜parameter>\n</｜｜DSML｜｜invoke>\n</｜｜DSML｜｜tool_calls>'


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_json_key')
        value[key] = item
    return value


def _constant(_value):
    raise ValueError('nonfinite_json_value')


def parse_codex_final(raw: bytes, schema: dict | None):
    """Never invent fields or select between objects, including invalid examples.

    A wrapped object must start on its own final block, consume the complete
    suffix, and be the only JSON object in the response. JSON-looking truncated
    prefixes are rejected, rather than salvaging an inner object from them.
    """
    text = raw.decode('utf-8').strip()
    decoder = json.JSONDecoder(object_pairs_hook=_object, parse_constant=_constant)
    normalized = False
    dsml_wrapped = schema == coding_schema() and text.endswith(_CODING_DSML_CLOSE)
    if dsml_wrapped:
        # DeepSeek can leak this exact closing sequence after its coding receipt.
        # Reuse the same strict, single-final-object checks for any prose prefix;
        # removing the suffix does not permit extra objects or malformed JSON.
        text, normalized = text[:-len(_CODING_DSML_CLOSE)].rstrip(), True
    fence = re.fullmatch(r'([\s\S]*?)^```(?:json)?[ \t]*\r?\n([\s\S]*)\r?\n```', text, re.MULTILINE)
    if fence:
        # Keep the prose in the candidate scan: a second object, truncated
        # prefix or earlier code block must still make the result ambiguous.
        if dsml_wrapped or '```' in fence.group(1):
            raise ValueError('final_json_has_ambiguous_wrapper')
        try:
            fenced_result = decoder.decode(fence.group(2).strip())
        except RecursionError:
            raise ValueError('json_nesting_limit') from None
        if not isinstance(fenced_result, dict):
            raise ValueError('final_json_has_ambiguous_wrapper')
        text = (fence.group(1) + fence.group(2)).strip()
        normalized = True
    try:
        result = decoder.decode(text)
    except RecursionError:
        raise ValueError('json_nesting_limit') from None
    except json.JSONDecodeError:
        found = []
        position = 0
        candidates = 0
        while position < len(text):
            starts = [index for token in ('{', '[') if (index := text.find(token, position)) >= 0]
            if not starts:
                break
            candidates += 1
            if candidates > MAX_JSON_WRAPPER_CANDIDATES:
                raise ValueError('json_wrapper_candidate_limit') from None
            start = min(starts)
            try:
                value, end = decoder.raw_decode(text, start)
            except RecursionError:
                raise ValueError('json_nesting_limit') from None
            except json.JSONDecodeError:
                remainder = text[start + 1:].lstrip()
                line_prefix = text[:start].rsplit('\n', 1)[-1]
                array_prefix = text[start] == '[' and remainder.startswith(tuple('"{[0123456789-]tfn'))
                if not remainder or not line_prefix.strip() or remainder.startswith(('"', '}')) or array_prefix:
                    raise ValueError('incomplete_or_invalid_json_object') from None
                position = start + 1
                continue
            found.append((start, end, value))
            position = end
        if len(found) != 1:
            raise ValueError('ambiguous_or_missing_final_json') from None
        start, end, result = found[0]
        prefix = text[:start]
        if (not isinstance(result, dict) or text[end:].strip() or not prefix.strip()
                or prefix.rsplit('\n', 1)[-1].strip() or '```' in prefix):
            raise ValueError('final_json_has_ambiguous_wrapper') from None
        normalized = True
    if (schema == coding_schema() and isinstance(result, dict)
            and result.get('type') == 'object' and set(result) == {'type', 'summary', 'status', 'next_action'}):
        # A literal schema annotation has no task meaning. Only this exact
        # coding receipt wrapper is stripped; unknown fields remain errors.
        result = {name: value for name, value in result.items() if name != 'type'}
        normalized = True
    if schema is not None:
        try:
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(result)
        except RecursionError:
            raise ValueError('json_nesting_limit') from None
    return result, normalized


def write_normalized_final(directory: Path, result) -> Path:
    """Create a separate private machine artifact without replacing raw evidence."""
    try:
        content = (json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')
    except RecursionError:
        raise ValueError('json_nesting_limit') from None
    name = 'codex_final.normalized.json'
    temporary = '.codex-normalized-' + uuid4().hex
    root = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600, dir_fd=root)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, name, src_dir_fd=root, dst_dir_fd=root, follow_symlinks=False)
        except FileExistsError:
            fd = os.open(name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), dir_fd=root)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_size != len(content) or stream.read(len(content) + 1) != content):
                    raise ValueError('normalized_final_conflict')
        os.unlink(temporary, dir_fd=root)
        if os.name != 'nt':
            os.fsync(root)
    finally:
        try:
            os.unlink(temporary, dir_fd=root)
        except FileNotFoundError:
            pass
        os.close(root)
    return directory / name
