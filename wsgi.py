"""Production entrypoint for the single-process Socket.IO deployment."""

from app import app
from database.db import init_db
from backend.product_image_sync import start_product_image_sync
from backend.catalog_sync import start_catalog_sync
from backend.daily_cleanup import start_daily_cleanup


init_db()
start_daily_cleanup()
start_product_image_sync()
start_catalog_sync()
