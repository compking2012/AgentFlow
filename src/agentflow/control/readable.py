"""Human documents derived from immutable workflow evidence, never a quality gate."""
from __future__ import annotations

import json
import re

from agentflow.control.document_policy import PRODUCT_STEPS, language_for

STEP_OUTPUTS = {
    'review_disposition': ('审查归属分析', '审查返工方案', '区分生产代码修复与已批准的测试契约迁移。'),
    'review_unit_migration': ('既有单测迁移', '既有单元测试迁移说明', '只迁移已批准的期望值，不改变断言和用例标识。'),
    'review_integration_migration': ('既有集成测试迁移', '既有集成测试迁移说明', '只迁移已批准的期望值，不改变断言和用例标识。'),
    'review_validation': ('返工诊断验证', '既有测试诊断报告', '验证当前修复；不替代后续正式测试门禁。'),
    'goal': ('目标整理', '产品目标说明', '完善原始产品目标，说明目标用户、产品价值、范围和成功表现。'),
    'research': ('调研分析', '市场与竞品调研报告', '研究用户、竞品能力、竞争力、商业模式和真实反馈，注明可验证来源。'),
    'prd': ('产品需求', '产品需求文档 PRD', '形成需求范围、用户流程、需求编号与验收标准。'),
    'requirements': ('需求拆解', '需求清单与验收标准', '拆分具名需求、依赖关系和正常及异常验收场景。'),
    'architecture': ('系统架构', '系统架构与接口设计', '检查或调整系统架构、架构图、API、数据模型和变更影响。'),
    'development_plan': ('开发任务规划', '开发任务实施计划', '分解实现、审查和测试任务，并确定依赖与文件范围。'),
    'implementation': ('功能实现', '完整项目代码', '交付当前功能的完整源码快照及变更说明。'),
    'code_review': ('代码审查', '代码审查报告', '审查准确代码版本，列出问题位置、严重程度和修改建议。'),
    'unit_test_plan': ('单元测试设计', '单元测试方案', '定义单测用例、边界情况及需求覆盖关系。'),
    'integration_test_strategy': ('集成测试设计', '集成测试技术方案', '按产品平台选择测试工具、场景、性能测量及验收标准。'),
    'unit_test_implementation': ('单元测试编写', '单元测试代码', '形成可运行的单元测试代码和固定用例标识。'),
    'integration_test_implementation': ('集成测试编写', '集成测试代码', '形成 Web/API 或目标平台可执行的集成测试代码。'),
    'unit_test_execution': ('单元测试', '单元测试报告', '执行原始测试，报告通过率、失败用例与执行耗时。'),
    'integration_test_execution': ('集成测试', '集成测试报告', '验证集成行为，报告通过率、失败用例及实际测量指标。'),
    'delivery': ('代码交付', '产品交付说明', '交付已通过门禁的代码、运行包、文档和测试报告。'),
    'retrospective': ('迭代复盘', '迭代复盘报告', '汇总本轮完成范围、质量和遗留事项。'),
}

_EN_STEP_OUTPUTS = {
    'goal': ('Product goal', 'Product Goal', 'Refine the original goal, intended users, value, scope and signs of success.'),
    'research': ('Market research', 'Market and Competitor Research', 'Research users, competitors, differentiation, business models and verified feedback.'),
    'prd': ('Product requirements', 'Product Requirements Document', 'Define scope, user journeys, requirement IDs and acceptance criteria.'),
    'requirements': ('Requirement breakdown', 'Requirements and Acceptance Criteria', 'List traceable requirements, dependencies and normal/error acceptance cases.'),
    'architecture': ('System architecture', 'Architecture and Interface Design', 'Assess architecture, interfaces, data models and change impact.'),
    'development_plan': ('Development planning', 'Development Plan', 'Plan implementation, review and test tasks, dependencies and file scopes.'),
    'implementation': ('Implementation', 'Project Source Code', 'Deliver the exact source snapshot and change notes.'),
    'code_review': ('Code review', 'Code Review Report', 'Review the exact code version with actionable findings.'),
    'unit_test_plan': ('Unit test design', 'Unit Test Plan', 'Define unit cases, boundaries and requirement coverage.'),
    'integration_test_strategy': ('Integration test design', 'Integration Test Strategy', 'Define platform tools, scenarios, measurements and acceptance criteria.'),
    'unit_test_implementation': ('Unit test implementation', 'Unit Test Code', 'Deliver executable unit tests with stable case IDs.'),
    'integration_test_implementation': ('Integration test implementation', 'Integration Test Code', 'Deliver executable integration tests for the declared platforms.'),
    'unit_test_execution': ('Unit testing', 'Unit Test Report', 'Report actual unit test results, failures and case durations.'),
    'integration_test_execution': ('Integration testing', 'Integration Test Report', 'Report actual integration results and measured performance.'),
    'delivery': ('Code delivery', 'Product Delivery Notes', 'Describe the verified code, runtime package, documents and test reports.'),
    'retrospective': ('Iteration retrospective', 'Iteration Retrospective', 'Summarize delivered scope, quality and unfinished work.'),
}


