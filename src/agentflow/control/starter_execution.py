"""Deterministic recipes for the verified built-in Node/Web/API foundation."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from agentflow.common import DomainError

STARTER_SUPPORT_FILES = (
    'package.json', 'package-lock.json', 'tooling/build.mjs',
    'tests/support/config.mjs', 'tests/support/fixtures.mjs', 'tests/support/global-setup.mjs',
    'tests/playwright.api.config.mjs', 'tests/playwright.web.config.mjs',
)


async def starter_execution_spec(repository, source, commit, plan, configs, phases, planned_cases, plan_sources):
    """Only the accepted plan supplies case identities; source tests are not scanned."""
    if (plan.get('product_contract', {}).get('stack') != 'node_web_api'
            or not configs or any(config.app_target not in {'api', 'web'} for config in configs)):
        raise DomainError('execution_plan_missing', '此项目需要提交明确的 agentflow.project.json；无法使用内置 Web/API 执行配置。')
    if (not plan_sources or any(not item['accepted'] for item in plan_sources)
            or any(not any(cases for (target, item_phase), cases in planned_cases.items() if item_phase == phase)
                   for phase in phases)):
        raise DomainError('test_plan_missing', '缺少已接受且仍有效的独立测试计划，不能生成执行配置。')
    by_id = {config.target_config_id: config for config in configs}
    if len(by_id) != len(configs) or any(target not in by_id for target, phase in planned_cases if phase in phases):
        raise DomainError('target_scope_mismatch', '独立测试计划必须对应已批准的目标配置。')
    template = Path(__file__).resolve().parents[1] / 'resources/web_api_starter'
    raw = await asyncio.to_thread(repository._run, source, ['ls-tree', '-r', '-z', commit, '--', *STARTER_SUPPORT_FILES])
    entries = {}
    for row in filter(None, raw.split(b'\0')):
        header, name = row.split(b'\t', 1)
        mode, kind, oid = header.split()
        entries[name.decode()] = (mode, kind, oid.decode())
    support = []
    for name in STARTER_SUPPORT_FILES:
        entry = entries.get(name)
        if not entry or entry[0] not in {b'100644', b'100755'} or entry[1] != b'blob':
            raise DomainError('execution_plan_missing', '内置执行支撑文件缺失或类型不符，请提供明确的执行清单。')
        content = await asyncio.to_thread(repository._run, source, ['cat-file', 'blob', entry[2]])
        if content != (template / name).read_bytes():
            raise DomainError('execution_plan_missing', '执行支撑文件已偏离内置模板，请提供明确的执行清单。')
        support.append({'path': name, 'blob_oid': entry[2], 'digest': 'sha256:' + hashlib.sha256(content).hexdigest()})
    targets = []
    for config in configs:
        kind = config.app_target.value
        recipes = {'target_config_id': config.target_config_id,
                   'build': {'adapter': kind, 'project_path': '.',
                             'output_paths': {'product': 'build/product', 'test': 'build/tests'}}}
        for phase in phases:
            expected = sorted(planned_cases.get((config.target_config_id, phase), set()))
            if not expected:
                continue
            recipe = {'adapter': kind, 'test_project_path': 'build/tests', 'product_path': 'build/product',
                      'expected_case_ids': expected}
            if phase == 'unit':
                recipe.update(test_kind='unit', unit_project='build/tests/unit.test.mjs',
                              report_path=f'reports/{kind}-unit.xml')
            else:
                recipe.update(test_kind='api' if kind == 'api' else 'integration',
                    framework_config=f'build/tests/playwright.{kind}.config.mjs', report_path=f'reports/{kind}-integration.json')
            recipes[phase] = recipe
        if not any(phase in recipes for phase in phases):
            raise DomainError('test_plan_missing', '每个必选目标都必须有已接受的测试阶段与用例。')
        targets.append(recipes)
    return {'schema_version': 1, 'targets': targets, 'cross_scenarios': []}, {
        'kind': 'verified_builtin_node_web_api', 'source_commit': commit, 'support_files': support,
        'accepted_plan_artifacts': plan_sources, 'plan_revision': plan['revision'],
        'target_config_fingerprints': {config.target_config_id: config.fingerprint for config in configs}}
