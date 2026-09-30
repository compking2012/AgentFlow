import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError, OwnerApi } from './api';
import { Empty, ErrorBox, Field, Panel } from './components';
import { useCommand } from './hooks';
import type { ExecutionSettingField, ExecutionSettings } from './types';

const path = '/api/v1/settings/execution';
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const format = (value: unknown) => typeof value === 'boolean' ? (value ? '开启' : '关闭') : finite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '未提供';
const continuousRepair = (key: string, value: unknown) => ['app.auto_review_repair_limit', 'app.auto_test_repair_limit'].includes(key) && value === -1;
const formatSetting = (key: string, value: unknown) => continuousRepair(key, value) ? '持续修复' : format(value);
type Values = Record<string, string>;
type Save = { expected_configuration_revision: string; values: Record<string, number | boolean> };
function validate(value: ExecutionSettings) {
  if (!value || typeof value.configuration_path !== 'string'
    || !/^sha256:[0-9a-f]{64}$/.test(value.configuration_revision) || typeof value.restart_required !== 'boolean'
    || value.current_runs_changed !== false || !Array.isArray(value.fields) || !value.fields.length
    || !value.saved_values || !value.loaded_values || value.model_parameters?.source !== 'model_configuration'
    || new Set(value.fields.map(field => field?.key)).size !== value.fields.length
    || value.fields.some(field => !field || !/^(app|product)\.[a-z_]+$/.test(field.key)
      || typeof field.label !== 'string' || typeof field.unit !== 'string' || typeof field.integer !== 'boolean'
      || !['common', 'advanced'].includes(field.group)
      || [field.default_value, field.saved_value, field.loaded_value].some(item => field.boolean ? typeof item !== 'boolean' : !finite(item))
      || value.saved_values[field.key] !== field.saved_value || value.loaded_values[field.key] !== field.loaded_value
      || [field.minimum, field.maximum, field.exclusive_minimum].some(bound => bound !== null && !finite(bound)))) {
    throw new ApiError('invalid_response', '执行设置记录无法核对，请重新读取固定配置文件。');
  }
  for (const role of ['roles', 'coding'] as const) {
    const model = value.model_parameters[role];
    if (!model || model.source !== `models.${role}` || typeof model.model !== 'string'
      || !finite(model.max_output_tokens) || typeof model.configured !== 'boolean') {
      throw new ApiError('invalid_response', '模型参数来源无法核对，请重新读取配置。');
    }
  }
}
function initial(value: ExecutionSettings): Values {
  return Object.fromEntries(value.fields.map(field => [field.key, String(field.saved_value)]));
}
function changed(values: Values, baseline: ExecutionSettings) {
  return baseline.fields.filter(field => values[field.key]?.trim() === '' || fieldValue(field, values[field.key]) !== field.saved_value);
}
function fieldValue(field: ExecutionSettingField, raw: string): number | boolean {
  return field.boolean ? raw === 'true' : Number(raw);
}
function validField(field: ExecutionSettingField, raw: string) {
  if (field.boolean) return raw === 'true' || raw === 'false';
  if (!/^-?\d+(?:\.\d+)?$/.test(raw)) return false;
  const value = Number(raw);
  return finite(value) && value <= Number.MAX_SAFE_INTEGER && (!field.integer || Number.isSafeInteger(value))
    && (field.minimum === null || value >= field.minimum) && (field.maximum === null || value <= field.maximum)
    && (field.exclusive_minimum === null || value > field.exclusive_minimum);
}

