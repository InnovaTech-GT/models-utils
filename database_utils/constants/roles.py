# database_utils/constants/roles.py
"""
Role constants for role-based access control (RBAC).

These are the built-in (global, ``company_id IS NULL``) roles. Tenants
can add their own custom roles on top, but may not reuse these names.
"""


class Roles:
    """
    Built-in role constants for RBAC.

    Attributes:
        ADMIN: Full access to everything (wildcard)
        VIEWER: Read access to everything on the web app
        COLLECTOR: Collectors' mobile app (cobros) only
        TECHNICIAN: Technicians' mobile app (tecnicos) only
        CAPTIVE_PORTAL: Service user of a tenant's captive portal (cp1,
            captive_portal.access only; never granted to humans)
        ALL: Set containing all built-in roles
    """

    ADMIN = "ADMIN"
    VIEWER = "VIEWER"
    COLLECTOR = "COLLECTOR"
    TECHNICIAN = "TECHNICIAN"

    CAPTIVE_PORTAL = "CAPTIVE_PORTAL"

    ALL = {ADMIN, VIEWER, COLLECTOR, TECHNICIAN, CAPTIVE_PORTAL}
