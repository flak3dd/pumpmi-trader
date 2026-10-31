"""Fuse LunarCrush per-network metrics into one SocialSnapshot."""
from dataclasses import dataclass, field, asdict

# LunarCrush network type keys -> our platform names
NETWORK_MAP = {"tweet": "x", "reddit-post": "reddit", "youtube-video": "youtube",
               "tiktok-video": "tiktok", "instagram-post": "instagram", "news": "news"}
PLATFORMS = list(NETWORK_MAP.values())
# Typical share of crypto mentions per platform; a platform at >= spike_mult x its usual share is a "spike".
BASELINE_SHARE = {"x": 0.60, "reddit": 0.10, "youtube": 0.03, "tiktok": 0.10, "instagram": 0.05, "news": 0.01}

@dataclass
class PlatformStat:
    mentions: int = 0
    interactions: int = 0
    sentiment_0_100: float | None = None

@dataclass
class SocialSnapshot:
    topic: str
    mention_volume: int
    interactions: int
    sentiment_0_100: float
    sentiment_m1_1: float
    trend: str
    by_platform: dict
    headlines: list = field(default_factory=list)
    top_posts: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    def to_dict(self):
        d = asdict(self); d["sentiment_-1_1"] = d.pop("sentiment_m1_1"); return d

def _trunc(s, n=90): s = (s or "").strip().replace("\n", " "); return s if len(s) <= n else s[:n-1] + "…"

def fuse(topic, bundle, platforms=PLATFORMS, spike_mult=2.0, pos=60, neg=40):
    t = bundle.get("topic", {}).get("data", {}) or {}
    counts, inter, sent = t.get("types_count", {}), t.get("types_interactions", {}), t.get("types_sentiment", {})
    by = {}
    for net, plat in NETWORK_MAP.items():
        if plat in platforms:
            by[plat] = PlatformStat(int(counts.get(net, 0)), int(inter.get(net, 0)),
                                    sent.get(net) if net in sent else None)
    vol = sum(p.mentions for p in by.values()); tot_int = sum(p.interactions for p in by.values())
    weighted = [(p.sentiment_0_100, max(p.interactions, p.mentions, 1)) for p in by.values() if p.sentiment_0_100 is not None]
    s100 = sum(s * w for s, w in weighted) / sum(w for _, w in weighted) if weighted else 50.0
    spikes, tone = [], []
    for plat, p in sorted(by.items(), key=lambda kv: -kv[1].mentions):
        if vol >= 20 and p.mentions / vol >= spike_mult * BASELINE_SHARE.get(plat, 0.1): spikes.append(f"{plat} volume spike")
        if p.sentiment_0_100 is not None and p.mentions:
            if p.sentiment_0_100 >= pos: tone.append(f"{plat} positive")
            elif p.sentiment_0_100 <= neg: tone.append(f"{plat} negative")
    reasons = spikes + tone
    heads = [_trunc(n.get("post_title") or n.get("title")) for n in bundle.get("news", {}).get("data", [])[:3]]
    posts = [{"network": NETWORK_MAP.get(p.get("post_type"), p.get("post_type")), "title": _trunc(p.get("post_title")),
              "interactions": p.get("interactions_24h", 0), "sentiment": p.get("post_sentiment")}
             for p in bundle.get("posts", {}).get("data", [])[:5]]
    return SocialSnapshot(topic, vol, int(t.get("interactions_24h", tot_int)), round(s100, 1),
                          round((s100 - 50) / 50, 3), t.get("trend", "flat"),
                          {k: asdict(v) for k, v in by.items()}, heads, posts, reasons)

def reason_text(snap, limit=3):
    return " + ".join(snap.reasons[:limit]) or "no platform signal"
