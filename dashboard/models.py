from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN
from django.conf import settings
from django.db import models
from django.utils import timezone

User = settings.AUTH_USER_MODEL


class ConfigurableRate(models.Model):
    """Admin-editable billing constants."""

    key = models.CharField(max_length=80, unique=True)
    value = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.CharField(max_length=255, blank=True)

    DEFAULTS = {
        "residential_trucking_within": (Decimal("2160"), "Fixed trucking - Residential within Bayawan"),
        "commercial_trucking_within": (Decimal("2376"), "Fixed trucking - Commercial within Bayawan"),
        "outside_trucking": (Decimal("3000"), "Fixed trucking - Outside Bayawan City"),
        "desludging_per_m3": (Decimal("500"), "Desludging fee per cubic meter"),
        "second_trip_surcharge": (Decimal("360"), "Additional surcharge per m3 on 2nd+ trip"),
        "meals_per_head": (Decimal("200"), "Meals & transportation allowance per person"),
        "inspection_fee": (Decimal("150"), "Inspection fee"),
        "per_km_rate": (Decimal("20"), "Rate per km beyond free distance"),
        "free_km": (Decimal("20"), "Free trucking distance from CENRO office (km)"),
        "wear_tear_pct": (Decimal("20"), "Wear and tear percentage (outside only)"),
        "bawad_free_limit_m3": (Decimal("5"), "BAWAD free service limit (cubic meters)"),
        "bawad_cycle_years": (Decimal("4"), "BAWAD cycle period (years)"),
        "min_cubic_meters": (Decimal("5"), "Minimum billable cubic meters"),
        "bayawan_resident_free_km": (
            Decimal("10"),
            "Free travel distance (km) for Bayawan City residents when service is within Bayawan",
        ),
    }

    class Meta:
        verbose_name = "Configurable Rate"
        verbose_name_plural = "Configurable Rates"

    def __str__(self) -> str:
        return f"{self.key} = {self.value}"

    @classmethod
    def get(cls, key: str, default=None) -> Decimal:
        try:
            return cls.objects.get(key=key).value
        except cls.DoesNotExist:
            if key in cls.DEFAULTS:
                return cls.DEFAULTS[key][0]
            if default is not None:
                return Decimal(str(default))
            return Decimal("0")

    @classmethod
    def seed_defaults(cls):
        for key, (value, description) in cls.DEFAULTS.items():
            cls.objects.get_or_create(key=key, defaults={"value": value, "description": description})


def _billable_travel_km_core(
    *,
    is_within_bayawan: bool,
    is_outside_bayawan: bool,
    is_public: bool,
    bawad_free_eligible: bool,
    consumer_is_bayawan_city_resident: bool,
    distance_whole_km: Decimal,
) -> Decimal:
    """
    Whole-km distance billed at (km × ₱20 × 2).

    First N km from CENRO (bayawan_resident_free_km, default 10) are not charged when:
    - public or BAWAD-cycle-eligible inside Bayawan, or
    - Bayawan City resident (private) inside Bayawan.
    Otherwise all whole kilometers are billable (e.g. outside Bayawan: full route).
    """
    free_km = ConfigurableRate.get("bayawan_resident_free_km", Decimal("10"))
    dist = distance_whole_km or Decimal("0")
    within = is_within_bayawan and not is_outside_bayawan
    if within and (is_public or bawad_free_eligible):
        return max(Decimal("0"), dist - free_km)
    if within and consumer_is_bayawan_city_resident:
        return max(Decimal("0"), dist - free_km)
    return dist


def _billable_travel_km(
    sr: "ServiceRequest",
    *,
    is_outside_bayawan: bool,
    distance_whole_km: Decimal,
) -> Decimal:
    from services.models import ServiceRequest

    is_desludging = sr.service_type in (
        ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
        ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
    )
    try:
        is_public = sr.public_private == ServiceRequest.PublicPrivate.PUBLIC
    except Exception:
        is_public = False
    bawad_ok = sr.connected_to_bawad and sr.bawad_free_eligible
    if not is_desludging:
        is_public = False
        bawad_ok = False
    return _billable_travel_km_core(
        is_within_bayawan=sr.is_within_bayawan,
        is_outside_bayawan=is_outside_bayawan,
        is_public=is_public,
        bawad_free_eligible=bawad_ok,
        consumer_is_bayawan_city_resident=sr.consumer_is_bayawan_city_resident,
        distance_whole_km=distance_whole_km,
    )


