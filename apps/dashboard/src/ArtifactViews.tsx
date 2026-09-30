import { useEffect, useRef, useState } from 'react';
import { ApiError, OwnerApi } from './api';
import { ErrorBox } from './components';
import { MarkdownDocument } from './MarkdownDocument';
import type { ReadableArtifact } from './workflow';

const MAX_DOCUMENT = 1024 * 1024;
async function readDocument(api: OwnerApi, artifact: ReadableArtifact, signal: AbortSignal): Promise<string> {
  if (!artifact.preview_url) {
    throw new ApiError('preview_unavailable', '这份文档尚未提供可阅读版本');
  }
  const response = await api.stream(artifact.preview_url, signal);
  const limit = 6 * MAX_DOCUMENT + 4096;
  const tooLarge = () => new ApiError('preview_too_large', '产物超过文本预览大小，请下载后查看。');
  if (Number(response.headers.get('content-length')) > limit) { await response.body?.cancel(); throw tooLarge(); }
  if (!response.body) throw new ApiError('invalid_response', '文档内容尚不可读取');
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let bytes = 0; let raw = '';
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) { raw += decoder.decode(); break; }
      bytes += chunk.value.byteLength;
      if (bytes > limit) { await reader.cancel(); throw tooLarge(); }
      raw += decoder.decode(chunk.value, { stream: true });
    }
  } finally { reader.releaseLock(); }
  let value: { content?: unknown; media_type?: string };
  try { value = JSON.parse(raw); } catch { throw new ApiError('invalid_response', '服务未返回可阅读的文档'); }
  if (typeof value.content !== 'string' || !['text/markdown', 'text/plain'].includes(value.media_type ?? '')) throw new ApiError('invalid_response', '服务未返回可阅读的文档');
  if (new TextEncoder().encode(value.content).byteLength > MAX_DOCUMENT) throw tooLarge();
  return value.content;
}

export function useArtifactReader(api: OwnerApi, scope?: string) {
  const [document, setDocument] = useState<{ artifact: ReadableArtifact; content: string; scope?: string }>();
  const [error, setError] = useState<Error>(); const [busy, setBusy] = useState<string>();
  const current = useRef<AbortController | null>(null);
  useEffect(() => {
    current.current?.abort(); setDocument(undefined); setError(undefined); setBusy(undefined);
    return () => current.current?.abort();
  }, [scope]);
  async function open(artifact: ReadableArtifact) {
    current.current?.abort(); const abort = new AbortController(); current.current = abort;
    setDocument(undefined); setError(undefined); setBusy(artifact.artifact_id);
    try {
      const content = await readDocument(api, artifact, abort.signal);
      if (!abort.signal.aborted && current.current === abort) setDocument({ artifact, content, scope });
    } catch (cause) { if (!abort.signal.aborted) setError(cause as Error); }
    finally { if (current.current === abort) setBusy(undefined); }
  }
  async function download(artifact: ReadableArtifact) {
    if (!artifact.download_url) return;
    setError(undefined);
    try {
      const result = await api.downloadFile(artifact.download_url);
      const url = URL.createObjectURL(result.blob); const link = window.document.createElement('a');
      link.href = url;
      link.download = result.filename ?? artifact.path?.split(/[\\/]/).at(-1) ?? `${artifact.name}.md`;
      link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) { setError(cause as Error); }
  }
  return { document: document?.scope === scope ? document : undefined, error, busy, open, download, close: () => { current.current?.abort(); setDocument(undefined); setBusy(undefined); } };
}

export function ArtifactCard({ artifact, reader }: { artifact: ReadableArtifact; reader: ReturnType<typeof useArtifactReader> }) {
  const [copyError, setCopyError] = useState<Error>(); const [copied, setCopied] = useState(false);
  const directory = artifact.kind === 'code' || artifact.kind === 'test_code';
  return <article className="artifact-card"><div><span className="artifact-kind">{directory ? artifact.kind === 'test_code' ? '测试代码' : '项目代码' : '文档'}</span>
    <strong>{artifact.name}</strong>{artifact.path && <code className="artifact-path">{artifact.path}</code>}{artifact.storage_error && <small>{artifact.storage_error}</small>}</div>
    <div className="artifact-actions">{(!directory || artifact.preview_url) && <button className="text-button" disabled={reader.busy === artifact.artifact_id} onClick={() => void reader.open(artifact)} aria-label={`${directory ? '查看说明' : '阅读'} ${artifact.name}`}>{reader.busy === artifact.artifact_id ? '读取中…' : directory ? '查看说明' : '阅读'}</button>}
      {artifact.download_url && <button className="text-button" onClick={() => void reader.download(artifact)} aria-label={`${directory ? '下载说明' : '下载'} ${artifact.name}`}>{directory ? '下载说明' : '下载'}</button>}
      {directory && artifact.path && <button className="text-button" onClick={async () => {
        try { await navigator.clipboard.writeText(artifact.path!); setCopied(true); } catch { setCopyError(new Error('复制失败，请复制上方目录。')); }
      }}>{copied ? '已复制' : '复制目录'}</button>}</div><ErrorBox error={copyError} />
  </article>;
}

export function ArtifactPreview({ reader }: { reader: ReturnType<typeof useArtifactReader> }) {
  const preview = useRef<HTMLElement | null>(null);
  useEffect(() => { if (reader.document) preview.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }); }, [reader.document?.artifact.artifact_id]);
  return <><ErrorBox error={reader.error} />{reader.document && <section ref={preview} className="artifact-preview" aria-label="文档阅读区">
    <div className="artifact-preview-heading"><div><h3>{reader.document.artifact.name}</h3>{reader.document.artifact.path && <code>{reader.document.artifact.path}</code>}</div>
      <button className="text-button" onClick={reader.close}>关闭预览</button></div>
    <MarkdownDocument content={reader.document.content} />
  </section>}</>;
}
