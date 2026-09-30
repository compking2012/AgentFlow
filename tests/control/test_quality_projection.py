import json

import pytest
from test_workflow import flow as flow
from test_workflow import start

from agentflow.control.presentation import RunPresentationService
from agentflow.execution.manifests import file_digest
from agentflow.execution.transport import ArtifactTransport
from agentflow.testing.reports import parse_junit


async def evidence_fixture(flow, tmp_path, *, passing=False):
    service, store, artifacts, project, _ = flow
    run = await start(flow)
    report_file = tmp_path / 'raw.xml'
    report_file.write_text('<testsuite><testcase name="passes" time="0.02"/><testcase name="fails" time="0.01">'
                           '<failure message="actual failed assertion"/></testcase><testcase name="skips"><skipped/>'
                           '</testcase></testsuite>')
    if passing:
        report_file.write_text('<testsuite><testcase name="first"/><testcase name="second"/>'
                               '<testcase name="third"/></testsuite>')
    normalized = parse_junit(report_file).model_dump(mode='json')
    digest = file_digest(report_file)
    target = ArtifactTransport(store, service.settings.data_dir / 'nodes/artifacts').object_path(digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(report_file.read_bytes())
    def seed(tx):
        tx.put('node_artifact', 'report', {'state': 'complete', 'digest': digest})
        tx.put('candidate', 'candidate', {'run_id': run['id'], 'run_input_fingerprint': run['input_fingerprint'],
            'fingerprint': 'current-fingerprint', 'source_repository': project['local_path'],
            'matrix_mappings': {key: {'phase': 'unit', 'target_config_id': 'api'} for key in ['one', 'two']}})
        tx.put('target_matrix', 'candidate', {'state': 'bound_to_platform_manifest', 'candidate_fingerprint': 'current-fingerprint'})
        tx.put('node_result', 'result', {'assessment_state': 'validated', 'verified_checks': [
            {'matrix_entry_id': key, 'raw_report_artifact_version_id': 'report', 'normalized_report': normalized}
            for key in ['one', 'two']]})
        for key in ['one', 'two']:
            tx.put('check', key, {'run_id': run['id'], 'matrix_entry_id': key, 'candidate_fingerprint': 'current-fingerprint',
                'evidence_verified': True, 'node_result_id': 'result', 'raw_report_artifact_id': 'report'})
        return {}
    await store.command('test.quality-state', 'seed', {}, seed)
    return RunPresentationService(store, artifacts, service.settings), run, target


async def test_pass_rate_comes_from_cases_and_shared_raw_reports_are_not_double_counted(flow, tmp_path):
    presenter, run, _ = await evidence_fixture(flow, tmp_path)
    view = await presenter.quality(run['id'])
    tests = view['unit_tests']
    assert tests['verified'] and tests['coverage_complete']
    assert tests['total'] == 3 and tests['passed'] == 1 and tests['failed'] == 1 and tests['skipped'] == 1
    assert tests['pass_rate'] == pytest.approx(1 / 3) and tests['duration_seconds'] == pytest.approx(.03)
    assert tests['status'] == 'failed'
    assert view['integration_tests']['pass_rate'] is None
    assert view['bugs']['status'] == 'not_run' and view['bugs']['total'] == 0, 'Failed cases are not code bug counts'
    assert view['performance']['status'] == 'not_measured' and not view['performance']['metrics']


async def test_declared_but_unmeasured_platform_prevents_complete_pass_rate(flow, tmp_path):
    presenter, run, _ = await evidence_fixture(flow, tmp_path)
    _, store, _, _, _ = flow
    def declare(tx):
        plan = tx.get('plan', run['plan_id'])
        return tx.put('plan', plan['id'], {**plan, 'app_targets': ['api', 'ios_native'],
            'target_configs': [{'target_config_id': 'api', 'app_target': 'api'},
                               {'target_config_id': 'ios', 'app_target': 'ios_native'}]}, plan['revision'])
    await store.command('test.declare-platforms', 'platforms', {}, declare)
    tests = (await presenter.quality(run['id']))['unit_tests']
    assert not tests['coverage_complete'] and tests['pass_rate'] is None


@pytest.mark.parametrize('missing', [None, 'web_matrix_entry', 'web_mapping', 'unit_report', 'native_target', 'target_mismatch'])
@pytest.mark.parametrize('passing', [False, True])
async def test_phase_coverage_uses_bound_matrix_instead_of_requiring_each_platform_in_each_phase(
    flow, tmp_path, missing, passing,
):
    presenter, run, _ = await evidence_fixture(flow, tmp_path, passing=passing)
    _, store, _, _, _ = flow

    def declare(tx):
        plan = tx.get('plan', run['plan_id'])
        targets = ['api', 'web'] + (['ios_native'] if missing == 'native_target' else [])
        tx.put('plan', plan['id'], {**plan, 'app_targets': targets,
            'target_configs': [{'target_config_id': target, 'app_target': target} for target in targets]}, plan['revision'])
        candidate = tx.get('candidate', 'candidate')
        mappings = dict(candidate['matrix_mappings'])
        if missing != 'web_mapping':
            mappings['web-integration'] = {'phase': 'integration',
                'target_config_id': 'other' if missing == 'target_mismatch' else 'web'}
        tx.put('candidate', candidate['id'], {**candidate, 'matrix_mappings': mappings}, candidate['revision'])
        matrix = tx.get('target_matrix', 'candidate')
        entries = [{'matrix_entry_id': key, 'target_config_id': 'api', 'app_target': 'api'} for key in ['one', 'two']]
        if missing != 'web_matrix_entry':
            entries.append({'matrix_entry_id': 'web-integration', 'target_config_id': 'web', 'app_target': 'web'})
        tx.put('target_matrix', matrix['id'], {**matrix, 'plan': {'entries': entries}}, matrix['revision'])
        if missing == 'unit_report':
            check = tx.get('check', 'two')
            tx.put('check', check['id'], {**check, 'stale': True}, check['revision'])
        return {}

    await store.command('test.phase-targets', str(missing), {}, declare)
    quality = await presenter.quality(run['id'])
    unit = quality['unit_tests']
    assert unit['coverage_complete'] is (missing is None)
    assert unit['pass_rate'] == (pytest.approx(1 if passing else 1 / 3) if missing is None else None)
    expected_status = 'failed' if not passing else 'passed' if missing is None else 'incomplete'
    assert unit['status'] == expected_status
    assert quality['integration_tests']['status'] == 'not_run'


async def test_independent_reports_with_same_case_name_cannot_overwrite_failed_execution(flow, tmp_path):
    presenter, run, _ = await evidence_fixture(flow, tmp_path)
    service, store, _, _, _ = flow
    transport = ArtifactTransport(store, service.settings.data_dir / 'nodes/artifacts')
    for index, failed in enumerate([True, False]):
        path = tmp_path / f'report-{index}.xml'
        path.write_text('<testsuite><testcase name="same-case">' + ('<failure/>' if failed else '') + '</testcase></testsuite>')
        report = parse_junit(path).model_dump(mode='json')
        digest = file_digest(path)
        stored = transport.object_path(digest)
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(path.read_bytes())
        key = ['one', 'two'][index]
        def record(tx):
            tx.put('node_artifact', f'raw-{index}', {'state': 'complete', 'digest': digest})
            tx.put('node_result', f'result-{index}', {'assessment_state': 'validated', 'verified_checks': [
                {'matrix_entry_id': key, 'raw_report_artifact_version_id': f'raw-{index}', 'normalized_report': report}]})
            check = tx.get('check', key)
            return tx.put('check', key, {**check, 'node_result_id': f'result-{index}',
                'raw_report_artifact_id': f'raw-{index}'}, check['revision'])
        await store.command('test.independent-report', str(index), {}, record)
    result = (await presenter.quality(run['id']))['unit_tests']
    assert result['total'] == 2 and result['failed'] == 1 and result['passed'] == 1
    assert result['status'] == 'failed' and result['pass_rate'] == .5


@pytest.mark.parametrize('damage', ['new_input', 'new_candidate', 'unbound_matrix', 'corrupt_raw', 'unverified_result'])
async def test_stale_unbound_or_corrupt_evidence_never_reports_current_pass_rate(flow, tmp_path, damage):
    presenter, run, path = await evidence_fixture(flow, tmp_path)
    _, store, _, _, _ = flow
    if damage == 'corrupt_raw':
        path.write_text('corrupted')
    else:
        kind, identity, changes = {
            'new_input': ('run', run['id'], {'input_fingerprint': 'changed'}),
            'new_candidate': ('candidate', 'candidate', {'fingerprint': 'other'}),
            'unbound_matrix': ('target_matrix', 'candidate', {'state': 'not_bound'}),
            'unverified_result': ('node_result', 'result', {'assessment_state': 'rejected'}),
        }[damage]
        def alter(tx):
            item = tx.get(kind, identity)
            return tx.put(kind, identity, {**item, **changes}, item['revision'])
        await store.command('test.damage', damage, {}, alter)
    view = await presenter.quality(run['id'])
    assert view['unit_tests']['pass_rate'] is None and not view['unit_tests']['verified']


async def test_current_review_lists_all_findings_with_human_categories(flow):
    from agentflow.domain.planning import WorkSpec
    service, store, artifacts, _, _ = flow
    run = await start(flow)
    await service.add_work_items(run['id'], [WorkSpec('review', 'code_review', 'review')], run['revision'], 'review')
    work = next(w for w in await store.list('work_item') if w['key'] == 'review')
    body = {'reviewed_commit': 'reviewed-code', 'summary': 'review', 'findings': [
        {'severity': 'blocking', 'category': 'bug', 'path': 'src/api.mjs', 'description': '未处理不存在的记录'},
        {'severity': 'warning', 'category': 'style', 'path': 'src/api.mjs', 'description': '命名格式不统一'}]}
    blob = await artifacts.put_bytes(json.dumps({'result': body}, ensure_ascii=False).encode())
    def seed(tx):
        tx.put('artifact', 'review-artifact', {'work_item_id': work['id'], 'run_id': run['id'], 'generation': 1,
            'digest': blob['id'], 'media_type': 'application/json', 'name': 'review.json'})
        tx.put('work_item', work['id'], {**work, 'status': 'completed', 'artifact_ids': ['review-artifact']}, work['revision'])
        return tx.put('review', 'review-result', {'work_item_id': work['id'], 'generation': 1, 'blocking_findings': body['findings'][:1]})
    await store.command('test.review', 'review-record', {}, seed)
    quality = await RunPresentationService(store, artifacts, service.settings).quality(run['id'])
    assert quality['bugs']['total'] == 2
    assert {issue['category'] for issue in quality['bugs']['items']} == {'bug', 'style'}
