import { OwnerApi } from './api';
import { ErrorBox } from './components';
import { useCommand, useResource } from './hooks';
import type { ProductSetup, Run, Target } from './types';

type ExecutorJob = {
  run_id?: string | null; parent_work_item_id?: string | null; parent_generation?: number; state: string;
  target_config?: { target_config_id: string; app_target?: Target; [key: string]: unknown };
};

const phaseNames: Record<string, string> = { preflight: '环境检查', build: '构建验证', unit: '单元测试验证',
  integration: '集成测试验证', recovery: '恢复检查', reconnect: '等待启动', revalidation: '重新验证',
  execution: '本机执行', stopped: '已停止', blocked: '准备受阻' };

function readableReason(message: string) {
  return message.replace(/本机验证未通过：(build|unit|integration)/g, (_, phase: string) => `本机验证未通过：${phaseNames[phase]}`)
    .replace(/tool_execution_failed:\s*timeout/g, '工具执行超时（timeout）');
}

function configurationKey(value: unknown) {
  // Match the entire API configuration. Object key order is irrelevant;
  // array order and every field remain part of the backend's target identity.
  return JSON.stringify(value, (_key, part: unknown) => part && typeof part === 'object' && !Array.isArray(part)
    ? Object.fromEntries(Object.entries(part).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)) : part);
}

export function LocalExecutionWaitStatus({ api, run, onChanged }: { api: OwnerApi; run: Run; onChanged: () => void }) {
  const waiting = (run.work_items ?? []).filter(item => item.status === 'waiting_execution');
  const setup = useResource<ProductSetup>(api, waiting.length ? '/api/v1/product_setup' : null, false, 2500);
  const jobs = useResource<ExecutorJob[]>(api, waiting.length ? '/api/v1/executor_jobs' : null, true, 2500);
  const prepare = useCommand(api);
  const local = setup.data?.local_execution;
  const targets = new Map((local?.target_configs ?? []).map(target => [configurationKey(target), target]));
  const queued = (jobs.data ?? []).filter(job => job.run_id === run.id && job.state === 'queued'
    && waiting.some(item => item.id === job.parent_work_item_id
      && (job.parent_generation === undefined || job.parent_generation === item.generation))
    && job.target_config && targets.has(configurationKey(job.target_config)));
  if (!local || !queued.length) return null;

  const ready = local.state === 'ready' || queued.every(job => {
    const target = targets.get(configurationKey(job.target_config))?.app_target;
    return target && local.ready_targets?.includes(target);
  });
  const preparing = !ready && (local.preparing || local.state === 'preparing');
  const failed = !ready && !preparing && ['blocked', 'partial'].includes(local.state);
  const unprepared = !ready && !preparing && ['unprepared', 'not_prepared'].includes(local.state);
  const reason = [...new Set([local.message?.trim(), local.detail?.trim()].filter(Boolean))].join(' ');
  const title = ready ? '本机执行器已就绪' : preparing ? '本机执行器准备中' : failed ? '本机执行器准备失败' : '本机执行器尚未启动';
  async function submit() {
    if (prepare.busy || setup.loading || preparing || ready || (!failed && !unprepared)) return;
    try {
      await prepare.execute('/api/v1/product_setup/local_execution', {});
      setup.refresh(); jobs.refresh(); onChanged();
    } catch { setup.refresh(); }
  }

  return <><div className={`notice ${failed ? 'notice-error' : ready ? '' : 'notice-warning'}`} role={failed ? 'alert' : 'status'}>
    <strong>{title}</strong>
    <p>{ready ? '当前任务所需本机环境已通过验证，正在等待执行器领取作业。'
      : reason ? readableReason(reason) : failed ? '本机环境检查未通过，请查看服务说明并修复后重新准备。'
        : preparing ? '正在检查本机开发与测试环境。' : '当前任务等待本机执行器启动。'}</p>
    {!ready && local.phase && phaseNames[local.phase] && <p>当前阶段：{phaseNames[local.phase]}</p>}
    {failed && local.error_code && <details><summary>查看服务说明</summary><p>{local.error_code}</p><p>{reason}</p></details>}
    {(failed || unprepared) && <button className="button secondary small" disabled={prepare.busy || setup.loading} onClick={() => void submit()}>
      {prepare.busy ? '正在提交…' : failed ? '重新准备本机执行器' : '准备本机执行器'}
    </button>}
  </div><ErrorBox error={prepare.error} /></>;
}
