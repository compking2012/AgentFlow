"""Real store/Git/NodeService orchestration, injected at validated receipt boundary."""
import importlib

import pytest
from test_execution_pipeline import fixture


def runner(env):
    assert importlib.util.find_spec('agentflow.control.review_diagnostics') is not None, 'diagnostic runner missing'
    from agentflow.control.review_diagnostics import ReviewDiagnostics
    return ReviewDiagnostics(env.store, env.workflow, env.nodes)


async def args(env):
    return (await env.store.read('run', env.run_id), {'id': 'batch-1'},
            {'repository': str(env.source), 'commit': env.snapshot['commit_oid'], 'tree_oid': env.snapshot['tree_oid']},
            [{'case_id': 'ast-case-1', 'target_config_id': 'target-0', 'phase': 'integration',
              'framework_case_ids': ['target-0::integration::persists']}])


async def finish_builds(env, state):
    for job_id in state['build_job_ids']:
        await env.build_receipt(job_id)


async def test_diagnostic_build_test_passes_without_formal_workflow_records(tmp_path):
    async with fixture(tmp_path) as env:
        diag = runner(env)
        inputs = await args(env)
        baseline = {kind: await env.store.list(kind) for kind in ['candidate', 'check', 'delivery', 'target_matrix', 'work_item', 'run']}
        state = await diag.start_or_poll(*inputs)
        assert state['state'] == 'waiting'
        assert state['build_job_ids']
        repeated = await diag.start_or_poll(*inputs)
        assert repeated['build_job_ids'] == state['build_job_ids']
        await finish_builds(env, state)
        state = await diag.start_or_poll(*inputs)
        assert state['test_job_ids']
        for job_id in state['test_job_ids']:
            await env.test_receipt(job_id)
        state = await diag.start_or_poll(*inputs)
        assert state['state'] == 'passed', state
        assert state['affected_case_ids'] == ['ast-case-1']
        assert state['reports']
        assert state['source_manifest']['source_commit'] == env.snapshot['commit_oid']
        for kind, rows in baseline.items():
            assert await env.store.list(kind) == rows
        job = await env.store.read('node_job', state['test_job_ids'][0])
        assert job['limits']['maximum_active_seconds'] == env.settings.node_active_seconds


@pytest.mark.parametrize('failure', ['failed', 'skipped', 'unknown', 'empty', 'wrong_source', 'unverified'])
async def test_diagnostic_rejects_bad_evidence(tmp_path, failure):
    async with fixture(tmp_path) as env:
        diag = runner(env)
        inputs = await args(env)
        state = await diag.start_or_poll(*inputs)
        await finish_builds(env, state)
        state = await diag.start_or_poll(*inputs)
        for job_id in state['test_job_ids']:
            await env.test_receipt(job_id, fail=failure == 'failed', assessment='rejected' if failure == 'unverified' else 'validated')
            if failure in {'skipped', 'unknown', 'empty', 'wrong_source'}:
                def mutate(tx):
                    job = tx.get('node_job', job_id)
                    result = tx.get('node_result', job['result_id'])
                    if failure == 'wrong_source':
                        job['source_manifest']['source_commit'] = '0' * 40
                        tx.put('node_job', job_id, job, job['revision'])
                    else:
                        report = result['verified_checks'][0]['normalized_report']
                        if failure == 'empty':
                            report['cases'] = []
                        else:
                            report['cases'][0]['status'] = failure
                        tx.put('node_result', result['id'], result, result['revision'])
                    return {}
                await env.store.command('diagnostic.fixture', job_id + failure, {}, mutate)
        state = await diag.start_or_poll(*inputs)
        assert state['state'] in {'failed', 'blocked'}, state
        assert state['blockers']
        assert not await env.store.list('check')


async def test_wrong_frozen_tree_and_unknown_case_never_enqueue(tmp_path):
    async with fixture(tmp_path) as env:
        diag = runner(env)
        run, batch, source, affected = await args(env)
        wrong = await diag.start_or_poll(run, batch, {**source, 'tree_oid': '0' * 40}, affected)
        assert wrong['state'] == 'blocked'
        assert not await env.store.list('node_job')
        unknown = await diag.start_or_poll(run, {'id': 'batch-2'}, source,
            [{**affected[0], 'framework_case_ids': ['missing::case']}])
        assert unknown['state'] == 'blocked'
        assert not await env.store.list('node_job')


