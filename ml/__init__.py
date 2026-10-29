"""Machine-learning support: turn audits into a dataset, then a model.

The split of responsibility is deliberate:

* :mod:`ml.features`   - the feature contract (what a row *is*).
* :mod:`ml.dataset`    - persistence + export of ``training_rows``.
* :mod:`ml.train`      - offline training (needs the optional ML extras).
* :mod:`ml.predictor`  - loads an artifact and answers one question per token.

Nothing here is imported by the hot path unless a predictor is enabled, and the
default configuration keeps the LLM as the authority, so enabling this module is
an explicit, reversible environment change.
"""

from __future__ import annotations

__all__ = ["FEATURE_SCHEMA_VERSION"]


#: Bumped whenever :func:`ml.features.flatten` changes meaning (not merely adds a
#: column). Every training row carries it, so a model trained on an older layout
#: is visibly stale instead of silently mis-aligned.
FEATURE_SCHEMA_VERSION = 1
