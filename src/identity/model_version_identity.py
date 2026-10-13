"""
Model-version-identity adapter (Phase 2, Gate 2).

INVENTORY (Gate 2 investigation)

v15 has no explicit model-version value anywhere in the notebook -- no
version string, no config file, no artifact registry. The only real
"configuration" that exists is the block of hardcoded formula constants
inside `build_composite_radar_v9` (cell 207), directly confirmed by
reading that cell during the OVR/radar batch (Phase 2B batch 6) --
``SCORING_EXPONENT = 1.35``, ``SHOOTING_EXPONENT = 1.55``,
``BENCH_3FG = 0.360``, ``MIN_3PA_THRESHOLD = 0.3``,
``GUARD_BLOCK_THRESHOLD = 0.30``, ``GUARD_BLOCK_WEIGHT_MULT = 0.25``,
``GUARD_REB_WEIGHT_MULT = 0.50``, ``WORKHORSE_MIN_THRESHOLD = 28.0``,
``WORKHORSE_MIN_ELITE = 32.0``, ``MAX_WORKHORSE_BOOST = 3.0``,
``RIM_PROTECTOR_THRESHOLD = 1.50``, ``RIM_PROTECTOR_ELITE = 1.75``,
``MAX_RIM_BOOST = 0.03``, ``BADGE_THRESHOLD = 1.50``. This is the
representative subset this migration has direct evidence for -- not a
claim that every constant in that ~4700-line function is captured here
(re-reading it in full is out of scope this batch, per instruction).

DESIGN

Per instruction ("if v15 has no explicit model-version value, establish a
documented legacy identifier without changing calculations" / "a version
ID may use an explicit version or deterministic configuration/artifact
fingerprint"):

1. `LEGACY_V15_RADAR_CONFIG` -- the confirmed constant snapshot above,
   exactly as it appears in the notebook. Not modified, not recalculated.
2. `compute_model_version_id(label, config)` -- hashes a sorted, JSON-
   stable serialization of `config` (via `sort_keys=True`, so insertion
   order never affects the result) into a stable id, namespaced by
   `label` -- two different configurations, or the same configuration
   under two different labels, never silently share one id.
3. `LEGACY_V15_MODEL_VERSION` -- the one legacy `ModelVersionIdentity`
   this migration establishes for "whatever the notebook's composite
   radar/OVR formula currently computes," built from the config above.

Never derived from a file path, mtime, DataFrame iteration order, or a
runtime timestamp -- only from the named constant values.
"""
from __future__ import annotations

import hashlib
import json

from src.identity.contracts import ModelVersionIdentity
from src.identity.stable_id import stable_namespaced_id
from src.logging_config import get_logger

log = get_logger(__name__)

#: Confirmed directly from notebook cell 207 (`build_composite_radar_v9`)
#: during Phase 2B batch 6 -- a representative subset, not exhaustive.
LEGACY_V15_RADAR_CONFIG: dict[str, float] = {
    "SCORING_EXPONENT": 1.35,
    "SHOOTING_EXPONENT": 1.55,
    "BENCH_3FG": 0.360,
    "MIN_3PA_THRESHOLD": 0.3,
    "GUARD_BLOCK_THRESHOLD": 0.30,
    "GUARD_BLOCK_WEIGHT_MULT": 0.25,
    "GUARD_REB_WEIGHT_MULT": 0.50,
    "WORKHORSE_MIN_THRESHOLD": 28.0,
    "WORKHORSE_MIN_ELITE": 32.0,
    "MAX_WORKHORSE_BOOST": 3.0,
    "RIM_PROTECTOR_THRESHOLD": 1.50,
    "RIM_PROTECTOR_ELITE": 1.75,
    "MAX_RIM_BOOST": 0.03,
    "BADGE_THRESHOLD": 1.50,
}


def _config_fingerprint(config: dict) -> str:
    """Deterministic fingerprint: sorted-key JSON, independent of dict insertion order."""
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def compute_model_version_id(label: str, config: dict) -> ModelVersionIdentity:
    """
    Build a `ModelVersionIdentity` from an explicit `label` plus a
    calculation-configuration dict. The id is namespaced by both the
    label AND the config's own fingerprint, so:
      - the same label with a different config never collides with an
        earlier version under that label (a real formula change gets a
        new id, satisfying "different calculation configurations must
        not silently share one version id"), and
      - the same (label, config) pair always reproduces the identical id,
        regardless of how `config`'s keys were inserted.
    """
    fingerprint = _config_fingerprint(config)
    return ModelVersionIdentity(
        model_version_id=stable_namespaced_id("model_version", f"{label}:{fingerprint}"),
        label=label,
        config_fingerprint=fingerprint,
    )


#: The one legacy model-version identity this migration establishes for
#: v15's current, unmodified radar/OVR formula.
LEGACY_V15_MODEL_VERSION: ModelVersionIdentity = compute_model_version_id(
    "legacy_v15_composite_radar", LEGACY_V15_RADAR_CONFIG
)
