import copy

import pytest

from agentflow.control.document_policy import language_for, stage_instructions
from agentflow.control.readable import output_contract, render_document
from agentflow.control.readable import test_report_markdown as render_test_report


def source(payload):
    return ({'name': 'openhands_final.json', 'media_type': 'application/json'}, payload)


@pytest.mark.parametrize(('step', 'expected'), [
    ('goal', ['原始意图', '只润色', '不写框架']),
    ('research', ['竞品能力', '商业模式', '用户反馈', '尚未完成外部调研', '不得编造']),
    ('prd', ['用户流程', '验收标准', '不要放入模块架构']),
    ('requirements', ['产品需求', '原 PRD', '不要把需求拆解写成代码文件']),
])
def test_product_writing_policy_has_stage_specific_boundaries_and_proportionate_scope(step, expected):
    policy = stage_instructions(step)
    for phrase in expected:
        assert phrase in policy
    assert '简单项目保持单任务' in policy
    assert '不凑字数' in policy
    assert '正文不插入 Agent 排程' in policy


@pytest.mark.parametrize('step', [
    'goal', 'research', 'prd', 'requirements', 'architecture', 'development_plan', 'implementation',
    'code_review', 'unit_test_plan', 'integration_test_strategy', 'unit_test_implementation',
    'integration_test_implementation', 'unit_test_execution', 'integration_test_execution', 'delivery', 'retrospective',
])
def test_aggregation_inherits_the_exact_stage_policy_and_english_language(step):
    individual = stage_instructions(step, language='en')
    aggregated = stage_instructions(step, language='en', aggregation=True)
    assert aggregated.startswith(individual)
    assert 'Document audience:' in individual and 'in English' in individual
    assert 'Do not pad text' in individual
    assert 'unsupported child claims into verified facts' in aggregated
    assert not any('\u4e00' <= character <= '\u9fff' for character in aggregated)


@pytest.mark.parametrize('step', ['goal', 'research', 'prd', 'requirements'])
def test_product_document_prefers_authored_body_and_excludes_machine_task_details(step):
    payload = {'title': 'A Product', 'summary': 'PREVIEW ONLY',
               'content': '# A Product\n\nHelp readers keep a useful reading list.',
               'sources': [], 'unknowns': [], 'commit_oid': 'internal-commit',
               'parallel_work': [{'stage_key': 'implementation', 'children': [
                   {'key': 'storage', 'goal': 'INTERNAL SCHEDULING', 'write_paths': ['src/database.mjs']}]}],
               'test_cases': [{'case_id': 'INTERNAL CASE'}],
               'checks': [{'matrix_entry_id': 'INTERNAL MATRIX'}],
               'findings': [{'description': 'UNRELATED FINDING'}],
               'changes': [{'path': 'src/database.mjs'}]}
    before = copy.deepcopy(payload)
    rendered = render_document({'step': step, 'language': 'en'}, [source(payload), source(payload)])
    assert rendered.count('# A Product') == 1
    assert rendered.count('Help readers keep a useful reading list.') == 1
    for unwanted in ('PREVIEW ONLY', 'INTERNAL', 'UNRELATED', 'internal-commit', 'database.mjs', 'Parallel Tasks'):
        assert unwanted not in rendered
    assert not any('\u4e00' <= character <= '\u9fff' for character in rendered)
    assert payload == before, 'Human rendering must not change immutable machine evidence'


def test_sources_and_unknowns_are_preserved_once_without_empty_sections():
    url = 'https://example.org/research'
    body = f'# Research\n\n[Original source]({url}) supports the capability comparison.\n\nPricing is unverified.'
    payload = {'title': 'Research', 'summary': 'Duplicate preview', 'content': body,
               'sources': [f'Original source — {url}', 'https://example.org/feedback', 'https://example.org/feedback'],
               'unknowns': ['Pricing is unverified.', 'Customer demand still needs validation.']}
    rendered = render_document({'step': 'research', 'language': 'en'}, [source(payload)])
    assert rendered.count(url) == 1
    assert rendered.count('https://example.org/feedback') == 1
    assert rendered.count('Pricing is unverified.') == 1
    assert 'Customer demand still needs validation.' in rendered
    assert 'Sources' in rendered and 'Open Questions' in rendered
    assert 'Parallel Tasks' not in rendered