def _text(language, chinese, english):
    return english if language == 'en' else chinese


def output_contract(work, language=None):
    language = language_for(work, language)
    step = work.get('step', '')
    outputs = _EN_STEP_OUTPUTS if language == 'en' else STEP_OUTPUTS
    fallback = ('Engineering task', 'Stage Notes', 'Provide readable results for this task.') if language == 'en' else (
        '研发任务', '阶段产物说明', '交付该任务的可读结果。')
    name, artifact, purpose = outputs.get(step, fallback)
    stage_key = work.get('logical_stage_key', work.get('key'))
    if stage_key == 'unit_test_implementation:review':
        name = _text(language, '单元测试代码审查', 'Unit test code review')
        artifact = _text(language, '单元测试代码审查报告', 'Unit Test Code Review Report')
    elif stage_key == 'integration_test_implementation:review':
        name = _text(language, '集成测试代码审查', 'Integration test code review')
        artifact = _text(language, '集成测试代码审查报告', 'Integration Test Code Review Report')
    return {'name': name, 'artifact_name': artifact, 'purpose': purpose,
            'kind': 'code' if step == 'implementation' else 'test_code' if step.endswith('_implementation') else 'document'}


def _cell(value, language='zh-CN'):
    if isinstance(value, (list, tuple)):
        value = ', '.join(_cell(item, language) for item in value)
    return str(value if value is not None else _text(language, '未提供', 'Not provided')).replace('|', '\\|').replace('\n', ' ')


def _section(name, values, language='zh-CN'):
    if not values:
        return ''
    return '\n## ' + name + '\n\n' + '\n'.join('- ' + _cell(v, language) for v in values) + '\n'


def _already_written(value, body):
    def normalized(text):
        return re.sub(r'[\s`*_#]+', ' ', str(text)).strip().casefold()
    needle = normalized(value)
    return bool(needle) and needle in normalized(body)


def _source_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return ' — '.join(str(value[k]) for k in ('title', 'publisher', 'url', 'date') if value.get(k))
    return ''


def _status(value, language):
    labels = {'blocking': '阻塞', 'warning': '警告', 'note': '提示', 'bug': '缺陷', 'security': '安全',
              'style': '格式', 'performance': '性能', 'maintainability': '可维护性', 'unclassified': '未分类',
              'passed': '通过', 'failed': '失败', 'error': '错误', 'skipped': '跳过', 'unknown': '未知',
              'completed': '已执行', 'not_run': '未执行', 'not_applicable': '不适用',
              'unit': '单元', 'integration': '集成'}
    return labels.get(value, value) if language != 'en' and isinstance(value, str) else value


def _table(headers, rows, language):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join('---' for _ in headers) + ' |',
                      *['| ' + ' | '.join(_cell(value, language) for value in row) + ' |' for row in rows]])


