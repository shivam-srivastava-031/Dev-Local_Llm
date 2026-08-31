# Local LLM stack

Fully local AI assistant. Nothing leaves this machine except model/embedding
downloads from HuggingFace on first boot.

## Services

| Service    | URL                    | Purpose                          |
|------------|------------------------|----------------------------------|
| Open WebUI | http://localhost:3000  | Chat interface, memory, RAG      |
| Jupyter    | http://localhost:8889  | Code execution backend (Phase 5) |
| n8n        | http://localhost:5678  | Automation (Phase 6)             |
| Ollama     | http://localhost:11434 | Models (runs natively on Windows, not in Docker) |

Jupyter is on 8889 because 8888 is taken by an unrelated `searxng` container.

## Phase mapping

- **1 Foundation** — Ollama + `qwen2.5-coder:7b` (coding) + `llama3.1:8b` (general)
- **2 Interface** — Open WebUI, connected via `host.docker.internal:11434`
- **3 Memory** — `memories.background_review.enable=true`: reviews every 10 turns
  and stores only what is worth keeping. Injection capped at 3000 chars.
- **4 RAG** — local extraction -> chunk 1000/150 -> `all-MiniLM-L6-v2` embeddings
  -> ChromaDB -> hybrid BM25+vector -> `ms-marco-MiniLM-L-6-v2` reranker.
  `top_k=6`, reranked to 4.
- **5 Coding agent** — Jupyter engine (not the pyodide browser sandbox), so it can
  touch real files. `chat.tool_permissions.enable=true` gates tool calls.
  `./workspace` is the shared filesystem, mounted at `/home/jovyan/work`.
- **6 Automation** — n8n, reaching Ollama and Open WebUI over the compose network.

## Ollama binding

Ollama must listen on `0.0.0.0`, not `127.0.0.1`, or containers cannot reach it.
Set once as a persistent user env var:

    [Environment]::SetEnvironmentVariable('OLLAMA_HOST','0.0.0.0','User')

To revert, set it to `$null` and restart Ollama. The Windows firewall blocks
`ollama.exe` inbound, so this does not expose it to the LAN.

## Commands

    docker compose up -d          # start all
    docker compose down           # stop all (data is kept in volumes)
    docker compose logs -f open-webui
    docker compose pull && docker compose up -d   # update

## Data

- `open-webui` volume — chats, memories, knowledge bases, vector DB
- `n8n` volume — workflows and credentials
- `./workspace` — bind mount shared by Jupyter and n8n

`.env` holds the Jupyter token and is gitignored.


## Models (measured, not guessed)

Two tuned models built from `qwen2.5-coder:7b`, defined in `modelfiles/`:

- **`coder`** - temp 0.15, ctx 8192. Bug-fixing and code writing.
- **`assistant`** - temp 0.35, ctx 8192. General questions and reasoning.

Rebuild after editing: `ollama create coder -f modelfiles/coder.Modelfile`

### What the benchmarks actually showed

Measured on this machine (Ryzen 5 7535HS, 13.3GB RAM, CPU inference ~5 tok/s):

| Finding | Result |
|---|---|
| `qwen2.5-coder:7b` | Best of the three. Kept as the base for both models. |
| `qwen3:4b` | Faster raw (7.2 vs 5.1 tok/s) but slower in practice - it over-thinks for 100s+ and sometimes never reaches an answer. Failed the coding test. Not used. |
| `llama3.1:8b` | Failed the bat-and-ball trap. Slowest. Not used. |
| Long system prompts | Harmful for coding. Listing edge cases made `coder` fixate on them and miss the real bug: 1/3 -> 3/3 after shortening. |
| Short system prompts | Harmful for reasoning. `assistant` dropped 3/3 -> 0/3 on trick questions when the explicit chain-of-thought instruction was removed. |

The two prompts pull in opposite directions on purpose. Do not "tidy" one to
match the other; both were measured.

### Known ceiling

