import json

from fastapi import HTTPException

from .access import require_access
from .batches import pin_batch
from .handover import require_arrival
from .scope import current_scope, require_decision
from .db import audit, today, transaction
from .feature_model import ModelUnavailable, estimate
from .scheduling import require_available, require_resolved_contact
from .models import (
    ORDER_FIELDS, PLAN_FIELDS, PRE_CONFIRM_STATUSES, TECH_FIELDS, OrderStatus, PlanStatus, plan_row,
)


def get_order(cur, order_id, *, lock=False):
    suffix = ' FOR UPDATE' if lock else ''
    cur.execute(f'SELECT {ORDER_FIELDS} FROM service_orders WHERE id=%s{suffix}', (order_id,))
    order = cur.fetchone()
    if not order:
        raise HTTPException(404, '服务订单不存在')
    return order


def get_technician(cur, technician_id, *, lock=False):
    suffix = ' FOR UPDATE' if lock else ''
    cur.execute(f'SELECT {TECH_FIELDS} FROM technicians WHERE id=%s{suffix}', (technician_id,))
    technician = cur.fetchone()
    if not technician:
        raise HTTPException(404, '技师不存在')
    return technician


def require_qualification(order, technician):
    if not technician['active']:
        raise HTTPException(409, '技师已停用')
    if technician['skill'] != order['service']:
        raise HTTPException(409, '技师技能与订单服务品类不匹配')
    if technician['valid_through'] < max(today(), order['service_date']):
        raise HTTPException(409, '技能资质已过期或不能覆盖预约日，禁止参与试算')
    if order['service_date'] < today():
        raise HTTPException(409, '预约日已过，无法进行派单')


def get_plan(cur, plan_id, *, lock=False):
    suffix = ' FOR UPDATE' if lock else ''
    cur.execute(f'SELECT {PLAN_FIELDS} FROM dispatch_plans WHERE id=%s{suffix}', (plan_id,))
    plan = plan_row(cur.fetchone())
    if not plan:
        raise HTTPException(404, '派单方案不存在')
    return plan


def plans():
    fields = ','.join(f'p.{name.strip()}' for name in PLAN_FIELDS.split(','))
    with transaction() as cur:
        cur.execute(f'''SELECT {fields},b.sample_count AS batch_sample_count,b.checksum AS batch_checksum,
          b.created_at AS batch_pinned_at FROM dispatch_plans p
          LEFT JOIN analysis_batches b ON b.id=p.batch_id
          ORDER BY (p.status IN ('closed','failed')),p.updated_at DESC,p.id DESC LIMIT 150''')
        return [plan_row(row) for row in cur.fetchall()]


def invalidate_unconfirmed(cur, order, reason, actor):
    """Fail the order's unconfirmed plan and requeue it on the originally pinned batch.

    The prior analysis stays on the row so the worker can skip republishing an
    identical result; confirmed plans are never touched and keep their prediction.
    """
    cur.execute('SELECT id,status FROM dispatch_plans WHERE order_id=%s FOR UPDATE', (order['id'],))
    plan = cur.fetchone()
    if not plan or plan['status'] not in PRE_CONFIRM_STATUSES:
        return False
    cur.execute('''UPDATE dispatch_plans SET status='queued',predicted_minutes=NULL,pre_review_note=NULL,
                   error_message=NULL,invalidation_reason=%s,scope_snapshot=%s WHERE id=%s''',
                (reason, json.dumps(current_scope(order)), plan['id']))
    audit(cur, actor, 'analysis_invalidated', order['code'], plan_id=plan['id'], reason=reason)
    return True


def require_dispatch_readiness(cur, order_id):
    require_resolved_contact(cur, order_id)
    require_decision(cur, order_id)
    require_access(cur, order_id)


def queue_analysis(order_id, technician_id, actor):
    with transaction() as cur:
        order = get_order(cur, order_id, lock=True)
        if order['status'] != OrderStatus.RECEIVED:
            raise HTTPException(409, '订单已派单，不能重新分析')
        cur.execute('SELECT id,status FROM dispatch_plans WHERE order_id=%s FOR UPDATE', (order_id,))
        prior = cur.fetchone()
        if prior and prior['status'] == PlanStatus.QUEUED:
            raise HTTPException(409, '该订单已有分析排队')
        technician = get_technician(cur, technician_id, lock=True)
        require_qualification(order, technician)
        require_available(cur, technician, order)
        require_dispatch_readiness(cur, order['id'])
        # Pin the training basis at initiation: closed completions as of now plus the
        # current scope. Follow-ups closed later only enter batches pinned afterwards.
        batch = pin_batch(cur, order)
        snapshot = json.dumps(current_scope(order))
        if prior:
            plan_id = prior['id']
            cur.execute('''UPDATE dispatch_plans SET status='queued',technician_id=%s,batch_id=%s,
              scope_snapshot=%s,predicted_minutes=NULL,error_message=NULL,pre_review_note=NULL
              WHERE id=%s''', (technician_id, batch['id'], snapshot, plan_id))
        else:
            cur.execute('''INSERT INTO dispatch_plans(order_id,technician_id,status,batch_id,scope_snapshot)
              VALUES (%s,%s,'queued',%s,%s)''', (order_id, technician_id, batch['id'], snapshot))
            plan_id = cur.lastrowid
        audit(cur, actor, 'analysis_queued', order['code'], plan_id=plan_id, technician=technician['code'],
              batch=batch['id'], samples=batch['sample_count'])
        return get_plan(cur, plan_id)


