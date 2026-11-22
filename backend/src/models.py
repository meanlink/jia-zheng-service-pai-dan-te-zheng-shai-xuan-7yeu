import json
from enum import StrEnum


class Role(StrEnum):
    INTAKE = 'intake'
    DISPATCHER = 'dispatcher'
    SUPERVISOR = 'supervisor'


class Service(StrEnum):
    DEEP_CLEAN = 'deep_clean'
    APPLIANCE_CLEAN = 'appliance_clean'


class Slot(StrEnum):
    AM = 'am'
    PM = 'pm'
    EVENING = 'evening'


class OrderStatus(StrEnum):
    RECEIVED = 'received'
    ASSIGNED = 'assigned'
    FOLLOWED_UP = 'followed_up'


class PlanStatus(StrEnum):
    QUEUED = 'queued'
    ANALYZED = 'analyzed'
    FAILED = 'failed'
    TRIAL = 'trial'
    REVIEW_PRE = 'review_pre'
    REVIEWED = 'reviewed'
    CONFIRMED = 'confirmed'
    REVIEW_POST = 'review_post'
    CLOSED = 'closed'


# Plans before dispatch confirmation. Scope approvals and qualification changes
# invalidate these and requeue them; confirmed plans keep their original prediction.
PRE_CONFIRM_STATUSES = ('queued', 'analyzed', 'failed', 'trial', 'review_pre', 'reviewed')


ORDER_FIELDS = '''id, code, customer, neighborhood, service, service_date, slot,
    area_m2, dirt_level, obstacles, supplies, baseline_minutes, status'''
TECH_FIELDS = 'id, code, name, skill, certificate, valid_through, experience_years, active'
PLAN_FIELDS = '''id, order_id, technician_id, status, analysis, error_message,
    predicted_minutes, deviation_limit, pre_review_note, confirm_by, actual_minutes,
    followup_note, post_review_note, batch_id, scope_snapshot, invalidation_reason, updated_at'''


def plan_row(row):
    if row and row['analysis'] is not None:
        row['analysis'] = json.loads(row['analysis'])
    if row and row.get('scope_snapshot') is not None:
        row['scope_snapshot'] = json.loads(row['scope_snapshot'])
    return row
