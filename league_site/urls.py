from django.conf import settings
from django.urls import path, re_path

from league import views as v
from league.http import not_found, serve_page
from competitions import api as c
from competitions import discover
from competitions import public as pub
from orgs import api as o
from payments import api as pay
from superadmin import api as sa

handler404 = "league.http.not_found"
handler500 = "league.http.server_error"


def state(request):
    return v.save_state(request) if request.method == "PUT" else v.state(request)


def admins(request):
    return v.invite(request) if request.method == "POST" else v.admins(request)


def app_page(request, **kwargs):
    return serve_page(request, settings.APP_FILE)


urlpatterns = [
    # ---- the original league page and its API ----
    path("", discover.home),     # the landing page (or the original league when HOME_PAGE=league)
    path("classic", v.index),
    path("index.html", v.index),
    path("api/state", state),
    path("api/seed", v.seed_view),
    path("api/me", v.me_view),
    path("api/login", v.login_view),
    path("api/logout", v.logout_view),
    path("api/logout-all", v.logout_all),
    path("api/password", v.change_password),
    path("api/forgot", v.forgot),
    path("api/token/check", v.token_check),
    path("api/token/use", v.token_use),
    path("api/draws", v.draws),
    path("api/draw/groups", v.draw_groups),
    path("api/draw/knockout", v.draw_knockout),
    path("api/admins", admins),
    path("api/admins/resend", v.resend),
    path("api/admins/remove", v.remove),
    path("api/email", v.set_email),
    path("api/smtp/test", v.test_email),

    # ---- the platform: accounts, organizations, members, invitations ----
    path("app", app_page),
    path("admin", app_page),
    re_path(r"^admin/.*$", app_page),
    re_path(r"^app/.*$", app_page),
    path("invite/<str:token>", app_page),
    path("api/auth/me", o.me),
    path("api/account", o.account),
    path("api/account/password", o.account_password),
    path("api/auth/signup", o.signup),
    path("api/auth/login", o.login_view),
    path("api/auth/logout", o.logout_view),
    path("api/roles", o.roles),
    path("api/orgs", o.orgs),
    path("api/org-slug", o.slug_check),
    path("api/orgs/<slug:slug>", o.org_detail),
    path("api/orgs/<slug:slug>/transfer", o.transfer),
    path("api/orgs/<slug:slug>/permissions", o.org_permissions),
    path("api/orgs/<slug:slug>/payments", pay.org_payments),
    path("api/orgs/<slug:slug>/payments/sync", pay.org_sync),
    path("api/orgs/<slug:slug>/invoices", pay.org_invoices),
    path("api/orgs/<slug:slug>/invoices/<int:inv_id>/cancel", pay.org_invoice_cancel),
    path("api/orgs/<slug:slug>/payouts", pay.org_payouts),
    path("api/orgs/<slug:slug>/payouts/<int:payout_id>/cancel", pay.org_payout_cancel),
    path("api/orgs/<slug:slug>/activity", o.activity),
    path("api/orgs/<slug:slug>/logo", o.org_logo_upload),
    path("api/orgs/<slug:slug>/leave", o.leave),
    path("api/orgs/<slug:slug>/members", o.members),
    path("api/orgs/<slug:slug>/members/<int:member_id>", o.member_detail),
    path("api/orgs/<slug:slug>/invitations", o.invitations),
    path("api/orgs/<slug:slug>/invitations/<int:inv_id>/revoke", o.invitation_action, {"action": "revoke"}),
    path("api/orgs/<slug:slug>/invitations/<int:inv_id>/resend", o.invitation_action, {"action": "resend"}),
    path("api/invitations/<str:token>", o.invitation_public),
    path("api/invitations/<str:token>/accept", o.invitation_accept),
    path("api/me/invitations", o.my_invitations),
    path("api/me/invitations/<int:inv_id>/accept", o.my_invitation_accept),

    # ---- competitions, teams, matches (inside an organization) ----
    path("api/orgs/<slug:slug>/summary", c.summary),
    path("api/orgs/<slug:slug>/competitions", c.competitions),
    path("api/orgs/<slug:slug>/competitions/<slug:cslug>", c.competition_detail),
    path("api/orgs/<slug:slug>/competitions/<slug:cslug>/logo", c.competition_logo),
    path("api/orgs/<slug:slug>/competitions/<slug:cslug>/entries", c.entries),
    path("api/orgs/<slug:slug>/competitions/<slug:cslug>/entries/<int:entry_id>", c.entry_detail),
    path("api/orgs/<slug:slug>/competitions/<slug:cslug>/generate", c.generate),
    path("api/orgs/<slug:slug>/competitions/<slug:cslug>/draw", c.draw),
    path("api/orgs/<slug:slug>/competitions/<slug:cslug>/dates", c.set_dates),
    path("api/orgs/<slug:slug>/competitions/<slug:cslug>/knockout-plan", c.knockout_plan),
    path("api/orgs/<slug:slug>/competitions/<slug:cslug>/matches", c.comp_matches),
    path("api/orgs/<slug:slug>/competitions/<slug:cslug>/standings", c.standings),
    path("api/orgs/<slug:slug>/competitions/<slug:cslug>/scorers", c.comp_scorers),
    path("api/orgs/<slug:slug>/matches", c.org_matches),
    path("api/orgs/<slug:slug>/matches/<int:match_id>", c.match_detail),
    path("api/orgs/<slug:slug>/matches/<int:match_id>/events", c.match_events),
    path("api/orgs/<slug:slug>/matches/<int:match_id>/events/<int:event_id>", c.match_event_detail),
    path("api/orgs/<slug:slug>/teams", c.teams),
    path("api/orgs/<slug:slug>/teams/<int:team_id>", c.team_detail),
    path("api/orgs/<slug:slug>/teams/<int:team_id>/logo", c.team_logo),
    path("api/orgs/<slug:slug>/teams/<int:team_id>/invitations", c.team_invitations),
    path("api/orgs/<slug:slug>/teams/<int:team_id>/players", c.team_players),
    path("api/orgs/<slug:slug>/players", c.players),
    path("api/orgs/<slug:slug>/players/<int:player_id>", c.player_detail),
    path("api/orgs/<slug:slug>/announcements", c.announcements),
    path("api/orgs/<slug:slug>/announcements/<int:ann_id>", c.announcement_detail),
    # ---- super admin (whole platform; every endpoint requires is_superuser) ----
    path("api/admin/overview", sa.overview),
    path("api/admin/analytics", sa.analytics),
    path("api/admin/search", sa.search),
    path("api/admin/users", sa.users),
    path("api/admin/users/bulk", sa.users_bulk),
    path("api/admin/users/<int:uid>", sa.user_detail),
    path("api/admin/users/<int:uid>/<slug:action>", sa.user_act),
    path("api/admin/organizations", sa.organizations),
    path("api/admin/organizations/bulk", sa.organizations_bulk),
    path("api/admin/organizations/<int:oid>", sa.organization_detail),
    path("api/admin/organizations/<int:oid>/<slug:action>", sa.organization_act),
    path("api/admin/competitions", sa.competitions),
    path("api/admin/competitions/bulk", sa.competitions_bulk),
    path("api/admin/competitions/<int:cid>", sa.competition_detail),
    path("api/admin/competitions/<int:cid>/standings", sa.standings),
    path("api/admin/competitions/<int:cid>/<slug:action>", sa.competition_act),
    path("api/admin/teams", sa.teams),
    path("api/admin/teams/bulk", sa.teams_bulk),
    path("api/admin/teams/<int:tid>/<slug:action>", sa.team_act),
    path("api/admin/players", sa.players),
    path("api/admin/matches", sa.matches),
    path("api/admin/result-changes", sa.result_changes),
    path("api/admin/groups", sa.groups),
    path("api/admin/staff", sa.staff),
    path("api/admin/invitations", sa.invitations),
    path("api/admin/invitations/<int:iid>/revoke", sa.invitation_revoke),
    path("api/admin/audit", sa.audit_log),
    path("api/admin/announcements", sa.announcements),
    path("api/admin/announcements/<int:aid>", sa.announcement_detail),
    path("api/admin/org-announcements/<int:aid>", sa.org_announcement),
    path("api/admin/settings", sa.platform_settings),
    path("api/admin/settings/test-email", sa.test_email),
    path("api/admin/payments", pay.admin_payments),
    path("api/admin/payments/test", pay.admin_payments_test),
    path("api/admin/payouts/<int:payout_id>/<slug:action>", lambda r, payout_id, action: pay.admin_payout_act(r, payout_id=payout_id, action=action) if action in ("approve", "reject") else not_found(r)),
    path("api/admin/system", sa.system),
    path("api/admin/security/logout-everyone", sa.logout_everyone),
    path("api/admin/tickets", sa.tickets),
    path("api/admin/tickets/<int:tid>", sa.ticket_detail),
    path("api/support", sa.my_support),

    # ---- public pages (no login) ----
    path("competitions", discover.competitions),
    path("search", discover.search),
    path("competition/<slug:slug>", pub.competition),
    path("competition/<slug:slug>/<slug:tab>", pub.competition),
    path("league/<slug:slug>", pub.league_alias),
    path("league/<slug:slug>/<slug:tab>", pub.league_alias),
    path("match/<slug:slug>", pub.match),
    path("team/<slug:slug>", pub.team),
    path("org/<slug:slug>", pub.organization),
    path("organization/<slug:slug>", pub.organization_alias),
    path("robots.txt", pub.robots),
    path("sitemap.xml", pub.sitemap),
    path("media/<str:kind>/<int:obj_id>/logo", lambda r, kind, obj_id: c.logo_file(r, kind, obj_id) if kind in ("competition", "team", "org") else not_found(r)),
]
