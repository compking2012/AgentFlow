"""The public command surface and goal submission recovery contract."""
from argparse import _SubParsersAction
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentflow.cli import build_parser, main
from agentflow.common import DomainError
from agentflow.configuration import load_configuration
from agentflow.control.product_models import product_identity
from agentflow.product_cli import run_product_command


def test_only_five_commands_and_two_run_options_are_public():
    parser = build_parser()
    commands = next(action for action in parser._actions if isinstance(action, _SubParsersAction)).choices
    assert set(commands) == {'start', 'run', 'status', 'launch', 'stop'}
    assert {flag for action in commands['run']._actions for flag in action.option_strings} == {
        '-h', '--help', '--name', '--output'}
    args = parser.parse_args(['run', 'Create a persistent reading list'])
    assert args.name is None and args.output is None
    assert parser.parse_args(['stop']).product_id is None
    assert parser.parse_args(['stop', 'product-id']).product_id == 'product-id'


@pytest.mark.parametrize('arguments', [['--help'], ['--version'], ['run', '--help']])
def test_help_never_reads_or_creates_configuration(monkeypatch, tmp_path, arguments):
    monkeypatch.setenv('HOME', str(tmp_path / 'untouched'))
    monkeypatch.setattr('sys.argv', ['agentflow', *arguments])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    assert not (tmp_path / 'untouched').exists()


@pytest.mark.parametrize('arguments', [
    ['serve'], ['setup-model'], ['backup'], ['--config', '/tmp/config', 'start'],
    ['run', '--goal', 'Make a web app'], ['run', 'Make a web app', '--target', 'api'],
    ['run', 'Make a web app', '--no-wait'], ['run', 'Make a web app', '--json'],
    ['run', 'Make a web app', '--out', '/tmp/product'], ['run', 'Make a web app', '--language', 'en'],
])
def test_removed_or_abbreviated_options_are_errors(arguments):
    with pytest.raises(SystemExit) as result:
        build_parser().parse_args(arguments)
    assert result.value.code == 2


