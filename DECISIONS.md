# DECISIONS.md

1. **Indexes.** A unique `employees.emp_code` index prevents duplicate employee codes. A unique `(emp_code, date)` index enforces one attendance record per employee/day and supports monthly lookups. `(date DESC, emp_code ASC)` supports attendance-list sorting; `(status, date, emp_code)` helps status filters. Employee indexes on `(department, joined_on)` and `(joined_on, department)` support headcount and join-date queries. I avoid indexing every field because indexes consume storage and slow writes.

2. **Punch-in race.** Concurrent requests may both see no record, but only one insert can satisfy the unique `(emp_code, date)` index. MongoDB rejects the other with a duplicate-key error, which becomes 409. The winner returns 201.

3. **Ties.** The leaderboard uses standard competition ranking by total late minutes. Everyone with rank at or below `limit` is returned, including all employees tied at the cutoff; ties are ordered by `emp_code`.

4. **Headcount.** The department summary starts from eligible employees and looks up logs. Employees with no logs remain in the pipeline and still count toward headcount.

5. **100x data.** I would measure explain plans and latency, then consider pre-aggregated summaries and archival before introducing sharding.
