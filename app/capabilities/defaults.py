"""The v1 capability registry (framework §4.1.3).

Note what is *absent*: there is no ``payment.*`` entry. The product promise
"payment always happens on the user's side" is expressed as absence, so no node
can call it even by mistake (DC-5).
"""

from __future__ import annotations

from app.capabilities.contract import CapabilitySpec, DegradationLevel, RiskLevel
from app.capabilities.registry import CapabilityBinding, CapabilityRegistry


def build_default_registry() -> CapabilityRegistry:
    reg = CapabilityRegistry()

    # --- time anchor -------------------------------------------------------
    reg.register(
        CapabilitySpec(
            capability_id="clock.today",
            description="authoritative current date; resolve relative dates against it",
            risk=RiskLevel.L0,
            timeout_seconds=5.0,
            cache_ttl_seconds=3600,
            degradation_ladder=[DegradationLevel.D2],
        ),
        CapabilityBinding(adapter_id="variflight", remote_name="getTodayDate", priority=10),
        CapabilityBinding(adapter_id="rail12306", remote_name="get-current-date", priority=20),
    )

    # --- places ------------------------------------------------------------
    reg.register(
        CapabilitySpec(
            capability_id="place.resolve",
            description="address or place name -> normalised coordinate + admin area",
            risk=RiskLevel.L0,
            timeout_seconds=5.0,
            cache_ttl_seconds=7 * 24 * 3600,
            quota_pool="amap_lbs",
            units_per_call=1,
            degradation_ladder=[DegradationLevel.D1, DegradationLevel.D2, DegradationLevel.D3],
        ),
        CapabilityBinding(adapter_id="amap", remote_name="amap_geocode", priority=10),
        # Keyless HTTP beats keyless MCP: same availability, no package spawn.
        CapabilityBinding(adapter_id="open_meteo_geocoding", remote_name="search", priority=20),
        CapabilityBinding(adapter_id="osm", remote_name="openstreetmap_search_places", priority=40),
    )
    reg.register(
        CapabilitySpec(
            capability_id="place.disambiguate",
            description="disambiguate a POI (e.g. which section of the Great Wall)",
            risk=RiskLevel.L0,
            timeout_seconds=6.0,
            cache_ttl_seconds=24 * 3600,
            # amap's *search* pool is 5k/month; detail is the cheap path.
            quota_pool="amap_search",
            units_per_call=1,
            degradation_ladder=[DegradationLevel.D1, DegradationLevel.D2],
        ),
        CapabilityBinding(adapter_id="amap", remote_name="amap_poi_detail", priority=10),
        CapabilityBinding(adapter_id="amap", remote_name="amap_poi_search", priority=20),
        CapabilityBinding(adapter_id="open_meteo_geocoding", remote_name="search", priority=30),
        CapabilityBinding(adapter_id="osm", remote_name="openstreetmap_search_places", priority=40),
    )

    # --- routing -----------------------------------------------------------
    reg.register(
        CapabilitySpec(
            capability_id="route.plan",
            description="point-to-point itinerary for a given mode",
            risk=RiskLevel.L0,
            timeout_seconds=8.0,
            cache_ttl_seconds=900,
            quota_pool="amap_lbs",
            units_per_call=1,
        ),
        CapabilityBinding(adapter_id="amap", remote_name="amap_direction_transit", priority=10),
        CapabilityBinding(adapter_id="valhalla", remote_name="route", priority=20),
        CapabilityBinding(adapter_id="osrm", remote_name="route", priority=30),
        CapabilityBinding(adapter_id="osm", remote_name="openstreetmap_search_places", priority=40),
    )
    reg.register(
        CapabilitySpec(
            capability_id="route.matrix",
            description="distance matrix between several points",
            risk=RiskLevel.L0,
            timeout_seconds=8.0,
            cache_ttl_seconds=900,
            quota_pool="amap_lbs",
            units_per_call=1,
        ),
        CapabilityBinding(adapter_id="amap", remote_name="amap_distance", priority=10),
    )

    # --- weather -----------------------------------------------------------
    reg.register(
        CapabilitySpec(
            capability_id="weather.forecast",
            description="forecast + derived risk factors",
            risk=RiskLevel.L0,
            timeout_seconds=5.0,
            cache_ttl_seconds=1800,
        ),
        CapabilityBinding(adapter_id="open_meteo", remote_name="forecast", priority=10),
        CapabilityBinding(adapter_id="amap", remote_name="amap_weather", priority=20),
    )
    reg.register(
        CapabilitySpec(
            capability_id="air.quality",
            description="air quality index",
            risk=RiskLevel.L0,
            timeout_seconds=5.0,
            cache_ttl_seconds=3600,
        ),
        CapabilityBinding(adapter_id="open_meteo", remote_name="air_quality", priority=10),
    )

    # --- intercity ---------------------------------------------------------
    reg.register(
        CapabilitySpec(
            capability_id="intercity.rail.search",
            description="rail search including pass-through and transfer options",
            risk=RiskLevel.L0,
            timeout_seconds=15.0,
            cache_ttl_seconds=60,
        ),
        CapabilityBinding(adapter_id="rail12306", remote_name="get-tickets", priority=10),
    )
    reg.register(
        CapabilitySpec(
            capability_id="intercity.rail.transfer",
            description="rail transfer / interline search (the multi-leg case)",
            risk=RiskLevel.L0,
            timeout_seconds=25.0,
            cache_ttl_seconds=120,
        ),
        CapabilityBinding(adapter_id="rail12306", remote_name="get-interline-tickets", priority=10),
    )
    reg.register(
        CapabilitySpec(
            capability_id="intercity.rail.availability",
            description="live seat availability (needs login cookie)",
            # L0 despite the browser channel: read-only means no consent needed.
            risk=RiskLevel.L0,
            timeout_seconds=20.0,
            cache_ttl_seconds=60,
        ),
        CapabilityBinding(adapter_id="browser", remote_name="read_rail_availability", priority=10),
        CapabilityBinding(adapter_id="rail12306", remote_name="get-tickets", priority=20),
    )
    reg.register(
        CapabilitySpec(
            capability_id="intercity.flight.search",
            description="flight search",
            risk=RiskLevel.L0,
            timeout_seconds=30.0,  # §11: flight list queries can take ~30s
            cache_ttl_seconds=300,
            quota_pool="variflight_points",
            units_per_call=50,
        ),
        CapabilityBinding(adapter_id="variflight", remote_name="searchFlightsByDepArr", priority=10),
    )
    reg.register(
        CapabilitySpec(
            capability_id="intercity.multimodal.search",
            description="air-rail combined transfer search",
            risk=RiskLevel.L0,
            timeout_seconds=30.0,
            cache_ttl_seconds=300,
            quota_pool="variflight_points",
            units_per_call=25,
        ),
        CapabilityBinding(adapter_id="variflight", remote_name="getFlightAndTrainTransferInfo", priority=10),
    )
    reg.register(
        CapabilitySpec(
            capability_id="price.quote",
            description="fare quote",
            risk=RiskLevel.L0,
            timeout_seconds=20.0,
            cache_ttl_seconds=300,
            quota_pool="variflight_points",
            units_per_call=50,
        ),
        CapabilityBinding(adapter_id="variflight", remote_name="getFlightPriceByCities", priority=10),
    )

    # --- discovery ---------------------------------------------------------
    reg.register(
        CapabilitySpec(
            capability_id="poi.discover",
            description="attraction / hotel recommendations",
            risk=RiskLevel.L0,
            timeout_seconds=20.0,
            cache_ttl_seconds=24 * 3600,
        ),
        CapabilityBinding(adapter_id="meituan", remote_name="query", priority=10),
    )
    reg.register(
        CapabilitySpec(
            capability_id="attraction.reservation.status",
            description="whether an attraction needs booking, and remaining quota",
            risk=RiskLevel.L0,
            timeout_seconds=20.0,
            cache_ttl_seconds=6 * 3600,
        ),
        CapabilityBinding(adapter_id="browser", remote_name="read_reservation_status", priority=10),
    )

    # --- local, side-effect-free actions (DC-4: L0, do not interrupt) -------
    reg.register(
        CapabilitySpec(
            capability_id="booking.deeplink.build",
            description="assemble a booking deep-link locally (M1)",
            risk=RiskLevel.L0,
            timeout_seconds=2.0,
            cache_ttl_seconds=None,
        ),
        CapabilityBinding(adapter_id="local", remote_name="build_deeplink", priority=10),
    )
    reg.register(
        CapabilitySpec(
            capability_id="booking.checklist.export",
            description="export a structured booking checklist locally (M2)",
            risk=RiskLevel.L0,
            timeout_seconds=2.0,
        ),
        CapabilityBinding(adapter_id="local", remote_name="export_checklist", priority=10),
    )
    reg.register(
        CapabilitySpec(
            capability_id="reminder.create",
            description="create trip reminders locally",
            risk=RiskLevel.L0,
            timeout_seconds=2.0,
        ),
        CapabilityBinding(adapter_id="local", remote_name="create_reminder", priority=10),
    )
    reg.register(
        CapabilitySpec(
            capability_id="plan.export",
            description="render the frozen plan as a file (markdown / text / html)",
            risk=RiskLevel.L0,
            timeout_seconds=2.0,
        ),
        CapabilityBinding(adapter_id="local", remote_name="export_plan", priority=10),
    )
    reg.register(
        CapabilitySpec(
            capability_id="plan.share",
            description="produce a self-contained read-only copy of the itinerary",
            risk=RiskLevel.L0,
            timeout_seconds=2.0,
        ),
        CapabilityBinding(adapter_id="local", remote_name="build_share", priority=10),
    )

    # --- browser, step-graded (DC-6) ---------------------------------------
    for cap, read_only in (
        ("browser.navigate", True),
        ("browser.read", True),
        ("browser.screenshot", True),
        ("browser.click", False),
        ("browser.type", False),
        ("browser.submit", False),
    ):
        reg.register(
            CapabilitySpec(
                capability_id=cap,
                description=f"browser {cap.split('.')[-1]} step",
                risk=RiskLevel.L0 if read_only else RiskLevel.L1,
                timeout_seconds=20.0,
            ),
            CapabilityBinding(adapter_id="browser", remote_name=cap.split(".")[-1], priority=10),
        )

    # --- irreversible (never retried, always double-confirmed) -------------
    reg.register(
        CapabilitySpec(
            capability_id="booking.reserve.submit",
            description="submit an attraction reservation",
            risk=RiskLevel.L2,
            timeout_seconds=30.0,
            idempotent=True,
        ),
        CapabilityBinding(adapter_id="browser", remote_name="submit_reservation", priority=10),
    )
    reg.register(
        CapabilitySpec(
            capability_id="booking.order.submit",
            description="submit an order",
            risk=RiskLevel.L2,
            timeout_seconds=30.0,
            idempotent=True,
        ),
        CapabilityBinding(adapter_id="browser", remote_name="submit_order", priority=10),
    )

    # --- deliberately not registered ---------------------------------------
    # payment.*  -> never auto-pay (DC-5)
    # note: attempts to call them raise CapabilityNotFound from the registry.
    return reg
