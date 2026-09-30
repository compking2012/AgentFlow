from __future__ import annotations

import copy
import hashlib
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.domain.planning_contract import validate_parallel_work
from agentflow.runtime.contracts import TaskEnvelope
from agentflow.runtime.launcher import atomic_json

from .output_builder import CHECKPOINT, NAMESPACE, ResultBuilderStore, _result_size, _serialized
from .text_pages import SourceReads, text_page


class ToolBroker:
    """No eval/terminal/tool spawning. Reads code; writes only staged documentation."""

    DOCUMENT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".mmd"}
    EXCLUDED = {".git", ".venv", "node_modules", ".codex", ".openhands", "auth.json"}

    def __init__(self, task: TaskEnvelope):
        self.task = task
        self.root = task.workspace.resolve(strict=True)
        self.documents = task.artifact_dir.resolve()
        self.documents.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.calls = 0
        self.lock = threading.Lock()
        self.finished_result: dict | None = None
        self.planning_failure = None
        self.rejected_planning_result = None
        self.artifacts: list[str] = []
        self.events: list[dict] = []
        self.stopped = threading.Event()
        self.output = ResultBuilderStore(task)
        self.source_reads = SourceReads(self.output)

    def consume(self, operation: str) -> None:
        with self.lock:
            if self.stopped.is_set():
                raise DomainError("cancelled", "Task is stopping")
            if self.calls >= self.task.max_tool_calls:
                raise DomainError("tool_limit_exceeded", "Task tool quota exhausted", 429)
            self.calls += 1
            self.events.append({"sequence": self.calls, "operation": operation, "at": utc_now()})

    def _path(self, root: Path, relative: str, *, exists: bool = False) -> Path:
        value = Path(relative)
        if value.is_absolute() or ".." in value.parts or "\x00" in relative:
            raise DomainError("forbidden_path", "Expected a bounded relative path", 403)
        reserved = {NAMESPACE, CHECKPOINT, 'openhands_final.json', 'role_result.json', 'role_error.json',
                    'tool_audit.json', 'openhands_events.jsonl'}
        if root == self.documents and any(part.casefold() in reserved for part in value.parts):
            raise DomainError('forbidden_path', 'Runtime results and staged-output metadata are not writable documents', 403)
        if any(part in self.EXCLUDED or (part.startswith(".env") and part not in {".env.example", ".env.sample"}) for part in value.parts):
            raise DomainError("forbidden_path", "Sensitive/runtime paths are not available to this role", 403)
        candidate = root / value
        current = root
        for part in value.parts:
            current = current / part
            if current.is_symlink():
                raise DomainError("forbidden_path", "Symbolic links are not accepted for role file access", 403)
        resolved = candidate.resolve(strict=exists)
        if not resolved.is_relative_to(root) or any(
            resolved == p.resolve() or resolved.is_relative_to(p.resolve()) for p in self.task.protected_roots
        ):
            raise DomainError("forbidden_path", "Path escapes its allowed root", 403)
        return resolved

    def read_code(self, path: str, offset: int = 0, limit: int = 24000) -> dict:
        self.consume("read_code")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 24000:
            raise DomainError('invalid_source_range', 'Use a nonnegative offset and a limit from 1 to 24000', 422)
        target = self._path(self.root, path, exists=True)
        if not target.is_file() or target.stat().st_size > 256 * 1024:
            raise DomainError("file_not_supported", "Read requires a small regular text file", 422)
        data = target.read_bytes()
        if b"\x00" in data:
            raise DomainError("file_not_supported", "Binary code input is not exposed as text", 422)
        text = data.decode('utf-8')
        page = text_page(path, text, 'sha256:' + hashlib.sha256(data).hexdigest(), offset, limit)
        with self.lock:
            self.source_reads.record(str(target.relative_to(self.root)), page, len(text))
        return page

    def read_context(self, path: str, offset: int = 0, limit: int = 24000) -> dict:
        """Read a bounded slice of controller-frozen stage evidence, never user paths."""
        self.consume('read_context')
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 24000:
            raise DomainError('invalid_context_range', 'Use a nonnegative offset and a limit from 1 to 24000', 422)
        if (self.task.context_directory is None or self.task.context_directory not in self.task.allowed_read_roots
                or len(path) != 69
                or not path.endswith('.json') or any(c not in '0123456789abcdef' for c in path[:-5])):
            raise DomainError('forbidden_path', 'Select a file from the frozen stage input index', 403)
        root = self.task.context_directory
        if root.is_symlink() or root.resolve() != root:
            raise DomainError('forbidden_path', 'Stage input root is not stable', 403)
        target = self._path(root, path, exists=True)
        if not target.is_file() or target.stat().st_size > 1024 * 1024:
            raise DomainError('file_not_supported', 'Stage input exceeds the readable document limit', 422)
        raw = target.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != path[:-5]:
            raise DomainError('context_modified', 'Stage input no longer matches its frozen digest', 409)
        text = raw.decode('utf-8')
        return text_page(path, text, 'sha256:' + digest, offset, limit)

    def list_code(self, path: str = ".") -> dict:
        self.consume("list_code")
        target = self._path(self.root, path, exists=True)
        if not target.is_dir():
            raise DomainError("invalid_path", "Expected directory", 422)
        files = []
        for directory, dirs, names in os.walk(target, followlinks=False):
            dirs[:] = [name for name in dirs if name not in self.EXCLUDED and not (Path(directory) / name).is_symlink()]
            for name in sorted(names):
                relative = str((Path(directory) / name).relative_to(self.root))
                try:
                    self._path(self.root, relative, exists=True)
                except DomainError:
                    continue
                files.append(relative)
                if len(files) >= 2000:
                    return {"files": files, "truncated": True}
        return {"files": files, "truncated": False}

    def write_document(self, path: str, content: str) -> dict:
        self.consume("write_document")
        target = self._path(self.documents, path)
        if target.suffix.lower() not in self.DOCUMENT_SUFFIXES:
            raise DomainError("code_write_forbidden", "This role may write documentation only", 403)
        if len(content.encode()) > 1024 * 1024:
            raise DomainError("document_too_large", "Document exceeds task bound", 422)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._path(self.documents, path)
        temporary = target.parent / (".write-" + str(uuid4()))
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        with self.lock:
            if str(target) not in self.artifacts:
                self.artifacts.append(str(target))
        return {"path": path, "digest": "sha256:" + hashlib.sha256(content.encode()).hexdigest()}

    def propose_work(self, proposal: dict) -> dict:
        self.consume("propose_work")
        if not isinstance(proposal, dict) or not proposal.get("goal") or not proposal.get("role"):
            raise DomainError("invalid_proposal", "Work proposal requires goal and role", 422)
        path = self.documents / f"work-proposal-{uuid4()}.json"
        atomic_json(path, {"proposal": proposal, "input_fingerprint": self.task.input_fingerprint,
                           "dispatch_authorized": False})
        self.artifacts.append(str(path))
        return {"proposal_path": path.name, "state": "proposed_not_dispatched"}

    def fetch_url(self, url: str) -> dict:
        self.consume("fetch_url")
        if self.task.role != 'research' or not self.task.allow_public_web:
            raise DomainError('network_not_authorized', 'Public research access is not enabled for this task', 403)
        proxy = urlsplit(self.task.proxy_base_url)
        endpoint = urlunsplit((proxy.scheme, proxy.netloc, '/internal/v1/research/fetch', '', ''))
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=35) as client:
                response = client.post(endpoint, json={'url': url}, headers={
                    'Authorization': 'Bearer ' + self.task.proxy_token.get_secret_value(),
                    'Idempotency-Key': str(uuid4())})
                if response.status_code != 200:
                    error = response.json().get('error', {})
                    raise DomainError(error.get('code', 'source_unavailable'),
                                      error.get('message', 'Research source is unavailable'), response.status_code)
                evidence = response.json()
        except (httpx.HTTPError, ValueError):
            raise DomainError('source_unavailable', 'Research reader is unavailable', 422) from None
        path = self.documents / f"source-{uuid4()}.json"
        atomic_json(path, evidence)
        self.artifacts.append(str(path))
        return {**evidence, "content": evidence["content"][:24000], "source_artifact": path.name}

    def finish(self, result: dict) -> dict:
        self.consume("finish")
        if self.output.status()['drafts']:
            raise DomainError('result_requires_staging', 'Finish an acknowledged sealed result_ref when a staged draft exists', 422)
        _result_size(result)
        if len(_serialized(result)) > self.output.max_direct_result_bytes:
            raise DomainError('result_requires_staging',
                'Use result_begin/result_append and finish(result_ref=...) for a large result; do not repeat the whole result', 422)
        Draft202012Validator(self.task.output_schema).validate(result)
        return self._publish_result(result)

    def _validate_planning(self, result):
        if self.task.planning_contract is not None:
            try:
                result = {**result, 'parallel_work': validate_parallel_work(result.get('parallel_work'), self.task.planning_contract)}
            except DomainError as error:
                if error.code == 'planning_validation_failed':
                    self.planning_failure = error
                    self.rejected_planning_result = copy.deepcopy(result)
                raise
            self.planning_failure = None
            self.rejected_planning_result = None
        return result

    def preserve_rejected_planning(self):
        if self.finished_result is None and self.rejected_planning_result is not None and not self.output.status()['drafts']:
            self.output.preserve_rejected_planning(self.rejected_planning_result)

    def _publish_result(self, result):
        result = self._validate_planning(result)
        _result_size(result)
        if self.task.role == 'review':
            with self.lock:
                self.source_reads.require_complete()
        if self.finished_result is not None and canonical_digest(self.finished_result) != canonical_digest(result):
            raise DomainError('result_already_finished', 'A completed result cannot be replaced', 409)
        path = self.documents / "openhands_final.json"
        if path.is_symlink():
            raise DomainError('forbidden_path', 'Canonical result cannot be a symbolic link', 403)
        atomic_json(path, result)
        if str(path) not in self.artifacts:
            self.artifacts.append(str(path))
        self.finished_result = result
        return result

    def finish_ref(self, result_ref):
        self.consume('finish')
        return self._publish_result(self.output.resolve(result_ref))

    def result_begin(self, fields, streamed_fields, request_id):
        self.consume('result_begin')
        return self.output.begin(fields, streamed_fields, request_id)

    def result_append(self, result_ref, field, chunk_id, expected_offset, value, final=False):
        self.consume('result_append')
        return self.output.append(result_ref, field, chunk_id, expected_offset, value, final)

    def result_revise_parallel_work(self, result_ref, parallel_work, request_id):
        self.consume('result_revise_parallel_work')
        if self.task.planning_contract is None:
            raise DomainError('forbidden_tool', 'Planning revisions require a frozen planning contract', 403)
        return self.output.revise_parallel_work(result_ref, parallel_work, request_id, self._validate_planning)

    def result_status(self, result_ref=None, field=None, offset=0, limit=12000):
        self.consume('result_status')
        return self.output.status(result_ref, field, offset, limit)

    def execute(self, operation: str, **args):
        mapping = {"read_code": self.read_code, "list_code": self.list_code, "read_context": self.read_context,
                   "write_document": self.write_document, "propose_work": self.propose_work,
                   "fetch_url": self.fetch_url, 'result_begin': self.result_begin,
                   'result_append': self.result_append, 'result_status': self.result_status,
                   'result_revise_parallel_work': self.result_revise_parallel_work}
        if operation not in mapping:
            raise DomainError("forbidden_tool", "Role tool is not allowed", 403)
        return mapping[operation](**args)