async def test_unaffected_failure_in_same_suite_is_not_hidden(tmp_path):
    async with fixture(tmp_path) as env:
        diag = runner(env)
        run, batch, source, affected = await args(env)
        affected = [{**affected[0], 'phase': 'unit', 'framework_case_ids': ['target-0::unit::denied']}]
        inputs = (run, batch, source, affected)
        state = await diag.start_or_poll(*inputs)
        await finish_builds(env, state)
        state = await diag.start_or_poll(*inputs)
        job = await env.store.read('node_job', state['test_job_ids'][0])
        assert job['matrix_entries'][0]['framework_case_ids'] == ['target-0::unit::normal', 'target-0::unit::denied']
        # The first (unaffected) case fails; the affected denied case passes.
        await env.test_receipt(job['id'], fail=True)
        state = await diag.start_or_poll(*inputs)
        assert state['state'] == 'failed'


async def test_source_archive_ignores_dirty_worktree_and_restart_keeps_jobs(tmp_path):
    import tarfile
    async with fixture(tmp_path) as env:
        diag = runner(env)
        inputs = await args(env)
        (env.source / 'feature.txt').write_text('uncommitted source must not execute')
        state = await diag.start_or_poll(*inputs)
        artifact = await env.store.read('node_artifact', state['source_manifest']['source_bundle_artifact_version_id'])
        with tarfile.open(env.nodes.artifacts.object_path(artifact['digest'])) as archive:
            assert archive.extractfile('feature.txt').read() == b'frozen implementation\n'
        restarted = runner(env)
        assert (await restarted.start_or_poll(*inputs))['build_job_ids'] == state['build_job_ids']
        altered = await restarted.start_or_poll(inputs[0], inputs[1], {**inputs[2], 'tree_oid': '0' * 40}, inputs[3])
        assert altered['state'] == 'blocked'
        assert len(await env.store.list('node_job')) == len(state['build_job_ids'])


async def test_verified_builtin_suite_maps_ast_cases_without_downstream_plans(tmp_path):
    from test_starter_execution_recipes import prepare_starter
    async with fixture(tmp_path) as env:
        await prepare_starter(env, change=lambda repo: (repo / 'tests/api.spec.mjs').write_text(
            "import {test,expect} from './support/fixtures.mjs'; test.describe('suite > literal',()=>test('case',()=>expect(1).toBe(1)));"))
        diag = runner(env)
        assert hasattr(diag, 'prepare_builtin_suite'), 'verified existing-suite recipe helper missing'
        run, batch, source, _ = await args(env)
        prepared = await diag.prepare_builtin_suite(run, source)
        api = next(t for t in prepared['execution_spec']['targets'] if t['target_config_id'] == 'target-0')
        assert api['integration']['expected_case_ids'] == ['api.spec.mjs::suite > literal::case::']
        affected = next(c for c in prepared['cases'] if c['target_config_id'] == 'target-0')
        assert affected['case_id'].startswith('case_')
        state = await diag.start_or_poll(run, batch, source, [affected], execution_spec=prepared['execution_spec'])
        assert state['state'] == 'waiting', state
        assert state['build_job_ids']


async def test_builtin_rejects_altered_support_configuration(tmp_path):
    from test_starter_execution_recipes import prepare_starter
    async with fixture(tmp_path) as env:
        await prepare_starter(env, change=lambda repo: (repo / 'tests/playwright.api.config.mjs').write_text('export default {grep: /only-one/};'))
        diag = runner(env)
        assert hasattr(diag, 'prepare_builtin_suite'), 'verified existing-suite recipe helper missing'
        run, _, source, _ = await args(env)
        with pytest.raises(ValueError, match='support'):
            await diag.prepare_builtin_suite(run, source)


async def test_existing_native_unit_suite_is_included_with_real_junit_case_ids(tmp_path):
    import asyncio
    import os
    import subprocess

    from test_starter_execution_recipes import prepare_starter

    from agentflow.testing.reports import parse_junit

    async with fixture(tmp_path) as env:
        def old_units(repo):
            (repo / 'tests/unit.test.mjs').write_text("import './unit/contract.mjs';")
            (repo / 'tests/unit').mkdir()
            (repo / 'tests/unit/contract.mjs').write_text(
                "import {describe,it} from 'node:test';import assert from 'node:assert/strict';"
                "describe('existing',()=>{it('contract shape',()=>assert.deepEqual({name:'a'},{name:'a'}));});")
        await prepare_starter(env, change=old_units)
        diag = runner(env)
        run, batch, source, _ = await args(env)
        prepared = await diag.prepare_builtin_suite(run, source)
        target = prepared['execution_spec']['targets'][0]
        assert target['unit']['expected_case_ids'] == ['test::contract shape']
        unit = next(c for c in prepared['cases'] if c['phase'] == 'unit')
        assert unit['path'] == 'tests/unit/contract.mjs'
        manifest = next(m for m in prepared['manifests'] if m['file'] == unit['path'])
        assert manifest['cases'][0]['assertions'][0]['matcher'] == 'node:assert.deepStrictEqual'
        report = tmp_path / 'actual-unit.xml'
        process = await asyncio.to_thread(subprocess.run, [os.environ.get('AGENTFLOW_NODE_PATH', 'node'),
            '--test', '--test-reporter=junit', f'--test-reporter-destination={report}',
            str(env.source / 'tests/unit.test.mjs')], capture_output=True, timeout=20)
        assert process.returncode == 0, process.stderr
        parsed = parse_junit(report, set(target['unit']['expected_case_ids']))
        assert parsed.quality_result == 'passed' and not parsed.missing_case_ids
        state = await diag.start_or_poll(run, batch, source, prepared['cases'], execution_spec=prepared['execution_spec'])
        await finish_builds(env, state)
        state = await diag.start_or_poll(run, batch, source, prepared['cases'], execution_spec=prepared['execution_spec'])
        jobs = [await env.store.read('node_job', j) for j in state['test_job_ids']]
        assert any(j['recipe']['test_kind'] == 'unit' for j in jobs)
        for job in jobs:
            await env.test_receipt(job['id'])
        done = await diag.start_or_poll(run, batch, source, prepared['cases'], execution_spec=prepared['execution_spec'])
        assert done['state'] == 'passed'
        assert not await env.store.list('check') and not await env.store.list('candidate')


