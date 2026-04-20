"""Desludging schedule slot parsing and driver/helper double-booking checks."""

from __future__ import annotations


def normalize_schedule_time(time_str: str) -> str:
    """Normalize time string for comparison (whitespace + case)."""
    return " ".join((time_str or "").strip().lower().split())


def normalize_personnel_name(name: str) -> str:
    return " ".join((name or "").strip().split()).casefold()


def get_desludging_timeslot_for_request(service_request):
    """
    Return (scheduled_date|None, normalized_time|None) from the request.
    Time is parsed from the latest 'Desludging scheduled on ... at ...' note line.
    """
    d = service_request.scheduled_desludging_date
    t_norm = None
    notes = service_request.notes or ""
    marker = "Desludging scheduled on "
    dl_idx = notes.rfind(marker)
    if dl_idx != -1:
        dl_segment = notes[dl_idx + len(marker) :]
        try:
            if " at " in dl_segment:
                _, time_part = dl_segment.split(" at ", 1)
                if "Reason:" in time_part:
                    time_only, _ = time_part.split("Reason:", 1)
                    raw_t = time_only.strip().rstrip(".")
                else:
                    raw_t = time_part.strip().rstrip(".")
                if raw_t:
                    t_norm = normalize_schedule_time(raw_t)
        except ValueError:
            pass
    return d, t_norm


def completion_personnel_norm_set(completion) -> set[str]:
    names = [completion.driver_name]
    for h in (completion.helper1_name, completion.helper2_name, completion.helper3_name):
        if h:
            names.append(h)
    return {normalize_personnel_name(n) for n in names if n}


def find_personnel_schedule_conflicts(
    *,
    exclude_request_id: int,
    sched_date,
    sched_time_normalized: str | None,
    selected_names: list[str],
) -> list[dict]:
    """
    Find other desludging jobs with the same date + time where completion already
    lists a driver/helper that overlaps the selected names.

    Requires both sched_date and sched_time_normalized to be present; otherwise returns [].
    """
    from .models import ServiceRequest

    if sched_date is None or not sched_time_normalized:
        return []

    selected_norms = {normalize_personnel_name(n) for n in selected_names if (n or "").strip()}
    if not selected_norms:
        return []

    candidates = (
        ServiceRequest.objects.filter(
            scheduled_desludging_date=sched_date,
            status__in=[
                ServiceRequest.Status.DESLUDGING_SCHEDULED,
                ServiceRequest.Status.COMPLETED,
            ],
        )
        .exclude(pk=exclude_request_id)
        .select_related("completion_info")
    )

    conflicts: list[dict] = []
    for other in candidates:
        _, ot = get_desludging_timeslot_for_request(other)
        if not ot or ot != sched_time_normalized:
            continue
        comp = getattr(other, "completion_info", None)
        if not comp:
            continue
        other_names = completion_personnel_norm_set(comp)
        overlap = selected_norms & other_names
        if not overlap:
            continue
        display_overlap = sorted(overlap)
        conflicts.append(
            {
                "request_id": other.pk,
                "client_name": other.client_name,
                "overlap_names": display_overlap,
            }
        )
    return conflicts
