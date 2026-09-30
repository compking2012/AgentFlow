import type { Target } from './types';

export const targetNames: Record<Target, string> = {
  web: 'Web 页面', api: 'API 服务', ios_native: 'iOS 原生', android_native: 'Android 原生',
  windows_native: 'Windows 原生', macos_native: 'macOS 原生', linux_native: 'Linux 桌面',
};
export const stepNames: Record<string, string> = {
  goal: '目标整理', research: '调研分析', prd: '产品需求文档', requirements: '需求拆解',
  architecture: '系统架构', development_plan: '开发任务', implementation: '功能实现',
  code_review: '代码审查', unit_test_plan: '单测方案', unit_test_implementation: '单测编写',
  unit_test_execution: '单测执行', integration_test_strategy: '集成方案',
  integration_test_implementation: '集成测试编写', integration_test_execution: '集成执行',
  delivery: '本地 Git 交付', retrospective: '迭代复盘', aggregation: '产物汇总',
};
export const steps = Object.keys(stepNames).filter(s => s !== 'aggregation');
export const roleNames: Record<string, string> = {
  research: '调研', product: '产品', architecture_planning: '架构 / 规划', development: '开发',
  review: 'Review', unit_test: '单元测试', integration_test: '集成测试', system: '系统',
};
export const stateNames: Record<string, string> = {
  pending: '待依赖', queued: '排队中', running: '执行中', waiting_approval: '待人工审核',
  completed: '步骤完成', failed: '失败', blocked: '阻塞', paused: '已暂停', cancelling: '取消中',
  cancel_requested: '正在停止', cancelled: '已取消', execution_unknown: '执行状态未知',
  ready: '已就绪', missing_inputs: '缺少输入', started: '已启动', stale: '已过期',
  passed: '通过', unknown: '未验证', inconclusive: '结论未定', error: '执行错误',
  not_run: '未执行', not_applicable: '本次不适用', approve: '已批准', reject: '已驳回',
  pending_user_confirmation: '等待确认模型版本', accepted: '已接受', rejected: '未接受',
  unverified: '待验证', configured: '已配置', missing: '未配置', revoked: '已吊销',
  offline: '离线', online: '在线', quarantined: '隔离待检', delivered: '已交付',
  waiting_execution: '等待节点执行', publishing: '正在交付', source_frozen: '源码已冻结',
  platform_frozen: '平台产物已冻结', platform_artifacts_frozen: '平台产物已冻结', testing: '平台测试中',
  bound_to_platform_manifest: '平台清单已绑定', planned: '已规划',
};
export const errorNames: Record<string, string> = {
  missing_target_config: '平台执行要求未齐备', pending_model_confirmation: '模型版本尚未确认',
  missing_credentials: '模型凭据未配置', missing_input: '前置产物缺失', invalid_input: '输入版本无效',
  invalid_repository: '无法读取 Git 仓库', invalid_path: '目录无效', directory_not_empty: '目录不是空目录',
  protected_path: '项目不能与控制数据目录重叠', dirty_worktree: '仓库包含未提交的修改',
  project_exists: '项目已经登记', stale_approval: '此审核版本已过期', revision_conflict: '对象已更新，请重新查看',
  plan_not_ready: '请先补齐运行条件', invalid_request: '请求内容不符合接口要求',
  invalid_bootstrap: '启动码无效或已过期', unauthorized: '本地会话已失效', forbidden: '操作未获授权',
  executor_unconfigured: '执行通道未配置', connection_error: '无法连接本地服务',
};
export const label = (value?: string) => value ? (stateNames[value] ?? value) : '未提供';

export const modelRequestLimit = (value?: number): string => value === 0 ? '不限次数'
  : Number.isInteger(value) && value! > 0 ? `${value} 次` : '未提供次数上限';
