import re

with open("src/api/services/cold_run_upload.py", "r") as f:
    content = f.read()

# Replace the first loop that creates cached_normalized_chunks
old_loop_1 = """    # Cache for successfully normalized and validated chunks
    cached_normalized_chunks: list[pd.DataFrame] = []

    for raw_chunk in _read_chunks(tmp_path, chunk_size, csv_props):
        if raw_chunk.empty:
            continue
        if header_cols is None:
            header_cols = list(raw_chunk.columns)
            _, imputed = detect_optional_sensor_presence(header_cols)
            imputed_channels = sorted(imputed)
        try:
            normalized_chunk = adapter.normalize(raw_chunk)
            adapter.validate(normalized_chunk)
        except Exception:
            continue

        cached_normalized_chunks.append(normalized_chunk)
        stats_parts.append(normalized_chunk[sensor_cols])

        raw_df, _ = parse_raw_rows(raw_chunk)
        if raw_df.empty:
            continue

        # Vectorize timestamp conversion
        raw_df["timestamp"] = raw_df["timestamp"].map(
            lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x)
        )

        # Fast record-oriented NaN stripping
        for row in raw_df.to_dict(orient="records"):
            entry = {
                "timestamp": row["timestamp"],
                **{
                    col: val for col, val in row.items()
                    if col not in ("timestamp", "source")
                    and val is not None and not (isinstance(val, float) and pd.isna(val))
                }
            }
            raw_readings.append(entry)

    if not stats_parts:
        raise ValueError("Uploaded CSV contains no valid data.")

    # Compute adaptive_ranges by concatenating the cached normalized chunks in memory.
    normalized_df = pd.concat(cached_normalized_chunks, ignore_index=True)
    stats_df = normalized_df[sensor_cols]"""

new_loop_1 = """    for raw_chunk in _read_chunks(tmp_path, chunk_size, csv_props):
        if raw_chunk.empty:
            continue
        if header_cols is None:
            header_cols = list(raw_chunk.columns)
            _, imputed = detect_optional_sensor_presence(header_cols)
            imputed_channels = sorted(imputed)
        try:
            normalized_chunk = adapter.normalize(raw_chunk)
            adapter.validate(normalized_chunk)
            stats_parts.append(normalized_chunk[sensor_cols])
        except Exception:
            pass

        raw_df, _ = parse_raw_rows(raw_chunk)
        if raw_df.empty:
            continue

        # Vectorize timestamp conversion
        raw_df["timestamp"] = raw_df["timestamp"].map(
            lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x)
        )

        # Fast record-oriented NaN stripping
        for row in raw_df.to_dict(orient="records"):
            entry = {
                "timestamp": row["timestamp"],
                **{
                    col: val for col, val in row.items()
                    if col not in ("timestamp", "source")
                    and val is not None and not (isinstance(val, float) and pd.isna(val))
                }
            }
            raw_readings.append(entry)

    if not stats_parts:
        raise ValueError("Uploaded CSV contains no valid data.")

    stats_df = pd.concat(stats_parts, ignore_index=True)"""

content = content.replace(old_loop_1, new_loop_1)

# Replace the second loop
old_loop_2 = """    # Iterate over cached normalized chunks list in memory instead of calling _read_chunks() a second time.
    for normalized_chunk in cached_normalized_chunks:
        if normalized_chunk.empty:
            continue"""

new_loop_2 = """    for raw_chunk in _read_chunks(tmp_path, chunk_size, csv_props):
        if raw_chunk.empty:
            continue
        try:
            normalized_chunk = adapter.normalize(raw_chunk)
            adapter.validate(normalized_chunk)
        except Exception:
            continue
"""

content = content.replace(old_loop_2, new_loop_2)

with open("src/api/services/cold_run_upload.py", "w") as f:
    f.write(content)

print("Updated cold_run_upload.py")
