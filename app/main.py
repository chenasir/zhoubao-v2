"""FastAPI 入口。"""
from __future__ import annotations

import io
import hmac
import logging
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import filter as flt, filter_pipeline, formatter, renderer, retriever, title_translator
from .config import STATIC_DIR, get_country_order_map, get_source_order_map, load_sources, settings
from .models import FormatItemRequest, GenerateRequest, ManualUrlRequest, RelatedSourcesRequest, RenderRequest, ScoreRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("zhoubao")
app = FastAPI(title="CICC Weekly News Digest Agent", version="2.0.0")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def optional_access_token(request: Request, call_next):
    """Protect API routes when APP_ACCESS_TOKEN is configured."""
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
        expected = settings.app_access_token
        supplied = request.headers.get("x-app-token", "")
        if expected and not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Access token required"}, status_code=401)
    return await call_next(request)


def _published_ts(row: dict) -> float:
    value = row.get("published_at")
    if not value:
        return 0.0
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _candidate_sort_key(row: dict) -> tuple[int, int, float, float]:
    country_order = get_country_order_map()
    source_order = get_source_order_map()
    return (
        country_order.get(row.get("country_code", ""), 999),
        source_order.get((row.get("country_code", ""), row.get("source", "")), 999),
        -float(row.get("score", 0) or 0),
        -_published_ts(row),
    )


def _select_generation_rows(candidates: list) -> list[dict]:
    """按前端提交顺序保留已勾选条目，不做重排或静默去重。"""
    return [cand.model_dump() for cand in candidates if cand.selected]


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
def api_health():
    return {"ok": True, "version": app.version, "auth_required": bool(settings.app_access_token)}


@app.get("/api/countries")
def api_countries():
    return [
        {"code": c["code"], "name_en": c["name_en"], "name_zh": c["name_zh"]}
        for c in load_sources()
    ]


@app.get("/api/sources")
def api_sources():
    result = []
    for c in load_sources():
        sources = []
        for s in c.get("sources", []):
            sources.append(
                {
                    "name": s.get("name", ""),
                    "type": s.get("type", ""),
                    "url": s.get("url", ""),
                    "query": s.get("query", ""),
                    "tier": int(s.get("tier", 3)),
                    "category": s.get("category", "media"),
                    "enabled": s.get("enabled", True),
                }
            )
        result.append(
            {
                "code": c["code"],
                "name_en": c["name_en"],
                "name_zh": c["name_zh"],
                "sources": sources,
            }
        )
    return result


@app.post("/api/fetch")
def api_fetch():
    raw = retriever.fetch_all()
    deduped = flt.dedupe_by_title(raw)
    pipeline = filter_pipeline.run_filtering_pipeline(deduped)
    candidates = title_translator.translate_candidate_titles(pipeline["candidates"])
    return {
        "fetched": len(raw),
        "deduped": len(deduped),
        "after_rules": pipeline["stats"]["after_layer1"],
        "after_shortlist": len(candidates),
        "passed_gates": pipeline["stats"]["passed_gates"],
        "final": pipeline["stats"]["final"],
        "reserve": pipeline["stats"]["reserve"],
        "hold": pipeline["stats"]["hold"],
        "dropped": pipeline["stats"]["dropped"],
        "candidates": candidates,
    }


@app.post("/api/score")
def api_score(req: ScoreRequest):
    rows = [cand.model_dump() for cand in req.candidates]
    if not rows:
        raise HTTPException(400, "No candidates provided")
    scored = flt.llm_score(rows, runtime_config=req.runtime.model_dump())
    return {
        "scored": len(scored),
        "candidates": scored,
    }


@app.post("/api/manual")
def api_manual(req: ManualUrlRequest):
    item = retriever.fetch_manual_url(req.country_code, req.url, req.source)
    if not item:
        raise HTTPException(400, "Failed to fetch the given URL")
    pipeline = filter_pipeline.run_filtering_pipeline([item])
    candidates = pipeline.get("candidates") or []
    if candidates:
        item = candidates[0]
        item["selected"] = False
        item["score_reason"] = (item.get("score_reason", "") + " | manual").strip(" |")
    else:
        item["score"] = 5.0
        item["score_reason"] = "manual"
        item["selected"] = False
        item["status"] = "HOLD_manual_review"
        item["route_country"] = req.country_code
    title_translator.translate_candidate_titles([item])
    return {"item": item}


