# Employee Attendance & Analytics API

A FastAPI application for employee management, attendance punches, and attendance analytics using MongoDB Atlas.

## Features

- Employee creation and paginated listing
- Attendance punch-in and attendance listing
- MongoDB-backed persistence and indexes
- Health endpoint at `/health`
- Interactive OpenAPI documentation at `/docs`

> This repository currently contains the starter implementation. Several endpoints listed in the assignment contract remain to be implemented; see the TODO list in `app/main.py`.

## Requirements

- Python 3.10+
- MongoDB Atlas free cluster (or a local MongoDB instance)

## Setup (Windows PowerShell)

1. Clone the repository and enter the project directory:

   ```powershell
   git clone https://github.com/Nageswarreddy06/attendance-api.git
   cd attendance-api
   ```

2. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Create a local `.env` file in this directory. Do not commit it:

   ```dotenv
   MONGO_URI=mongodb+srv://YOUR_USERNAME:YOUR_PASSWORD@YOUR_ATLAS_HOST/?retryWrites=true&w=majority&appName=Cluster0
   MONGO_DB=attendance_db
   ```

   Replace the placeholders with the URI copied from **Atlas → Connect → Drivers → Python**. Keep the URI on one line.

4. Start the API:

   ```powershell
   python -m uvicorn app.main:app --reload --port 8000
   ```

5. Visit:
   - API docs: http://127.0.0.1:8000/docs
   - Health check: http://127.0.0.1:8000/health

## Security

- Never commit `.env`, database credentials, or other secrets.
- Rotate any database password that has been accidentally shared.
- Confirm MongoDB Atlas Network Access allows your current IP address.

## Project structure

```text
.
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
│   ├── attendance_logs.json
│   └── employees.json
├── .env.example
├── requirements.txt
├── openapi.yaml
├── DATA_MODEL.md
├── DECISIONS.md
├── REVIEW.md
├── MONGODB_SETUP.md
└── PROBLEM_STATEMENT.docx
```
