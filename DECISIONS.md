# DECISIONS.md

## 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employees, and a unique compound index on `attendance_logs (emp_code, date)` to guarantee one attendance record per employee per day. The `(date DESC, emp_code ASC)` index supports attendance-list sorting, while `(status, date, emp_code)` helps status-filtered queries. Employee indexes on `(department, joined_on)` and `(joined_on, department)` support department and joining-date queries. I avoided adding standalone indexes for every field because indexes increase storage requirements and write overhead.

## 2. Punch-in race

Two simultaneous requests may both check for an existing record. However, the unique `(emp_code, date)` index allows only one insertion. The successful request returns HTTP 201; the other receives a duplicate-key error that the API converts to HTTP 409.

## 3. Leaderboard ties

The leaderboard uses standard competition ranking. Employees tied on total late minutes share the same rank, and the next rank is skipped. The limit is applied after ranking, so all employees tied at the cutoff are returned. Tied employees are ordered by `emp_code`.

## 4. Department headcount

The department summary starts from eligible employees and looks up attendance records. Employees without logs remain included in headcount, while employees who joined after the reporting period are excluded.

## 5. Scaling to 100 times the data

I would measure query plans and latency, optimize indexes based on actual workloads, and consider pre-aggregated summaries and archival. I would evaluate sharding only if profiling showed that a single database could no longer meet performance requirements.