import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { OwnerApi } from './api';
import { ErrorBox, Field, Panel } from './components';
import { useCommand, useResource } from './hooks';
import { modelRequestLimit } from './labels';
import { nativeProductTarget, ProductTargets, productTargetNames } from './ProductTargets';
import { recordId } from './types';
import type { Product, ProductImportDiagnosis, ProductLanguage, ProductSetup, ProductTarget } from './types';

function ModelSetup({ api, setup, active, onSaved }: { api: OwnerApi; setup?: ProductSetup; active: boolean; onSaved: () => void }) {
  const [role, setRole] = useState<'roles' | 'coding' | 'both'>('roles');
  const [provider, setProvider] = useState<'deepseek' | 'openai_compatible'>('deepseek');
  const [baseUrl, setBaseUrl] = useState('https://api.deepseek.com');
  const [model, setModel] = useState(''); const [apiKey, setApiKey] = useState('');
  const [credentialEnv, setCredentialEnv] = useState(''); const [useEnvironment, setUseEnvironment] = useState(false);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<Error>(); const [notice, setNotice] = useState('');
  const submitting = useRef(false);
  useEffect(() => { if (!active) setApiKey(''); }, [active]);
  async function submit(event: FormEvent) {
    event.preventDefault(); if (submitting.current) return;
    submitting.current = true; setBusy(true); setError(undefined); setNotice('');
    const secret = apiKey;
    const payload = { role, provider, base_url: baseUrl.trim(), model: model.trim(),
      ...(useEnvironment ? { credential_env: credentialEnv.trim() } : { api_key: secret }) };
    setApiKey('');
    try {
      await api.command('/api/v1/product_setup/models', payload, crypto.randomUUID());
      setNotice('模型配置已保存，正在重新检查可执行条件。'); onSaved();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : '模型设置未完成';
      // Do not retain the credential in a retry signature or expose echoed error payloads.
      setError(new Error(secret ? message.split(secret).join('••••') : message));
    } finally { submitting.current = false; setBusy(false); }
  }
  function profileName(id: string | null | undefined) {
    const profile = setup?.profiles.find(value => recordId(value) === id);
    return profile?.accepted_api_model ?? profile?.requested_model ?? '尚未设置';
  }
  return <div className="model-setup">
    <p className="muted">模型设置统一保存到 ~/.config/agentflow/config.toml。专业角色与编码模型可以分别配置。</p>
    <div className="model-binding-summary"><div><span>分析与规划</span><strong>{profileName(setup?.model_bindings.role_model_profile_id)}</strong></div>
      <div><span>编码开发</span><strong>{profileName(setup?.model_bindings.coding_model_profile_id)}</strong></div></div>
    <form aria-label="设置研发模型" onSubmit={submit}>
      <div className="form-grid"><Field label="模型用途"><select value={role} onChange={event => setRole(event.target.value as typeof role)}>
        <option value="roles">分析、规划与审查</option><option value="coding">编码开发</option><option value="both">两种用途使用同一模型</option></select></Field>
        <Field label="模型服务"><select value={provider} onChange={event => { const value = event.target.value as typeof provider; setProvider(value); setBaseUrl(value === 'deepseek' ? 'https://api.deepseek.com' : ''); setApiKey(''); setCredentialEnv(''); }}>
          <option value="deepseek">DeepSeek</option><option value="openai_compatible">兼容 OpenAI 的接口</option></select></Field></div>
      <Field label="接口地址"><input type="url" required value={baseUrl} onChange={event => { setBaseUrl(event.target.value); setApiKey(''); setCredentialEnv(''); }} placeholder="https://…" autoComplete="off" spellCheck={false} /></Field>
      <Field label="模型名称" hint={role === 'both' ? '同一模型需要同时支持 Chat Completions 与 Responses。' : role === 'coding' ? '编码模型需要支持 Responses 接口。' : '专业角色使用 Chat Completions 接口。'}>
        <input required value={model} onChange={event => setModel(event.target.value)} maxLength={200} placeholder="填写服务实际提供的模型名称" autoComplete="off" /></Field>
      <label className="inline-check"><input type="checkbox" checked={useEnvironment} onChange={event => { setUseEnvironment(event.target.checked); setApiKey(''); }} />使用已有环境变量</label>
      {useEnvironment ? <Field label="凭据环境变量"><input required value={credentialEnv} onChange={event => setCredentialEnv(event.target.value)} pattern="[A-Za-z_][A-Za-z0-9_]*" placeholder="例如 DEEPSEEK_API_KEY" autoComplete="off" /></Field>
        : <Field label="API Key" hint="仅提交给本机服务，提交后从输入框清除。"><input type="password" required value={apiKey} onChange={event => setApiKey(event.target.value)} autoComplete="new-password" spellCheck={false} /></Field>}
      <ErrorBox error={error} />{notice && <p className="notice" role="status">{notice}</p>}
      <button className="button" disabled={busy || !model.trim()}>{busy ? '正在保存…' : '保存并使用此模型'}</button>
    </form>
  </div>;
}

