from src.rules import *
M=lambda **k: CoinMetrics(**{"mint":"m","symbol":"S","dev_wallet":"d","dev_pct":5,"bundle_pct":5,**k})
R={"max_dev_pct":10,"max_bundle_pct":25}
def test_dev(): assert not check_dev(M(dev_pct=31))[0] and check_dev(M(dev_pct=10))[0]
def test_bundle(): assert not check_bundle(M(bundle_pct=37))[0] and check_bundle(M(bundle_pct=25))[0]
def test_known_rug_forever():
    ok,r,f=entry_checks(M(dev_wallet="rug"),{"rug"},R); assert not ok and f
def test_flags():
    assert not entry_checks(M(honeypot=True),set(),R)[0]; assert not entry_checks(M(dev_sold=True),set(),R)[0]
def test_pass(): assert entry_checks(M(),set(),R)[0]
def test_exit_bundle(): assert should_exit_bundle(M(bundle_moved=True))
def test_goal(): assert goal_hit(6000,1500,4) and not goal_hit(5999,1500,4)
def test_social(): assert not social_ok(5,0.5)[0] and not social_ok(50,-0.2)[0] and social_ok(50,0.5)[0]
