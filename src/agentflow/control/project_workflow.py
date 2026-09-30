"""Read-only project/version navigation and full workflow maps.

Template stages are presentation records only. Executable IDs, attempts, quality
and budgets always come from the selected run's real graph.
"""
from __future__ import annotations

from agentflow.common import DomainError
from agentflow.control.readable import output_contract
from agentflow.control.workflow_stages import STAGE_ORDER

PRODUCT_STAGE_KEYS = STAGE_ORDER[:STAGE_ORDER.index('delivery') + 1]


def _run_ids(product):
    return list(dict.fromkeys(identity for identity in [product.get('initial_run_id'),
        *(product.get('run_ids') or []), product.get('run_id')] if identity))


def _product_for(run, plan, products):
    identity = (plan or {}).get('product_contract', {}).get('product_id')
    return next((product for product in products if product['id'] == identity), None) or next(
        (product for product in products if run['id'] in _run_ids(product)), None) or next(
        (product for product in products if product.get('project_id') == run.get('project_id')
         and run.get('project_id')), None)


async def project_workflow_index(store):
    products = await store.list('product')
    projects = {project['id']: project for project in await store.list('project')}
    runs = await store.list('run')
    plans = {plan['id']: plan for plan in await store.list('plan')}
    changes = await store.list('product_change')
    owned, assigned = {product['id']: [] for product in products}, set()
    for run in runs:
        product = _product_for(run, plans.get(run.get('plan_id')), products)
        if product:
            owned[product['id']].append(run)
            assigned.add(run['id'])
    rows = []
    for product in products:
        versions = []
        product_changes = sorted((change for change in changes if change.get('product_id') == product['id']),
                                 key=lambda change: (str(change.get('created_at', '')), change['id']))
        by_run = {change['run_id']: change for change in product_changes if change.get('run_id')}
        product_runs = {run['id']: run for run in owned[product['id']]}
        identities = list(dict.fromkeys([*_run_ids(product), *product_runs]))
        initial_number = 0
        for identity in identities:
            run = product_runs.get(identity)
            if not run or identity in by_run:
                continue
            initial_number += 1
            versions.append({'id': 'initial' if initial_number == 1 else 'run:' + identity, 'run_id': identity, 'change_id': None,
                'kind': 'initial' if initial_number == 1 else 'run',
                'label': '初始开发' if initial_number == 1 else f'开发记录 {initial_number}',
                'state': run.get('execution_state', 'pending')})
        if not versions and not product_runs:
            versions.append({'id': 'initial', 'run_id': None, 'change_id': None,
                'kind': 'baseline' if product.get('creation_mode') == 'import' else 'initial',
                'label': '已有项目基线' if product.get('creation_mode') == 'import' else '初始开发',
                'state': product.get('state', 'registered')})
        restart_count = 0
        for number, change in enumerate(product_changes, 1):
            restart = change.get('kind') == 'restart'
            restart_count += int(restart)
            title = str(change.get('title') or '').strip()
            versions.append({'id': 'change:' + change['id'], 'change_id': change['id'],
                'run_id': change.get('run_id'), 'kind': 'restart' if restart else 'change',
                'label': f'从头运行 {restart_count}' if restart else f'需求变更 {number} · {title or "未命名变更"}',
                'state': change.get('state', 'preparing'), 'base_run_id': change.get('base_run_id'),
                'created_at': change.get('created_at')})
        default = next((version for version in versions if version['change_id'] == product.get('current_change_id')
                        and version['change_id']), None) or next(
            (version for version in versions if version['run_id'] == product.get('run_id') and version['run_id']), None)
        rows.append({'id': 'product:' + product['id'], 'product_id': product['id'],
            'project_id': product.get('project_id'), 'name': product.get('name') or '未命名项目',
            'deleted': bool(product.get('deleted_at')), 'versions': versions,
            'default_version_id': (default or (versions[-1] if versions else {})).get('id')})
    for identity, project in projects.items():
        legacy = [run for run in runs if run['id'] not in assigned and run.get('project_id') == identity]
        if not legacy:
            continue
        versions = [{'id': 'run:' + run['id'], 'run_id': run['id'], 'change_id': None,
            'kind': 'run', 'label': f'开发记录 {number} · {str(run.get("display_name") or run.get("goal") or "未命名任务")[:90]}',
            'state': run.get('execution_state', 'pending')} for number, run in enumerate(legacy, 1)]
        rows.append({'id': 'project:' + identity, 'project_id': identity, 'product_id': None,
            'name': project.get('name') or '历史项目', 'deleted': False, 'versions': versions,
            'default_version_id': versions[-1]['id']})
    return {'items': rows}


async def _lineage(store, base_run_id, product):
    """Walk only frozen parents; never walk forward from a product's current run."""
    found = set()
    while base_run_id and base_run_id not in found:
        run = await store.read('run', base_run_id)
        if not run:
            break
        plan = await store.read('plan', run['plan_id']) if run.get('plan_id') else {}
        contract = (plan or {}).get('product_contract', {})
        if contract.get('product_id') not in {None, product['id']}:
            break
        if not contract.get('product_id') and run['id'] not in _run_ids(product):
            break
        found.add(run['id'])
        base_run_id = contract.get('base_run_id')
    return found