`assistant` fails "I have 3 apples, I eat 2 bananas, how many apples?" - it
reasons correctly that bananas do not affect apples, then answers "0". Right
reasoning, wrong conclusion, in one sentence. This is a 7B capability limit, not
a configuration problem. Prompting does not fix it.

## Memory tuning

WSL2 was holding 5.56GB. The `.wslconfig` in your user folder caps it at 4GB
with `autoMemoryReclaim=gradual`, freeing ~3.3GB. This did NOT raise tok/s
(generation is CPU-bound) but it stops swapping and keeps the model resident:
load time went 23.4s -> 0.0s.

Ollama env vars set (User scope): `OLLAMA_KEEP_ALIVE=30m` (model stays warm),
`OLLAMA_MAX_LOADED_MODELS=1` and `OLLAMA_NUM_PARALLEL=1` (avoid RAM thrashing on
13GB), `OLLAMA_FLASH_ATTENTION=1`.

## Unused models

`llama3.1:8b` (4.9GB) and `qwen3:4b` (2.5GB) both lost their benchmarks and are
not used by anything. Remove with `ollama rm llama3.1:8b qwen3:4b` to reclaim
~7.4GB, or keep them to re-test later.


## Cloud providers (OpenRouter + Gemini)

Local models stay the default. Cloud models sit beside them in the same picker,
so you switch per task: local for private or routine work, cloud for the hard
reasoning and coding that a 7B model cannot do.

### Adding your keys

Keys live in `.env` only - never in `docker-compose.yml`, never in the database
by hand, and never pasted into a chat window.

1. Edit `.env` and fill in:

       OPENROUTER_API_KEY=sk-or-v1-...      # https://openrouter.ai/keys
       GEMINI_API_KEY=AIza...               # https://aistudio.google.com/apikey

2. Run:

       bash scripts/apply.sh

That script stops Open WebUI, writes the connections and the quota monitor into
its database, and restarts it. It is idempotent - re-run it whenever a key
changes. Providers with a blank key are skipped, so you can add just one.

Endpoints used:

| Provider   | Base URL |
|------------|----------|
| OpenRouter | `https://openrouter.ai/api/v1` |
| Gemini     | `https://generativelanguage.googleapis.com/v1beta/openai` |

Models are prefixed in the picker (`openrouter.…`, `gemini.…`) so you always know
where a request is going.

## Quota display

`functions/quota_monitor.py` is an Open WebUI filter, installed active and global.

- **Before a request** to an OpenRouter model it reads your balance from
  `GET /api/v1/key` and shows remaining credit. Silent when healthy; warns below
  `warn_below_usd` (default $1); shows a red notification at zero.
- **After any response** it inspects the structured `error` object and raises a
  clear message when a provider is out of quota or rate limited, telling you your
  local models still work.

Gemini has no public "credits remaining" endpoint, so Gemini is detected
reactively - the warning appears when it actually returns 429 / RESOURCE_EXHAUSTED.

Tune it in Open WebUI under **Admin → Functions → Quota Monitor → Valves**
(`warn_below_usd`, `show_balance_every_message`, `cache_seconds`).

### Detection accuracy

The first version scanned the whole response body and flagged an ordinary reply
containing the digits "429" as rate limiting. It now inspects only the structured
`error` field. Test suite: 10/10, including three deliberate false-positive traps
(a reply mentioning "402 plus 429", a reply explaining how to handle rate limits,
and an unrelated HTTP 500).

To re-run those tests after editing the filter, see `scripts/apply.sh` and the
test block in the project history - the filter's `_quota_error` takes a parsed
body dict and returns a label or `None`.


## Verified working (with live keys)

| Check | Result |
|---|---|
| OpenRouter key | valid, 396 models, $0.0027 used |
| Gemini key | valid, 54 models |
| `anthropic/claude-opus-5` | live completion OK |
| `anthropic/claude-fable-5` | live completion OK |
| `gemini-3.6-flash` | live completion OK |
| Both providers from inside the container | reachable |
| Quota monitor balance lookup | live, returns real usage |
| Quota monitor error path | fires on 429 and 402, silent on normal replies |