def render_document(work, sources, language=None):
    """Prefer the authored document; add only evidence relevant to this stage."""
    language = language_for(work, language)
    contract = output_contract(work, language)
    step = work.get('step', '')
    heading = contract['artifact_name']
    if work.get('kind') == 'aggregation':
        heading = contract['name'] + _text(language, '汇总', ' Summary')
    elif work.get('parent_stage_id'):
        subject = str(work.get('payload', {}).get('goal') or work.get('key') or
                      _text(language, '子任务', 'Subtask')).splitlines()[0][:32]
        heading += _text(language, '：', ': ') + subject
    result, seen = [], set()
    preferred = [s for s in sources if s[0].get('name') in {
        'openhands_final.json', 'codex_final.json', 'codex_final.normalized.json', 'platform-results.json'}]
    selected = preferred or sources
    for meta, raw in selected:
        fingerprint = json.dumps(raw, sort_keys=True, ensure_ascii=False)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        if isinstance(raw, str):
            if meta.get('media_type', meta.get('content_type')) in {'text/markdown', 'text/plain'}:
                if not result and not raw.lstrip().startswith('# '):
                    result.append(f'# {heading}\n')
                result.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        if isinstance(raw.get('result'), dict):
            raw = raw['result']
        content = raw.get('content')
        body = content.strip() if isinstance(content, str) else ''
        if not body:
            body = str(raw.get('summary') or '').strip()
        if not result and not body.startswith('# '):
            result.append('# ' + _cell(raw.get('title') or heading, language) + '\n')
        if body:
            result.append(body)
        if step == 'review_disposition' and isinstance(raw.get('actions'), list):
            for action in raw['actions']:
                label = {'production_fix': '生产代码修复', 'test_contract_migration': '既有测试契约迁移',
                         'needs_clarification': '需要核对'}.get(action.get('classification'), '归属分析')
                result.append('\n## ' + label + '\n\n' + str(action.get('reason', '')))
                result.append('\n发现位置：' + '、'.join(action.get('evidence_paths', [])))
                result.append('\n修复位置：' + '、'.join(action.get('repair_paths', [])))
                for migration in action.get('migrations', []):
                    result.append('\n用例：`' + migration['case_id'] + '`\n\n原期望：\n```js\n'
                        + migration['old_expected'] + '\n```\n新期望：\n```js\n' + migration['new_expected'] + '\n```')
        if step == 'code_review' and isinstance(raw.get('findings'), list):
            findings = [f for f in raw['findings'] if isinstance(f, dict)]
            blocking = sum(f.get('severity') == 'blocking' for f in findings)
            result.append(_text(language, f'\n## 审查结果\n\n共 {len(findings)} 项问题，其中 {blocking} 项阻塞问题。',
                                f'\n## Review Findings\n\n{len(findings)} findings, including {blocking} blocking findings.'))
            if findings:
                headers = _text(language, ['严重程度', '分类', '文件', '问题与建议'],
                                ['Severity', 'Category', 'File', 'Finding and Recommendation'])
                result.append(_table(headers, [[_status(f.get('severity'), language),
                    _status(f.get('category', 'unclassified'), language), f.get('path'), f.get('description')]
                    for f in findings], language))
            result.append(_text(language, '\n审查方式：Agent 静态代码审查；独立工具检查以执行记录为准。',
                                '\nMethod: static agent code review; independent tool checks require execution evidence.'))
            if raw.get('reviewed_commit'):
                result.append(_text(language, '\n审查代码版本：`', '\nReviewed commit: `') + _cell(raw['reviewed_commit']) + '`')
        if step in {'unit_test_execution', 'integration_test_execution'} and raw.get('checks'):
            checks = [c for c in raw['checks'] if isinstance(c, dict)]
            result.append(_text(language, '\n## 验证检查\n', '\n## Verification Checks\n'))
            result.append(_table(_text(language, ['检查', '执行', '质量', '用例数'],
                                       ['Check', 'Execution', 'Quality', 'Case Count']),
                [[c.get('matrix_entry_id'), _status(c.get('execution_status'), language),
                  _status(c.get('quality_result'), language), c.get('executed_case_count')] for c in checks], language))
        if step in {'unit_test_plan', 'integration_test_strategy'} and raw.get('test_cases'):
            cases = [c for c in raw['test_cases'] if isinstance(c, dict)]
            result.append(_text(language, '\n## 测试用例与需求对应\n', '\n## Test Cases and Requirements\n'))
            result.append(_table(_text(language, ['用例', '需求', '目标配置', '阶段', '框架标识'],
                                       ['Case', 'Requirement', 'Target Configuration', 'Phase', 'Framework IDs']),
                [[c.get('case_id'), c.get('requirement_id'), c.get('target_config_id'),
                  _status(c.get('phase'), language), c.get('framework_case_ids')] for c in cases], language))
        if step == 'development_plan' and raw.get('parallel_work'):
            groups = []
            for group in raw['parallel_work']:
                if not isinstance(group, dict):
                    continue
                children = [child for child in group.get('children', []) if isinstance(child, dict)
                            and not (_already_written(child.get('key', ''), body)
                                     and _already_written(child.get('goal', ''), body))]
                if children:
                    groups.append('### ' + _cell(group.get('stage_key'), language) + '\n')
                    groups.extend('- **' + _cell(child.get('key'), language) + '**: ' + _cell(child.get('goal'), language)
                                  for child in children)
            if groups:
                result.append(_text(language, '\n## 并行任务安排\n', '\n## Parallel Tasks\n') + '\n'.join(groups))
        for field, label in [('sources', _text(language, '来源', 'Sources')),
                             ('unknowns', _text(language, '待确认事项', 'Open Questions'))]:
            if isinstance(raw.get(field), list):
                values = []
                for entry in raw[field]:
                    value = _source_text(entry)
                    urls = re.findall(r'https?://[^\s)>]+', value)
                    if value and not _already_written(value, body) and not (
                            field == 'sources' and urls and all(url in body for url in urls)):
                        if value not in values:
                            values.append(value)
                result.append(_section(label, values, language))
        if step not in PRODUCT_STEPS and raw.get('commit_oid'):
            result.append(_text(language, '\n源码版本：`', '\nSource commit: `') + _cell(raw['commit_oid']) + '`\n')
        if step in {'implementation', 'unit_test_implementation', 'integration_test_implementation'} and raw.get('changes'):
            result.append(_section(_text(language, '代码变更文件', 'Changed Files'),
                [v['path'] for v in raw['changes'] if isinstance(v, dict) and v.get('path')
                 and not _already_written(v['path'], body)], language))
        if step == 'delivery' and raw.get('confirmed_at'):
            result.append(_text(language, '\nGit 交付已确认：', '\nGit delivery confirmed: ') + _cell(raw['confirmed_at']) + '\n')
    if not result:
        result.append(f'# {heading}\n')
    result.append(_text(language, f"\n---\n工作阶段：{contract['name']} · 第 {work.get('generation', 1)} 版。\n",
                        f"\n---\nStage: {contract['name']} · Version {work.get('generation', 1)}.\n"))
    return '\n'.join(result)


