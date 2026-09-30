"""Known browser-test lifecycle failures stay in test-only repair authority."""
import asyncio

import pytest
from test_execution_pipeline import fixture
from test_product_test_runtime_repair import failed_candidate, patch

from agentflow.control.product_repair import ProductTestRepair

ROUTE_ERROR = ('Error: route.continue: Route is already handled!\n'
               '    at workspace/build/tests/web.spec.mjs:349:25')


async def route_failure(env):
    payload = await failed_candidate(env, phase='integration', failure_message=ROUTE_ERROR)
    await patch(env, 'run', 'run', execution_state='running')
    return payload


async def test_route_handler_failure_automatically_repairs_only_tests_and_preserves_gates(tmp_path):
    async with fixture(tmp_path, app_targets=('web',)) as env:
        await route_failure(env)
        await patch(env, 'plan', 'plan', approval_steps=['integration_test_implementation', 'code_review'])
        before = {kind: await env.store.list(kind) for kind in ('budget_account', 'model_invocation',
                                                              'node_result', 'node_artifact', 'plan')}
        service = ProductTestRepair(env.store, env.workflow, nodes=env.nodes)
        product = {'id': 'product', 'run_id': 'run'}
        result = await service.attempt(product)
        assert result['scheduled']
        work = await env.store.read('work_item', result['repair_id'])
        assert work['step'] == 'integration_test_implementation'
        assert work['write_paths'] == ['tests/web.spec.mjs']
        assert work['approval_required']
        records = await env.store.list('product_test_runtime_repair')
        assert len(records) == 1 and records[0]['actor'] == 'system'
        review = await env.store.read('work_item', records[0]['review_work_item_id'])
        assert review['write_paths'] == [] and review['approval_required']
        assert review['dependencies'] == [work['id']]
        assert (await env.store.read('run', 'run'))['execution_state'] == 'running'
        assert not await env.store.list('product_test_repair')
        assert {kind: await env.store.list(kind) for kind in before} == before
        await service.attempt(product)
        assert len(await env.store.list('product_test_runtime_repair')) == 1


@pytest.mark.parametrize('stop', ['scope', 'budget', 'corrupt_report', 'unknown_execution'])
async def test_automatic_fixture_routing_cannot_bypass_authority_or_fall_back_to_product_edits(tmp_path, stop):
    async with fixture(tmp_path, app_targets=('web',)) as env:
        payload = await route_failure(env)
        if stop == 'scope':
            await patch(env, 'plan', 'plan', authorized_rework_steps=['implementation'])
        elif stop == 'budget':
            env.workflow.settings = env.settings.model_copy(update={'auto_test_repair_limit': 0})
        elif stop == 'unknown_execution':
            await patch(env, 'attempt', 'code-snapshot', status='execution_unknown')
        else:
            job = await env.store.read('node_job', payload['failed_job_id'])
            result = await env.store.read('node_result', job['result_id'])
            artifact = await env.store.read('node_artifact', result['verified_checks'][0]['raw_report_artifact_version_id'])
            path = env.nodes.artifacts.object_path(artifact['digest'])
            path.chmod(0o600)
            path.write_bytes(b'corrupt fixture')
        result = await ProductTestRepair(env.store, env.workflow, nodes=env.nodes).attempt({'id': 'product', 'run_id': 'run'})
        assert not result['scheduled']
        assert not await env.store.list('product_test_repair')
        assert not await env.store.list('product_test_runtime_repair')


@pytest.mark.parametrize('message', ['expected amount 1 to equal 2', 'TypeError: fetch failed ECONNRESET'])
async def test_business_and_ambiguous_transport_failures_are_not_silently_reclassified_as_test_edits(tmp_path, message):
    async with fixture(tmp_path, app_targets=('web',)) as env:
        await failed_candidate(env, phase='integration', failure_message=message)
        await patch(env, 'run', 'run', execution_state='running')
        result = await ProductTestRepair(env.store, env.workflow, nodes=env.nodes).attempt({'id': 'product', 'run_id': 'run'})
        assert result['scheduled'] and not await env.store.list('product_test_runtime_repair')
        work = await env.store.read('work_item', result['repair_id'])
        assert work['write_paths'] == ['src', 'public']


async def test_concurrent_classification_creates_one_fixture_repair(tmp_path):
    async with fixture(tmp_path, app_targets=('web',)) as env:
        await route_failure(env)
        service = ProductTestRepair(env.store, env.workflow, nodes=env.nodes)
        results = await asyncio.gather(*(service.attempt({'id': 'product', 'run_id': 'run'}) for _ in range(2)))
        assert any(result['scheduled'] for result in results)
        assert len(await env.store.list('product_test_runtime_repair')) == 1
        assert not await env.store.list('product_test_repair')


async def test_test_only_authority_suffices_without_granting_product_writes(tmp_path):
    async with fixture(tmp_path, app_targets=('web',)) as env:
        await route_failure(env)
        await patch(env, 'plan', 'plan', authorized_rework_steps=['integration_test_implementation'])
        result = await ProductTestRepair(env.store, env.workflow, nodes=env.nodes).attempt({'id': 'product', 'run_id': 'run'})
        assert result['scheduled']
        work = await env.store.read('work_item', result['repair_id'])
        assert work['write_paths'] == ['tests/web.spec.mjs']


async def test_runtime_repairs_share_the_configured_test_repair_limit_without_resetting_usage(tmp_path):
    async with fixture(tmp_path, app_targets=('web',)) as env:
        await route_failure(env)
        await patch(env, 'product_test_runtime_repair', 'prior-runtime-repair',
                    product_id='product', run_id='run', actor='system', candidate_id='prior')
        env.workflow.settings = env.settings.model_copy(update={'auto_test_repair_limit': 1})
        before = await env.store.list('budget_account')
        service = ProductTestRepair(env.store, env.workflow, nodes=env.nodes)
        product = {'id': 'product', 'run_id': 'run'}
        assert not (await service.attempt(product))['scheduled']
        assert len(await env.store.list('product_test_runtime_repair')) == 1
        env.workflow.settings = env.settings.model_copy(update={'auto_test_repair_limit': 2})
        assert (await service.attempt(product))['scheduled']
        assert len(await env.store.list('product_test_runtime_repair')) == 2
        assert await env.store.list('budget_account') == before
