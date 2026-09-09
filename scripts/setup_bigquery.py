"""
Setup script to export the local SQLite database to Google BigQuery.

Instructions:
1. Ensure you have authenticated with Google Cloud: `gcloud auth application-default login`
2. Ensure your GCP Project has BigQuery enabled.
3. Run this script. It will create the dataset and upload the tables.
"""

import sqlite3
import pandas as pd
from google.cloud import bigquery
from google.api_core.exceptions import Conflict
import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROJECT_ID = "969488392244"
DATASET_ID = f"{PROJECT_ID}.vigileye_fleet"
SQLITE_DB = "vigileye_fleet.db"

def setup_bigquery():
    client = bigquery.Client(project=PROJECT_ID)
    
    # 1. Create Dataset
    logger.info(f"Creating dataset {DATASET_ID}...")
    dataset = bigquery.Dataset(DATASET_ID)
    dataset.location = "US"
    try:
        client.create_dataset(dataset, timeout=30)
        logger.info("Dataset created successfully.")
    except Conflict:
        logger.info("Dataset already exists.")

    # 2. Connect to SQLite and read tables
    logger.info("Connecting to local SQLite database...")
    if not os.path.exists(SQLITE_DB):
        logger.error(f"Could not find {SQLITE_DB}. Please run generate_enterprise_db.py first.")
        return

    conn = sqlite3.connect(SQLITE_DB)
    
    tables = ['pilots', 'daily_readings']
    
    for table in tables:
        logger.info(f"Reading table '{table}' from SQLite...")
        df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
        
        # BigQuery does not like the 'id' column from daily_readings being an int if it expects something else,
        # but pandas to_gbq is usually smart enough. We will enforce datetime on the date column.
        if table == 'daily_readings' and 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date']).dt.date
            
        # 3. Upload to BigQuery
        table_ref = f"{DATASET_ID}.{table}"
        logger.info(f"Uploading {len(df)} rows to BigQuery table {table_ref}...")
        
        job_config = bigquery.LoadJobConfig(
            write_disposition="WRITE_TRUNCATE", # Overwrite if exists
        )
        
        job = client.load_table_from_dataframe(
            df, table_ref, job_config=job_config
        )
        job.result() # Wait for the job to complete
        logger.info(f"Successfully uploaded {table}.")
        
    conn.close()
    logger.info("✅ BigQuery setup complete! You can now switch the connector in app.py to 'bigquery'.")

if __name__ == "__main__":
    setup_bigquery()
