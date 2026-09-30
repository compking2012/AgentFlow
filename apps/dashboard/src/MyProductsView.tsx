import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError, OwnerApi, segment } from './api';
import { Empty, ErrorBox, Field, Panel, Status } from './components';
import { useDialogFocus, useResource } from './hooks';
import { modelRequestLimit } from './labels';
import { ProductProgress, productRequiresRestart, productStates } from './ProductProgress';
import { ProductTargets, productTargetNames } from './ProductTargets';
import type { Product, ProductLanguage, ProductTarget, Run } from './types';

type Action = 'rename' | 'edit' | 'delete' | 'restore' | 'restart';
type Selection = { action: Action; product: Product };
const actionTitles: Record<Action, string> = {
  rename: '修改产品名称', edit: '编辑产品配置', delete: '删除产品', restore: '恢复产品', restart: '从头运行产品',
};
const targetsOf = (product: Product): ProductTarget[] => product.targets ?? (product.target ? [product.target] : []);
const sameTargets = (a: ProductTarget[], b: ProductTarget[]) => JSON.stringify([...a].sort()) === JSON.stringify([...b].sort());
const permission = (product: Product | undefined, action: Action) => Boolean(product?.management?.[
  action === 'delete' ? 'can_delete' : action === 'restore' ? 'can_restore' : action === 'restart' ? 'can_restart' : 'can_edit']);

