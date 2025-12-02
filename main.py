from fastapi import FastAPI
from routes import register_zone_routes

app = FastAPI(title="breathe backend")

register_zone_routes(app)