import importlib
import os
import shutil

import pytest

NODE = os.environ.get('AGENTFLOW_NODE_PATH') or shutil.which('node') or 'node'
PATH = 'tests/coverage.spec.mjs'
SOURCE = '''import test from 'node:test';
import assert from 'node:assert/strict';
const existingHelper = (value) => value;
test('cascade', async () => {
  await withDb(async (db) => {
    const result = db.remove(1);
    assert.equal(result, true);
  });
  assert.equal(existingHelper(true), true);
});
test('detail', async ({page}) => {
  try {
    await expect(page.locator('h1')).toHaveText('Word');
  } finally {
    await page.close();
  }
});
'''


def verify(tmp_path, updated, original=SOURCE, paths=None):
    module = importlib.import_module('agentflow.control.test_coverage_guard')
    guard = module.TestCoverageGuard(node_path=NODE)
    before, after = tmp_path / 'before', tmp_path / 'after'
    for root, source in ((before, original), (after, updated)):
        (root / 'tests').mkdir(parents=True, exist_ok=True)
        (root / PATH).write_text(source)
        (root / 'package.json').write_text('{"scripts":{"test":"node --test"}}')
    return guard, before, after, guard.verify(before, after, paths if paths is not None else [PATH])


def append_nested(source=SOURCE):
    return source.replace('assert.equal(result, true);',
                          'assert.equal(result, true);\n    assert.equal(db.count("children"), 0);')


def test_appends_database_and_dom_assertions_inside_existing_blocks(tmp_path):
    updated = append_nested().replace("toHaveText('Word');", "toHaveText('Word');\n"
                                    "    await expect(page.locator('.mastery')).toHaveText('Mastered');")
    guard, _, _, report = verify(tmp_path, updated)
    assert report['ok'], report
    assert report['evidence'][0]['added_statements'] == 2
    assert report['evidence'][0]['original_assertion_ids'] == [
        assertion['assertion_id'] for case in guard.inspect(PATH, SOURCE)['cases']
        for assertion in case['assertions']
    ]
    assert report['requires_independent_review'] is True


def test_accepts_fresh_http_helper_with_return_and_cleanup_without_executing_it(tmp_path):
    helper = '''import {createServer} from 'node:http';
import {handler} from '../src/server.mjs';
const route = '/api/words';
async function withHttp(db, callback) {
  const server = createServer(handler(db));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try { return await callback(`http://127.0.0.1:${server.address().port}`); }
  finally { await new Promise(resolve => server.close(resolve)); }
}
'''
    updated = helper + SOURCE.replace('assert.equal(existingHelper(true), true);',
        '''assert.equal(existingHelper(true), true);
  await withHttp(null, async base => {
    const response = await fetch(base + route);
    assert.equal(response.status, 200);
  });''')
    _, _, _, report = verify(tmp_path, updated)
    assert report['ok'], report
    assert report['evidence'][0]['added_top_level_nodes'] == 4


@pytest.mark.parametrize('mutation', [
    lambda s: s.replace('assert.equal(result, true);', 'assert.equal(result, false);'),
    lambda s: s.replace('assert.equal(result, true);', ''),
    lambda s: s.replace('assert.equal(result, true);', 'assert.ok(true);'),
    lambda s: s.replace("test('cascade'", "test.skip('cascade'"),
    lambda s: s.replace("test('cascade'", "test.only('cascade'"),
    lambda s: s.replace("test('cascade'", "test.todo('cascade'"),
    lambda s: s.replace("test('cascade'", "test('renamed'"),
    lambda s: s.replace("async ({page})", "async ({page: other})"),
    lambda s: s.replace('const result = db.remove(1);', 'const result = true;'),
    lambda s: s.replace('const result = db.remove(1);\n    assert.equal(result, true);',
                        'assert.equal(result, true);\n    const result = db.remove(1);'),
    lambda s: s.replace('const result = db.remove(1);', 'assert.ok(true);\n    const result = db.remove(1);'),
    lambda s: s.replace('const existingHelper = (value) => value;', 'const existingHelper = () => true;'),
    lambda s: s.replace("'node:assert/strict'", "'node:assert'"),
    lambda s: s + "test('extra', () => {});",
    lambda s: s + "test.todo('extra');",
    lambda s: s.replace("test('detail'", "it('detail'"),
    lambda s: s.replace("test('cascade',", "test('cascade', {timeout: 0},"),
])
def test_rejects_modified_original_structure(tmp_path, mutation):
    _, _, _, report = verify(tmp_path, mutation(SOURCE))
    assert not report['ok']
    assert report['errors']