def _document_output(row):
    return {'artifact_id': row['id'], 'name': row['name'], 'media_type': 'text/markdown',
        'kind': 'document', 'digest': row['digest'], 'path': None,
        'preview_url': '/api/v1/readable_artifacts/' + row['id'],
        'download_url': '/api/v1/readable_artifacts/' + row['id'] + '?download=true'}


async def _verified_sources(store, product, plan, change):
    plan = plan or {}
    contract = plan.get('product_contract', {})
    base = (change or {}).get('base_run_id', contract.get('base_run_id'))
    lineage = await _lineage(store, base, product)
    bound = plan.get('reused_input_versions', {})
    identities = list(dict.fromkeys(identity for values in plan.get('stage_reused_inputs', {}).values() for identity in values))
    sources = {}
    for identity in identities:
        source = await store.read('artifact', identity)
        version = bound.get(identity) if isinstance(bound, dict) else None
        if (not source or not isinstance(version, dict) or source.get('stale')
                or source.get('digest') != version.get('digest') or source.get('revision') != version.get('revision')
                or source.get('product_id') not in {None, product['id']}):
            continue
        source_run_id = source.get('run_id')
        if source_run_id:
            if source_run_id not in lineage:
                continue
            run = await store.read('run', source_run_id)
            work = await store.read('work_item', source['work_item_id']) if source.get('work_item_id') else None
            if (not run or not work or work.get('run_id') != source_run_id or work.get('archived')
                    or work.get('parent_stage_id') or source.get('project_id') not in {None, run.get('project_id')}
                    or work.get('project_id') != run.get('project_id')
                    or work.get('generation') != source.get('generation') or work.get('status') != 'completed'
                    or work.get('quality_result') in {'failed', 'inconclusive'}
                    or identity not in work.get('artifact_ids', [])):
                continue
        elif (source.get('product_id') != product['id']
              or source.get('project_id') != (plan.get('project_id') or product.get('project_id'))
              or source.get('source_kind') not in {'owner_input', 'static_diagnosis'}):
            continue
        sources[source.get('step')] = source
    # Frozen project documents remain readable even after their old work items
    # are revised. Never substitute a newer project document for this snapshot.
    baseline = contract.get('document_baseline') or (change or {}).get('document_baseline') or {}
    if baseline.get('product_id') == product['id'] and baseline.get('run_id') == base:
        for stage, ref in baseline.get('documents', {}).items():
            row = await store.read('readable_artifact', ref.get('artifact_id', ''))
            if (not row or not row.get('project_document') or row.get('product_id') != product['id']
                    or row.get('logical_stage_key') != stage or row.get('digest') != ref.get('digest')
                    or any(row.get(field) != ref.get(field) for field in ('run_id', 'work_item_id', 'generation'))):
                continue
            sources[stage] = {**row, '_output': _document_output(row)}
    if not baseline:
        # Legacy versions can expose migrated snapshots only when their frozen
        # raw input is present in that exact historical producer's document.
        records = await store.list('readable_artifact')
        for stage, source in sources.items():
            candidates = [row for row in records if row.get('project_document')
                and row.get('product_id') == product['id'] and row.get('logical_stage_key') == stage
                and row.get('run_id') == source.get('run_id') and source.get('run_id')
                and row.get('work_item_id') == source.get('work_item_id')
                and row.get('generation') == source.get('generation')
                and any(version.get('id') == source['id'] and version.get('digest') == source['digest']
                        for version in row.get('source_versions', []))]
            if candidates:
                row = min(candidates, key=lambda row: (str(row.get('created_at', '')), row['id']))
                source['_output'] = _document_output(row)
    return sources


def _provenance(source=None, *, static=False):
    return {'kind': 'static_baseline' if static else 'inherited_output',
        'source_run_id': source.get('run_id') if source else None,
        'source_work_item_id': source.get('work_item_id') if source else None,
        'artifact_id': source.get('id') if source else None,
        'revision': source.get('revision') if source else None,
        'digest': source.get('digest') if source else None,
        'label': '完成 · 沿用已有项目基线' if static else '完成 · 沿用上版',
        'description': '沿用用户目标与现有工程静态基线；未开展市场或竞品调研，未新增 Agent 执行或质量验证。' if static
            else '沿用本次需求冻结的前置阶段产物；本次运行不重新执行该阶段，质量和执行记录仍属于来源版本。'}


