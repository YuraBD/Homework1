from airflow import DAG
from datetime import datetime, timezone
import json
from airflow.providers.http.sensors.http import HttpSensor
from airflow.models.variable import Variable
from airflow.operators.python import PythonOperator
import requests
from airflow.models.baseoperator import chain
import logging
from airflow.providers.postgres.operators.postgres import PostgresOperator

def _process_weather(ti, city):
    info = ti.xcom_pull(f"extract_data_{city}")
    timestamp = info["data"][0]["dt"]
    temp = info["data"][0]["temp"]
    date = datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
    humidity = info["data"][0]["humidity"]
    clouds = info["data"][0]["clouds"]
    wind_speed = info["data"][0]["wind_speed"]

    return timestamp, date, temp, humidity, clouds, wind_speed

def _extract_city_data(ts, city):
    weather_url = "https://api.openweathermap.org/data/3.0/onecall/timemachine"
    cities_url = "http://api.openweathermap.org/geo/1.0/direct"

    response = requests.get(cities_url, params= {
        "appid": Variable.get("WEATHER_API_KEY"),
        "limit": 1,
        "q": city
    })

    city_coords = json.loads(response.text)[0]

    dt_object = datetime.fromisoformat(ts[:-6])
    unix_timestamp = int(dt_object.replace(tzinfo=timezone.utc).timestamp())

    logging.info(f"THIS IS TS VALUE ------ {ts}")
    logging.info(f"THIS IS City ------ {city}")

    response = requests.get(weather_url, params = {
        "appid": Variable.get("WEATHER_API_KEY"),
        "units": "metric",
        "lat": city_coords["lat"],
        "lon": city_coords["lon"],
        "dt": unix_timestamp
    })

    return json.loads(response.text)


cities = ["Lviv", "Kyiv", "Kharkiv", "Odesa", "Zhmerynka"]

with DAG(dag_id="weather", schedule_interval="@daily", start_date=datetime(2023, 11, 10), catchup=True) as dag:
    db_create = PostgresOperator(
        task_id="create_table_postgres",
        postgres_conn_id="postgres_conn",
        sql="""
        CREATE TABLE IF NOT EXISTS measures
        (
        city TEXT,
        timestamp TIMESTAMP,
        date TEXT,
        temp FLOAT,
        humidity FLOAT,
        clouds FLOAT,
        wind_speed FLOAT
        );"""
    )

    check_api = HttpSensor(
        task_id="check_api",
        http_conn_id="weather_conn",
        endpoint="data/3.0/onecall",
        request_params={"appid": Variable.get("WEATHER_API_KEY"), "lat": 33.44, "lon": -94.04}
    )

    extract_data_tasks = []
    process_data_tasks = []
    inject_data_tasks = []
    for city in cities:
        extract_data = PythonOperator(
            task_id=f"extract_data_{city}",
            python_callable=_extract_city_data,
            op_kwargs={"city": city}
        )
        extract_data_tasks.append(extract_data)

        process_data = PythonOperator(
            task_id=f"process_data_{city}",
            python_callable=_process_weather,
            op_kwargs={"city": city}
        )
        process_data_tasks.append(process_data)

        inject_data = PostgresOperator(
            task_id=f"inject_data_{city}",
            postgres_conn_id="postgres_conn",
            sql="""
            INSERT INTO measures (city, timestamp, date, temp, humidity, clouds, wind_speed) VALUES
            ('{{ params.city }}',
            to_timestamp({{ti.xcom_pull(task_ids='process_data_' + params.city)[0]}}),
            '{{ti.xcom_pull(task_ids='process_data_' + params.city)[1]}}',
            {{ti.xcom_pull(task_ids='process_data_' + params.city)[2]}},
            {{ti.xcom_pull(task_ids='process_data_' + params.city)[3]}},
            {{ti.xcom_pull(task_ids='process_data_' + params.city)[4]}},
            {{ti.xcom_pull(task_ids='process_data_' + params.city)[5]}});
            """,
            params={"city": city}
        )
        inject_data_tasks.append(inject_data)

    chain(db_create, check_api, extract_data_tasks, process_data_tasks, inject_data_tasks)
