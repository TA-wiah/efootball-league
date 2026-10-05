from django.urls import path

from league import views as v

handler404 = "league.views.not_found"
handler500 = "league.views.server_error"


def state(request):
    return v.save_state(request) if request.method == "PUT" else v.state(request)


def admins(request):
    return v.invite(request) if request.method == "POST" else v.admins(request)


urlpatterns = [
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
]