export function ExecutionSettingsPanel({ api, active, onModelSettings, onExecution }: {
  api: OwnerApi; active: boolean; onModelSettings: () => void; onExecution: () => void;
}) {
  const command = useCommand(api);
  const [baseline, setBaseline] = useState<ExecutionSettings>();
  const [latest, setLatest] = useState<ExecutionSettings>();
  const [values, setValues] = useState<Values>({});
  const [loading, setLoading] = useState(false);
  const [readError, setReadError] = useState<Error>();
  const [sent, setSent] = useState<Save>();
  const [needsReview, setNeedsReview] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [notice, setNotice] = useState('');
  const [awaitingRead, setAwaitingRead] = useState(false);
  const dirty = useRef(false);
  const sentRef = useRef<Save | undefined>(undefined);
  const saving = useRef(false);
  const alive = useRef(true);
  const activeRef = useRef(active); activeRef.current = active;
  const generation = useRef(0);
  const controller = useRef<AbortController | undefined>(undefined);
  const reading = useRef(false);
  const conflict = Boolean(needsReview || (baseline && latest && baseline.configuration_revision !== latest.configuration_revision));
  const changedFields = baseline ? changed(values, baseline) : [];
  const fieldsValid = Boolean(baseline && baseline.fields.every(field => validField(field, values[field.key] ?? '')));

  function apply(value: ExecutionSettings) {
    setBaseline(value); setValues(initial(value)); dirty.current = false;
    setNeedsReview(false); setReviewOpen(false); setAwaitingRead(false);
  }
  async function read(background = false) {
    if (!activeRef.current || (background && (reading.current || saving.current))) return;
    const request = ++generation.current;
    controller.current?.abort(); controller.current = new AbortController();
    reading.current = true; setLoading(true); setReadError(undefined);
    try {
      const value = await api.get<ExecutionSettings>(path, controller.current.signal);
      validate(value);
      if (!alive.current || request !== generation.current) return;
      setLatest(value);
      if (!dirty.current && !sentRef.current && !saving.current) apply(value);
    } catch (error) {
      if (alive.current && request === generation.current && !controller.current?.signal.aborted) setReadError(error as Error);
    } finally {
      if (alive.current && request === generation.current) { reading.current = false; setLoading(false); }
    }
  }
  useEffect(() => { alive.current = true; return () => { alive.current = false; generation.current += 1; controller.current?.abort(); }; }, []);
  useEffect(() => {
    if (active) void read();
    const timer = active ? window.setInterval(() => void read(true), 8000) : undefined;
    return () => { window.clearInterval(timer); generation.current += 1; controller.current?.abort(); reading.current = false; };
  }, [active, api]);

  function update(key: string, value: string) {
    const next = { ...values, [key]: value };
    setValues(next); dirty.current = Boolean(baseline && changed(next, baseline).length); setNotice('');
  }
  function rebase() {
    if (!baseline || !latest || sentRef.current) return;
    const next = initial(latest);
    for (const field of changed(values, baseline)) if (field.key in next) next[field.key] = values[field.key];
    setBaseline(latest); setValues(next); dirty.current = changed(next, latest).length > 0;
    setNeedsReview(false); setReviewOpen(false); command.clearError();
    setNotice('已采用当前文件版本，并保留你的修改。核对后点击保存。');
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (saving.current) return;
    const reconciling = Boolean(sentRef.current);
    if (!sentRef.current && (!baseline || !fieldsValid || !changedFields.length || conflict || awaitingRead || readError || loading)) return;
    const payload = sentRef.current ?? { expected_configuration_revision: baseline!.configuration_revision,
      values: Object.fromEntries(changedFields.map(field => [field.key, fieldValue(field, values[field.key])])) };
    sentRef.current = payload; setSent(payload); saving.current = true;
    generation.current += 1; controller.current?.abort(); reading.current = false; setLoading(false);
    try {
      const receipt = await command.execute<ExecutionSettings>(path, payload, 'POST', value => {
        validate(value);
        if (typeof value.operation_id !== 'string' || !value.operation_id || typeof value.saved_at !== 'string'
          || Object.entries(payload.values).some(([key, expected]) => value.saved_values[key] !== expected)) {
          throw new ApiError('invalid_response', '保存回执无法核对，请核对原保存操作。');
        }
      });
      if (!receipt || !alive.current) return;
      sentRef.current = undefined; setSent(undefined); dirty.current = false;
      setAwaitingRead(true); setNeedsReview(false); setReviewOpen(false);
      setNotice('本次保存已确认。执行设置写入固定配置文件，重启平台后加载；已有运行的额度和已用量保持不变。');
    } catch (error) {
      if (!alive.current) return;
      const prewriteConflict = error instanceof ApiError && error.status === 409
        && ['configuration_changed', 'idempotency_conflict'].includes(error.code);
      const prewriteValidation = error instanceof ApiError && error.status === 422
        && ['invalid_request', 'configuration_invalid', 'validation_error'].includes(error.code);
      // Filesystem confirmation can fail after replacement. HTTP 409/422 alone
      // does not prove that no write occurred; preserve ambiguous operations.
      if (!reconciling && (prewriteConflict || prewriteValidation)) {
        sentRef.current = undefined; setSent(undefined);
        if (prewriteConflict) { setNeedsReview(true); setReviewOpen(true); }
      }
    } finally {
      saving.current = false;
      if (alive.current) void read();
    }
  }
  const display = latest ?? baseline;
  const frozen = command.busy || Boolean(sent) || awaitingRead;
  const unconfirmedFile = command.error instanceof ApiError && command.error.code === 'execution_settings_save_unconfirmed';
  function fieldControl(field: ExecutionSettingField) {
    const current = display?.fields.find(item => item.key === field.key) ?? field;
    const defaultValue = `${formatSetting(field.key, field.default_value)}${continuousRepair(field.key, field.default_value) ? '' : ` ${field.unit}`}`;
    const hint = `文件值 ${formatSetting(field.key, current.saved_value)}；本次已加载 ${formatSetting(field.key, current.loaded_value)}；默认 ${defaultValue}。${field.zero_meaning ? `0：${field.zero_meaning}。` : ''}`;
    return <div key={field.key} className="execution-setting-field">
      <Field label={`${field.label}（${field.unit}）`} hint={hint}>
        {field.boolean ? <select value={values[field.key] ?? ''} disabled={frozen}
          onChange={event => update(field.key, event.target.value)}>
          <option value="true">开启</option><option value="false">关闭</option>
        </select> : <input type="number" required min={field.minimum ?? field.exclusive_minimum ?? undefined} max={field.maximum ?? Number.MAX_SAFE_INTEGER}
          step={field.integer ? '1' : 'any'} value={values[field.key] ?? ''} disabled={frozen}
          onChange={event => update(field.key, event.target.value)} />}
      </Field>
      <p className="muted">{field.description} {field.effect_scope}</p>
      <small className="muted"><code>{field.key}</code> · {current.source === 'configuration_file' ? '来源：固定配置文件' : '来源：应用默认值'}</small>
    </div>;
  }
  return <Panel title="执行设置" subtitle="工作额度与应用执行策略保存到固定配置文件；模型参数由模型配置提供。" className="execution-settings"
    action={<button className="button secondary small" disabled={command.busy || loading} onClick={() => void read()}>重新读取执行设置</button>}>
    <ErrorBox error={readError} onRetry={() => void read()} />
    {!baseline ? <Empty title={loading ? '正在读取执行设置' : '执行设置尚未就绪'}>读取当前文件后才能编辑，不使用页面猜测的额度。</Empty> : <>
      <p className="muted">固定文件：<code>{display!.configuration_path}</code></p>
      <div className="notice"><strong>保存后需要重启平台</strong><p>重启后按各项说明应用新策略。默认工作额度用于新运行；已建立的工作额度和累计用量不会重置。若当前工作已耗尽，请在执行台明确追加该工作的额度。</p>
        <button type="button" className="button secondary small" onClick={onExecution}>前往执行台调整当前工作额度</button></div>
      {display!.restart_required && <p className="notice notice-warning">文件值与本次已加载值不同；需要重启平台后生效。</p>}
      <div className="item-details"><h3>模型参数来源</h3><p>{display!.model_parameters.description}</p>
        <div className="model-grid">{(['roles', 'coding'] as const).map(role => {
          const model = display!.model_parameters[role];
          return <div key={role}><strong>{role === 'roles' ? '调研、产品、架构、Review 等专业角色' : '开发与测试编写'}</strong>
            <p>{model.configured ? model.model : '尚未配置模型'} · 单次输出上限 {format(model.max_output_tokens)} token</p>
            <small><code>{model.source}.max_output_tokens</code></small></div>;
        })}</div><p className="muted">推理设置也来自模型配置；本面板不会另建模型输出或推理参数。</p>
        <button type="button" className="button secondary small" onClick={onModelSettings}>前往模型设置</button>
      </div>
      <form aria-label="执行设置" onSubmit={submit}>
        <fieldset><legend>常用工作额度</legend><div className="form-grid">{baseline.fields.filter(field => field.group === 'common').map(fieldControl)}</div></fieldset>
        <details className="advanced"><summary>高级执行策略</summary><div className="form-grid">{baseline.fields.filter(field => field.group === 'advanced').map(fieldControl)}</div></details>
        {changedFields.length > 0 && !sent && <p className="muted">有 {changedFields.length} 项未保存修改；重新读取或切换页面不会覆盖这些输入。</p>}
        {conflict && !sent && <div className="notice notice-warning"><strong>配置文件已变化，你的输入已保留</strong>
          <p>请先核对当前文件与本次修改，页面不会自动覆盖外部编辑。</p>
          <button type="button" className="button secondary small" disabled={loading || !latest} onClick={() => setReviewOpen(true)}>核对外部修改</button></div>}
        {reviewOpen && !sent && latest && <div className="item-details"><h3>核对配置差异</h3>
          <div className="table-wrap"><table><thead><tr><th>设置项</th><th>开始编辑时</th><th>当前文件</th><th>你的修改</th></tr></thead>
            <tbody>{baseline.fields.filter(field => fieldValue(field, values[field.key]) !== field.saved_value
              || latest.saved_values[field.key] !== field.saved_value).map(field => <tr key={field.key}>
              <th scope="row">{field.label}</th><td>{formatSetting(field.key, field.saved_value)}</td><td>{formatSetting(field.key, latest.saved_values[field.key])}</td><td>{changedFields.some(item => item.key === field.key) ? values[field.key] : '未修改'}</td>
            </tr>)}</tbody></table></div><p>选择保留修改只更新编辑基线，仍需再次点击保存。</p>
          <button type="button" className="button secondary small" disabled={loading || Boolean(readError)}
            onClick={rebase}>采用当前文件版本并保留我的修改</button>
        </div>}
        {sent && command.error && <p className="notice notice-warning">保存结果尚未确认；核对原保存会复用相同内容和提交标识，不会重复写入。</p>}
        {sent && unconfirmedFile && latest && <div className="item-details"><h3>核对当前文件</h3>
          <p>原保存是否完成尚无法确认。下面只比较当前文件与原提交；采用当前文件不会再次写入。</p>
          <div className="table-wrap"><table><thead><tr><th>设置项</th><th>原提交</th><th>当前文件</th></tr></thead>
            <tbody>{Object.entries(sent.values).map(([key, value]) => <tr key={key}><th scope="row">{baseline.fields.find(field => field.key === key)?.label ?? key}</th>
              <td>{formatSetting(key, value)}</td><td>{formatSetting(key, latest.saved_values[key])}</td></tr>)}</tbody></table></div>
          <button type="button" className="button secondary small" disabled={loading || Boolean(readError) || command.busy}
            onClick={() => { sentRef.current = undefined; setSent(undefined); apply(latest); command.clearError();
              setNotice('已采用当前文件并结束本次核对，没有发起新的保存。'); }}>以当前文件为准</button>
        </div>}
        {awaitingRead && <p className="muted">保存已确认，正在读取最新文件；读取成功前暂不继续编辑。</p>}
        <ErrorBox error={command.error} />
        {notice && <p className="notice" role="status">{notice}</p>}
        <div className="form-actions"><button className="button" disabled={command.busy || (!sent
          && (!changedFields.length || !fieldsValid || conflict || awaitingRead || Boolean(readError) || loading))}>
          {command.busy ? '正在保存…' : sent ? '核对原保存' : '保存执行设置'}</button>
          <button type="button" className="button secondary" disabled={frozen || !display || loading}
            onClick={() => { if (display) { apply(display); command.clearError(); setNotice('已放弃未保存修改，采用当前读取的文件值。'); } }}>放弃未保存修改</button>
        </div>
      </form>
    </>}
  </Panel>;
}
