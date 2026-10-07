"""Platform settings kept in the database (edited by super admins at /admin/settings)."""
from django.db.utils import DatabaseError

from .models import PlatformSetting

DEFAULTS = {
    "site": {"name": "Competition Manager", "support_email": "", "allow_signups": True, "require_org_approval": False,
             "max_orgs_per_user": 25, "notice": "", "base_url": ""},
    "email": {"provider": "", "host": "", "port": 587, "user": "", "password": "", "from": "", "api_key": "", "secure": ""},
    # PayNova: the platform's payment account. Organizations invoice through it; the platform can keep a fee.
    "payments": {"enabled": False, "secret_key": "", "currency": "GHS", "wallet_id": "", "fee_enabled": False, "fee_percent": "0",
                 "fee_fixed": "0"},
    # Text messages: the provider and its keys, and what organizations pay per credit (1 credit = 1 SMS to 1 person).
    "sms": {"enabled": False, "provider": "", "api_key": "", "sender": "", "country_code": "233",
            "credit_price": "0.05", "currency": "GHS", "min_credits": 100},
}
SECRETS = {"email": {"password", "api_key"}, "payments": {"secret_key"}, "sms": {"api_key"}}
TYPES = {"allow_signups": bool, "require_org_approval": bool, "max_orgs_per_user": int, "port": int, "enabled": bool, "fee_enabled": bool,
         "min_credits": int}


def get(section):
    try:
        row = PlatformSetting.objects.filter(key=section).first()
    except DatabaseError:            # before the first migration
        row = None
    return {**DEFAULTS[section], **((row.value if row else {}) or {})}


def masked(section):
    """For the browser: secrets replaced by "is it set?" flags."""
    data = get(section)
    for k in SECRETS.get(section, ()):
        data[k + "_set"] = bool(data.pop(k))
    return data


def update(section, changes):
    """Apply validated changes. An empty secret keeps the stored one; send {"<secret>_clear": true} to remove it."""
    data = get(section)
    old = masked(section)
    for k, v in changes.items():
        if k.endswith("_clear") and k[:-6] in SECRETS.get(section, ()):
            if v is True:
                data[k[:-6]] = ""
            continue
        if k not in DEFAULTS[section]:
            raise ValueError(f"Unknown setting: {k}")
        want = TYPES.get(k, str)
        if want is bool:
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be on or off.")
        elif want is int:
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 65535:
                raise ValueError(f"{k} must be a whole number.")
        else:
            if not isinstance(v, str) or len(v) > 500:
                raise ValueError(f"{k} must be text (up to 500 characters).")
            v = v.strip()
            if k in SECRETS.get(section, ()) and not v:
                continue                 # blank secret field = keep what's stored
        data[k] = v
    PlatformSetting.objects.update_or_create(key=section, defaults={"value": data})
    return old, masked(section)


def site():
    return get("site")
