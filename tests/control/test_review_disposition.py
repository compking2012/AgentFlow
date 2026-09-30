import json
from copy import deepcopy
from pathlib import Path

from agentflow.control.review_disposition import ReviewDisposition, stable_findings


def fixture():
    review = json.loads((Path(__file__).parent / 'fixtures/webcalendar_review_disposition.json').read_text())
    review['id'] = review['reviewer_id']
    findings = stable_findings([review])
    context = {'run_id': 'run', 'source_snapshot_id': 'snapshot', 'source_commit': 'commit',
               'findings': findings, 'owners': [
                   {'work_item_id': 'api-tests', 'step': 'integration_test_implementation', 'write_paths': ['tests/api.spec.mjs'], 'planned_write_paths': ['tests']},
                   {'work_item_id': 'web-tests', 'step': 'integration_test_implementation', 'write_paths': ['tests/web.spec.mjs'], 'planned_write_paths': ['tests']},
                   {'work_item_id': 'web', 'step': 'implementation', 'write_paths': ['public'], 'planned_write_paths': ['public']}],
               'accepted_requirements': [{'artifact_id': 'prd', 'requirement_id': 'R1',
                                          'text': 'Holiday responses include emoji. Show 💐 母亲节. Preserve original legend.'}],
               'assertions': [dict(path=p, case_id='case', assertion_id='assert', matcher='toEqual', old_expected=v)
                              for p, v in [('tests/api.spec.mjs', "{name: '母亲节'}"), ('tests/web.spec.mjs', "'母亲节'")]]}
    actions = []
    for finding, owner, new in zip(findings, context['owners'], ["{name: '母亲节', emoji: '💐'}", "'💐 母亲节'", None]):
        action = {'finding_id': finding['finding_id'], 'classification': 'test_contract_migration' if new else 'production_fix',
                  'evidence_paths': [finding['path']], 'repair_paths': [finding['path'] if new else 'public/index.html'],
                  'owner_work_item_id': owner['work_item_id'], 'reason': 'Accepted requirement explicitly changes expected behavior',
                  'requirement_refs': [{'artifact_id': 'prd', 'requirement_id': 'R1', 'quote': 'Show 💐 母亲节.'}],
                  'migrations': []}
        if new:
            action['migrations'] = [{**context['assertions'][len(actions)], 'new_expected': new,
                                     'requirement_refs': action['requirement_refs']}]
        actions.append(action)
    return context, {'run_id': 'run', 'source_snapshot_id': 'snapshot', 'source_commit': 'commit', 'actions': actions}


def test_three_findings_keep_production_fix_separate():
    context, result = fixture()
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert checked['ok'], checked
    assert [a['classification'] for a in checked['actions']] == ['test_contract_migration', 'test_contract_migration', 'production_fix']


def test_two_reviewers_can_bind_one_identical_assertion_edit():
    context, result = fixture()
    context['findings'].append({**context['findings'][0], 'finding_id': 'second-review'})
    result['actions'].append({**deepcopy(result['actions'][0]), 'finding_id': 'second-review'})
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert checked['ok'], checked
    from agentflow.control.review_disposition import migration_edits
    edits = migration_edits(checked['actions'])
    assert len(edits) == 2
    assert {a['finding_id'] for a in checked['actions']} == {f['finding_id'] for f in context['findings']}


def test_conflicting_reviewers_do_not_select_an_arbitrary_expected_value():
    context, result = fixture()
    context['findings'].append({**context['findings'][0], 'finding_id': 'second-review'})
    other = {**deepcopy(result['actions'][0]), 'finding_id': 'second-review'}
    other['migrations'][0]['new_expected'] = "{name: 'wrong', emoji: 'x'}"
    result['actions'].append(other)
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert not checked['ok']
    conflict = next(i for i in checked['issues'] if i['code'] == 'conflicting_assertion_migration')
    assert conflict['related_finding_id'] == result['actions'][0]['finding_id']


