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
    version="1.0.0"
)

# Configure CORS: Allow all origins to enable cross-origin requests from web/mobile clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


@app.get("/")
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
        "project_id": db.project
    }


@app.get("/api/v1/movies/search")
async def search_movies(
    keyword: Optional[str] = Query(None, description="Fuzzy search keyword for movie's Chinese title"),
    genre: Optional[str] = Query(None, description="Filter by movie genre (exact match in genres list, e.g. 動作)"),
    page: int = Query(1, ge=1, description="Page number (starts at 1)"),
    limit: int = Query(20, ge=1, le=100, description="Number of results per page (default: 20, max: 100)")
):
    """
    Search and filter movies from Firestore 'movies' collection.
    
    Query Logic:
    1. If `keyword` is provided: Queries 'search_keywords' array using 'array_contains'.
       If `genre` is also provided, filters results in-memory.
    2. If `genre` is provided (but no keyword): Queries 'genres' array using 'array_contains' in Firestore.
    3. If neither is provided: Fetches movies ordered by updated_at descending.
    
    Results are sorted by rating (imdb_rating) descending and release year descending in-memory to provide premium search UX.
    """
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Firestore Database is not initialized. Please verify credentials."
        )

    try:
        movies_ref = db.collection("movies")
        results = []

        # Helper to format and serialize Firestore documents
        def format_doc(doc):
            data = doc.to_dict()
            data["id"] = doc.id
            # ISO format date strings for JSON compatibility
            if "created_at" in data and data["created_at"]:
                data["created_at"] = data["created_at"].isoformat()
            if "updated_at" in data and data["updated_at"]:
                data["updated_at"] = data["updated_at"].isoformat()
            return data

        # ----------------------------------------------------
        # Scenario 1: Keyword-based Search (Fuzzy Title Match)
        # ----------------------------------------------------
        if keyword:
            keyword_clean = keyword.strip()
            # Perform array-contains lookup on ngram index using modern FieldFilter syntax
            query = movies_ref.where(filter=firestore.FieldFilter("search_keywords", "array_contains", keyword_clean))
            docs = query.stream()

            for doc in docs:
                data = format_doc(doc)
                # Apply secondary filtering in-memory for genre if specified
                if genre:
                    genres_list = data.get("genres", [])
                    if genre in genres_list:
                        results.append(data)
                else:
                    results.append(data)

            # Sort results in-memory (sort by imdb_rating descending, then year descending)
            # Handle possible None values in sorting fields gracefully
            results.sort(
                key=lambda x: (x.get("imdb_rating") or 0.0, x.get("year") or 0),
                reverse=True
            )

            # In-memory Pagination
            total_items = len(results)
            start_idx = (page - 1) * limit
            end_idx = start_idx + limit
            paginated_results = results[start_idx:end_idx]

        # ----------------------------------------------------
        # Scenario 2: Genre-based Search Only (No Keyword)
        # ----------------------------------------------------
        elif genre:
            genre_clean = genre.strip()
            # Query by genre (exact match in array) using modern FieldFilter syntax
            query = movies_ref.where(filter=firestore.FieldFilter("genres", "array_contains", genre_clean))
            
            # Since Firestore does pagination on serverside, apply limit and offset
            offset = (page - 1) * limit
            query_paginated = query.limit(limit).offset(offset)
            docs = query_paginated.stream()
            
            paginated_results = [format_doc(doc) for doc in docs]
            total_items = None  # Offset queries do not easily expose total count without a separate count query

        # ----------------------------------------------------
        # Scenario 3: Retrieve All (No Keyword and No Genre)
        # ----------------------------------------------------
        else:
            # Query all ordered by updated_at descending
            query = movies_ref.order_by("updated_at", direction=firestore.Query.DESCENDING)
            offset = (page - 1) * limit
            query_paginated = query.limit(limit).offset(offset)
            docs = query_paginated.stream()
            
            paginated_results = [format_doc(doc) for doc in docs]
            total_items = None

        return {
            "success": True,
            "page": page,
            "limit": limit,
            "total": total_items,
            "results": paginated_results
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Database query failed: {str(e)}"
        )


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
            
            # Determine Document ID
            if imdb_id and isinstance(imdb_id, str) and imdb_id.startswith("tt"):
                doc_id = imdb_id.strip()
            else:
                doc_id = uuid.uuid4().hex

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
