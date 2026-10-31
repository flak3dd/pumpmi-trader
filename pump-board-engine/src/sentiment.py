"""Lexicon sentiment for crypto X posts. Score in [-1, 1]. No ML deps."""
import re, math
POS = {"moon":2,"mooning":2,"send":1.5,"sending":1.5,"bullish":2,"gem":1.5,"lfg":1.5,"pump":1,"ath":1.5,
       "based":1,"buy":1,"buying":1,"aped":1,"ape":1,"100x":2,"10x":1.5,"love":1,"strong":1,"graduated":1.5,
       "early":1,"fire":1,"🚀":2,"🔥":1.5,"💎":1.5,"🌕":1.5,"legit":1.5,"cto":0.5}
NEG = {"rug":-3,"rugged":-3,"rugpull":-3,"scam":-3,"honeypot":-3,"dump":-2,"dumping":-2,"dead":-2,"bundled":-2,
       "bundle":-1,"sold":-1,"sell":-1,"exit":-1,"jeet":-1,"jeets":-1,"fake":-2,"avoid":-2,"bearish":-2,"rekt":-2,
       "careful":-1,"warning":-1.5,"💀":-1.5,"🚩":-2,"⚠️":-1.5}
NEGATORS = {"not","no","never","isnt","isn't","dont","don't"}
TOK = re.compile(r"[\w$']+|[^\w\s]", re.UNICODE)

def score_text(text):
    toks = [t.lower() for t in TOK.findall(text or "")]
    s = 0.0
    for i, t in enumerate(toks):
        w = POS.get(t, NEG.get(t, 0))
        if w and NEGATORS & set(toks[max(0, i-3):i]): w = -w * 0.7
        s += w
    if (text or "").count("!") >= 2: s *= 1.1
    return math.tanh(s / 4)

def score_posts(posts):
    """Engagement-weighted mean. posts: [{text, likes?, reposts?, author_followers?}]"""
    if not posts: return 0.0
    num = den = 0.0
    for p in posts:
        w = 1 + math.log1p(p.get("likes",0) + 2*p.get("reposts",0)) + 0.5*math.log1p(p.get("author_followers",0))/5
        num += w * score_text(p.get("text","")); den += w
    return num / den

def velocity(counts):
    """counts: chronological buckets. Ratio of last bucket to mean of prior."""
    if len(counts) < 2: return 1.0
    prior = sum(counts[:-1]) / (len(counts)-1)
    return counts[-1] / prior if prior else float(counts[-1] > 0) * 10
