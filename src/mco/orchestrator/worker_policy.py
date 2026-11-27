"""Job capability admission and durable quota cooldown evidence."""
import json
import re
from datetime import datetime, timedelta, timezone

from mco.config import get_config
from mco.orchestrator.delivery import _parse_ts


def can_run(job, role, instance, config=None):
    cfg = config if config is not None else get_config()
    payload = job.get('input_payload') or {}
    routing = payload.get('routing', {}) if isinstance(payload, dict) else {}
    if not isinstance(routing, dict):
        return False
    roles = routing.get('allowed_roles')
    required = routing.get('required_capabilities', [])
    if roles is not None and (not isinstance(roles, list) or any(not isinstance(v, str) for v in roles)
                              or role.lower() not in {v.lower() for v in roles}):
        return False
    if not isinstance(required, list) or any(not isinstance(v, str) for v in required):
        return False
    if not required:
        return True
    try:
        capabilities = json.loads(cfg.get('MCO_WORKER_CAPABILITIES') or '{}')
        available = capabilities.get(instance, [])
        return isinstance(available, list) and set(required) <= set(available)
    except (ValueError, TypeError, AttributeError):
        return False


_QUOTA_EVENT = 'worker_out_of_quota'
_MAX_DEFERRALS = 2
_RECOVERY_WINDOW = timedelta(minutes=15)


def quota_error(message):
    return bool(re.match(
        r'^\s*(?:HTTP(?:/\d(?:\.\d)?)?\s+402(?=\s|:|$)|'
        r'(?:API error:\s*)?(?:insufficient_quota|insufficient_credits)(?=\s|:|$))',
        str(message or ''), re.I,
    ))


def defer_quota(db, job, actor):
    """Recoverable second phase after a fenced failure receipt is committed."""
    from mco.orchestrator.audit import record_event
    from mco.orchestrator.store_context import transaction
    with transaction(db):
        current = (db.table('agent_jobs').select('archived').eq('id', job['id'])
                   .eq('status', 'failed').eq('lease_id', job.get('lease_id')).execute().data or [])
        if not current or current[0].get('archived'):
            return None
        outbox_id = f"quota:{job.get('lease_id')}"
        attempts = db.table('agent_job_events').select('outbox_id').eq('job_id', job['id']).eq(
            'event', _QUOTA_EVENT
        ).limit(_MAX_DEFERRALS).execute().data or []
        if len(attempts) >= _MAX_DEFERRALS and not any(event.get('outbox_id') == outbox_id for event in attempts):
            return None
        # A hosted update is a separate request. Commit evidence first so a
        # crash leaves enough information to replay this same deferral.
        record_event(db, job['id'], _QUOTA_EVENT, actor.get('instance_id'), actor.get('role'),
                     {'org_id': job.get('org_id') or 'default'}, outbox_id=outbox_id)
        update = db.table('agent_jobs').update({
            'status': 'pending', 'leased_by_instance_id': None, 'lease_id': None,
            'lease_expires_at': None, 'completed_at': None,
            'error_message': 'Worker out of quota; waiting for an eligible worker.'
        }).eq('id', job['id']).eq('status', 'failed').eq('lease_id', job.get('lease_id'))
        if current[0].get('archived') is None:
            update = update.is_('archived', 'null')
        else:
            update = update.eq('archived', False)
        rows = update.execute().data or []
    return rows[0] if rows else None


def recover_quota(db, *, now=None):
    cutoff = (now or datetime.now(timezone.utc)) - _RECOVERY_WINDOW
    rows = (db.table('agent_jobs').select('*').eq('status', 'failed')
            .gt('lease_expires_at', cutoff.isoformat()).execute().data or [])
    recovered = []
    for job in rows:
        lease_id = job.get('lease_id')
        worker = job.get('leased_by_instance_id')
        completed = _parse_ts(job.get('completed_at'))
        if not lease_id or not worker or job.get('archived') or (completed and completed <= cutoff):
            continue
        if not quota_error(job.get('error_message')):
            continue
        receipts = db.table('mco_attempt_receipts').select('updates,job').eq('id', lease_id).execute().data or []
        if not receipts or receipts[0]['job'].get('id') != job['id']:
            continue
        updates = receipts[0]['updates']
        if updates.get('status') != 'failed' or updates.get('error_message') != job.get('error_message'):
            continue
        updated = defer_quota(db, job, {'instance_id': worker, 'role': job.get('target_agent_role')})
        if updated:
            recovered.append(updated)
    return recovered


def quota_workers(db, *, org='default', now=None, config=None):
    cfg = config if config is not None else get_config()
    try:
        seconds = max(1, int(cfg.get('MCO_QUOTA_COOLDOWN_SECONDS', 3600)))
    except (ValueError, TypeError):
        seconds = 3600
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=seconds)
    rows = db.table('agent_job_events').select('*').eq('event', _QUOTA_EVENT).gt('created_at', cutoff.isoformat()).execute().data or []
    from mco.localstore import LocalStore

    legacy_orgs = {}
    if isinstance(db, LocalStore):
        # Older embedded audit rows defaulted to the host org. Resolve those
        # from their jobs, while new quota events carry signed tenant evidence.
        legacy_ids = sorted({r['job_id'] for r in rows if r.get('job_id') and not (r.get('detail') or {}).get('org_id')})
        for start in range(0, len(legacy_ids), 100):
            jobs = (db.table('agent_jobs').select('id,org_id').in_('id', legacy_ids[start:start + 100])
                    .execute().data or [])
            legacy_orgs.update({job['id']: job['org_id'] for job in jobs})

    return {r['actor_id'] for r in rows
            if r.get('actor_id') and (_parse_ts(r.get('created_at')) or cutoff) > cutoff
            and ((r.get('detail') or {}).get('org_id') or legacy_orgs.get(r.get('job_id'))
                 or r.get('org_id') or 'default') == org}
