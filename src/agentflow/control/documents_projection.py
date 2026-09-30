"""Mutable current documents projected from immutable, versioned evidence."""
from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from agentflow.common import DomainError, canonical_digest
from agentflow.control.readable import document_sources, output_contract, render_document, safe_filename


def group_items(group, items):
    roots = {work['id'] for work in group['members']}
    return [work for work in items if work['id'] in roots or work.get('parent_stage_id') in roots]


def projection_state(group, items, revisions):
    related = group_items(group, items)
    identities = {work['id'] for work in related}
    fields = ('id', 'generation', 'revision', 'status', 'quality_result', 'artifact_ids', 'attempt_id', 'archived')
    return canonical_digest({'key': group['key'], 'work': group['work']['id'],
        'items': sorted([{key: work.get(key) for key in fields} for work in related], key=lambda w: w['id']),
        'history': sorted([(row['id'], row.get('revision')) for row in revisions
                           if row.get('work_item_id') in identities])})


def _records(work, metadata, *, historical=False):
    return [row for row in metadata if row['id'] in work.get('artifact_ids', [])
            and row.get('run_id') == work['run_id'] and row.get('work_item_id') == work['id']
            and row.get('generation', work['generation']) == work['generation']
            and (historical or not row.get('stale'))]


def _bodies(sources):
    for _, body in sources:
        if isinstance(body, dict):
            yield body.get('result', body) if isinstance(body.get('result'), dict) else body


def _findings(sources):
    return [finding for body in _bodies(sources) for finding in body.get('findings', [])
            if isinstance(finding, dict) and finding.get('description')]


def _brief(text):
    return ' '.join(str(text).split())[:500].replace('|', '\\|')


async def render_group_document(group, items, artifacts, metadata, revisions=(), *, language='zh-CN'):
    """Return one current body and a short history, never alter source artifacts."""
    current = {**group['work'], 'logical_stage_key': group['key'], 'language': language}
    versions = []
    reasons = []
    for member in group['members']:
        history = [row for row in revisions if row.get('work_item_id') == member['id']
                   and row.get('snapshot', {}).get('run_id') == current['run_id']]
        history.sort(key=lambda row: (row['snapshot'].get('generation', 0), row['snapshot'].get('revision', 0)))
        for row in history:
            versions.append((row['snapshot'], True))
            if row.get('reason'):
                reasons.append(_brief(row['reason']))
        versions.append((member, member['id'] != current['id']))
        if member.get('payload', {}).get('change_expectation'):
            reasons.append(_brief(member['payload']['change_expectation']))
    selected = None
    evidence = []
    for work, historical in versions:
        records = _records(work, metadata, historical=historical)
        if records:
            evidence.append((work, records))
            selected = (work, records)
    if selected is None:
        return None
    source_work, source_records = selected
    parsed = await document_sources(artifacts, source_records)
    readable = next((row for row in source_records if row.get('readable')), None)
    if readable:
        await artifacts.verify(readable['digest'])
        body = (await artifacts.read(readable['digest'])).decode('utf-8')
    else:
        body = render_document({**source_work, 'logical_stage_key': group['key'], 'language': language}, parsed)
    current_ids = set(group.get('current_ids', [current['id']]))
    active = [work for work in group_items(group, items)
              if work['id'] in current_ids or work.get('parent_stage_id') in current_ids]
    pending = (source_work['id'] != current['id'] or source_work['generation'] != current['generation']
               or any(work['status'] not in {'completed', 'waiting_approval'} for work in active))
    if pending:
        notice = ('The current stage is unfinished. The latest completed content follows; it is not a new approval.'
                  if language == 'en' else '当前阶段尚未完成；以下保留最近完成的内容，不代表本轮已通过。')
        body = '> ' + notice + '\n\n' + body
    current_findings = {(_brief(f.get('path', '')), _brief(f['description'])) for f in _findings(parsed)}
    review_passed = (not pending and current.get('step') == 'code_review'
                     and current.get('quality_result') == 'passed'
                     and not any(f.get('severity') == 'blocking' for f in _findings(parsed)))
    history_lines = []
    for issue in dict.fromkeys(reasons):
        outcome = ('Pending' if pending else 'Regenerated in the current version') if language == 'en' else (
            '待处理' if pending else '已在当前版本重新生成')
        history_lines.append(f'- {issue} — {outcome}')
    seen_findings = set()
    source_versions = {row['id']: {'id': row['id'], 'digest': row['digest'], 'generation': row.get('generation'),
                                  'stale': bool(row.get('stale'))}
                       for row in source_records}
    for old_work, records in evidence:
        if old_work['id'] == source_work['id'] and old_work['generation'] == source_work['generation']:
            continue
        old_sources = await document_sources(artifacts, records)
        for row in records:
            source_versions[row['id']] = {'id': row['id'], 'digest': row['digest'], 'generation': row.get('generation'),
                                          'stale': bool(row.get('stale'))}
        for finding in _findings(old_sources):
            identity = (_brief(finding.get('path', '')), _brief(finding['description']))
            if identity in seen_findings:
                continue
            seen_findings.add(identity)
            if review_passed and identity not in current_findings and finding.get('severity') == 'blocking':
                if current_findings:
                    outcome = ('The current review has no blockers and no longer reports this issue'
                               if language == 'en' else '当前审查已无阻塞，未再报告此问题')
                else:
                    outcome = 'No longer reported by the passing current review' if language == 'en' else '当前审查通过，未再报告此问题'
            elif identity in current_findings or pending:
                outcome = 'Pending' if language == 'en' else '待处理'
            else:
                outcome = 'Not listed in the current result; resolution unconfirmed' if language == 'en' else '当前结果未列出，处理结果待确认'
            location = f' ({identity[0]})' if identity[0] else ''
            history_lines.append(f'- {identity[1]}{location} — {outcome}')
    if history_lines:
        body = ('## ' + ('Issue History and Outcomes' if language == 'en' else '历史问题与处理') + '\n\n'
                + '\n'.join(dict.fromkeys(history_lines)) + '\n\n' + body)
    return {'content': body.encode('utf-8'), 'source_versions': sorted(source_versions.values(), key=lambda row: row['id']),
            'source_work': source_work, 'pending': pending}