def test_each_migration_citation_is_sufficient_without_duplicate_action_citations():
    context, result = fixture()
    result['actions'][0]['requirement_refs'] = []
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert checked['ok'], checked
    assert checked['actions'][0]['requirement_refs'] == result['actions'][0]['migrations'][0]['requirement_refs']
    assert result['actions'][0]['requirement_refs'] == []


def test_inherited_citations_cannot_hide_missing_or_invented_migration_evidence():
    for references in ([], [{'artifact_id': 'prd', 'requirement_id': 'R1', 'quote': 'invented'}]):
        context, result = fixture()
        result['actions'][0]['requirement_refs'] = []
        result['actions'][0]['migrations'][0]['requirement_refs'] = references
        assert not ReviewDisposition(None, None, None).validate(result, context)['ok']


def test_disposition_schema_can_be_embedded_in_finish_tool_parameters():
    from jsonschema import Draft202012Validator

    from agentflow.control.review_disposition import REVIEW_DISPOSITION_SCHEMA
    _, result = fixture()
    tool_schema = {'type': 'object', 'properties': {'result': REVIEW_DISPOSITION_SCHEMA}}
    Draft202012Validator(tool_schema).validate({'result': result})


def test_reject_missing_finding_and_pass_downgrade():
    context, result = fixture()
    result['actions'].pop()
    result['actions'][0]['classification'] = 'pass'
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert not checked['ok']
    assert {'finding_coverage', 'invalid_result'} <= {x['code'] for x in checked['issues']}


def test_unaccepted_requirement_wrong_quote_assertion_scope_and_owner_rejected():
    for mutate, code in [
        (lambda c, r: r['actions'][0]['migrations'][0].update(assertion_id='new'), 'assertion_not_found'),
        (lambda c, r: r['actions'][0]['requirement_refs'][0].update(artifact_id='foreign'), 'requirement_not_accepted'),
        (lambda c, r: r['actions'][0]['requirement_refs'][0].update(quote='invented'), 'requirement_quote_missing'),
        (lambda c, r: c['owners'][0].update(planned_write_paths=['src']), 'repair_scope_denied'),
        (lambda c, r: r['actions'][0].update(owner_work_item_id='web'), 'repair_scope_denied'),
        (lambda c, r: r['actions'][0]['migrations'][0].update(matcher='toContain'), 'assertion_contract_changed'),
    ]:
        context, result = deepcopy(fixture())
        mutate(context, result)
        checked = ReviewDisposition(None, None, None).validate(result, context)
        assert not checked['ok'], code
        assert code in {x['code'] for x in checked['issues']}, checked


def test_malformed_result_is_explicit_and_clarification_does_not_authorize_fix():
    context, result = fixture()
    result['actions'][0]['finding_id'] = []
    assert not ReviewDisposition(None, None, None).validate(result, context)['ok']
    context, result = fixture()
    result['actions'][0].update(classification='needs_clarification', repair_paths=[], migrations=[], owner_work_item_id=None)
    assert ReviewDisposition(None, None, None).validate(result, context)['ok']
    result['actions'][0]['repair_paths'] = ['public/index.html']
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert 'clarification_has_mutations' in {i['code'] for i in checked['issues']}


