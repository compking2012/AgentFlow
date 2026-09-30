"""Verified starter recipes freeze accepted cases without changing source code."""
import json
import shutil
import tarfile
from pathlib import Path
from uuid import uuid4
from xml.sax.saxutils import quoteattr

import pytest
from test_execution_pipeline import fixture

import agentflow
from agentflow.common import DomainError
from agentflow.control.scheduler import Scheduler
from agentflow.testing.reports import parse_junit

STARTER = Path(agentflow.__file__).resolve().parent / 'resources/web_api_starter'
SUPPORT_FILES = ('package.json', 'package-lock.json', 'tooling/build.mjs',
    'tests/support/config.mjs', 'tests/support/fixtures.mjs', 'tests/support/global-setup.mjs',
    'tests/playwright.api.config.mjs', 'tests/playwright.web.config.mjs')
CASES = {('target-0', 'unit'): [f'API unit::case-{i:02d}' for i in range(44)],
         ('target-0', 'integration'): [f'API integration::case-{i:02d}' for i in range(20)],
         ('target-1', 'integration'): [f'Web integration::case-{i:02d}' for i in range(15)]}


def accepted_cases(plans):
    for phase in ('unit', 'integration'):
        plans[phase] = [{'case_id': f'{target}-{phase}', 'requirement_id': f'requirement-{target}',
            'target_config_id': target, 'phase': phase, 'framework_case_ids': list(cases)}
            for (target, planned_phase), cases in CASES.items() if planned_phase == phase]


def explicit_recipes(spec):
    for target in spec['targets']:
        for phase in ('unit', 'integration'):
            expected = CASES.get((target['target_config_id'], phase))
            if expected:
                target[phase]['expected_case_ids'] = list(expected)
            else:
                target.pop(phase, None)


async def prepare_starter(env, *, change=None, manifest='absent', stack='node_web_api'):
    shutil.copytree(STARTER, env.source, dirs_exist_ok=True)
    manifest_path = env.source / 'agentflow.project.json'
    if manifest == 'absent':
        manifest_path.unlink()
    elif manifest == 'invalid':
        manifest_path.write_text('{invalid committed execution manifest')
    elif manifest == 'missing_phase':
        value = json.loads(manifest_path.read_text())
        value['targets'][0].pop('unit')
        manifest_path.write_text(json.dumps(value))
    if change:
        change(env.source)
    original = env.snapshot
    env.snapshot = await env.repository.freeze_workspace(env.source, original['base_oid'], 'isolated starter execution source')
    async def resolve(_run, _work):
        return env.source, env.snapshot['commit_oid']
    env.pipeline.source_resolver = resolve
    def update(tx):
        plan = tx.get('plan', 'plan')
        tx.put('plan', 'plan', {**plan, 'product_contract': {'stack': stack}}, plan['revision'])
        snapshot = tx.get('code_snapshot', 'code-snapshot')
        tx.put('code_snapshot', snapshot['id'], {**snapshot, 'commit_oid': env.snapshot['commit_oid'],
            'tree_oid': env.snapshot['tree_oid'], 'base_oid': env.snapshot['base_oid']}, snapshot['revision'])
        for facet in ('api', 'web'):
            tx.put('review', facet + '-review', {'run_id': 'run', 'work_item_id': 'code-work', 'generation': 1,
                'reviewed_commit': env.snapshot['commit_oid'], 'quality_result': 'passed', 'stale': False})
        return {}
    await env.store.command('fixture.starter-source', 'prepare', {}, update)


def source_state(env):
    return {'head': env.repository._run(env.source, ['rev-parse', 'HEAD']),
        'index': (env.source / '.git/index').read_bytes(),
        'status': env.repository._run(env.source, ['status', '--porcelain']),
        'support': {name: (env.source / name).read_bytes() for name in SUPPORT_FILES},
        'commit': env.repository._run(env.source, ['cat-file', 'commit', env.snapshot['commit_oid']])}


