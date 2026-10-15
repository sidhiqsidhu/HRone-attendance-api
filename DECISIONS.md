# Design decisions

1. **Indexes.** I create a unique index on `employees.emp_code` because the code is the business identifier. I create a unique compound index on `attendance_logs.emp_code` and `date` because an employee can have only one record for an attendance date. Additional indexes support attendance lookup by employee and punch time, date-range listing, and department/join-date filtering. I considered an index on every derived analytics field, but rejected it because the analytics group and sort values rather than perform simple point lookups.

2. **Punch-in race.** Both requests may read that no record exists, but neither relies on that check for correctness. The first insert accepted by MongoDB wins the unique `(emp_code, date)` constraint. The other insert raises `DuplicateKeyError`, which the API converts to HTTP 409.

3. **Ties.** Results are sorted by total late minutes descending and employee code ascending. Ranking uses competition ranking, so equal totals receive the same rank. The response includes entries whose rank is within `limit`, which avoids arbitrary tie-breaking at the cutoff.

4. **Headcount.** Department summaries begin with employees whose `joined_on` is on or before the end of the month. Attendance logs are then aggregated for those employee codes, so an employee with no logs still contributes to headcount.

5. **At 100x the data,** I would replace repeated per-department queries and day-by-day headcount queries with one aggregation pipeline using `$lookup` and a calendar boundary pipeline, then inspect the explain plans and add compound indexes based on measured workload.
