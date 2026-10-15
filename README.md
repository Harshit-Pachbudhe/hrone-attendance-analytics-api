# Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the HROne attendance assignment. All application code is in `app/main.py`.

## Run locally

Python 3.11+ and MongoDB 6.0+ are required.

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file in the repository root (do not commit it):

```dotenv
MONGO_URI=mongodb+srv://<database-user>:<URL-encoded-password>@<cluster-host>/?appName=AttendanceCluster
MONGO_DB=attendance_db
```

Use the connection string from Atlas and replace the placeholders. If the password contains reserved URL characters, URL-encode it. Environment variables already set by the runner take precedence over `.env`.

Start the API from the repository root:

```powershell
uvicorn app.main:app --port 8000 --reload
```

Open `http://127.0.0.1:8000/docs`; `GET /health` checks the MongoDB connection. Required indexes are created idempotently at startup.

Only load `sample_data/` into a disposable database. Do not run the sample seeder against a database whose contents you need to preserve.

## Validation Status

- Manual API checks completed for employee creation, attendance punch-in and punch-out, duplicate punch-in handling, unknown employee handling, attendance listing, monthly analytics, department summaries, late-arrival leaderboard, and department trends.
- Explain endpoints were tested individually. Query-plan findings and the department-trend explain limitation are documented accurately.
- Business-rule edge cases and concurrency tests: [list the cases actually tested].
- The hidden evaluation dataset and performance at approximately 100,000 attendance records have not been independently verified.