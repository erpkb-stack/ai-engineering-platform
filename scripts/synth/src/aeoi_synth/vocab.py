"""Fictional vocabulary. No real company, product, person or internal term."""

from __future__ import annotations

COMPANY = "Northwind Cloud Systems"
EMAIL_DOMAIN = "northwind.example"

DOMAINS = (
    # order matters: services only depend on APIs of EARLIER domains (acyclic graph),
    # and checkout-api (the demo service) must come after its dependencies.
    "orders",
    "payments",
    "inventory",
    "cart",
    "checkout",
    "catalog",
    "search",
    "pricing",
    "shipping",
    "returns",
    "notifications",
    "identity",
    "accounts",
    "billing",
    "ledger",
    "fraud",
    "reviews",
    "media",
    "recommendations",
    "analytics",
    "loyalty",
    "promotions",
    "tax",
    "invoicing",
    "support",
    "chat",
    "scheduler",
    "reporting",
    "partner",
    "geo",
)
# Per domain we create several components; together they give 120+ services.
COMPONENTS = (
    ("api", "api"),
    ("worker", "worker"),
    ("db", "db"),
    ("cache", "cache"),
    ("events", "stream"),
)
LANGUAGES = {"api": ("java", "python", "go"), "worker": ("python", "java"), "stream": ("java",)}

FIRST_NAMES = (
    "Asha",
    "Bram",
    "Chen",
    "Dalia",
    "Emre",
    "Farah",
    "Goran",
    "Hana",
    "Ivo",
    "Jun",
    "Kiri",
    "Lena",
    "Mateo",
    "Nadia",
    "Oren",
    "Priya",
    "Quinn",
    "Rafa",
    "Sana",
    "Tomas",
    "Uma",
    "Viktor",
    "Wen",
    "Ximena",
    "Yara",
    "Zane",
)
LAST_NAMES = (
    "Okafor",
    "Lindqvist",
    "Moreau",
    "Tanaka",
    "Haddad",
    "Novak",
    "Silva",
    "Kowalski",
    "Mensah",
    "Ivanova",
    "Reyes",
    "Dubois",
    "Arslan",
    "Kaur",
    "Brennan",
    "Sato",
)

GROUPS = {
    "eng-all": "All engineers",
    "sre": "Site reliability engineering",
    "security-team": "Security engineering",
    "architecture-team": "Architecture review board",
    "incident-commanders": "Trained incident commanders",
    "management": "Engineering management",
}

# root-cause category -> (title template, symptom, error code, root cause, remediation)
ROOT_CAUSES: dict[str, tuple[str, str, str, str, str]] = {
    "connection_pool_exhaustion": (
        "{svc} returning HTTP 500 after deployment",
        "HTTP 5xx rose and latency increased; connection acquisition timeouts in logs",
        "ERR_POOL_TIMEOUT",
        "A code change opened one database connection per item, exhausting the pool.",
        "Rolled back the deployment; added a pool-usage alert and a load test for the code path.",
    ),
    "thread_pool_saturation": (
        "{svc} latency spike under normal load",
        "p95 latency rose, request queue grew, CPU stayed moderate",
        "ERR_EXECUTOR_REJECTED",
        "A blocking HTTP call ran on the request thread pool and saturated it.",
        "Moved the call to an async client with a timeout; sized the pool from load tests.",
    ),
    "memory_leak": (
        "{svc} pods restarting with OOMKilled",
        "Memory grew steadily for hours, then pods were OOM-killed",
        "ERR_OUT_OF_MEMORY",
        "An unbounded in-process cache grew without eviction.",
        "Added a size-bounded LRU cache and a memory growth alert.",
    ),
    "bad_config_change": (
        "{svc} errors after configuration change",
        "Errors began within a minute of a config push; no code deploy",
        "ERR_CONFIG_INVALID",
        "A config change set a timeout to 0, which the client treated as 'no wait'.",
        "Reverted the config; added schema validation for config pushes.",
    ),
    "dependency_timeout": (
        "{svc} degraded due to downstream timeouts",
        "Timeouts to a downstream service; retries amplified the load",
        "ERR_UPSTREAM_TIMEOUT",
        "A downstream dependency slowed down and aggressive retries caused a retry storm.",
        "Added retry budget and circuit breaker; coordinated with the dependency owner.",
    ),
    "certificate_expiry": (
        "{svc} TLS handshake failures",
        "All outbound calls failed with TLS errors at the same minute",
        "ERR_TLS_HANDSHAKE",
        "An internal certificate expired and was not rotated automatically.",
        "Rotated the certificate; added expiry monitoring 30 days ahead.",
    ),
    "disk_full": (
        "{svc} write failures",
        "Writes failed with 'no space left on device'",
        "ERR_DISK_FULL",
        "Log volume grew after a debug flag was left enabled.",
        "Disabled the flag, cleaned logs, added disk usage alerts.",
    ),
    "cache_stampede": (
        "{svc} database overload after cache flush",
        "Cache hit rate dropped to near zero and DB CPU hit 100%",
        "ERR_DB_OVERLOADED",
        "A cache flush made every request hit the database at once.",
        "Added request coalescing and staggered TTLs.",
    ),
    "schema_migration_lock": (
        "{svc} requests hanging during migration",
        "Requests blocked on a table lock during a schema migration",
        "ERR_LOCK_TIMEOUT",
        "A migration took an ACCESS EXCLUSIVE lock on a hot table.",
        "Rewrote the migration with expand/contract and lock_timeout.",
    ),
    "kafka_consumer_lag": (
        "{svc} processing delays",
        "Consumer lag grew for hours; downstream data was stale",
        "ERR_CONSUMER_LAG",
        "A poison message caused repeated retries and blocked the partition.",
        "Added a dead-letter queue and per-message retry limits.",
    ),
    "dns_failure": (
        "{svc} intermittent name resolution errors",
        "Random requests failed with name resolution errors",
        "ERR_DNS_RESOLUTION",
        "The DNS resolver cache was overloaded after a cluster upgrade.",
        "Scaled the resolver and added node-local DNS caching.",
    ),
    "feature_flag_regression": (
        "{svc} wrong results after flag rollout",
        "A feature flag rollout changed behaviour for a share of users",
        "ERR_VALIDATION_FAILED",
        "A new code path behind a flag skipped input validation.",
        "Turned the flag off; added contract tests for the new path.",
    ),
}

ERROR_CODES_BACKGROUND = (
    "ERR_UPSTREAM_TIMEOUT",
    "ERR_VALIDATION_FAILED",
    "ERR_NOT_FOUND",
    "ERR_RATE_LIMITED",
)
INFO_MESSAGES = (
    "request completed",
    "cache refreshed",
    "health check ok",
    "batch processed",
    "connection established",
    "job scheduled",
    "config loaded",
)
DOC_TYPES = {
    "architecture": "Architecture overview of {svc}",
    "api": "API reference: {svc}",
    "onboarding": "Onboarding guide for the {team} team",
    "postmortem": "Postmortem {inc}: {title}",
    "design": "Design note: {topic} in {svc}",
    "faq": "Operational FAQ: {svc}",
}
DESIGN_TOPICS = (
    "connection pooling",
    "retry policy",
    "idempotency keys",
    "caching strategy",
    "rate limiting",
    "schema evolution",
    "pagination",
    "timeouts",
    "circuit breakers",
)