function Preparation({ api, setup, loading, error, refresh, active, modelSettingsRequest }: {
  api: OwnerApi; setup?: ProductSetup; loading: boolean; error?: Error; refresh: () => void; active: boolean;
  modelSettingsRequest: number;
}) {
  const [editingModels, setEditingModels] = useState(false); const prepare = useCommand(api);
  useEffect(() => { if (modelSettingsRequest > 0) setEditingModels(true); }, [modelSettingsRequest]);
  const state = setup?.local_execution.state;
  return <Panel title="开始前准备" className="preparation-panel" subtitle="状态来自本机检查，配置好后可用于后续产品。">
    {loading && !setup && <p role="status" className="loading">正在检查研发环境…</p>}
    <ErrorBox error={error} onRetry={refresh} />
    <div className="preparation-item"><span className={setup?.models_ready ? 'readiness-check ready' : 'readiness-check'} aria-hidden="true">{setup?.models_ready ? '✓' : '1'}</span>
      <div><strong>研发模型</strong><p>{setup?.models_ready ? '模型与凭据已配置' : '配置分析和编码所用的模型'}</p></div>
      <button className="text-button" onClick={() => setEditingModels(!editingModels)}>{editingModels ? '收起设置' : '设置模型'}</button></div>
    {editingModels && <ModelSetup api={api} setup={setup} active={active} onSaved={refresh} />}
    <div className="preparation-item"><span className={state === 'ready' ? 'readiness-check ready' : 'readiness-check'} aria-hidden="true">{state === 'ready' ? '✓' : '2'}</span>
      <div><strong>本机开发与测试</strong><p>{state === 'ready' ? '已准备，可以运行验证' : state === 'preparing' ? '正在准备，完成后自动继续' : state === 'blocked' ? '准备遇到问题' : '创建产品时将自动准备'}</p></div></div>
    {setup?.local_execution.detail && <p className="preparation-detail">{setup.local_execution.detail}</p>}
    {setup?.restart_required && <div className="notice notice-warning" role="status"><strong>配置已更新，需要重启平台</strong>
      <p>先运行 <code>agentflow stop</code>，确认平台退出后再运行 <code>agentflow start</code>。新产品创建将在重启后恢复。</p></div>}
    {state !== 'ready' && <button className="button secondary small" disabled={prepare.busy || state === 'preparing' || setup?.restart_required} onClick={async () => {
      try { await prepare.execute('/api/v1/product_setup/local_execution', {}); refresh(); } catch { /* Visible below. */ }
    }}>{state === 'preparing' || prepare.busy ? '本机环境准备中…' : state === 'blocked' ? '重新准备本机环境' : '提前准备本机环境'}</button>}
    <ErrorBox error={prepare.error} />
    {Boolean(setup?.requirements.length) && <div className="preparation-requirements" role="status"><strong>还需完成</strong><ul>{setup!.requirements.map((item, index) => <li key={`${item.code}-${index}`}>{item.message}</li>)}</ul></div>}
    {setup?.ready && !setup.restart_required && <p className="ready-notice" role="status">✓ 研发条件已齐备</p>}
  </Panel>;
}

