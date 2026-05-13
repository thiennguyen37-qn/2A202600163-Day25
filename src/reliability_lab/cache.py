from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory cache with TTL, semantic similarity, and false-hit guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0

        best_value: str | None = None
        best_score = 0.0
        best_key: str | None = None
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]

        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_value = entry.value
                best_key = entry.key

        if best_score >= self.similarity_threshold and best_key is not None:
            if _looks_like_false_hit(query, best_key):
                return None, best_score
            return best_value, best_score

        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Similarity via exact match + token Jaccard + character bigram overlap."""
        a_norm = a.lower().strip()
        b_norm = b.lower().strip()
        if a_norm == b_norm:
            return 1.0

        left = set(a_norm.split())
        right = set(b_norm.split())
        if not left or not right:
            return 0.0
        token_score = len(left & right) / len(left | right)

        def bigrams(s: str) -> set[str]:
            return {s[i : i + 2] for i in range(len(s) - 1)}

        bg_a, bg_b = bigrams(a_norm), bigrams(b_norm)
        bigram_score = len(bg_a & bg_b) / len(bg_a | bg_b) if bg_a and bg_b else 0.0

        return 0.6 * token_score + 0.4 * bigram_score


# ---------------------------------------------------------------------------
# Redis shared cache
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis (exact match, then similarity scan)."""
        if _is_uncacheable(query):
            return None, 0.0

        try:
            # Exact match via hash key
            key = f"{self.prefix}{self._query_hash(query)}"
            response = self._redis.hget(key, "response")
            if response is not None:
                return response, 1.0

            # Similarity scan over all keys with this prefix
            best_value: str | None = None
            best_score = 0.0
            best_query: str | None = None

            for k in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(k, "query")
                if cached_query is None:
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_score = score
                    best_value = self._redis.hget(k, "response")
                    best_query = cached_query

            if best_score >= self.similarity_threshold and best_query is not None:
                if _looks_like_false_hit(query, best_query):
                    self.false_hit_log.append(
                        {"query": query, "matched": best_query, "score": best_score}
                    )
                    return None, best_score
                return best_value, best_score

            return None, best_score

        except Exception:
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL. No-op for uncacheable queries."""
        if _is_uncacheable(query):
            return
        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            self._redis.hset(key, mapping={"query": query, "response": value})
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            pass  # Graceful degradation: Redis down → skip caching, don't crash

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
