import os, pytest
from src.aggregators.lunarcrush import LunarCrushClient
from src.aggregators.fuse import fuse, PLATFORMS, reason_text

def client(): return LunarCrushClient(api_key="", fixture_path="fixtures/lunarcrush.json")
def test_fixture_mode_without_key():
    c = client(); assert c.mode == "fixture"
    d = c.topic("$PIXA")["data"]; assert d["types_count"]["tiktok-video"] == 4100
def test_live_mode_with_key_no_fixture():
    c = LunarCrushClient(api_key="dummy"); assert c.mode == "live" and c._fx is None
def test_fuse_all_platforms():
    s = fuse("PIXA", client().bundle("PIXA"))
    assert set(s.by_platform) == set(PLATFORMS) and s.mention_volume == 7486
    assert "tiktok volume spike" in s.reasons and "reddit positive" in s.reasons
    assert 50 < s.sentiment_0_100 <= 100 and -1 <= s.sentiment_m1_1 <= 1 and s.headlines
def test_fuse_negative():
    s = fuse("FUDZ", client().bundle("FUDZ")); assert s.sentiment_0_100 < 40 and "reddit negative" in s.reasons
def test_platform_filter():
    s = fuse("PIXA", client().bundle("PIXA"), platforms=["x", "reddit"])
    assert set(s.by_platform) == {"x", "reddit"} and s.mention_volume == 3200
def test_empty_topic(): s = fuse("NONE", client().bundle("none")); assert s.mention_volume == 0 and s.sentiment_0_100 == 50
def test_reason_text(): assert reason_text(fuse("PIXA", client().bundle("PIXA")))

@pytest.mark.skipif(not os.environ.get("LUNARCRUSH_API_KEY"), reason="no LUNARCRUSH_API_KEY")
def test_live_topic():
    assert "data" in LunarCrushClient().topic("bitcoin")
