from __future__ import annotations

import os

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from .access_routes import router as access_router
from .auth import require_operator
from .core import ApiProblem, iso, utc_now
from .engine_routes import router as engine_router
from .notification_routes import router as notification_router
from .public_routes import router as public_router

app = FastAPI(title="ASAPify Operator API", version="0.1.0", docs_url=None, redoc_url=None,
              openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)
origins = [v.strip() for v in os.environ.get("CORS_ORIGINS", "").split(",") if v.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PUT", "DELETE"],
                       allow_headers=["Authorization", "Content-Type"], allow_credentials=False)


@app.middleware("http")
async def no_shared_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(ApiProblem)
async def problem_handler(request, exc: ApiProblem):
    return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}},
                        headers={"Cache-Control": "no-store"})


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse(status_code=400, content={"error": {"code": "VALIDATION", "message": "Invalid parameter or request body."}})


@app.exception_handler(Exception)
async def unavailable(request, exc):
    # Credentials, external response bodies and token-bearing URLs never enter the response.
    return JSONResponse(status_code=503, content={"error": {"code": "SERVICE_UNAVAILABLE", "message": "The data service is not available yet. Try again later."}})


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "version": os.environ.get("ASAPIFY_VERSION", "0.1.0"), "time": iso(utc_now())}


operator = APIRouter(prefix="/api/v1/operator", dependencies=[Depends(require_operator)])


@operator.get("/alerts")
def alerts():
    raise ApiProblem(503, "INTEGRATION_NOT_READY", "Alert management is not available at this stage.")


@operator.post("/alerts/{alert_id}/close")
def close_alert(alert_id: str):
    raise ApiProblem(503, "INTEGRATION_NOT_READY", "Closing alerts is not connected yet.")


app.include_router(operator)

app.include_router(engine_router)

app.include_router(notification_router)

app.include_router(access_router)

app.include_router(public_router)
