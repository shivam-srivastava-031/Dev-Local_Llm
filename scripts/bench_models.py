"""Benchmarks local Ollama models on coding + reasoning, scoring BOTH accuracy
and wall-clock. A model that is right but takes 3 minutes is not usable for
interactive work on this hardware, so time is reported alongside the score."""
import json, sys, time, urllib.request

OLLAMA = "http://localhost:11434/api/chat"

TESTS = [
    # (category, prompt, checker, label)
    ("reason", "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the "
               "ball. How much does the ball cost?",
     lambda a: "0.05" in a or "5 cent" in a.lower(), "bat-and-ball"),
    ("reason", "I have 3 apples. I eat 2 bananas. How many apples do I have left?",
     lambda a: "3" in a and "0 apple" not in a.lower(), "apples/bananas"),
    ("reason", "How many times does the letter 'r' appear in 'strawberry'?",
     lambda a: "3" in a, "count r"),
    ("code", "This returns the median but has a bug. Fix it:\n\n"
             "def median(xs):\n    xs = sorted(xs)\n    n = len(xs)\n    return xs[n // 2]",
     lambda a: "n % 2" in a or "n%2" in a, "median even-length"),
    ("code", "This should return the max but has a bug. Fix it:\n\n"
             "def largest(xs):\n    m = 0\n    for x in xs:\n        if x > m:\n"
             "            m = x\n    return m",
     lambda a: "xs[0]" in a or "-inf" in a or "max(" in a, "max all-negative"),
    ("code", "Write a Python function that safely divides two numbers.",
     lambda a: "ZeroDivision" in a or "== 0" in a or "!= 0" in a, "safe divide"),
]


def chat(model, prompt, timeout=600):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "stream": False, "options": {"num_predict": 2000, "num_ctx": 8192,
                                         "temperature": 0.2}}
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        d = json.load(urllib.request.urlopen(req, timeout=timeout))
    except Exception as e:
        return "", time.time() - t0, 0
    msg = d.get("message", {})
    # reasoning models put chain-of-thought in a separate field
    text = (msg.get("content") or "") + " " + (msg.get("thinking") or "")
    ec, ed = d.get("eval_count", 0), d.get("eval_duration", 1)
    return text, time.time() - t0, (ec / (ed / 1e9) if ed else 0)


models = sys.argv[1:]
print("%-20s %-8s %-20s %6s %8s  %s" % ("MODEL", "CAT", "TEST", "TIME", "TOK/S", "RESULT"))
print("-" * 78)
summary = {}
for m in models:
    passed = total = 0
    elapsed = 0.0
    for cat, prompt, check, label in TESTS:
        ans, el, tps = chat(m, prompt)
        ok = bool(ans) and check(ans)
        passed += ok
        total += 1
        elapsed += el
        print("%-20s %-8s %-20s %5.0fs %8.1f  %s"
              % (m, cat, label, el, tps, "PASS" if ok else "FAIL"))
        sys.stdout.flush()
    summary[m] = (passed, total, elapsed)
    print("-" * 78)

print("\n%-20s %8s %10s %12s" % ("MODEL", "SCORE", "TOTAL", "AVG/ANSWER"))
for m, (p, t, el) in summary.items():
    print("%-20s %6d/%d %9.0fs %11.0fs" % (m, p, t, el, el / t))
