#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Movie Search API
Author: Antigravity

This is a FastAPI-based backend service that connects to GCP Firestore
to serve movie search, filtering, and pagination requests.
Designed to be containerized and deployed to Google Cloud Run.
"""

import os
import io
import re
import uuid
import requests
from typing import Optional
from datetime import datetime, timezone
import pandas as pd
from pydantic import BaseModel, HttpUrl
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from google.cloud import firestore
from dotenv import load_dotenv

# Load local environment variables (useful for local development)
load_dotenv()

# Self-healing check: If credentials path is set but file doesn't exist, clear it
# to allow GCP Application Default Credentials (ADC) auto-discovery in Cloud Run.
credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
if credentials_path and not os.path.exists(credentials_path):
    print(f"[Info] GOOGLE_APPLICATION_CREDENTIALS file '{credentials_path}' not found locally. Clearing env var to fallback to GCP Default Credentials.")
    del os.environ["GOOGLE_APPLICATION_CREDENTIALS"]

# Initialize FastAPI App
app = FastAPI(
    title="Movie Search API",
    description="Backend API for searching movies from GCP Firestore",
    version="1.0.1"
)

# Configure CORS: Allow all origins to enable cross-origin requests from web/mobile clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Static Files (hosted at /static)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Initialize Firestore Client
init_error = None
try:
    db = firestore.Client()
    print(f"Firestore initialized successfully. GCP Project: {db.project}")
except Exception as e:
    import traceback
    init_error = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
    print(f"[Warning] Failed to initialize Firestore: {e}")
    db = None

# Global Cache Variables
MOVIES_CACHE = []
GENRES_CACHE = []
COUNTRIES_CACHE = []

def load_cache():
    global MOVIES_CACHE, GENRES_CACHE, COUNTRIES_CACHE
    if db is None:
        print("[Warning] Firestore client is not initialized. Cache loading skipped.")
        return
    print("Loading movie cache from Firestore...")
    try:
        movies_ref = db.collection("movies")
        docs = movies_ref.stream()
        
        movies = []
        genres_set = set()
        countries_set = set()
        
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            if "created_at" in data and data["created_at"]:
                data["created_at"] = data["created_at"].isoformat()
            if "updated_at" in data and data["updated_at"]:
                data["updated_at"] = data["updated_at"].isoformat()
            
            movies.append(data)
            
            # Extract genres and countries
            for g in data.get("genres", []):
                if g:
                    genres_set.add(g.strip())
            for c in data.get("countries", []):
                if c:
                    countries_set.add(c.strip())
                    
        MOVIES_CACHE = movies
        GENRES_CACHE = sorted(list(genres_set))
        COUNTRIES_CACHE = sorted(list(countries_set))
        print(f"Cache successfully loaded: {len(MOVIES_CACHE)} movies, {len(GENRES_CACHE)} genres, {len(COUNTRIES_CACHE)} countries.")
    except Exception as e:
        print(f"[Error] Failed to load cache: {e}")

@app.on_event("startup")
async def startup_event():
    load_cache()

@app.get("/")
async def read_index():
    """Serve index.html at root."""
    return FileResponse("static/index.html")

@app.get("/health")
async def health_check():
    """Health check route for GCP Cloud Run startup/liveness probes."""
    if db is None:
        return {
            "status": "warning",
            "message": "API is online, but Firestore is not initialized.",
            "database_connected": False,
            "error": init_error
        }
    return {
        "status": "healthy",
        "message": "FastAPI service is online and connected to Firestore.",
        "database_connected": True,
        "project_id": db.project,
        "cached_movies_count": len(MOVIES_CACHE)
    }


@app.get("/api/v1/movies/search")
async def search_movies(
    keyword: Optional[str] = Query(None, description="Fuzzy search keyword for title, directors, or actors"),
    genre: Optional[str] = Query(None, description="Filter by movie genre"),
    country: Optional[str] = Query(None, description="Filter by movie country"),
    min_imdb: Optional[float] = Query(None, description="Minimum IMDb rating"),
    min_douban: Optional[float] = Query(None, description="Minimum Douban rating"),
    min_rt: Optional[int] = Query(None, description="Minimum Rotten Tomatoes Tomatometer rating"),
    min_rt_audience: Optional[int] = Query(None, description="Minimum Rotten Tomatoes Audience rating"),
    min_mc: Optional[int] = Query(None, description="Minimum Metacritic rating"),
    min_hami: Optional[float] = Query(None, description="Minimum Hami Video rating"),
    sort_by: str = Query("none", description="Sort criteria"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(20, ge=1, le=100, description="Items per page")
):
    """
    Search and filter movies from the in-memory cache.
    Provides sub-millisecond, highly-responsive filtering and sorting.
    """
    # Filter the MOVIES_CACHE list
    filtered = list(MOVIES_CACHE)
    
    # 1. Keyword search (fuzzy case-insensitive match on Chinese title, English title, directors, actors)
    if keyword:
        kw = keyword.strip().lower()
        filtered = [
            m for m in filtered
            if (m.get("title_zh") and kw in m["title_zh"].lower()) or
               (m.get("title_en") and kw in m["title_en"].lower()) or
               (m.get("directors") and any(kw in d.lower() for d in m["directors"])) or
               (m.get("actors") and any(kw in a.lower() for a in m["actors"]))
        ]
        
    # 2. Genre filter
    if genre and genre != "all":
        genre_clean = genre.strip()
        filtered = [
            m for m in filtered
            if m.get("genres") and genre_clean in m["genres"]
        ]
        
    # 3. Country filter
    if country and country != "all":
        country_clean = country.strip()
        filtered = [
            m for m in filtered
            if m.get("countries") and country_clean in m["countries"]
        ]
        
    # 4. Rating thresholds
    if min_imdb is not None and min_imdb > 0:
        filtered = [
            m for m in filtered
            if m.get("imdb_rating") is not None and m["imdb_rating"] >= min_imdb
        ]
    if min_douban is not None and min_douban > 0:
        filtered = [
            m for m in filtered
            if m.get("douban_rating") is not None and m["douban_rating"] >= min_douban
        ]
    if min_rt is not None and min_rt > 0:
        filtered = [
            m for m in filtered
            if m.get("rt_tomatometer") is not None and m["rt_tomatometer"] >= min_rt
        ]
    if min_rt_audience is not None and min_rt_audience > 0:
        filtered = [
            m for m in filtered
            if m.get("rt_audience_score") is not None and m["rt_audience_score"] >= min_rt_audience
        ]
    if min_mc is not None and min_mc > 0:
        filtered = [
            m for m in filtered
            if m.get("metacritic_rating") is not None and m["metacritic_rating"] >= min_mc
        ]
    if min_hami is not None and min_hami > 0:
        filtered = [
            m for m in filtered
            if m.get("hami_rating") is not None and m["hami_rating"] >= min_hami
        ]
        
    # 5. Sorting
    if sort_by == "imdb_desc":
        filtered.sort(key=lambda x: x.get("imdb_rating") or 0.0, reverse=True)
    elif sort_by == "douban_desc":
        filtered.sort(key=lambda x: x.get("douban_rating") or 0.0, reverse=True)
    elif sort_by == "rt_desc":
        filtered.sort(key=lambda x: x.get("rt_tomatometer") or 0.0, reverse=True)
    elif sort_by == "hami_desc":
        filtered.sort(key=lambda x: x.get("hami_rating") or 0.0, reverse=True)
    elif sort_by == "year_desc":
        filtered.sort(key=lambda x: x.get("year") or 0, reverse=True)
    elif sort_by == "year_asc":
        filtered.sort(key=lambda x: x.get("year") if x.get("year") is not None else 9999)
    else:
        # Default sorting: IMDb rating desc, then release year desc
        filtered.sort(key=lambda x: (x.get("imdb_rating") or 0.0, x.get("year") or 0), reverse=True)

    # 6. Pagination
    total = len(filtered)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    paginated = filtered[start_idx:end_idx]

    return {
        "success": True,
        "page": page,
        "limit": limit,
        "total": total,
        "results": paginated
    }


@app.get("/api/v1/movies/meta")
async def get_movies_meta():
    """Return the list of all unique genres and countries for dropdown menus."""
    return {
        "success": True,
        "genres": GENRES_CACHE,
        "countries": COUNTRIES_CACHE
    }


# N-gram helper and cleaning functions copied from importer for `/sync` route
def generate_ngrams(text):
    if not isinstance(text, str) or not text.strip():
        return []
    cleaned = "".join(re.findall(r'[\u4e00-\u9fff\w\d]+', text))
    if not cleaned:
        return []
    ngrams = set()
    n = len(cleaned)
    for char in cleaned:
        ngrams.add(char)
    for i in range(n - 1):
        ngrams.add(cleaned[i : i + 2])
    for i in range(n - 2):
        ngrams.add(cleaned[i : i + 3])
    return sorted(list(ngrams))


def clean_row_data(row):
    def clean_val(val):
        if pd.isna(val) or val is None:
            return None
        if isinstance(val, str):
            val_strip = val.strip()
            if val_strip in ("", "N/A", "null", "NaN", "undefined"):
                return None
            return val_strip
        return val

    def parse_int_score(val):
        val = clean_val(val)
        if val is None:
            return None
        if isinstance(val, str):
            val = val.replace("%", "").strip()
            if "/" in val:
                val = val.split("/")[0].strip()
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return None

    def parse_float_score(val):
        val = clean_val(val)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def parse_list(val):
        val = clean_val(val)
        if val is None:
            return []
        items = [item.strip() for item in re.split(r'[,;，；]', val) if item.strip()]
        return items

    def parse_year(val):
        val = clean_val(val)
        if val is None:
            return None
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return None

    def parse_duration_minutes(val):
        val = clean_val(val)
        if val is None:
            return None
        match = re.search(r'(\d+)', val)
        if match:
            try:
                return int(match.group(1))
            except (ValueError, TypeError):
                return None
        return None

    title_zh = clean_val(row.get("電影中文名稱"))
    title_en = clean_val(row.get("電影英文名稱"))
    genres = parse_list(row.get("類型"))
    countries = parse_list(row.get("國家"))
    year = parse_year(row.get("上映年份"))
    directors = parse_list(row.get("導演"))
    actors = parse_list(row.get("演員"))
    hami_url = clean_val(row.get("Hami詳細頁Url"))
    imdb_id = clean_val(row.get("imdb_id"))

    rt_tomatometer = parse_int_score(row.get("rt_tomatometer"))
    rt_audience_score = parse_int_score(row.get("rt_audience_score"))
    douban_rating = parse_float_score(row.get("douban_rating"))
    imdb_rating = parse_float_score(row.get("imdb_rating"))
    metacritic_rating = parse_int_score(row.get("metacritic_rating"))
    
    hami_rating = parse_float_score(row.get("hami_rating"))
    if hami_rating is None:
        hami_rating = parse_float_score(row.get("中華電信評分"))
        
    hami_duration = clean_val(row.get("hami_duration"))
    if hami_duration is None:
        hami_duration = clean_val(row.get("片長"))
        
    duration = parse_duration_minutes(hami_duration)

    search_keywords = generate_ngrams(title_zh)

    return {
        "title_zh": title_zh,
        "title_en": title_en,
        "genres": genres,
        "countries": countries,
        "year": year,
        "directors": directors,
        "actors": actors,
        "hami_url": hami_url,
        "imdb_id": imdb_id,
        "rt_tomatometer": rt_tomatometer,
        "rt_audience_score": rt_audience_score,
        "douban_rating": douban_rating,
        "imdb_rating": imdb_rating,
        "metacritic_rating": metacritic_rating,
        "duration": duration,
        "hami_duration": hami_duration,
        "hami_rating": hami_rating,
        "search_keywords": search_keywords,
    }


class SyncRequest(BaseModel):
    source_url: Optional[HttpUrl] = None


@app.post("/api/v1/movies/sync")
async def sync_movies(payload: Optional[SyncRequest] = None):
    """
    Sync endpoint called securely by Cloud Scheduler.
    Downloads the latest CSV movie list from a URL and upserts the data in batches of 500.
    """
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Firestore Database is not initialized. Please verify credentials."
        )

    source_url = None
    if payload:
        source_url = payload.source_url
    if not source_url:
        source_url = os.getenv("SYNC_DATA_URL")

    if not source_url:
        raise HTTPException(
            status_code=400,
            detail="No sync data source URL specified. Provide source_url in body or configure SYNC_DATA_URL."
        )

    print(f"Syncing movies from data source: {source_url} ...")
    
    # 1. Fetch CSV
    try:
        response = requests.get(str(source_url), timeout=30)
        response.raise_for_status()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to download CSV from {source_url}: {str(e)}"
        )

    # 2. Parse CSV
    try:
        df = pd.read_csv(io.BytesIO(response.content))
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to parse CSV data: {str(e)}"
        )

    total_records = len(df)
    if total_records == 0:
        return {
            "success": True,
            "message": "CSV file was parsed successfully but contained 0 rows.",
            "processed": 0,
            "upserted": 0,
            "errors": 0
        }

    # 3. Batch Upsert to Firestore (merge=True)
    batch = db.batch()
    batch_counter = 0
    total_committed = 0
    errors_count = 0

    now = datetime.now(timezone.utc)

    for idx, row in df.iterrows():
        try:
            doc_data = clean_row_data(row)
            imdb_id = doc_data.get("imdb_id")
            hami_url = doc_data.get("hami_url")
            
            # Determine Document ID: prioritize hami_url to prevent duplicate IMDb merges
            if hami_url and isinstance(hami_url, str):
                import hashlib
                match = re.search(r'/product/(\d+)\.do', hami_url)
                if match:
                    doc_id = f"hami_{match.group(1)}"
                else:
                    doc_id = hashlib.md5(hami_url.encode('utf-8')).hexdigest()
            elif imdb_id and isinstance(imdb_id, str) and imdb_id.startswith("tt"):
                doc_id = imdb_id.strip()
            else:
                import hashlib
                title_zh = doc_data.get("title_zh") or ""
                doc_id = hashlib.md5(title_zh.encode('utf-8')).hexdigest()

            # Set created_at and updated_at
            doc_data["created_at"] = now
            doc_data["updated_at"] = now

            doc_ref = db.collection("movies").document(doc_id)
            batch.set(doc_ref, doc_data, merge=True)
            batch_counter += 1

            if batch_counter >= 500:
                batch.commit()
                total_committed += batch_counter
                batch = db.batch()
                batch_counter = 0

        except Exception as row_err:
            print(f"[Error] Failed to process sync row at index {idx}: {row_err}")
            errors_count += 1

    # Commit remaining
    if batch_counter > 0:
        try:
            batch.commit()
            total_committed += batch_counter
        except Exception as commit_err:
            print(f"[Error] Failed to commit final sync batch: {commit_err}")
            errors_count += batch_counter

    # Reload the cache with the newly updated Firestore records
    load_cache()

    return {
        "success": True,
        "message": f"Sync operation finished. Committed {total_committed} records with {errors_count} errors.",
        "processed": total_records,
        "upserted": total_committed,
        "errors": errors_count
    }


if __name__ == "__main__":
    import uvicorn
    # Local debugging
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
