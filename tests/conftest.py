"""Shared pytest configuration.

Sets a JWT secret for the test session BEFORE any ``leaksentinel`` module is
imported, so token minting/verification works and the settings cache picks it
up. Bootstrap of the first admin is disabled (no FIRST_ADMIN_PASSWORD) — the
API tests create their own users explicitly.
"""

from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production")
os.environ.setdefault("JWT_EXPIRY_HOURS", "8")

from leaksentinel.config import get_settings  # noqa: E402

# In case anything imported settings before this ran, drop the cached instance
# so the JWT secret above is honored.
get_settings.cache_clear()