OpenRouter is curated to 8 strong models (out of 396) so the picker stays usable.
Edit `CURATED` in `scripts/configure_providers.py` and re-run `scripts/apply.sh`
to change the list. Gemini is left unfiltered - its ids are `models/x` prefixed
and a mismatch would hide every model.

### Model gotchas found

- `gemini-2.5-flash` and `gemini-3-pro-preview` are **listed but not usable** by
  new accounts - the API returns 404 pointing at a newer id. Current working
  flash model is `gemini-3.6-flash`; current pro is `gemini-3.1-pro-preview`.
  A model appearing in `/models` does not mean your account can call it.
- Gemini returns errors as a single-element JSON **array**, not an object. The
  quota filter unwraps this; naive `body.get(...)` code will throw on it.

### The point of the cloud models

Your local `assistant` answers "I have 3 apples, I eat 2 bananas, how many
apples?" with "0". Both `claude-opus-5` and `claude-fable-5` answer: "You still
have 3 apples, since eating bananas doesn't change your apple count."

Use local for private and routine work, cloud for the reasoning it cannot do.




## Free models only

Every credit-gated model was removed. This account has `total_credits: 0`, so
paid models fail with HTTP 402 at a shrinking `max_tokens` as the free allowance
drains - it went $0.036 -> $0.027 over one session, taking `claude-fable-5` from
735 affordable tokens down to 539.

The picker now has **10 OpenRouter `:free` + 11 Gemini free-tier + 2 local**.
`configure_providers.py` drops anything not ending in `:free`, regardless of what
the probe file says, so a paid model cannot creep back in.

Verified: `:free` and Gemini free-tier models accept `max_tokens: 32768` with no
402, so the quota filter skips clamping them entirely.

## "Unlimited" - what is actually true

Nothing in the cloud is unlimited. Measured limits:

| Tier | Cost | Real limit |
|---|---|---|
| **Local** (`coder`, `assistant`) | free | **genuinely unlimited** - only your CPU |
| Gemini free tier | free | per-minute and per-day request quotas |
| OpenRouter `:free` | free | **50 requests/day** at 0 credits (1000/day at $10) |

The OpenRouter cap is real and confirmed from its own headers:

    X-RateLimit-Limit: 50
    X-RateLimit-Remaining: 0
    limit_source: openrouter_free_tier_daily

Probing every model to build this list consumed a large share of today's 50.
That is a one-off cost of the prober, not of normal use, but re-run it sparingly.

The quota filter reports this case specifically, with the reset time:

    OpenRouter free-model daily cap reached (50/day). Resets in ~3h.
    Gemini and your local models are unaffected.

It distinguishes three different OpenRouter failures, because each has a
different fix: daily cap (wait), out of credits (pay), and max_tokens too large
for the balance (clamp). Ordering matters - the specific checks must run before
the bare HTTP 402 / 429 ones.

**If you want a genuinely unlimited assistant, that is the local pair.** The
cloud models are the quality tier, rationed by someone else's free plan.


## Local model tiers

| Model | Base | Use for | Typical answer |
|---|---|---|---|
| `assistant` | qwen2.5-coder:7b | everyday questions | 20-40s |
| `coder` | qwen2.5-coder:7b | writing/fixing code | 30-70s |
| `reasoner` | deepseek-r1:8b | problems the fast pair gets wrong | 75-150s |

`reasoner` exists because the 7B models fail a specific class of question. Both
of these it gets right and `assistant` gets wrong:

| Test | `assistant` | `reasoner` |
|---|---|---|
| "3 apples, eat 2 bananas" | FAIL - answers 0 | PASS 144s |
| max() bug with all-negative input | FAIL | PASS 75s |

Use it when an answer looks wrong, not by default - it is 3-5x slower.

### Hardware ceiling

Only 8B models fit. Measured against ~7.7 GB usable RAM:

