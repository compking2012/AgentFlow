import type { Check, Entity, MatrixEntry, MatrixRow, Run } from './types';
import type { QualitySummary, TestSummary } from './workflow';

export type MatrixEvidence = MatrixEntry & { candidateId: string; checks: Check[];
  state: 'passed' | 'failed' | 'unknown' | 'not_run'; reason: string };

/** A plan or a manifest binding is never execution evidence. */
export function matrixEvidence(run: Run | undefined, matrices: MatrixRow[], candidates: Entity[], checks: Check[]): MatrixEvidence[] {
  if (!run?.input_fingerprint) return [];
  const current = candidates.filter(c => c.run_id === run.id && c.run_input_fingerprint === run.input_fingerprint && !c.stale);
  return matrices.filter(m => current.some(c => c.id === m.id)).flatMap(matrix => {
    const candidate = current.find(c => c.id === matrix.id)!;
    const fingerprint = matrix.candidate_fingerprint;
    const bound = current.length === 1 && typeof fingerprint === 'string' && fingerprint.length > 0
      && fingerprint === candidate.fingerprint && matrix.state === 'bound_to_platform_manifest';
    return (matrix.plan?.entries ?? []).map(entry => {
      const matching = bound ? checks.filter(check => check.run_id === run.id && check.matrix_entry_id === entry.matrix_entry_id
        && check.candidate_fingerprint === fingerprint) : [];
      const failed = matching.some(check => check.quality_result === 'failed' || check.execution_status === 'error');
      const passed = matching.length > 0 && matching.every(check => check.execution_status === 'completed'
        && check.quality_result === 'passed' && check.evidence_verified === true
        && typeof check.executed_case_count === 'number' && check.executed_case_count > 0
        && Boolean(check.raw_report_artifact_id));
      const state = failed ? 'failed' : passed ? 'passed' : matching.length ? 'unknown' : 'not_run';
      return { ...entry, candidateId: candidate.id, checks: matching, state,
        reason: !bound ? '候选与平台清单尚未完成唯一绑定' : !matching.length ? '尚无当前候选的执行证据'
          : passed ? '执行完成，原始报告已校验' : failed ? '检查失败，请查看原始报告' : '执行或证据验证未完成' };
    });
  });
}

export function isConfirmedDelivery(delivery: Entity): boolean {
  return ['confirmed_at', 'commit_oid', 'delivery_ref'].every(key => typeof delivery[key] === 'string' && Boolean(delivery[key]));
}

export function currentQualitySummary(summary: QualitySummary | undefined, run: Run | undefined, candidates?: Entity[]): QualitySummary | undefined {
  if (!run || summary?.run_id !== run.id || summary.input_fingerprint !== run.input_fingerprint) return undefined;
  if (summary.candidate_fingerprint && candidates) {
    const current = candidates.filter(candidate => candidate.run_id === run.id && candidate.run_input_fingerprint === run.input_fingerprint && !candidate.stale);
    if (current.length !== 1 || current[0].fingerprint !== summary.candidate_fingerprint) return undefined;
  }
  return summary;
}

export function measuredPassRate(summary: TestSummary | undefined): string {
  if (!summary?.verified || !Number.isFinite(summary.total) || summary.total! <= 0
    || typeof summary.pass_rate !== 'number' || !Number.isFinite(summary.pass_rate)
    || summary.pass_rate < 0 || summary.pass_rate > 1) return '未测';
  return `${(summary.pass_rate * 100).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}%`;
}
