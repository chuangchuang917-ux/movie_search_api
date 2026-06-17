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
from typing import Optional
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google.cloud import firestore
from dotenv import load_dotenv

# Load local environment variables (useful for local development)
load_dotenv()

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


if __name__ == "__main__":
    import uvicorn
    # Local debugging
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
