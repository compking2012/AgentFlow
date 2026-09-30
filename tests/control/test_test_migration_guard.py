import importlib
import os
import shutil

import pytest

NODE = os.environ.get('AGENTFLOW_NODE_PATH') or shutil.which('node') or 'node'
SOURCE = '''import {test, expect} from '@playwright/test';
const helper = /expect\\(.*\\)/g;
test.describe('calendar', () => {
 test('api', async ({request}) => {
  const actual = await request.get('/api');
  expect(actual).toContainEqual({name: '母亲节', type: 'western'});
 });
 test('web', async ({page}) => {
  // expect(fake).toBe('trap')
  const template = `expect(fake).toBe('trap')`;
  await expect(page.locator('h1')).toHaveText('母亲节');
 });
});
'''


def guard():
    import agentflow.control as control
    assert importlib.util.find_spec(control.__name__ + '.test_migration_guard') is not None, 'AST migration guard missing'
    from agentflow.control.test_migration_guard import TestMigrationGuard
    return TestMigrationGuard(node_path=NODE)


def fixture(tmp_path):
    g = guard()
    before, after = tmp_path / 'before', tmp_path / 'after'
    for root in [before, after]:
        (root / 'tests').mkdir(parents=True)
        (root / 'tests/calendar.spec.mjs').write_text(SOURCE)
    manifest = g.inspect('tests/calendar.spec.mjs', SOURCE)
    case = manifest['cases'][0]
    assertion = case['assertions'][0]
    action = dict(file=manifest['file'], case_id=case['case_id'], assertion_id=assertion['assertion_id'],
                  matcher=assertion['matcher'], old_expected=assertion['expected_source'],
                  new_expected="{name: '母亲节', type: 'western', emoji: '💐'}", requirement_refs=['requirement-1'])
    return g, before, after, manifest, action


def test_manifest_and_literal_object_migration(tmp_path):
    g, before, after, manifest, action = fixture(tmp_path)
    assert [c['name'] for c in manifest['cases']] == ['calendar > api', 'calendar > web']
    assert [len(c['assertions']) for c in manifest['cases']] == [1, 1]
    assert manifest['cases'][1]['assertions'][0]['actual_expression'] == "page.locator('h1')"
    (after / action['file']).write_text(SOURCE.replace(action['old_expected'], action['new_expected']))
    report = g.verify(before, after, [action])
    assert report['ok'], report
    assert report['affected_case_ids'] == [action['case_id']]
    assert report['evidence']


@pytest.mark.parametrize('imports,call,matcher', [
    ("import assert from 'node:assert/strict';", 'assert.deepEqual', 'node:assert.deepStrictEqual'),
    ("import {strict as check} from 'node:assert';", 'check.equal', 'node:assert.strictEqual'),
    ("import {deepStrictEqual as check} from 'node:assert';", 'check', 'node:assert.deepStrictEqual'),
    ("import {deepEqual as check} from 'node:assert/strict';", 'check', 'node:assert.deepStrictEqual'),
    ("import * as assert from 'node:assert/strict';", 'assert.deepEqual', 'node:assert.deepStrictEqual'),
])
def test_native_unit_assertions_migrate_only_expected_argument(tmp_path, imports, call, matcher):
    g = guard()
    original = "import test from 'node:test';\n" + imports + "\ntest('API contract',()=>{" + call + "(result(), {name:'a'}, 'keep message');});"
    before, after = tmp_path / 'before', tmp_path / 'after'
    for root in (before, after):
        root.mkdir()
        (root / 'unit.mjs').write_text(original)
    case = g.inspect('unit.mjs', original)['cases'][0]
    assert len(case['assertions']) == 1
    assertion = case['assertions'][0]
    assert assertion['matcher'] == matcher and assertion['actual_expression'] == 'result()'
    migration = {'path':'unit.mjs', 'case_id':case['case_id'], 'assertion_id':assertion['assertion_id'],
        'matcher':matcher, 'old_expected':"{name:'a'}", 'new_expected':"{name:'a',emoji:'x'}", 'requirement_refs':['R1']}
    (after / 'unit.mjs').write_text(original.replace(migration['old_expected'], migration['new_expected']))
    assert g.verify(before, after, [migration])['ok']
    (after / 'unit.mjs').write_text((after / 'unit.mjs').read_text().replace('result()', "{name:'a',emoji:'x'}"))
    assert not g.verify(before, after, [migration])['ok']


