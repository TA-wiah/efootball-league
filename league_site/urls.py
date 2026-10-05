from django.conf import settings
from django.urls import path, re_path

from league import views as v
from league.http import serve_page
from orgs import api as o

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
    path("", v.index),
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
    re_path(r"^app/.*$", app_page),
    path("invite/<str:token>", app_page),
    path("api/auth/me", o.me),
    path("api/auth/signup", o.signup),
    path("api/auth/login", o.login_view),
    path("api/auth/logout", o.logout_view),
    path("api/roles", o.roles),
    path("api/orgs", o.orgs),
    path("api/orgs/<slug:slug>", o.org_detail),
    path("api/orgs/<slug:slug>/transfer", o.transfer),
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
]