async def test_context_only_accepts_current_and_version_bound_requirements(monkeypatch):
    from agentflow.control import review_disposition as module

    run = {'id': 'run', 'plan_id': 'plan'}
    reviewer = {'id': 'reviewer', 'run_id': 'run'}
    producer = {'id': 'producer', 'attempt_id': 'source', 'generation': 1}
    review = {'id': 'review', 'reviewed_commit': 'commit', 'blocking_findings': [{'path': 'tests/api.spec.mjs'}]}
    author = {'id': 'prd-author', 'status': 'completed', 'step': 'prd', 'artifact_ids': ['accepted', 'foreign', 'reused'], 'generation': 1}
    artifacts = [{'id': identity, 'digest': 'digest', 'run_id': origin, 'work_item_id': 'prd-author',
                  'step': 'prd', 'generation': 1, 'revision': 3}
                 for identity, origin in [('accepted', 'run'), ('foreign', 'other'), ('reused', 'old')]]
    rows = {'work_item': [reviewer, author, {'id': 'test-owner', 'key': 'tests', 'run_id': 'run',
                                          'step': 'integration_test_implementation', 'write_paths': ['tests']}],
            'artifact': artifacts,
            'plan': [{'id': 'plan', 'state': 'started', 'started_run_id': 'run',
                      'work_specs': [{'key': 'tests', 'step': 'integration_test_implementation'}],
                      'reused_inputs': ['reused'], 'reused_input_versions': {'reused': {'revision': 3, 'digest': 'digest'}}}],
            'code_snapshot': [{'id': 'source', 'run_id': 'run', 'work_item_id': 'producer', 'generation': 1, 'commit_oid': 'commit'}]}
    class Store:
        async def list(self, kind):
            return rows.get(kind, [])
    class Artifacts:
        async def read(self, digest):
            return json.dumps({'requirements': [{'requirement_id': 'R-1', 'text': 'Add emoji'}]}).encode()
    monkeypatch.setattr(module, 'review_producer', lambda *args: producer)
    monkeypatch.setattr(module, 'review_cohort', lambda *args: {'members': [(reviewer, review)]})
    monkeypatch.setattr(module, '_completed_review', lambda *args: review)
    result = await ReviewDisposition(Store(), Artifacts(), None).build(run, reviewer)
    assert result['ok'], result
    assert {r['artifact_id'] for r in result['context']['accepted_requirements']} == {'accepted', 'reused'}
    assert result['context']['owners'][0]['planned_write_paths'] == ['.']
    artifacts[2]['revision'] = 4
    result = await ReviewDisposition(Store(), Artifacts(), None).build(run, reviewer)
    assert {r['artifact_id'] for r in result['context']['accepted_requirements']} == {'accepted'}
    author.update(approval_required=True, approved_fingerprint='approval')
    result = await ReviewDisposition(Store(), Artifacts(), None).build(run, reviewer)
    assert result['context']['accepted_requirements'] == []


async def test_build_reports_missing_reviewer_without_silent_none():
    class Store:
        async def list(self, kind):
            return []
    result = await ReviewDisposition(Store(), None, None).build({'id': 'run'}, {'id': 'reviewer'})
    assert result == {'ok': False, 'issues': [{'code': 'review_stale', 'message': 'Reviewer changed before triage'}]}


def test_development_author_cannot_receive_test_migration_even_with_broad_scope():
    context, result = fixture()
    context['owners'][0].update(step='implementation', write_paths=['.'], planned_write_paths=['.'])
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert not checked['ok']
    assert 'repair_owner_role_mismatch' in {i['code'] for i in checked['issues']}


def test_more_specific_test_author_cannot_be_replaced_with_broad_unit_owner():
    context, result = fixture()
    context['owners'].append({'work_item_id': 'unit', 'step': 'unit_test_implementation',
                              'write_paths': ['.'], 'planned_write_paths': ['.']})
    result['actions'][0]['owner_work_item_id'] = 'unit'
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert not checked['ok']
    assert 'repair_owner_ambiguous' in {i['code'] for i in checked['issues']}


def test_markdown_requirement_ids_include_table_rows_and_wrapped_documents():
    from agentflow.control.review_disposition import markdown_requirements
    text = '## **FR-15** Emoji\nShow emoji\n| PR-A-09 | Retain labels |\n### `NFR-07` fallback\nKeep text\n'
    assert [r[0] for r in markdown_requirements(text)] == ['FR-15', 'PR-A-09', 'NFR-07']


