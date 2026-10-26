"""AlphaSentinel configuration package.

Import the singleton with::

    from config.settings import get_settings

Vendor URLs are not configuration *code*: they live in ``config/endpoints.yaml``
and are resolved by :mod:`config.endpoints` (override the file with
``ENDPOINTS_FILE`` or single entries with ``<VENDOR>_BASE_URL`` /
``<VENDOR>_<NAME>_URL``).

Nothing in this package performs network I/O at import time.
"""
