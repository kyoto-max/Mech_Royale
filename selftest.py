"""Replay the kit's recorded match through every strategy profile and fail on any act() error or slow turn.
check.py can't catch these: the crash guard in bot.py always answers in time, even when act() raises.

    python3 bot/selftest.py            (from the kit folder; needs check/stream.jsonl.gz and arena/)
"""
import gzip
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import bot as B  # noqa: E402
import mechfight as mf  # noqa: E402

msgs = [json.loads(l) for l in gzip.open(os.path.join(KIT, "check", "stream.jsonl.gz"), "rt") if l.strip()]
ok = True
for name, prof in B.PROFILES.items():
    B.STRATEGY, B.PROF = name, prof
    hello = json.loads(json.dumps(msgs[0]))
    hello["map"]["dir"] = os.path.join(KIT, "arena")
    bot = B.Bot(hello)
    worst = 0.0
    for o in (m for m in msgs if m.get("type") == "obs"):
        s = time.perf_counter()
        bot.act(o)
        worst = max(worst, time.perf_counter() - s)
        mf.draw.clear()
    errs = getattr(bot, "errors", 0)
    bad = errs > 0 or bot.world is None or worst > 0.035
    ok &= not bad
    print(f"{'FAIL' if bad else 'ok  '} {name:9s} errors={errs} map={'yes' if bot.world else 'NO'} worst={worst * 1000:.1f} ms")
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
