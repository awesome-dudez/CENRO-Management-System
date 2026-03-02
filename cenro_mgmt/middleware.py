"""
Authentication middleware and exception logging for production.
"""
import logging
from django.shortcuts import redirect
from django.urls import reverse

logger = logging.getLogger(__name__)


class ExceptionLoggingMiddleware:
    """
    Logs every unhandled exception with full traceback to stdout (Render logs).
    Must be first in MIDDLEWARE so it wraps the rest.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except Exception as e:
            logger.exception("Unhandled exception (500): %s", e)
            raise


class LoginRequiredMiddleware:
    """
    Middleware that requires users to be logged in.
    Allows access to:
    - /accounts/login/ (login page)
    - /accounts/logout/ (logout)
    - /accounts/register/ (registration pages)
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
        
        # Pages that don't require login
        self.public_paths = [
            '/accounts/login/',
            '/accounts/logout/',
            '/accounts/register/consumer/',
            '/accounts/register/staff/',
            '/static/',
            '/media/',
        ]
    
    def __call__(self, request):
        # Check if path is public
        is_public = any(request.path.startswith(path) for path in self.public_paths)
        # Only access .is_authenticated (safe for AnonymousUser); never .role or .is_approved here
        if not is_public and getattr(request.user, "is_authenticated", False) is False:
            return redirect(f"{reverse('accounts:login')}?next={request.path}")
        return self.get_response(request)
