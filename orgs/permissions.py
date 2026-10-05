"""Who can do what inside an organization. The single source of truth: the API checks these on every request,
and the dashboard only uses them to decide which buttons to show."""

OWNER, ORGANIZER, ADMIN, EDITOR, MODERATOR, VIEWER = "owner", "organizer", "admin", "editor", "moderator", "viewer"
ROLES = [OWNER, ORGANIZER, ADMIN, EDITOR, MODERATOR, VIEWER]
RANK = {OWNER: 6, ORGANIZER: 5, ADMIN: 4, EDITOR: 3, MODERATOR: 2, VIEWER: 1}

ROLE_INFO = {
    OWNER: ("Owner", "Full access, including settings, ownership transfer and deleting the organization."),
    ORGANIZER: ("Organizer", "Runs competitions: teams, fixtures, results, tables and members below organizer."),
    ADMIN: ("Admin", "Like an organizer, but can only manage editors, moderators and viewers."),
    EDITOR: ("Editor", "Updates scores, matches, team information and competition content."),
    MODERATOR: ("Moderator", "Looks after published content, match information, teams and users."),
    VIEWER: ("Viewer", "Read-only access to the organization's dashboard."),
}

_STAFF = {OWNER, ORGANIZER, ADMIN}
PERMISSIONS = [
    # (key, what it lets you do, roles that have it)
    ("org.view", "See the organization dashboard", set(ROLES)),
    ("members.view", "See the member list", set(ROLES)),
    ("members.invite", "Invite people and manage invitations", _STAFF),
    ("members.manage", "Change roles and remove members", _STAFF),
    ("competitions.manage", "Create and manage competitions", _STAFF),
    ("teams.manage", "Manage teams and players", _STAFF | {EDITOR}),
    ("fixtures.manage", "Create fixtures and competition stages", _STAFF),
    ("results.enter", "Enter and update match results", _STAFF | {EDITOR}),
    ("standings.manage", "Manage league tables", _STAFF),
    ("content.edit", "Edit competition information and news", _STAFF | {EDITOR}),
    ("content.moderate", "Moderate content, match information, teams and users", _STAFF | {MODERATOR}),
    ("org.settings", "Change organization settings", {OWNER}),
    ("org.transfer", "Transfer ownership", {OWNER}),
    ("org.delete", "Delete the organization", {OWNER}),
]
_BY_KEY = {key: roles for key, _, roles in PERMISSIONS}


def can(role, perm):
    return role in _BY_KEY.get(perm, ())


def perms_of(role):
    return [key for key, _, roles in PERMISSIONS if role in roles]


def assignable_roles(actor_role):
    """Roles this person may give to others: strictly below their own (ownership moves only by transfer)."""
    return [r for r in ROLES if r != OWNER and RANK[r] < RANK[actor_role]]


def can_manage(actor_role, target_role):
    """May the actor change or remove someone who currently has target_role?"""
    return can(actor_role, "members.manage") and RANK[target_role] < RANK[actor_role]


def matrix():
    """For the dashboard's "What each role can do" table."""
    return {
        "roles": [{"key": r, "label": ROLE_INFO[r][0], "description": ROLE_INFO[r][1]} for r in ROLES],
        "permissions": [{"key": k, "label": label, "roles": [r for r in ROLES if r in roles]} for k, label, roles in PERMISSIONS],
    }
