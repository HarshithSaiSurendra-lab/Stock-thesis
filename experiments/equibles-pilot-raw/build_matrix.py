import json, glob

tickers = ['AAPL','ABBV','ABT','ADBE','AEP','AMD','AMGN','AMT','AMZN','AXP']
cells_cfg = json.load(open('cells.json'))

# ranking info
import os
m = json.load(open('/home/hatch/workspace/stock-thesis/data/edgar_excerpts/manifest.json'))
pt = m['coverage']['per_ticker']
ranked = sorted(pt.items(), key=lambda kv: (-kv[1], kv[0]))
top10 = [t for t, _ in ranked[:10]]

# transcript-missing reasons from events rows
reasons = {}
for t in tickers:
    d = json.load(open(f'events-{t}.json'))
    for r in d['result']['data']:
        if r.get('eventType') == 'EarningsCall':
            key = (t, r['fiscalYear'], r['fiscalQuarter'])
            mat = r.get('materials', {}).get('transcript', {})
            reasons[key] = {'status': mat.get('status'), 'reason': mat.get('reason')}

matrix = []
for t in tickers:
    for fy, fq in cells_cfg[t]:
        c = json.load(open(f'call-{t}-FY{fy}Q{fq}.json'))
        assert c['status'] == 200, (t, fy, fq, c['status'])
        r = c['result']
        key = (t, fy, fq)
        matrix.append({
            'ticker': t,
            'fiscalYear': fy,
            'fiscalQuarter': fq,
            'callDate': (r.get('callDate') or '')[:10],
            'status': r.get('status'),
            'hasTranscript': bool(r.get('hasTranscript')),
            'hasAudio': bool(r.get('hasAudio')),
            'hasSlides': bool(r.get('hasSlides')),
            'guidanceStatus': r.get('guidanceStatus'),
            'hasEarningsRelease': r.get('earningsReleaseDocumentId') is not None,
            'transcriptMaterialStatus': reasons.get(key, {}).get('status'),
            'transcriptMaterialReason': reasons.get(key, {}).get('reason'),
        })

n_transcript = sum(1 for x in matrix if x['hasTranscript'])
n_release = sum(1 for x in matrix if x['hasEarningsRelease'])
n_both = sum(1 for x in matrix if x['hasTranscript'] and x['hasEarningsRelease'])
from collections import Counter
gs = Counter(x['guidanceStatus'] for x in matrix)

per_ticker = {}
for t in tickers:
    rows = [x for x in matrix if x['ticker'] == t]
    per_ticker[t] = {
        'cells': len(rows),
        'transcripts': sum(1 for x in rows if x['hasTranscript']),
        'withRelease': sum(1 for x in rows if x['hasEarningsRelease']),
        'transcriptAndRelease': sum(1 for x in rows if x['hasTranscript'] and x['hasEarningsRelease']),
    }

# recommended pilot subset: full 4-quarter transcript coverage (all 4 cells have transcript)
full = [t for t in tickers if per_ticker[t]['cells'] == 4 and per_ticker[t]['transcripts'] == 4]
# also tickers where every returned cell has transcript+release
full_both = [t for t in tickers if per_ticker[t]['cells'] >= 3 and per_ticker[t]['transcriptAndRelease'] == per_ticker[t]['cells']]

out = {
    'generated': '2026-11-22',
    'source': 'Equibles REST API v1 (free tier, 100 requests/day shared REST+MCP)',
    'pilotSetSelection': 'Top 10 universe tickers by EDGAR record count (manifest data/edgar_excerpts/manifest.json); 113 tickers tie at 16 records, 20 at 15; tie broken alphabetically by ticker',
    'pilotTickers': [{'ticker': t, 'edgarRecords': pt[t]} for t in top10],
    'requestsUsedThisJob': 45,
    'requestsBreakdown': {'events': 10, 'call': 35},
    'requestsBudget': 50,
    'note': 'One additional Equibles request was made earlier today by the parent agent (NVDA FY2027 Q2 call check, 200, transcript available) and is not counted in this job total. Daily total so far: 46 of 100.',
    'totals': {
        'cells': len(matrix),
        'withTranscript': n_transcript,
        'withEarningsRelease': n_release,
        'withTranscriptAndRelease': n_both,
        'guidanceStatusCounts': dict(gs),
    },
    'perTicker': per_ticker,
    'recommendedPilotSubset': full,
    'recommendedPilotSubsetNote': 'Tickers with all 4 checked quarters having a transcript. ABBV/AMD/AMT (3 cells) and AXP (2 cells) were short because the first events page (limit 8) held fewer than 4 earnings calls; their 4th quarters are on the next events page.',
    'matrix': matrix,
}
json.dump(out, open('/home/hatch/workspace/stock-thesis/experiments/equibles-coverage-pilot.json', 'w'), indent=2)
print('cells:', len(matrix), '| transcripts:', n_transcript, '| releases:', n_release, '| both:', n_both)
print('guidanceStatus:', dict(gs))
print('per-ticker:', json.dumps(per_ticker, indent=1))
print('full 4-quarter transcript tickers:', full)
print('all-returned-cells transcript+release:', full_both)
print()
for x in matrix:
    if not x['hasTranscript']:
        print('NO TRANSCRIPT:', x['ticker'], f"FY{x['fiscalYear']}Q{x['fiscalQuarter']}", x['callDate'], x['status'], '|', x['transcriptMaterialStatus'], '|', x['transcriptMaterialReason'])
for x in matrix:
    if not x['hasEarningsRelease']:
        print('NO RELEASE:', x['ticker'], f"FY{x['fiscalYear']}Q{x['fiscalQuarter']}", x['callDate'], x['status'])
