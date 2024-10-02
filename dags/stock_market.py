from airflow.decorators import dag,task
from airflow.hooks.base import BaseHook
from airflow.sensors.base import PokeReturnValue
from airflow.operators.python import PythonOperator
from airflow.providers.docker.operators.docker import DockerOperator
from include.stock_marked.tasks import _get_stock_prices,_store_prices,_get_formated_csv,BUCKET_NAME

from astro import sql as aql
from astro.files import File
from astro.sql.table import Table ,Metadata
from datetime import datetime
import requests





SYMBOL='AAPL'

# Define the DAG
@dag(
    start_date=datetime(2024, 1, 1),
    schedule_interval='@daily',  # Runs daily
    catchup=False,               # Prevent running past dates when it starts
    tags=['stock_market']         # Tags for the DAG
)


def stock_market():
    @task.sensor(poke_interval=30, timeout=300, mode='poke')
    
    def is_api_available()->PokeReturnValue:
        api=BaseHook.get_connection('stock_api')
        url = f"{api.host}{api.extra_dejson['endpoint']}"
        response= requests.get(url, headers=api.extra_dejson['headers'])
        condition = response.json()['finance']['result'] is None
        return PokeReturnValue(is_done=condition,xcom_value=url)

    get_stock_prices = PythonOperator(

        task_id='get_stock_prices',
        python_callable =_get_stock_prices,
        op_kwargs={'url':'{{task_instance.xcom_pull(task_ids="is_api_available")}}','symbol':SYMBOL}
    )


    store_prices=PythonOperator(
        task_id='store_prices',
        python_callable=_store_prices,
        op_kwargs={'stock':'{{task_instance.xcom_pull(task_ids="get_stock_prices")}}'}

    )


    format_prices= DockerOperator(
        task_id ='format_prices',
        image='airflow/stock-app',
        container_name='format_prices',
        api_version='auto',
        auto_remove=True,
        docker_url='tcp://docker-proxy:2375',
        network_mode='container:spark-master',
        tty=True,
        xcom_all=False,
        mount_tmp_dir=False,
        environment={
            'SPARK_APPLICATION_ARGS': '{{task_instance.xcom_pull(task_ids="store_prices")}}'

        }


    )

    get_formated_prices = PythonOperator(
        task_id ='get_formated_prices',
        python_callable =_get_formated_csv,
        op_kwargs={
            'path': '{{task_instance.xcom_pull(task_ids="store_prices")}}'

        }
    )

    
    load_to_dw = aql.load_file(
        task_id='load_to_dw',
        input_file=File(
            path=f"s3://{BUCKET_NAME}/{{{{ task_instance.xcom_pull(task_ids='get_formated_prices') }}}}",
            conn_id='minio'
            #filetype='csv'  # Specify filetype explicitly

        ),
        output_table=Table(
            name='stock_market',
            conn_id='postgres',
            metadata=Metadata(
                schema='public',
            ),
        )
    )


    is_api_available() >> get_stock_prices >> store_prices >>format_prices >> get_formated_prices >>load_to_dw
    

stock_market()