def _template(product, actual, first_step, sources, *, static_registration=False):
    actual_by_key = {stage.get('key', stage['step']): stage for stage in actual}
    keys = list(PRODUCT_STAGE_KEYS)
    keys.extend(key for key in actual_by_key if key not in keys)
    ids = {key: f"project-stage:{product['id']}:{key}" for key in keys}
    old_ids = {stage['id']: ids[key] for key, stage in actual_by_key.items()}
    first = keys.index(first_step) if first_step in keys else 0
    stages = []
    for index, key in enumerate(keys):
        if key in actual_by_key:
            original = actual_by_key[key]
            dependencies = [old_ids[identity] for identity in original.get('dependencies', []) if identity in old_ids]
            if not dependencies and index == first and index:
                dependencies = [ids[keys[index - 1]]]
            stages.append({**original, 'id': ids[key], 'key': key,
                'work_item_id': original.get('work_item_id', original['id']), 'dependencies': dependencies})
            continue
        step = 'code_review' if key.endswith(':review') else key
        contract = output_contract({'step': step, 'logical_stage_key': key})
        source = sources.get(key)
        inherited = index < first and (source is not None or static_registration and key in {'goal', 'research'})
        status = 'inherited' if inherited else 'missing_baseline' if index < first else 'pending'
        stage = {'id': ids[key], 'key': key, 'step': step, 'name': contract['name'], 'status': status,
            'quality_result': 'not_applicable' if inherited else 'unknown', 'tasks': [], 'output': source.get('_output') if inherited and source else None,
            'presentation_only': True, 'expected_artifact': {'name': contract['artifact_name'],
                'kind': contract['kind'], 'description': contract['purpose']},
            'dependencies': [ids[keys[index - 1]]] if index else []}
        if inherited:
            stage['provenance'] = _provenance(source, static=not source or not source.get('run_id'))
            if stage['provenance']['kind'] == 'static_baseline':
                stage['expected_artifact'].update(
                    name='现有工程静态基线' if key == 'research' else '原始产品目标',
                    description=stage['provenance']['description'])
        elif status == 'missing_baseline':
            stage['blocking_reason'] = '缺少本次需求冻结且可核验的前置阶段基线，不能标记为已完成。'
        stages.append(stage)
    return stages


async def compose_product_workflow(store, run, plan, presentation):
    product = _product_for(run, plan, await store.list('product'))
    if not product:
        return presentation
    plan = plan or {}
    contract = plan.get('product_contract', {})
    change_id = contract.get('change_id') or contract.get('restart_id')
    change = await store.read('product_change', change_id) if change_id else next(
        (change for change in await store.list('product_change') if change.get('run_id') == run['id']
         and change.get('product_id') == product['id']), None)
    actual_steps = plan.get('actual_steps') or [stage['step'] for stage in presentation['stages']]
    first = actual_steps[0] if actual_steps else 'goal'
    sources = await _verified_sources(store, product, plan, change)
    return {**presentation, 'project_id': run.get('project_id'), 'product_id': product['id'],
        'template_id': 'product:' + product['id'],
        'version': {'change_id': (change or {}).get('id'), 'run_id': run['id'],
            'base_run_id': (change or {}).get('base_run_id', contract.get('base_run_id')),
            'start_stage': first, 'kind': (change or {}).get('kind', 'initial')},
        'stages': _template(product, presentation['stages'], first, sources)}


async def unstarted_product_workflow(store, product_id, change_id=None):
    product = await store.read('product', product_id)
    if not product:
        raise DomainError('not_found', 'Unknown product', 404)
    change = await store.read('product_change', change_id) if change_id else None
    if change_id and (not change or change.get('product_id') != product_id):
        raise DomainError('not_found', 'Unknown requirement change', 404)
    run_id = change.get('run_id') if change else product.get('initial_run_id')
    if run_id:
        raise DomainError('version_has_run', '该版本已有运行，请读取对应运行的工作流。', 409)
    plan_id = change.get('plan_id') if change else product.get('plan_id')
    plan = await store.read('plan', plan_id) if plan_id else None
    first = ((plan or {}).get('actual_steps') or [(change or {}).get('start_stage')
             or ('prd' if product.get('creation_mode') == 'import' else 'goal')])[0]
    static = bool(product.get('creation_mode') == 'import' and not (change or {}).get('base_run_id') and not plan)
    return {'run_id': None, 'input_fingerprint': (plan or {}).get('input_fingerprint', ''),
        'project_id': product.get('project_id'), 'product_id': product['id'], 'template_id': 'product:' + product_id,
        'version': {'change_id': change_id, 'run_id': None, 'base_run_id': (change or {}).get('base_run_id'),
            'start_stage': first, 'kind': (change or {}).get('kind', 'baseline' if static else 'initial')},
        'stages': _template(product, [], first, await _verified_sources(store, product, plan, change),
                            static_registration=static)}


def project_workflow_router(store):
    from fastapi import APIRouter
    router = APIRouter()

    @router.get('/api/v1/project_workflows')
    async def index():
        return await project_workflow_index(store)

    @router.get('/api/v1/products/{product_id}/workflow')
    async def unstarted(product_id: str, change_id: str | None = None):
        return await unstarted_product_workflow(store, product_id, change_id)

    return router
