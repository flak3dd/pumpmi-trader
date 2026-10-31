from src.sentiment import *
def test_pos(): assert score_text("$X sending 🚀 bullish gem") > 0.5
def test_neg(): assert score_text("rug scam 🚩") < -0.5
def test_negation(): assert score_text("not a rug") > 0
def test_neutral(): assert score_text("hello world") == 0
def test_weighted():
    assert score_posts([{"text":"moon","likes":1000},{"text":"rug","likes":0}]) > 0
def test_empty(): assert score_posts([]) == 0
def test_velocity(): assert velocity([10,10,40]) == 4
