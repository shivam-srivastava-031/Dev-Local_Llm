"""Probes every candidate model with a real (tiny) completion and records which
ones actually answer. Writes working_models.json for configure_providers.py."""
import json, os, sys, time, random, urllib.request, urllib.error, concurrent.futures as cf

# 429 / 503 / timeouts are usually the free tier throttling us, not a broken
# model - especially when probing many models at once. Retry those.
TRANSIENT = ("429", "503", "timed out", "timeout", "high demand",
             "Provider returned error", "temporarily")

OR_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
GM_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
OR = "https://openrouter.ai/api/v1"
GM = "https://generativelanguage.googleapis.com/v1beta/openai"
# Must match the clamp the quota filter applies (openrouter_max_tokens), so a
# model that passes here can actually serve a real request.
# Small on purpose. The quota filter sizes real requests per model from live
# pricing, so the probe only needs to answer "is this model reachable at all".
# Probing large would fail expensive models for affordability, not availability.
PROBE_MAX_TOKENS = 400
RETRIES = 2
# A model is only "working" if it returns non-empty text. Classifiers, robotics
# and transcription models accept the request and return nothing usable.
PROBE_PROMPT = "What is 2+2? Reply with just the number."


def post_once(url, key, model, max_tokens):
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": PROBE_PROMPT}],
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(url + "/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode())
        except Exception:
            return False, "http %s" % e.code
    except Exception as e:
        return False, str(e)[:60]

    if isinstance(d, list):
        d = d[0] if d else {}
    if "error" in d:
        err = d["error"]
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        code = err.get("code") if isinstance(err, dict) else None
        return False, "%s %s" % (code, msg[:70])
    try:
        content = d["choices"][0]["message"].get("content")
    except Exception:
        return False, "odd shape"
    if not content or not str(content).strip():
        return False, "empty response (not a chat model)"
    return True, "ok"


def post(url, key, model, max_tokens):
    """Retries transient throttling before declaring a model broken."""
    last = ""
    for attempt in range(RETRIES):
        ok, why = post_once(url, key, model, max_tokens)
        if ok:
            return True, why
        last = why
        if not any(t.lower() in why.lower() for t in TRANSIENT):
            return False, why                      # permanent - do not retry
        time.sleep(3 + random.random() * 2)
    return False, last + " (after %d tries)" % RETRIES


def list_models(url, key):
    req = urllib.request.Request(url + "/models",
                                 headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=40) as r:
        return [x["id"] for x in json.load(r).get("data", [])]


def probe_all(label, url, key, ids, workers=4):
    out = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(post, url, key, m, PROBE_MAX_TOKENS): m for m in ids}
        for f in cf.as_completed(futs):
            m = futs[f]
            ok, why = f.result()
            out[m] = (ok, why)
            print("  %-52s %s  %s" % (m, "WORKS" if ok else "fail ", "" if ok else why[:70]))
            sys.stdout.flush()
    return out


result = {"openrouter": [], "gemini": [], "rejected": {}}

if OR_KEY:
    ids = list_models(OR, OR_KEY)
    junk = ("safety", "guard", "moderation", "embed", "rerank", "vision-exp",
            "tts", "image", "omni")
    free = [i for i in ids if i.endswith(":free") and not any(j in i for j in junk)]
    # Credit-gated models are excluded on purpose: this account has
    # total_credits = 0, so they fail with HTTP 402 at ever-smaller max_tokens
    # as the free allowance is consumed. Only no-cost models are kept.
    paid = []
    print("=== OpenRouter: %d free models (credit-gated excluded), probing at %d max_tokens ==="
          % (len(free), PROBE_MAX_TOKENS))
    r = probe_all("openrouter", OR, OR_KEY, free + paid)
    result["openrouter"] = sorted(m for m, (ok, _) in r.items() if ok)
    result["rejected"]["openrouter"] = {m: w for m, (ok, w) in r.items() if not ok}

if GM_KEY:
    ids = list_models(GM, GM_KEY)
    skip = ("embedding", "tts", "image", "aqa", "veo", "imagen", "learnlm",
            "native-audio", "live-", "robotics", "transcribe", "lyria",
            "computer-use", "deep-research", "antigravity", "guard", "safety")
    chat = [i for i in ids if not any(s in i for s in skip)]
    print("\n=== Gemini: probing %d chat models ===" % len(chat))
    r = probe_all("gemini", GM, GM_KEY, chat, workers=2)
    result["gemini"] = sorted(m for m, (ok, _) in r.items() if ok)
    result["rejected"]["gemini"] = {m: w for m, (ok, w) in r.items() if not ok}

json.dump(result, open("/out/working_models.json", "w"), indent=2)
print("\n=== SUMMARY ===")
print("  openrouter working: %d" % len(result["openrouter"]))
print("  gemini working    : %d" % len(result["gemini"]))
