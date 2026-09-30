"""Plumbing-based Git operations, without source hooks, filters or shared objects."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO

from agentflow.common import DomainError, utc_now

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_BUNDLE_REF = "refs/agentflow/source"


class RepositoryAdapter:
    def __init__(self, git_path: str | Path | None = None, *, timeout: float = 120,
                 max_output_bytes: int = 64 * 1024 * 1024,
                 max_bundle_bytes: int = 2 * 1024 * 1024 * 1024):
        self.git_path = str(git_path or shutil.which("git") or "git")
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self.max_bundle_bytes = max_bundle_bytes

    def _run(self, repo: Path | None, args: list[str], *, data: bytes | None = None,
             stdin: BinaryIO | None = None, stdout: BinaryIO | None = None,
             extra_env: dict[str, str] | None = None, check: bool = True) -> bytes:
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
               if key in os.environ}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                    "GIT_NO_REPLACE_OBJECTS": "1", "GIT_ATTR_NOSYSTEM": "1",
                    "GIT_OPTIONAL_LOCKS": "0"})
        env.update(extra_env or {})
        command = [self.git_path, "-c", f"core.hooksPath={os.devnull}",
                   "-c", "core.fsmonitor=false", "-c", f"core.attributesFile={os.devnull}",
                   "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
                   "-c", "submodule.recurse=false", "-c", "maintenance.auto=false",
                   "-c", "gc.auto=0", "-c", "credential.helper=", "-c", "pack.threads=1",
                   "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false",
                   "-c", "fetch.fsckObjects=true", "-c", "transfer.fsckObjects=true"]
        if repo is not None:
            command += ["-C", str(repo)]
        command += args
        try:
            process = subprocess.Popen(command, stdin=stdin if stdin is not None else subprocess.PIPE,
                                       stdout=stdout if stdout is not None else subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=env, start_new_session=os.name == "posix")
        except OSError as exc:
            raise DomainError("git_unavailable", "Git executable could not be started", 503) from exc
        try:
            output, errors = process.communicate(input=data, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.communicate()
            raise DomainError("git_timeout", "Git operation exceeded its time limit", 504) from exc
        output = output or b""
        if len(output) > self.max_output_bytes:
            raise DomainError("git_output_too_large", "Git evidence exceeds configured size limit", 413)
        if check and process.returncode:
            raise DomainError("git_error", "Git operation failed", 409,
                              {"operation": args[0] if args else "", "exit_code": process.returncode,
                               "diagnostic": (errors or b"")[-8192:].decode("utf-8", "replace")})
        return output if process.returncode == 0 else b""

    def _repo(self, value: Path, *, worktree: bool = False) -> Path:
        path = Path(value).absolute()
        if path.is_symlink() or not path.is_dir():
            raise DomainError("invalid_repository", "Repository path must be a real directory", 422)
        path = path.resolve()
        self._run(path, ["rev-parse", "--git-dir"])
        if worktree:
            top = self._run(path, ["rev-parse", "--show-toplevel"]).decode().strip()
            if Path(top).resolve() != path:
                raise DomainError("invalid_repository", "Operation requires the repository root", 422)
        return path

    def _commit(self, repo: Path, ref: str) -> str:
        if not isinstance(ref, str) or not ref or ref.startswith("-") or any(c in ref for c in "\x00\r\n"):
            raise DomainError("invalid_ref", "Invalid source reference", 422)
        oid = self._run(repo, ["rev-parse", "--verify", "--end-of-options", ref + "^{commit}"])
        result = oid.decode().strip()
        if not _OID.fullmatch(result):
            raise DomainError("invalid_oid", "Git did not resolve a complete commit identity", 409)
        return result

    def _validate_ref(self, ref: str) -> None:
        if not isinstance(ref, str) or not ref.startswith("refs/") or any(c in ref for c in "\x00\r\n"):
            raise DomainError("invalid_ref", "A fully qualified Git ref is required", 422)
        self._run(None, ["check-ref-format", ref])

    @staticmethod
    def _oid(value: str) -> str:
        if not isinstance(value, str) or not _OID.fullmatch(value):
            raise DomainError("invalid_oid", "A full hexadecimal object id is required", 422)
        return value

    def _integrity(self, repo: Path, oid: str) -> str:
        if self._commit(repo, self._oid(oid)) != oid:
            raise DomainError("candidate_mismatch", "Candidate is not the requested commit", 409)
        self._run(repo, ["fsck", "--strict", "--no-reflogs", "--no-dangling", oid])
        return self._run(repo, ["rev-parse", oid + "^{tree}"]).decode().strip()

    async def contains_ancestor(self, repository: Path, ancestor: str, descendant: str) -> bool:
        """Prove ancestry locally; unrelated or unavailable ancestors are not evidence."""
        def check():
            repo = self._repo(repository)
            self._commit(repo, self._oid(descendant))
            try:
                self._run(repo, ['merge-base', '--is-ancestor', self._oid(ancestor), descendant])
            except DomainError as error:
                if error.code == 'git_error' and (error.details or {}).get('exit_code') in {1, 128}:
                    return False
                raise
            return True
        return await asyncio.to_thread(check)

    async def prepare_bundle(self, source_repo: Path, commit_oid: str, bundle_path: Path) -> dict:
        return await asyncio.to_thread(self._prepare_bundle, source_repo, commit_oid, bundle_path)

    def _prepare_bundle(self, source_repo: Path, commit_oid: str, bundle_path: Path) -> dict:
        source = self._repo(source_repo)
        oid = self._commit(source, commit_oid)
        tree = self._integrity(source, oid)
        destination = Path(bundle_path).absolute()
        if destination.exists() or destination.is_symlink() or destination.parent.is_symlink():
            raise DomainError("bundle_exists", "Bundle destination must be a new safe file", 409)
        destination.parent.mkdir(parents=True, exist_ok=True)
        object_format = self._run(source, ["rev-parse", "--show-object-format"]).decode().strip()
        if object_format not in ("sha1", "sha256"):
            raise DomainError("unsupported_repository", "Unsupported Git object format", 422)
        temp = destination.parent / (".bundle-" + uuid.uuid4().hex)
        try:
            with temp.open("xb") as output:
                os.chmod(temp, 0o600)
                header = ("# v3 git bundle\n@object-format=" + object_format + "\n" +
                          oid + " " + _BUNDLE_REF + "\n\n").encode()
                output.write(header)
                output.flush()
                self._run(source, ["pack-objects", "--stdout", "--revs"], data=(oid + "\n").encode(),
                          stdout=output)
                output.flush()
                os.fsync(output.fileno())
            if temp.stat().st_size > self.max_bundle_bytes:
                raise DomainError("bundle_too_large", "Bundle exceeds configured size limit", 413)
            self._run(source, ["bundle", "verify", str(temp)])
            os.link(temp, destination)
            self._sync(destination.parent)
        finally:
            temp.unlink(missing_ok=True)
        with destination.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return {"commit_oid": oid, "tree_oid": tree, "object_format": object_format,
                "path": str(destination), "sha256": digest, "size": destination.stat().st_size}

    @staticmethod
    def _sync(directory: Path) -> None:
        if os.name == "posix":
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _copy_bundle(self, source: Path, destination: Path) -> str:
        from agentflow.storage.artifacts import _file_fd

        digest = hashlib.sha256()
        size = 0
        with os.fdopen(_file_fd(Path(source)), "rb") as input_file, destination.open("xb") as output:
            before = os.fstat(input_file.fileno())
            while chunk := input_file.read(1024 * 1024):
                size += len(chunk)
                if size > self.max_bundle_bytes:
                    raise DomainError("bundle_too_large", "Bundle exceeds configured size limit", 413)
                digest.update(chunk)
                output.write(chunk)
            after = os.fstat(input_file.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ):
                raise DomainError("bundle_changed", "Bundle changed during import", 409)
        return digest.hexdigest()

    async def import_bundle(self, target_repo: Path, bundle_path: Path, commit_oid: str,
                            *, expected_sha256: str | None = None) -> dict:
        return await asyncio.to_thread(self._import_bundle, target_repo, bundle_path, commit_oid,
                                       expected_sha256)

    def _import_bundle(self, target_repo: Path, bundle_path: Path, commit_oid: str,
                       expected_sha256: str | None) -> dict:
        target = self._repo(target_repo)
        oid = self._oid(commit_oid)
        with tempfile.TemporaryDirectory(prefix="agentflow-import-") as temporary:
            local_bundle = Path(temporary) / "candidate.bundle"
            digest = self._copy_bundle(bundle_path, local_bundle)
            if expected_sha256 is not None and digest != expected_sha256.removeprefix("sha256:"):
                raise DomainError("bundle_digest_mismatch", "Bundle hash does not match the approved input", 409)
            heads = self._run(target, ["bundle", "list-heads", str(local_bundle)]).decode().splitlines()
            if heads != [oid + " " + _BUNDLE_REF]:
                raise DomainError("bundle_candidate_mismatch", "Bundle does not contain the exact candidate", 409)
            self._run(target, ["bundle", "verify", str(local_bundle)])
            # unbundle installs objects, verifies the pack, and deliberately does not update any ref/index.
            self._run(target, ["bundle", "unbundle", str(local_bundle)])
            tree = self._integrity(target, oid)
        return {"commit_oid": oid, "tree_oid": tree, "bundle_sha256": digest, "objects_verified": True}

    async def clone_snapshot(self, source: Path, destination: Path, ref: str = "HEAD") -> dict:
        return await asyncio.to_thread(self._clone_snapshot, source, destination, ref)

    def _clone_snapshot(self, source: Path, destination: Path, ref: str) -> dict:
        source = self._repo(source)
        oid = self._commit(source, ref)
        destination = Path(destination).absolute()
        if destination.exists() or destination.is_symlink() or destination.parent.is_symlink():
            raise DomainError("workspace_exists", "Snapshot destination must not exist", 409)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(mode=0o700)
        try:
            with tempfile.TemporaryDirectory(prefix="agentflow-snapshot-") as temporary:
                # Resolve our own temporary directory: macOS's system temp prefix aliases /private/var.
                # User-supplied import paths still reject symlink components.
                bundle = Path(temporary).resolve() / "source.bundle"
                info = self._prepare_bundle(source, oid, bundle)
                empty_template = Path(temporary) / "empty-template"
                empty_template.mkdir()
                self._run(None, ["init", f"--object-format={info['object_format']}",
                                 f"--template={empty_template}", str(destination)])
                self._import_bundle(destination, bundle, oid, info["sha256"])
                self._run(destination, ["checkout", "--detach", oid])
            return {"path": str(destination), "base_oid": oid, "tree_oid": info["tree_oid"],
                    "object_format": info["object_format"]}
        except BaseException:
            shutil.rmtree(destination)
            raise

    @staticmethod
    def _safe_file(root: Path, relative: bytes) -> tuple[bytes | None, BinaryIO | None, os.stat_result]:
        if relative.startswith(b"/") or any(p in (b"", b".", b"..", b".git") for p in relative.split(b"/")):
            raise DomainError("unsafe_path", "Unsafe repository path", 422)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = relative.split(b"/")
            for component in parts[:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = next_fd
            info = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                link = os.readlink(parts[-1], dir_fd=directory)
                path = root / os.fsdecode(relative)
                try:
                    if not path.resolve().is_relative_to(root):
                        raise DomainError("unsafe_symlink", "Repository symlink escapes the workspace", 422)
                except (RuntimeError, OSError) as exc:
                    raise DomainError("unsafe_symlink", "Invalid repository symlink", 422) from exc
                return os.fsencode(link), None, info
            if not stat.S_ISREG(info.st_mode):
                raise DomainError("unsupported_file", "Only regular files and internal symlinks can be captured", 422)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            opened = os.fstat(fd)
            if (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev):
                os.close(fd)
                raise DomainError("workspace_changed", "Workspace changed during snapshot", 409)
            return None, os.fdopen(fd, "rb"), info
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise DomainError("unsafe_path", "Repository path is unsafe or inaccessible", 422) from exc
        finally:
            os.close(directory)

    async def collect_diff(self, repo: Path, base_oid: str) -> dict:
        return await asyncio.to_thread(self._collect_diff, repo, base_oid)

    async def freeze_workspace(self, repo: Path, parent_oid: str, message: str, *,
                               diff_base_oid: str | None = None, additional_parent_oids=()) -> dict:
        """Keep the execution parent chain separate from its cumulative diff base."""
        return await asyncio.to_thread(self._freeze_workspace, repo, parent_oid, message,
                                       diff_base_oid, additional_parent_oids)

    def _freeze_workspace(self, repo: Path, parent_oid: str, message: str,
                          diff_base_oid: str | None = None, additional_parent_oids=()) -> dict:
        if not isinstance(message, str) or not message.strip() or "\x00" in message or len(message) > 65536:
            raise DomainError("invalid_message", "A bounded commit message is required", 422)
        snapshot = self._collect_diff(repo, diff_base_oid or parent_oid)
        repo = self._repo(repo, worktree=True)
        parents = list(dict.fromkeys([self._commit(repo, self._oid(parent_oid)),
            *(self._commit(repo, self._oid(parent)) for parent in additional_parent_oids)]))
        for parent in parents:
            self._integrity(repo, parent)
            self._run(repo, ['merge-base', '--is-ancestor', snapshot['base_oid'], parent])
        timestamp = max(int(self._run(repo, ["show", "-s", "--format=%ct", parent]).strip())
                        for parent in parents) + 1
        environment = {"GIT_AUTHOR_NAME": "AgentFlow", "GIT_AUTHOR_EMAIL": "agentflow@localhost",
                       "GIT_COMMITTER_NAME": "AgentFlow", "GIT_COMMITTER_EMAIL": "agentflow@localhost",
                       "GIT_AUTHOR_DATE": f"{timestamp} +0000", "GIT_COMMITTER_DATE": f"{timestamp} +0000"}
        commit = self._run(repo, ["commit-tree", snapshot["tree_oid"],
                                *(argument for parent in parents for argument in ('-p', parent))],
                           data=(message.rstrip() + "\n").encode("utf-8"), extra_env=environment).decode().strip()
        return {"commit_oid": commit, "tree_oid": snapshot["tree_oid"], "base_oid": snapshot['base_oid'],
                "parent_commit_oids": parents, "diff": snapshot}

    def _collect_diff(self, repo: Path, base_oid: str) -> dict:
        repo = self._repo(repo, worktree=True)
        base = self._commit(repo, base_oid)
        head = self._commit(repo, "HEAD")
        head_paths = self._run(repo, ["ls-tree", "-r", "--name-only", "-z", "HEAD"]).split(b"\0")
        index = self._run(repo, ["ls-files", "--stage", "-z"])
        for entry in filter(None, index.split(b"\0")):
            metadata = entry.split(b"\t", 1)[0].split()
            if metadata[0] == b"160000":
                raise DomainError("unsupported_submodule", "Submodule snapshots require an explicit adapter", 422)
            if metadata[2] != b"0":
                raise DomainError("unresolved_conflicts", "Resolve index conflicts before collecting evidence", 409)
        index_paths = [e.split(b"\t", 1)[1] for e in filter(None, index.split(b"\0"))]
        untracked = self._run(repo, ["ls-files", "--others", "--exclude-standard", "-z"]).split(b"\0")
        paths = sorted(set(filter(None, [*head_paths, *index_paths, *untracked])))
        snapshots = []
        index_records = bytearray()
        observed = {}
        missing = []
        with tempfile.TemporaryDirectory(prefix="agentflow-index-") as temporary:
            environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            self._run(repo, ["read-tree", "--empty"], extra_env=environment)
            for relative in paths:
                try:
                    link, stream, info = self._safe_file(repo, relative)
                except FileNotFoundError:
                    missing.append(relative)
                    continue
                if stream is not None:
                    with stream:
                        content_hash = hashlib.file_digest(stream, "sha256").hexdigest()
                        stream.seek(0)
                        oid = self._run(repo, ["hash-object", "-w", "--no-filters", "--stdin"], stdin=stream).strip()
                        after = os.fstat(stream.fileno())
                    if self._signature(info) != self._signature(after):
                        raise DomainError("workspace_changed", "File changed while being captured", 409)
                    mode = b"100755" if info.st_mode & 0o111 else b"100644"
                else:
                    content_hash = hashlib.sha256(link).hexdigest()
                    oid = self._run(repo, ["hash-object", "-w", "--no-filters", "--stdin"], data=link).strip()
                    mode = b"120000"
                observed[relative] = self._signature(info)
                index_records += mode + b" " + oid + b"\t" + relative + b"\0"
                snapshots.append({"path": relative.decode("utf-8", "backslashreplace"),
                                  "path_b64": base64.b64encode(relative).decode(), "mode": mode.decode(),
                                  "blob_oid": oid.decode(), "sha256": content_hash, "size": info.st_size})
            self._run(repo, ["update-index", "-z", "--index-info"], data=bytes(index_records), extra_env=environment)
            tree = self._run(repo, ["write-tree"], extra_env=environment).decode().strip()
        # Detect late mutation and new untracked paths instead of attesting a mixed snapshot.
        if self._commit(repo, "HEAD") != head or self._run(repo, ["ls-files", "--stage", "-z"]) != index:
            raise DomainError("workspace_changed", "Repository changed while being captured", 409)
        latest_untracked = self._run(repo, ["ls-files", "--others", "--exclude-standard", "-z"]).split(b"\0")
        if set(latest_untracked) != set(untracked):
            raise DomainError("workspace_changed", "Untracked paths changed while being captured", 409)
        for relative in missing:
            try:
                os.lstat(repo / os.fsdecode(relative))
            except FileNotFoundError:
                continue
            raise DomainError("workspace_changed", "Deleted file reappeared during snapshot", 409)
        for relative, signature in observed.items():
            try:
                link, stream, info = self._safe_file(repo, relative)
                if stream is not None:
                    stream.close()
            except FileNotFoundError as exc:
                raise DomainError("workspace_changed", "File disappeared while being captured", 409) from exc
            if self._signature(info) != signature:
                raise DomainError("workspace_changed", "File changed while being captured", 409)
        raw = self._run(repo, ["diff", "--raw", "--no-abbrev", "-z", "-M", "--no-ext-diff",
                               "--no-textconv", base, tree])
        changes = self._parse_changes(raw)
        patch = self._run(repo, ["diff", "--binary", "--full-index", "-M", "--no-ext-diff",
                                 "--no-textconv", base, tree])
        return {"base_oid": base, "head_oid": head, "tree_oid": tree, "files": snapshots,
                "changes": changes, "has_changes": bool(changes),
                "untracked_paths": [p.decode("utf-8", "backslashreplace") for p in untracked if p],
                "ignored_files_included": False, "patch": patch.decode("utf-8", "replace"),
                "patch_b64": base64.b64encode(patch).decode(), "patch_sha256": hashlib.sha256(patch).hexdigest(),
                "captured_at": utc_now()}

    @staticmethod
    def _signature(info: os.stat_result) -> tuple:
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    @staticmethod
    def _parse_changes(raw: bytes) -> list[dict]:
        fields = raw.split(b"\0")
        result = []
        i = 0
        while i < len(fields) and fields[i]:
            old_mode, new_mode, old_oid, new_oid, status = fields[i].lstrip(b":").split()
            path = fields[i + 1]
            i += 2
            next_path = path
            if status[:1] in (b"R", b"C"):
                next_path = fields[i]
                i += 1
            result.append({"status": status.decode(), "old_path": path.decode("utf-8", "backslashreplace"),
                           "path": next_path.decode("utf-8", "backslashreplace"),
                           "old_path_b64": base64.b64encode(path).decode(),
                           "path_b64": base64.b64encode(next_path).decode(),
                           "old_mode": old_mode.decode(), "mode": new_mode.decode(),
                           "old_oid": old_oid.decode(), "oid": new_oid.decode()})
        return result

    async def publish(self, target_repo: Path, base_ref: str, expected_base_oid: str,
                      delivery_ref: str, candidate_oid: str, operation_id: str,
                      *, expected_tree_oid: str | None = None) -> dict:
        return await asyncio.to_thread(self._publish, target_repo, base_ref, expected_base_oid,
                                       delivery_ref, candidate_oid, operation_id, expected_tree_oid)

    def _publish(self, target_repo: Path, base_ref: str, expected_base_oid: str,
                 delivery_ref: str, candidate_oid: str, operation_id: str,
                 expected_tree_oid: str | None) -> dict:
        target = self._repo(target_repo)
        self._validate_ref(base_ref)
        self._validate_ref(delivery_ref)
        self._oid(expected_base_oid)
        self._oid(candidate_oid)
        if base_ref == delivery_ref or not isinstance(operation_id, str) or not operation_id or any(
            c in operation_id for c in "\x00\r\n"
        ):
            raise DomainError("invalid_delivery", "Invalid delivery references or operation id", 422)
        tree = self._integrity(target, candidate_oid)
        self._run(target, ["merge-base", "--is-ancestor", expected_base_oid, candidate_oid])
        if expected_tree_oid is not None and tree != self._oid(expected_tree_oid):
            raise DomainError("candidate_mismatch", "Candidate tree does not match verified content", 409)
        existing = self._run(target, ["show-ref", "--verify", "--hash", delivery_ref], check=False).decode().strip()
        if existing:
            if existing != candidate_oid:
                raise DomainError("delivery_conflict", "Delivery ref already names different content", 409)
            return {"operation_id": operation_id, "delivery_ref": delivery_ref, "commit_oid": candidate_oid,
                    "tree_oid": tree, "already_published": True, "base_oid": expected_base_oid}
        commands = (f"start\nverify {base_ref} {expected_base_oid}\ncreate {delivery_ref} {candidate_oid}\n"
                    "prepare\ncommit\n").encode()
        try:
            self._run(target, ["update-ref", "--create-reflog", "-m", "agentflow:" + operation_id, "--stdin"],
                      data=commands)
        except DomainError:
            # Another retry may have won the same reference transaction.
            existing = self._run(target, ["show-ref", "--verify", "--hash", delivery_ref],
                                 check=False).decode().strip()
            if existing != candidate_oid:
                raise
        return {"operation_id": operation_id, "delivery_ref": delivery_ref, "commit_oid": candidate_oid,
                "tree_oid": tree, "already_published": False, "base_oid": expected_base_oid}