async def test_exact_starter_freezes_only_the_three_accepted_target_phase_combinations(tmp_path):
    async with fixture(tmp_path, plan_change=accepted_cases, spec_change=explicit_recipes) as env:
        await prepare_starter(env)
        before_source = source_state(env)
        before_code = await env.store.read('work_item', 'code-work')
        before_reviews = await env.store.list('review')
        before_snapshot = await env.store.read('code_snapshot', 'code-snapshot')
        claim = await env.claim()
        await env.pipeline.begin(claim)
        candidate = await env.candidate()
        mappings = {(row['target_config_id'], row['phase']): row['framework_case_ids']
                    for row in candidate['matrix_mappings'].values()}
        assert mappings == CASES
        assert len(candidate['matrix_plan']['entries']) == 3 and len(candidate['build_job_ids']) == 2
        evidence = candidate['execution_spec_source']
        assert evidence['kind'] == 'verified_builtin_node_web_api'
        assert {row['path'] for row in evidence['support_files']} == set(SUPPORT_FILES)
        assert all(row['accepted'] for row in evidence['accepted_plan_artifacts'])
        recipes = {row['target_config_id']: row for row in candidate['recipes']['targets']}
        assert recipes['target-0']['build']['output_paths'] == {'product': 'build/product', 'test': 'build/tests'}
        assert recipes['target-0']['unit']['unit_project'] == 'build/tests/unit.test.mjs'
        assert recipes['target-1']['unit'] is None
        assert recipes['target-0']['integration']['framework_config'] == 'build/tests/playwright.api.config.mjs'
        assert recipes['target-1']['integration']['framework_config'] == 'build/tests/playwright.web.config.mjs'
        source_blob = await env.store.read('node_artifact', candidate['source_manifest']['source_bundle_artifact_version_id'])
        with tarfile.open(env.nodes.artifacts.object_path(source_blob['digest'])) as archive:
            assert 'agentflow.project.json' not in archive.getnames()
            assert archive.extractfile('package.json').read() == (STARTER / 'package.json').read_bytes()
        plan_blob = await env.store.read('node_artifact', candidate['source_manifest']['build_plan_artifact_version_id'])
        assert json.loads(env.nodes.artifacts.object_path(plan_blob['digest']).read_bytes()) == candidate['recipes']
        await env.finish_builds()
        candidate = await env.candidate()
        assert len(candidate['phase_jobs']['unit']) == 1 and 'integration' not in candidate['phase_jobs']
        unit_job = await env.store.read('node_job', candidate['phase_jobs']['unit'][0])
        assert unit_job['target_config']['target_config_id'] == 'target-0'
        assert unit_job['recipe']['expected_case_ids'] == CASES['target-0', 'unit']
        assert (await env.claim())['attempt'] is None
        await env.test_receipt(unit_job['id'])
        await env.pipeline.reconcile()
        unit = await env.store.read('work_item', 'unit-work')
        assert unit['status'] == 'completed' and unit['quality_result'] == 'passed'
        await env.pipeline.begin(await env.claim())
        candidate = await env.candidate()
        assert len(candidate['phase_jobs']['integration']) == 2
        for job_id in candidate['phase_jobs']['integration']:
            job = await env.store.read('node_job', job_id)
            assert job['recipe']['expected_case_ids'] == CASES[job['target_config']['target_config_id'], 'integration']
        assert len(await env.store.list('node_job')) == 5
        assert source_state(env) == before_source and not (env.source / 'agentflow.project.json').exists()
        assert await env.store.read('work_item', 'code-work') == before_code
        assert await env.store.read('code_snapshot', 'code-snapshot') == before_snapshot
        assert await env.store.list('review') == before_reviews


@pytest.mark.parametrize('name', SUPPORT_FILES)
async def test_committed_support_file_mismatch_cannot_use_the_builtin_recipes(tmp_path, name):
    def change(source):
        with (source / name).open('ab') as stream:
            stream.write(b'\n// source differs from the pinned starter\n')
    async with fixture(tmp_path, plan_change=accepted_cases, spec_change=explicit_recipes) as env:
        await prepare_starter(env, change=change)
        with pytest.raises(DomainError) as error:
            await env.pipeline.begin(await env.claim())
        assert error.value.code == 'execution_plan_missing'
        assert await env.store.list('candidate') == [] and await env.store.list('node_job') == []
        assert not (env.source / 'agentflow.project.json').exists()


async def patch_record(env, kind, identity, **fields):
    def update(tx):
        row = tx.get(kind, identity)
        return tx.put(kind, identity, {**row, **fields}, row['revision'])
    return await env.store.command('fixture.starter-change', str(uuid4()), {}, update)


async def commit_link(env, name):
    # Construct a committed link through a private temporary index. The source
    # checkout/index remain untouched, so the guard must inspect the frozen tree.
    temporary_index = env.settings.data_dir / 'fixture-link-index'
    environment = {'GIT_INDEX_FILE': str(temporary_index)}
    env.repository._run(env.source, ['read-tree', env.snapshot['commit_oid']], extra_env=environment)
    blob = env.repository._run(env.source, ['hash-object', '-w', '--stdin'], data=b'protected-target').decode().strip()
    env.repository._run(env.source, ['update-index', '--add', '--cacheinfo', '120000', blob, name], extra_env=environment)
    tree = env.repository._run(env.source, ['write-tree'], extra_env=environment).decode().strip()
    commit = env.repository._run(env.source, ['commit-tree', tree, '-p', env.snapshot['commit_oid']],
        data=b'Isolated linked-entry fixture\n', extra_env={'GIT_AUTHOR_NAME': 'Fixture',
            'GIT_AUTHOR_EMAIL': 'fixture@localhost', 'GIT_COMMITTER_NAME': 'Fixture',
            'GIT_COMMITTER_EMAIL': 'fixture@localhost'}).decode().strip()
    env.snapshot = {**env.snapshot, 'commit_oid': commit, 'tree_oid': tree}
    await patch_record(env, 'code_snapshot', 'code-snapshot', commit_oid=commit, tree_oid=tree)


