"""非秘密报价配置严格解析；字段边界不能静默变成业务默认。"""

import importlib
import json
from copy import deepcopy

import pytest

from tests.quotation_runtime_fixtures import quotation_settings_values


def configuration():
    assert importlib.util.find_spec("infra.quotation_settings") is not None, (
        "缺少显式报价配置解析"
    )
    return importlib.import_module("infra.quotation_settings")


def leaf_paths(value, prefix=()):
    result = []
    for key, item in value.items():
        result.extend(
            leaf_paths(item, (*prefix, key))
            if isinstance(item, dict)
            else [(*prefix, key)]
        )
    return result


PATHS = leaf_paths(quotation_settings_values())
INTEGER_PATHS = [path for path in PATHS if path[-1] != "template_version"]


def parent_for(values, path):
    for key in path[:-1]:
        values = values[key]
    return values


@pytest.mark.parametrize("files_enabled", [True, False])
def test_configuration_reuses_real_dtos_and_all_fields_are_explicit(files_enabled):
    from infra.quote_evidence_settings import QuoteEvidenceSettings
    from shared.schemas.evidence_read import ObjectReadLimits
    from shared.schemas.generated_documents import QuotePdfWriteLimits
    from tool_gateway.file_rate_limit import QuoteFileRateLimits

    values = quotation_settings_values()
    if not files_enabled:
        values["files"] = None
    result = configuration().from_mapping(values)
    assert result.model_dump() == values
    assert isinstance(result.evidence, QuoteEvidenceSettings)
    if files_enabled:
        assert isinstance(result.files.object_read, ObjectReadLimits)
        assert isinstance(result.files.object_write, QuotePdfWriteLimits)
        assert isinstance(result.files.rate_limit, QuoteFileRateLimits)
    with pytest.raises(ValueError):
        result.core.maximum_page_size = 1


@pytest.mark.parametrize("path", INTEGER_PATHS, ids=lambda p: ".".join(p))
@pytest.mark.parametrize("invalid", [0, -1, True, "1"])
def test_every_integer_budget_rejects_nonpositive_bool_and_string(path, invalid):
    module = configuration()
    values = quotation_settings_values()
    parent_for(values, path)[path[-1]] = invalid
    with pytest.raises(module.QuotationConfigurationError, match="报价运行配置无效"):
        module.from_mapping(values)


@pytest.mark.parametrize(
    "path", PATHS + [("core",), ("evidence",), ("files",)], ids=lambda p: ".".join(p)
)
def test_every_leaf_and_top_level_group_is_required(path):
    module = configuration()
    values = quotation_settings_values()
    del parent_for(values, path)[path[-1]]
    with pytest.raises(module.QuotationConfigurationError):
        module.from_mapping(values)


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("core",),
        ("evidence",),
        ("evidence", "parser"),
        ("evidence", "probe"),
        ("files",),
        ("files", "object_read"),
        ("files", "object_write"),
        ("files", "rate_limit"),
    ],
)
def test_unknown_fields_fail_with_fixed_safe_error(path):
    module = configuration()
    values = quotation_settings_values()
    parent_for(values, (*path, "unknown"))["unknown"] = "controlled-private-marker"
    with pytest.raises(module.QuotationConfigurationError) as error:
        module.from_mapping(values)
    assert "controlled-private-marker" not in str(error.value)


@pytest.mark.parametrize(
    "path,value",
    [
        (("core", "lock_timeout_ms"), 2147483648),
        (("core", "statement_timeout_ms"), 2147483648),
        (("core", "maximum_page_size"), 2147483648),
        (("core", "expiry_batch_limit"), 1001),
        (("files", "gateway_lease_seconds"), 86401),
        (("files", "object_write", "maximum_attempts"), 2),
        (("files", "template_version"), ""),
        (("files", "template_version"), "quote_pdf_future"),
    ],
)
def test_explicit_upper_bounds_and_only_registered_template(path, value):
    module = configuration()
    values = quotation_settings_values()
    parent_for(values, path)[path[-1]] = value
    with pytest.raises(module.QuotationConfigurationError):
        module.from_mapping(values)


def test_core_database_expiry_and_gateway_maxima_are_accepted_exactly():
    values = quotation_settings_values()
    values["core"] = {
        "lock_timeout_ms": 2147483647,
        "statement_timeout_ms": 2147483647,
        "maximum_page_size": 2147483647,
        "expiry_batch_limit": 1000,
    }
    values["files"]["gateway_lease_seconds"] = 86400
    assert configuration().from_mapping(values).model_dump() == values


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "null",
        "[]",
        '{"core":1,"core":2}',
        '{"core":NaN}',
        '{"core":{"lock_timeout_ms":1,"lock_timeout_ms":2}}',
    ],
)
def test_json_rejects_duplicate_keys_nonfinite_and_nonobject(raw):
    module = configuration()
    with pytest.raises(module.QuotationConfigurationError):
        module.from_json(raw)


def test_json_roundtrip_uses_same_pure_mapping_contract():
    module = configuration()
    values = quotation_settings_values()
    assert module.from_json(json.dumps(values)) == module.from_mapping(deepcopy(values))


def test_api_environment_absence_preserves_old_configuration():
    from apps.api.runtime_config import Phase1RuntimeSettings
    from tests.unit.test_api_runtime_config import _VALID_ENV

    result = Phase1RuntimeSettings.from_environ(_VALID_ENV)
    assert hasattr(result, "quotation"), "API尚未声明可选报价配置"
    assert result.quotation is None


@pytest.mark.parametrize(
    "raw", ["", "null", "[]", '{"core":1,"core":2}', '{"core":NaN}']
)
def test_api_explicit_bad_configuration_never_downgrades_to_disabled(raw):
    from apps.api.runtime_config import Phase1RuntimeSettings, RuntimeConfigurationError
    from tests.unit.test_api_runtime_config import _VALID_ENV

    with pytest.raises(RuntimeConfigurationError) as error:
        Phase1RuntimeSettings.from_environ(
            {**_VALID_ENV, "TRADEOS_QUOTATION_SETTINGS_JSON": raw}
        )
    assert error.value.field_name == "TRADEOS_QUOTATION_SETTINGS_JSON"


def test_api_explicit_configuration_keeps_exact_budget_and_files_disabled():
    from apps.api.runtime_config import Phase1RuntimeSettings
    from tests.unit.test_api_runtime_config import _VALID_ENV

    values = quotation_settings_values()
    values["files"] = None
    result = Phase1RuntimeSettings.from_environ(
        {**_VALID_ENV, "TRADEOS_QUOTATION_SETTINGS_JSON": json.dumps(values)}
    )
    assert hasattr(result, "quotation"), "API尚未读取显式报价配置"
    assert result.quotation.model_dump() == values
