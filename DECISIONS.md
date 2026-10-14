# Design Decisions

1. **Indexes.** I create unique indexes on `employees.emp_code` and attendance `(emp_code, date)` to enforce identity and prevent duplicate daily records. Department/code and department/join-date indexes serve employee lists, summaries, and trends. Attendance indexes support employee/date lookup, punch-out ordering, date-sorted pages, and month/late filtering. I considered a standalone `late_minutes` index but rejected it: monthly queries filter by date first, and late minutes alone selects many records.

2. **Punch-in race.** Two requests can both read the employee, but neither checks for an existing log. They insert the same natural key; the unique index accepts one, and the other request becomes a 409.

3. **Ties.** `$rank` uses total late minutes alone, so tied employees share a rank and the next rank is skipped. I apply the cutoff after ranking, keeping every employee tied at an included rank, then sort those rows by `emp_code`.

4. **Headcount.** The department pipeline starts with employees who joined by month end and looks up their logs. Employees with no matching logs remain in the pipeline and count once.

5. **100x data.** I would measure query plans and latency on representative data, then consider pre-aggregated daily summaries and cursor pagination for large result sets.