def coverage_fixture():
    context, result = fixture()
    context['owners'][0].update(work_item_id='unit-tests', step='unit_test_implementation',
                                write_paths=['tests/unit.test.mjs'])
    context['test_paths'] = ['tests/unit.test.mjs', 'tests/web.spec.mjs']
    context['assertions'] = []  # Coverage need not start from a migratable assertion.
    plans = [('unit-plan', 'unit_test_plan', 'UT-1', 'Verify real HTTP responses and cascade deletion.'),
             ('web-plan', 'integration_test_strategy', 'WEB-1', 'Assert the detail mastery badge.')]
    context['accepted_documents'] = [dict(artifact_id=artifact, step=step, digest=artifact + '-digest',
                                           revision=1, text=text) for artifact, step, _, text in plans]
    context['accepted_requirements'].extend(dict(artifact_id=artifact, step=step, requirement_id=rid,
                                                 text=text) for artifact, step, rid, text in plans)
    for index in (0, 1):
        context['findings'][index]['path'] = 'tests/unit.test.mjs'
        result['actions'][index].update(classification='test_coverage_extension', owner_work_item_id='unit-tests',
            evidence_paths=['tests/unit.test.mjs'], repair_paths=['tests/unit.test.mjs'], migrations=[],
            requirement_refs=[{'artifact_id': 'unit-plan', 'requirement_id': 'UT-1',
                               'quote': 'Verify real HTTP responses and cascade deletion.'}])
    context['findings'][2]['path'] = 'public/app.mjs'
    result['actions'][2].update(evidence_paths=['public/app.mjs'], repair_paths=['public/app.mjs'])
    result['actions'].append({**deepcopy(result['actions'][2]), 'classification': 'test_coverage_extension',
        'owner_work_item_id': 'web-tests', 'repair_paths': ['tests/web.spec.mjs'],
        'requirement_refs': [{'artifact_id': 'web-plan', 'requirement_id': 'WEB-1',
                             'quote': 'Assert the detail mastery badge.'}]})
    return context, result


def test_one_finding_can_have_distinct_production_and_coverage_owners():
    context, result = coverage_fixture()
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert checked['ok'], checked
    assert len(checked['actions']) == 4
    assert {a['finding_id'] for a in checked['actions']} == {f['finding_id'] for f in context['findings']}
    assert [(a['classification'], a['owner_work_item_id'], a['repair_paths']) for a in checked['actions'][2:]] == [
        ('production_fix', 'web', ['public/app.mjs']),
        ('test_coverage_extension', 'web-tests', ['tests/web.spec.mjs'])]


def test_duplicate_actions_for_the_same_finding_and_owner_are_rejected():
    context, result = coverage_fixture()
    result['actions'].append(deepcopy(result['actions'][0]))
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert not checked['ok']
    assert 'finding_coverage' in {i['code'] for i in checked['issues']}


def test_coverage_requires_an_accurate_corresponding_plan_citation():
    for references, code in [
        ([], 'coverage_evidence_missing'),
        ([{'artifact_id': 'unit-plan', 'requirement_id': 'UT-1', 'quote': 'invented'}], 'requirement_quote_missing'),
        ([{'artifact_id': 'unit-plan', 'requirement_id': 'UT-1', 'quote': ' '}], 'requirement_quote_missing'),
        ([{'artifact_id': 'missing', 'requirement_id': 'UT-1', 'quote': 'Verify real HTTP'}], 'requirement_not_accepted'),
        ([{'artifact_id': 'prd', 'requirement_id': 'R1', 'quote': 'Show 💐 母亲节.'}], 'coverage_evidence_missing'),
        ([{'artifact_id': 'web-plan', 'requirement_id': 'WEB-1', 'quote': 'Assert the detail mastery badge.'}], 'coverage_evidence_missing'),
    ]:
        context, result = coverage_fixture()
        result['actions'][0]['requirement_refs'] = references
        checked = ReviewDisposition(None, None, None).validate(result, context)
        assert not checked['ok'], references
        assert code in {i['code'] for i in checked['issues']}, checked


