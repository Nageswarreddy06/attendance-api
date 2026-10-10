# DECISIONS.md

1. **Indexes.** The unique `emp_code` index protects employee identity and supports single-employee lookups. The unique compound `(emp_code, date)` index both prevents duplicate attendance days under races and supports an employee's date history. `(date desc, emp_code asc)` supports attendance date filters/sort; `(status, date desc, emp_code asc)` supports status-filtered listing; `(emp_code, punch_in desc)` helps locate the latest punch-in for punch-out. Employee department/code and joined-on indexes support sorted department lists, month headcount filters, and trend lookups. I avoided a separate `half_day` index because it is low-selectivity and is not a leading filter in these queries.

2. **Punch-in race.** Requests insert directly after validating the employee. The database unique index chooses the winner atomically: one insert returns 201; a duplicate insert raises `DuplicateKeyError`, mapped to 409. A pre-check alone would not be race-safe.

3. **Ties.** The leaderboard ranks only by total late minutes with MongoDB `$rank`, then sorts tied rows by `emp_code`. The limit is applied to rank, so all employees tied at the cutoff rank are returned.

4. **Headcount.** Department summary starts from employees who joined by month end, then looks up their logs. Employees with no logs remain in the aggregation and still contribute to headcount.

5. **100x scale.** I would benchmark explain plans against production-like data, add a managed load test and monitoring, and consider materialized daily summaries for expensive analytics before introducing sharding. Indexes must be measured against their write cost.
