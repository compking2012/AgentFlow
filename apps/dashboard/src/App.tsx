import { Component, useCallback, useEffect, useRef, useState } from 'react';
import type { ErrorInfo, PropsWithChildren } from 'react';
import { ApiError, OwnerApi, segment } from './api';
import { ErrorBox, Status } from './components';
import { useResource, useRunEvents } from './hooks';
import { ProductsView } from './ProductsView';
import { MyProductsView } from './MyProductsView';
import { RequirementsView } from './RequirementsView';
import { ApprovalsView, ExecutionView, QualityView } from './RunViews';
import { SettingsView } from './SettingsView';
import { resolveProjectSelection } from './projectSelection';
import type { ProjectSelection } from './projectSelection';
import type { Approval, Backend, Check, Entity, MatrixRow, Meta, Node, Plan, Profile, Resource, Run, WorkflowProject } from './types';

type Tab = 'products' | 'my_products' | 'requirements' | 'execution' | 'approvals' | 'quality' | 'settings';
const tabs: { key: Tab; label: string; icon: string; description: string }[] = [
  { key: 'products', label: '创建产品', icon: '✦', description: '输入新目标，或导入已有项目' },
  { key: 'my_products', label: '我的产品', icon: '◈', description: '管理已有产品、关键配置与完整重跑，保留每一轮历史' },
  { key: 'requirements', label: '需求变更', icon: '＋', description: '围绕原产品目标，推进一项小范围变更' },
  { key: 'execution', label: '执行台', icon: '⊞', description: '查看依赖、Agent 与研发进展' },
  { key: 'approvals', label: '人工审核', icon: '✓', description: '查看产物，按版本作出决定' },
  { key: 'quality', label: '产物与质量', icon: '▤', description: '从证据判断当前是否可交付' },
  { key: 'settings', label: '本地设置', icon: '⚙', description: '执行额度、模型配置与自有节点' },
];

