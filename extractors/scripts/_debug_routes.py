import json, io
data = json.loads(io.open('parsed/program_transport.json', encoding='utf-8').read())
by_num = {r['route_number']: r for r in data}
for rn in ['015', '028', '034']:
    r = by_num.get(rn)
    if not r:
        print(rn, '-- MISSING')
        continue
    print('Route', rn, r['from_location'], '->', r['to_location'], '(planned=%d)' % r['planned_trips'])
    for e in r['schedule_entries']:
        print('  dep=%s  arr=%s  back_dep=%s  back_arr=%s  days=%s  school=%s  notes=%s' % (
            e['way_departure'], e['way_arrival'],
            e['back_departure'], e['back_arrival'],
            e['days_of_operation'], e['only_for_school_days'], e['notes']))
    print()
