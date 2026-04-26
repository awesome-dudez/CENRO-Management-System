from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any

# OSM / Nominatim often names the true municipality — block neighbors of the
# three LGUs so bbox ambiguity does not admit them.
_FORBIDDEN_SERVICE_MUNICIPALITY_RE = re.compile(
    r"\b(siaton|zamboanguita|dauin|sibulan|dumaguete|bacong)\b",
    re.IGNORECASE,
)

DEFAULT_USER_AGENT = "ECO-TRACK Bayawan CENRO Office (development) - Django reverse geocoder"


def reverse_geocode_osm(lat: float, lon: float, timeout: int = 10) -> dict[str, Any] | None:
    params = {
        "format": "jsonv2",
        "lat": f"{lat:.6f}",
        "lon": f"{lon:.6f}",
        "addressdetails": "1",
    }
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except Exception:
        return None


def _geocode_text_haystack(addr: dict[str, Any], display_name: str | None) -> str:
    parts = [
        display_name or "",
        addr.get("city"),
        addr.get("town"),
        addr.get("municipality"),
        addr.get("county"),
        addr.get("province"),
        addr.get("state"),
        addr.get("region"),
        addr.get("island"),
    ]
    return " ".join([p for p in parts if p]).lower()


def address_names_forbidden_municipality(addr: dict[str, Any], display_name: str | None) -> bool:
    """True when Nominatim text clearly places the pin outside the three served LGUs."""
    hay = _geocode_text_haystack(addr, display_name)
    return bool(_FORBIDDEN_SERVICE_MUNICIPALITY_RE.search(hay))


def address_in_service_area(addr: dict[str, Any], display_name: str | None) -> bool:
    """
    True if reverse-geocoded text refers to an address inside CENRO's accepted
    territory: Bayawan City, Municipality of Basay, or Municipality of Santa Catalina.
    """
    hay = _geocode_text_haystack(addr, display_name)
    if address_names_forbidden_municipality(addr, display_name):
        return False
    if "bayawan" in hay:
        return True
    if "basay" in hay:
        return True
    if "santa catalina" in hay or "sta. catalina" in hay or "sta catalina" in hay:
        return True
    if "municipality of santa catalina" in hay:
        return True
    if "municipality of basay" in hay:
        return True
    return False


def address_in_bayawan(addr: dict[str, Any], display_name: str | None) -> bool:
    """Deprecated alias for :func:`address_in_service_area`."""
    return address_in_service_area(addr, display_name)


def extract_barangay(addr: dict[str, Any]) -> str | None:
    for key in ("suburb", "village", "hamlet", "neighbourhood"):
        val = addr.get(key)
        if val:
            return str(val)
    return None