function Workspace({ api }: { api: OwnerApi }) {
  const [tab, setTab] = useState<Tab>('products'); const [requirementsProductId, setRequirementsProductId] = useState(''); const [selection, setSelection] = useState<ProjectSelection>({});
  const [modelSettingsRequest, setModelSettingsRequest] = useState(0);
  const [myProductId, setMyProductId] = useState('');
  const meta = useResource<Meta>(api, '/api/v1/meta');
  const projects = useResource<WorkflowProject[]>(api, '/api/v1/project_workflows', true);
  const selected = resolveProjectSelection(projects.data ?? [], selection);
  const runId = selected.runId;
  const setRunId = useCallback((id: string) => { setSelection({ runId: id }); projects.refresh(); }, [projects.refresh]);
  const runVisible = ['my_products', 'execution', 'approvals', 'quality'].includes(tab);
  const profiles = useResource<Profile[]>(api, tab === 'settings' ? '/api/v1/model_profiles' : null, true);
  const backends = useResource<Backend[]>(api, tab === 'settings' ? '/api/v1/backends' : null, true);
  const nodes = useResource<Node[]>(api, tab === 'settings' ? '/api/v1/executor_nodes' : null, true);
  const resources = useResource<Resource[]>(api, tab === 'settings' ? '/api/v1/executor_resources' : null, true);
  const detail = useResource<Run>(api, runVisible && runId ? `/api/v1/runs/${segment(runId)}` : null);
  const plan = useResource<Plan>(api, tab === 'quality' && detail.data?.plan_id ? `/api/v1/run_plans/${segment(detail.data.plan_id)}` : null);
  const approvals = useResource<Approval[]>(api, runVisible && runId ? `/api/v1/runs/${segment(runId)}/approvals` : null, true);
  const matrix = useResource<MatrixRow[]>(api, tab === 'quality' && runId ? `/api/v1/runs/${segment(runId)}/target_matrix` : null, true);
  const checks = useResource<Check[]>(api, tab === 'quality' && runId ? `/api/v1/runs/${segment(runId)}/checks` : null, true);
  const candidates = useResource<Entity[]>(api, tab === 'quality' && runId ? `/api/v1/runs/${segment(runId)}/candidates` : null, true);
  const deliveries = useResource<Entity[]>(api, tab === 'quality' && runId ? `/api/v1/runs/${segment(runId)}/deliveries` : null, true);
  const refreshRun = useCallback(() => {
    projects.refresh(); detail.refresh(); approvals.refresh(); matrix.refresh(); checks.refresh(); candidates.refresh(); deliveries.refresh();
  }, [projects.refresh, detail.refresh, approvals.refresh, matrix.refresh, checks.refresh, candidates.refresh, deliveries.refresh]);
  const refreshTimer = useRef(0);
  const onEvent = useCallback(() => {
    if (!refreshTimer.current) refreshTimer.current = window.setTimeout(() => { refreshTimer.current = 0; refreshRun(); }, 350);
  }, [refreshRun]);
  useEffect(() => () => window.clearTimeout(refreshTimer.current), []);
  const eventStatus = useRunEvents(api, runVisible ? runId || null : null, onEvent);
  useEffect(() => {
    if (!selection.runId && !selection.versionId && selected.project && selected.version)
      setSelection({ projectId: selected.project.id, versionId: selected.version.id });
  }, [selection.runId, selection.versionId, selected.project, selected.version]);
  const current = tabs.find(t => t.key === tab)!;
  const pending = approvals.data?.filter(a => !a.stale && a.decision === null).length ?? 0;
  const error = meta.error ?? projects.error ?? detail.error
    ?? (tab === 'settings' ? profiles.error ?? backends.error ?? nodes.error ?? resources.error : undefined)
    ?? (tab === 'quality' ? matrix.error ?? checks.error ?? candidates.error ?? deliveries.error ?? plan.error : undefined)
    ?? (tab === 'approvals' ? approvals.error : undefined);
  function refreshAll() { meta.refresh(); profiles.refresh(); backends.refresh(); nodes.refresh(); resources.refresh(); refreshRun(); }
  return <div className="app-shell">
    <aside className="sidebar"><a className="brand" href="#" onClick={e => { e.preventDefault(); setTab('products'); }}><span className="brand-symbol">A</span><div>AgentFlow<small>本地研发工作台</small></div></a>
      <div className="sidebar-caption">工作空间</div><nav aria-label="主导航">{tabs.map(item => <button key={item.key} className={tab === item.key ? 'nav-item selected' : 'nav-item'} aria-current={tab === item.key ? 'page' : undefined} onClick={() => setTab(item.key)}><span aria-hidden="true">{item.icon}</span>{item.label}{item.key === 'approvals' && pending > 0 && <b>{pending}</b>}</button>)}</nav>
      <div className="sidebar-footer"><span className="local-dot" /> 本机控制 · 单用户<small>{meta.data ? `v${meta.data.version}` : '正在读取服务版本'}</small></div>
    </aside>
    <div className="workspace"><header className="topbar"><span className="workspace-label">LOCAL WORKSPACE</span><span className="session-label">本地所有者会话</span></header>
      <main id="main-content"><div className="page-heading"><div><div className="eyebrow">AGENTFLOW / {current.label}</div><h1>{current.label}</h1><p>{current.description}</p></div><button className="button secondary small" onClick={refreshAll}>刷新数据</button></div>
        <div className="run-selector project-workflow-selector" hidden={tab === 'products' || tab === 'my_products' || tab === 'requirements'}>
          <label>项目<select aria-label="项目" value={selected.project?.id ?? ''} onChange={e => setSelection({ projectId: e.target.value })}>
            <option value="">{runId ? '正在定位项目…' : '尚未选择项目'}</option>
            {(projects.data ?? []).filter(project => !project.deleted || project.id === selected.project?.id).map(project =>
              <option value={project.id} key={project.id}>{project.name}{project.deleted ? ' · 已删除项目历史' : ''}</option>)}
          </select></label>
          <label>需求变更<select aria-label="需求变更" value={selected.version?.id ?? ''} disabled={!selected.project} onChange={e => setSelection({ projectId: selected.project?.id, versionId: e.target.value })}>
            {!selected.version && <option value="">尚未选择需求变更</option>}
            {(selected.project?.versions ?? []).map(version => <option value={version.id} key={version.id}>{version.label}</option>)}
          </select></label>
          <span className="live-state"><span className={eventStatus === '实时连接' ? 'live-dot' : 'offline-dot'} />{runId ? eventStatus : '尚未创建运行'}</span>
          {detail.data && <Status value={detail.data.execution_state} />}
        </div>
        <ErrorBox error={error} onRetry={refreshAll} />
        {meta.loading && !meta.data && <p role="status" className="loading">正在读取本地工作空间…</p>}
        <div hidden={tab !== 'products'}><ProductsView api={api} active={tab === 'products'} modelSettingsRequest={modelSettingsRequest} onCreated={product => {
          setMyProductId(product.id); if (product.run_id) setRunId(product.run_id); else setSelection({ projectId: 'product:' + product.id }); projects.refresh(); setTab('my_products');
        }} /></div>
        <div hidden={tab !== 'my_products'}><MyProductsView api={api} active={tab === 'my_products'} selectedProductId={myProductId}
          onSelectProduct={setMyProductId} onCreate={() => setTab('products')} run={detail.data} onTrack={setRunId}
          onRun={id => { setRunId(id); setTab('execution'); }}
          onApprovals={id => { setRunId(id); refreshRun(); setTab('approvals'); }}
          onRequirements={id => { setRequirementsProductId(id); setTab('requirements'); }} /></div>
        <div hidden={tab !== 'requirements'}><RequirementsView api={api} active={tab === 'requirements'} selectedProductId={requirementsProductId}
          onSelectProduct={setRequirementsProductId}
          onRun={id => { setRunId(id); setTab('execution'); }} /></div>
        {tab === 'execution' && <ExecutionView key={`${selected.project?.id}:${selected.version?.id}:${runId}`} api={api} run={detail.data} project={selected.project} version={selected.version} meta={meta.data} onRefresh={refreshRun} />}
        {tab === 'approvals' && <ApprovalsView key={runId} api={api} run={detail.data} approvals={approvals.data ?? []} onRefresh={refreshRun} />}
        {tab === 'quality' && <QualityView key={runId} api={api} run={detail.data} plan={plan.data} matrix={matrix.data ?? []} checks={checks.data ?? []} candidates={candidates.data ?? []} deliveries={deliveries.data ?? []} />}
        <div hidden={tab !== 'settings'}><SettingsView api={api} active={tab === 'settings'} meta={meta.data} profiles={profiles.data ?? []} backends={backends.data ?? []} nodes={nodes.data ?? []} resources={resources.data ?? []} onProductSetup={() => { setModelSettingsRequest(value => value + 1); setTab('products'); }} onExecution={() => setTab('execution')} refresh={() => { nodes.refresh(); resources.refresh(); }} /></div>
        <footer className="page-footer">状态与产物来自本地服务 · 等待审核、测试通过与代码交付分别记录</footer>
      </main>
    </div>
  </div>;
}

