import json,time
d=json.load(open('out/board.json'))
def card(c):
    bp=c.get('by_platform') or {}
    return {k:v for k,v in dict(s=c['symbol'],col=c['column'],v=c.get('verdict',''),r=c.get('reason',''),pnl=c.get('pnl'),pp=c.get('pnl_pct'),
      mc=c.get('mcap'),cv=c.get('curve_pct'),top=c.get('dev_pct'),b5=c.get('bundle_pct'),sent=c.get('sentiment_0_100'),men=c.get('mentions'),
      pl={p:bp[p].get('mentions',0) for p in bp}).items() if v not in (None,'',{})}
cols=d['columns'];done=sorted(cols['done'],key=lambda c:-c.get('ts_seen',0))[:14]
out=dict(u=d['updated'],eq=d['equity'],bank=d['bankroll'],x=d['multiple'],st=d['stats'],
 cards=[card(c) for c in cols['checking']+cols['holding']+done],log=d['log'][:14])
import sys
print('const DATA = '+json.dumps(out,separators=(',',':'),ensure_ascii=False)+';')
