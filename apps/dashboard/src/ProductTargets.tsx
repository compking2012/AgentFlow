import type { ProductTarget } from './types';
import { PRODUCT_TARGETS } from './types';

export const productTargetNames: Record<ProductTarget, string> = {
  web: 'Web 应用', api: 'API 服务', ios: 'iOS', android: 'Android', windows: 'Windows', macos: 'macOS', linux: 'Linux',
};
const descriptions: Record<ProductTarget, string> = {
  web: '浏览器界面与交互', api: '后端接口与业务服务', ios: 'iPhone 与 iPad 应用', android: 'Android 应用',
  windows: 'Windows 桌面应用', macos: 'Mac 桌面应用', linux: 'Linux 桌面应用',
};
export const nativeProductTarget = (target: ProductTarget) => target !== 'web' && target !== 'api';

export function ProductTargets({ targets, onChange, disabled = false }: {
  targets: ProductTarget[]; onChange: (targets: ProductTarget[]) => void; disabled?: boolean;
}) {
  return <fieldset className="product-platforms" disabled={disabled}><legend>产品平台（可多选）</legend>
    <div className="product-platform-grid">{PRODUCT_TARGETS.map(target => <label key={target}
      className={`product-platform-card${targets.includes(target) ? ' product-platform-selected' : ''}`}>
      <input type="checkbox" aria-label={productTargetNames[target]} checked={targets.includes(target)}
        onChange={event => onChange(PRODUCT_TARGETS.filter(value => value === target ? event.target.checked : targets.includes(value)))} />
      <span><strong>{productTargetNames[target]}</strong><small>{descriptions[target]}</small>
        {nativeProductTarget(target) && <span className="product-platform-unavailable">暂不支持自动研发</span>}</span>
    </label>)}</div>
    <p className="product-platform-hint">Web 与 API 可一起选择。Web 研发会包含所需的 API 验证；原生平台目前可识别、登记，暂不执行研发。</p>
  </fieldset>;
}