@pytest.mark.parametrize('condition', ['missing_support', 'linked_support', 'linked_manifest'])
async def test_missing_or_nonregular_committed_entries_do_not_trigger_a_recipe_guess(tmp_path, condition):
    async with fixture(tmp_path, plan_change=accepted_cases, spec_change=explicit_recipes) as env:
        def missing(source):
            (source / 'tests/support/config.mjs').unlink()
        await prepare_starter(env, change=missing if condition == 'missing_support' else None,
                              manifest='valid' if condition == 'linked_manifest' else 'absent')
        if condition != 'missing_support':
            await commit_link(env, 'agentflow.project.json' if condition == 'linked_manifest' else 'tests/support/config.mjs')
        with pytest.raises(DomainError) as error:
            await env.pipeline.begin(await env.claim())
        assert error.value.code == 'execution_plan_missing'
        assert await env.store.list('candidate') == [] and await env.store.list('node_job') == []


async def test_native_targets_require_an_explicit_manifest_even_with_all_starter_files(tmp_path):
    async with fixture(tmp_path, app_targets=('linux_native',)) as env:
        await prepare_starter(env)
        with pytest.raises(DomainError) as error:
            await env.pipeline.begin(await env.claim())
        assert error.value.code == 'execution_plan_missing'
        assert await env.store.list('candidate') == [] and await env.store.list('node_job') == []


@pytest.mark.parametrize('manifest,expected', [('invalid', 'execution_plan_missing'), ('missing_phase', 'missing_test_phase')])
async def test_existing_manifest_errors_are_not_replaced_by_the_starter_fallback(tmp_path, manifest, expected):
    async with fixture(tmp_path, plan_change=accepted_cases, spec_change=explicit_recipes) as env:
        await prepare_starter(env, manifest=manifest)
        original = (env.source / 'agentflow.project.json').read_bytes()
        with pytest.raises(DomainError) as error:
            await env.pipeline.begin(await env.claim())
        assert error.value.code == expected
        assert (env.source / 'agentflow.project.json').read_bytes() == original
        assert await env.store.list('candidate') == [] and await env.store.list('node_job') == []


async def test_a_valid_committed_manifest_takes_priority_over_template_recipes(tmp_path):
    def customized(source):
        (source / 'tooling/build.mjs').write_text('// Custom support is governed by the explicit manifest.\n')
    async with fixture(tmp_path, plan_change=accepted_cases, spec_change=explicit_recipes) as env:
        await prepare_starter(env, manifest='valid', change=customized)
        original = (env.source / 'agentflow.project.json').read_bytes()
        await env.pipeline.begin(await env.claim())
        candidate = await env.candidate()
        assert candidate['execution_spec_source']['kind'] == 'committed_manifest'
        assert all(target['build']['output_paths']['product'] == 'build/app' for target in candidate['recipes']['targets'])
        assert (env.source / 'agentflow.project.json').read_bytes() == original
        assert len(candidate['matrix_mappings']) == 3


@pytest.mark.parametrize('condition', ['missing_cases', 'missing_web_target', 'stale_plan',
                                    'unaccepted_producer', 'generation_mismatch'])
async def test_only_current_accepted_independent_plans_authorize_generated_recipes(tmp_path, condition):
    async with fixture(tmp_path, plan_change=accepted_cases, spec_change=explicit_recipes) as env:
        await prepare_starter(env)
        claim = await env.claim()
        if condition in {'missing_cases', 'missing_web_target'}:
            phase = 'unit' if condition == 'missing_cases' else 'integration'
            remaining = [] if condition == 'missing_cases' else [{
                'case_id': 'api-integration', 'requirement_id': 'requirement-api',
                'target_config_id': 'target-0', 'phase': 'integration',
                'framework_case_ids': CASES['target-0', 'integration']}]
            blob = await env.workflow.artifacts.put_bytes(json.dumps({'result': {'test_cases': remaining}}).encode())
            await patch_record(env, 'artifact', phase + '-plan-artifact', digest=blob['id'])
        elif condition == 'stale_plan':
            await patch_record(env, 'artifact', 'unit-plan-artifact', stale=True)
        elif condition == 'unaccepted_producer':
            await patch_record(env, 'work_item', 'unit-plan-work', status='pending')
        else:
            await patch_record(env, 'artifact', 'unit-plan-artifact', generation=2)
        with pytest.raises(DomainError) as error:
            await env.pipeline.begin(claim)
        assert error.value.code == 'test_plan_missing'
        assert await env.store.list('candidate') == [] and await env.store.list('node_job') == []


