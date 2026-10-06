"""Security helpers: building blocks, not the whole defence (architecture.md §15-16)."""

from aeoi_security.auth import AuthError, Principal, issue_service_token, issue_token, verify_token
from aeoi_security.injection import detect_injection
from aeoi_security.pii import ScrubResult, scrub_pii
from aeoi_security.rbac import ROLE_PERMISSIONS, Perm, Role, permissions_for
from aeoi_security.redaction import REDACTED, redact_mapping, redact_text, redact_text_counted
from aeoi_security.untrusted import UNTRUSTED_TAG, wrap_untrusted

__all__ = [
    "REDACTED",
    "ROLE_PERMISSIONS",
    "UNTRUSTED_TAG",
    "AuthError",
    "Perm",
    "Principal",
    "Role",
    "ScrubResult",
    "detect_injection",
    "issue_service_token",
    "issue_token",
    "permissions_for",
    "redact_mapping",
    "redact_text",
    "redact_text_counted",
    "scrub_pii",
    "verify_token",
    "wrap_untrusted",
]
