"""An invalid validation must not strip the record's own flags (the delivery's `test_adapter`), only
this stage's `gen_test` verdict."""

from susvibes.curate.validate.utils import apply_validate_report
from susvibes.curate.validate.constants import ValidateStatus


def test_invalid_keeps_record_flags() -> None:
    record = {"instance_id": "x", "flags": {"test_adapter": True, "gen_test": True},
              "expected_pf": {"func": 0, "sec": 0}, "image_name": "eval_x", "keep": {}}
    apply_validate_report({"validate_status": ValidateStatus.INVALID, "reason": "no"}, record, {}, {})
    assert record["flags"] == {"test_adapter": True}
    assert "expected_pf" not in record and "image_name" not in record
    assert record["keep"]["validate"] is False


def test_invalid_drops_flags_that_were_only_gen_test() -> None:
    record = {"instance_id": "x", "flags": {"gen_test": True}, "keep": {}}
    apply_validate_report({"validate_status": ValidateStatus.INVALID, "reason": "no"}, record, {}, {})
    assert "flags" not in record


def test_validated_takes_report_flags() -> None:
    record = {"instance_id": "x", "flags": {"test_adapter": True}, "keep": {}}
    report = {"validate_status": ValidateStatus.VALIDATED, "flags": {"test_adapter": True, "gen_test": True},
              "expected_pf": {"func": 0, "sec": 0}, "image_name": "eval_x", "logs_handler": {}, "stats": {}}
    apply_validate_report(report, record, {}, {})
    assert record["flags"] == {"test_adapter": True, "gen_test": True} and record["keep"]["validate"] is True
