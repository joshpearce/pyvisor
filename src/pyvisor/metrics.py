"""Small, dependency-free Prometheus metrics for service lifecycle events."""

from __future__ import annotations

from collections import Counter

from pyvisor.models import JobStatus


class LifecycleMetrics:
    """In-process counters rendered in Prometheus's text exposition format."""

    def __init__(self) -> None:
        self._terminal_jobs: Counter[JobStatus] = Counter()
        self._runtime_reaps = 0
        self._runtime_reap_failures = 0
        self._runtime_orphans_reconciled = 0
        self._jobs_reconciled = 0

    def job_terminal(self, status: JobStatus) -> None:
        self._terminal_jobs[status] += 1

    def runtime_reaped(self) -> None:
        self._runtime_reaps += 1

    def runtime_reap_failed(self) -> None:
        self._runtime_reap_failures += 1

    def runtime_orphans_reconciled(self, count: int) -> None:
        self._runtime_orphans_reconciled += count

    def job_reconciled(self) -> None:
        self._jobs_reconciled += 1

    def render(self, *, running_jobs: int) -> str:
        """Render a deterministic Prometheus payload without dynamic labels."""
        lines = [
            "# HELP pyvisor_jobs_terminal_total Terminal jobs by final status.",
            "# TYPE pyvisor_jobs_terminal_total counter",
        ]
        for status in ("completed", "timed_out", "cancelled", "runtime_error"):
            lines.append(
                f'pyvisor_jobs_terminal_total{{status="{status}"}} {self._terminal_jobs[status]}'
            )
        lines.extend(
            (
                "# HELP pyvisor_jobs_running Current live jobs.",
                "# TYPE pyvisor_jobs_running gauge",
                f"pyvisor_jobs_running {running_jobs}",
                "# HELP pyvisor_runtime_reaps_total Warm runtimes reaped after idling.",
                "# TYPE pyvisor_runtime_reaps_total counter",
                f"pyvisor_runtime_reaps_total {self._runtime_reaps}",
                "# HELP pyvisor_runtime_reap_failures_total Failed warm-runtime reaps.",
                "# TYPE pyvisor_runtime_reap_failures_total counter",
                f"pyvisor_runtime_reap_failures_total {self._runtime_reap_failures}",
                "# HELP pyvisor_runtime_orphans_reconciled_total Managed runtimes removed at startup.",
                "# TYPE pyvisor_runtime_orphans_reconciled_total counter",
                f"pyvisor_runtime_orphans_reconciled_total {self._runtime_orphans_reconciled}",
                "# HELP pyvisor_jobs_reconciled_total Interrupted jobs terminalized at startup.",
                "# TYPE pyvisor_jobs_reconciled_total counter",
                f"pyvisor_jobs_reconciled_total {self._jobs_reconciled}",
            )
        )
        return "\n".join(lines) + "\n"