def test_review_keeps_blocking_evidence_even_if_summary_is_overconfident():
    payload = {'summary': 'The implementation looks complete.', 'reviewed_commit': 'abc123', 'findings': [
        {'severity': 'blocking', 'category': 'security', 'path': 'src/access.mjs',
         'description': 'The owner check is absent.'}]}
    rendered = render_document({'step': 'code_review', 'language': 'en'}, [source(payload)])
    assert '1 findings, including 1 blocking findings.' in rendered
    assert 'security' in rendered and 'The owner check is absent.' in rendered
    assert 'Reviewed commit: `abc123`' in rendered
    assert 'static agent code review' in rendered
    assert '未提供' not in rendered and '审查结果' not in rendered


def test_test_plan_preserves_target_and_framework_mapping_as_technical_evidence():
    payload = {'title': 'Unit Strategy', 'summary': 'Preview', 'content': 'Isolate persistence and check invalid input.',
               'test_cases': [{'case_id': 'UNIT-1', 'requirement_id': 'READ-1', 'target_config_id': 'web-production',
                               'phase': 'unit', 'framework_case_ids': ['books rejects empty title']}],
               'sources': [], 'unknowns': []}
    rendered = render_document({'step': 'unit_test_plan', 'language': 'en'}, [source(payload)])
    for value in ('UNIT-1', 'READ-1', 'web-production', 'books rejects empty title'):
        assert value in rendered
    assert 'Target Configuration' in rendered
    assert "['books" not in rendered
    assert 'Sources' not in rendered and 'Open Questions' not in rendered


def test_development_plan_omits_empty_or_already_written_parallel_task_lists():
    content = 'Implement storage: Persist reader entries.'
    payload = {'content': content, 'parallel_work': [{'stage_key': 'implementation', 'children': [
        {'key': 'storage', 'goal': 'Persist reader entries.'}]}]}
    rendered = render_document({'step': 'development_plan', 'language': 'en'}, [source(payload)])
    assert rendered.count('Persist reader entries.') == 1
    assert 'Parallel Tasks' not in rendered


def test_language_resolution_and_document_names_keep_legacy_defaults():
    assert language_for({}) == 'zh-CN'
    assert language_for({'language': 'en'}, 'zh-CN') == 'zh-CN'
    assert language_for({'payload': {'language': 'en'}}) == 'en'
    assert output_contract({'step': 'goal'})['artifact_name'] == '产品目标说明'
    assert output_contract({'step': 'research'})['artifact_name'] == '市场与竞品调研报告'
    assert output_contract({'step': 'research'}, language='en')['artifact_name'] == 'Market and Competitor Research'
    assert output_contract({'step': 'code_review', 'key': 'unit_test_implementation:review'},
                           language='en')['artifact_name'] == 'Unit Test Code Review Report'


def test_english_execution_report_preserves_failures_skips_and_actual_measurements():
    reports = [{'cases': [{'case_id': 'CASE-1', 'status': 'failed', 'duration_seconds': 0.2, 'message': 'Wrong value'},
                          {'case_id': 'CASE-2', 'status': 'skipped', 'duration_seconds': None, 'message': None}],
                'errors': ['Report is incomplete'], 'missing_case_ids': ['CASE-3'],
                'performance_metrics': [{'name': 'API latency p95', 'value': 32, 'unit': 'ms',
                                         'sample_count': 20, 'case_id': 'CASE-1'}]}]
    rendered = render_test_report('Integration Test Report', reports, language='en')
    assert '| CASE-1 | failed | 0.2 | Wrong value |' in rendered
    assert '| CASE-2 | skipped | Not provided | Not provided |' in rendered
    assert 'Report Completeness Issues' in rendered and 'CASE-3' in rendered
    assert '| API latency p95 | 32 | ms | 20 | CASE-1 |' in rendered
    assert not any('\u4e00' <= character <= '\u9fff' for character in rendered)


def test_runtime_review_uses_its_logical_test_stage_document_name():
    contract = output_contract({'step': 'code_review', 'key': 'runtime-repair-123:review',
                                'logical_stage_key': 'unit_test_implementation:review'})
    assert contract['artifact_name'] == '单元测试代码审查报告'
    assert output_contract({'step': 'code_review', 'logical_stage_key': 'integration_test_implementation:review'},
                           language='en')['artifact_name'] == 'Integration Test Code Review Report'
