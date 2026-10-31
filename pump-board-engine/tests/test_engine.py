import pytest, os
from src.config import load
from src.holders import make_holders
from src.social_crawl import make_source
from src.engine import Engine
from src.live import execute_live, LiveRefused
def run(tmp, monkey=None):
    c=load("config.example.yaml"); c["paths"]={**c["paths"],"ledger_dir":str(tmp/"l"),"board_json":str(tmp/"b.json")}
    h=make_holders(c); e=Engine(c,h,make_source(c)); v={}
    e.run_feed(h.new_coins(), log=lambda m: v.__setitem__(m.split()[0], m.split()[1])); e.write_board()
    return e,v
def test_pipeline(tmp_path):
    e,v=run(tmp_path)
    for s in ["RAGEY","HALO","QUARK","MEH","FUDZ","TRAP","EXITD"]: assert v[s]=="SKIP",s
    assert "devRUG1" in e.ledger.do_not_rebuy
    assert e.board.cards["FLUX"]["reason"].startswith("bundle moved")
    assert e.closed and e.equity()>=6000 and (tmp_path/"b.json").exists()
def test_live_refuses():
    with pytest.raises(LiveRefused): execute_live({}, {"mode":"paper"})
    os.environ["PUMP_ENGINE_ENABLE_LIVE"]="true"
    with pytest.raises(LiveRefused): execute_live({}, {"mode":"live"})
