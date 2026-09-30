import { cloneElement, isValidElement, useId } from 'react';
import type { PropsWithChildren, ReactElement, ReactNode } from 'react';
import { ApiError } from './api';
import { errorNames, label } from './labels';

export function Status({ value, text }: { value?: string; text?: string }) {
  const tone = value && ['failed', 'error', 'blocked', 'rejected', 'revoked'].includes(value) ? 'danger'
    : value && ['waiting_approval', 'pending_user_confirmation', 'execution_unknown', 'missing_inputs', 'inconclusive'].includes(value) ? 'warning'
    : value && ['running', 'online', 'configured', 'accepted', 'approve', 'delivered'].includes(value) ? 'active' : 'neutral';
  return <span className={`status status-${tone}`}><span className="status-dot" />{text ?? label(value)}</span>;
}

export function ErrorBox({ error, onRetry }: { error?: Error; onRetry?: () => void }) {
  if (!error) return null;
  const title = error instanceof ApiError ? errorNames[error.code] ?? '操作未完成' : '操作未完成';
  return <div className="notice notice-error" role="alert"><strong>{title}</strong><p>{error.message}</p>
    {error instanceof ApiError && error.details != null && <details><summary>查看服务说明</summary><pre>{JSON.stringify(error.details, null, 2)}</pre></details>}
    {onRetry && <button className="button secondary small" onClick={onRetry}>重新读取</button>}
  </div>;
}

export function Empty({ title, children }: PropsWithChildren<{ title: string }>) {
  return <div className="empty"><span className="empty-mark" aria-hidden="true">◇</span><strong>{title}</strong><p>{children}</p></div>;
}
export function Panel({ title, subtitle, action, children, className = '' }: PropsWithChildren<{ title: string; subtitle?: string; action?: ReactNode; className?: string }>) {
  return <section className={`panel ${className}`}><header className="panel-heading"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>{action}</header>{children}</section>;
}
export function Field({ label: name, children, hint }: PropsWithChildren<{ label: string; hint?: string }>) {
  const hintId = useId();
  const control = isValidElement(children) ? children as ReactElement<{ 'aria-label'?: string; 'aria-describedby'?: string }> : undefined;
  const accessible = control ? cloneElement(control, {
    'aria-label': control.props['aria-label'] ?? name,
    'aria-describedby': control.props['aria-describedby'] ?? (hint ? hintId : undefined),
  }) : children;
  return <label className="field"><span>{name}</span>{accessible}{hint && <small id={hintId}>{hint}</small>}</label>;
}
export function Metric({ title, value, detail }: { title: string; value: ReactNode; detail?: string }) {
  return <div className="metric"><span>{title}</span><strong>{value}</strong>{detail && <small>{detail}</small>}</div>;
}
export function Detail({ value }: { value: unknown }) {
  return <pre className="code-view">{JSON.stringify(value, null, 2)}</pre>;
}