export function App({ api, bootstrap }: { api: OwnerApi; bootstrap: string | null }) {
  const [authenticated, setAuthenticated] = useState(false); const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error>(); const used = useRef(false);
  const reconnect = useCallback(() => {
    setLoading(true); setError(undefined);
    api.restore().then(setAuthenticated).catch(setError).finally(() => setLoading(false));
  }, [api]);
  useEffect(() => {
    api.onExpired = () => { setAuthenticated(false); setError(new Error('本机连接已结束，请重新连接工作台。')); };
    if (used.current) return;
    used.current = true;
    (bootstrap ? api.exchange(bootstrap).then(() => true) : api.restore())
      .then(setAuthenticated).catch(setError).finally(() => setLoading(false));
  }, [api, bootstrap]);
  if (authenticated) return <Workspace api={api} />;
  return <main className="connection-page"><section className="connection-card"><span className="brand-symbol">A</span><div className="eyebrow">AGENTFLOW · LOCAL</div><h1>{loading ? '正在连接工作台' : '打开本机工作台'}</h1>
    <p>服务运行时，直接访问当前网址即可连接，无需启动链接。</p>
    {error instanceof ApiError && error.code === 'connection_error' && <p className="muted">若本机服务尚未运行，请先运行 <code>agentflow start</code> 启动服务。</p>}
    {!loading && <button className="button" onClick={reconnect}>重新连接</button>}
    <ErrorBox error={error} />{loading && <p role="status" className="loading">正在续接本机连接…</p>}
  </section></main>;
}

export class ErrorBoundary extends Component<PropsWithChildren, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(_error: Error, _info: ErrorInfo) { /* Do not log potentially sensitive server or artifact data. */ }
  render() { return this.state.failed ? <main className="connection-page"><section className="connection-card"><h1>页面无法读取当前数据</h1><p>请刷新页面重新连接。未确认的操作不会由页面自动重试。</p><button className="button" onClick={() => window.location.reload()}>重新连接</button></section></main> : this.props.children; }
}