function ProductActionDialog({ api, selection, current, onClose, onSaved, onRefresh }: {
  api: OwnerApi; selection: Selection; current?: Product; onClose: () => void;
  onSaved: (action: Action, productId: string) => void; onRefresh: () => void;
}) {
  const { action, product } = selection;
  const [name, setName] = useState(product.name); const [goal, setGoal] = useState(product.goal);
  const [targets, setTargets] = useState<ProductTarget[]>(targetsOf(product));
  const [language, setLanguage] = useState<ProductLanguage>(product.language ?? 'zh-CN');
  const [reviewMode, setReviewMode] = useState(product.review_mode ?? 'auto');
  const [requests, setRequests] = useState(String(product.max_model_requests ?? 200));
  const [reason, setReason] = useState(''); const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error>(); const [uncertain, setUncertain] = useState(false);
  const pending = useRef<{ signature: string; key: string } | undefined>(undefined);
  const submitting = useRef(false);
  useDialogFocus(true, onClose, busy || uncertain);
  const stale = Boolean(current && current.revision !== product.revision);
  const targetChanged = !sameTargets(targets, targetsOf(product));
  const critical = goal.trim() !== product.goal || targetChanged;
  const count = Number(requests);
  const countValid = /^\d+$/.test(requests) && Number.isInteger(count) && count >= 0 && count <= 2000;
  const changes: Record<string, unknown> = {};
  if (action === 'rename' && name.trim() !== product.name) changes.name = name.trim();
  if (action === 'edit') {
    if (goal.trim() !== product.goal) changes.goal = goal.trim();
    if (targetChanged) changes.targets = targets;
    if (language !== (product.language ?? 'zh-CN')) changes.language = language;
    if (reviewMode !== (product.review_mode ?? 'auto')) changes.review_mode = reviewMode;
    if (count !== (product.max_model_requests ?? 200)) changes.max_model_requests = count;
  }
  const valid = action === 'rename' ? Boolean(name.trim()) && name.trim().length <= 100 && Object.keys(changes).length > 0
    : action === 'edit' ? goal.trim().length >= 8 && countValid && (!targetChanged || targets.length > 0) && Object.keys(changes).length > 0 : true;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current || !Number.isInteger(product.revision) || !valid
        || (!uncertain && (stale || !permission(current ?? product, action)))) return;
    const path = `/api/v1/products/${segment(product.id)}${action === 'restore' ? '/restore' : action === 'restart' ? '/restart' : ''}`;
    const method = action === 'delete' ? 'DELETE' : action === 'restore' || action === 'restart' ? 'POST' : 'PATCH';
    const payload = { expected_revision: product.revision, ...changes, ...(reason.trim() ? { reason: reason.trim() } : {}) };
    const signature = JSON.stringify({ path, method, payload });
    if (pending.current?.signature !== signature) pending.current = { signature, key: crypto.randomUUID() };
    submitting.current = true; setBusy(true); setError(undefined);
    try {
      const result = await api.command<Record<string, unknown>>(path, payload, pending.current.key, method);
      const validReceipt = result && typeof result.id === 'string' && result.id.length > 0 && (action === 'restart'
        ? result.product_id === product.id && result.kind === 'restart'
          && typeof result.state === 'string' && ['preparing', 'running', 'waiting_approval', 'blocked', 'completed', 'cancelled'].includes(result.state)
        : result.id === product.id && Number.isInteger(result.revision) && Number(result.revision) >= product.revision!);
      if (!validReceipt) throw new ApiError('invalid_response', '操作回执无法确认，请核对原操作。', 502);
      pending.current = undefined; setUncertain(false); onSaved(action, product.id);
    } catch (cause) {
      setError(cause as Error);
      if (!(cause instanceof ApiError) || cause.status === 0 || cause.status >= 500) setUncertain(true);
      onRefresh();
    } finally { submitting.current = false; setBusy(false); }
  }

  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy && !uncertain) onClose(); }}>
    <section className="modal my-products-dialog" role="dialog" aria-modal="true" aria-labelledby="product-action-title">
      <h2 id="product-action-title">{actionTitles[action]}</h2><p className="muted">{product.name}</p>
      <form aria-label={actionTitles[action]} onSubmit={submit}>
        <fieldset disabled={busy || uncertain} className="my-products-dialog-fields">
          {action === 'rename' && <Field label="新的产品名称"><input required value={name} maxLength={100} onChange={event => setName(event.target.value)} /></Field>}
          {action === 'edit' && <>
            <Field label="长期产品目标"><textarea rows={5} required minLength={8} maxLength={16000} value={goal} onChange={event => setGoal(event.target.value)} /></Field>
            <ProductTargets targets={targets} onChange={setTargets} />
            <div className="form-grid"><Field label="后续文档与代码注释语言"><select value={language} onChange={event => setLanguage(event.target.value as ProductLanguage)}>
              <option value="zh-CN">中文</option><option value="en">English</option></select></Field>
              <Field label="后续人工参与方式"><select value={reviewMode} onChange={event => setReviewMode(event.target.value as typeof reviewMode)}>
                <option value="auto">自动推进</option><option value="milestones">关键节点确认</option><option value="every_step">每阶段确认</option></select></Field></div>
            <Field label="后续模型调用次数上限" hint="0 表示不限次数；1 至 2000 为有限上限。"><input type="number" min="0" max="2000" step="1" required value={requests} onChange={event => setRequests(event.target.value)} /></Field>
            <div className={`notice${critical ? ' notice-warning' : ''}`} role="status">{critical
              ? '目标或平台已改变。保存后必须从目标整理开始完整重跑；保存本身不会调用模型。'
              : '保存只更新后续配置，不启动模型，不修改已有运行和已确认产物。'}</div>
          </>}
          {action === 'delete' && <div className="notice notice-warning"><strong>移入“已删除产品”，可恢复</strong>
            <p>不会删除磁盘源码、交付目录或历史运行记录。恢复产品也不会自动启动研发。</p></div>}
          {action === 'restore' && <div className="notice"><p>将此产品恢复到正常列表。磁盘源码和历史记录保持原样，不自动启动研发。</p></div>}
          {action === 'restart' && <><div className="notice notice-warning"><strong>从目标整理开始完整运行</strong>
            <p>将重新执行目标、调研、PRD、架构、开发、审查和测试。旧运行与交付保留，点击下方按钮才开始。</p></div>
            <div className="my-products-restart-summary"><p>{product.goal}</p>
              <small>{targetsOf(product).map(target => productTargetNames[target]).join(' · ')} · 模型调用：{modelRequestLimit(product.max_model_requests)}</small></div></>}
          {action !== 'rename' && <Field label="操作说明（可选）"><textarea rows={2} maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} /></Field>}
        </fieldset>
        {stale && !uncertain && <p className="hint-warning" role="status">产品已更新，请关闭后重新打开，核对最新版本再提交。</p>}
        {uncertain && <p className="notice notice-warning" role="status">操作结果尚未确认。请核对原操作，页面会沿用同一提交标识。</p>}
        <ErrorBox error={error} />
        <div className="form-actions"><button type="button" className="button secondary" disabled={busy || uncertain} onClick={onClose}>返回</button>
          <button className={`button${action === 'delete' ? ' danger' : ''}`} disabled={busy || !valid || !Number.isInteger(product.revision)
            || (!uncertain && (stale || !permission(current ?? product, action)))}>{busy ? '正在提交…' : uncertain ? '核对原操作'
              : action === 'delete' ? '移入已删除产品' : action === 'restore' ? '恢复到我的产品'
              : action === 'restart' ? '开始从头运行' : '保存更改'}</button></div>
      </form>
    </section>
  </div>;
}

