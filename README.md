# Employee Attendance API

Python 3.11+, FastAPI, and MongoDB 6.0+ implementation of the HROne attendance assignment. The app reads `MONGO_URI` and `MONGO_DB` from the environment and creates its indexes at startup.

## Run

Install the packages with `python -m pip install -r requirements.txt`, set `MONGO_URI` and `MONGO_DB`, then run `uvicorn app.main:app --port 8000` from this directory. `GET /health` returns 200 when MongoDB responds and 503 otherwise.

The source kit's sample data is not included in this repository. To try the analytics locally, load the provided sample documents into the configured database first. No `.env`, credentials, data dump, virtual environment, or Dockerfile belongs in the submission.

## Scope

The API implements employee creation/listing, punch-in/out, attendance listing and regularization, four analytics endpoints, and query explain output. See `REVIEW.md` for starter defects and `DECISIONS.md` for the design choices. Review the MongoDB aggregation pipelines in `app/main.py` before the live walkthrough; they are the core of the analytics.


![GET /health](https://imgh.in/host/4h255l)
