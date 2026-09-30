import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError, OwnerApi, segment } from './api';
import { Empty, ErrorBox, Field, Panel, Status } from './components';
import { useCommand, useResource } from './hooks';
import { nativeProductTarget, productTargetNames } from './ProductTargets';
import type { Product, ProductChange, ProductLanguage } from './types';

const changeStates: Record<string, string> = {
  registered: '需求已登记', preparing: '正在准备', queued: '等待处理', running: '研发中',
  waiting_approval: '等待审核', blocked: '需要处理', completed: '已完成', cancelled: '已取消',
};

export function RequirementsView({ api, active, selectedProductId, onSelectProduct, onRun }: {
  api: OwnerApi; active: boolean; selectedProductId: string; onSelectProduct: (id: string) => void; onRun: (id: string) => void;
}) {
  const products = useResource<Product[]>(api, active ? '/api/v1/products' : null, true, 2500);
  const [productId, setProductId] = useState(selectedProductId);
  const detail = useResource<Product>(api, active && productId ? `/api/v1/products/${segment(productId)}` : null, false, 2500);
  const history = useResource<ProductChange[]>(api, active && productId ? `/api/v1/products/${segment(productId)}/changes` : null, true, 2500);
  const listed = products.data?.find(product => product.id === productId);
  const product = detail.data && listed && (listed.revision ?? 0) > (detail.data.revision ?? 0) ? listed : detail.data ?? listed;
  const [title, setTitle] = useState(''); const [description, setDescription] = useState('');
  const [acceptance, setAcceptance] = useState('');
  const [language, setLanguage] = useState<ProductLanguage>('zh-CN');
  const languageEdited = useRef(false);
  const [submitted, setSubmitted] = useState<ProductChange>(); const [error, setError] = useState<Error>();
  const [uncertain, setUncertain] = useState(false);
  const command = useCommand(api);
  const recovery = useCommand(api);
  const captured = useRef<{ signature: string; revision: number } | undefined>(undefined);
  useEffect(() => { if (selectedProductId && !uncertain) setProductId(selectedProductId); }, [selectedProductId, uncertain]);
  useEffect(() => {
    if (!productId && !selectedProductId && products.data?.length) {
      setProductId(products.data[0].id); onSelectProduct(products.data[0].id);
    }
  }, [productId, selectedProductId, products.data, onSelectProduct]);
  useLayoutEffect(() => {
    setTitle(''); setDescription(''); setAcceptance(''); setSubmitted(undefined); setError(undefined); setUncertain(false); captured.current = undefined;
    languageEdited.current = false; setLanguage('zh-CN');
  }, [productId]);
  useLayoutEffect(() => {
    if (product && !languageEdited.current && !uncertain && !command.busy) setLanguage(product.language ?? 'zh-CN');
  }, [product?.id, product?.language, uncertain, command.busy]);
  const unsupported = product?.execution_supported === false;
  const activeRun = product && ['preparing', 'running', 'waiting_approval'].includes(product.state);
  const restoring = product?.restore_reconciliation_required;
  const notEligible = product?.can_add_change === false;
  const previewActive = product?.launch && ['running', 'starting', 'execution_unknown'].includes(product.launch.state);
  const cannotStart = unsupported || activeRun || restoring || notEligible || previewActive;
  const changedRevision = error instanceof ApiError && error.code === 'revision_conflict';
  const changes = submitted?.product_id === productId && !history.data?.some(change => change.id === submitted.id)
    ? [submitted, ...(history.data ?? [])] : history.data ?? [];

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!product || (!uncertain && cannotStart) || !Number.isInteger(product.revision) || description.trim().length < 8) return;
    setError(undefined);
    const signature = JSON.stringify({ product: product.id, title: title.trim(), description: description.trim(), acceptance: acceptance.trim(), language });
    if (captured.current?.signature !== signature) captured.current = { signature, revision: product.revision! };
    try {
      const response = await command.execute<ProductChange>(`/api/v1/products/${segment(product.id)}/changes`, {
        ...(title.trim() ? { title: title.trim() } : {}), description: description.trim(),
        language,
        ...(acceptance.trim() ? { acceptance_criteria: acceptance.trim() } : {}), expected_revision: captured.current.revision,
      });
      if (response) {
        setSubmitted(response); setTitle(''); setDescription(''); setAcceptance(''); setUncertain(false); captured.current = undefined;
        languageEdited.current = false; setLanguage(product.language ?? 'zh-CN');
        products.refresh(); detail.refresh(); history.refresh();
      }
    } catch (cause) {
      setError(cause as Error);
      if (!(cause instanceof ApiError) || cause.status === 0 || cause.status >= 500) setUncertain(true);
    }
  }

  return <div className="requirements-page">
    <div className="requirements-intro"><h2>在原有产品上，完成下一项需求</h2>
      <p>聚焦一个功能或一次改进。产品目标保持不变，Agent 从 PRD 更新开始，再检查架构与接口影响。</p></div>
    <ErrorBox error={products.error ?? detail.error} onRetry={() => { products.refresh(); detail.refresh(); }} />
    {products.loading && !products.data && <p className="loading" role="status">正在读取已有产品…</p>}
    {!products.loading && products.data?.length === 0 ? <Panel title="尚无可选产品"><Empty title="先创建或导入一个产品">在“创建产品”中登记产品目标后，就可以持续添加需求。</Empty></Panel>
      : <div className="requirements-layout"><Panel title="描述本次变更" className="requirements-form-panel">
        <form aria-label="需求变更" onSubmit={submit}>
          <Field label="选择产品"><select required value={productId} disabled={command.busy || uncertain} onChange={event => {
            setProductId(event.target.value); onSelectProduct(event.target.value);
          }}>
            <option value="">请选择已有产品</option>{(products.data ?? []).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
          {product && <div className="requirements-product-context"><strong>原产品目标</strong><p>{product.goal}</p>
            <small>{(product.targets ?? (product.target ? [product.target] : [])).map(target => productTargetNames[target]).join(' · ')} · 本次提交不会替换此目标</small></div>}
          <fieldset disabled={!product || product.id !== productId || command.busy || uncertain} className="requirements-fields">
            <Field label="需求标题（可选）"><input maxLength={100} value={title} onChange={event => setTitle(event.target.value)} placeholder="例如：为事项增加归档筛选" /></Field>
            <Field label="这次要新增或修改什么" hint="说明要改变的行为、使用场景和验收条件。尽量一次只处理一项小范围需求。">
              <textarea required minLength={8} maxLength={16000} rows={6} value={description} onChange={event => setDescription(event.target.value)}
                placeholder="例如：在事项列表增加归档筛选，默认仅显示未归档事项；切换后能够查看并恢复已归档项。" /></Field>
            <Field label="验收标准（可选）" hint="描述完成后应能验证的行为，包含必要的边界与异常情况。"><textarea rows={3} maxLength={16000}
              value={acceptance} onChange={event => setAcceptance(event.target.value)} placeholder="例如：切换筛选不丢失已输入内容；归档和恢复后列表立即更新。" /></Field>
            <Field label="本次文档与代码注释语言" hint="默认沿用产品设置；在此更改只适用于本次新迭代，不会改写已确认产物。"><select value={language}
              onChange={event => { languageEdited.current = true; setLanguage(event.target.value as ProductLanguage); }}>
              <option value="zh-CN">中文</option><option value="en">English</option></select></Field>
          </fieldset>
          {unsupported && <p className="requirements-blocker" role="status">该产品当前还不具备自动研发条件，请先处理项目诊断中的问题。{product?.targets?.some(nativeProductTarget) ? '原生平台暂不支持启动研发。' : ''}</p>}
          {activeRun && <p className="requirements-blocker" role="status">该产品仍有正在推进或等待审核的运行。处理完成后，再提交下一项需求。</p>}
          {restoring && <p className="requirements-blocker" role="status">该产品的恢复状态尚未核对，暂不能开始新需求。</p>}
          {previewActive && <p className="requirements-blocker" role="status">请先在产品页停止预览，再提交需求变更。</p>}
          {notEligible && !unsupported && !activeRun && !restoring && !previewActive && <p className="requirements-blocker" role="status">该产品尚有未结束的运行或基线待处理，请先在执行台核对。</p>}
          <ErrorBox error={error} />
          {uncertain && <p className="requirements-blocker" role="status">提交结果尚未确认。先核对原需求提交，再修改内容；核对会使用同一提交标识，不会新建另一项需求。</p>}
          {changedRevision && !uncertain && <button type="button" className="text-button" onClick={() => {
            captured.current = undefined; setError(undefined); command.clearError(); detail.refresh(); products.refresh();
          }}>重新读取产品版本</button>}
          <div className="form-actions"><span className="muted">从 PRD 开始更新，沿用当前产品的人审配置。</span>
            <button className="button" disabled={command.busy || !product || !Number.isInteger(product.revision)
              || (!uncertain && cannotStart) || description.trim().length < 8}>
              {command.busy ? '正在提交需求…' : uncertain ? '核对原需求提交' : '提交需求变更'}</button></div>
        </form>
      </Panel><Panel title="本次迭代怎么推进" className="requirements-process"><ol>
        <li><strong>更新 PRD 与需求项</strong><p>结合原目标和已有产物，明确这次变更的范围与验收条件。</p></li>
        <li><strong>检查架构与接口</strong><p>核对已有架构是否仍然适用；需要调整时更新相应设计。</p></li>
        <li><strong>开发、审查与验证</strong><p>拆解开发任务，通过代码审查、单元测试和集成测试后交付。</p></li>
      </ol><p className="requirements-scope-note">若需求已经改变产品服务的用户或核心目标，建议创建另一个产品。</p></Panel></div>}
    {productId && <Panel title="这个产品的需求记录" subtitle="展示已提交的变更及其真实运行状态。" className="requirements-history">
      <ErrorBox error={history.error} onRetry={history.refresh} />
      <ErrorBox error={recovery.error} />
      {changes.length ? <div>{changes.map(change => <article className="requirements-change" key={change.id}>
        <header><h3>{change.title || '需求变更'}</h3><Status value={change.state} text={changeStates[change.state]} /></header>
        <p>{change.description}</p>{change.acceptance_criteria && <div className="requirements-acceptance"><strong>验收标准</strong><p>{change.acceptance_criteria}</p></div>}<small>{change.start_stage === 'goal' ? '从目标整理开始 · 完整重跑' : '从 PRD 开始 · 检查已有架构'}{change.language ? ` · 文档与注释：${change.language === 'en' ? 'English' : '中文'}` : ''}</small>
        {change.blocking_reasons?.length > 0 && <ul className="requirements-blocker">{change.blocking_reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}
        {change.run_id && <button className="button secondary small" onClick={() => onRun(change.run_id!)}>查看这次需求的研发进展</button>}
        {product?.current_change_id === change.id && change.state === 'blocked' && !change.run_id && !restoring && !unsupported
          && <button className="button secondary small" disabled={recovery.busy} onClick={async () => {
            try { await recovery.execute(`/api/v1/products/${segment(productId)}/retry`, {}); detail.refresh(); products.refresh(); history.refresh(); }
            catch { /* Keep the same recovery operation for an acknowledgement retry. */ }
          }}>{recovery.busy ? '正在重试需求准备…' : '重试本次需求准备'}</button>}
      </article>)}</div> : !history.loading && <Empty title="还没有需求变更">提交后，需求说明和对应运行会保留在这里。</Empty>}
    </Panel>}
  </div>;
}
