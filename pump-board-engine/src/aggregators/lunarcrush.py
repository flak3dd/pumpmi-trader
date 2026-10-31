"""LunarCrush API v4 client. Base https://lunarcrush.com/api4, Bearer LUNARCRUSH_API_KEY.
Fixture mode is used ONLY when no key is set; live mode never falls back to fixtures (errors propagate)."""
import json, os, urllib.parse, urllib.request
from pathlib import Path

BASE = "https://lunarcrush.com/api4"

class LunarCrushError(RuntimeError): pass

class LunarCrushClient:
    def __init__(self, api_key=None, fixture_path="fixtures/lunarcrush.json", timeout=15):
        self.key = api_key if api_key is not None else os.environ.get("LUNARCRUSH_API_KEY", "")
        self.mode = "live" if self.key else "fixture"
        self.timeout = timeout
        self._fx = json.loads(Path(fixture_path).read_text()) if self.mode == "fixture" else None

    def _get(self, path):
        req = urllib.request.Request(
            BASE + path,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Accept": "application/json",
                "User-Agent": "PumpBoardEngine/1.0 (Grok Bot; paper trading research)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r: return json.load(r)
        except urllib.error.HTTPError as e:
            raise LunarCrushError(f"{e.code} on {path}: {e.read()[:200]!r}") from e

    def _fixture(self, topic, kind):
        return self._fx.get(topic, {}).get(kind, {"data": [] if kind != "topic" else {}})

    def topic(self, topic):
        t = topic.lower().lstrip("$")
        return self._get(f"/public/topic/{urllib.parse.quote(t)}/v1") if self.mode == "live" else self._fixture(t, "topic")
    def posts(self, topic):
        t = topic.lower().lstrip("$")
        return self._get(f"/public/topic/{urllib.parse.quote(t)}/posts/v1") if self.mode == "live" else self._fixture(t, "posts")
    def news(self, topic):
        t = topic.lower().lstrip("$")
        return self._get(f"/public/topic/{urllib.parse.quote(t)}/news/v1") if self.mode == "live" else self._fixture(t, "news")
    def coins_list(self):
        return self._get("/public/coins/list/v2") if self.mode == "live" else self._fx.get("_coins_list", {"data": []})

    def bundle(self, topic):
        """topic + posts + news; posts/news are optional (404/plan limits -> empty)."""
        out = {"topic": self.topic(topic)}
        for k, fn in (("posts", self.posts), ("news", self.news)):
            try: out[k] = fn(topic)
            except LunarCrushError: out[k] = {"data": []}
        return out
