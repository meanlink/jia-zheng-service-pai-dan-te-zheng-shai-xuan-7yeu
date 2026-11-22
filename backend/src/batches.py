import json

from .feature_model import ModelUnavailable, fingerprint


def pin_batch(cur, order):
    """Snapshot the closed completions eligible for this order as a reusable sample batch.

    Rows are stored oldest-first, matching the order fit() expects. Identical history
    reuses the existing batch row, so reruns and parallel orders share one sample version.
    """
    cur.execute('''SELECT o.id,o.service_date,o.area_m2,o.dirt_level,o.obstacles,o.supplies,
      p.experience_at_assignment AS experience_years,p.actual_minutes
      FROM service_orders o JOIN dispatch_plans p ON p.order_id=o.id
      WHERE o.service=%s AND o.status='followed_up' AND p.status='closed' AND p.actual_minutes IS NOT NULL
        AND p.experience_at_assignment IS NOT NULL
        AND o.service_date<%s ORDER BY o.service_date DESC,o.id DESC LIMIT 2000''',
                (order['service'], order['service_date']))
    rows = [{'id': row['id'], 'service_date': str(row['service_date']),
             'area_m2': row['area_m2'], 'dirt_level': row['dirt_level'], 'obstacles': row['obstacles'],
             'supplies': bool(row['supplies']), 'experience_years': row['experience_years'],
             'actual_minutes': row['actual_minutes']}
            for row in reversed(cur.fetchall())]
    checksum = fingerprint(rows)
    cur.execute('''INSERT INTO analysis_batches(service,sample_count,checksum,sample_rows)
                   VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE id=id''',
                (order['service'], len(rows), checksum, json.dumps(rows, separators=(',', ':'))))
    cur.execute('SELECT id,sample_count,checksum,created_at FROM analysis_batches WHERE service=%s AND checksum=%s',
                (order['service'], checksum))
    return cur.fetchone()


def load_batch(cur, batch_id):
    cur.execute('SELECT sample_rows FROM analysis_batches WHERE id=%s', (batch_id,))
    row = cur.fetchone()
    if not row:
        raise ModelUnavailable('样本批次缺失，请重新提交特征分析')
    return json.loads(row['sample_rows'])
