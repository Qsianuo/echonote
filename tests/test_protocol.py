"""RG-04: protocol v3 contract.

Every message the server can emit must be constructible, valid, and reject
the mistakes we actually made before (missing codes, reset ids, end < start).
"""

from __future__ import annotations

import pytest

from app import protocol


def test_status_carries_protocol_version():
    message = protocol.status(ready=True, model="small")
    assert message["protocol"] == protocol.PROTOCOL_VERSION
    assert protocol.validate(message) is message


def test_status_allows_null_error_and_flag_fields():
    message = protocol.status(
        ready=False, model="small", error=None, retrying=False, attempts=2
    )
    protocol.validate(message)
    assert message["error"] is None


def test_partial_may_be_empty_to_clear_the_line():
    message = protocol.partial(text="", epoch=1, start=0.0)
    protocol.validate(message)
    assert message["text"] == ""


def test_final_id_is_epoch_scoped():
    message = protocol.final(final_id="2-3", epoch=2, text="你好", start=1.2, end=4.08)
    protocol.validate(message)
    assert message["id"] == "2-3"


@pytest.mark.parametrize("bad_id", ["3", "2-0", "0-1", "a-1", "2-"])
def test_final_rejects_malformed_ids(bad_id: str):
    message = protocol.final(final_id=bad_id, epoch=2, text="x", start=0.0, end=1.0)
    with pytest.raises(protocol.ProtocolError):
        protocol.validate(message)


def test_final_rejects_end_before_start():
    message = protocol.final(final_id="1-1", epoch=1, text="x", start=5.0, end=4.0)
    with pytest.raises(protocol.ProtocolError):
        protocol.validate(message)


def test_final_rejects_empty_text():
    message = protocol.final(final_id="1-1", epoch=1, text="", start=0.0, end=1.0)
    with pytest.raises(protocol.ProtocolError):
        protocol.validate(message)


def test_unknown_error_code_is_rejected_at_construction():
    with pytest.raises(protocol.ProtocolError):
        protocol.error(code="made_up", message="x")


def test_all_declared_error_codes_are_valid():
    for code in protocol.ERROR_CODES:
        protocol.validate(protocol.error(code=code, message="x", retryable=False))


def test_warning_requires_a_whitelisted_code():
    protocol.validate(protocol.warning(code="audio_backlog_dropped", dropped_ms=320))
    with pytest.raises(protocol.ProtocolError):
        protocol.warning(code="dropped_something", dropped_ms=1)


def test_validate_rejects_unknown_type():
    with pytest.raises(protocol.ProtocolError):
        protocol.validate({"type": "surprise"})


def test_validate_rejects_bool_where_number_expected():
    message = protocol.partial(text="x", epoch=1, start=0.0)
    message["start"] = True
    with pytest.raises(protocol.ProtocolError):
        protocol.validate(message)


def test_done_is_minimal():
    assert protocol.done() == {"type": "done"}


# --------------------------------------------------------------------------
# v3: provisional finals and refinement
# --------------------------------------------------------------------------


def test_final_is_stable_by_default():
    message = protocol.final(final_id="1-1", epoch=1, text="你好", start=0.0, end=1.0)
    assert message["stable"] is True
    protocol.validate(message)


def test_provisional_final_is_marked():
    message = protocol.final(
        final_id="1-2", epoch=1, text="你好", start=0.0, end=1.0, stable=False
    )
    assert message["stable"] is False
    protocol.validate(message)


def test_refine_carries_the_same_id_shape_as_final():
    message = protocol.refine(final_id="1-2", epoch=1, text="精修", start=0.0, end=1.0)
    protocol.validate(message)
    assert message["type"] == "refine"
    assert message["id"] == "1-2"


@pytest.mark.parametrize("bad_id", ["3", "2-0", "a-1"])
def test_refine_rejects_malformed_ids(bad_id: str):
    message = protocol.refine(final_id=bad_id, epoch=2, text="x", start=0.0, end=1.0)
    with pytest.raises(protocol.ProtocolError):
        protocol.validate(message)


def test_refine_rejects_empty_text():
    message = protocol.refine(final_id="1-1", epoch=1, text="", start=0.0, end=1.0)
    with pytest.raises(protocol.ProtocolError):
        protocol.validate(message)


def test_refine_backlog_warning_is_whitelisted():
    protocol.validate(protocol.warning(code="refine_backlog_dropped", dropped_ms=0))
