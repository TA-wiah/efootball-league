"""Who can do what inside an organization. The single source of truth: the API checks these on every request,
and the dashboard only uses them to decide which buttons to show.

Each organization's owner can switch most permissions on or off per role (Organization.permissions); the defaults
below apply otherwise. Owner-only powers (roles, transfer, delete) can never be given away.
"""

OWNER, ADMIN, ORGANIZER = "owner", "admin", "organizer"
TEAM_MANAGER, COACH, SCOREKEEPER = "team_manager", "coach", "scorekeeper"
EDITOR, MODERATOR, PLAYER, VIEWER = "editor", "moderator", "player", "viewer"
ROLES = [OWNER, ADMIN, ORGANIZER, TEAM_MANAGER, COACH, SCOREKEEPER, EDITOR, MODERATOR, PLAYER, VIEWER]
RANK = {OWNER: 10, ADMIN: 9, ORGANIZER: 8, TEAM_MANAGER: 6, COACH: 5, SCOREKEEPER: 5, EDITOR: 5, MODERATOR: 4, PLAYER: 2, VIEWER: 1}
TEAM_ROLES = {TEAM_MANAGER, COACH}          # work only on the teams they're assigned to
STAFF_ROLES = [r for r in ROLES if r not in (PLAYER, VIEWER)]

ROLE_INFO = {
    OWNER: ("Owner", "Full access to the organization, including roles, ownership transfer and deleting it."),
    ADMIN: ("Administrator", "Manages the organization, competitions, teams, members and settings."),
    ORGANIZER: ("Competition Manager", "Manages competitions, fixtures, results and standings, and invites staff."),
    TEAM_MANAGER: ("Team Manager", "Manages the teams and players they're assigned to."),
    COACH: ("Coach", "Updates information for the teams they're assigned to."),
    SCOREKEEPER: ("Scorekeeper", "Enters match results, scores, goals and cards."),
    EDITOR: ("Editor", "Updates scores, team information and competition content."),
    MODERATOR: ("Moderator", "Looks after published content and announcements."),
    PLAYER: ("Player", "Plays in the organization's teams; read-only access."),
    VIEWER: ("Viewer", "Read-only access to the organization's dashboard."),
}

_MGMT = {OWNER, ADMIN, ORGANIZER}
PERMISSIONS = [
    # (key, what it lets you do, default roles, can the owner change it?)
    ("org.view", "See the organization dashboard", set(ROLES), False),
    ("members.view", "See the member list", set(ROLES), False),
    ("members.invite", "Invite people and manage invitations", _MGMT, True),
    ("members.manage", "Change roles, team access and remove members", {OWNER, ADMIN}, True),
    ("competitions.manage", "Create and manage competitions", _MGMT, True),
    ("fixtures.manage", "Create fixtures, draws and competition stages", _MGMT, True),
    ("results.enter", "Enter and update match results", _MGMT | {SCOREKEEPER, EDITOR}, True),
    ("standings.manage", "Manage league tables (points adjustments)", _MGMT, True),
    ("teams.manage", "Manage all teams and players", _MGMT | {EDITOR}, True),
    ("teams.manage_assigned", "Manage the teams they're assigned to", {TEAM_MANAGER, COACH}, True),
    ("team.invite", "Invite players and coaches to the teams they're assigned to", {TEAM_MANAGER}, True),
    ("content.edit", "Edit competition information and news", _MGMT | {EDITOR}, True),
    ("content.moderate", "Moderate announcements and content", _MGMT | {MODERATOR}, True),
    ("org.settings", "Edit organization profile, branding and public website", {OWNER, ADMIN}, True),
    ("payments.manage", "Send invoices, see payments and request payouts", {OWNER, ADMIN}, True),
    ("roles.manage", "Change what each role can do", {OWNER}, False),
    ("org.transfer", "Transfer ownership", {OWNER}, False),
    ("org.delete", "Delete the organization", {OWNER}, False),
]
_DEFAULTS = {key: roles for key, _, roles, _ in PERMISSIONS}
CONFIGURABLE = {key for key, _, _, ok in PERMISSIONS if ok}


def roles_for(perm, org=None):
    """The roles that have `perm` in this organization (its owner's settings, or the defaults)."""
    custom = (getattr(org, "permissions", None) or {}).get(perm) if perm in CONFIGURABLE else None
    roles = set(custom) & set(ROLES) if isinstance(custom, list) else set(_DEFAULTS.get(perm, ()))
    return roles | ({OWNER} if perm in _DEFAULTS else set())          # the owner can always do everything


def can(role, perm, org=None):
    return role in roles_for(perm, org)


def perms_of(role, org=None):
    return [key for key, *_ in PERMISSIONS if can(role, key, org)]


def assignable_roles(actor_role):
    """Roles this person may give to others: strictly below their own (ownership moves only by transfer)."""
    return [r for r in ROLES if r != OWNER and RANK[r] < RANK[actor_role]]


def can_manage(actor_role, target_role, org=None):
    """May the actor change or remove someone who currently has target_role?"""
    return can(actor_role, "members.manage", org) and RANK[target_role] < RANK[actor_role]


def matrix(org=None):
    """For "Roles & permissions": every role, every permission, who has it here, and what the owner may change."""
    return {
        "roles": [{"key": r, "label": ROLE_INFO[r][0], "description": ROLE_INFO[r][1], "staff": r in STAFF_ROLES} for r in ROLES],
        "permissions": [{"key": k, "label": label, "configurable": ok, "roles": [r for r in ROLES if can(r, k, org)],
                         "defaults": sorted(_DEFAULTS[k] | {OWNER}, key=ROLES.index)} for k, label, _, ok in PERMISSIONS],
    }


def clean_overrides(data):
    """Validate an owner's permission settings: {perm: [roles]} for configurable permissions only (owner implied)."""
    if not isinstance(data, dict):
        raise ValueError("Send permissions as {permission: [roles]}.")
    out = {}
    for k, v in data.items():
        if k not in CONFIGURABLE:
            raise ValueError(f"“{k}” can't be changed.")
        if not isinstance(v, list) or not all(r in ROLES for r in v):
            raise ValueError(f"Unknown role in {k}.")
        out[k] = sorted({r for r in v if r != OWNER}, key=ROLES.index)
    return out
