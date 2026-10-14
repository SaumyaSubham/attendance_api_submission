# Employee Attendance & Analytics API

A FastAPI and MongoDB implementation of the HROne Software Engineer Trainee assignment. It provides employee and attendance operations, business-rule calculations, attendance corrections with audit history, four analytics endpoints, and query explain output.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+ (MongoDB Atlas works).

1. Install dependencies: `python -m pip install -r requirements.txt`.
2. Create a local `.env` file with `MONGO_URI` and `MONGO_DB`. Never commit this file or share its credentials.
3. Start the API from this folder: `uvicorn app.main:app --port 8000`.
4. Open `http://127.0.0.1:8000/docs` to explore the API. `GET /health` checks the MongoDB connection.

The candidate kit provides `sample_seed.py` and sample documents for local testing. The seed script clears the `employees` and `attendance_logs` collections in the selected database before loading the sample records; use it only with a development database. The sample files are intentionally not included in this repository.

## Verification

Manually checked against MongoDB Atlas with the kit's sample data: health check; employee creation/listing; punch-in and punch-out; attendance listing; all four analytics endpoints; and an attendance correction whose reason and reviewer appeared in the audit history. All five explain endpoints returned 200. Each explain response showed `IXSCAN`; `COLLSCAN` was not found in any of the five responses with the sample data.

GET /health
![GET /health](https://imgh.in/host/kp1tfo)

department summary
![Department summary](https://imgh.in/host/1yxqdw)

department trend
![Department trend](https://imgh.in/host/s44a1m)


Explain checks used only the kit's nine sample documents; query plans and performance at the assignment's larger target scale (about 100,000 rows) have not been verified.

See [REVIEW.md](REVIEW.md) for the starter-code review and [DECISIONS.md](DECISIONS.md) for design decisions.
