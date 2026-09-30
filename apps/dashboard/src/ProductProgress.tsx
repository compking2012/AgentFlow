import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError, OwnerApi, segment } from './api';
import { ErrorBox, Field, Panel, Status } from './components';
import { useCommand } from './hooks';
import { stepNames } from './labels';
import { nativeProductTarget, productTargetNames } from './ProductTargets';
import type { Product, ProductLanguage, Run } from './types';
import { workflowIsComplete } from './workflow';

export const productStates: Record<Product['state'], string> = {
  registered: '项目已登记',
  preparing: '正在准备', running: 'Agent 正在研发', waiting_approval: '等待你确认',
  blocked: '需要处理', completed: '研发已完成', cancelled: '已取消',
};
export function productRequiresRestart(product: Product): boolean {
  const change = product.current_change;
  const currentRestart = change?.kind === 'restart' && !change.run_id
    && change.config_revision === (product.config_revision ?? 1) && ['preparing', 'blocked'].includes(change.state);
  return Boolean(product.needs_restart && !currentRestart);
}
const languageNames: Record<ProductLanguage, string> = { 'zh-CN': '中文', en: 'English' };

function ProductLanguageSettings({ api, product, onSaved }: { api: OwnerApi; product: Product; onSaved: () => void }) {
  const [language, setLanguage] = useState<ProductLanguage>(product.language ?? 'zh-CN');
  const [error, setError] = useState<Error>(); const [notice, setNotice] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const command = useCommand(api);
  const dirty = useRef(false);
  const captured = useRef<{ language: ProductLanguage; revision: number } | undefined>(undefined);
  useEffect(() => {
    if (!dirty.current && !uncertain) setLanguage(product.language ?? 'zh-CN');
  }, [product.language, uncertain]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!Number.isInteger(product.revision) || (product.management?.can_edit === false && !uncertain)) return;
    if (captured.current?.language !== language) captured.current = { language, revision: product.revision! };
    setError(undefined); setNotice('');
    try {
      const result = await command.execute<Product>(`/api/v1/products/${segment(product.id)}/language`, {
        language, expected_revision: captured.current.revision,
      });
      if (result) {
        captured.current = undefined; dirty.current = false; setUncertain(false);
        setNotice(`已保存后续默认语言：${languageNames[result.language ?? language]}。已接受运行仍使用原语言快照。`);
        onSaved();
      }
    } catch (cause) {
      setError(cause as Error);
      if (!(cause instanceof ApiError) || cause.status === 0 || cause.status >= 500) setUncertain(true);
      else if (!uncertain && cause.code === 'revision_conflict') { captured.current = undefined; onSaved(); }
    }
  }
  return <details className="advanced"><summary>文档与代码注释语言 · 当前默认{languageNames[product.language ?? 'zh-CN']}</summary>
    <p className="muted">此设置只改变后续新迭代的默认语言。已接受的运行使用各自的语言快照，已确认文档和现有代码不会自动改写。</p>
    <form aria-label="设置产品语言" onSubmit={submit}>
      <Field label="后续迭代默认语言"><select value={language} disabled={command.busy || uncertain || product.management?.can_edit === false} onChange={event => {
        setLanguage(event.target.value as ProductLanguage); dirty.current = true; setNotice(''); captured.current = undefined;
      }}><option value="zh-CN">中文</option><option value="en">English</option></select></Field>
      <ErrorBox error={error} />
      {notice && <p className="notice" role="status">{notice}</p>}
      {uncertain && <p className="notice notice-warning">保存结果尚未确认，将使用原提交标识核对语言设置。</p>}
      <button className="button secondary small" disabled={command.busy || !Number.isInteger(product.revision) || (!uncertain && product.management?.can_edit === false)
        || (!uncertain && language === (product.language ?? 'zh-CN'))}>{command.busy ? '正在保存…' : uncertain ? '核对语言设置' : '保存语言设置'}</button>
    </form>
  </details>;
}

function localProductUrl(value: string | undefined): string | undefined {
  if (!value) return;
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
        || url.username || url.password) return;
    return url.href;
  } catch { return; }
}

