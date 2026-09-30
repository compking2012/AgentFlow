"""Preserve only shareable acceptance outputs, never controller credentials/state.

Run after the complete strict E2E passes, with its pytest case directory as input.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def digest(path):
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def preserve(source: Path, destination: Path):
    source = source.resolve(strict=True)
    diagnostic = json.loads((source / "workflow-diagnostics.json").read_text())
    connection = sqlite3.connect((source / "controller/state/agentflow.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        artifacts = {identity: json.loads(body) for identity, body in connection.execute(
            "SELECT id,body FROM records WHERE kind='node_artifact'")}
    finally:
        connection.close()
    destination.mkdir(parents=True, exist_ok=False)

    def copied(original, relative):
        original = Path(original)
        assert original.is_file() and not original.is_symlink(), original
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        assert digest(target) == digest(original)

    def written(relative, content):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n")

    for target in ("api", "web"):
        scope = json.loads((source / target / "acceptance-scope.json").read_text())
        product = json.loads((source / target / "cli-output.json").read_text())
        assert scope["live_llm_verified"] is False and scope["target"] == target
        assert product["state"] == "completed" and scope["product_id"] == product["id"]
        assert scope["valid_checks"] == (2 if target == "api" else 4)
        assert scope["repair_verified"] == (target == "web")
        copied(source / target / "acceptance-scope.json", f"{target}/acceptance-scope.json")
        copied(source / target / "cli-output.json", f"{target}/product-receipt.json")
        copied(source / target / "cli-stderr.log", f"{target}/entry-stderr.log")
        copied(source / target / "cli-stdout.log", f"{target}/entry-stdout.log")
        copied(source / target / "observer-reconnects.json", f"{target}/observer-reconnects.json")
        archive = Path(product["delivery"]["archive_path"])
        assert digest(archive) == product["delivery"]["archive_digest"]
        copied(archive, f"{target}/verified-release.zip")
        checks = [check for check in diagnostic["check"] if check["run_id"] == scope["run_id"]]
        check_summary = []
        for check in checks:
            artifact = artifacts[check["raw_report_artifact_id"]]
            original = source / "controller/nodes/artifacts/objects" / artifact["digest"][7:]
            assert check["evidence_verified"] and digest(original) == artifact["digest"]
            relative = f"{target}/raw-reports/{check['id']}.report"
            copied(original, relative)
            check_summary.append({key: check.get(key) for key in (
                "id", "candidate_fingerprint", "quality_result", "execution_status", "evidence_verified",
                "executed_case_count", "node_result_id", "raw_report_artifact_id")} | {
                    "raw_report_digest": artifact["digest"], "raw_report_path": relative})
        candidates = [candidate for candidate in diagnostic["candidate"] if candidate["run_id"] == scope["run_id"]]
        repairs = [repair for repair in diagnostic["product_test_repair"] if repair["run_id"] == scope["run_id"]]
        changed_files = []
        if target == "web":
            assert len(repairs) == 1 and sum(check["quality_result"] == "failed" for check in checks) == 1
            current = next(candidate for candidate in candidates if candidate["fingerprint"] == scope["candidate_fingerprint"])
            changed_files = subprocess.check_output(["git", "-C", current["source_repository"], "diff", "--name-only",
                repairs[0]["preserved_test_source"], current["source_commit"]], text=True).splitlines()
            assert changed_files == ["public/index.html"]
            for name in ("ui-entry.json", "owner-ui-submitted.png", "owner-ui-final.png", "delivered-product.png"):
                copied(source / target / name, f"{target}/{name}")
        written(f"{target}/engineering-evidence.json", {
            "entry": scope["entry"], "receipt_source": scope.get("receipt_source", "entry_stdout"),
            "run_id": scope["run_id"], "final_candidate_fingerprint": scope["candidate_fingerprint"],
            "candidates": [{key: candidate.get(key) for key in (
                "id", "fingerprint", "source_commit", "tree_oid", "run_input_fingerprint")} for candidate in candidates],
            "checks": check_summary, "repairs": repairs, "repair_changed_files": changed_files,
            "original_test_source_preserved": target == "web" and changed_files == ["public/index.html"]})
    written("model-boundary.json", {"live_llm_verified": False,
        "scripted_boundary": "Only upstream LLM HTTP responses are scripted; all downstream runtime, tools, reports and delivery execute normally.",
        "calls": [{key: call[key] for key in ("protocol", "stage", "commit", "targets", "repair")} | {
            "actual_codex_tool_output_observed": bool(call["tool_outputs"])} for call in diagnostic["fixture_calls"]]})
    (destination / "README.md").write_text(
        "# AgentFlow A 层严格验收证据\n\n"
        "唯一脚本化边界是上游 LLM HTTP 响应。这些结果证明实际工程流程可以执行，不证明真实 LLM 的自主研发能力。\n\n"
        "API 产品从实际 CLI 发起；Web 产品从本次编译的实际 WebUI 填表点击发起，无 Product API mock。"
        "实际 OpenHands SDK、Codex CLI/apply_patch、隔离构建、Node 单元测试、Playwright、报告校验、质量门禁、Git、ZIP 导出和产品启动均执行。\n\n"
        "Web 初始版本故意含标签缺陷；原始测试真实失败后，平台发起修复，Codex 仅修改 public/index.html，"
        "再经 Review 和完整测试矩阵通过。原测试源码及旧失败报告保留。\n\n"
        "两个产品均经过独立 HTTP CRUD、筛选、输入校验和重启持久化检查；Web 另经独立浏览器检查。"
        "证据中的 URL 是当时测试进程地址，测试结束后已停止。verified-release.zip 是按原字节复制的最终交付。\n\n"
        "未复制 PKI、所有者/会话/任务令牌、模型请求头或控制器数据库。index.json 记录每个证据文件的哈希。\n")
    files = [{"path": path.relative_to(destination).as_posix(), "sha256": digest(path), "bytes": path.stat().st_size}
             for path in sorted(destination.rglob("*")) if path.is_file()]
    written("index.json", {"scope": "A-layer strict CLI/WebUI product acceptance", "live_llm_verified": False,
        "preserved_at": datetime.now(UTC).isoformat(), "files": files})
    return destination


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    output = root / "validation/goal-to-product" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    print(preserve(Path(sys.argv[1]), output))
