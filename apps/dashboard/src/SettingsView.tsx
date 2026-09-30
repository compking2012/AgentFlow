import { useState } from 'react';
import type { FormEvent } from 'react';
import { OwnerApi } from './api';
import { Detail, Empty, ErrorBox, Field, Panel, Status } from './components';
import { useCommand } from './hooks';
import { targetNames } from './labels';
import { TARGETS, recordId, stringValue } from './types';
import type { Backend, Entity, Meta, Node, Profile, Resource, Target } from './types';
import { ExecutionSettingsPanel } from './ExecutionSettingsPanel';

export function SettingsView({ api, active = true, meta, profiles, backends, nodes, resources, refresh, onProductSetup, onExecution }: {
  api: OwnerApi; meta?: Meta; profiles: Profile[]; backends: Backend[]; nodes: Node[]; resources: Resource[]; refresh: () => void; onProductSetup: () => void;
  active?: boolean; onExecution: () => void;
}) {
  const command = useCommand(api); const [nodeLabel, setNodeLabel] = useState('');
  const [location, setLocation] = useState('user_lan_host'); const [fingerprint, setFingerprint] = useState('');
  const [allowed, setAllowed] = useState<Target[]>([]); const [expiry, setExpiry] = useState('300');
  const [secret, setSecret] = useState<{ pairing: Entity; single_use_code: string }>(); const [visible, setVisible] = useState(false);
  const [copied, setCopied] = useState(false); const [copyError, setCopyError] = useState<Error>();
  async function createPairing(event: FormEvent) {
    event.preventDefault(); setSecret(undefined); setVisible(false); setCopied(false);
    try {
      const result = await command.execute<{ pairing: Entity; single_use_code: string }>('/api/v1/executor_pairings', {
        node_label: nodeLabel.trim(), location, expected_node_public_key_fingerprint: fingerprint.trim(),
        allowed_app_targets: allowed, expires_in_seconds: Number(expiry),
      });
      if (result) { setSecret(result); refresh(); }
    } catch { /* command error is visible */ }
  }
  return <>
    <Panel title="模型与凭据" subtitle="模型与平台默认项使用 ~/.config/agentflow/config.toml；这里展示实际配置与能力详情。" action={<button className="button secondary small" onClick={onProductSetup}>配置产品研发环境</button>}>
      {profiles.length === 0 ? <Empty title="尚未配置模型">未配置或未接受模型时，运行计划会明确阻塞，界面不会自动替你选择供应商。</Empty> : <div className="model-grid">{profiles.map(profile => <article className="model-card" key={recordId(profile)}>
        <div className="eyebrow">{stringValue(profile.provider, '模型供应商未提供')}</div><h3>{stringValue(profile.name ?? profile.label ?? profile.accepted_api_model ?? profile.requested_model_id ?? profile.requested_model ?? profile.requested_api_model, '模型配置')}</h3>
        <p><Status value={profile.acceptance_status} /></p><dl><dt>请求模型</dt><dd>{stringValue(profile.requested_model_id ?? profile.requested_model ?? profile.requested_api_model)}</dd>
          <dt>已接受模型</dt><dd>{stringValue(profile.accepted_api_model, '尚未明确接受')}</dd><dt>凭据状态</dt><dd><Status value={profile.credential_status} text={profile.credential_status ? undefined : '服务未提供状态'} /></dd>
          <dt>协议</dt><dd>{profile.protocols?.join(' / ') || '未提供'}</dd><dt>配置版本</dt><dd>{profile.revision ?? '未提供'}</dd></dl>
        <code>{recordId(profile)}</code>
      </article>)}</div>}
      <p className="footnote">静态配置或 CLI 可用，不等于真实模型调用、工具循环或计费上界已经验证。</p>
    </Panel>
    <ExecutionSettingsPanel api={api} active={active} onModelSettings={onProductSetup} onExecution={onExecution} />
    <Panel title="执行后端" subtitle="能力与限制来自服务探针；Coding Agent 和模型配置分别管理。">
      {backends.length ? <div className="backend-list">{backends.map((backend, index) => <details key={recordId(backend) || String(index)}><summary>{stringValue(backend.name ?? backend.backend, '执行后端')} · {backend.available === true ? '程序可用' : backend.available === false ? '程序不可用' : '可用性未提供'}</summary><Detail value={backend} /></details>)}</div> : <Empty title="尚无后端探针数据">请先在本地服务中配置执行器；这里不会伪造可用能力。</Empty>}
    </Panel>
    <Panel title="自有执行节点" subtitle="管理入口与节点执行通道分离。配对前请核对节点公钥指纹。" action={<span className="count-pill">{nodes.length} 个已登记节点</span>}>
      {!meta?.executor_configured && <div className="notice notice-warning">执行通道尚未配置。请先在本地服务设置私网 HTTPS、证书及客户端 CA，再创建节点配对。</div>}
      {nodes.length > 0 && <div className="table-wrap"><table><thead><tr><th>节点</th><th>状态</th><th>目标范围</th><th>最近心跳</th></tr></thead><tbody>{nodes.map(node => <tr key={recordId(node)}><td><strong>{node.label ?? node.id.slice(0, 8)}</strong><small>{node.location ?? '位置未提供'}</small></td><td><Status value={node.state} /></td><td>{node.allowed_app_targets?.map(t => targetNames[t] ?? t).join('、') || '未提供'}</td><td>{node.last_heartbeat_at ? new Date(node.last_heartbeat_at).toLocaleString() : '暂无心跳'}</td></tr>)}</tbody></table></div>}
      <details className="advanced"><summary>创建一次性节点配对</summary><form onSubmit={createPairing} aria-label="节点配对">
        <div className="form-grid"><Field label="节点名称"><input required value={nodeLabel} onChange={e => setNodeLabel(e.target.value)} maxLength={160} /></Field><Field label="节点位置"><select value={location} onChange={e => setLocation(e.target.value)}><option value="user_lan_host">自有局域网主机</option><option value="user_vm">自有虚拟机</option><option value="controller_host">当前控制主机</option></select></Field></div>
        <Field label="节点公钥指纹" hint="从节点获取并核对 SHA-256 指纹。不要粘贴私钥。"><input required pattern="sha256:[0-9a-f]{64}" value={fingerprint} onChange={e => setFingerprint(e.target.value)} placeholder="sha256:…" spellCheck={false} autoComplete="off" /></Field>
        <fieldset><legend>允许执行的目标</legend><div className="check-grid">{TARGETS.map(t => <label key={t}><input type="checkbox" checked={allowed.includes(t)} onChange={() => setAllowed(previous => previous.includes(t) ? previous.filter(x => x !== t) : [...previous, t])} />{targetNames[t]}</label>)}</div></fieldset>
        <Field label="配对码有效时间（秒）"><input type="number" required min="60" max="900" value={expiry} onChange={e => setExpiry(e.target.value)} /></Field>
        <ErrorBox error={command.error} /><button className="button" disabled={!meta?.executor_configured || !allowed.length || command.busy}>{command.busy ? '正在创建…' : '创建配对码'}</button>
      </form></details>
      {secret && <div className="notice"><strong>一次性配对码已创建</strong><p>有效期至 {stringValue(secret.pairing.expires_at)}。仅用于此节点配对，请勿放入日志或 URL。</p>
        {visible && <code className="pairing-secret">{secret.single_use_code}</code>}<div className="inline-actions"><button className="button secondary small" onClick={() => setVisible(!visible)}>{visible ? '隐藏配对码' : '显示配对码'}</button>
          <button className="button secondary small" onClick={async () => { try { await navigator.clipboard.writeText(secret.single_use_code); setCopied(true); } catch { setCopyError(new Error('剪贴板不可用，请显示配对码后手动复制。')); } }}>{copied ? '已复制' : '复制配对码'}</button><button className="text-button" onClick={() => { setSecret(undefined); setVisible(false); }}>关闭并清除</button></div><ErrorBox error={copyError} /></div>}
      {nodes.length === 0 && <p className="muted">尚无已配对节点。目标支持必须通过真实环境与执行证据验证。</p>}
    </Panel>
    <Panel title="设备与桌面资源" subtitle="占用、失联和隔离待检由服务判定；租约到期不等于进程已经停止。">
      {resources.length ? <div className="table-wrap"><table><thead><tr><th>资源</th><th>类型</th><th>状态</th><th>节点</th></tr></thead><tbody>{resources.map(resource => <tr key={resource.id}><td>{resource.label ?? resource.id.slice(0, 8)}</td><td>{resource.kind ?? resource.resource_type ?? '未提供'}</td><td><Status value={resource.state} /></td><td>{resource.node_id?.slice(0, 8) ?? '未提供'}</td></tr>)}</tbody></table></div> : <Empty title="尚无资源记录">资源探针和节点心跳上报后在此显示。</Empty>}
    </Panel>
  </>;
}
