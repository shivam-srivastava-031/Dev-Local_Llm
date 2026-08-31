"""Writes cloud provider connections + the quota monitor into Open WebUI's DB.
Run against /data (the open-webui volume) with the container STOPPED."""
import json, os, sqlite3, time, uuid

DB = "/data/webui.db"
now = int(time.time())

PROVIDERS = [
    ("openrouter", "https://openrouter.ai/api/v1",
     os.environ.get("OPENROUTER_API_KEY", "").strip()),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai",
     os.environ.get("GEMINI_API_KEY", "").strip()),
]

# Populated by scripts/probe_models.py - only models that answered a real
# 512-token completion on THIS account. Re-run the prober when keys or plans
# change; a model being listed by the API does not mean you can call it.
# Models the prober accepts but that fail in real use. Kept as an explicit
# blocklist so a future re-probe cannot quietly reintroduce them.
#   nemotron-3-ultra-550b : 301s abort + 291s provider error on retest
#   gemini-flash-latest   : quota exceeded on every attempt (0/2, twice)
#   gemini-3.7-flash      : quota exceeded / high demand (1/3, then 0/2)
BLOCKLIST = {
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "models/gemini-flash-latest",
    "models/gemini-3.7-flash",
}


def _load_working():
    try:
        w = json.load(open("/out/working_models.json"))
    except Exception:
        return {}
    # The prober now records Gemini ids exactly as /models returns them
    # ("models/gemini-3.6-flash"), which is also what Open WebUI sends back.
    # Do NOT add a second bare form - it double-listed every model in the picker.
    # Belt and braces: only ":free" OpenRouter models survive, whatever the
    # probe file says. Everything else draws on credits and eventually 402s.
    orl = [m for m in w.get("openrouter", [])
           if m.endswith(":free") and m not in BLOCKLIST]
    gm = [m for m in w.get("gemini", []) if m not in BLOCKLIST]
    return {"openrouter": orl, "gemini": gm}

CURATED = _load_working()

c = sqlite3.connect(DB)

def put(key, value):
    c.execute(
        "INSERT INTO config(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value), now))

urls, keys, configs = [], [], {}
active = []
for i, (name, url, key) in enumerate([p for p in PROVIDERS if p[2]]):
    urls.append(url)
    keys.append(key)
    # OpenRouter lists 396 models; curate to the strong coding/reasoning ones.
    # Gemini lists 54 and its ids are "models/x" prefixed, so filtering there is
    # left off deliberately - an id mismatch would hide everything.
    curated = CURATED.get(name, [])
    print("  %-11s curated to %d verified-working models" % (name, len(curated)))
    configs[str(i)] = {"enable": True, "prefix_id": name,
                       "model_ids": curated, "tags": []}
    active.append(name)

# Benchmarked 3/3 on the reasoning + coding cases the local 7B models fail,
# and 10-20x faster than the local reasoner. Best default for real work.
DEFAULT_MODEL = "openrouter.cohere/north-mini-code:free"

if urls:
    put("ui.default_models", DEFAULT_MODEL)
    put("openai.api_base_urls", urls)
    put("openai.api_keys", keys)
    put("openai.api_configs", configs)
    put("openai.enable", True)
    print("configured providers: %s" % ", ".join(active))
else:
    put("openai.enable", False)
    print("no API keys found in environment - openai connections left disabled")

# --- install the quota monitor filter ---
src = open("/src/quota_monitor.py", encoding="utf-8").read()
row = c.execute("select id from function where id='quota_monitor'").fetchone()
owner = c.execute("select id from user order by created_at limit 1").fetchone()
owner_id = owner[0] if owner else None

valves = {
    "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY", "").strip(),
    "warn_below_usd": 1.0,
    "show_balance_every_message": False,
    "cache_seconds": 120,
    # Open WebUI otherwise forwards the model's full max_completion_tokens
    # (65536 for gemini-3.7-flash), which a zero-credit account cannot afford.
    "clamp_max_tokens": True,
    "openrouter_max_tokens": 4096,
    # Measured: OpenRouter funds ~$0.036 per completion on this account.
    # affordable_tokens = budget_usd / model_completion_price, so opus (expensive)
    # gets ~1150 and gemini-flash (cheap) gets the 4096 ceiling.
    "budget_usd": 0.036,
    "safety_fraction": 0.8,
}
meta = {"description": "Shows remaining cloud quota and warns when exhausted.",
        "manifest": {"title": "Quota Monitor", "version": "0.1.0"}}

if row:
    c.execute("update function set content=?, valves=?, meta=?, is_active=1, is_global=1, "
              "updated_at=?, user_id=COALESCE(user_id,?) where id='quota_monitor'",
              (src, json.dumps(valves), json.dumps(meta), now, owner_id))
    print("quota monitor: updated")
else:
    c.execute("insert into function(id,user_id,name,type,content,meta,valves,is_active,"
              "is_global,created_at,updated_at) values(?,?,?,?,?,?,?,1,1,?,?)",
              ("quota_monitor", owner_id, "Quota Monitor", "filter", src,
               json.dumps(meta), json.dumps(valves), now, now))
    print("quota monitor: installed (owner=%s)" % (owner_id or "none yet"))

c.commit()

print("\n--- verification ---")
for k in ["openai.enable", "openai.api_base_urls", "openai.api_configs"]:
    v = c.execute("select value from config where key=?", (k,)).fetchone()
    print("  %-24s %s" % (k, (v[0] if v else "<unset>")[:120]))
n = c.execute("select count(*) from function where is_active=1").fetchone()[0]
print("  active functions        %d" % n)
kk = c.execute("select value from config where key='openai.api_keys'").fetchone()
if kk:
    print("  api_keys                %s" % ["<set len %d>" % len(x) for x in json.loads(kk[0])])
