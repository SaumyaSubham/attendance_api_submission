# Starter Code Review

| # | Where | What is wrong | How to notice | How it is fixed |
|---|---|---|---|---|
| 1 | `health` | Always reports ready without contacting MongoDB. | Stop MongoDB; it still returns 200. | Ping MongoDB and return 503 on a PyMongo error. |
| 2 | `compute_late_minutes` | Treats the punch instant's clock fields as the shift date and does not convert UTC to IST. | Use a UTC timestamp for an IST shift or an overnight punch. | Compare UTC instants against an IST shift start anchored to the attendance date. |
| 3 | `compute_late_minutes` | The integer-minute comparison misses the strict 10-minute boundary: 09:40:01 can still be treated as 10 and rejected as late. | Test 09:40:00 and 09:40:01 for a 09:30 shift. | Compare elapsed seconds to 600 first, then floor total minutes. |
| 4 | `compute_late_minutes` | Does not account for an early-morning punch belonging to the prior overnight shift date. | Punch in at 02:00 for a 22:00-06:00 shift. | Derive the attendance day with the overnight rule before calculating lateness. |
| 5 | `compute_work_hours` | Python `round` does not guarantee the contract's decimal half-up rounding. | Choose a duration exactly on a two-decimal half boundary. | Use `Decimal` and `ROUND_HALF_UP` after truncating instants to seconds. |
| 6 | `compute_overtime` | Combines a naive local datetime with an aware IST datetime, which can raise `TypeError`. | Call it with the starter's naive `datetime.fromtimestamp` value. | Keep all instants aware UTC and compare with an aware IST shift end. |
| 7 | `compute_overtime` | Uses the attendance date for every shift and misses the following-day end of overnight shifts. | Punch out after midnight on a 22:00-06:00 shift. | Anchor overnight shift end on the next calendar day. |
| 8 | `compute_overtime` | Counts any positive overtime; the required minimum is 30 whole minutes. | Punch out 29 minutes after shift end. | Floor to whole minutes and report only when the result is at least 30. |
| 9 | Request models | They accept malformed employee codes, statuses, dates, times, and non-millisecond timestamps. | Send a seconds-looking timestamp or an unknown status. | Add strict Pydantic validation and reject out-of-range values with 422. |
| 10 | Index/startup setup | No indexes are created, so duplicate checks race and common queries can scan the collections. | Send simultaneous duplicate employee or punch-in requests; inspect explain plans. | Create idempotent unique and query indexes at startup. |
| 11 | `create_employee` | The duplicate check is check-then-insert and cannot guarantee one winner under concurrency. | Issue parallel creates with the same `emp_code`. | Rely on the unique database index and translate duplicate-key errors to 409. |
| 12 | `create_employee` | `created_at` is naive and returned as a datetime, not epoch milliseconds. | Inspect the JSON response and run under a different local timezone. | Store aware UTC and serialize instants as integer milliseconds. |
| 13 | `list_employees` | `skip = page * page_size` skips the first page, and `total` ignores the department filter. | Compare page 1 and page 2; filter by department. | Use `(page - 1) * page_size`, filtered counts, an index-backed sort, and pagination bounds. |
| 14 | `list_employees` | No deterministic `emp_code` sort is requested. | Insert codes in a different order and repeat the request. | Sort ascending by `emp_code`. |
| 15 | `punch_in` | An unknown employee is dereferenced instead of returning 404. | Punch in with an unknown code. | Check the employee lookup and return 404. |
| 16 | `punch_in` | `if body.punched_at` treats zero as omission; `fromtimestamp` uses machine-local time and retains fractional seconds. | Supply zero, run in another timezone, or send milliseconds not divisible by 1000. | Validate epoch milliseconds, convert as UTC, and truncate to whole seconds. |
| 17 | `punch_in` | The attendance date is taken from the host's local date, not IST with overnight handling. | Test near midnight UTC and early morning for an overnight shift. | Derive the attendance date from the IST instant and shift. |
| 18 | `punch_in` | Check-then-insert can create duplicate records for parallel requests. | Send identical simultaneous punch-in requests. | Enforce a unique `(emp_code, date)` index and map the losing insert to 409. |
| 19 | `punch_in` | It does not reject a missing employee or invalid presence status cleanly, and returns an extraneous `id`. | Use an unknown code, `ABSENT`, or compare the body to the contract. | Validate before writing and return only the contract's natural-key record fields. |
| 20 | `list_attendance` | It loads every matching document and sorts/pages in Python. | Use a large collection and observe memory and latency. | Count and page in MongoDB with indexed filters and stable sort. |
| 21 | `list_attendance` | It sorts only by date, leaves ties unstable, and returns an `id` not in the contract. | Create records on the same date for multiple employees. | Sort date descending then employee code ascending; serialize no Mongo identifier. |
| 22 | `list_attendance` | It does not validate date order, status, or page bounds. | Supply reversed dates or page size 101. | Validate inclusive date filters and pagination/status values before querying. |

## Examined and retained

Using `emp_code` for employee lookup is correct because it is the assigned business key. The starter's default punch status of `PRESENT` and default shift times are also consistent with the API contract. The absent punch-out, regularization, analytics, and explain handlers were explicitly marked TODO in the starter, so they were missing assignment work rather than defects in an existing implementation.