def _bawad_customer_desludging_fee(
    *,
    effective_m3: Decimal,
    cubic_meters: Decimal,
    free_m3: Decimal,
    desludging_per_m3: Decimal,
    second_trip_surcharge: Decimal,
) -> Decimal:
    """
    Septage/tipping due for private BAWAD inside Bayawan: bill only volume beyond cycle allowance,
    trip-by-trip (first 5 m³ at base rate, further m³ at base + surcharge).
    """
    max_m3_per_trip = Decimal("5")
    cm = cubic_meters or Decimal("0")
    trips = max(1, math.ceil(float(cm) / float(max_m3_per_trip)))
    remaining_free = min(max(free_m3, Decimal("0")), effective_m3)
    total = Decimal("0")
    for t in range(trips):
        if t == 0:
            vol_this_trip = max_m3_per_trip
        else:
            remaining_vol = effective_m3 - (t * max_m3_per_trip)
            vol_this_trip = min(max_m3_per_trip, max(Decimal("0"), remaining_vol))
        rate = desludging_per_m3 if t == 0 else (desludging_per_m3 + second_trip_surcharge)
        waived_here = min(vol_this_trip, remaining_free)
        billable_vol = vol_this_trip - waived_here
        remaining_free -= waived_here
        total += billable_vol * rate
    return total


class ChargeCategory(models.Model):
    """Categories for service charges"""

    class Category(models.TextChoices):
        RESIDENTIAL = "RESIDENTIAL", "Residential"
        COMMERCIAL = "COMMERCIAL", "Commercial"

    category = models.CharField(max_length=20, choices=Category.choices, unique=True)
    base_rate = models.DecimalField(max_digits=10, decimal_places=2, help_text="Base rate per cubic meter or unit")
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Charge Categories"

    def __str__(self) -> str:
        return f"{self.get_category_display()} - ₱{self.base_rate}"


