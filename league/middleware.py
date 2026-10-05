from django.db import connection
from django.http import JsonResponse


def health(get_response):
    """GET /api/health answers before any host check, so platform health probes work on internal addresses."""
    def middleware(request):
        if request.path == "/api/health" and request.method == "GET":
            try:
                connection.ensure_connection()
                return JsonResponse({"ok": True}, headers={"Cache-Control": "no-store"})
            except Exception:
                return JsonResponse({"ok": False, "error": "database unavailable"}, status=503)
        return get_response(request)
    return middleware


def security_headers(get_response):
    def middleware(request):
        response = get_response(request)
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        response.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.path.startswith("/api/"):
            response["Cache-Control"] = "no-store"
        return response
    return middleware
