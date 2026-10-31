"""X-shaped social data. Sources:
- lunarcrush: LunarCrush API v4 aggregator (preferred; see src/aggregators/)
- fixture : offline JSON (tests / paper)
- x_api   : public X API v2 (needs X_BEARER_TOKEN in env); GET /2/tweets/search/recent + /2/tweets/counts/recent
- grok_mcp: Grok Bot calls x MCP tools itself and drops results as JSON for this module:
     x.search_posts_all(query="$TICKER OR <mint>", max_results=100)  -> posts
     x.get_posts_counts_recent(query="$TICKER", granularity="minute") -> counts
     x.get_users_by_username(usernames=[...])  -> follower counts for weighting
   Write to out/social/<SYMBOL>.json as {"posts":[...],"counts":[...]} and set source: grok_mcp.
Normalized output: {"symbol","posts":[{text,likes,reposts,author,author_followers}],"counts":[int],"posts_15m":int}
"""
import json, os, urllib.parse, urllib.request
from pathlib import Path

def _norm(symbol, posts, counts):
    return {"symbol": symbol, "posts": posts, "counts": counts, "posts_15m": sum(counts[-15:]) if counts else len(posts)}

class FixtureSource:
    def __init__(self, path): self.data = json.loads(Path(path).read_text())
    def fetch(self, symbol, mint=None):
        d = self.data.get(symbol, {"posts": [], "counts": []})
        return _norm(symbol, d["posts"], d["counts"])

class GrokMcpDropSource:
    def __init__(self, root="out/social"): self.root = Path(root)
    def fetch(self, symbol, mint=None):
        f = self.root / f"{symbol}.json"
        if not f.exists(): return _norm(symbol, [], [])
        d = json.loads(f.read_text()); return _norm(symbol, d.get("posts", []), d.get("counts", []))

class XApiSource:
    BASE = "https://api.x.com/2"
    def __init__(self):
        self.tok = os.environ.get("X_BEARER_TOKEN")
        if not self.tok: raise RuntimeError("X_BEARER_TOKEN not set (see .env.example)")
    def _get(self, path, params):
        req = urllib.request.Request(f"{self.BASE}{path}?{urllib.parse.urlencode(params)}",
                                     headers={"Authorization": f"Bearer {self.tok}"})
        with urllib.request.urlopen(req, timeout=15) as r: return json.load(r)
    def fetch(self, symbol, mint=None):
        q = f"${symbol}" + (f" OR {mint}" if mint else "") + " -is:retweet"
        s = self._get("/tweets/search/recent", {"query": q, "max_results": 100,
            "tweet.fields": "public_metrics", "expansions": "author_id", "user.fields": "public_metrics"})
        users = {u["id"]: u for u in s.get("includes", {}).get("users", [])}
        posts = [{"text": t["text"], "likes": t["public_metrics"]["like_count"],
                  "reposts": t["public_metrics"]["retweet_count"], "author": users.get(t["author_id"], {}).get("username"),
                  "author_followers": users.get(t["author_id"], {}).get("public_metrics", {}).get("followers_count", 0)}
                 for t in s.get("data", [])]
        c = self._get("/tweets/counts/recent", {"query": q, "granularity": "minute"})
        return _norm(symbol, posts, [b["tweet_count"] for b in c.get("data", [])])

class LunarCrushSource:
    """Multi-platform aggregator (x, reddit, youtube, tiktok, instagram, news).
    Live HTTP when LUNARCRUSH_API_KEY is set; offline fixture otherwise."""
    def __init__(self, cfg_social):
        from .aggregators.lunarcrush import LunarCrushClient
        self.platforms = cfg_social.get("platforms") or None
        self.client = LunarCrushClient(fixture_path=cfg_social.get("lunarcrush_fixture", "fixtures/lunarcrush.json"))
    def fetch(self, symbol, mint=None):
        from .aggregators.fuse import fuse, PLATFORMS
        snap = fuse(symbol, self.client.bundle(symbol), self.platforms or PLATFORMS).to_dict()
        posts = [{"text": p["title"], "likes": p["interactions"]} for p in snap["top_posts"]]
        return {"symbol": symbol, "posts": posts, "counts": [], "posts_15m": snap["mention_volume"],
                "snapshot": snap, "client_mode": self.client.mode}

def make_source(cfg):
    s = cfg["social"].get("primary") or cfg["social"].get("source")
    if s == "lunarcrush": return LunarCrushSource(cfg["social"])
    if s == "fixture": return FixtureSource(cfg["social"]["fixture_path"])
    if s == "grok_mcp": return GrokMcpDropSource()
    if s == "x_api": return XApiSource()
    raise ValueError(s)
