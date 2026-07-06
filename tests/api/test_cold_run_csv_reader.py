"""CSV chunk reader tolerates malformed rows (async upload path)."""

from src.api.services.cold_run_upload import _read_chunks


def test_read_chunks_skips_inconsistent_column_rows(tmp_path):
    csv_text = (
        "timestamp,temperature,humidity,co2,light,extra\n"
        "2024-01-01 00:00:00,22,65,400,500,a\n"
        "2024-01-01 01:00:00,23,66,401,501,b\n"
        "2024-01-01 02:00:00,24,67,402,502,c,d,e\n"
    )
    path = tmp_path / "sample.csv"
    path.write_text(csv_text, encoding="utf-8")
    props = {"sep": ",", "encoding": "utf-8", "decimal": "."}

    chunks = list(_read_chunks(str(path), chunk_size=10, csv_props=props))
    row_count = sum(len(chunk) for chunk in chunks)

    assert row_count == 2
