# Movie Search API (FastAPI & Google Cloud Run)

This directory contains a lightweight Python FastAPI service that connects to Google Cloud Firestore and provides movie search and filtering endpoints.

---

## Folder Structure

```
movie_search_api/
├── Dockerfile          # Multi-stage optimized Docker setup for Cloud Run
├── main.py             # FastAPI entrypoint, routes, and Firestore query logic
├── requirements.txt    # Python packages needed
└── README.md           # This developer guide
```

---

## 1. Local Development Setup

To run the API server locally:

### Step 1: Initialize Virtual Environment and Dependencies
```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
# On Windows:
venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

# Install requirements
pip install -r requirements.txt
```

### Step 2: Configure Environment Variables
Create a `.env` file in the `movie_search_api` folder:
```env
# Path to your Firestore service account key file
GOOGLE_APPLICATION_CREDENTIALS=../hami_review/hami-review-42791ab934c4.json
PORT=8000
```

### Step 3: Run the Server
```bash
python main.py
```
Or run directly with uvicorn:
```bash
uvicorn main:app --reload --port 8000
```
Visit http://127.0.0.1:8000/docs to view the interactive Swagger API documentation.

---

## 2. API Endpoints

### Health Check
* **Route**: `GET /health` or `GET /`
* **Purpose**: Verifies server connection to Firestore.
* **Response**:
  ```json
  {
    "status": "healthy",
    "message": "FastAPI service is online and connected to Firestore.",
    "database_connected": true,
    "project_id": "hami-review"
  }
  ```

### Movie Search & Filtering
* **Route**: `GET /api/v1/movies/search`
* **Parameters**:
  * `keyword` (Optional string): Search keyword matching Chinese movie titles.
  * `genre` (Optional string): Filter by genre (exact string match in genres array, e.g. `動作`).
  * `page` (Optional integer, default: `1`): Pagination page index.
  * `limit` (Optional integer, default: `20`, max: `100`): Results per page.
* **Example Requests**:
  * Search by Chinese title keyword:
    `http://127.0.0.1:8000/api/v1/movies/search?keyword=捍衛`
  * Filter by genre:
    `http://127.0.0.1:8000/api/v1/movies/search?genre=動作`
  * Combined query with pagination:
    `http://127.0.0.1:8000/api/v1/movies/search?keyword=捍衛&genre=動作&page=1&limit=5`

---

## 3. Run Locally with Docker

To test containerization before deploying:

```bash
# Build the Docker image
docker build -t movie-search-api .

# Run the container (mounting the credential key for local testing)
docker run -p 8080:8080 \
  -e GOOGLE_APPLICATION_CREDENTIALS=/app/credentials.json \
  -v "C:\Users\alber\Desktop\antigravity\hami_review\hami-review-42791ab934c4.json:/app/credentials.json" \
  movie-search-api
```
The API will be available at http://127.0.0.1:8080/health.

---

## 4. Deploying to Google Cloud Run

To deploy this API to production on GCP:

### Step 1: Authenticate with GCP
```bash
gcloud auth login
gcloud config set project hami-review
```

### Step 2: Build and Push to Artifact Registry / GCR
Submit your code to Google Cloud Build to compile the container image:
```bash
gcloud builds submit --tag gcr.io/hami-review/movie-search-api .
```

### Step 3: Deploy to Cloud Run
Deploy the compiled container image onto Cloud Run:
```bash
gcloud run deploy movie-search-api \
  --image gcr.io/hami-review/movie-search-api \
  --platform managed \
  --region asia-east1 \
  --allow-unauthenticated
```

*Note: Since the API connects to Firestore, the Cloud Run instance automatically uses the Default Service Account of Cloud Run. Ensure this service account has the **Cloud Datastore User** (`roles/datastore.user`) role assigned in IAM console.*
