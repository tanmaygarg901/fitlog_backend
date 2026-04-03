from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db.supabase_client import supabase, _ensure_supabase_client as _ensure_db_client
from app.services.correlation import compute_weekly_correlations, generate_weekly_report

router = APIRouter(tags=["reports"])


class GenerateReportRequest(BaseModel):
    user_id: str


def _ensure_supabase_client() -> None:
    global supabase
    if supabase is None:
        supabase = _ensure_db_client()


def _last_monday_iso() -> str:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


@router.post("/reports/generate")
async def generate_report(request: GenerateReportRequest) -> dict:
    try:
        _ensure_supabase_client()

        report_text = generate_weekly_report(request.user_id)
        week_start = _last_monday_iso()

        insert_resp = (
            supabase.table("weekly_reports")
            .insert(
                {
                    "user_id": request.user_id,
                    "week_start": week_start,
                    "report_text": report_text,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .execute()
        )
        insert_error = getattr(insert_resp, "error", None)
        if insert_error:
            raise RuntimeError(f"Failed inserting weekly report: {insert_error}")

        insert_data = getattr(insert_resp, "data", None)
        if not insert_data:
            raise RuntimeError("weekly_reports insert returned no data")

        return {"report_text": report_text, "week_start": week_start}
    except HTTPException:
        raise
    except Exception as exc:
        print("Error in POST /api/reports/generate:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to generate report: {exc}")


@router.get("/reports/latest")
async def get_latest_report(user_id: str = Query(...)) -> dict:
    try:
        _ensure_supabase_client()

        response = (
            supabase.table("weekly_reports")
            .select("*")
            .eq("user_id", user_id)
            .order("generated_at", desc=True)
            .limit(1)
            .execute()
        )

        error = getattr(response, "error", None)
        if error:
            raise RuntimeError(f"Failed fetching latest report: {error}")

        data = getattr(response, "data", None) or []
        if not data:
            raise HTTPException(status_code=404, detail="No reports yet")

        return data[0]
    except HTTPException:
        raise
    except Exception as exc:
        print("Error in GET /api/reports/latest:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to fetch latest report: {exc}")


@router.get("/reports/correlations")
async def get_correlations(user_id: str = Query(...)) -> dict:
    try:
        result = compute_weekly_correlations(user_id)
        return {
            "enough_data": result.get("enough_data", False),
            "message": result.get("message", ""),
            "pairs": result.get("pairs", []),
            "stats": result.get("stats", {}),
            "days": result.get("days", 0),
        }
    except Exception as exc:
        print("Error in GET /api/reports/correlations:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to compute correlations: {exc}")


@router.get("/templates")
async def get_templates(user_id: str = Query(...)) -> list[dict]:
    try:
        _ensure_supabase_client()

        response = (
            supabase.table("workout_templates")
            .select("*")
            .eq("user_id", user_id)
            .order("usage_count", desc=True)
            .execute()
        )

        error = getattr(response, "error", None)
        if error:
            raise RuntimeError(f"Failed fetching templates: {error}")

        return getattr(response, "data", None) or []
    except Exception as exc:
        print("Error in GET /api/templates:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to fetch templates: {exc}")


@router.delete("/templates/{template_id}")
async def delete_template(template_id: str) -> dict:
    try:
        _ensure_supabase_client()

        response = (
            supabase.table("workout_templates")
            .delete()
            .eq("id", template_id)
            .execute()
        )

        error = getattr(response, "error", None)
        if error:
            raise RuntimeError(f"Failed deleting template: {error}")

        deleted_rows = getattr(response, "data", None) or []
        if not deleted_rows:
            raise HTTPException(status_code=404, detail="Template not found")

        return {"deleted": True, "template_id": template_id}
    except HTTPException:
        raise
    except Exception as exc:
        print("Error in DELETE /api/templates/{template_id}:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to delete template: {exc}")