@pytest.fixture
async def cli_environment(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    config = load_configuration(create=True)
    client = SimpleNamespace(data_dir=tmp_path / 'data', origin='http://127.0.0.1:8787',
                             request=AsyncMock(), close=AsyncMock())
    connect = AsyncMock(return_value=client)
    monkeypatch.setattr('agentflow.product_cli.OwnerClient.connect', connect)
    return config, client, connect


async def test_blank_input_rejected_before_connect(cli_environment):
    config, _, connect = cli_environment
    with pytest.raises(DomainError, match='产品目标'):
        await run_product_command(build_parser().parse_args(['run', '  ']), config)
    connect.assert_not_called()


async def test_uncertain_submit_reuses_original_payload_and_key(cli_environment, capsys):
    config, client, _ = cli_environment
    args = build_parser().parse_args(['run', '创建可保存和编辑书目的阅读清单'])
    client.request.side_effect = DomainError('owner_connection_interrupted', 'lost acknowledgement', 503)
    with pytest.raises(DomainError):
        await run_product_command(args, config)
    original = client.request.call_args
    assert len(list((config.config_path.parent / 'cli_submissions').glob('*.json'))) == 1
    config.config_path.write_text(config.config_path.read_text().replace('target = "web"', 'target = "api"')
                                .replace('language = "zh-CN"', 'language = "en"'))
    config = config.reload()
    product = {'id': product_identity(original.kwargs['key']), 'name': 'reading', 'state': 'completed', 'phase': 'delivered',
               'delivery': {'path': '/tmp/release'}}
    client.request.side_effect = [product, product]
    await run_product_command(args, config)
    repeated = client.request.call_args_list[-2]
    assert original == repeated and repeated.args[2]['target'] == 'web'
    assert repeated.args[2]['language'] == 'zh-CN'
    assert not list((config.config_path.parent / 'cli_submissions').glob('*.json'))
    assert 'agentflow launch ' + product['id'] in capsys.readouterr().out


async def test_cli_uses_file_language_for_new_goals_without_a_new_flag(cli_environment):
    config, client, _ = cli_environment
    config.config_path.write_text(config.config_path.read_text().replace('language = "zh-CN"', 'language = "en"'))
    client.request.side_effect = DomainError('owner_connection_interrupted', 'uncertain fixture', 503)
    with pytest.raises(DomainError):
        await run_product_command(build_parser().parse_args(['run', 'Build a persistent reading list']), config.reload())
    assert client.request.call_args.args[2]['language'] == 'en'


async def test_cli_replays_an_old_pending_payload_without_adding_language(cli_environment):
    from agentflow.control.submissions import Submission
    config, client, _ = cli_environment
    goal = 'Build a persistent reading list'
    task_input = {'goal': goal, 'name': None, 'output': None}
    original = {'name': goal[:30], 'goal': goal, 'output_directory': None,
                'target': 'web', 'review_mode': 'auto', 'max_model_requests': 200}
    with Submission(client.data_dir, task_input, original) as intent:
        key = intent.key
    config.config_path.write_text(config.config_path.read_text().replace('language = "zh-CN"', 'language = "en"'))
    client.request.return_value = {'id': product_identity(key), 'state': 'completed'}
    await run_product_command(build_parser().parse_args(['run', goal]), config.reload())
    call = client.request.call_args_list[0]
    assert call.args[2] == original and call.kwargs['key'] == key
    assert 'language' not in call.args[2]


@pytest.mark.parametrize('original_limit,new_default', [(0, 25), (25, 0)])
async def test_cli_pending_count_keeps_original_zero_or_finite_value_after_config_changes(cli_environment, original_limit, new_default):
    config, client, _ = cli_environment
    config.config_path.write_text(config.config_path.read_text().replace('max_model_requests = 200', f'max_model_requests = {original_limit}'))
    args = build_parser().parse_args(['run', 'Create a persistent reading list'])
    client.request.side_effect = DomainError('owner_connection_interrupted', 'Uncertain acknowledgement', 503)
    with pytest.raises(DomainError):
        await run_product_command(args, config.reload())
    original = client.request.call_args
    assert original.args[2]['max_model_requests'] == original_limit
    config.config_path.write_text(config.config_path.read_text().replace(
        f'max_model_requests = {original_limit}', f'max_model_requests = {new_default}'))
    product = {'id': product_identity(original.kwargs['key']), 'state': 'completed'}
    client.request.side_effect = [product, product]
    await run_product_command(args, config.reload())
    assert client.request.call_args_list[-2] == original
    assert not list((config.config_path.parent / 'cli_submissions').glob('*.json'))


async def test_cli_rejects_a_boolean_in_pending_count_instead_of_granting_unlimited_calls(cli_environment):
    import json

    from agentflow.common import canonical_digest
    from agentflow.control.submissions import Submission
    config, client, _ = cli_environment
    goal = 'Create a persistent reading list'
    task = {'goal': goal, 'name': None, 'output': None}
    payload = {'name': goal[:30], 'goal': goal, 'output_directory': None,
               'target': 'web', 'review_mode': 'auto', 'max_model_requests': 200}
    with Submission(client.data_dir, task, payload) as intent:
        record_path = intent.path
    record = json.loads(record_path.read_text())
    record['payload']['max_model_requests'] = False
    record['payload_fingerprint'] = canonical_digest(record['payload'])
    record_path.write_text(json.dumps(record))
    with pytest.raises(DomainError) as error:
        await run_product_command(build_parser().parse_args(['run', goal]), config)
    assert error.value.code == 'invalid_cli_submission'
    client.request.assert_not_called()
    assert record_path.exists()


async def test_definite_rejection_does_not_pin_obsolete_defaults(cli_environment):
    config, client, _ = cli_environment
    args = build_parser().parse_args(['run', '创建可保存和编辑书目的阅读清单'])
    client.request.side_effect = DomainError('model_setup_required', 'configure first', 409)
    with pytest.raises(DomainError):
        await run_product_command(args, config)
    assert not list((config.config_path.parent / 'cli_submissions').glob('*.json'))
    client.close.assert_awaited_once()


async def test_status_does_not_start_the_platform(cli_environment, capsys):
    config, client, connect = cli_environment
    client.request.return_value = {'items': []}
    await run_product_command(build_parser().parse_args(['status']), config)
    connect.assert_awaited_once_with(config.settings, start=False, discover_active=True)
    assert '还没有产品' in capsys.readouterr().out


async def test_cancelled_observer_leaves_work_in_background(cli_environment, capsys):
    import asyncio
    config, client, _ = cli_environment
    async def request(method, path, *args, key=None):
        if method == 'POST':
            return {'id': product_identity(key), 'state': 'preparing'}
        raise asyncio.CancelledError()
    client.request.side_effect = request
    with pytest.raises(asyncio.CancelledError):
        await run_product_command(build_parser().parse_args(['run', 'Build a persistent reading list']), config)
    assert 'agentflow status ' + product_identity(client.request.call_args_list[0].kwargs['key']) in capsys.readouterr().err
    assert not list((config.config_path.parent / 'cli_submissions').glob('*.json'))
    client.close.assert_awaited_once()


@pytest.mark.parametrize('command', ['status', 'stop'])
def test_malformed_config_does_not_lock_out_running_controller(monkeypatch, tmp_path, command):
    monkeypatch.setattr('sys.argv', ['agentflow', command])
    monkeypatch.setattr('agentflow.configuration.load_configuration',
                        lambda **_: (_ for _ in ()).throw(DomainError('configuration_invalid', 'bad config')))
    monkeypatch.setattr('agentflow.control.instance.active_data_dir', lambda: tmp_path / 'active-data')
    dispatch = AsyncMock()
    monkeypatch.setattr('agentflow.product_cli.run_product_command', dispatch)
    main()
    assert dispatch.await_args.args[0].command == command
    assert dispatch.await_args.args[1].settings.data_dir == tmp_path / 'active-data'


@pytest.mark.parametrize('status', [401, 403, 413, 429])
async def test_replay_rejected_before_dispatch_keeps_original_uncertain_identity(cli_environment, status):
    config, client, _ = cli_environment
    args = build_parser().parse_args(['run', '创建可保存和编辑书目的阅读清单'])
    client.request.side_effect = DomainError('owner_connection_interrupted', 'lost acknowledgement', 503)
    with pytest.raises(DomainError):
        await run_product_command(args, config)
    original = client.request.call_args
    client.request.side_effect = DomainError('replay_rejected', 'request rejected before product lookup', status)
    with pytest.raises(DomainError):
        await run_product_command(args, config)
    assert client.request.call_args == original
    assert len(list((config.config_path.parent / 'cli_submissions').glob('*.json'))) == 1
    product = {'id': product_identity(original.kwargs['key']), 'name': 'reading', 'state': 'completed'}
    client.request.side_effect = [product, product]
    await run_product_command(args, config)
    assert client.request.call_args_list[-2] == original
    assert not list((config.config_path.parent / 'cli_submissions').glob('*.json'))


@pytest.mark.parametrize('receipt', [None, {}, {'state': 'preparing'}, {'id': 'another-product', 'state': 'preparing'}])
async def test_malformed_success_receipt_keeps_uncertain_submission(cli_environment, receipt):
    config, client, _ = cli_environment
    args = build_parser().parse_args(['run', '创建可保存和编辑书目的阅读清单'])
    client.request.return_value = receipt
    with pytest.raises(DomainError) as error:
        await run_product_command(args, config)
    assert error.value.code == 'owner_response_invalid'
    assert len(list((config.config_path.parent / 'cli_submissions').glob('*.json'))) == 1
    original = client.request.call_args
    product = {'id': product_identity(original.kwargs['key']), 'state': 'completed'}
    client.request.side_effect = [product, product]
    await run_product_command(args, config)
    assert client.request.call_args_list[-2] == original
    assert not list((config.config_path.parent / 'cli_submissions').glob('*.json'))
