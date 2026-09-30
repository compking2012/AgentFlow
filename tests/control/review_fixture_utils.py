"""Complete durable context for review fixtures without weakening production guards."""
from uuid import uuid4

from agentflow.models.budget import account_id


async def complete_review_guard_context(store):
    def apply(tx):
        run = tx.get('run', 'run')
        for kind, owner in [('run', 'run'), ('iteration', 'iteration')]:
            account = tx.get('budget_account', account_id(kind, owner))
            row = tx.get(kind, owner)
            tx.put(kind, owner, {**(row or {}), 'budget_limit': {**(row or {}).get('budget_limit', {}),
                'currency': account['currency'], 'limit_micros': account['limit_micros'],
                'max_model_requests': account['max_requests']}}, row['revision'] if row else None)
        for work in tx.list('work_item'):
            if work.get('run_id') != run['id'] or not work.get('attempt_id'):
                continue
            row = tx.get('attempt', work['attempt_id'])
            tx.put('attempt', work['attempt_id'], {**(row or {}), 'run_id': run['id'],
                'iteration_id': run['iteration_id'], 'work_item_id': work['id'], 'status': work['status'],
                **{key: work[key] for key in ('generation', 'fencing_token', 'input_fingerprint')}},
                row['revision'] if row else None)
        return {}
    await store.command('fixture.review-context', str(uuid4()), {}, apply)
