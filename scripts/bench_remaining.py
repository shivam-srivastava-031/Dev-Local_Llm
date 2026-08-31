"""Tests the not-yet-benchmarked curated models on two discriminating cases,
so removal is evidence-based rather than a guess."""
import json, os, sys, time, urllib.request, urllib.error

OR_KEY = os.environ["OPENROUTER_API_KEY"]
GM_KEY = os.environ["GEMINI_API_KEY"]
OR = "https://openrouter.ai/api/v1/chat/completions"
GM = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

APPLES = ("apples", "I have 3 apples. I eat 2 bananas. How many apples do I have left?",
          lambda t: ("3" in t) and ("0 apple" not in t.lower()))
BUGMAX = ("max-bug",
          "This should return the max but has a bug. Fix it:\n\ndef largest(xs):\n"
          "    m = 0\n    for x in xs:\n        if x > m:\n            m = x\n    return m",
          lambda t: ("xs[0]" in t) or ("-inf" in t) or ("max(" in t))
TESTS = [APPLES, BUGMAX]

OR_MODELS = ["dots-studio/dots-3-note-preview:free",
             "inclusionai/ling-3.0-flash-fin:free",
             "liquid/lfm-2.5-2.6b:free",
             "minimax/minimax-m2.7:free",
             "nvidia/nemotron-3.5-lightning:free"]
GM_MODELS = ["models/gemini-3-flash-preview", "models/gemini-3.1-flash-lite",
             "models/gemini-3.1-flash-lite-preview", "models/gemini-3.5-flash-lite",
             "models/gemini-3.6-flash", "models/gemini-flash-latest",
             "models/gemini-flash-lite-latest", "models/gemma-4-26b-a4b-it",
             "models/gemma-4-31b-it"]

def ask(url, key, model, prompt):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 1500}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    t0 = time.time()
    try:
        d = json.load(urllib.request.urlopen(req, timeout=240))
    except urllib.error.HTTPError as e:
        try: d = json.loads(e.read().decode())
        except Exception: return None, "http %s" % e.code, time.time()-t0
    except Exception as e:
        return None, str(e)[:34], time.time()-t0
    if isinstance(d, list): d = d[0] if d else {}
    if "error" in d:
        err = d["error"]
        return None, str(err.get("message", err))[:44], time.time()-t0
    try: msg = d["choices"][0]["message"]
    except Exception: return None, "odd shape", time.time()-t0
    return (msg.get("content") or "") + " " + str(msg.get("reasoning") or ""), "", time.time()-t0

results = {}
print("%-46s %-9s %6s  %s" % ("MODEL", "TEST", "TIME", "RESULT"))
print("-" * 80)
for url, key, models in [(OR, OR_KEY, OR_MODELS), (GM, GM_KEY, GM_MODELS)]:
    for m in models:
        score = 0
        for name, prompt, check in TESTS:
            txt, err, el = ask(url, key, m, prompt)
            if txt is None:
                print("%-46s %-9s %5.0fs  ERR %s" % (m, name, el, err))
            else:
                ok = check(txt)
                score += ok
                print("%-46s %-9s %5.0fs  %s" % (m, name, el, "PASS" if ok else "FAIL"))
            sys.stdout.flush()
        results[m] = score
print("-" * 80)
print("\nKEEP (2/2):")
for m, v in results.items():
    if v == 2: print("   ", m)
print("\nREMOVE (<2):")
for m, v in results.items():
    if v < 2: print("    %-46s %d/2" % (m, v))
json.dump(results, open("/out/remaining_scores.json", "w"), indent=2)