def test_legacy_evidence_without_step_provenance_cannot_authorize_coverage():
    context, result = coverage_fixture()
    context.pop('accepted_documents')
    for requirement in context['accepted_requirements']:
        requirement.pop('step', None)
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert not checked['ok']
    assert 'coverage_evidence_missing' in {i['code'] for i in checked['issues']}


def test_coverage_accepts_plan_document_provenance_for_legacy_requirement_rows():
    context, result = coverage_fixture()
    for requirement in context['accepted_requirements']:
        requirement.pop('step', None)
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert checked['ok'], checked


def test_test_plan_never_authorizes_expectation_migration():
    for provenance in ('requirement', 'document'):
        context, result = fixture()
        if provenance == 'requirement':
            context['accepted_requirements'][0]['step'] = 'integration_test_strategy'
        else:
            context['accepted_documents'] = [{'artifact_id': 'prd', 'step': 'integration_test_strategy'}]
        checked = ReviewDisposition(None, None, None).validate(result, context)
        assert not checked['ok'], provenance
        assert 'migration_authority_invalid' in {i['code'] for i in checked['issues']}, checked


def mixed_test_repairs(coverage_path):
    context, result = fixture()
    context['owners'][0]['write_paths'] = ['tests']
    context['test_paths'] = ['tests/api.spec.mjs', 'tests/api-status.test.mjs', 'tests/web.spec.mjs']
    context['accepted_documents'] = [{'artifact_id': 'test-plan', 'step': 'integration_test_strategy'}]
    context['accepted_requirements'].append({'artifact_id': 'test-plan', 'step': 'integration_test_strategy',
        'requirement_id': 'IT1', 'text': 'Append response status assertion.'})
    context['findings'].append({**context['findings'][0], 'finding_id': 'extra-coverage-finding'})
    result['actions'].append({'finding_id': 'extra-coverage-finding', 'classification': 'test_coverage_extension',
        'owner_work_item_id': 'api-tests', 'repair_paths': [coverage_path], 'evidence_paths': ['tests/api.spec.mjs'],
        'migrations': [], 'reason': 'Response status lacks coverage.',
        'requirement_refs': [{'artifact_id': 'test-plan', 'requirement_id': 'IT1', 'quote': 'Append response status assertion.'}]})
    return context, result


def test_overlapping_coverage_and_migration_require_separate_verified_batches():
    context, result = mixed_test_repairs('tests/api.spec.mjs')
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert not checked['ok']
    conflict = next(issue for issue in checked['issues'] if issue['code'] == 'conflicting_test_repair_kinds')
    assert conflict['path'] == 'tests/api.spec.mjs'
    assert set(conflict['finding_ids']) == {result['actions'][0]['finding_id'], 'extra-coverage-finding'}


def test_distinct_files_allow_coverage_and_migration_for_one_owner():
    context, result = mixed_test_repairs('tests/api-status.test.mjs')
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert checked['ok'], checked
    assert len(checked['actions']) == 4


def test_coverage_rejects_migrations_new_paths_and_protected_support_configuration():
    for path in ('tests/new.test.mjs', 'public/app.mjs', 'tests/support/config.mjs',
                 'tests/playwright.web.config.mjs', 'tests/custom.config.mjs', 'tests/locked.test.mjs'):
        context, result = coverage_fixture()
        context['owners'][0].update(write_paths=['.'], planned_write_paths=['.'])
        if path != 'tests/new.test.mjs':
            context['test_paths'].append(path)
        context['protected_paths'] = ['tests/locked.test.mjs']
        result['actions'][0]['repair_paths'] = [path]
        checked = ReviewDisposition(None, None, None).validate(result, context)
        assert not checked['ok'], path
        assert 'coverage_scope_denied' in {i['code'] for i in checked['issues']}, checked
    context, result = coverage_fixture()
    result['actions'][0]['migrations'] = fixture()[1]['actions'][0]['migrations']
    checked = ReviewDisposition(None, None, None).validate(result, context)
    assert not checked['ok']
    assert 'coverage_has_migrations' in {i['code'] for i in checked['issues']}