async def test_a_plan_revision_race_cannot_commit_a_candidate_from_stale_cases(tmp_path, monkeypatch):
    async with fixture(tmp_path, plan_change=accepted_cases, spec_change=explicit_recipes) as env:
        await prepare_starter(env)
        original = env.store.command
        changed = False
        async def revise_before_freeze(scope, key, payload, handler):
            nonlocal changed
            if scope == 'candidate.freeze' and not changed:
                changed = True
                await patch_record(env, 'plan', 'plan', note='Plan revised after recipe derivation')
            return await original(scope, key, payload, handler)
        monkeypatch.setattr(env.store, 'command', revise_before_freeze)
        with pytest.raises(DomainError) as error:
            await env.pipeline.begin(await env.claim())
        assert changed and error.value.code == 'stale_candidate'
        assert await env.store.list('candidate') == [] and await env.store.list('node_job') == []


async def test_node_starter_child_prompt_keeps_its_test_scope_without_requiring_a_manifest(tmp_path):
    async with fixture(tmp_path, plan_change=accepted_cases, spec_change=explicit_recipes) as env:
        await prepare_starter(env)
        run = await env.store.read('run', 'run')
        child = {**(await env.store.read('work_item', 'code-work')), 'kind': 'stage_child',
                 'write_paths': ['tests/api.spec.mjs']}
        prompt = await Scheduler(env.workflow, env.store, None, None, env.settings)._prompt(run, child, env.snapshot['commit_oid'])
        assert 'controller freezes execution recipes' in prompt
        assert 'Also commit agentflow.project.json' not in prompt
        assert 'tests/api.spec.mjs' in prompt


async def test_real_unit_allocation_freezes_api_36_web_8_and_keeps_extra_report_failures(tmp_path):
    api_unit = CASES['target-0', 'unit'][:36]
    web_unit = [f'Web unit::case-{i:02d}' for i in range(8)]
    expected = {**CASES, ('target-0', 'unit'): api_unit, ('target-1', 'unit'): web_unit}
    all_unit = [*api_unit, *web_unit]
    assert len(all_unit) == 44 and len(set(all_unit)) == 44
    def plans_for_actual_allocation(plans):
        for phase in ('unit', 'integration'):
            plans[phase] = [{'case_id': f'{target}-{phase}', 'requirement_id': f'requirement-{target}',
                'target_config_id': target, 'phase': phase, 'framework_case_ids': list(cases)}
                for (target, planned_phase), cases in expected.items() if planned_phase == phase]
    async with fixture(tmp_path, plan_change=plans_for_actual_allocation) as env:
        await prepare_starter(env)
        await env.pipeline.begin(await env.claim())
        candidate = await env.candidate()
        assert {(row['target_config_id'], row['phase']): row['framework_case_ids']
                for row in candidate['matrix_mappings'].values()} == expected
        assert len(candidate['matrix_plan']['entries']) == 4
        await env.finish_builds()
        candidate = await env.candidate()
        jobs = [await env.store.read('node_job', identity) for identity in candidate['phase_jobs']['unit']]
        assert {job['target_config']['target_config_id']: job['recipe']['expected_case_ids'] for job in jobs} == {
            'target-0': api_unit, 'target-1': web_unit}
        assert 'integration' not in candidate['phase_jobs']
        def report(path, failed_case=None):
            cases = ''.join(f'<testcase name={quoteattr(case)} fullname={quoteattr(case)}>'
                + ('<failure>Observed failure outside this target requirement subset</failure>' if case == failed_case else '')
                + '</testcase>' for case in all_unit)
            path.write_text('<testsuite>' + cases + '</testsuite>')
        passing = tmp_path / 'all-44-unit-cases.xml'
        report(passing)
        for target, required in [('target-0', set(api_unit)), ('target-1', set(web_unit))]:
            parsed = parse_junit(passing, required)
            assert parsed.execution_status == 'completed' and parsed.quality_result == 'passed'
            assert parsed.missing_case_ids == [] and {case.case_id for case in parsed.cases} == set(all_unit)
            extra_case = next(case for case in all_unit if case not in required)
            failing = tmp_path / f'extra-failure-{target}.xml'
            report(failing, extra_case)
            parsed = parse_junit(failing, required)
            assert parsed.execution_status == 'completed' and parsed.quality_result == 'failed'
            assert parsed.missing_case_ids == []
            assert next(case for case in parsed.cases if case.case_id == extra_case).status == 'failed'
