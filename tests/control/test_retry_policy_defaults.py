"""Large retry defaults preserve explicit compatibility and existing execution records."""
import tomllib

from test_execution_settings import execution_config as execution_config
from test_execution_settings import request

from agentflow.configuration import _template
from agentflow.settings import Settings

RETRY_KEYS = ('auto_failure_retry_limit', 'auto_timeout_retry_limit', 'auto_failure_run_limit',
              'auto_review_repair_limit', 'auto_test_repair_limit')


def test_every_retry_default_and_generated_configuration_is_one_hundred():
    settings = Settings()
    assert {key: getattr(settings, key) for key in RETRY_KEYS} == dict.fromkeys(RETRY_KEYS, 100)
    generated = tomllib.loads(_template())['app']
    assert {key: generated[key] for key in RETRY_KEYS} == dict.fromkeys(RETRY_KEYS, 100)


async def test_retry_metadata_and_saved_one_hundred_preserve_history_and_authorization(execution_config):
    env = execution_config
    old_values = '\n'.join(f'{key} = 2' for key in RETRY_KEYS)
    env.path.write_text(env.path.read_text().replace('[app]', '[app]\n' + old_values))
    configuration = env.configuration.reload()
    protected = ('run', 'work_item', 'attempt', 'failure_analysis', 'timeout_recovery', 'review_repair',
                 'product_test_repair', 'coding_work_budget', 'coding_step_usage', 'budget_account', 'authorization')
    for kind in protected:
        await env.store.command('fixture', kind, {}, lambda tx, kind=kind: tx.put(kind, 'existing', {'count': 2, 'used': 123}))
    before = {kind: await env.store.list(kind) for kind in protected}
    document = tomllib.loads(env.path.read_text())
    fields = {row['key']: row for row in configuration.execution_settings()['fields']}
    for key in RETRY_KEYS:
        field = fields['app.' + key]
        assert field['default_value'] == 100
        assert field['maximum'] >= 100
        assert field['zero_meaning']
    values = {'app.' + key: 100 for key in RETRY_KEYS}
    result = await configuration.update_execution_settings(request(configuration, **values), 'raise-retries', env.store)
    assert {key: result['saved_values'][key] for key in values} == values
    assert {key: result['loaded_values'][key] for key in values} == dict.fromkeys(values, 2)
    assert result['restart_required'] and not result['current_runs_changed']
    saved = configuration.reload()
    assert {key: getattr(saved.settings, key) for key in RETRY_KEYS} == dict.fromkeys(RETRY_KEYS, 100)
    after_document = tomllib.loads(env.path.read_text())
    assert after_document['models'] == document['models'] and after_document['product'] == document['product']
    assert {key: value for key, value in after_document['app'].items() if key not in RETRY_KEYS} == {
        key: value for key, value in document['app'].items() if key not in RETRY_KEYS}
    assert {kind: await env.store.list(kind) for kind in protected} == before
    assert not await env.store.list('run_recovery') and not await env.store.list('model_invocation')


def test_zero_still_disables_and_explicit_continuous_review_and_test_remain_valid():
    disabled = Settings(**dict.fromkeys(RETRY_KEYS, 0))
    assert all(getattr(disabled, key) == 0 for key in RETRY_KEYS)
    continuous = Settings(auto_review_repair_limit=-1, auto_test_repair_limit=-1)
    assert continuous.auto_review_repair_limit == continuous.auto_test_repair_limit == -1
