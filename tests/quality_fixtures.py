"""Legacy rows for cutover compatibility tests; new writes belong to probe_store."""
def pair_record(c, pair_id, slot, status, rtt=None, error='', resolved_ip=''):
    c.execute('INSERT OR IGNORE INTO quality_samples VALUES (?,?,?,?,?,?)',
              (pair_id, slot, status, rtt, error, resolved_ip))


def link_record(c, spec, slot, status, rtt=None, error='', resolved_ip=''):
    c.execute('INSERT OR IGNORE INTO link_quality_samples VALUES (?,?,?,?,?,?,?,?,?,?)',
              (spec['link_id'], spec['section'], spec['protocol'], slot, status, rtt,
               error, resolved_ip, spec['path_key'], spec['path_label']))
