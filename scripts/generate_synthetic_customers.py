import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

def generate_customer_data(customer_id, temp_range, humidity_range, co2_range, light_range):
    base_dir = f"data/synthetic_customers/{customer_id}"
    os.makedirs(base_dir, exist_ok=True)
    
    # 2 weeks = 336 hours = 1344 intervals of 15 min
    start_time = datetime(2026, 6, 1)
    timestamps = [start_time + timedelta(minutes=15*i) for i in range(1344)]
    
    # Create diurnal patterns (0 to 1 for day, 0 at night)
    hours = np.array([t.hour + t.minute/60 for t in timestamps])
    day_cycle = np.sin(np.clip((hours - 6) * np.pi / 12, 0, np.pi))  # 6 AM to 6 PM
    
    # Generate Normal Data
    normal_temp = temp_range[0] + day_cycle * (temp_range[1] - temp_range[0]) + np.random.normal(0, 0.5, 1344)
    normal_hum = humidity_range[1] - day_cycle * (humidity_range[1] - humidity_range[0]) + np.random.normal(0, 1.0, 1344) # higher at night
    normal_co2 = co2_range[0] + (1 - day_cycle) * (co2_range[1] - co2_range[0]) + np.random.normal(0, 10, 1344)
    normal_light = day_cycle * light_range[1] + np.random.normal(0, 50, 1344)
    normal_light = np.clip(normal_light, 0, None)
    
    df_normal = pd.DataFrame({
        'timestamp': [t.isoformat() + "Z" for t in timestamps],
        'temperature': np.clip(normal_temp, 0, 50).round(2),
        'humidity': np.clip(normal_hum, 0, 100).round(2),
        'co2': np.clip(normal_co2, 0, 1000).round(2),
        'light': np.clip(normal_light, 0, 2000).round(2)
    })
    df_normal.to_csv(os.path.join(base_dir, "normal.csv"), index=False)
    
    # Generate Anomalies (1 week = 672 intervals)
    anomaly_timestamps = [start_time + timedelta(days=14) + timedelta(minutes=15*i) for i in range(672)]
    a_hours = np.array([t.hour + t.minute/60 for t in anomaly_timestamps])
    a_day_cycle = np.sin(np.clip((a_hours - 6) * np.pi / 12, 0, np.pi))
    
    a_temp = temp_range[0] + a_day_cycle * (temp_range[1] - temp_range[0]) + np.random.normal(0, 0.5, 672)
    a_hum = humidity_range[1] - a_day_cycle * (humidity_range[1] - humidity_range[0]) + np.random.normal(0, 1.0, 672)
    a_co2 = co2_range[0] + (1 - a_day_cycle) * (co2_range[1] - co2_range[0]) + np.random.normal(0, 10, 672)
    a_light = a_day_cycle * light_range[1] + np.random.normal(0, 50, 672)
    
    # Inject specific anomalies based on days
    # Day 3: Heating failure (drop temp by 10)
    day3_mask = (np.array([t.day for t in anomaly_timestamps]) == (start_time + timedelta(days=16)).day)
    a_temp[day3_mask] = a_temp[day3_mask] - 10.0
    
    # Day 6: Humidity spike (jump to 95+)
    day6_mask = (np.array([t.day for t in anomaly_timestamps]) == (start_time + timedelta(days=19)).day)
    a_hum[day6_mask] = 96.0 + np.random.normal(0, 1, np.sum(day6_mask))
    
    df_anomaly = pd.DataFrame({
        'timestamp': [t.isoformat() + "Z" for t in anomaly_timestamps],
        'temperature': np.clip(a_temp, 0, 50).round(2),
        'humidity': np.clip(a_hum, 0, 100).round(2),
        'co2': np.clip(a_co2, 0, 1000).round(2),
        'light': np.clip(a_light, 0, 2000).round(2)
    })
    df_anomaly.to_csv(os.path.join(base_dir, "anomalies.csv"), index=False)
    
    print(f"Generated synthetic data for {customer_id}")

if __name__ == "__main__":
    # Tomato
    generate_customer_data("customer_tomato", temp_range=(22, 26), humidity_range=(65, 75), co2_range=(400, 500), light_range=(0, 1000))
    # Lettuce
    generate_customer_data("customer_lettuce", temp_range=(18, 22), humidity_range=(70, 80), co2_range=(350, 450), light_range=(0, 800))
    # Cucumber
    generate_customer_data("customer_cucumber", temp_range=(24, 28), humidity_range=(60, 70), co2_range=(450, 550), light_range=(0, 1200))