export function ProductsView({ api, active, onCreated, modelSettingsRequest = 0 }: {
  api: OwnerApi; active: boolean; onCreated: (product: Product) => void;
  modelSettingsRequest?: number;
}) {
  const setup = useResource<ProductSetup>(api, active ? '/api/v1/product_setup' : null, false, 2500);
  const create = useCommand(api);
  const diagnose = useCommand(api);
  const [creationMode, setCreationMode] = useState<'new' | 'import'>('new');
  const [sourceDirectory, setSourceDirectory] = useState('');
  const [diagnosis, setDiagnosis] = useState<ProductImportDiagnosis>(); const [diagnosedPath, setDiagnosedPath] = useState('');
  const diagnosisGeneration = useRef(0);
  const [name, setName] = useState(''); const [goal, setGoal] = useState(''); const [directory, setDirectory] = useState('');
  const [targets, setTargets] = useState<ProductTarget[]>([]); const [reviewMode, setReviewMode] = useState<'auto' | 'milestones' | 'every_step'>('auto');
  const [targetsReady, setTargetsReady] = useState(false);
  const [language, setLanguage] = useState<ProductLanguage>('zh-CN');
  const [requests, setRequests] = useState('200');
  const initialized = useRef({ target: false, review: false, requests: false, language: false });
  useEffect(() => {
    if (!setup.data) return;
    const defaults = setup.data.product_defaults;
    if (!initialized.current.language && (defaults?.language === 'zh-CN' || defaults?.language === 'en')) {
      initialized.current.language = true; setLanguage(defaults.language);
    }
    if (!initialized.current.target) {
      if (defaults?.target === 'api' || defaults?.target === 'web') {
        initialized.current.target = true; setTargets([defaults.target]);
      } else if (!targetsReady) setTargets(['web']);
      setTargetsReady(true);
    }
    if (!initialized.current.review && defaults?.review_mode && ['auto', 'milestones', 'every_step'].includes(defaults.review_mode)) {
      initialized.current.review = true; setReviewMode(defaults.review_mode);
    }
    if (defaults && !initialized.current.requests && Number.isInteger(defaults.max_model_requests)
      && defaults.max_model_requests! >= 0 && defaults.max_model_requests! <= 2000) {
      initialized.current.requests = true; setRequests(String(defaults.max_model_requests));
    }
  }, [setup.data, targetsReady]);
  const importing = creationMode === 'import';
  const requestCount = Number(requests);
  const requestCountValid = /^\d+$/.test(requests) && Number.isInteger(requestCount) && requestCount >= 0 && requestCount <= 2000;
  const nativeSelected = targets.some(nativeProductTarget);
  const currentDiagnosis = diagnosis && diagnosedPath === sourceDirectory.trim() ? diagnosis : undefined;
  async function inspectProject() {
    const generation = ++diagnosisGeneration.current; const requestedPath = sourceDirectory.trim();
    setDiagnosis(undefined); setDiagnosedPath('');
    try {
      const result = await diagnose.execute<ProductImportDiagnosis>('/api/v1/products/diagnose', { project_path: requestedPath });
      if (result && generation === diagnosisGeneration.current) {
        setDiagnosis(result); setDiagnosedPath(requestedPath);
        initialized.current.target = true; setTargets(result.detected_targets); setTargetsReady(true);
      }
    } catch { /* Static diagnosis errors remain visible with the source input. */ }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!requestCountValid || (importing ? !currentDiagnosis : !targets.length || nativeSelected)) return;
    try {
      const product = await create.execute<Product>('/api/v1/products', {
        name: name.trim(), goal: goal.trim(), ...(targets.length ? { targets } : {}), creation_mode: creationMode, review_mode: reviewMode,
        ...(initialized.current.language ? { language } : {}),
        ...(importing ? { project_path: sourceDirectory.trim() } : directory.trim() ? { output_directory: directory.trim() } : {}),
        max_model_requests: requestCount,
      });
      if (product) { setup.refresh(); onCreated(product); }
    } catch { /* The unresolved command retains its idempotency key. */ }
  }
  return <>
    <div className="product-intro"><div><span className="product-intro-tag">从目标到可运行产品</span><h2>把你想做的产品告诉 AgentFlow</h2>
      <p>Agent 负责分析、开发、审查与测试。提交后进入“我的产品”查看进展，并按所选模式确认关键产物。</p></div>
      <ol aria-label="产品研发流程"><li>明确目标</li><li>并行开发</li><li>审查与验证</li><li>本机交付</li></ol></div>
    <div className="product-create-grid"><Panel title="创建或导入产品" subtitle="从新目标开始，也可以在已有项目上持续迭代。" className="product-form-panel">
      <form aria-label="创建产品" onSubmit={submit}>
        <fieldset className="product-entry-mode"><legend>开始方式</legend><div>{[
          ['new', '创建新产品'], ['import', '导入已有项目'],
        ].map(([value, label]) => <label key={value} className={creationMode === value ? 'product-entry-selected' : ''}>
          <input type="radio" name="product-entry-mode" value={value} checked={creationMode === value} onChange={() => {
            setCreationMode(value as 'new' | 'import'); create.clearError(); initialized.current.target = true;
            if (value === 'import') setTargets(currentDiagnosis?.detected_targets ?? []);
            else if (!targets.length) setTargets([setup.data?.product_defaults?.target ?? 'web']);
          }} />{label}</label>)}</div></fieldset>
        {importing && <div className="product-import-source">
          <Field label="已有项目目录" hint="填写本机项目的绝对路径，先识别平台与项目情况。"><input required value={sourceDirectory}
            onChange={event => { setSourceDirectory(event.target.value); setDiagnosis(undefined); setDiagnosedPath(''); diagnosisGeneration.current += 1; diagnose.clearError(); }}
            placeholder="例如 /Users/you/Projects/my-product" spellCheck={false} autoComplete="off" /></Field>
          <button type="button" className="button secondary" disabled={diagnose.busy || !sourceDirectory.trim()}
            onClick={() => void inspectProject()}>{diagnose.busy ? '正在检查项目…' : '识别项目平台'}</button>
          <ErrorBox error={diagnose.error} />
          {currentDiagnosis && <div className="product-diagnosis" role="status"><strong>静态检查完成</strong>
            <p>识别平台：{currentDiagnosis.detected_targets.map(target => productTargetNames[target]).join('、') || '尚未识别，请手动选择'}</p>
            <p>{currentDiagnosis.git_detected ? '检测到 Git 仓库' : '尚未检测到 Git 仓库'}{currentDiagnosis.frameworks?.length ? ` · ${currentDiagnosis.frameworks.join('、')}` : ''}</p>
            <small>仅检查目录与项目标记，未运行项目代码、构建或测试。登记后再通过“需求变更”开始迭代。</small>
            {currentDiagnosis.blocking_reasons.length > 0 && <ul>{currentDiagnosis.blocking_reasons.map((reason, index) => <li key={`${reason.code}-${index}`}>{reason.message}</li>)}</ul>}
          </div>}
        </div>}
        <Field label="产品名称"><input required maxLength={100} value={name} onChange={event => setName(event.target.value)} placeholder="给这个产品起个名字" /></Field>
        <Field label="产品目标" hint={importing ? '填写这个产品持续服务的用户与目标。后续需求变更会单独记录，不替换此目标。' : '建议说明使用者、需要解决的问题，以及你期望的关键功能。'}><textarea required rows={5} minLength={8} maxLength={16000} value={goal} onChange={event => setGoal(event.target.value)} placeholder="描述你希望产品做到什么…" /></Field>
        <Field label="文档与代码注释语言" hint="用于新生成的项目文档和代码注释，不改变产品目标或已有代码的语言。"><select value={language} disabled={!setup.data && !initialized.current.language}
          onChange={event => { initialized.current.language = true; setLanguage(event.target.value as ProductLanguage); }}>
          <option value="zh-CN">中文</option><option value="en">English</option></select></Field>
        <ProductTargets targets={targets} onChange={values => { initialized.current.target = true; setTargets(values); }} disabled={create.busy || !targetsReady} />
        {nativeSelected && <p className="product-native-notice" role="status">{importing ? '所选原生平台可以登记为已有产品，但当前不会启动原生平台研发。' : '当前版本尚不支持原生平台自动研发。请仅选择 Web / API 后开始创建，或登记已有项目。'}</p>}
        {!targets.length && <p className="product-native-notice">{importing ? '尚未识别产品平台。可以手动选择，也可以保留未识别状态登记；当前不会启动研发。' : '请至少选择一个产品平台。'}</p>}
        {!importing && <Field label="输出目录（可选）" hint={setup.data?.product_defaults?.output_root ? `留空时在 ${setup.data.product_defaults.output_root} 下自动分配；指定时请使用本机绝对路径。` : '留空由配置文件中的输出根目录安排；指定时请使用本机绝对路径。'}><input value={directory} onChange={event => setDirectory(event.target.value)} placeholder="留空使用默认产品目录" spellCheck={false} autoComplete="off" /></Field>}
        <Field label="人工参与方式"><select value={reviewMode} onChange={event => { initialized.current.review = true; setReviewMode(event.target.value as typeof reviewMode); }}>
          <option value="auto">自动推进</option><option value="milestones">关键节点确认</option><option value="every_step">每个阶段都确认</option></select></Field>
        <p className="form-mode-hint">{reviewMode === 'milestones' ? '在关键产物上等待你的决定，其余步骤自动推进。' : reviewMode === 'every_step' ? '每个阶段完成后等待你确认，再继续下一步。' : 'Agent 按审查与测试门禁自动推进，遇到阻塞时暂停。'}</p>
        <details className="advanced product-limits"><summary>本次运行限额</summary><Field label="模型调用次数上限" hint="0 表示不限次数；1 至 2000 为调用次数上限，默认 200。"><input type="number" required min="0" max="2000" step="1" value={requests} onChange={event => { initialized.current.requests = true; setRequests(event.target.value); }} /></Field><p className="muted">{setup.data?.cost_notice ?? '费用按供应商计费；每次输出、工具与执行时长限制继续生效。'}</p></details>
        {requestCountValid && requestCount === 0 && <p className="form-readiness-note" role="status">模型调用：{modelRequestLimit(requestCount)}。费用按供应商计费，每次输出、工具与执行时长限制继续生效。</p>}
        {!requestCountValid && <p className="form-readiness-note" role="alert">模型调用次数必须是 0 至 2000 之间的整数，0 表示不限次数。</p>}
        <ErrorBox error={create.error} />
        {!importing && !setup.data?.models_ready && !setup.loading && <p className="form-readiness-note">先在“开始前准备”中设置研发模型，再开始创建。</p>}
        <div className="form-actions"><span className="muted">{importing ? '仅登记项目与目标；需求变更由你单独提交。' : '提交后会创建真实研发运行，可随时查看与暂停。'}</span><button className="button product-submit" disabled={create.busy || !requestCountValid || !name.trim() || goal.trim().length < 8 || (importing ? !currentDiagnosis : !targets.length || nativeSelected || !setup.data?.models_ready || setup.data?.restart_required || setup.data.local_execution.state === 'blocked' || setup.data.requirements.some(item => item.code === 'runtime_missing'))}>
          {create.busy ? '正在提交…' : importing ? '登记已有项目' : '开始创建产品'}<span aria-hidden="true"> →</span></button></div>
      </form>
    </Panel><Preparation api={api} setup={setup.data} loading={setup.loading} error={setup.error} refresh={setup.refresh} active={active} modelSettingsRequest={modelSettingsRequest} /></div>
  </>;
}