def lock_plan_and_order(cur, plan_id):
    order = get_order(cur, get_plan(cur, plan_id)['order_id'], lock=True)
    return get_plan(cur, plan_id, lock=True), order


def require_status(plan, *states):
    if plan['status'] not in states:
        raise HTTPException(409, '当前方案状态不允许该操作')


def run_trial(plan_id, actor):
    with transaction() as cur:
        plan, order = lock_plan_and_order(cur, plan_id)
        require_status(plan, PlanStatus.ANALYZED)
        technician = get_technician(cur, plan['technician_id'], lock=True)
        require_qualification(order, technician)
        require_available(cur, technician, order)
        require_dispatch_readiness(cur, order['id'])
        if technician['certificate'] != plan['analysis']['certificate']:
            raise HTTPException(409, '证书已更换，请使用新证书重新分析')
        try:
            prediction = estimate(plan['analysis'])
        except ModelUnavailable as exc:
            raise HTTPException(422, str(exc)) from exc
        deviation = prediction - order['baseline_minutes']
        status = PlanStatus.REVIEW_PRE if abs(deviation) > plan['deviation_limit'] else PlanStatus.TRIAL
        cur.execute('UPDATE dispatch_plans SET predicted_minutes=%s,status=%s WHERE id=%s',
                    (prediction, status, plan_id))
        audit(cur, actor, 'dispatch_trial', order['code'], plan_id=plan_id, prediction=prediction,
              deviation=deviation, status=status)
        return get_plan(cur, plan_id)


def confirm(plan_id, actor):
    with transaction() as cur:
        plan, order = lock_plan_and_order(cur, plan_id)
        require_status(plan, PlanStatus.TRIAL, PlanStatus.REVIEWED)
        technician = get_technician(cur, plan['technician_id'], lock=True)
        require_qualification(order, technician)
        require_available(cur, technician, order)
        require_dispatch_readiness(cur, order['id'])
        if technician['certificate'] != plan['analysis']['certificate']:
            raise HTTPException(409, '试算证书已更换，请重新分析')
        # The technician row serializes competing reservations. READ COMMITTED sees the last winner.
        cur.execute('''SELECT o.code FROM dispatch_plans p JOIN service_orders o ON o.id=p.order_id
          WHERE p.technician_id=%s AND o.status IN ('assigned','followed_up') AND o.service_date=%s
            AND o.slot=%s AND o.id<>%s LIMIT 1''', (technician['id'], order['service_date'], order['slot'], order['id']))
        if conflict := cur.fetchone():
            raise HTTPException(409, f"技师该时段已确认工单 {conflict['code']}，请另选技师")
        cur.execute("UPDATE dispatch_plans SET status='confirmed',confirm_by=%s,experience_at_assignment=%s WHERE id=%s",
                    (actor, technician['experience_years'], plan_id))
        cur.execute("UPDATE service_orders SET status='assigned' WHERE id=%s", (order['id'],))
        audit(cur, actor, 'dispatch_confirmed', order['code'], plan_id=plan_id, technician=technician['code'])
        return get_plan(cur, plan_id)


def record_followup(plan_id, data, actor):
    with transaction() as cur:
        plan, order = lock_plan_and_order(cur, plan_id)
        require_status(plan, PlanStatus.CONFIRMED)
        if order['service_date'] > today():
            raise HTTPException(409, '预约日期尚未到达，无法记录完工回访')
        require_arrival(cur, plan_id)
        deviation = data.actual_minutes - plan['predicted_minutes']
        status = PlanStatus.REVIEW_POST if abs(deviation) > plan['deviation_limit'] else PlanStatus.CLOSED
        cur.execute('UPDATE dispatch_plans SET status=%s,actual_minutes=%s,followup_note=%s WHERE id=%s',
                    (status, data.actual_minutes, data.note, plan_id))
        cur.execute("UPDATE service_orders SET status='followed_up' WHERE id=%s", (order['id'],))
        audit(cur, actor, 'followup_recorded', order['code'], plan_id=plan_id, deviation=deviation, status=status)
        return get_plan(cur, plan_id)


def approve_review(plan_id, note, actor):
    with transaction() as cur:
        plan, order = lock_plan_and_order(cur, plan_id)
        require_status(plan, PlanStatus.REVIEW_PRE, PlanStatus.REVIEW_POST)
        if plan['status'] == PlanStatus.REVIEW_PRE:
            cur.execute("UPDATE dispatch_plans SET status='reviewed',pre_review_note=%s WHERE id=%s", (note, plan_id))
            event = 'pre_dispatch_reviewed'
        else:
            cur.execute("UPDATE dispatch_plans SET status='closed',post_review_note=%s WHERE id=%s", (note, plan_id))
            event = 'post_followup_reviewed'
        audit(cur, actor, event, order['code'], plan_id=plan_id, note=note)
        return get_plan(cur, plan_id)
