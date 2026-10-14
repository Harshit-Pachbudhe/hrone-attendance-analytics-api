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

## Validation note

The Python file has been syntax-compiled. Full end-to-end verification against a live MongoDB instance and the hidden evaluation dataset remains to be run before submission.