export function MyProductsView({ api, active, selectedProductId, onSelectProduct, onCreate, run, onTrack, onRun, onApprovals, onRequirements }: {
  api: OwnerApi; active: boolean; selectedProductId: string; onSelectProduct: (id: string) => void; onCreate: () => void;
  run?: Run; onTrack: (id: string) => void; onRun: (id: string) => void;
  onApprovals: (id: string) => void; onRequirements: (id: string) => void;
}) {
  const products = useResource<Product[]>(api, active ? '/api/v1/products?view=all' : null, true, 2500);
  const detail = useResource<Product>(api, active && selectedProductId ? `/api/v1/products/${segment(selectedProductId)}` : null, false, 2000);
  const [deleted, setDeleted] = useState(false); const [search, setSearch] = useState('');
  const [selection, setSelection] = useState<Selection>(); const [notice, setNotice] = useState('');
  const listed = products.data?.find(product => product.id === selectedProductId);
  const current = detail.data && listed && (listed.revision ?? 0) > (detail.data.revision ?? 0) ? listed : detail.data ?? listed;
  const visible = (products.data ?? []).filter(product => Boolean(product.deleted_at) === deleted
    && `${product.name} ${product.goal}`.toLowerCase().includes(search.toLowerCase().trim()));
  useEffect(() => {
    if (current && !selection) setDeleted(Boolean(current.deleted_at));
  }, [current?.id, current?.deleted_at]);
  useEffect(() => {
    if (!selectedProductId && visible.length && !selection) onSelectProduct(visible[0].id);
  }, [selectedProductId, visible, selection, onSelectProduct]);
  useEffect(() => { if (active && current?.run_id && !current.deleted_at && !current.needs_restart) onTrack(current.run_id); },
    [active, current?.run_id, current?.deleted_at, current?.needs_restart, onTrack]);
  function refresh() { detail.refresh(); products.refresh(); }
  function changeView(value: boolean) {
    setDeleted(value); setSearch(''); setNotice('');
    const next = products.data?.find(product => Boolean(product.deleted_at) === value);
    onSelectProduct(next?.id ?? '');
  }
  function saved(action: Action, productId: string) {
    setSelection(undefined); onSelectProduct(productId); refresh();
    if (action === 'delete') setDeleted(true);
    if (action === 'restore') setDeleted(false);
    setNotice(action === 'delete' ? '产品已移入已删除列表，磁盘源码和历史记录保留。'
      : action === 'restore' ? '产品已恢复，尚未启动研发。'
      : action === 'restart' ? '从头运行请求已接受，可查看准备状态和后续执行。'
      : action === 'rename' ? '产品名称已更新。' : '产品配置已保存。目标或平台变更需要从头运行，保存未启动模型。');
  }
  return <div className="my-products-page">
    <div className="my-products-toolbar"><div className="my-products-tabs" aria-label="产品列表范围">
      <button className={deleted ? 'button secondary small' : 'button small'} aria-pressed={!deleted} onClick={() => changeView(false)}>使用中的产品</button>
      <button className={deleted ? 'button small' : 'button secondary small'} aria-pressed={deleted} onClick={() => changeView(true)}>已删除产品</button></div>
      <button className="button secondary" onClick={onCreate}>创建或导入产品</button></div>
    {notice && <p className="notice" role="status">{notice}</p>}
    <ErrorBox error={products.error ?? detail.error} onRetry={refresh} />
    <div className="my-products-layout"><Panel title={deleted ? '已删除产品' : '产品列表'} className="my-products-list-panel">
      <Field label="搜索产品"><input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="按名称或目标搜索" /></Field>
      {products.loading && !products.data && <p role="status" className="loading">正在读取已有产品…</p>}
      {visible.length ? <div className="products-list">{visible.map(product => <button key={product.id}
        className={product.id === selectedProductId ? 'product-list-item selected' : 'product-list-item'}
        aria-pressed={product.id === selectedProductId} onClick={() => onSelectProduct(product.id)}>
        <span><strong>{product.name}</strong><small>{product.goal}</small></span><Status value={productRequiresRestart(product) ? 'missing_inputs' : product.state}
          text={product.deleted_at ? '已删除' : productRequiresRestart(product) ? '需从头运行' : productStates[product.state]} /></button>)}</div>
        : !products.loading && <Empty title={search ? '没有匹配的产品' : deleted ? '暂无已删除产品' : '还没有产品'}>
          {deleted ? '删除的产品会保留在这里，可恢复到正常列表。' : '从创建页面新建产品，或导入已有项目。'}</Empty>}
    </Panel><div className="my-products-detail">
      {current ? <>
        <div className="my-products-actions" aria-label="产品管理操作">{current.deleted_at
          ? <button className="button" disabled={!permission(current, 'restore')} onClick={() => setSelection({ action: 'restore', product: current })}>恢复产品</button>
          : <><button className="button secondary small" disabled={!permission(current, 'rename')} onClick={() => setSelection({ action: 'rename', product: current })}>改名</button>
            <button className="button secondary small" disabled={!permission(current, 'edit')} onClick={() => setSelection({ action: 'edit', product: current })}>编辑配置</button>
            <button className="button" disabled={!permission(current, 'restart')} onClick={() => setSelection({ action: 'restart', product: current })}>从头运行</button>
            <button className="button danger-quiet small" disabled={!permission(current, 'delete')} onClick={() => setSelection({ action: 'delete', product: current })}>删除产品</button></>}</div>
        {Boolean(current.management?.blocked_reasons.length) && <div className="notice notice-warning"><strong>当前操作条件</strong><ul>
          {current.management!.blocked_reasons.map((reason, index) => <li key={`${reason.code}-${index}`}>{reason.message}</li>)}</ul></div>}
        {current.deleted_at ? <Panel title={current.name} subtitle={current.goal} action={<Status value="revoked" text="已删除" />}>
          <p>此产品已移入已删除列表。磁盘源码、交付目录与历史记录仍保留；恢复不会自动启动运行。</p>
          {current.project_path && <p className="my-products-source">源码目录：<code>{current.project_path}</code></p>}
          {current.run_id && <button className="button secondary small" onClick={() => onRun(current.run_id!)}>查看历史运行</button>}
        </Panel> : <ProductProgress api={api} product={current} run={run} onRun={onRun} onApprovals={onApprovals}
          onRequirements={onRequirements} refresh={refresh} />}
      </> : <Panel title="产品详情"><Empty title="选择一个产品">在左侧列表查看现有产品，或创建、导入一个新产品。</Empty></Panel>}
    </div></div>
    {selection && <ProductActionDialog api={api} selection={selection} current={current?.id === selection.product.id ? current : undefined}
      onClose={() => setSelection(undefined)} onSaved={saved} onRefresh={refresh} />}
  </div>;
}
