# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Employee creation | The starter did not enforce uniqueness safely for concurrent requests. | Send two creates with the same `emp_code` at the same time. | Added a unique MongoDB index and translated `DuplicateKeyError` to 409. |
| 2 | Employee listing | Pagination skipped `page * page_size` records, so page 1 started after the first page. | Request page 1 with a small page size and observe missing employees. | Use `(page - 1) * page_size` and keep sorting deterministic. |
| 3 | Punch-in | The starter did not validate that the employee existed or prevent duplicate attendance records. | Punch in with an unknown employee or twice on one attendance date. | Check the employee first and enforce a unique `(emp_code, date)` index. |
| 4 | Punch-in and punch-out calculations | Time-zone handling and derived business fields were incomplete. | Test the ten-minute grace period, overtime threshold, half-day rule, and an overnight shift. | Normalize instants to UTC, calculate calendar dates in IST, truncate to seconds, and compute derived fields server-side. |
| 5 | Punch-out | The starter did not safely close the correct open record or validate invalid time ranges. | Punch out before punch-in, more than 24 hours later, or across midnight. | Select the most recent eligible open record and validate the time range before updating it. |
| 6 | Regularization | Manual corrections and their audit trail were missing. | Correct a punch time and inspect the saved attendance document. | Recompute derived values and append one history entry containing only changed fields. |
| 7 | Attendance listing | Filtering and pagination were incomplete and could produce incorrect totals. | Combine employee, date, status, and page filters. | Build one MongoDB filter, count that filtered query, sort, skip, and limit. |
| 8 | Analytics | The starter did not provide the required monthly, department, leaderboard, trend, and explain endpoints. | Call the analytics paths from the OpenAPI contract. | Added the endpoints and used MongoDB aggregation for attendance summaries and rankings. |

I also checked the starter's basic FastAPI route structure and kept it because the paths and HTTP methods already matched the contract. The MongoDB-backed design was retained because the assignment explicitly evaluates database indexes and aggregation behavior.
