"""
title: Quota Monitor
author: local-llm-stack
version: 0.1.0
description: Shows remaining cloud quota in the chat UI and raises a clear
             notification when a provider's quota is exhausted or rate limited.
"""

import json
import re
import time
from typing import Optional

import aiohttp
from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        openrouter_api_key: str = Field(
            default="", description="OpenRouter API key. Used only to read the balance."
        )
        warn_below_usd: float = Field(
            default=1.0, description="Warn when remaining OpenRouter credit falls below this."
        )
        show_balance_every_message: bool = Field(
            default=False,
            description="If off, the balance is only shown when it is low or exhausted.",
        )
        cache_seconds: int = Field(
            default=120, description="How long to cache the balance lookup."
        )
        openrouter_max_tokens: int = Field(
            default=4096,
            description="Upper bound on max_tokens for OpenRouter requests. The real "
                        "cap is usually lower - see budget_usd.",
        )
        budget_usd: float = Field(
            default=0.036,
            description="What OpenRouter will currently fund for one completion. "
                        "affordable_tokens = budget_usd / model_completion_price, so "
                        "an expensive model gets a smaller cap than a cheap one. "
                        "Learned automatically from 402 errors. Raise after adding credits.",
        )
        safety_fraction: float = Field(
            default=0.8, description="Fraction of the affordable ceiling to actually request."
        )
        clamp_max_tokens: bool = Field(
            default=True, description="Enable the max_tokens cap above."
        )

    def __init__(self):
        self.valves = self.Valves()
        self._cache = {"at": 0.0, "data": None}
        self._pricing = {}          # model id -> completion price per token
        self._pricing_at = 0.0

    # ---------- helpers ----------

    async def _emit(self, emitter, description: str, done: bool = True):
        if emitter:
            await emitter(
                {"type": "status", "data": {"description": description, "done": done}}
            )

    async def _notify(self, emitter, content: str, kind: str = "warning"):
        if emitter:
            await emitter({"type": "notification", "data": {"type": kind, "content": content}})

    async def _completion_price(self, model: str) -> Optional[float]:
        """Per-token completion price for an OpenRouter model id."""
        now = time.time()
        if not self._pricing or now - self._pricing_at > 3600:
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout) as s:
                    async with s.get(f"{'https://openrouter.ai/api/v1'}/models") as r:
                        if r.status == 200:
                            data = (await r.json()).get("data", [])
                            self._pricing = {
                                x["id"]: float(x["pricing"]["completion"])
                                for x in data
                                if (x.get("pricing") or {}).get("completion")
                            }
                            self._pricing_at = now
            except Exception:
                return None
        return self._pricing.get(model)

    @staticmethod
    def _bare_model(model_id: str) -> str:
        """Strips Open WebUI's connection prefix: openrouter.foo/bar -> foo/bar"""
        return model_id.split(".", 1)[1] if model_id.startswith("openrouter.") else model_id

    async def _openrouter_balance(self) -> Optional[dict]:
        """Returns {'usage','limit','remaining','is_free_tier'} or None."""
        key = (self.valves.openrouter_api_key or "").strip()
        if not key:
            return None

        now = time.time()
        if self._cache["data"] and now - self._cache["at"] < self.valves.cache_seconds:
            return self._cache["data"]

        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(
                    "https://openrouter.ai/api/v1/key",
                    headers={"Authorization": f"Bearer {key}"},
                ) as r:
                    if r.status != 200:
                        return None
                    payload = (await r.json()).get("data", {})
        except Exception:
            return None

        limit = payload.get("limit")
        usage = payload.get("usage") or 0.0
        remaining = payload.get("limit_remaining")
        if remaining is None and limit is not None:
            remaining = limit - usage

        data = {
            "usage": usage,
            "limit": limit,
            "remaining": remaining,
            "is_free_tier": payload.get("is_free_tier", False),
        }
        self._cache = {"at": now, "data": data}
        return data

    @staticmethod
    def _provider_of(model_id: str) -> str:
        m = (model_id or "").lower()
        if m.startswith("openrouter"):
            return "openrouter"
        if m.startswith("gemini") or "generativelanguage" in m:
            return "gemini"
        return "local"

    @staticmethod
    def _quota_error(body) -> Optional[str]:
        """Detects provider quota/rate-limit errors.

        Only inspects a structured `error` object. Scanning the whole payload
        produced false positives: an ordinary reply containing the digits
        "429" or "402" was flagged as rate limiting.
        """
        # Gemini returns errors as a single-element JSON *array*, not an object.
        if isinstance(body, list):
            body = body[0] if body else None
        if not isinstance(body, dict):
            return None
        err = body.get("error")
        if err is None:
            return None

        if isinstance(err, str):
            err, text, code = {}, err.lower(), None
        elif isinstance(err, dict):
            text = " ".join(
                str(err.get(k, "")) for k in ("message", "status", "type", "code")
            ).lower()
            code = err.get("code")
        else:
            return None

        # Check this BEFORE the bare status codes: OpenRouter returns 402 for
        # "max_tokens too large for your balance", which is a different problem
        # from "out of credits" and has a different fix.
        # OpenRouter's free tier is capped per DAY (50 requests at 0 credits),
        # which is a different problem from being out of credits - it fixes
        # itself at the reset time.
        if "free-models-per-day" in text or "free_tier_daily" in text:
            reset = ""
            try:
                hdrs = (err.get("metadata") or {}).get("headers") or {}
                ms = int(hdrs.get("X-RateLimit-Reset", 0))
                if ms:
                    hrs = max(0, (ms / 1000 - time.time()) / 3600)
                    reset = " Resets in ~%.0fh." % hrs
            except Exception:
                pass
            return ("OpenRouter free-model daily cap reached (50/day).%s "
                    "Gemini and your local models are unaffected" % reset)

        afford = re.search(r"can only afford (\d+)", text)
        if afford:
            return "max_tokens too high for your balance (can afford %s tokens)" % afford.group(1)

        if code in (402, "402"):
            return "credits exhausted (HTTP 402)"
        if code in (429, "429"):
            return "rate limited or out of quota (HTTP 429)"

        for needle, label in (
            ("resource_exhausted", "quota exhausted"),
            ("resource has been exhausted", "quota exhausted"),
            ("insufficient_quota", "quota exhausted"),
            ("insufficient credits", "credits exhausted"),
            ("quota exceeded", "quota exceeded"),
            ("exceeded your current quota", "quota exceeded"),
            ("rate limit exceeded", "rate limited"),
            ("rate_limit_exceeded", "rate limited"),
        ):
            if needle in text:
                return label
        return None

    # ---------- filter hooks ----------

    async def inlet(self, body: dict, __event_emitter__=None, __user__=None) -> dict:
        try:
            model = body.get("model", "")
            provider = self._provider_of(model)
            if provider != "openrouter":
                return body

            # ":free" models cost nothing, so no affordability ceiling applies.
            # Verified: they accept max_tokens=32768 without a 402.
            if self._bare_model(model).endswith(":free"):
                return body

            # --- clamp max_tokens ---
            if self.valves.clamp_max_tokens:
                cap = self.valves.openrouter_max_tokens
                price = await self._completion_price(self._bare_model(model))
                if price and self.valves.budget_usd > 0:
                    afford = int(self.valves.budget_usd / price * self.valves.safety_fraction)
                    cap = max(256, min(cap, afford))
                requested = body.get("max_tokens")
                if requested is None or requested > cap:
                    body["max_tokens"] = cap
                    if requested is not None:
                        await self._emit(
                            __event_emitter__,
                            f"max_tokens {requested} -> {cap} (credit limit)",
                        )

            bal = await self._openrouter_balance()
            if not bal:
                return body

            remaining, limit = bal["remaining"], bal["limit"]

            if remaining is None:
                if self.valves.show_balance_every_message:
                    await self._emit(
                        __event_emitter__, f"OpenRouter: ${bal['usage']:.2f} used (no cap set)"
                    )
                return body

            if remaining <= 0:
                msg = "OpenRouter quota exhausted - this request will fail. Switch to a local model."
                await self._emit(__event_emitter__, msg)
                await self._notify(__event_emitter__, msg, "error")
            elif remaining < self.valves.warn_below_usd:
                msg = f"OpenRouter running low: ${remaining:.2f} left of ${limit:.2f}"
                await self._emit(__event_emitter__, msg)
                await self._notify(__event_emitter__, msg, "warning")
            elif self.valves.show_balance_every_message:
                await self._emit(
                    __event_emitter__, f"OpenRouter: ${remaining:.2f} of ${limit:.2f} remaining"
                )
        except Exception:
            # Never break a chat because the meter failed.
            pass
        return body

    async def outlet(self, body: dict, __event_emitter__=None, __user__=None) -> dict:
        try:
            hit = self._quota_error(body)
            if hit:
                # body may be the array shape Gemini uses; only dicts have .get
                src = body[0] if isinstance(body, list) and body else body
                model = src.get("model", "") if isinstance(src, dict) else ""
                provider = self._provider_of(model)
                pretty = {"openrouter": "OpenRouter", "gemini": "Gemini"}.get(provider, "Provider")
                if "daily cap" in hit:
                    msg = hit + "."
                elif "max_tokens too high" in hit:
                    msg = (f"{pretty}: {hit}. Retry - the cap is now applied "
                           f"automatically. Add credits to raise it.")
                else:
                    msg = f"{pretty}: {hit}. Your local models still work - switch model to continue."
                await self._emit(__event_emitter__, msg)
                await self._notify(__event_emitter__, msg, "error")
                self._cache = {"at": 0.0, "data": None}  # force refresh next time
                m = re.search(r"can afford (\d+) tokens", hit)
                if m:
                    # affordable_tokens x price = the dollar budget, which is the
                    # model-independent quantity worth remembering.
                    price = await self._completion_price(self._bare_model(model))
                    if price:
                        self.valves.budget_usd = int(m.group(1)) * price
        except Exception:
            pass
        return body
