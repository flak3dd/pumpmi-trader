import time
from src import similarity as S
W=dict(curve_pct=60,mcap=40000,replies=15,top10_pct=15,dev_pct=1,bundle_pct=5,rise_pts=8,sentiment_0_100=80,mention_volume=900,size_pct_equity=1,age_s=600)
L=dict(curve_pct=30,mcap=200000,replies=1,top10_pct=29,dev_pct=4,bundle_pct=24,rise_pts=-5,sentiment_0_100=50,mention_volume=100,size_pct_equity=1,age_s=3500)
now=time.time()
ex=lambda f,p,**k:{**f,**k,"side":"EXIT","pnl_pct":p,"ts":now}
H=[ex(W,30),ex(W,25,replies=14),ex(W,40,curve_pct=62),ex(L,-18),ex(L,-20,mcap=190000),ex(L,-15,replies=2)]
# Mechanism checks keep a small sample. Production SIM requires 30 prior exits.
RELAX={**S.SIM,"min_labeled":5,"min_each":2}
def test_inconclusive_thin(): assert S.decide(W,H[:4])["decision"]=="inconclusive"
def test_inconclusive_one_side(): assert S.decide(W,[ex(W,10)]*5+[ex(L,-5)])["decision"]=="inconclusive"
def test_boost(): assert S.decide(W,H,RELAX)["decision"]=="boost"
def test_skip(): assert S.decide(L,H,RELAX)["decision"]=="skip"
def test_disagree():
    C={**RELAX,"score_min":5}  # score can't reach -> kNN says win, score doesn't -> disagree
    assert S.decide(W,H,C)["decision"]=="disagree"
def test_default_requires_30_and_ignores_future_and_self():
    assert S.SIM["min_labeled"]==30
    assert S.decide(W,H)["decision"]=="inconclusive"
    future=[{**r,"ts":now+10_000} for r in H]
    assert S.decide(W,future,RELAX,now=now)["decision"]=="inconclusive"
    self_rows=[{**r,"mint":"same","ts":now-5} for r in H]
    assert S.decide({**W,"mint":"same"},self_rows,RELAX,now=now)["decision"]=="inconclusive"
    prior=[{**r,"mint":"other","ts":now-5} for r in H]
    assert S.decide({**W,"mint":"fresh"},prior,RELAX,now=now)["decision"]=="boost"
def test_missing_flags(): assert S.vec({"replies":None})[13]==1 and S.vec({"replies":0})[13]==0
def test_weight_cap_and_decay():
    assert S.weight({"pnl_pct":1000,"ts":now},now)==S.SIM["w_cap"]
    assert S.weight({"pnl_pct":0,"ts":now-14*86400},now)<0.3
