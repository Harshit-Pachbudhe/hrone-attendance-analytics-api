# REVIEW.md

Defects found in the starter `app/main.py` and how the replacement addresses them.

| # | Where (starter function) | What is wrong | How to notice it | How it is fixed |
|---:|---|---|---|---|
| 1 | `compute_late_minutes` | Uses a naive wall-clock datetime and assumes the punch-in date is always the shift-start date. This breaks timezone and overnight-shift calculations. | Punch in after midnight on a 22:00–06:00 shift and compare against the previous shift start. | Convert instants to IST and calculate shift start from the attendance date. |
| 2 | `compute_late_minutes` | Uses a local/naive datetime and does not explicitly truncate instants to whole seconds before calculations. | Send a timestamp with fractional seconds near the 10-minute grace boundary. | Treat stored instants as UTC and truncate to whole seconds at the API boundary. |
| 3 | `compute_work_hours` | Python `round()` uses ties-to-even behavior rather than the required decimal half-up rule. | Test a duration whose hour value is exactly halfway at two decimal places. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Always anchors shift end to the same calendar date, so overnight shifts get the wrong end instant. | Punch out at 06:40 for a 22:00–06:00 shift. | Move shift end to the next day when `shift_end <= shift_start`. |
| 5 | `compute_overtime` | Counts any positive overtime, although the contract requires at least 30 whole minutes. | Punch out 20 minutes after shift end. | Return zero below 30 minutes; otherwise return floored whole minutes. |
| 6 | `/health` | Returns success without checking whether MongoDB responds. | Stop MongoDB and request `/health`. | Ping MongoDB and return 503 when it is unavailable. |
| 7 | `create_employee` | Check-then-insert is vulnerable to simultaneous duplicate requests; no unique database index guarantees one winner. | Send parallel requests with the same `emp_code`. | Create a unique index and translate duplicate-key errors to 409. |
| 8 | `create_employee` | Uses `datetime.now()` (machine-local, naive) and returns a datetime instead of epoch milliseconds. | Compare `created_at` with the contract or run on a non-UTC host. | Store UTC BSON datetime and serialize API instants as epoch milliseconds. |
| 9 | `list_employees` | Uses `skip = page * page_size`, skipping the first page. | Request page 1 with page size 20. | Use `(page - 1) * page_size`. |
| 10 | `list_employees` | `total` counts every employee instead of the filtered department result, and results have no stable ordering. | Filter by a department or repeat a paged request. | Count the same filter and sort by `emp_code`. |
| 11 | `punch_in` | Unknown employee causes a `None` subscript error (500), rather than 404. | Punch in using an unregistered code. | Check the employee first and return 404. |
| 12 | `punch_in` | `if body.punched_at` treats zero as “not supplied”; it also accepts implicit local-time conversion and does not enforce epoch-millisecond bounds/types. | Supply seconds, a float, zero, or a timestamp near the range boundary. | Validate strict integer milliseconds and convert from UTC. |
| 13 | `punch_in` | Uses the host's local date instead of the IST attendance date and does not implement the overnight attendance-date rule. | Use a timestamp near midnight IST or an overnight shift. | Derive the attendance date in IST and associate post-midnight punches with the prior shift day when required. |
| 14 | `punch_in` | Check-then-insert can create duplicate attendance records under concurrency. | Send simultaneous punch-ins for one employee/date. | Enforce unique `(emp_code, date)` in MongoDB and handle duplicate-key errors as 409. |
| 15 | `punch_in` | Does not validate employee existence/status values and does not truncate instants before storing. | Use an unknown employee, `ABSENT` status, or fractional-second timestamp. | Validate the request model, restrict punch-in to presence statuses, and truncate to whole seconds. |
| 16 | `list_attendance` | Loads the entire matching collection into Python and sorts/slices in memory; this is slow at 100,000 records. | Run with a large attendance collection and inspect memory/query time. | Count in MongoDB and apply indexed sort, skip, and limit in the query. |
| 17 | `list_attendance` | Sorts only by date, leaving same-date order unspecified, and returns an `id` derived from MongoDB `_id`, contrary to the natural-key contract. | Create multiple employees' records on the same date and inspect fields/order. | Sort by date descending then `emp_code` ascending; omit `_id` and `id`. |
| 18 | `list_attendance` | Does not validate date formats, reversed date ranges, status values, or pagination bounds. | Try `date_from > date_to`, an invalid status, page 0, or page size 101. | Validate query parameters and return 422 for invalid inputs. |
| 19 | Starter endpoint set | Punch-out, manual regularization, four analytics endpoints, and explain are absent. | Request the paths specified in `openapi.yaml`. | Implement each contract path and response shape. |
| 20 | Analytics design in starter | The required monthly, department, leaderboard, and trend reports need indexed MongoDB aggregation; fetching all logs and calculating in Python would not scale or handle zero-log employees correctly. | Test employees without logs, weekend logs, ranking ties, and empty calendar days. | Use aggregation pipelines, lookups from employees, window ranking, and date densification. |

## Examined and not considered defects

- `emp_code` is the correct business identifier; MongoDB `_id` is internal and must not be exposed.
- `PRESENT` as the default punch-in status is correct.
- The starter attendance-list slice `(page - 1) * page_size : page * page_size` uses the right offset formula; the main problems are that it occurs after loading/sorting all matching documents and the sort is incomplete.
- A 404 response for `/` is not a defect: the contract defines `/health` and the API routes, not a root page.