export function ProductProgress({ api, product, run, onRun, onApprovals, onRequirements, refresh }: {
  api: OwnerApi; product: Product; run?: Run; onRun: (id: string) => void; onApprovals: (id: string) => void;
  onRequirements: (id: string) => void; refresh: () => void;
}) {
  const launch = useCommand(api); const preparation = useCommand(api); const [downloadBusy, setDownloadBusy] = useState(false); const [error, setError] = useState<Error>();
  const [copied, setCopied] = useState(false);
  useEffect(() => { setCopied(false); setError(undefined); }, [product.id]);
  const delivery = !product.needs_restart && product.state === 'completed' && product.delivery?.source_commit && product.delivery.path
    && product.delivery.archive_download_url && product.delivery.launch_command && product.delivery.working_directory ? product.delivery : undefined;
  const launchState = product.launch;
  const url = launchState?.state === 'running' ? localProductUrl(launchState.url) : undefined;
  const launchUnknown = launchState?.state === 'execution_unknown' || launchState?.state === 'starting';
  const requiresRestart = productRequiresRestart(product);
  const historyOnly = product.needs_restart || Boolean(product.current_change && !product.current_change.run_id);
  const tracked = !historyOnly && run?.id === product.run_id ? run : undefined;
  const working = tracked?.work_items?.filter(item => item.status === 'running') ?? [];
  const restart = product.current_change?.kind === 'restart';
  const retryingChange = product.current_change?.state === 'blocked' && !product.current_change.run_id
    && (product.current_change.config_revision ?? 1) === (product.config_revision ?? 1);
  const canRetry = (!product.needs_restart || (restart && retryingChange)) && (product.finalization_error || retryingChange
    || (product.creation_mode !== 'import' && product.execution_supported !== false && !product.run_id));
  async function download() {
    if (!delivery) return; setDownloadBusy(true); setError(undefined);
    try {
      const result = await api.downloadFile(delivery.archive_download_url);
      const object = URL.createObjectURL(result.blob); const anchor = document.createElement('a');
      anchor.href = object; anchor.download = result.filename ?? `${product.name.replace(/[\\/:*?"<>|\x00-\x1f]/g, '_')}-source.zip`;
      anchor.click(); window.setTimeout(() => URL.revokeObjectURL(object), 1000);
    } catch (cause) { setError(cause as Error); } finally { setDownloadBusy(false); }
  }
  return <Panel title={product.name} subtitle={product.goal} className="product-progress" action={<Status value={requiresRestart ? 'missing_inputs' : product.state} text={requiresRestart ? '需从头运行' : delivery ? '产品已交付' : productStates[product.state]} />}>
    <div className="product-platform-summary">{(product.targets ?? (product.target ? [product.target] : [])).map(target =>
      <span key={target}>{productTargetNames[target]}{nativeProductTarget(target) ? ' · 暂不支持自动研发' : ''}</span>)}
      {product.creation_mode === 'import' && <span>已有项目</span>}</div>
    {product.creation_mode === 'import' && product.project_path && <p className="product-source-location">项目目录：<code>{product.project_path}</code></p>}
    <ProductLanguageSettings key={product.id} api={api} product={product} onSaved={refresh} />
    <div className="product-progress-line" role="status">
      <span className={product.state === 'running' || product.state === 'preparing' ? 'activity-indicator' : 'activity-indicator quiet'} />
      <div><strong>{requiresRestart ? '配置已更新，必须从头运行' : delivery ? '代码与验证结果已准备好' : product.state === 'registered' ? '已有项目已登记，填写需求变更后开始下一次迭代' : product.state === 'preparing' ? restart ? '正在准备从头运行，将从目标整理开始' : '正在准备研发环境与执行计划' : product.state === 'waiting_approval' ? '有阶段产物等待你确认' : product.state === 'blocked' ? '研发已暂停，等待处理以下问题' : product.state === 'cancelled' ? '本次产品创建已取消' : product.state === 'completed' ? '运行已结束，交付信息尚未齐备' : working.length ? working.map(item => stepNames[item.step] ?? item.step).join('、') : 'Agent 正在推进研发任务'}</strong>
        {tracked && <p>已完成 {tracked.work_items?.filter(workflowIsComplete).length ?? tracked.completed_work_count ?? 0} / {tracked.total_work_count ?? tracked.work_items?.length ?? 0} 项工作{working.length > 0 ? ` · ${working.length} 个 Agent 正在执行` : ''}</p>}</div>
    </div>
    {product.blocking_reasons?.length > 0 && <div className="notice notice-warning"><ul>{product.blocking_reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul></div>}
    <div className="inline-actions product-actions">{product.run_id && <button className="button secondary small" onClick={() => onRun(product.run_id!)}>{historyOnly ? '查看历史运行' : '查看 Agent 和研发进展'}</button>}
      <button className="button secondary small" disabled={product.needs_restart} onClick={() => onRequirements(product.id)}>需求变更</button>
      {product.state === 'blocked' && canRetry && !product.restore_reconciliation_required && <button className="button secondary small" disabled={preparation.busy} onClick={async () => {
        setError(undefined);
        try { await preparation.execute(`/api/v1/products/${segment(product.id)}/retry`, {}); refresh(); }
        catch { /* Keep the unresolved command key for an acknowledgement retry. */ }
      }}>{product.finalization_error ? (preparation.busy ? '正在重试导出…' : '重试导出') : retryingChange && restart
        ? (preparation.busy ? '正在重试重跑准备…' : '重试重跑准备') : retryingChange
        ? (preparation.busy ? '正在重试需求准备…' : '重试需求准备') : (preparation.busy ? '正在重试准备…' : '重试准备')}</button>}
      {product.state === 'waiting_approval' && product.run_id && <button className="button" onClick={() => onApprovals(product.run_id!)}>处理待审产物</button>}</div>
    {delivery && <div className="product-delivery" data-testid="product-delivery">
      {launchState?.detail && <div className="notice notice-warning">{launchState.detail}</div>}
      <div className="delivery-location"><span>交付目录</span><code>{delivery.path}</code><button className="text-button" onClick={async () => {
        try { await navigator.clipboard.writeText(delivery.path); setCopied(true); } catch { setError(new Error('无法访问剪贴板，请复制上方目录。')); }
      }}>{copied ? '目录已复制' : '复制目录'}</button></div>
      <div className="inline-actions"><button className="button" disabled={downloadBusy} onClick={() => void download()}>{downloadBusy ? '正在下载…' : '下载产品源码'}</button>
        {!url && !launchUnknown && <button className="button secondary" disabled={launch.busy} onClick={async () => {
          setError(undefined);
          try {
            const result = await launch.execute<Product['launch']>(`/api/v1/products/${segment(product.id)}/launch`, {});
            if (result) { if (result.url && !localProductUrl(result.url)) throw new ApiError('unsafe_launch_url', '服务返回的启动地址不是本机地址。'); refresh(); }
          } catch (cause) { setError(cause as Error); }
        }}>{launch.busy ? '正在启动…' : '启动产品'}</button>}
        {url && <a className="button secondary" href={url} target="_blank" rel="noopener noreferrer">打开产品</a>}
        {launchUnknown && <span role="status">产品启动或停止状态尚待确认</span>}
        {(url || launchUnknown) && <>
          <button className="text-button" disabled={launch.busy} onClick={async () => {
            setError(undefined);
            try { const result = await launch.execute<Product['launch']>(`/api/v1/products/${segment(product.id)}/stop`, {}); if (result) { refresh(); if (result.state === 'execution_unknown') setError(new Error('产品停止状态尚未确认，请查看本机服务状态后再启动。')); } }
            catch (cause) { setError(cause as Error); }
          }}>{launch.busy ? '正在停止…' : '停止产品'}</button></>}</div>
      <details className="launch-instructions"><summary>查看启动说明</summary><p>在以下目录运行启动命令：</p><code>{delivery.working_directory}</code><pre>{delivery.launch_command}</pre>
        <small>交付版本 {delivery.source_commit.slice(0, 12)}</small></details>
    </div>}
    <ErrorBox error={error ?? preparation.error ?? launch.error} />
  </Panel>;
}
