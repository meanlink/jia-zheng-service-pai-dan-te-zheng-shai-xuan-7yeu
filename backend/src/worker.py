import json
import logging
import time
from pathlib import Path

from fastapi import HTTPException

from .batches import load_batch, pin_batch
from .db import audit, cache, transaction
from .dispatch import get_order, get_technician, require_qualification
from .feature_model import ModelUnavailable, fit
from .scope import current_scope

HEARTBEAT = Path('/run/dispatch/worker')
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('dispatch-worker')


def consume():
    with transaction() as cur:
        cur.execute("SELECT id,order_id FROM dispatch_plans WHERE status='queued' ORDER BY id LIMIT 1")
        job = cur.fetchone()
        if not job:
            return False
        # Match the order-first lock order used by dispatch and appointment edits.
        order = get_order(cur, job['order_id'], lock=True)
        cur.execute('''SELECT technician_id,status,batch_id,scope_snapshot,analysis FROM dispatch_plans
          WHERE id=%s FOR UPDATE''', (job['id'],))
        locked = cur.fetchone()
        if not locked or locked['status'] != 'queued':
            return False
        technician = get_technician(cur, locked['technician_id'])
        try:
            require_qualification(order, technician)
            if locked['batch_id'] is None:
                # Plans queued before batch pinning existed get their batch pinned now.
                batch = pin_batch(cur, order)
                locked['batch_id'] = batch['id']
                locked['scope_snapshot'] = json.dumps(current_scope(order))
                cur.execute('UPDATE dispatch_plans SET batch_id=%s,scope_snapshot=%s WHERE id=%s',
                            (batch['id'], locked['scope_snapshot'], job['id']))
            history = load_batch(cur, locked['batch_id'])
            scope = json.loads(locked['scope_snapshot']) if locked['scope_snapshot'] else {}
            analysis = fit(history, {**order, **scope}, technician)
        except (ModelUnavailable, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            cur.execute("UPDATE dispatch_plans SET status='failed',analysis=NULL,error_message=%s WHERE id=%s",
                        (detail, job['id']))
            audit(cur, 'worker', 'analysis_failed', order['code'], plan_id=job['id'], detail=detail)
        else:
            previous = json.loads(locked['analysis']) if locked['analysis'] else None
            if previous == analysis:
                # Rerun on the original batch with unchanged inputs: keep the existing
                # publication instead of issuing a duplicate model version.
                cur.execute("UPDATE dispatch_plans SET status='analyzed' WHERE id=%s", (job['id'],))
                audit(cur, 'worker', 'analysis_rerun', order['code'], plan_id=job['id'],
                      evidence=analysis['history_checksum'], batch=locked['batch_id'])
            else:
                cur.execute("UPDATE dispatch_plans SET status='analyzed',analysis=%s WHERE id=%s",
                            (json.dumps(analysis), job['id']))
                audit(cur, 'worker', 'analysis_ready', order['code'], plan_id=job['id'],
                      evidence=analysis['history_checksum'], batch=locked['batch_id'],
                      selected=[f['key'] for f in analysis['features'] if f['selected']])
        logger.info('Processed order %s, plan #%s', order['code'], job['id'])
        return True


def main():
    while True:
        try:
            cache.ping()
            worked = consume()
            HEARTBEAT.touch()
        except Exception:
            logger.exception('Queue iteration failed; current transaction rolled back')
            worked = False
        if not worked:
            time.sleep(1)


if __name__ == '__main__':
    main()
