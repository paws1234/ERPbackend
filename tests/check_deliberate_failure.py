"""T-0.CICD.01 proof — a deliberately failing check, never for merge.

Pushed on a scratch branch to observe two things at once: the pipeline's loop
picks a new `tests/check_*.py` up without the workflow being edited, and a red
check fails the run so `publish` and `deploy` (which need it) never run.
"""

assert False, "T-0.CICD.01: this check must fail the pipeline"
