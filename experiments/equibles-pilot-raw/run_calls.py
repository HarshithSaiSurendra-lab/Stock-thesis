import json, subprocess, sys, os

cells = json.load(open('cells.json'))
helper = '/home/hatch/workspace/custom-connectors/custom.equibles/bin/equibles_api.py'
results = {}
n_calls = 0
for ticker in ['AAPL','ABBV','ABT','ADBE','AEP','AMD','AMGN','AMT','AMZN','AXP']:
    for fy, fq in cells[ticker]:
        fname = f'call-{ticker}-FY{fy}Q{fq}.json'
        if os.path.exists(fname):
            print(f'{ticker} FY{fy}Q{fq}: cached')
            results[(ticker, fy, fq)] = json.load(open(fname))
            continue
        out = subprocess.run(['python3', helper, 'call', '--ticker', ticker, '--fy', str(fy), '--fq', str(fq)],
                             capture_output=True, text=True, timeout=120)
        n_calls += 1
        try:
            payload = json.loads(out.stdout)
        except Exception:
            payload = {'status': 'ERROR', 'raw_stdout': out.stdout[:500], 'raw_stderr': out.stderr[:500]}
        # one retry on transport-level failure (no HTTP status) only
        if payload.get('status') in ('ERROR', None):
            out2 = subprocess.run(['python3', helper, 'call', '--ticker', ticker, '--fy', str(fy), '--fq', str(fq)],
                                  capture_output=True, text=True, timeout=120)
            n_calls += 1
            try:
                payload = json.loads(out2.stdout)
            except Exception:
                payload = {'status': 'ERROR', 'raw_stdout': out2.stdout[:500], 'raw_stderr': out2.stderr[:500]}
        json.dump(payload, open(fname, 'w'), indent=2)
        results[(ticker, fy, fq)] = payload
        st = payload.get('status')
        print(f'{ticker} FY{fy}Q{fq}: status={st}')
print('call requests made in this run:', n_calls)
json.dump({f'{t}|{fy}|{fq}': (v.get('status') if isinstance(v, dict) else str(v)) for (t, fy, fq), v in results.items()},
          open('call_statuses.json', 'w'), indent=2)
