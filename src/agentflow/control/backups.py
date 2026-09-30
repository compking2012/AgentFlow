"""Private application checkpoints, including execution evidence and portable Git objects.

The caller must stop the controller and retain Store's single-writer lock while
creating a checkpoint. This is a sensitive disaster-recovery backup, not a report
export. Restore never treats an old checkpoint as proof that a process stopped.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path, PurePosixPath
from uuid import uuid4

from agentflow.common import DomainError, canonical_digest, canonical_json, utc_now
from agentflow.repository import RepositoryAdapter
from agentflow.runtime.workspace import WorkspaceManager
from agentflow.runtime.workspace import _private_json as _workspace_json
from agentflow.storage import LocalArtifactStore, Store

FORMAT = "agentflow-application-backup"
MANIFEST = "application-backup.json"
MANAGED_ROOTS = {"nodes", "model_invocations", "workspaces", "workspace_metadata", "repositories",
                 "assemblies", "attempt_artifacts", "candidates", "deliveries", "supervisor",
                 "sandbox_profiles", "codex_homes", "openhands_homes", "restore_evidence", "product_exports"}
REPOSITORY_FIELDS = {"repository_path", "source_repository", "target_repository", "base_repository",
                     "source_repo", "local_path"}
SECRET_FIELDS = {"task_token", "attempt_token", "proxy_token", "access_token", "refresh_token", "authorization",
                 "single_use_code", "bootstrap_code", "redact_values"}
TERMINAL = {"completed", "failed", "cancelled", "succeeded", "released", "settled"}


def _private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(canonical_json(value))
        output.flush()
        os.fsync(output.fileno())
    path.chmod(0o600)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def _sync_directory(path: Path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _secure_tree(root: Path):
    directories = [root]
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            directories.append(path)
        else:
            parts = path.relative_to(root).parts
            private_key = parts[:2] == ("nodes", "pki") or parts[:3] == ("managed", "nodes", "pki")
            path.chmod(0o700 if path.stat().st_mode & 0o111 and not private_key else 0o600)
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        path.chmod(0o700)
        _sync_directory(path)


def _safe_relative(value: str) -> Path:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or "." in path.parts
            or "\\" in value or "\x00" in value or path.as_posix() != value):
        raise DomainError("invalid_backup_path", "Backup paths must be normalized relative paths", 422)
    return Path(*path.parts)


def _sanitize(value):
    if isinstance(value, dict):
        return {key: child if key == "response_receipt" else _sanitize(child) for key, child in value.items()
                if key.lower() not in SECRET_FIELDS and not key.upper().endswith(("API_KEY", "PASSWORD"))
                and not (key == "environment" and isinstance(child, dict)
                         and any(any(marker in name.upper() for marker in ("TOKEN", "API_KEY", "SECRET", "PASSWORD")) for name in child))}
    if isinstance(value, list):
        return [_sanitize(child) for child in value]
    return value


def _relocate(value, source: Path, destination: Path, workspace_paths=None):
    if isinstance(value, dict):
        relocated = {}
        for key, child in value.items():
            if key == "response_receipt" and isinstance(child, dict):
                # The locator moves; a provider's hashed payload remains original evidence.
                relocated[key] = dict(child)
                if "path" in child:
                    relocated[key]["path"] = _relocate(child["path"], source, destination, workspace_paths)
            else:
                relocated[key] = _relocate(child, source, destination, workspace_paths)
        return relocated
    if isinstance(value, list):
        return [_relocate(child, source, destination, workspace_paths) for child in value]
    if isinstance(value, str):
        for old, new in (workspace_paths or {}).items():
            if value == str(old):
                return str(new)
            if value.startswith(str(old) + os.sep):
                normalized = Path(os.path.abspath(value))
                if normalized.is_relative_to(old):
                    return str(new / normalized.relative_to(old))
    if isinstance(value, str) and value.startswith(str(source) + os.sep):
        normalized = Path(os.path.abspath(value))
        if normalized.is_relative_to(source):
            return str(destination / normalized.relative_to(source))
    return str(destination) if value == str(source) else value


def _records(database: Path) -> list[tuple[str, str, int, dict]]:
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        return [(kind, identity, revision, json.loads(body)) for kind, identity, revision, body
                in connection.execute("SELECT kind,id,revision,body FROM records ORDER BY kind,id")]


def _path_fields(value, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            field = f"{prefix}.{key}" if prefix else key
            if isinstance(child, str) and key in REPOSITORY_FIELDS and Path(child).is_absolute():
                yield field, child
            yield from _path_fields(child, field)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _path_fields(child, f"{prefix}[{index}]")


def _source_repositories(records, source: Path, workspace_locations=None):
    internal, external = {}, []
    for kind, identity, _, body in records:
        for field, value in _path_fields(body):
            path = Path(os.path.abspath(value))
            commits = sorted({oid for oid in (body.get("commit_oid"), body.get("base_oid"), body.get("base_commit"),
                                               body.get("source_commit"), body.get("candidate_commit")) if oid})
            if path in (workspace_locations or {}) or path.is_relative_to(source):
                if path == source:
                    raise DomainError("unsafe_backup_repository", "Application data and a managed repository cannot share their root")
                internal.setdefault(path, set()).update(commits)
            else:
                external.append({"path": str(path), "record_kind": kind, "record_id": identity,
                                 "field": field, "included": False,
                                 "required_commit_oids": commits,
                                 "requirement": "Preserve this external user repository separately; restore does not write it"})
    unique = {canonical_json(row): row for row in external}
    return internal, list(unique.values())


def _project_workspace_entries(data_dir):
    manager = WorkspaceManager(data_dir, create=False)
    if not manager.metadata.exists():
        return []
    entries = []
    try:
        if manager.metadata.is_symlink():
            raise ValueError('linked_workspace_metadata')
        retiring = manager.metadata / 'retiring'
        if retiring.is_symlink():
            raise ValueError('linked_workspace_retirement')
        if retiring.exists() and any(retiring.glob('*.json')):
            raise DomainError('backup_workspace_retiring', 'Complete interrupted workspace retirement before creating a checkpoint')
        for file in sorted(manager.metadata.glob('*.json')):
            metadata = _workspace_json(file)
            if metadata.get('version', 1) != 2:
                continue
            metadata = manager.registration(metadata['attempt_id'])
            name = canonical_digest(metadata['attempt_id']).split(':')[1]
            if file.name != name + '.json':
                raise ValueError('workspace_registration_name_mismatch')
            entries.append({'attempt_id': metadata['attempt_id'], 'project_id': metadata['project_id'],
                'project_root': metadata['project_root'], 'source_path': metadata['path'],
                'backup_path': 'workspaces/' + name, 'base_oid': metadata['base_oid']})
    except DomainError:
        raise
    except (OSError, ValueError, KeyError) as error:
        raise DomainError('backup_workspace_invalid', 'A project workspace registration cannot be verified') from error
    return entries


def _workspace_locations(manifest):
    locations = {}
    entries = manifest.get('project_workspaces', [])
    if not isinstance(entries, list):
        raise DomainError('invalid_backup', 'Invalid project workspace inventory')
    for entry in entries:
        try:
            name = canonical_digest(entry['attempt_id']).split(':')[1]
            relative = _safe_relative(entry['backup_path'])
            root = Path(entry['project_root'])
            original = Path(entry['source_path'])
            if (not isinstance(entry['attempt_id'], str) or not entry['attempt_id']
                    or not isinstance(entry['project_id'], str) or not entry['project_id']
                    or not root.is_absolute() or Path(os.path.abspath(root)) != root
                    or original != root / '.agentflow/workspaces' / name
                    or relative != Path('workspaces') / name or original in locations):
                raise ValueError('invalid_workspace_mapping')
            locations[original] = relative
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError('invalid_backup', 'Invalid project workspace mapping') from error
    if len(set(locations.values())) != len(locations):
        raise DomainError('invalid_backup', 'Project workspace destinations overlap')
    return locations


def _excluded(relative: Path) -> str | None:
    if any(part == ".env" or part.startswith(".env.") for part in relative.parts):
        return "credential_environment_file"
    if any(part in {".ssh", ".aws", ".azure", ".gcloud"} for part in relative.parts):
        return "ambient_credentials"
    if (relative.name in {".git-credentials", ".netrc", ".npmrc"}
            or (relative.parts[0] in {"codex_homes", "openhands_homes", "supervisor"}
                and relative.name in {"auth.json", "credentials.json"})):
        return "ambient_credentials"
    if ((relative.parts[0] == "supervisor" and relative.name in {"launch.lock", "go.json", "cancel.json"})
            or (relative.parts[0] == "assemblies" and len(relative.parts) == 2 and relative.name.endswith(".lock"))
            or (".git" in relative.parts and relative.name.endswith(".lock"))):
        return "transient_lock_or_execution_permit"
    if relative.parts[0] == "supervisor" and relative.name == "launch.json":
        return "launch_environment_and_authority"
    return None


def _copy_file(source: Path, destination: Path, *, executable=False):
    observed = source.lstat()
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as incoming:
        opened = os.fstat(incoming.fileno())
        if not stat.S_ISREG(opened.st_mode) or (observed.st_dev, observed.st_ino) != (opened.st_dev, opened.st_ino):
            raise DomainError("backup_source_changed", "Backup source changed while opening a file")
        with destination.open("xb") as outgoing:
            os.chmod(destination, 0o700 if executable else 0o600)
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        after = os.fstat(incoming.fileno())
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise DomainError("backup_source_changed", "A managed file changed during the checkpoint")


def _copy_tree(source: Path, destination: Path, *, logical_root: str, exclusions=None):
    """Copy bytes, never hardlinks; preserve only internal relative symlinks."""
    if source.is_symlink() or not source.is_dir():
        raise DomainError("unsafe_backup_source", "Managed roots must be real directories")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    source_root = source.resolve()

    def copy(directory, output):
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(source)
            logical = Path(logical_root) / relative
            reason = _excluded(logical) if exclusions is not None else None
            if reason:
                exclusions.append({"path": logical.as_posix(), "reason": reason})
                continue
            info = path.lstat()
            target = output / path.name
            if stat.S_ISLNK(info.st_mode):
                try:
                    resolved = path.resolve()
                except (OSError, RuntimeError) as exc:
                    raise DomainError("unsafe_backup_symlink", "A managed link cannot be resolved safely") from exc
                if not resolved.is_relative_to(source_root) or logical.parts[:2] == ("nodes", "pki"):
                    raise DomainError("unsafe_backup_symlink", "A managed link escapes its independently copied root")
                target.symlink_to(os.path.relpath(resolved, path.parent))
            elif stat.S_ISDIR(info.st_mode):
                target.mkdir(mode=0o700)
                copy(path, target)
            elif stat.S_ISREG(info.st_mode):
                if path.name == ".git":
                    raise DomainError("dependent_git_workspace", "Linked Git workspaces must be made independent before backup")
                if path.name == "alternates" and path.parent.name == "info" and path.stat().st_size:
                    raise DomainError("dependent_git_workspace", "Git object alternates are not independent backup content")
                _copy_file(path, target, executable=bool(info.st_mode & 0o111) and logical.parts[:2] != ("nodes", "pki"))
            elif stat.S_ISSOCK(info.st_mode) and exclusions is not None:
                exclusions.append({"path": logical.as_posix(), "reason": "transient_socket"})
            else:
                raise DomainError("unsupported_backup_file", "Managed storage contains a non-regular device or pipe")
    copy(source, destination)


def _inventory(root: Path):
    result = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative in {MANIFEST, ".incomplete"}:
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            if Path(target).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
                raise DomainError("unsafe_backup_symlink", "Backup contains an escaping link")
            result.append({"path": relative, "kind": "symlink", "target": target,
                           "digest": "sha256:" + hashlib.sha256(target.encode()).hexdigest()})
        elif stat.S_ISDIR(info.st_mode):
            result.append({"path": relative, "kind": "directory"})
        elif stat.S_ISREG(info.st_mode):
            result.append({"path": relative, "kind": "file", "size": info.st_size,
                           "digest": _digest(path), "executable": bool(info.st_mode & 0o111)})
        else:
            raise DomainError("invalid_backup", "Backup contains a special file")
    return result


def _sanitize_snapshot(snapshot: Path):
    database = snapshot / "state/agentflow.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA secure_delete=ON")
        for kind, identity, _, body in _records(database):
            clean = _sanitize(body)
            if clean != body:
                connection.execute("UPDATE records SET body=? WHERE kind=? AND id=?", (canonical_json(clean), kind, identity))
        for rowid, encoded in connection.execute("SELECT rowid,result FROM commands").fetchall():
            connection.execute("UPDATE commands SET result=? WHERE rowid=?", (canonical_json(_sanitize(json.loads(encoded))), rowid))
        for identity, encoded in connection.execute("SELECT id,body FROM events").fetchall():
            connection.execute("UPDATE events SET body=? WHERE id=?", (canonical_json(_sanitize(json.loads(encoded))), identity))
        connection.commit()
        connection.execute("VACUUM")
    manifest = json.loads((snapshot / "backup.json").read_text())
    manifest["sha256"] = _digest(database)[7:]
    manifest["credential_fields_removed"] = True
    _private_json(snapshot / "backup.json", manifest)


def _verify_required_evidence(records, data: Path, *, source: Path):
    for kind, identity, _, body in records:
        if kind == "node_artifact" and body.get("state") == "complete":
            digest = body.get("digest", "")
            path = data / "nodes/artifacts/objects" / digest.removeprefix("sha256:")
            if not path.is_file() or path.is_symlink() or _digest(path) != digest or path.stat().st_size != body.get("size"):
                raise DomainError("backup_evidence_missing", f"Node artifact {identity} is missing or corrupt")
        if kind == "model_invocation" and isinstance(body.get("response_receipt"), dict):
            receipt = body["response_receipt"]
            if receipt.get("path"):
                original = Path(receipt["path"])
                if not original.is_relative_to(source):
                    raise DomainError("external_model_evidence", "Model receipts must be in managed storage")
                path = data / original.relative_to(source)
                valid = path.is_file() and not path.is_symlink()
                if valid and receipt.get("media_type") == "application/json":
                    try:
                        valid = (path.stat().st_size <= 64 * 1024 * 1024
                                 and canonical_digest(json.loads(path.read_bytes())) == receipt.get("digest")
                                 and canonical_digest(receipt.get("body")) == receipt.get("digest"))
                    except (ValueError, UnicodeDecodeError):
                        valid = False
                elif valid:
                    valid = _digest(path) == receipt.get("digest")
                if not valid:
                    raise DomainError("backup_evidence_missing", f"Model receipt {identity} is missing or corrupt")


def _verify_repositories(repositories, source: Path, copied: Path, workspace_locations=None):
    adapter = RepositoryAdapter(git_path=shutil.which("git") or "git")
    for original, commits in repositories.items():
        relative = (workspace_locations or {}).get(original)
        repo = copied / (relative if relative is not None else original.relative_to(source))
        if not repo.is_dir():
            raise DomainError("backup_repository_missing", "A managed repository is missing from the checkpoint")
        if (repo / ".git").is_file():
            raise DomainError("dependent_git_workspace", "Managed Git objects depend on an external worktree directory")
        # Check the copied object database, including unreferenced commit-tree snapshots.
        adapter._run(repo, ["fsck", "--full", "--no-reflogs"])
        for commit in commits:
            adapter._integrity(repo, commit)


class ApplicationBackup:
    def __init__(self, store: Store, data_dir: Path):
        self.store = store
        self.data_dir = Path(data_dir).resolve()
        if self.data_dir != Path(store.data_dir).resolve():
            raise DomainError("backup_store_mismatch", "Store and managed data directory must match")

    async def create(self, new_destination: Path) -> dict:
        destination = Path(new_destination).absolute()
        if destination.exists() or destination.is_symlink() or destination.resolve().is_relative_to(self.data_dir):
            raise DomainError("backup_exists", "Backup requires a new directory outside managed storage")
        destination = destination.resolve()
        destination = destination.resolve()
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        _private_json(destination / ".incomplete", {"operation": "application_backup"})
        snapshot = destination / "store"
        await self.store.backup(snapshot, LocalArtifactStore(self.data_dir / "artifacts"))
        return await asyncio.to_thread(self._finish_create, destination)

    def _finish_create(self, destination):
        snapshot = destination / "store"
        _sanitize_snapshot(snapshot)
        records = _records(snapshot / "state/agentflow.sqlite3")
        project_workspaces = _project_workspace_entries(self.data_dir)
        workspace_locations = _workspace_locations({'project_workspaces': project_workspaces})
        repositories, external = _source_repositories(records, self.data_dir, workspace_locations)
        for entry in project_workspaces:
            repositories.setdefault(Path(entry['source_path']), set()).add(entry['base_oid'])
        roots = set(MANAGED_ROOTS)
        roots.update(path.relative_to(self.data_dir).parts[0] for path in repositories if path.is_relative_to(self.data_dir))
        roots -= {"state", "artifacts", "auth"}
        exclusions = [{"path": "auth", "reason": "owner_credentials_and_ipc_are_not_restored"}]
        managed = destination / "managed"
        managed.mkdir(mode=0o700)
        for name in sorted(roots):
            source = self.data_dir / name
            if source.exists() or source.is_symlink():
                _copy_tree(source, managed / name, logical_root=name, exclusions=exclusions)
        for original, relative in workspace_locations.items():
            target = managed / relative
            if target.exists() or target.is_symlink():
                raise DomainError('backup_workspace_collision', 'Project and legacy workspace backup locations overlap')
            _copy_tree(original, target, logical_root=relative.as_posix(), exclusions=exclusions)
        for path in self.data_dir.iterdir():
            if path.name not in roots | {"state", "artifacts"}:
                exclusions.append({"path": path.name, "reason": "not_managed_backup_content"})
        _verify_required_evidence(records, managed, source=self.data_dir)
        _verify_repositories(repositories, self.data_dir, managed, workspace_locations)
        # New source changes cannot silently replace immutable files already copied.
        for row in _inventory(managed):
            if row["kind"] == "file":
                original = self.data_dir / _safe_relative(row["path"])
                relative = _safe_relative(row['path'])
                for workspace, location in workspace_locations.items():
                    if relative.is_relative_to(location):
                        original = workspace / relative.relative_to(location)
                        break
                if original.is_symlink() or not original.is_file() or _digest(original) != row["digest"]:
                    raise DomainError("backup_source_changed", "Managed content changed during backup verification")
        _secure_tree(destination)
        stored = json.loads((snapshot / "backup.json").read_text())
        manifest = {"format": FORMAT, "version": 2 if project_workspaces else 1, "backup_id": str(uuid4()), "created_at": utc_now(),
                    "source_data_dir": str(self.data_dir), "sensitive_backup": True,
                    "contains_controller_private_keys": (managed / "nodes/pki").exists(),
                    "credential_environment_included": False, "external_repositories": external,
                    "store_snapshot": {"schema_version": stored["schema_version"], "event_watermark": stored["event_watermark"],
                                       "artifact_count": len(stored.get("artifacts", {}).get("artifacts", []))},
                    "managed_roots": sorted(name for name in roots if (managed / name).exists()),
                    "excluded_paths": exclusions, "files": _inventory(destination)}
        if project_workspaces:
            manifest['project_workspaces'] = project_workspaces
        manifest["fingerprint"] = canonical_digest(manifest)
        _private_json(destination / MANIFEST, manifest)
        (destination / ".incomplete").unlink()
        _sync_directory(destination)
        return self._view(destination, manifest)

    @staticmethod
    def _view(path, manifest):
        return {"path": str(path), "backup_id": manifest["backup_id"], "sensitive_backup": True,
                **manifest.get("store_snapshot", {}),
                "contains_controller_private_keys": manifest["contains_controller_private_keys"],
                "file_count": sum(row["kind"] == "file" for row in manifest["files"]),
                'project_workspaces': manifest.get('project_workspaces', []),
                "external_repositories": manifest["external_repositories"], "excluded_paths": manifest["excluded_paths"]}

    @staticmethod
    def _verify(backup: Path) -> dict:
        if backup.is_symlink() or not backup.is_dir() or (backup / ".incomplete").exists():
            raise DomainError("incomplete_backup", "Application backup is incomplete or unsafe")
        path = backup / MANIFEST
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            raise DomainError("invalid_backup", "Application backup manifest is missing or unsafe")
        try:
            manifest = json.loads(path.read_text())
            if (manifest["format"] != FORMAT or type(manifest["version"]) is not int or manifest["version"] not in {1, 2}
                    or not manifest["sensitive_backup"]
                    or canonical_digest({k: v for k, v in manifest.items() if k != "fingerprint"}) != manifest["fingerprint"]):
                raise ValueError("format or digest")
            origin = manifest["source_data_dir"]
            if not isinstance(origin, str) or not Path(origin).is_absolute() or os.path.abspath(origin) != origin:
                raise ValueError("source directory")
            paths = [row["path"] for row in manifest["files"]]
            if len(paths) != len(set(paths)):
                raise ValueError("duplicate paths")
            for value in paths:
                _safe_relative(value)
            if _inventory(backup) != manifest["files"]:
                raise ValueError("file digest inventory")
            if manifest['version'] == 1 and manifest.get('project_workspaces'):
                raise ValueError('project workspace layout requires backup version 2')
            _workspace_locations(manifest)
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError("backup_corrupt", "Application backup manifest or content hashes do not match") from exc
        return manifest

    @staticmethod
    async def restore(backup: Path, new_data_dir: Path) -> dict:
        backup, destination = Path(backup).absolute(), Path(new_data_dir).absolute()
        if backup.is_symlink():
            raise DomainError("incomplete_backup", "Application backup root must be a real directory")
        if destination.exists() or destination.is_symlink() or destination.resolve().is_relative_to(backup.resolve()):
            raise DomainError("unsafe_restore", "Restore requires a new directory outside the backup")
        # Canonicalize the owner's directory selection (including macOS /var
        # and /tmp aliases) before strict no-symlink traversal of its contents.
        backup, destination = backup.resolve(), destination.resolve()
        destination = destination.resolve()
        manifest = await asyncio.to_thread(ApplicationBackup._verify, backup)
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        _private_json(destination / ".incomplete", {"operation": "application_restore", "backup_id": manifest["backup_id"]})
        store_stage = destination / ".store-restore"
        await Store.restore_backup(backup / "store", store_stage)
        for path in store_stage.iterdir():
            path.rename(destination / path.name)
        store_stage.rmdir()
        report = await asyncio.to_thread(ApplicationBackup._finish_restore, backup, destination, manifest)
        (destination / ".incomplete").unlink()
        _sync_directory(destination)
        return report

    @staticmethod
    def _finish_restore(backup, destination, manifest):
        source = Path(manifest["source_data_dir"])
        workspace_locations = _workspace_locations(manifest)
        workspace_paths = {original: destination / relative for original, relative in workspace_locations.items()}
        workspace_inventory = {Path(entry['source_path']): entry for entry in manifest.get('project_workspaces', [])}
        for name in manifest["managed_roots"]:
            _safe_relative(name)
            if len(Path(name).parts) != 1 or name in {"state", "artifacts", "auth"}:
                raise DomainError("invalid_backup", "Invalid managed root in backup")
            _copy_tree(backup / "managed" / name, destination / name, logical_root=name)
        database = destination / "state/agentflow.sqlite3"
        records = _records(database)
        _verify_required_evidence(records, destination, source=source)
        repos, _ = _source_repositories(records, source, workspace_locations)
        for entry in manifest.get('project_workspaces', []):
            repos.setdefault(Path(entry['source_path']), set()).add(entry['base_oid'])
        _verify_repositories(repos, source, destination, workspace_locations)
        report = {**ApplicationBackup._view(destination, manifest), "restored_at": utc_now(),
                  "source_data_dir": str(source), "path_mapping": {str(source): str(destination),
                      **{str(old): str(new) for old, new in workspace_paths.items()}},
                  "paused_run_ids": [], "uncertain_attempt_ids": [], "revoked_node_ids": [],
                  "budget_reconciliation_required": True, "automatic_resume_allowed": False,
                  "process_stop_verified": False, "relocated_record_count": 0}
        ApplicationBackup._freeze_records(database, records, source, destination, manifest, report, workspace_paths)
        evidence = destination / "restore_evidence" / manifest["backup_id"]
        for root_name in ("workspace_metadata", "assemblies", "supervisor", "openhands_homes"):
            root = destination / root_name
            if not root.exists():
                continue
            for path in root.rglob("*.json"):
                if path.is_symlink() or ".git" in path.parts or (root_name == "assemblies" and "repository" in path.relative_to(root).parts):
                    continue
                try:
                    original = json.loads(path.read_text())
                except (ValueError, UnicodeDecodeError):
                    continue
                updated = _relocate(original, source, destination, workspace_paths)
                if root_name == 'workspace_metadata' and original.get('version') == 2:
                    prior_path = Path(original.get('path', ''))
                    if prior_path not in workspace_paths or original.get('layout') != 'project':
                        raise DomainError('invalid_backup', 'Project workspace metadata has no matching backup inventory')
                    entry = workspace_inventory[prior_path]
                    if (any(original.get(key) != entry.get(key) for key in ('project_id', 'project_root', 'attempt_id', 'base_oid'))
                            or original.get('input_fingerprint') != canonical_digest(original.get('request'))):
                        raise DomainError('invalid_backup', 'Project workspace metadata differs from its backup inventory')
                    name = canonical_digest(original.get('attempt_id')).split(':')[1]
                    if path.name != name + '.json' or updated.get('path') != str(destination / 'workspaces' / name):
                        raise DomainError('invalid_backup', 'Project workspace restoration identity does not match')
                    request = updated.get('request') or {}
                    if request.get('attempt_id') != original['attempt_id']:
                        raise DomainError('invalid_backup', 'Project workspace source request is missing')
                    # Historical execution evidence is restored into the new
                    # private data directory. Never overwrite the original project.
                    updated.update(version=1, layout='legacy', restored_project_registration={
                        'project_id': original['project_id'], 'project_root': original['project_root'],
                        'path': original['path']}, input_fingerprint=canonical_digest({
                            'source': request['source'], 'ref': request['ref'], 'attempt_id': original['attempt_id']}))
                if updated != original:
                    archive = evidence / path.relative_to(destination)
                    archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    _copy_file(path, archive)
                    if isinstance(updated, dict):
                        updated.update(restore_revalidation_required=True, restore_original_digest=canonical_digest(original))
                    _private_json(path, updated)
        _private_json(destination / "restore-report.json", report)
        _secure_tree(destination)
        return report

    @staticmethod
    def _freeze_records(database, records, source, destination, manifest, report, workspace_paths=None):
        changed_invocations, uncertain_counts = {}, {}
        completed_work = {identity for kind, identity, _, body in records
                          if kind == "work_item" and body.get("status") == "completed"}
        for kind, _, _, body in records:
            if kind == "model_invocation" and body.get("state") in {"reserved", "dispatching"}:
                for owner_kind in ("run", "iteration"):
                    key = (owner_kind, body.get(f"{owner_kind}_id"))
                    changed_invocations[key] = changed_invocations.get(key, 0) + body.get("amount_micros", 0)
                attempt = body.get("attempt_id")
                uncertain_counts[attempt] = uncertain_counts.get(attempt, 0) + 1
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA secure_delete=ON")
            for kind, identity, revision, original in records:
                body = _relocate(_sanitize(original), source, destination, workspace_paths)
                if body != original:
                    body.update(restore_original_record_digest=canonical_digest(original), restore_revalidation_required=True)
                    report["relocated_record_count"] += 1
                if kind == "run":
                    # Even a completed/cancelled run can have a development
                    # snapshot pointing at an external user repository. Restore
                    # preserves its terminal state, but never authorizes a startup
                    # reconciler to write that original repository automatically.
                    body['restore_reconciliation_required'] = True
                    if body.get("execution_state") not in {"completed", "cancelled"}:
                        body.update(restore_prior_state=body.get("execution_state"), execution_state="paused", quality_result="unknown")
                        report["paused_run_ids"].append(identity)
                elif kind == "product":
                    body['restore_reconciliation_required'] = True
                    if body.get("state") not in {"completed", "cancelled"}:
                        body.update(restore_prior_state=body.get("state"), state="blocked",
                                    blocking_reasons=["恢复的研发任务需要先核对旧执行和预算，未自动重新启动"])
                elif kind == "product_launch":
                    body.update(state="execution_unknown", url=None, restore_reconciliation_required=True)
                elif kind == "local_execution":
                    body.update(state="blocked", phase="recovery", restore_reconciliation_required=True,
                                message="恢复后需要重新配对并验证本机执行环境")
                elif kind in {"attempt", "work_item"} and body.get("status") not in TERMINAL:
                    prior = body.get("status")
                    body.update(restore_prior_status=prior, status="execution_unknown" if kind == "attempt" or prior in {
                        "running", "waiting_execution", "cancel_requested", "execution_unknown"} else "blocked",
                        execution_status="execution_unknown", quality_result="unknown", restore_reconciliation_required=True,
                        blocking_reason="restore_requires_reconciliation", process_stop_verified=False,
                        fencing_token=body.get("fencing_token", 0) + 1)
                    if kind == "attempt":
                        report["uncertain_attempt_ids"].append(identity)
                elif kind == "supervised_attempt" and body.get("state") not in TERMINAL:
                    body.update(restore_previous_identity={key: body.get(key) for key in (
                        "pid", "process_started_at", "boot_fingerprint", "boot_identity_source",
                        "process_birth_source", "process_birth_fingerprint", "nonce")},
                        state="execution_unknown", pid=None, process_started_at=None, boot_fingerprint=None, nonce=uuid4().hex,
                        boot_identity_source=None, process_birth_source=None, process_birth_fingerprint=None,
                        fencing_token=body.get("fencing_token", 0) + 1, reason="restored_checkpoint_cannot_verify_process",
                        restore_reconciliation_required=True, process_stop_verified=False)
                elif kind == "node":
                    body.update(state="revoked", config_revision=body.get("config_revision", 1) + 1,
                                restore_repair_required=True, active_job_ids=[])
                    report["revoked_node_ids"].append(identity)
                elif kind == "node_pairing":
                    body.update(state="expired", expires_at=utc_now(), restore_revoked=True)
                elif kind == "node_capability":
                    body.update(verification_state="blocked", functional_result_id=None, restore_revalidation_required=True)
                elif kind == "node_resource":
                    body.update(state="quarantined", quarantine_reason="restore_requires_new_pairing_and_cleanup",
                                fencing_token=body.get("fencing_token", 0) + 1, expires_at=None, process_stop_verified=False)
                elif kind == "node_job" and body.get("state") not in TERMINAL:
                    body.update(state="execution_unknown", quality_result="unknown", restore_reconciliation_required=True,
                                fencing_token=body.get("fencing_token", 0) + 1, lease_revision=body.get("lease_revision", 0) + 1,
                                lease_expires_at=None, process_stop_verified=False)
                elif kind == "node_upload" and body.get("state") != "complete":
                    body.update(state="quarantined", restore_reconciliation_required=True)
                elif kind == "approval" and body.get("decision") is None:
                    body.update(stale=True, restore_revalidation_required=True)
                elif (kind == "delivery_intent" and body.get("status") in {"prepared", "confirmed"}
                      and (body["status"] == "prepared" or body.get("work_item_id") not in completed_work)):
                    body.update(restore_prior_status=body["status"], status="execution_unknown", restore_reconciliation_required=True)
                elif kind == "cross_scenario" and not body.get("terminal", False):
                    body.update(status="execution_unknown", terminal=True, restore_reconciliation_required=True,
                                blocking_reason="restore_checkpoint_cannot_confirm_remote_effects")
                elif kind == "budget_account":
                    body.update(restore_uncertain=True, restore_backup_id=manifest["backup_id"],
                        uncertain_micros=body.get("uncertain_micros", 0) + changed_invocations.get((body.get("owner_kind"), body.get("owner_id")), 0))
                elif kind == "model_attempt_budget":
                    body.update(restore_uncertain=True, uncertain_invocations=body.get("uncertain_invocations", 0) + uncertain_counts.get(identity, 0))
                elif kind == "model_invocation":
                    body["restore_uncertain"] = True
                    if body.get("state") in {"reserved", "dispatching"}:
                        body.update(state="uncertain", reason="restored_checkpoint_cannot_prove_dispatch_or_cost", actual_micros=None)
                elif kind == "task_authorization":
                    connection.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, identity))
                    continue
                elif kind == "dispatch_context":
                    body["restore_reconciliation_required"] = True
                if body != original:
                    connection.execute("UPDATE records SET body=?,revision=? WHERE kind=? AND id=?",
                                       (canonical_json(body), revision + 1, kind, identity))
            for rowid, encoded in connection.execute("SELECT rowid,result FROM commands").fetchall():
                connection.execute("UPDATE commands SET result=? WHERE rowid=?",
                    (canonical_json(_relocate(_sanitize(json.loads(encoded)), source, destination, workspace_paths)), rowid))
            connection.execute("INSERT INTO events(type,body,run_id,created_at) VALUES(?,?,?,?)",
                ("application.restored", canonical_json(report), None, utc_now()))
            connection.commit()
            connection.execute("VACUUM")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise DomainError("restore_corrupt", "Restored database failed integrity verification")
