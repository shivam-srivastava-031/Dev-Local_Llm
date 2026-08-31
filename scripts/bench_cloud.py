"""Benchmarks the large free OpenRouter models + Gemini free tier on the coding
and reasoning cases that the local 7B models provably fail."""
import json, os, sys, time, urllib.request, urllib.error

OR_KEY = os.environ["OPENROUTER_API_KEY"]
GM_KEY = os.environ["GEMINI_API_KEY"]
OR = "https://openrouter.ai/api/v1/chat/completions"
GM = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

BUG_MAX = ("max all-negative",
           "This should return the max but has a bug. Fix it:\n\ndef largest(xs):\n"
           "    m = 0\n    for x in xs:\n        if x > m:\n            m = x\n    return m",
           lambda t: ("xs[0]" in t) or ("-inf" in t) or ("max(" in t))
BUG_MED = ("median even-length",
           "This returns the median but has a bug. Fix it:\n\ndef median(xs):\n"
           "    xs = sorted(xs)\n    n = len(xs)\n    return xs[n // 2]",
           lambda t: ("n % 2" in t) or ("n%2" in t))
APPLES = ("apples/bananas",
          "I have 3 apples. I eat 2 bananas. How many apples do I have left?",
          lambda t: ("3" in t) and ("0 apple" not in t.lower()))
TESTS = [APPLES, BUG_MAX, BUG_MED]

MODELS = [
    ("or", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    ("or", "nvidia/nemotron-3-super-120b-a12b:free"),
    ("or", "minimax/minimax-m3:free"),
    ("or", "z-ai/glm-5.2:free"),
    ("or", "cohere/north-mini-code:free"),
    ("or", "poolside/laguna-s-2.1:free"),
    ("or", "google/gemma-4-31b-it:free"),
    ("gm", "models/gemini-3.7-flash"),
    ("gm", "models/gemini-3.5-flash"),
]

def ask(kind, model, prompt):
    url, key = (OR, OR_KEY) if kind == "or" else (GM, GM_KEY)
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 2000}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    t0 = time.time()
    try:
        d = json.load(urllib.request.urlopen(req, timeout=300))
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode())
        except Exception:
            return None, "http %s" % e.code, time.time() - t0
    except Exception as e:
        return None, str(e)[:40], time.time() - t0
    if isinstance(d, list):
        d = d[0] if d else {}
    if "error" in d:
        err = d["error"]
        return None, str(err.get("message", err))[:50], time.time() - t0
    try:
        msg = d["choices"][0]["message"]
    except Exception:
        return None, "odd shape", time.time() - t0
    txt = (msg.get("content") or "") + " " + str(msg.get("reasoning") or "")
    return txt, "", time.time() - t0

print("%-46s %-19s %6s  %s" % ("MODEL", "TEST", "TIME", "RESULT"))
print("-" * 84)
score = {}
for kind, m in MODELS:
    p = 0
    for name, prompt, check in TESTS:
        txt, err, el = ask(kind, m, prompt)
        if txt is None:
            print("%-46s %-19s %5.0fs  ERR %s" % (m, name, el, err))
            continue
        ok = check(txt)
        p += ok
        print("%-46s %-19s %5.0fs  %s" % (m, name, el, "PASS" if ok else "FAIL"))
        sys.stdout.flush()
    score[m] = p
print("-" * 84)
print("\nSCORES (out of %d)" % len(TESTS))
for m, v in sorted(score.items(), key=lambda x: -x[1]):
    print("  %-46s %d/%d" % (m, v, len(TESTS)))