async def document_sources(artifacts, metadata):
    values = []
    for item in metadata:
        if item.get('readable'):
            continue
        verified = await artifacts.verify(item['digest'])
        if verified['size'] > 2 * 1024 * 1024:
            continue
        raw = await artifacts.read(item['digest'])
        media = item.get('media_type', item.get('content_type', ''))
        if media == 'application/json' or str(item.get('name', '')).endswith('.json'):
            try:
                values.append((item, json.loads(raw)))
            except (ValueError, UnicodeError):
                continue
        elif media in {'text/markdown', 'text/plain'} or str(item.get('name', '')).endswith(('.md', '.txt')):
            values.append((item, raw.decode('utf-8', errors='replace')))
    return values


def safe_filename(value):
    return re.sub(r'[\\/\x00-\x1f:*?"<>|]', '-', str(value)).strip('. ')[:100] or '阶段产物'


def test_report_markdown(title, reports, language='zh-CN'):
    language = language_for(language=language)
    rows = [[case.get('case_id'), _status(case.get('status'), language), case.get('duration_seconds'), case.get('message')]
            for report in reports for case in report.get('cases', [])]
    lines = [f'# {title}\n', _table(_text(language, ['测试用例', '结果', '耗时（秒）', '详情'],
                                          ['Test Case', 'Result', 'Duration (Seconds)', 'Details']), rows, language)]
    for report in reports:
        lines.append(_section(_text(language, '报告完整性问题', 'Report Completeness Issues'),
                              [*report.get('errors', []), *report.get('missing_case_ids', [])], language))
        metrics = report.get('performance_metrics', [])
        if metrics:
            lines.append(_text(language, '\n## 性能测量\n', '\n## Performance Measurements\n'))
            lines.append(_table(_text(language, ['指标', '测量值', '单位', '样本数', '来源用例'],
                                      ['Metric', 'Measured Value', 'Unit', 'Sample Count', 'Source Case']),
                [[metric.get(k) for k in ('name', 'value', 'unit', 'sample_count', 'case_id')]
                 for metric in metrics], language))
    return '\n'.join(lines) + '\n'
