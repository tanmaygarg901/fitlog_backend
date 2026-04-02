from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import parse, reports

app = FastAPI(title="FitLog AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

parse_router = getattr(parse, "router", APIRouter())
reports_router = getattr(reports, "router", APIRouter())

app.include_router(parse_router, prefix="/api")
app.include_router(reports_router, prefix="/api")


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "fitlog-api"}


@app.on_event("startup")
async def on_startup():
    print("FitLog API started")