@contextmanager
def _parent_fd(path):
    """Keep operations anchored to a directory opened without following links."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise DomainError('unsafe_document_path', '文档路径必须是绝对路径。')
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        yield directory
    finally:
        os.close(directory)


def _existing(directory, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DomainError('document_modified', '工作目录中的文档已修改，原文件已保留。')
        with os.fdopen(os.dup(fd), 'rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        return info, 'sha256:' + digest
    finally:
        os.close(fd)


def _unchanged(directory, name, prior):
    try:
        now = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return prior is None
    if prior is None:
        return False
    return (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns, now.st_ctime_ns, now.st_nlink) == (
        prior.st_dev, prior.st_ino, prior.st_size, prior.st_mtime_ns, prior.st_ctime_ns, prior.st_nlink)


def copy_digest(path):
    with _parent_fd(path) as directory:
        existing = _existing(directory, path.name)
        if existing and not _unchanged(directory, path.name, existing[0]):
            raise DomainError('document_modified', '工作目录中的文档已修改，原文件已保留。')
        return existing[1] if existing else None


def finder_metadata_digest(path):
    """Recognize bounded Finder metadata without following links or trusting its name."""
    if path.name != '.DS_Store':
        return None
    try:
        with _parent_fd(path) as directory:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or not 36 <= info.st_size <= 16 * 1024 * 1024):
                    return None
                header = os.read(fd, 20)
                if header[:8] != b'\x00\x00\x00\x01Bud1':
                    return None
                offset, size, repeated = (int.from_bytes(header[index:index + 4], 'big')
                                          for index in (8, 12, 16))
                if offset != repeated or offset < 32 or size < 32 or offset + size + 4 > info.st_size:
                    return None
                os.lseek(fd, 0, os.SEEK_SET)
                with os.fdopen(os.dup(fd), 'rb') as source:
                    digest = 'sha256:' + hashlib.file_digest(source, 'sha256').hexdigest()
                return digest if _unchanged(directory, path.name, info) else None
            finally:
                os.close(fd)
    except (OSError, DomainError):
        return None


def copy_matches(path, digest):
    try:
        return copy_digest(path) == digest
    except (OSError, DomainError):
        return False


def write_current_copy(path, content, known_digests=()):
    """Atomically update a proven generated copy; preserve edits and linked files."""
    digest = 'sha256:' + hashlib.sha256(content).hexdigest()
    with _parent_fd(path) as directory:
        existing = _existing(directory, path.name)
        if existing and existing[1] == digest:
            return
        if existing and existing[1] not in known_digests:
            raise DomainError('document_modified', '工作目录中的文档已修改，原文件已保留。')
        temporary = '.document-' + str(uuid4())
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            if not _unchanged(directory, path.name, existing[0] if existing else None):
                raise DomainError('document_modified', '工作目录中的文档已修改，原文件已保留。')
            if existing:
                os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            else:
                os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def legacy_copies(root, records, works):
    """Enumerate only paths reconstructable from prior registered readable records."""
    by_id = {work['id']: work for work in works}
    for record in records:
        work = by_id.get(record.get('work_item_id'))
        if not work or record.get('projection_key') or record.get('run_id') != work['run_id']:
            continue
        if not isinstance(record.get('generation'), int) or not record.get('name'):
            continue
        for language in ('zh-CN', 'en'):
            label = output_contract(work, language=language)['name']
            directory = root / safe_filename(work['run_id']) / (
                safe_filename(label) + '-' + safe_filename(work['id'])[:12] + '-v' + str(record['generation']))
            yield directory / safe_filename(record['name']), record['digest']


def prune_legacy_copies(candidates):
    """Remove exact generated bytes, preserving modified files and nonempty directories."""
    for path, digest in candidates:
        try:
            with _parent_fd(path) as directory:
                existing = _existing(directory, path.name)
                if not existing or existing[1] != digest or not _unchanged(directory, path.name, existing[0]):
                    continue
                os.unlink(path.name, dir_fd=directory)
                os.fsync(directory)
            path.parent.rmdir()
        except (OSError, DomainError):
            continue


def prune_empty_legacy_directories(root):
    """Remove unused stage scaffolding without unlinking files or following links."""
    if root.is_symlink():
        return
    for directory, _, _ in os.walk(root, topdown=False, followlinks=False):
        path = Path(directory)
        try:
            with _parent_fd(path) as parent:
                os.rmdir(path.name, dir_fd=parent)
        except (OSError, DomainError):
            continue
