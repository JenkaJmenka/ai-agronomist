"""Round-trip and encoding tests for MQ message contracts."""

import pytest

from src.mq.publisher import GZIP_THRESHOLD_BYTES, decode_message_body, encode_message_body
from src.mq.schemas import (
    OnlineTrainingPortionMessage,
    RetrainJobMessage,
    UploadFinalizeMessage,
    UploadPortionMessage,
)


def _portion_message(rows=None) -> UploadPortionMessage:
    return UploadPortionMessage(
        job_id="job-1",
        tenant_id="tenant_1",
        greenhouse_id="default",
        portion_idx=0,
        portion_count=2,
        rows=rows or [{"timestamp": "2026-07-03T10:00:00+00:00", "temperature": 24.5}],
        overlap_rows=[],
        stat_carry_rows=[],
        csv_props={"sep": ",", "encoding": "utf-8"},
        adaptive_ranges=[[19.0, 31.0], [45.0, 85.0], [350.0, 550.0], [0.0, 1100.0]],
        checkpoint_path=None,
        persist_data=True,
    )


class TestSchemaRoundTrips:
    def test_upload_portion_round_trip(self):
        msg = _portion_message()
        restored = UploadPortionMessage.model_validate_json(msg.model_dump_json())
        assert restored == msg

    def test_finalize_round_trip(self):
        msg = UploadFinalizeMessage(job_id="job-9")
        assert UploadFinalizeMessage.model_validate_json(msg.model_dump_json()) == msg

    def test_online_training_round_trip(self):
        msg = OnlineTrainingPortionMessage(
            job_id="job-2",
            tenant_id="tenant_1",
            portion_idx=1,
            portion_count=4,
            sequences=[[[0.1] * 4] * 20],
        )
        restored = OnlineTrainingPortionMessage.model_validate_json(msg.model_dump_json())
        assert restored == msg

    def test_retrain_round_trip_defaults(self):
        msg = RetrainJobMessage(job_id="job-3", tenant_id="tenant_1")
        restored = RetrainJobMessage.model_validate_json(msg.model_dump_json())
        assert restored.epochs == 5
        assert restored.portion_rows == 10000


class TestBodyEncoding:
    def test_small_body_is_plain_json(self):
        body, encoding = encode_message_body(UploadFinalizeMessage(job_id="job-1"))
        assert encoding is None
        assert b"job-1" in body

    def test_large_body_is_gzipped_and_decodable(self):
        rows = [
            {"timestamp": f"2026-07-03T10:{i % 60:02d}:00+00:00", "temperature": 20.0 + i}
            for i in range(5000)
        ]
        msg = _portion_message(rows=rows)
        body, encoding = encode_message_body(msg)
        assert encoding == "gzip"
        assert len(body) < GZIP_THRESHOLD_BYTES * 4  # sanity: compression happened

        decoded = decode_message_body(body, encoding)
        restored = UploadPortionMessage.model_validate_json(decoded)
        assert len(restored.rows) == 5000
