# Employee Attendance & Analytics API

A FastAPI service for employee records, attendance punches, regularizations, monthly analytics, and MongoDB query-plan inspection.

## Features

- Employee creation and filtering with paginated, sorted results
- Punch-in and punch-out with IST attendance-day handling, overnight shifts, rounding rules, and conflict-safe updates
- Attendance listing and audited manual regularization
- Monthly employee summaries, department summaries, late leaderboard with tie-aware ranking, and gap-filled department trends with a 7-day moving average
- MongoDB indexes created at application startup and an execution-stats explain endpoint

## Requirements

- Python 3.10+
- MongoDB 6.0+ (an Atlas M0 cluster is sufficient)

## Run locally

From the repository root:

1. Install packages:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env`. Paste the connection URI copied from **MongoDB Atlas → Connect → Drivers → Python**, and set the database name:

   ```dotenv
   MONGO_URI=mongodb+srv://YOUR_USERNAME:YOUR_PASSWORD@YOUR_EXACT_ATLAS_HOST/?retryWrites=true&w=majority&appName=Cluster0
   MONGO_DB=attendance_db
   ```

   Replace every placeholder with real Atlas values. Keep the URI on one line. Make sure your Atlas Network Access list includes your current public IP.

3. Start the API:

   ```powershell
   python -m uvicorn app.main:app --reload --port 8000
   ```

4. Open:
   - Interactive API documentation: http://127.0.0.1:8000/docs
   - MongoDB readiness check: http://127.0.0.1:8000/health

## API routes

- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

The API accepts instants as epoch milliseconds and stores them as UTC BSON dates. Attendance calendar dates and shift times follow the assignment's IST rules. Analytics are implemented with MongoDB aggregation pipelines.

## Security

- Never commit `.env`, real credentials, or virtual environments.
- Rotate any database password that has been shared accidentally.
- This repository intentionally excludes the assignment's sample data dump.