def test_coverage_keeps_original_owner_role_plan_scope_and_most_specific_assignment():
    for mutate, code in [
        (lambda c, r: c['owners'][0].update(step='implementation'), 'repair_owner_role_mismatch'),
        (lambda c, r: c['owners'][0].update(planned_write_paths=['src']), 'repair_scope_denied'),
        (lambda c, r: c['owners'].append({**c['owners'][0], 'work_item_id': 'conflicting-owner'}), 'repair_owner_ambiguous'),
    ]:
        context, result = coverage_fixture()
        mutate(context, result)
        checked = ReviewDisposition(None, None, None).validate(result, context)
        assert not checked['ok']
        assert code in {i['code'] for i in checked['issues']}, checked


async def test_context_includes_current_accepted_test_plans_with_exact_text_and_step(monkeypatch):
    from agentflow.control import review_disposition as module

    run = {'id': 'run', 'plan_id': 'plan'}
    reviewer = {'id': 'reviewer', 'run_id': 'run'}
    producer = {'id': 'producer', 'attempt_id': 'source', 'generation': 1}
    review = {'id': 'review', 'reviewed_commit': 'commit', 'blocking_findings': [{'path': 'tests/unit.test.mjs'}]}
    document = '## UT-1 Real HTTP\nAssert that a live server returns the requested record.\n'
    rows = {'work_item': [reviewer], 'artifact': [], 'plan': [
        {'id': 'plan', 'state': 'started', 'started_run_id': 'run', 'work_specs': []}],
        'code_snapshot': [{'id': 'source', 'run_id': 'run', 'work_item_id': 'producer',
                           'generation': 1, 'commit_oid': 'commit'}]}
    for name, step in [('unit', 'unit_test_plan'), ('integration', 'integration_test_strategy')]:
        rows['work_item'].append({'id': name, 'run_id': 'run', 'status': 'completed', 'step': step,
                                  'artifact_ids': [name + '-plan'], 'generation': 2})
        rows['artifact'].append({'id': name + '-plan', 'digest': name, 'run_id': 'run',
                                'work_item_id': name, 'step': step, 'generation': 2, 'revision': 3})
    class Store:
        async def list(self, kind):
            return rows.get(kind, [])
    class Artifacts:
        async def read(self, digest):
            return json.dumps({'result': {'content': document}}, ensure_ascii=False).encode()
    monkeypatch.setattr(module, 'review_producer', lambda *args: producer)
    monkeypatch.setattr(module, 'review_cohort', lambda *args: {'members': [(reviewer, review)]})
    monkeypatch.setattr(module, '_completed_review', lambda *args: review)
    disposition = ReviewDisposition(Store(), Artifacts(), None)
    built = await disposition.build(run, reviewer)
    assert built['ok'], built
    assert [(d['artifact_id'], d['step'], d['text'], d['revision']) for d in built['context']['accepted_documents']] == [
        ('unit-plan', 'unit_test_plan', document, 3),
        ('integration-plan', 'integration_test_strategy', document, 3)]
    assert {r['step'] for r in built['context']['accepted_requirements']} == {
        'unit_test_plan', 'integration_test_strategy'}
    assert all(r['text'] == document for r in built['context']['accepted_requirements'])
    rows['artifact'][0]['generation'] = 1
    rows['work_item'][2]['quality_result'] = 'failed'
    built = await disposition.build(run, reviewer)
    assert built['context']['accepted_documents'] == []
    assert built['context']['accepted_requirements'] == []
