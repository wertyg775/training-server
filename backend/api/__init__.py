from ninja import NinjaAPI

from backend.api.routers.health import router as health_router
from backend.api.routers.projects import router as projects_router

api = NinjaAPI(title="Training Server API", version="0.1.0")
api.add_router("/health", health_router)
api.add_router("/projects", projects_router)
