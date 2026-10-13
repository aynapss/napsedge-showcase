"""Phase 2, Gate 2 tests: model-version identity. No live requests, no retraining."""
from src.identity.contracts import ModelVersionIdentity
from src.identity.model_version_identity import (
    LEGACY_V15_MODEL_VERSION,
    LEGACY_V15_RADAR_CONFIG,
    compute_model_version_id,
)


def test_legacy_v15_model_version_is_established():
    assert isinstance(LEGACY_V15_MODEL_VERSION, ModelVersionIdentity)
    assert LEGACY_V15_MODEL_VERSION.label == "legacy_v15_composite_radar"
    assert LEGACY_V15_MODEL_VERSION.config_fingerprint is not None


def test_deterministic_repeated_computation():
    ids = {compute_model_version_id("test_config", {"a": 1, "b": 2}).model_version_id for _ in range(3)}
    assert len(ids) == 1


def test_independent_of_dict_insertion_order():
    a = compute_model_version_id("test_config", {"a": 1, "b": 2})
    b = compute_model_version_id("test_config", {"b": 2, "a": 1})
    assert a.model_version_id == b.model_version_id
    assert a.config_fingerprint == b.config_fingerprint


def test_different_configurations_get_different_ids():
    a = compute_model_version_id("test_config", {"weight": 1.35})
    b = compute_model_version_id("test_config", {"weight": 1.40})
    assert a.model_version_id != b.model_version_id


def test_same_config_different_label_gets_different_id():
    a = compute_model_version_id("v1", {"weight": 1.35})
    b = compute_model_version_id("v2", {"weight": 1.35})
    assert a.model_version_id != b.model_version_id


def test_not_derived_from_runtime_timestamp_or_path():
    """Calling twice, potentially at different wall-clock times, yields the same id."""
    import time

    a = compute_model_version_id("stable_check", LEGACY_V15_RADAR_CONFIG)
    time.sleep(0.01)
    b = compute_model_version_id("stable_check", LEGACY_V15_RADAR_CONFIG)
    assert a.model_version_id == b.model_version_id


def test_legacy_config_values_match_confirmed_notebook_constants():
    """Locks in the specific real values found by direct source reading (cell 207)."""
    assert LEGACY_V15_RADAR_CONFIG["SCORING_EXPONENT"] == 1.35
    assert LEGACY_V15_RADAR_CONFIG["SHOOTING_EXPONENT"] == 1.55
    assert LEGACY_V15_RADAR_CONFIG["BENCH_3FG"] == 0.360


def test_no_model_is_retrained_or_modified_by_this_module():
    import src.identity.model_version_identity as mod

    forbidden = {"fit", "train", "retrain", "save_model", "load_model"}
    assert not (forbidden & set(dir(mod)))
