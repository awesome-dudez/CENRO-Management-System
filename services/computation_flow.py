"""Rules for when a fee computation may be finalized and sent to the customer."""

from __future__ import annotations

from services.models import ServiceRequest


def stored_filefield_exists(filefield) -> bool:
    """True if a FileField/ImageField points at a file that is present in storage."""
    if not filefield or not getattr(filefield, "name", None):
        return False
    try:
        return filefield.storage.exists(filefield.name)
    except Exception:
        return False


def inspection_is_waived(service_request: ServiceRequest) -> bool:
    return "[NO_INSPECTION_FEE]" in (service_request.notes or "")


def computation_finalize_blockers(
    service_request: ServiceRequest,
    computation,
    *,
    uploaded_signature,
) -> list[str]:
    """
    Return human-readable reasons the computation cannot be finalized/sent, or [] if OK.

    ``uploaded_signature`` is the File object from request.FILES (or None).
    """
    blockers: list[str] = []
    desludging = service_request.service_type in (
        ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
        ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
    )
    if desludging:
        if inspection_is_waived(service_request):
            if not service_request.waived_inspection_crew_ready:
                blockers.append(
                    "Assign crew (driver and helpers as needed) on the request before sending the computation."
                )
        else:
            comp_inf = getattr(service_request, "completion_info", None)
            if not comp_inf or not (comp_inf.driver_name or "").strip():
                blockers.append(
                    "Completion information (including driver) must be submitted before sending the computation."
                )

    cm = computation.cubic_meters
    if cm is None or cm <= 0:
        blockers.append("Cubic meters must be greater than zero.")

    has_sig = bool(uploaded_signature) or stored_filefield_exists(
        getattr(computation, "prepared_by_signature", None)
    )
    if not has_sig:
        blockers.append("Upload a prepared-by signature before sending the computation to the customer.")

    return blockers
