import { useState } from 'react';
import { ArtifactCard, useArtifactReader } from './ArtifactViews';
import { Empty, ErrorBox, Panel, Status } from './components';
import { measuredPassRate } from './quality';
import type { QualitySummary, TestSummary } from './workflow';

function TestMetric({ name, value }: { name: string; value?: TestSummary }) {
  const rate = measuredPassRate(value); const measured = Boolean(value?.verified && Number.isFinite(value.total) && value.total! > 0);
  const failed = value?.status === 'failed' || (measured && ((value?.failed ?? 0) > 0 || (value?.errors ?? 0) > 0));
  const passed = measured && value?.status === 'passed' && value.coverage_complete !== false && !failed;
  const state = failed ? 'failed' : passed ? 'passed' : measured ? 'inconclusive' : 'not_run';
  return <section className={`quality-metric quality-result-${state}`} aria-label={name}>
    <header><h3>{name}</h3><span className={`quality-test-status quality-test-${state}`}><Status value={state} text={failed ? '存在失败' : passed ? '测试通过' : measured ? '待齐备' : '未测'} /></span></header>
    <strong className="quality-rate">{rate === '未测' && measured ? '待齐备' : rate}</strong><span className="quality-caption">通过率</span>
    {measured ? <><p>{value?.passed ?? '—'} 通过 · {value?.failed ?? '—'} 失败 · {value?.errors ?? '—'} 错误 · {value?.skipped ?? '—'} 跳过 · {value?.unknown ?? 0} 未确认</p>
      <small>共 {value?.total} 个用例{typeof value?.duration_seconds === 'number' && Number.isFinite(value.duration_seconds) ? ` · 测试耗时 ${value.duration_seconds.toLocaleString('zh-CN', { maximumFractionDigits: 2 })} 秒` : ''}</small>{value?.coverage_complete === false && <small>必需测试尚未全部完成</small>}</>
      : <p>等待当前版本的已核验测试结果</p>}
  </section>;
}

export function QualityMetrics({ summary }: { summary?: QualitySummary }) {
  const bugs = summary?.bugs;
  const measuredBugs = Boolean(bugs && !['not_measured', 'not_run', 'unknown', 'unverified', 'not_reported'].includes(bugs.status)
    && Number.isInteger(bugs.open) && bugs.open! >= 0);
  const metrics = summary?.performance?.status === 'measured' ? (summary.performance.metrics ?? []).filter(metric => Number.isFinite(metric.value)) : [];
  return <>
    <div className="quality-metrics"><TestMetric name="单元测试" value={summary?.unit_tests} /><TestMetric name="集成测试" value={summary?.integration_tests} />
      <section className="quality-metric" aria-label="代码审查问题"><header><h3>代码审查问题</h3><span>Review</span></header><strong className="quality-rate">{measuredBugs ? bugs!.open : '未测'}</strong>
        <span className="quality-caption">待解决</span><p>{measuredBugs ? `共 ${bugs!.total ?? '—'} 项 · ${bugs!.resolution_tracking !== false && typeof bugs!.resolved === 'number' ? `已解决 ${bugs!.resolved} 项` : '关闭情况未登记'}` : '等待当前版本的审查记录'}</p></section>
      <section className="quality-metric" aria-label="性能测量"><header><h3>性能测量</h3></header><strong className="quality-rate">{metrics.length ? `${metrics.length} 项` : '未测'}</strong>
        <span className="quality-caption">实际测量指标</span><p>{metrics.length ? '已登记测量结果，见下方明细' : '尚无实际性能测量数据'}</p></section>
    </div>
    {measuredBugs && Boolean(bugs?.items?.length) && <Panel title="审查问题清单" subtitle="保留报告中的分类和结论。" className="quality-bugs"><ul>{bugs!.items!.map((bug, index) => <li key={`${bug.title}:${index}`}>
      <div><strong>{bug.title}</strong>{bug.path && <code>{bug.path}</code>}</div><span>{({ bug: '功能缺陷', security: '安全', style: '代码风格', performance: '性能', maintainability: '可维护性', unclassified: '未分类' } as Record<string, string>)[bug.category ?? 'unclassified'] ?? '未分类'}</span><span>{({ critical: '严重', blocking: '阻断', error: '错误', warning: '提示', info: '信息', high: '高', medium: '中', low: '低' } as Record<string, string>)[bug.severity] ?? bug.severity}</span><Status value={bug.status === 'resolved' ? 'passed' : 'failed'} text={bug.status === 'resolved' ? '已解决' : '待解决'} />
    </li>)}</ul></Panel>}
    {metrics.length > 0 && <Panel title="性能指标" className="quality-performance"><dl>{metrics.map((metric, index) => <div key={`${metric.name}:${index}`}><dt>{metric.name}</dt><dd>{metric.value.toLocaleString('zh-CN', { maximumFractionDigits: 3 })} <span>{metric.unit}</span></dd>
      <small>{Number.isInteger(metric.sample_count) && metric.sample_count! > 0 ? `${metric.sample_count} 个样本 · ` : ''}测试报告测量</small>
      {metric.source_artifact_id && <details><summary>查看来源</summary><code>{metric.source_artifact_id}</code></details>}</div>)}</dl></Panel>}
  </>;
}

export function CodeDirectories({ summary, reader }: { summary?: QualitySummary; reader: ReturnType<typeof useArtifactReader> }) {
  const [error, setError] = useState<Error>(); const [copied, setCopied] = useState('');
  const source = summary?.paths;
  const rows = [
    ...(source?.project_directory ? [{ label: '项目代码目录', path: source.project_directory }] : []),
    ...(source?.test_directories ?? []).map(path => ({ label: '测试代码目录', path })),
    ...(source?.delivery_directory ? [{ label: '交付目录', path: source.delivery_directory }] : []),
  ];
  const codeArtifacts = (summary?.artifacts ?? []).filter(artifact => !artifact.internal && !artifact.stale && (artifact.kind === 'code' || artifact.kind === 'test_code'));
  const unique = rows.filter((row, index) => rows.findIndex(other => row.path === other.path) === index
    && !codeArtifacts.some(artifact => artifact.path === row.path));
  return <Panel title="项目与测试代码" subtitle="使用服务登记的实际目录。" className="quality-directories">
    {codeArtifacts.map(artifact => <ArtifactCard key={artifact.artifact_id} artifact={artifact} reader={reader} />)}
    {unique.length ? <dl>{unique.map(row => <div key={row.path}><dt>{row.label}</dt><dd><code>{row.path}</code><button className="text-button" onClick={async () => {
      try { await navigator.clipboard.writeText(row.path); setCopied(row.path); } catch { setError(new Error('复制失败，请复制上方目录。')); }
    }}>{copied === row.path ? '已复制' : '复制目录'}</button></dd></div>)}</dl> : codeArtifacts.length ? null : <Empty title="目录尚未登记">项目和测试代码生成后会在这里显示位置。</Empty>}
    <ErrorBox error={error} />
  </Panel>;
}
