from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from mco.localstore import LocalStore
from mco.orchestrator import routes, delivery, presence
from mco.orchestrator.handlers import handle_job_update
from mco.orchestrator.leases import acquire_lease


@pytest.fixture
def db(tmp_path):
    store = LocalStore(tmp_path / 'reliability.db')
    yield store
    store.close()


@pytest.fixture(autouse=True)
def approvers(monkeypatch):
    monkeypatch.setattr(routes.utils_mod, 'get_approver_roles', lambda: {'owner', 'admin'})


def job(db, id='job', **kw):
    row = {'id': id, 'org_id': 'default', 'status': 'pending',
           'created_at': (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
           'target_agent_role': 'grok', 'target_agent_id': 'grok-renderer-a', **kw}
    db.table('agent_jobs').insert(row).execute()
    return row


def row(db, id='job'):
    return db.table('agent_jobs').select('*').eq('id', id).execute().data[0]


def test_expiry_skips_live_parent_and_score_graphs(db):
    from mco.orchestrator.stale import expire_waiting
    job(db, 'failed', status='failed')
    job(db, 'active', status='in_progress', leased_by_instance_id='grok-renderer-a')
    job(db, 'dead', status='waiting', depends_on=['failed'])
    job(db, 'missing', status='waiting', depends_on=['absent'])
    job(db, 'live', status='waiting', depends_on=['active'])
    job(db, 'score', status='waiting', depends_on=['failed'], input_payload={'score': {'protocol': 'score-v1'}})
    assert {j['id'] for j in expire_waiting(db)} == {'dead', 'missing'}
    assert row(db, 'dead')['status'] == 'halted'
    assert row(db, 'live')['status'] == row(db, 'score')['status'] == 'waiting'
    assert expire_waiting(db) == []


@pytest.mark.asyncio
async def test_cancel_stale_requires_recent_preview_and_approver(db, monkeypatch):
    job(db)
    monkeypatch.setattr(routes, 'get_db_client', lambda: db)
    owner = {'instance_id': 'owner', 'role': 'owner', 'org_id': 'default'}
    with pytest.raises(HTTPException) as e:
        await routes.cancel_stale_jobs({}, {'instance_id': 'worker', 'role': 'grok'})
    assert e.value.status_code == 403
    with pytest.raises(HTTPException):
        await routes.cancel_stale_jobs({'dry_run': False}, owner)
    preview = await routes.cancel_stale_jobs({}, owner)
    assert [j['id'] for j in preview['jobs']] == ['job']
    assert row(db)['status'] == 'pending'
    # A newly leased job must survive a previously approved stale preview.
    acquire_lease(db, 'job', 'grok-renderer-a')
    result = await routes.cancel_stale_jobs({'dry_run': False, 'preview_id': preview['preview_id']}, owner)
    assert result['cancelled'] == []
    assert row(db)['status'] == 'leased'


@pytest.mark.asyncio
async def test_cancel_stale_only_previewed_org_and_single_use(db, monkeypatch):
    job(db)
    job(db, 'other', org_id='other')
    monkeypatch.setattr(routes, 'get_db_client', lambda: db)
    owner = {'instance_id': 'owner', 'role': 'owner', 'org_id': 'default'}
    preview = await routes.cancel_stale_jobs({}, owner)
    job(db, 'later')
    result = await routes.cancel_stale_jobs({'dry_run': False, 'preview_id': preview['preview_id']}, owner)
    assert result['cancelled'] == ['job']
    assert row(db, 'other')['status'] == row(db, 'later')['status'] == 'pending'
    with pytest.raises(HTTPException):
        await routes.cancel_stale_jobs({'dry_run': False, 'preview_id': preview['preview_id']}, owner)


def test_capability_constraints_prevent_unsafe_fallback(db):
    job(db, input_payload={'routing': {'required_capabilities': ['gpu-render'], 'allowed_roles': ['grok']}})
    cfg = {'MCO_ROUTE_FALLBACKS': 'grok:claude', 'MCO_DELIVERY_STALL_SECONDS': '10'}
    delivery.sweep(db, config=cfg)
    result = delivery.sweep(db, config=cfg, online_instances=lambda *_: [('claude', 'claude-renderer-a')])
    assert result.rerouted == []
    assert row(db)['target_agent_role'] == 'grok'


def test_capability_match_is_per_instance_and_admission_uses_it(db, monkeypatch):
    from mco.orchestrator.worker_policy import can_run
    payload = {'routing': {'required_capabilities': ['gpu-render']}}
    j = job(db, input_payload=payload)
    cfg = {'MCO_WORKER_CAPABILITIES': '{"grok-renderer-a":["gpu-render"]}'}
    assert can_run(j, 'grok', 'grok-renderer-a', cfg)
    assert not can_run(j, 'claude', 'claude-renderer-a', cfg)
    monkeypatch.setattr(routes, 'get_config', lambda: {})
    assert routes._pending_for_agent(db, 'grok', 'grok-renderer-a', {'org_id': 'default'}) == []


@pytest.mark.asyncio
async def test_quota_failure_defers_job_without_retry_or_failure_escalation(db):
    job(db, max_retries=0)
    proof = acquire_lease(db, 'job', 'grok-renderer-a')
    err, ack, broadcast = AsyncMock(), AsyncMock(), AsyncMock()
    payload = {'task_id': 'job', 'status': 'failed', 'error_message': 'HTTP 402: out of credits', **proof.as_claim()}
    await handle_job_update(db, payload, 'quota', err, ack, broadcast, actor={'instance_id': 'grok-renderer-a', 'role': 'grok'})
    err.assert_not_called()
    assert row(db)['status'] == 'pending'
    assert not row(db).get('retry_count')
    assert not row(db).get('lease_id')
    worker = {'instance_id': 'grok-renderer-a', 'role': 'grok', 'status': 'online'}
    assert presence.describe_fleet(db, [worker], threshold=300, connected={'grok-renderer-a'}, now=datetime.now(timezone.utc))[0]['state'] == 'out_of_quota'
    assert routes._pending_for_agent(db, 'grok', 'grok-renderer-a', {'org_id': 'default'}) == []
    await handle_job_update(db, payload, 'quota', err, ack, broadcast, actor={'instance_id': 'grok-renderer-a', 'role': 'grok'})
    err.assert_not_called()
    assert row(db)['status'] == 'pending'


def test_untaken_work_does_not_prove_broken(db):
    job(db)
    a = {'instance_id': 'grok-renderer-a', 'role': 'grok', 'status': 'online'}
    r = presence.describe_fleet(db, [a], threshold=300, connected={'grok-renderer-a'}, now=datetime.now(timezone.utc))[0]
    assert r['state'] == 'standby'
    assert 'untaken' in r['state_reason']


def test_board_limit_applies_after_archived_filter(db, monkeypatch):
    job(db, 'active')
    for i in range(3):
        job(db, str(i), archived=True, created_at=datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(routes, 'get_db_client', lambda: db)
    assert [j['id'] for j in routes.get_jobs(limit=2, agent={'org_id': 'default'})] == ['active']


def test_wake_single_instance_lock_releases_after_owner_exits(tmp_path):
    from mco.waker import worker_lock
    from filelock import Timeout
    with worker_lock('grok', 'grok-renderer-a', directory=tmp_path):
        with pytest.raises(Timeout):
            with worker_lock('grok', 'grok-renderer-a', directory=tmp_path):
                pass
        with worker_lock('grok', 'grok-renderer-b', directory=tmp_path):
            pass
    with worker_lock('grok', 'grok-renderer-a', directory=tmp_path):
        pass


def test_actual_worker_fault_is_broken(db):
    a = {'instance_id': 'grok-renderer-a', 'role': 'grok', 'status': 'broken'}
    assert presence.describe_fleet(db, [a], threshold=300, connected={'grok-renderer-a'}, now=datetime.now(timezone.utc))[0]['state'] == 'broken'


def test_quota_has_plain_helper_word():
    from mco.helpers import health, doing
    h = {'instance_id': 'grok-renderer-a', 'state': 'out_of_quota'}
    assert health(h)['word'] == 'Out of quota'
    assert 'quota' in doing(h)


def test_console_paginates_and_has_preview_before_cleanup():
    from pathlib import Path
    root = Path(__file__).parents[1] / 'src/mco/console_src'
    store = (root / '47e66145-9c4d-41a1-acbb-42b12848f160.js').read_text(encoding='utf-8')
    board = (root / '5adac14f-6645-4e02-866b-22c4e571989b.js').read_text(encoding='utf-8')
    assert 'api("/api/jobs?compact=true")' in store and '/api/jobs?limit=200' not in store
    assert '/api/jobs/cancel-stale' in store
    assert 'Preview stale jobs' in board and 'Cancel stale jobs' in board
    assert 'preview_id' in store


def test_board_pages_storage_without_losing_old_active_work(db, monkeypatch):
    for i in range(505):
        job(db, f'j{i:04}', archived=i < 501)
    monkeypatch.setattr(routes, 'get_db_client', lambda: db)
    all_rows = routes.get_jobs(agent={'org_id': 'default'})
    assert len(all_rows) == 4
    assert routes.get_jobs(limit=2, offset=2, agent={'org_id': 'default'}) == all_rows[2:4]


def test_expired_dependencies_stay_available_for_reviewed_cleanup(db):
    from mco.orchestrator.stale import expire_waiting, stale_candidates
    job(db, status='waiting', depends_on=['missing'])
    expire_waiting(db)
    assert [j['id'] for j in stale_candidates(db, org='default')] == ['job']


def test_capable_fallback_is_pinned_to_the_matching_instance(db):
    job(db, input_payload={'routing': {'required_capabilities': ['gpu-render']}})
    cfg = {'MCO_ROUTE_FALLBACKS': 'grok:claude', 'MCO_DELIVERY_STALL_SECONDS': '10',
           'MCO_WORKER_CAPABILITIES': '{"claude-renderer-a":["gpu-render"]}'}
    delivery.sweep(db, config=cfg)
    result = delivery.sweep(db, config=cfg, online_instances=lambda *_: [('claude', 'claude-renderer-a')])
    assert result.rerouted == ['job']
    assert row(db)['target_agent_id'] == 'claude-renderer-a'


def test_malformed_requirements_fail_closed_for_unpinned_fallback(db):
    job(db, target_agent_id=None, input_payload={'routing': []})
    cfg = {'MCO_ROUTE_FALLBACKS': 'grok:claude', 'MCO_DELIVERY_STALL_SECONDS': '10'}
    delivery.sweep(db, config=cfg)
    result = delivery.sweep(db, config=cfg, online_roles=lambda *_: {'claude'},
                            online_instances=lambda *_: [('claude', 'claude-renderer-a')])
    assert result.rerouted == []


def test_quota_cooldown_expires_automatically(db):
    from mco.orchestrator.audit import record_event
    from mco.orchestrator.worker_policy import quota_workers
    record_event(db, 'job', 'worker_out_of_quota', 'grok-renderer-a', 'grok')
    assert 'grok-renderer-a' in quota_workers(db)
    assert 'grok-renderer-a' not in quota_workers(db, now=datetime.now(timezone.utc) + timedelta(hours=2))


def test_maintenance_recovers_interrupted_quota_deferral(db):
    from mco.orchestrator.leases import fenced_update
    job(db)
    proof = acquire_lease(db, 'job', 'grok-renderer-a')
    fenced_update(db, 'job', proof.as_claim(), {'status': 'failed', 'error_message': 'HTTP 402 out of credits'})
    delivery.sweep(db, config={})
    assert row(db)['status'] == 'pending'
    assert not row(db).get('lease_id')


@pytest.mark.asyncio
async def test_unfenced_quota_report_cannot_block_worker(db):
    from mco.orchestrator.worker_policy import quota_workers
    job(db)
    acquire_lease(db, 'job', 'grok-renderer-a')
    err, ack, broadcast = AsyncMock(), AsyncMock(), AsyncMock()
    await handle_job_update(db, {'task_id': 'job', 'status': 'failed', 'error_message': '402'},
                            'quota', err, ack, broadcast, actor={'instance_id': 'grok-renderer-a', 'role': 'grok'})
    err.assert_called_once()
    assert row(db)['status'] == 'leased'
    assert quota_workers(db) == set()


@pytest.mark.asyncio
async def test_direct_lease_cannot_bypass_capabilities(db, monkeypatch):
    job(db, input_payload={'routing': {'required_capabilities': ['gpu-render']}})
    monkeypatch.setattr(routes, 'get_db_client', lambda: db)
    monkeypatch.setattr(routes, 'get_config', lambda: {})
    monkeypatch.setattr(routes, 'touch_agent_presence', lambda *_: None)
    monkeypatch.setattr(routes, 'reclaim_stale_leases', lambda *_: None)
    with pytest.raises(HTTPException) as e:
        await routes.lease_job({'task_id': 'job', 'agent_instance_id': 'grok-renderer-a'}, {'instance_id': 'grok-renderer-a', 'role': 'grok', 'org_id': 'default'})
    assert e.value.status_code == 403

@pytest.mark.parametrize("message", ["AssertionError: tests/test_x.py:402", "PR #402 failed", "write failed: disk quota exceeded"])
def test_unrelated_failures_are_not_quota(message):
    from mco.orchestrator.worker_policy import quota_error
    assert not quota_error(message)


@pytest.mark.parametrize("archived,age", [(False, 90), (True, 0)])
def test_sweep_does_not_revive_historical_or_archived_failures(db, archived, age):
    completed = datetime.now(timezone.utc) - timedelta(days=age)
    job(db, status='failed', lease_id='old-attempt', leased_by_instance_id='grok-renderer-a',
        completed_at=completed.isoformat(), archived=archived, error_message='HTTP 402 out of credits')
    delivery.sweep(db, config={})
    assert row(db)['status'] == 'failed'


def test_quota_deferrals_have_a_per_job_cap(db):
    from mco.orchestrator.worker_policy import defer_quota
    actor = {'instance_id': 'grok-renderer-a', 'role': 'grok'}
    job(db, status='failed', lease_id='attempt-1', leased_by_instance_id=actor['instance_id'])
    assert defer_quota(db, row(db), actor)
    db.table('agent_jobs').update({'status': 'failed', 'lease_id': 'attempt-2'}).eq('id', 'job').execute()
    assert defer_quota(db, row(db), actor)
    db.table('agent_jobs').update({'status': 'failed', 'lease_id': 'attempt-3'}).eq('id', 'job').execute()
    assert defer_quota(db, row(db), actor) is None
    assert row(db)['status'] == 'failed'


@pytest.mark.asyncio
async def test_old_pending_parent_can_still_release_its_child(db):
    from mco.orchestrator.handlers import _unlock_dependents
    from mco.orchestrator.stale import expire_waiting
    job(db, 'parent')
    job(db, 'child', status='waiting', depends_on=['parent'])
    assert expire_waiting(db) == []
    db.table('agent_jobs').update({'status': 'completed'}).eq('id', 'parent').execute()
    await _unlock_dependents(db, 'parent', AsyncMock())
    assert row(db, 'child')['status'] == 'pending'


def test_quota_cooldown_is_tenant_local(db):
    from mco.orchestrator.worker_policy import defer_quota
    job(db, 'other-failure', org_id='other', status='failed', lease_id='other-attempt',
        leased_by_instance_id='grok-renderer-a')
    job(db)
    defer_quota(db, row(db, 'other-failure'), {'instance_id': 'grok-renderer-a', 'role': 'grok'})
    assert [item['id'] for item in routes._pending_for_agent(db, 'grok', 'grok-renderer-a', {'org_id': 'default'})] == ['job']


def test_recent_reroute_is_evidence_of_worker_unavailability(db):
    from mco.orchestrator.audit import record_event
    job(db, 'rerouted', target_agent_id='grok-renderer-b')
    record_event(db, 'rerouted', delivery.REROUTED, 'delivery', 'system', {'from_instance': 'grok-renderer-a'})
    agent = {'instance_id': 'grok-renderer-a', 'role': 'grok', 'status': 'online'}
    fleet = presence.describe_fleet(db, [agent], threshold=300, connected={'grok-renderer-a'}, now=datetime.now(timezone.utc))
    assert fleet[0]['state'] == 'standby'
    assert fleet[0]['routing_available'] is False
    assert presence.available_roles(fleet) == set()


def test_full_console_board_omits_terminal_outputs_and_limited_reads_window_storage(db, monkeypatch):
    job(db, 'latest', status='completed', output_payload={'result': 'large'},
        created_at=datetime.now(timezone.utc).isoformat())
    job(db, 'older', status='completed', output_payload={'result': 'large'})
    monkeypatch.setattr(routes, 'get_db_client', lambda: db)
    queries = []
    run = db._run
    def capture(query):
        if query._table == 'agent_jobs':
            queries.append(query)
        return run(query)
    monkeypatch.setattr(db, '_run', capture)
    assert [j['id'] for j in routes.get_jobs(limit=1, agent={'org_id': 'default'})] == ['latest']
    assert queries[0]._limit == 1
    compact = routes.get_jobs(agent={'org_id': 'default'}, compact=True)
    assert {j['id'] for j in compact} == {'older', 'latest'}
    assert all('output_payload' not in j for j in compact)
    assert 'output_payload' not in queries[-1]._columns


def test_bundled_console_never_reparses_dom_text_as_html():
    from pathlib import Path
    html = (Path(__file__).parents[1] / 'src/mco/static/console.html').read_text(encoding='utf-8')
    assert 'parseFromString(' not in html
    assert '<template id="__bundler_head">' in html
    assert '<template id="__bundler_body">' in html


def test_board_storage_windows_keep_the_full_order_on_timestamp_ties(db, monkeypatch):
    timestamp = datetime.now(timezone.utc).isoformat()
    for job_id in ('c', 'a', 'b'):
        job(db, job_id, created_at=timestamp)
    monkeypatch.setattr(routes, 'get_db_client', lambda: db)
    agent = {'org_id': 'default'}
    full = routes.get_jobs(agent=agent)
    assert [item['id'] for item in full] == ['a', 'b', 'c']
    assert routes.get_jobs(limit=1, offset=1, agent=agent) == full[1:2]


def test_legacy_cooldown_uses_embedded_job_org(db):
    from mco.orchestrator.audit import record_event
    from mco.orchestrator.worker_policy import quota_workers
    job(db, org_id='other')
    record_event(db, 'job', 'worker_out_of_quota', 'grok-renderer-a', 'grok')
    assert quota_workers(db) == set()
    assert quota_workers(db, org='other') == {'grok-renderer-a'}


def test_repeatedly_untaken_pinned_work_is_unavailable_even_without_a_fallback(db):
    job(db)
    config = {'MCO_DELIVERY_STALL_SECONDS': '10'}
    delivery.sweep(db, config=config)
    assert delivery.sweep(db, config=config).escalated == ['job']
    agent = {'instance_id': 'grok-renderer-a', 'role': 'grok', 'status': 'online'}
    fleet = presence.describe_fleet(db, [agent], threshold=300, connected={'grok-renderer-a'},
                                    now=datetime.now(timezone.utc), stall_seconds=10)
    assert fleet[0]['routing_available'] is False
    assert presence.available_roles(fleet) == set()