| Model | Size | Verdict |
|---|---|---|
| qwen3:8b, deepseek-r1:8b | 5.2 GB | fits |
| qwen2.5-coder:14b, qwen3:14b, deepseek-r1:14b | 9.0-9.3 GB | swaps - unusable |
| gpt-oss:20b | 13.8 GB | far too big |
| devstral:24b | 14.3 GB | far too big |
| qwen3-coder:30b | 18.6 GB | far too big |

14B+ is not "slow but workable" here - it swaps to disk and effectively stops.

### Two measured surprises

**deepseek-r1:8b beat qwen3:8b decisively.** Same accuracy, less than half the
time on the bug-fix (223s vs 509s). qwen3:8b is kept only as a fallback; nothing
uses it.

**DeepSeek's own recommended temperature made it 9x slower.** The model card says
0.6. On this CPU-only machine 0.6 took 705s on the bug-fix and 0.25 took 75s -
same correct answer. Higher temperature buys rambling, and here rambling is
billed in minutes. The Modelfile pins 0.25 for that reason; do not "restore" it
to the documented value.

`reasoner` has no system prompt on purpose - R1-family models are degraded by one.


## Which model to actually use

Benchmarked on the three cases the local 7B models get wrong. Free tier only.

| Model | Score | Speed | Use for |
|---|---|---|---|
| `cohere/north-mini-code:free` | **3/3** | 2-10s | **default** - coding + reasoning |
| `minimax/minimax-m3:free` | **3/3** | 3-46s | long context (1M) |
| `poolside/laguna-s-2.1:free` | **3/3** | 12-25s | coding |
| `gemini-3.5-flash` | **3/3** | 8-18s | not on OpenRouter's 50/day cap |
| `nemotron-3-ultra-550b-a55b:free` | 2/3* | 12-24s | 550B, 1M ctx - flaky upstream |
| local `reasoner` | 3/3 | 75-144s | offline / private only |
| local `assistant` | 1/3 | 22-46s | offline / private only |

\* both NVIDIA failures were "Service temporarily overloaded", not wrong answers -
they passed every coding test they actually ran.

`ui.default_models` is now `cohere/north-mini-code:free`. New chats start there.

**The headline: the free cloud models beat your local models on accuracy AND are
10-20x faster.** The local pair's job is offline and private work, not quality.

Excluded as consistently broken: `z-ai/glm-5.2:free` and `google/gemma-4-31b-it:free`
returned provider errors on every attempt.


## Removed models and why

Every remaining model is benchmarked, not merely reachable. Each was tested on
the reasoning and coding cases the local 7B models fail.

**Cloud - removed (blocklisted in `configure_providers.py`):**

| Model | Evidence |
|---|---|
| `nvidia/nemotron-3-ultra-550b-a55b:free` | retest 0/2: aborted at 301s, provider error at 291s |
| `models/gemini-flash-latest` | quota exceeded on all 4 attempts across two runs |
| `models/gemini-3.7-flash` | 1/3 then 0/2 - quota exceeded / high demand |

Earlier drops: `z-ai/glm-5.2:free` and `google/gemma-4-31b-it:free` errored on
every probe attempt (gemma-4-31b later recovered via the Gemini endpoint and is
kept there - the OpenRouter route was the broken one).

The blocklist is explicit so a future re-probe cannot silently reintroduce them.

**Kept despite a failure:** `nvidia/nemotron-3-super-120b-a12b:free` passed both
coding tests and one reasoning test, failing only on "service temporarily
overloaded". That is availability, not capability - it answers in 3s when the
provider is up.

**Local - removed:** `qwen3:8b`. Same accuracy as `deepseek-r1:8b` but 509s vs
223s on the bug-fix, and nothing referenced it. Reclaimed 5.2 GB.

### Final inventory

- **18 cloud models**, 9 OpenRouter free + 9 Gemini free tier, all benchmarked
- **4 local models**: `coder`, `assistant`, `reasoner`, plus the two base models
  they are built from
- Default for new chats: `cohere/north-mini-code:free`