class ServiceComputation(models.Model):
    """Detailed computation and charges for a service request"""

    class PaymentStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        AWAITING_VERIFICATION = "AWAITING_VERIFICATION", "Awaiting Verification"
        PAID = "PAID", "Paid"
        FREE = "FREE", "Free (BAWAD)"

    service_request = models.OneToOneField(
        "services.ServiceRequest",
        on_delete=models.CASCADE,
        related_name="computation",
    )
    charge_category = models.ForeignKey(
        ChargeCategory, on_delete=models.SET_NULL, null=True, blank=True
    )

    is_outside_bayawan = models.BooleanField(default=False)
    distance_km = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    cubic_meters = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    trips = models.PositiveIntegerField(default=1)
    personnel_count = models.PositiveIntegerField(default=4)

    fixed_trucking = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    desludging_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    distance_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    distance_travel_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    wear_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    meals_transport_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    inspection_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    payment_status = models.CharField(
        max_length=25, choices=PaymentStatus.choices, default=PaymentStatus.PENDING
    )
    prepared_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="computations_prepared"
    )
    prepared_by_signature = models.FileField(
        upload_to="computation_signatures/", null=True, blank=True
    )
    letter_signatory_signature = models.FileField(
        upload_to="computation_signatures/",
        null=True,
        blank=True,
        help_text="Scanned signature for the letter signatory (e.g. City ENRO), shown above the closing block.",
    )
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(null=True, blank=True)
    ready_to_finalize = models.BooleanField(
        default=False,
        help_text="Set when charges are saved from the edit screen; enables Finalize on the letter.",
    )
    receipt_generated = models.BooleanField(default=False)
    receipt_date = models.DateTimeField(null=True, blank=True)

    waive_wear_charge = models.BooleanField(
        default=False,
        help_text="Admin: waive wear & tear (20% of fixed trucking + distance travel) for this computation.",
    )
    waive_meals_transport_charge = models.BooleanField(
        default=False,
        help_text="Admin: waive meals & transportation charge for this computation.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Computation for {self.service_request} - ₱{self.total_charge}"

    def get_desludging_breakdown(self):
        """Return a list of {label, amount} for detailed desludging fee breakdown.
        Trip 1: fixed 5 m³ × base rate.
        Remaining volume (any additional trips) is grouped as "Excess trip(s)" × (base + ₱360 surcharge).
        """
        R = ConfigurableRate.get
        desludging_per_m3 = R("desludging_per_m3")
        second_trip_surcharge = R("second_trip_surcharge")
        min_m3 = R("min_cubic_meters")
        max_m3_per_trip = Decimal("5")
        raw_m3 = self.cubic_meters if self.cubic_meters is not None else Decimal("0")
        effective_m3 = max(raw_m3, min_m3)
        rate_extra = desludging_per_m3 + second_trip_surcharge
        lines = []
        # Trip 1 (always 5 m³ billed at base rate)
        first_trip_volume = max_m3_per_trip
        first_trip_amount = first_trip_volume * desludging_per_m3
        lines.append({
            "label": f"Trip 1: 5 m³ × ₱{desludging_per_m3}/m³ (min 5 m³, max 5 m³)",
            "amount": first_trip_amount,
        })

        # Excess trips: group all remaining volume beyond the first 5 m³
        remaining_volume = effective_m3 - max_m3_per_trip
        if remaining_volume > 0:
            excess_amount = remaining_volume * rate_extra
            lines.append({
                "label": f"Excess trip(s): {remaining_volume} m³ × ₱{rate_extra}/m³ (₱{desludging_per_m3} + ₱{second_trip_surcharge} surcharge)",
                "amount": excess_amount,
            })
        return lines

    def billable_subtotal(self) -> Decimal:
        """Sum of line-item charges (same as total_charge before a FREE waiver zeroes it)."""
        return (
            (self.fixed_trucking or Decimal("0"))
            + (self.desludging_fee or Decimal("0"))
            + (self.distance_travel_fee or Decimal("0"))
            + (self.wear_charge or Decimal("0"))
            + (self.meals_transport_charge or Decimal("0"))
        )

    @property
    def cenro_free_travel_km(self) -> Decimal:
        """First N whole km from CENRO office not charged when inside-Bayawan rules apply."""
        return ConfigurableRate.get("bayawan_resident_free_km", Decimal("10"))

    @property
    def billable_travel_km(self) -> Decimal:
        """Whole km billed for distance travel fee (after CENRO free km when applicable)."""
        sr = self.service_request
        dist = (self.distance_km or Decimal("0")).quantize(Decimal("1"), rounding=ROUND_DOWN)
        return _billable_travel_km(sr, is_outside_bayawan=self.is_outside_bayawan, distance_whole_km=dist)

    @property
    def qualifies_inside_public_bawad_program(self) -> bool:
        """Declogging only: public property or BAWAD cycle-eligible, inside Bayawan (not forced outside)."""
        sr = self.service_request
        from services.models import ServiceRequest

        if sr.service_type not in (
            ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
            ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
        ):
            return False
        if not sr.is_within_bayawan or self.is_outside_bayawan:
            return False
        try:
            is_public = sr.public_private == ServiceRequest.PublicPrivate.PUBLIC
        except Exception:
            is_public = False
        return bool(is_public or (sr.connected_to_bawad and sr.bawad_free_eligible))

    @property
    def uses_inside_public_bawad_partial_waiver(self) -> bool:
        """Inside public property: site farther than free km — trucking & septage waived, travel/wear/meals still apply."""
        if not self.qualifies_inside_public_bawad_program:
            return False
        sr = self.service_request
        from services.models import ServiceRequest

        try:
            if sr.public_private != ServiceRequest.PublicPrivate.PUBLIC:
                return False
        except Exception:
            return False
        dist = (self.distance_km or Decimal("0")).quantize(Decimal("1"), rounding=ROUND_DOWN)
        free_km = ConfigurableRate.get("bayawan_resident_free_km", Decimal("10"))
        return dist > free_km

    @property
    def uses_private_bawad_volume_discount(self) -> bool:
        return getattr(self, "_uses_private_bawad_volume_discount", False)

    @property
    def private_bawad_free_m3_this_job(self) -> Decimal:
        return getattr(self, "_private_bawad_free_m3_this_job", Decimal("0"))

    @property
    def declog_gross_before_inside_rules(self) -> Decimal:
        return getattr(
            self,
            "_declog_gross_before_inside_rules",
            self.billable_subtotal,
        )

    @property
    def private_bawad_volume_discount_amount(self) -> Decimal:
        if not self.uses_private_bawad_volume_discount:
            return Decimal("0")
        return self.declog_gross_before_inside_rules - self.total_charge

    @property
    def letter_wear_display_amount(self) -> Decimal:
        if self.uses_private_bawad_volume_discount:
            v = getattr(self, "_letter_gross_wear_charge", None)
            if v is not None:
                return v
        return self.wear_charge or Decimal("0")

    @property
    def waived_inside_base_service_amount(self) -> Decimal:
        if not self.uses_inside_public_bawad_partial_waiver:
            return Decimal("0")
        return (self.fixed_trucking or Decimal("0")) + (self.desludging_fee or Decimal("0"))

    def calculate_charges(self):
        """Calculate all charges based on the correct business rules."""
        from services.models import ServiceRequest

        sr = self.service_request
        R = ConfigurableRate.get

        desludging_per_m3 = R("desludging_per_m3")
        second_trip_surcharge = R("second_trip_surcharge")
        meals_per_head = R("meals_per_head")
        inspection_fee = R("inspection_fee")
        per_km_rate = R("per_km_rate")
        free_km = R("free_km")
        wear_pct = R("wear_tear_pct") / Decimal("100")
        min_m3 = R("min_cubic_meters")

        raw_m3 = self.cubic_meters if self.cubic_meters is not None else Decimal("0")
        effective_m3 = max(raw_m3, min_m3)

        # 1 trip = max 5 m³; more than 5 m³ auto-adjusts to additional trips
        max_m3_per_trip = Decimal("5")
        self.trips = max(1, math.ceil(float(raw_m3) / float(max_m3_per_trip)))

        # Fixed trucking based on service type and location
        if self.is_outside_bayawan:
            self.fixed_trucking = R("outside_trucking")
        elif sr.service_type == ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING:
            self.fixed_trucking = R("commercial_trucking_within")
        else:
            self.fixed_trucking = R("residential_trucking_within")

        # Desludging fee: Trip 1 = always 5 m³ × 500/m³; Trip 2+ = remaining m³ × 860/m³
        self.desludging_fee = Decimal("0")
        for t in range(self.trips):
            if t == 0:
                vol_this_trip = max_m3_per_trip  # First trip always 5 m³ (min and max)
            else:
                remaining = effective_m3 - (t * max_m3_per_trip)
                vol_this_trip = min(max_m3_per_trip, max(Decimal("0"), remaining))
            rate = desludging_per_m3 if t == 0 else (desludging_per_m3 + second_trip_surcharge)
            self.desludging_fee += vol_this_trip * rate

        # Distance Travel Fee: billable whole km × ₱20 × 2 (see _billable_travel_km).
        raw_dist = self.distance_km or Decimal("0")
        dist = raw_dist.quantize(Decimal("1"), rounding=ROUND_DOWN)
        self.distance_km = dist
        billable_dist = _billable_travel_km(
            sr, is_outside_bayawan=self.is_outside_bayawan, distance_whole_km=dist
        )
        self.distance_travel_fee = billable_dist * Decimal("20") * 2
        self.distance_charge = Decimal("0")  # no longer used in breakdown

        # Meals and transportation: ₱200 per personnel (driver + helpers)
        self.meals_transport_charge = self.personnel_count * meals_per_head

        # Inspection fee removed from computation (paid separately at the beginning)
        self.inspection_charge = Decimal("0")

        # Wear and tear: 20% of (Fixed Trucking + Distance Travel Fee)
        base_for_wear = self.fixed_trucking + self.distance_travel_fee
        self.wear_charge = base_for_wear * wear_pct

        if self.waive_wear_charge:
            self.wear_charge = Decimal("0")
        if self.waive_meals_transport_charge:
            self.meals_transport_charge = Decimal("0")

        # Total (no inspection_charge) before inside public/BAWAD rules
        full_total = (
            self.fixed_trucking
            + self.desludging_fee
            + self.distance_travel_fee
            + self.wear_charge
            + self.meals_transport_charge
        )
        self.total_charge = full_total
        self._declog_gross_before_inside_rules = full_total
        self._uses_private_bawad_volume_discount = False
        self._private_bawad_free_m3_this_job = Decimal("0")
        self._letter_gross_wear_charge = None

        free_km = ConfigurableRate.get("bayawan_resident_free_km", Decimal("10"))
        qualifies_inside = self.qualifies_inside_public_bawad_program

        if qualifies_inside:
            try:
                is_public = sr.public_private == ServiceRequest.PublicPrivate.PUBLIC
            except Exception:
                is_public = False

            if is_public:
                if dist <= free_km:
                    # Fully free: total ₱0 (first N km from CENRO; no billable travel).
                    self.total_charge = Decimal("0")
                    if self.payment_status not in (
                        self.PaymentStatus.PAID,
                        self.PaymentStatus.AWAITING_VERIFICATION,
                    ):
                        self.payment_status = self.PaymentStatus.FREE
                else:
                    # Waive fixed trucking + tipping/septage only; charge distance, wear, meals.
                    self.total_charge = (
                        self.distance_travel_fee
                        + self.wear_charge
                        + self.meals_transport_charge
                    )
                    if self.payment_status not in (
                        self.PaymentStatus.PAID,
                        self.PaymentStatus.AWAITING_VERIFICATION,
                    ):
                        self.payment_status = self.PaymentStatus.PENDING
            else:
                # Private BAWAD inside Bayawan: fixed trucking waived; cycle allowance (m³) free on
                # septage; within free km — no distance / wear / meals; beyond free km — pay excess
                # distance, wear on distance only, and meals.
                prior = sr.bawad_prior_used_m3_in_cycle
                limit = R("bawad_free_limit_m3", 5)
                allowance = max(Decimal("0"), limit - prior)
                free_m3 = min(effective_m3, allowance)
                self._private_bawad_free_m3_this_job = free_m3

                ch_ft = Decimal("0")
                ch_des = _bawad_customer_desludging_fee(
                    effective_m3=effective_m3,
                    cubic_meters=raw_m3,
                    free_m3=free_m3,
                    desludging_per_m3=desludging_per_m3,
                    second_trip_surcharge=second_trip_surcharge,
                )

                if dist <= free_km:
                    if ch_des <= 0:
                        self.total_charge = Decimal("0")
                        self._uses_private_bawad_volume_discount = False
                        self._letter_gross_wear_charge = None
                        if self.payment_status not in (
                            self.PaymentStatus.PAID,
                            self.PaymentStatus.AWAITING_VERIFICATION,
                        ):
                            self.payment_status = self.PaymentStatus.FREE
                    else:
                        gross_wear = self.wear_charge
                        self.wear_charge = Decimal("0")
                        if self.waive_wear_charge:
                            self.wear_charge = Decimal("0")
                        self._letter_gross_wear_charge = gross_wear
                        self.total_charge = ch_des
                        self._uses_private_bawad_volume_discount = True
                        if self.payment_status not in (
                            self.PaymentStatus.PAID,
                            self.PaymentStatus.AWAITING_VERIFICATION,
                        ):
                            self.payment_status = self.PaymentStatus.PENDING
                else:
                    gross_wear = self.wear_charge
                    dist_fee = self.distance_travel_fee
                    wear_base = ch_ft + dist_fee
                    self.wear_charge = wear_base * wear_pct
                    if self.waive_wear_charge:
                        self.wear_charge = Decimal("0")
                    meals_ch = (
                        Decimal("0")
                        if self.waive_meals_transport_charge
                        else self.meals_transport_charge
                    )
                    self._letter_gross_wear_charge = gross_wear
                    self.total_charge = ch_des + dist_fee + self.wear_charge + meals_ch
                    self._uses_private_bawad_volume_discount = (
                        self._declog_gross_before_inside_rules > self.total_charge
                    )
                    if self.payment_status not in (
                        self.PaymentStatus.PAID,
                        self.PaymentStatus.AWAITING_VERIFICATION,
                    ):
                        self.payment_status = self.PaymentStatus.PENDING
        elif not qualifies_inside and self.payment_status == self.PaymentStatus.FREE and self.total_charge > 0:
            self.payment_status = self.PaymentStatus.PENDING

    def recompute_letter_breakdown(self) -> None:
        """
        Re-run charge rules in memory so letter/PDF templates see ephemeral fields
        (_declog_gross_before_inside_rules, _uses_private_bawad_volume_discount, etc.)
        that are not stored in the database. Does not write to the DB.
        """
        self.calculate_charges()

    def save(self, *args, **kwargs):
        self.calculate_charges()
        super().save(*args, **kwargs)


def compute_quick_desludging_estimate(
    *,
    category: str,
    location: str,
    cubic_meters: Decimal,
    distance_km: Decimal,
    personnel_count: int,
    meals_transport_override: Decimal | None,
    connected_to_bawad: bool,
    public_private: str,
    bawad_prior_used_m3: Decimal,
    bayawan_city_resident: bool = False,
    waive_wear_charge: bool = False,
    waive_meals_transport_charge: bool = False,
) -> dict:
    """
    Same charge math as ServiceComputation.calculate_charges, for admin demo / quick calculator.
    Pass explicit BAWAD prior usage (m³ in current cycle) instead of querying the database.
    """
    from services.models import ServiceRequest

    R = ConfigurableRate.get
    desludging_per_m3 = R("desludging_per_m3")
    second_trip_surcharge = R("second_trip_surcharge")
    meals_per_head = R("meals_per_head")
    wear_pct = R("wear_tear_pct") / Decimal("100")
    min_m3 = R("min_cubic_meters")
    bawad_limit = R("bawad_free_limit_m3", 5)

    is_outside = location == "outside"
    is_within_bayawan = not is_outside
    st = (
        ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING
        if category == "COMMERCIAL"
        else ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING
    )

    effective_m3 = max(cubic_meters, min_m3)
    max_m3_per_trip = Decimal("5")
    trips = max(1, math.ceil(float(cubic_meters) / float(max_m3_per_trip)))

    if is_outside:
        fixed_trucking = R("outside_trucking")
    elif st == ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING:
        fixed_trucking = R("commercial_trucking_within")
    else:
        fixed_trucking = R("residential_trucking_within")

    desludging_fee = Decimal("0")
    for t in range(trips):
        if t == 0:
            vol_this_trip = max_m3_per_trip
        else:
            remaining = effective_m3 - (t * max_m3_per_trip)
            vol_this_trip = min(max_m3_per_trip, max(Decimal("0"), remaining))
        rate = desludging_per_m3 if t == 0 else (desludging_per_m3 + second_trip_surcharge)
        desludging_fee += vol_this_trip * rate

    raw_dist = distance_km or Decimal("0")
    dist = raw_dist.quantize(Decimal("1"), rounding=ROUND_DOWN)
    is_public = public_private == ServiceRequest.PublicPrivate.PUBLIC
    bawad_eligible = connected_to_bawad and bawad_prior_used_m3 < bawad_limit
    billable_dist = _billable_travel_km_core(
        is_within_bayawan=is_within_bayawan,
        is_outside_bayawan=is_outside,
        is_public=is_public,
        bawad_free_eligible=bawad_eligible,
        consumer_is_bayawan_city_resident=bayawan_city_resident,
        distance_whole_km=dist,
    )
    distance_travel_fee = billable_dist * Decimal("20") * 2

    if meals_transport_override is not None and meals_transport_override > 0:
        meals_transport_charge = meals_transport_override
    else:
        meals_transport_charge = Decimal(personnel_count) * meals_per_head

    base_for_wear = fixed_trucking + distance_travel_fee
    wear_charge = base_for_wear * wear_pct
    if waive_wear_charge:
        wear_charge = Decimal("0")
    if waive_meals_transport_charge:
        meals_transport_charge = Decimal("0")

    subtotal = (
        fixed_trucking
        + desludging_fee
        + distance_travel_fee
        + wear_charge
        + meals_transport_charge
    )

    # Desludging line items (same labels as ServiceComputation.get_desludging_breakdown)
    rate_extra = desludging_per_m3 + second_trip_surcharge
    desludging_breakdown = [
        {
            "label": f"Trip 1 · 5 m³ @ ₱{desludging_per_m3}",
            "amount": max_m3_per_trip * desludging_per_m3,
        }
    ]
    remaining_volume = effective_m3 - max_m3_per_trip
    if remaining_volume > 0:
        desludging_breakdown.append({
            "label": f"Excess · {remaining_volume} m³ @ ₱{rate_extra}",
            "amount": remaining_volume * rate_extra,
        })

    free_km = R("bayawan_resident_free_km", Decimal("10"))
    qualifies_inside = is_within_bayawan and not is_outside and (is_public or bawad_eligible)

    free_reason = None
    partial_waiver = False
    waived_base = Decimal("0")
    uses_bawad_volume_discount = False
    bawad_volume_discount_amount = Decimal("0")
    bawad_free_m3_applied = Decimal("0")
    total_charge = subtotal
    if qualifies_inside:
        if is_public:
            if dist <= free_km:
                total_charge = Decimal("0")
                free_reason = "public_bawad_inside_under_10km"
            else:
                total_charge = distance_travel_fee + wear_charge + meals_transport_charge
                partial_waiver = True
                waived_base = fixed_trucking + desludging_fee
        else:
            allowance = max(Decimal("0"), bawad_limit - bawad_prior_used_m3)
            free_m3 = min(effective_m3, allowance)
            bawad_free_m3_applied = free_m3
            ch_ft = Decimal("0")
            ch_des = _bawad_customer_desludging_fee(
                effective_m3=effective_m3,
                cubic_meters=cubic_meters or Decimal("0"),
                free_m3=free_m3,
                desludging_per_m3=desludging_per_m3,
                second_trip_surcharge=second_trip_surcharge,
            )
            if dist <= free_km:
                if ch_des <= 0:
                    total_charge = Decimal("0")
                    free_reason = "bawad_private_full_allowance_under_10km"
                else:
                    wear_charge = Decimal("0")
                    if waive_wear_charge:
                        wear_charge = Decimal("0")
                    total_charge = ch_des
                    uses_bawad_volume_discount = True
                    bawad_volume_discount_amount = subtotal - total_charge
            else:
                wear_charge = (ch_ft + distance_travel_fee) * wear_pct
                if waive_wear_charge:
                    wear_charge = Decimal("0")
                if waive_meals_transport_charge:
                    meals_transport_charge = Decimal("0")
                total_charge = (
                    ch_des + distance_travel_fee + wear_charge + meals_transport_charge
                )
                uses_bawad_volume_discount = subtotal > total_charge
                if uses_bawad_volume_discount:
                    bawad_volume_discount_amount = subtotal - total_charge

    return {
        "trips": trips,
        "effective_m3": effective_m3,
        "fixed_trucking": fixed_trucking,
        "desludging_fee": desludging_fee,
        "desludging_breakdown": desludging_breakdown,
        "distance_km": dist,
        "distance_travel_fee": distance_travel_fee,
        "wear_charge": wear_charge,
        "meals_transport_charge": meals_transport_charge,
        "personnel_count": personnel_count,
        "subtotal_before_waiver": subtotal,
        "total_charge": total_charge,
        "free_reason": free_reason,
        "partial_waiver": partial_waiver,
        "waived_base_amount": waived_base,
        "uses_bawad_volume_discount": uses_bawad_volume_discount,
        "bawad_volume_discount_amount": bawad_volume_discount_amount,
        "bawad_free_m3_applied": bawad_free_m3_applied,
        "is_public": is_public,
        "is_within_bayawan": is_within_bayawan,
        "connected_to_bawad": connected_to_bawad,
        "bawad_eligible": bawad_eligible,
        "bawad_prior_used_m3": bawad_prior_used_m3,
        "bawad_limit_m3": bawad_limit,
        "public_private": public_private,
        "category": category,
        "location": location,
        "cubic_meters": cubic_meters,
    }


class DecloggingApplication(models.Model):
    """Declogging application with signatures"""

    service_request = models.OneToOneField(
        "services.ServiceRequest",
        on_delete=models.CASCADE,
        related_name="declogging_app",
    )
    applicant_name = models.CharField(max_length=255)
    applicant_signature = models.FileField(upload_to="signatures/", null=True, blank=True)
    applicant_sign_date = models.DateField(null=True, blank=True)

    cenro_representative = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="declogging_apps_signed",
    )
    cenro_signature = models.FileField(upload_to="signatures/", null=True, blank=True)
    cenro_sign_date = models.DateField(null=True, blank=True)

    application_date = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_signed = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"Declogging App - {self.applicant_name}"


class MembershipRecord(models.Model):
    """Track membership with service history and balance"""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="membership_record")
    total_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_free = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    remaining_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    joined_date = models.DateField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Membership - {self.user.get_full_name()}"