@pytest.mark.parametrize('source', [
    "const assert={deepEqual(){}};test('a',()=>assert.deepEqual(actual(),{}));",
    "import assert from 'node:assert';test('a',()=>assert.deepEqual(actual(),{}));",
    "import assert from 'node:assert/strict';test('a',(assert)=>assert.deepEqual(actual(),{}));",
    "import assert from 'node:assert/strict';assert.deepEqual=custom;test('a',()=>assert.deepEqual(actual(),{}));",
    "import assert from 'node:assert/strict';test('a',()=>assert.deepEqual(...actual(),{}));",
    "import assert from 'node:assert/strict';const alias=assert;test('a',()=>assert.deepEqual(actual(),{}));",
])
def test_native_assert_adapter_rejects_loose_custom_or_shadowed_bindings(source):
    assert not guard().inspect('unit.mjs', source)['cases'][0]['assertions']


@pytest.mark.parametrize('mutation', [
    lambda s: s.replace("test('api'", "test.skip('api'"),
    lambda s: s.replace("test('api'", "test.only('api'"),
    lambda s: s.replace("test('api'", "test('renamed'"),
    lambda s: s.replace('toContainEqual', 'toEqual'),
    lambda s: s.replace('expect(actual)', 'expect(other)'),
    lambda s: s.replace("request.get('/api')", "request.get('/other')"),
    lambda s: s.replace("await expect(page.locator('h1')).toHaveText('母亲节');", ''),
    lambda s: s.replace("// expect(fake)", "// changed expect(fake)"),
])
def test_rejects_non_expected_changes(tmp_path, mutation):
    g, before, after, _, action = fixture(tmp_path)
    changed = SOURCE.replace(action['old_expected'], action['new_expected'])
    (after / action['file']).write_text(mutation(changed))
    assert not g.verify(before, after, [action])['ok']


@pytest.mark.parametrize('replacement', ["process.exit()", "{...actual}", "{get name(){return 'x'}}", "`x${evil()}`", "expect.objectContaining({name:'母亲节'})", "{name: evil()}"])
def test_rejects_executable_expected(tmp_path, replacement):
    g, before, after, _, action = fixture(tmp_path)
    action['new_expected'] = replacement
    (after / action['file']).write_text(SOURCE.replace(action['old_expected'], replacement))
    assert not g.verify(before, after, [action])['ok']


def test_old_expected_is_exact_and_all_files_protected(tmp_path):
    g, before, after, _, action = fixture(tmp_path)
    (after / action['file']).write_text(SOURCE.replace(action['old_expected'], action['new_expected']))
    action['old_expected'] = "{name: '母亲节'}"
    assert not g.verify(before, after, [action])['ok']
    action['old_expected'] = "{name: '母亲节', type: 'western'}"
    (after / 'config.json').write_text('{}')
    assert not g.verify(before, after, [action])['ok']


def test_plain_text_migration_stable_ids_and_no_code_execution(tmp_path):
    g, before, after, manifest, action = fixture(tmp_path)
    case = manifest['cases'][1]
    assertion = case['assertions'][0]
    action.update(case_id=case['case_id'], assertion_id=assertion['assertion_id'], matcher=assertion['matcher'],
                  old_expected=assertion['expected_source'], new_expected="'💐 母亲节'")
    updated = SOURCE.replace("toHaveText('母亲节')", "toHaveText('💐 母亲节')")
    (after / action['file']).write_text(updated)
    assert g.verify(before, after, [action])['ok']
    assert [c['case_id'] for c in g.inspect(action['file'], updated)['cases']] == [c['case_id'] for c in manifest['cases']]
    assert g.inspect('test.js', "throw new Error('must not run'); test('x',()=>expect(1).toBe(1));")['cases']


@pytest.mark.parametrize('replacement', ["{name: '母亲节'}", "{name: '母亲节', type: null}", "{name: '母亲节', type: 'western', emoji: '💐'} /* stray */"])
def test_rejects_reduced_constraints_and_trailing_tokens(tmp_path, replacement):
    g, before, after, _, action = fixture(tmp_path)
    action['new_expected'] = replacement
    (after / action['file']).write_text(SOURCE.replace(action['old_expected'], replacement))
    assert not g.verify(before, after, [action])['ok']