@pytest.mark.parametrize('addition', [
    'return;', 'break;', 'continue;', 'throw new Error("skip");',
    'assert = () => {};', 'assert.equal = () => {};', 'delete assert.equal;',
    'const assert = {equal() {}};', 'var result = true;',
    'process.exit(0);', 'process.exitCode = 0;', 'globalThis.process.exit(0);',
    'Object.assign(assert, {equal() {}});', 'Object.defineProperty(assert, "equal", {value() {}});',
    'Reflect.set(assert, "equal", () => {});',
    'const alias = assert; alias.equal = () => {};',
    'test.skip();', 'test.setTimeout(0);', 'expect.configure({soft:true});',
    'eval("assert.equal = () => {}");', 'new Function("return true")();',
    'import("node:process").then(p => p.exit(0));',
    'test("new", () => {});',
])
def test_rejects_appended_control_exits_rebindings_and_configuration(tmp_path, addition):
    updated = SOURCE.replace('assert.equal(result, true);', 'assert.equal(result, true);\n' + addition)
    _, _, _, report = verify(tmp_path, updated)
    assert not report['ok'], addition


@pytest.mark.parametrize('addition', [
    'boot();', 'const fresh = boot();', 'let fresh = 1;',
    'const existingHelper = () => true;',
    'function fresh() { assert.equal = () => {}; }',
    'function fresh() { process.exit(0); }',
    'function fresh() { test.skip(); }',
    'const fresh = {get value() { return 1; }};',
    'const fresh = {...assert};',
    "import {test as fresh} from 'node:test';",
    "import {exit as fresh} from 'node:process';",
    'const fresh = assert;',
])
def test_rejects_executable_or_unsafe_new_top_level_nodes(tmp_path, addition):
    _, _, _, report = verify(tmp_path, addition + '\n' + append_nested())
    assert not report['ok'], addition


def test_original_top_level_order_and_executable_statements_are_preserved(tmp_path):
    original = "setup();\n" + SOURCE
    _, _, _, report = verify(tmp_path, SOURCE + '\nsetup();', original)
    assert not report['ok']


def test_comments_and_formatting_are_ignored_and_inspected_source_is_never_run(tmp_path):
    original = "throw new Error('never execute');\n" + SOURCE
    _, _, _, report = verify(tmp_path, append_nested(original).replace('true', '/*comment*/ true'), original)
    assert report['ok'], report


@pytest.mark.parametrize('change', ['support', 'add', 'delete', 'mode', 'non_executable_mode', 'symlink'])
def test_rejects_file_set_modes_symlinks_and_unrelated_changes(tmp_path, change):
    guard, before, after, report = verify(tmp_path, append_nested())
    assert report['ok'], report
    if change == 'support':
        (after / 'package.json').write_text('{}')
    elif change == 'add':
        (after / 'extra.js').write_text('')
    elif change == 'delete':
        (after / 'package.json').unlink()
    elif change == 'mode':
        (after / PATH).chmod(0o755)
    elif change == 'non_executable_mode':
        (after / PATH).chmod(0o600)
    else:
        (after / PATH).unlink()
        (after / PATH).symlink_to(before / PATH)
    assert not guard.verify(before, after, [PATH])['ok']


@pytest.mark.parametrize('paths', [[], ['../outside.js'], ['/tmp/test.js'], [PATH, PATH],
                                 ['tests/./coverage.spec.mjs'], ['missing.js'], ['package.json']])
def test_requires_canonical_existing_test_file_authority(tmp_path, paths):
    _, _, _, report = verify(tmp_path, append_nested(), paths=paths)
    assert not report['ok']


def test_suite_callbacks_and_test_parameters_remain_exact(tmp_path):
    original = "test.describe('suite', () => { test('old', async () => { expect(1).toBe(1); }); });"
    updated = original.replace('expect(1).toBe(1);', 'expect(1).toBe(1); expect(2).toBe(2);')
    _, _, _, report = verify(tmp_path, updated, original)
    assert report['ok'], report
    _, _, _, report = verify(tmp_path, updated.replace("test('old'", "test('new'"), original)
    assert not report['ok']


