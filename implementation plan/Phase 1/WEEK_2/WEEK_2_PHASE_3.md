Continue Week 2 Phase 3 - Implement Streamlit dashboard for visualization and threshold management.

Phase 3 (after Phase 2):

- backend-engineer: Create Streamlit dashboard with 4 pages and interactive components.
  Output: src/dashboard/app.py, src/dashboard/pages/sensor_data.py, src/dashboard/pages/anomalies.py, src/dashboard/pages/thresholds.py, src/dashboard/pages/metrics.py, src/dashboard/components/charts.py
  
  Deliverables:
  
  1. src/dashboard/app.py (Main app)
     Multi-page Streamlit app with:
     
     - Page configuration:
       st.set_page_config(page_title="AI Agronomist", layout="wide")
     
     - Sidebar:
       * Tenant selector (dropdown)
       * Greenhouse selector (dropdown, filtered by tenant)
       * Date range picker (start_date, end_date)
       * Refresh button
     
     - Navigation:
       * Home / Overview page
       * Sensor Data page
       * Anomalies page
       * Thresholds page
       * Metrics page
     
     - Database connection:
       * Initialize SQLAlchemy session
       * Cache connection across reruns using @st.cache_resource
     
     - State management:
       * st.session_state for selected tenant, greenhouse, date range
       * Persist state across page reloads
  
  2. src/dashboard/pages/sensor_data.py
     Visualize uploaded sensor data:
     
     - Query SensorReading table:
       SELECT * FROM sensor_reading
       WHERE tenant_id = selected_tenant
       AND greenhouse_id = selected_greenhouse
       AND timestamp BETWEEN start_date AND end_date
       ORDER BY timestamp
     
     - Display 4 time series plots (one per sensor):
       * Temperature (°C)
       * Humidity (%)
       * CO2 (ppm)
       * Light (lux)
     
     - Use Plotly for interactive charts:
       fig = px.line(df, x='timestamp', y='temperature', title='Temperature Over Time')
       st.plotly_chart(fig, use_container_width=True)
     
     - Show summary stats:
       st.metric("Avg Temperature", f"{df['temperature'].mean():.1f}°C")
       st.metric("Max Humidity", f"{df['humidity'].max():.1f}%")
       st.metric("CO2 Range", f"{df['co2'].min()}-{df['co2'].max()} ppm")
  
  3. src/dashboard/pages/anomalies.py
     Visualize detected anomalies:
     
     - Query anomaly scores:
       SELECT timestamp, anomaly_score FROM sensor_reading
       WHERE tenant_id = selected_tenant
       AND anomaly_score IS NOT NULL
       ORDER BY timestamp
     
     - Plot 1: Anomaly score timeline
       * X-axis: timestamp
       * Y-axis: anomaly_score (0-1)
       * Color-code: green (< 0.3), yellow (0.3-0.7), red (> 0.7)
       * Interactive (hover shows exact values)
     
     - Plot 2: Anomaly heatmap (by hour, by day)
       * Show which hours/days had most anomalies
       * Helps identify patterns
     
     - Statistics:
       st.metric("Total Anomalies", f"{len(anomalies)}")
       st.metric("High-Severity (>0.8)", f"{len(anomalies[anomalies.anomaly_score > 0.8])}")
       st.metric("Anomaly Rate", f"{anomaly_rate:.2%}")
     
     - Anomaly list:
       Display table of top 20 anomalies (sortable):
       timestamp | anomaly_score | temperature | humidity | co2 | light
  
  4. src/dashboard/pages/thresholds.py
     Manage anomaly detection thresholds:
     
     - Current threshold display:
       st.info(f"Current threshold: {current_threshold:.2f}")
     
     - Slider to adjust threshold:
       new_threshold = st.slider(
         "Anomaly Threshold",
         min_value=0.0,
         max_value=1.0,
         value=current_threshold,
         step=0.05
       )
     
     - Preview effect of new threshold:
       * Show how many anomalies detected at new threshold
       * Show which new sequences would be flagged
     
     - Save button:
       if st.button("Save Threshold"):
           # Update threshold in database
           # Recalculate anomalies
           st.success("Threshold updated!")
     
     - Threshold history:
       Display table of past threshold changes:
       timestamp | old_threshold | new_threshold | anomalies_before | anomalies_after
     
     - Recommended threshold:
       Analyze data, suggest threshold that captures known anomalies
       st.info(f"Recommended threshold based on data: {recommended:.2f}")
  
  5. src/dashboard/pages/metrics.py
     Show model performance and coverage stats:
     
     - Coverage metrics:
       st.metric("Total Sequences Processed", total_count)
       st.metric("Date Range Covered", f"{start_date} to {end_date}")
       st.metric("Greenhouses Monitored", num_greenhouses)
       st.metric("Data Points", f"{len(df):,}")
     
     - Anomaly statistics:
       st.metric("Normal Sequences", f"{normal_count:,}")
       st.metric("Anomalous Sequences", f"{anomaly_count:,}")
       st.metric("Anomaly Percentage", f"{anomaly_pct:.2%}")
     
     - Model info:
       st.info(f"Model: LSTM-VAE (Week 1 Synthetic)")
       st.info(f"Input: 4 sensors × 20 timesteps")
       st.info(f"Inference latency: ~45ms per sequence")
     
     - Data quality:
       st.metric("Missing Values", missing_count)
       st.metric("Out-of-Range Values", out_of_range_count)
     
     - Export data:
       csv = df.to_csv(index=False)
       st.download_button(
           label="Download CSV",
           data=csv,
           file_name=f"export_{selected_tenant}_{selected_greenhouse}.csv"
       )
  
  6. src/dashboard/components/charts.py
     Reusable chart components:
     
     - Function: plot_time_series(df, column, title)
       Returns: plotly figure
       Used across pages
     
     - Function: plot_anomaly_heatmap(anomalies_df)
       Returns: heatmap showing anomaly distribution
     
     - Function: plot_anomaly_timeline(anomalies_df, threshold)
       Returns: timeline with color-coded severity
     
     - Function: query_sensor_readings(tenant_id, greenhouse_id, start, end)
       Returns: DataFrame from database
  
  Requirements:
  - Use Streamlit 1.28+
  - Use Plotly for interactive charts
  - Use SQLAlchemy for database queries
  - All pages use same sidebar (tenant, greenhouse, date range)
  - Multi-tenant filtering (user can only see their tenant's data)
  - Error handling (database errors, no data, etc.)
  - Logging (page views, actions)
  
  Additional files:
  - src/dashboard/__init__.py (empty)
  - src/dashboard/pages/__init__.py (empty)
  - src/dashboard/components/__init__.py (empty)

auto-proceed