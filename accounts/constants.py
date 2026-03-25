"""
Shared auth-related constants.

Used when an admin resets a consumer password to a known temporary value.
Must satisfy Django's AUTH_PASSWORD_VALIDATORS (aligned with seed_consumers).
"""

CONSUMER_DEFAULT_RESET_PASSWORD = "Password123!"