async def test_starter_unit_placeholder_does_not_become_a_required_regression(tmp_path):
    from test_starter_execution_recipes import prepare_starter
    async with fixture(tmp_path) as env:
        await prepare_starter(env)
        run, _, source, _ = await args(env)
        prepared = await runner(env).prepare_builtin_suite(run, source)
        assert all(c['phase'] == 'integration' for c in prepared['cases'])


async def test_build_failure_drains_other_queued_diagnostic_jobs(tmp_path):
    async with fixture(tmp_path) as env:
        diag = runner(env)
        run, batch, source, affected = await args(env)
        affected.append({'case_id': 'ast-web', 'target_config_id': 'target-1', 'phase': 'integration',
                         'framework_case_ids': ['target-1::integration::persists']})
        inputs = (run, batch, source, affected)
        state = await diag.start_or_poll(*inputs)
        first, second = state['build_job_ids']
        await env.build_receipt(first, state='failed', quality='failed')
        state = await diag.start_or_poll(*inputs)
        assert (await env.store.read('node_job', second))['state'] == 'cancelled'
        assert state['state'] == 'waiting'
        assert (await diag.start_or_poll(*inputs))['state'] == 'failed'


@pytest.mark.parametrize('phase', ['source', 'platform'])
async def test_overlapping_poll_and_restarted_instance_keep_one_frozen_manifest(tmp_path, monkeypatch, phase):
    import asyncio

    async with fixture(tmp_path) as env:
        first, restarted = runner(env), runner(env)
        inputs = await args(env)
        if phase == 'platform':
            prepared = await first.start_or_poll(*inputs)
            await finish_builds(env, prepared)
        reached, resume = asyncio.Event(), asyncio.Event()
        original_save = first._save
        proposed = {}
        async def paused_save(record, **changes):
            if ('source_manifest' if phase == 'source' else 'platform_manifest') in changes:
                proposed.update(changes)
                reached.set()
                await resume.wait()
            return await original_save(record, **changes)
        monkeypatch.setattr(first, '_save', paused_save)
        overlapping = asyncio.create_task(first.start_or_poll(*inputs))
        try:
            await asyncio.wait_for(reached.wait(), timeout=15)
            newer = await restarted.start_or_poll(*inputs)
        finally:
            resume.set()
        older = await asyncio.wait_for(overlapping, timeout=15)
        current = (await env.store.list('review_diagnostic'))[0]
        assert current['state'] == newer['state'] == older['state'] == 'waiting', current
        field = 'source_manifest' if phase == 'source' else 'platform_manifest'
        assert proposed[field] == current[field]
        for job_id in current['build_job_ids']:
            assert (await env.store.read('node_job', job_id))['source_manifest'] == current['source_manifest']
        if phase == 'platform':
            for job_id in current['test_job_ids']:
                assert (await env.store.read('node_job', job_id))['platform_artifact_manifest'] == current['platform_manifest']
                await env.test_receipt(job_id)
            completed = await restarted.start_or_poll(*inputs)
            assert completed['state'] == 'passed', completed
            assert len(await env.store.list('node_job')) == 2


async def test_frozen_manifest_cannot_be_replaced_after_jobs_are_queued(tmp_path):
    from agentflow.common import DomainError

    async with fixture(tmp_path) as env:
        diag = runner(env)
        state = await diag.start_or_poll(*(await args(env)))
        changed = {**state['source_manifest'], 'manifest_id': 'replacement'}
        with pytest.raises(DomainError, match='frozen'):
            await diag._save(state, source_manifest=changed)
        persisted = await env.store.read('review_diagnostic', state['id'])
        assert persisted['source_manifest'] == state['source_manifest']
