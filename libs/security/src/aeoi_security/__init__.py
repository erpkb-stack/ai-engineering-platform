"""Security helpers: building blocks, not the whole defence (architecture.md §15-16)."""

from aeoi_security.auth import AuthError, Principal, issue_token, verify_token
from aeoi_security.rbac import ROLE_PERMISSIONS, Perm, Role, permissions_for
from aeoi_security.redaction import REDACTED, redact_mapping, redact_text
from aeoi_security.untrusted import UNTRUSTED_TAG, wrap_untrusted

__all__ = [
    "REDACTED",
    "ROLE_PERMISSIONS",
    "UNTRUSTED_TAG",
    "AuthError",
    "Perm",
    "Principal",
    "Role",
    "issue_token",
    "permissions_for",
    "redact_mapping",
    "redact_text",
    "verify_token",
    "wrap_untrusted",
]