def test_rejects_range_relaxation_even_with_action(tmp_path):
    g, before, after, _, action = fixture(tmp_path)
    source = "test('range',()=>expect(actual).toBeGreaterThan(5));"
    for root in (before, after):
        (root / action['file']).write_text(source)
    case = g.inspect(action['file'], source)['cases'][0]
    assertion = case['assertions'][0]
    action.update(case_id=case['case_id'], assertion_id=assertion['assertion_id'], matcher=assertion['matcher'],
                  old_expected='5', new_expected='1')
    (after / action['file']).write_text(source.replace('(5)', '(1)'))
    assert not g.verify(before, after, [action])['ok']


def test_multiple_actions_unicode_offsets_and_approved_exact_source(tmp_path):
    g, before, after, manifest, action = fixture(tmp_path)
    source = SOURCE.replace("const helper", "const emoji = '💐'; const helper")
    for root in (before, after):
        (root / action['file']).write_text(source)
    manifest = g.inspect(action['file'], source)
    second = manifest['cases'][1]
    a2 = dict(path=action['file'], case_id=second['case_id'], assertion_id=second['assertions'][0]['assertion_id'],
              matcher='toHaveText', old_expected="'母亲节'", new_expected="'💐 母亲节'", requirement_refs=['req-1'])
    updated = source.replace(action['old_expected'], action['new_expected']).replace("toHaveText('母亲节')", "toHaveText('💐 母亲节')")
    (after / action['file']).write_text(updated)
    assert g.verify(before, after, [action, a2])['ok']
    (after / action['file']).write_text(updated.replace("'💐 母亲节'", '"💐 母亲节"'))
    assert not g.verify(before, after, [action, a2])['ok']


def test_duplicate_actions_and_symlink_or_mode_changes(tmp_path):
    g, before, after, _, action = fixture(tmp_path)
    (after / action['file']).write_text(SOURCE.replace(action['old_expected'], action['new_expected']))
    assert not g.verify(before, after, [action, action])['ok']
    (after / action['file']).chmod(0o755)
    assert not g.verify(before, after, [action])['ok']


def test_real_regex_and_template_are_not_treated_as_literal_migrations(tmp_path):
    g, before, after, _, action = fixture(tmp_path)
    for expectation in ['/母亲节|foo/', '`母亲节`']:
        source = f"test('web', async()=>await expect(actual).toHaveText({expectation}));"
        for root in (before, after):
            (root / action['file']).write_text(source)
        case = g.inspect(action['file'], source)['cases'][0]
        a = case['assertions'][0]
        action.update(case_id=case['case_id'], assertion_id=a['assertion_id'], matcher=a['matcher'], old_expected=expectation, new_expected="'💐 母亲节'")
        (after / action['file']).write_text(source.replace(expectation, action['new_expected']))
        assert not g.verify(before, after, [action])['ok']


def test_framework_steps_belong_to_original_case():
    g = guard()
    manifest = g.inspect('step.spec.js', "test('parent',async()=>{await test.step('step',async()=>{await expect(actual).toHaveText('x');});});")
    assert [c['name'] for c in manifest['cases']] == ['parent']
    assert len(manifest['cases'][0]['assertions']) == 1


def test_structured_approved_requirement_references(tmp_path):
    g, before, after, _, action = fixture(tmp_path)
    action['requirement_refs'] = [{'artifact_id': 'accepted-1', 'requirement_id': 'R1', 'quote': 'Display emoji'}]
    (after / action['file']).write_text(SOURCE.replace(action['old_expected'], action['new_expected']))
    assert g.verify(before, after, [action])['ok']
    action['requirement_refs'][0]['quote'] = ''
    assert not g.verify(before, after, [action])['ok']


def test_preserves_exact_title_path_for_framework_case_mapping():
    manifest = guard().inspect('tests/api.spec.mjs', "test.describe('suite > literal',()=>test('case',()=>expect(1).toBe(1)));")
    assert manifest['cases'][0]['title_path'] == ['suite > literal', 'case']