@pytest.mark.parametrize('addition', [
    'function assert() {}',
    'function result() { return true; }',
    'const wrappers = [assert]; wrappers[0].equal = () => {};',
    'const wrappers = {check: expect}; wrappers.check.configure({soft: true});',
    'silence(assert);',
    'test["sk" + "ip"]();',
    'expect["configure"]({soft: true});',
    'const wrapped = Object(assert); wrapped.equal = () => {};',
    'assert.fail = () => {};',
    'assert["equal"]["constructor"]("return process")().exit(0);',
    'const check = expect; check = () => ({toBe() {}});',
])
def test_cannot_shadow_or_escape_original_assertion_and_framework_bindings(tmp_path, addition):
    _, _, _, report = verify(tmp_path, SOURCE.replace('assert.equal(result, true);',
                                                    'assert.equal(result, true);\n' + addition))
    assert not report['ok'], addition


def test_existing_hazardous_import_alias_cannot_be_invoked_by_added_code(tmp_path):
    original = "import {exit as endRun} from 'node:process';\n" + SOURCE
    updated = original.replace('assert.equal(result, true);', 'assert.equal(result, true); endRun(0);')
    _, _, _, report = verify(tmp_path, updated, original)
    assert not report['ok']


@pytest.mark.parametrize(('prefix', 'addition'), [
    ("import {test as register} from 'node:test';", "register('extra', () => {});"),
    ("import {beforeEach as hook} from 'node:test';", 'hook(() => {});'),
    ('const register = test;', "register('extra', () => {});"),
    ('const endRun = process.exit;', 'endRun(0);'),
    ('const options = test;', 'options["skip"]();'),
])
def test_existing_aliases_do_not_bypass_registration_or_exit_restrictions(tmp_path, prefix, addition):
    original = prefix + '\n' + SOURCE
    updated = original.replace('assert.equal(result, true);', 'assert.equal(result, true); ' + addition)
    _, _, _, report = verify(tmp_path, updated, original)
    assert not report['ok']


@pytest.mark.parametrize(('prefix', 'addition'), [
    ('', '{ const wrapper = {gate}; wrapper.gate.check = false; }'),
    ('', '{ const wrapper = {gate}; let fresh; ({value: wrapper.gate.check, other: fresh} = {value: false, other: 1}); }'),
    ('', "[]['filter']['con' + 'structor']('return process')().exit(0);"),
    ('', "{ const {['con' + 'structor']: fresh} = () => {}; fresh('return process')().exit(0); }"),
    ('', "{ const {constructor: fresh} = () => {}; fresh('return process')().exit(0); }"),
    ('', "gate.__defineGetter__('check', () => false);"),
    ('', "{ const mutate = Object.defineProperty; mutate(gate, 'check', {value: false}); }"),
    ('', "{ const {defineProperty: mutate} = Object; mutate(gate, 'check', {value: false}); }"),
    ("import strictAgain from 'node:assert/strict';\n"
     'function disable() { const fresh = strictAgain; fresh.equal = () => {}; }\n', 'disable();'),
])
def test_preserved_failing_assertion_cannot_be_disabled_through_indirection(tmp_path, prefix, addition):
    original = '''import test from 'node:test';
import assert from 'node:assert/strict';
const gate = {check: true};
test('retained failure', () => {
  { assert.equal(1, 1); }
  if (gate.check) assert.equal(1, 2);
});
'''
    updated = prefix + original.replace('assert.equal(1, 1);', 'assert.equal(1, 1); ' + addition)
    _, _, _, report = verify(tmp_path, updated, original)
    assert not report['ok']


@pytest.mark.parametrize(('prefix', 'addition'), [
    ('const mutate = Object.defineProperty;', "mutate(gate, 'check', {value: false});"),
    ('const O = Object;', "O.defineProperty(gate, 'check', {value: false});"),
    ('const O = Object; const mutate = O.defineProperty;', "mutate(gate, 'check', {value: false});"),
])
def test_old_reflection_aliases_cannot_disable_later_assertions(tmp_path, prefix, addition):
    original = prefix + '''
import test from 'node:test';
import assert from 'node:assert/strict';
const gate = {check: true};
test('retained failure', () => {
  { assert.equal(1, 1); }
  if (gate.check) assert.equal(1, 2);
});
'''
    updated = original.replace('assert.equal(1, 1);', 'assert.equal(1, 1); ' + addition)
    _, _, _, report = verify(tmp_path, updated, original)
    assert not report['ok']
