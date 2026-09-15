from ninja import Router, Schema

router = Router(tags=["health"])


class HealthResponse(Schema):
    status: str


@router.get("", response=HealthResponse)
def health(request):
    """Application liveness only; does not check Docker or the GPU."""
    return {"status": "ok"}
