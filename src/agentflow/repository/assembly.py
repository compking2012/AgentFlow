"""Content-addressed assembly of parallel snapshots in a private repository."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from agentflow.common import DomainError, canonical_digest, canonical_json, utc_now

from .git import RepositoryAdapter


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        output.write(canonical_json(value))
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    RepositoryAdapter._sync(path.parent)


class AssemblyManager:
    def __init__(self, data_dir: Path, repository: RepositoryAdapter | None = None, *, max_snapshots: int = 32):
        self.root = Path(data_dir).resolve() / "assemblies"
        self.repository = repository or RepositoryAdapter()
        self.max_snapshots = max_snapshots

    async def assemble(self, snapshots: list[dict], base_repo: Path, base_oid: str,
                       operation_id: str) -> dict:
        return await asyncio.to_thread(self._assemble, snapshots, Path(base_repo), base_oid, operation_id)

    def _assemble(self, snapshots, base_repo, base_oid, operation_id):
        adapter = self.repository
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 512 or "\0" in operation_id:
            raise DomainError("invalid_operation", "A bounded stable assembly operation id is required", 422)
        if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= self.max_snapshots:
            raise DomainError("assembly_limit_exceeded", "Assembly snapshot count is outside the configured bound", 422)
        base = adapter._repo(base_repo)
        base_oid = adapter._oid(base_oid)
        inputs = []
        for snapshot in snapshots:
            if not isinstance(snapshot, dict) or not {"repository_path", "commit_oid", "base_oid"} <= set(snapshot):
                raise DomainError("invalid_snapshot", "Snapshots require repository_path, commit_oid and base_oid", 422)
            if snapshot.get("stale") or adapter._oid(snapshot["base_oid"]) != base_oid:
                raise DomainError("assembly_base_mismatch", "All snapshots must use the exact requested base commit")
            source = adapter._repo(Path(snapshot["repository_path"]))
            inputs.append({"repository_path": str(source), "commit_oid": adapter._oid(snapshot["commit_oid"]),
                           "base_oid": base_oid})
        inputs.sort(key=lambda item: (item["commit_oid"], item["repository_path"]))
        if len({item["commit_oid"] for item in inputs}) != len(inputs):
            raise DomainError("duplicate_snapshot", "A snapshot commit may appear only once", 422)
        if any(self.root == source or self.root.is_relative_to(source)
               for source in [base, *[Path(item["repository_path"]) for item in inputs]]):
            raise DomainError("unsafe_assembly_directory", "Assembly storage must be outside source repositories", 403)
        if self.root.is_symlink():
            raise DomainError("unsafe_assembly_directory", "Assembly storage cannot be a symlink", 403)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = self.root / canonical_digest({"operation_id": operation_id}).split(":")[1]
        if any(source == directory or source.is_relative_to(directory)
               for source in [base, *[Path(item["repository_path"]) for item in inputs]]):
            raise DomainError("unsafe_assembly_directory", "Assembly cannot replace one of its own inputs", 403)
        request = {"algorithm": "merge-tree-v1", "base_repository": str(base), "base_oid": base_oid, "snapshots": inputs}
        fingerprint = canonical_digest(request)
        lock_path = directory.with_suffix(".lock")
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as exc:
            raise DomainError("assembly_lock_unavailable", "Assembly lock could not be acquired safely") from exc
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                raise DomainError("assembly_lock_unavailable", "This controller lacks a verified assembly lock")
            return self._locked(directory, request, fingerprint, operation_id)
        finally:
            os.close(fd)

    def _locked(self, directory, request, fingerprint, operation_id):
        adapter = self.repository
        intent_path = directory / "intent.json"
        result_path = directory / "result.json"
        final_repo = directory / "repository"
        if directory.is_symlink():
            raise DomainError("unsafe_assembly_directory", "Assembly operation directory cannot be a symlink", 403)
        if directory.exists():
            if not intent_path.is_file() or intent_path.is_symlink():
                raise DomainError("assembly_unknown", "Existing assembly directory has no valid ownership receipt")
            try:
                intent = json.loads(intent_path.read_text())
            except (ValueError, OSError) as exc:
                raise DomainError("assembly_unknown", "Assembly intent cannot be verified") from exc
            if intent.get("input_fingerprint") != fingerprint or intent.get("operation_id") != operation_id:
                raise DomainError("idempotency_conflict", "Assembly operation id already has different inputs")
            if result_path.exists():
                return self._verify_result(result_path, final_repo, fingerprint, request)
            if intent.get("state") == "failed":
                error = intent["error"]
                raise DomainError(error["code"], error["message"], error.get("status", 409), error.get("details"))
            pending = directory / "pending_result.json"
            if final_repo.exists():
                if not pending.is_file():
                    raise DomainError("assembly_unknown", "Repository exists without an assembly completion receipt")
                value = self._verify_result(pending, final_repo, fingerprint, request)
                _write_json(result_path, value)
                return value
        else:
            directory.mkdir(mode=0o700)
            intent = {"operation_id": operation_id, "input_fingerprint": fingerprint, "request": request,
                      "state": "preparing", "created_at": utc_now()}
            _write_json(intent_path, intent)
        build = directory / "build"
        bundles = directory / "bundles"
        # No external effect has occurred. A matching private intent authorizes
        # rebuilding only this operation's incomplete staging after interruption.
        for path in (build, bundles):
            if path.is_symlink():
                raise DomainError("assembly_unknown", "Incomplete assembly staging was replaced")
            if path.exists():
                if not path.is_dir():
                    raise DomainError("assembly_unknown", "Incomplete assembly staging is not a directory")
                shutil.rmtree(path)
        bundles.mkdir(mode=0o700)
        try:
            base_oid = request["base_oid"]
            adapter._integrity(Path(request["base_repository"]), base_oid)
            adapter._clone_snapshot(Path(request["base_repository"]), build, base_oid)
            parents = []
            for index, snapshot in enumerate(request["snapshots"]):
                source, oid = Path(snapshot["repository_path"]), snapshot["commit_oid"]
                adapter._integrity(source, oid)
                try:
                    adapter._run(source, ["merge-base", "--is-ancestor", base_oid, oid])
                except DomainError as exc:
                    raise DomainError("assembly_base_mismatch", "Snapshot does not descend from its declared base") from exc
                bundle = bundles / f"{index}.bundle"
                info = adapter._prepare_bundle(source, oid, bundle)
                adapter._import_bundle(build, bundle, oid, info["sha256"])
                parents.append(oid)
            timestamp = max(int(adapter._run(build, ["show", "-s", "--format=%ct", oid]).strip()) for oid in parents) + 1
            environment = {"GIT_AUTHOR_NAME": "AgentFlow", "GIT_AUTHOR_EMAIL": "agentflow@localhost",
                "GIT_COMMITTER_NAME": "AgentFlow", "GIT_COMMITTER_EMAIL": "agentflow@localhost",
                "GIT_AUTHOR_DATE": f"{timestamp} +0000", "GIT_COMMITTER_DATE": f"{timestamp} +0000"}
            current = parents[0]
            tree = adapter._integrity(build, current)
            for other in parents[1:]:
                try:
                    output = adapter._run(build, ["-c", "merge.renormalize=false", "merge-tree", "--write-tree",
                        "--no-messages", f"--merge-base={base_oid}", current, other])
                except DomainError as exc:
                    if exc.code == "git_error" and (exc.details or {}).get("exit_code") == 1:
                        raise DomainError("assembly_conflict", "Parallel snapshots conflict; no combined candidate was accepted",
                            details={"base_oid": base_oid, "left_commit_oid": current, "right_commit_oid": other}) from exc
                    raise
                tree = adapter._oid(output.splitlines()[0].decode())
                current = adapter._run(build, ["commit-tree", tree, "-p", current, "-p", other],
                    data=f"AgentFlow assembly intermediate {fingerprint}\n".encode(), extra_env=environment).decode().strip()
            arguments = ["commit-tree", tree]
            for oid in parents:
                arguments += ["-p", oid]
            commit = adapter._run(build, arguments, data=f"AgentFlow assembled candidate\n\n{fingerprint}\n".encode(),
                                  extra_env=environment).decode().strip()
            if adapter._integrity(build, commit) != tree:
                raise DomainError("assembly_mismatch", "Assembled tree integrity check failed")
            adapter._run(build, ["checkout", "--detach", commit])
            materialized = adapter._collect_diff(build, commit)
            if materialized["tree_oid"] != tree:
                raise DomainError("assembly_materialization_mismatch", "The local filesystem cannot faithfully materialize the combined tree")
            result = {"repository_path": str(final_repo), "commit_oid": commit, "tree_oid": tree,
                "base_oid": base_oid, "parent_commit_oids": parents, "input_fingerprint": fingerprint,
                "operation_id": operation_id, "requires_review_and_tests": True}
            _write_json(directory / "pending_result.json", result)
            build.rename(final_repo)
            adapter._sync(directory)
            _write_json(result_path, result)
            _write_json(intent_path, {**intent, "state": "completed"})
            return result
        except DomainError as exc:
            _write_json(intent_path, {**intent, "state": "failed", "error": {
                "code": exc.code, "message": exc.message, "status": exc.status, "details": exc.details}})
            raise

    def _verify_result(self, path, repository, fingerprint, request):
        try:
            if path.is_symlink() or repository.is_symlink():
                raise ValueError("Assembly receipt or repository is a symbolic link")
            result = json.loads(path.read_text())
            if (result["input_fingerprint"] != fingerprint or result["repository_path"] != str(repository)
                    or result["base_oid"] != request["base_oid"] or result.get("requires_review_and_tests") is not True
                    or result["parent_commit_oids"] != [item["commit_oid"] for item in request["snapshots"]]):
                raise ValueError("Assembly receipt identity mismatch")
            tree = self.repository._integrity(repository, result["commit_oid"])
            parents = self.repository._run(repository, ["show", "-s", "--format=%P", result["commit_oid"]]).decode().split()
            if tree != result["tree_oid"] or parents != result["parent_commit_oids"]:
                raise ValueError("Assembly receipt object mismatch")
            return result
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DomainError("assembly_unknown", "Assembly completion receipt could not be verified") from exc