@app.post("/api/related-sources")
def api_related_sources(req: RelatedSourcesRequest):
    if not req.title.strip():
        raise HTTPException(400, "No title provided")
    items = retriever.search_related_sources(req.title, country_code=req.country_code)
    if req.current_url:
        items = [item for item in items if item.get("url") != req.current_url]
    return {"items": items}


@app.post("/api/clear")
def api_clear():
    return {"deleted": 0}


@app.post("/api/generate")
def api_generate(req: GenerateRequest):
    zip_bytes, headers = _build_generate_zip(req)
    return StreamingResponse(io.BytesIO(zip_bytes), media_type="application/zip", headers=headers)


@app.post("/api/format-item")
def api_format_item(req: FormatItemRequest):
    item = formatter.format_one(req.candidate.model_dump(), runtime_config=req.runtime.model_dump())
    if item is None:
        raise HTTPException(500, "Selected news item could not be formatted")
    return {"item": item.model_dump()}


@app.post("/api/render")
def api_render(req: RenderRequest):
    if not req.formatted_items:
        raise HTTPException(400, "No formatted items provided")
    zip_bytes, headers = _render_formatted_zip(
        req.formatted_items,
        issue_number=req.issue_number,
        start_date_raw=req.start_date,
        end_date_raw=req.end_date,
    )
    return StreamingResponse(io.BytesIO(zip_bytes), media_type="application/zip", headers=headers)


def _build_generate_zip(req: GenerateRequest) -> tuple[bytes, dict[str, str]]:
    rows = _select_generation_rows(req.candidates)
    if not rows:
        raise HTTPException(400, "No candidates selected")

    logger.info(
        "Formatting %d selected items (order preserved): %s",
        len(rows),
        [str(r.get("id") or r.get("title", ""))[:60] for r in rows],
    )
    formatted_results = formatter.format_many(rows, runtime_config=req.runtime.model_dump())
    failed_indices = [i for i, item in enumerate(formatted_results) if item is None]
    failed_count = len(failed_indices)
    if failed_count:
        failed_titles = [
            (rows[i].get("title") or rows[i].get("url") or f"item#{i}")[:100]
            for i in failed_indices
        ]
        raise HTTPException(
            500,
            f"{failed_count} 条勾选新闻格式化失败，已中止生成以避免 DOC 与勾选不一致："
            + "；".join(failed_titles[:8])
            + ("…" if len(failed_titles) > 8 else ""),
        )

    formatted = formatted_results
    warning_count = sum(len(item.fact_check_warnings) for item in formatted)

    return _render_formatted_zip(
        formatted,
        issue_number=req.issue_number,
        start_date_raw=req.start_date,
        end_date_raw=req.end_date,
        selected_count=len(rows),
        warning_count=warning_count,
    )


def _render_formatted_zip(
    formatted,
    *,
    issue_number: int,
    start_date_raw: str | None,
    end_date_raw: str | None,
    selected_count: int | None = None,
    warning_count: int | None = None,
) -> tuple[bytes, dict[str, str]]:
    from datetime import date, timedelta

    try:
        end_date = date.fromisoformat(end_date_raw) if end_date_raw else date.today()
    except ValueError:
        end_date = date.today()
    try:
        start_date = date.fromisoformat(start_date_raw) if start_date_raw else end_date - timedelta(days=6)
    except ValueError:
        start_date = end_date - timedelta(days=6)

    with tempfile.TemporaryDirectory() as temp_dir:
        out_dir = Path(temp_dir)
        en_path, cn_path = renderer.render_bilingual(
            formatted,
            issue_number=issue_number,
            start_date=start_date,
            end_date=end_date,
            out_dir=out_dir,
        )

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(en_path, arcname=en_path.name)
            zf.write(cn_path, arcname=cn_path.name)
        buffer.seek(0)

    actual_count = len(formatted)
    selected_count = actual_count if selected_count is None else selected_count
    warning_count = sum(len(item.fact_check_warnings) for item in formatted) if warning_count is None else warning_count
    filename = f"issue_{issue_number}_bilingual_docx.zip"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Zhoubao-Selected-Count": str(selected_count),
        "X-Zhoubao-Deduped-Count": str(selected_count),
        "X-Zhoubao-Formatted-Count": str(actual_count),
        "X-Zhoubao-Failed-Count": "0",
        "X-Zhoubao-Warning-Count": str(warning_count),
    }
    return buffer.getvalue(), headers
