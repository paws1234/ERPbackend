"""Human resources and payroll (§2.6) — Phase 5.

:mod:`app.hr.employees` is the employee role on the shared party (T-0.PARTY.01): the
employment record, the contract history behind the current terms, the assets the employee
holds, and the field-restricted personal details and salary (T-0.SEC.01).

:mod:`app.hr.org` is where somebody sits (T-5.EMP.02): a dated, append-only reporting
hierarchy that refuses the cycles and the second root a tree cannot have, and the chart read
that renders it.

:mod:`app.hr.movements` is the lifecycle (T-5.EMP.03): joining, transfers, salary revisions
and exit as dated facts with an actor and a reason, and the readings the rest of the phase
consumes — who is active on a date, and what portion of a period they were employed for.

The rest of the phase lands beside them: shifts and attendance (T-5.ATT.*), leave
(T-5.LEAVE.*) and the payroll engine with its statutory rules, payslips and banking files
(T-5.PAY.*). Every one of them reads the employee these modules create rather than a second
copy of it.
"""
