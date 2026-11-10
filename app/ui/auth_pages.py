"""Authentication and password recovery pages."""

from nicegui import context, ui, run, app as _napp
from app import login_guard, password_reset, users
from app.ui import session
from app.ui.theme import apply_auth_theme
from app.ui.footer import footer as _footer


def register_auth_pages():
    """Register login and password recovery routes."""
    @ui.page("/login")
    def login_page():
        """Render the login form or redirect an already authenticated user."""
        if session.current_user() is not None:
            ui.navigate.to("/ui")
            return

        apply_auth_theme()

        def do_login():
            """Authenticate the form credentials and store a session token on success."""
            request = context.client.request
            ip = login_guard.client_ip(request.headers.get("x-forwarded-for") if request else None,
                                       request.client.host if request and request.client else None)
            try:
                user = login_guard.authenticate(email_inp.value or "", password_inp.value or "", ip)
            except login_guard.Locked as locked:
                error.set_text(f"Too many failed attempts. Try again in {max(1, round(locked.retry_after / 60))} minutes.")
                return
            if user:
                _napp.storage.user["token"] = users.issue_token(user)
                ui.navigate.to("/ui")
            else:
                error.set_text("Invalid email or password")

        with ui.element("div").classes("login-bg"):
            with ui.element("div").classes("login-card"):
                ui.label("EPISERVE").classes("text-2xl font-bold text-white text-center w-full mb-8").style("display:block")
                email_inp = ui.input("Email").props('outlined dense prefix-icon="mail"').classes("w-full mb-4")
                password_inp = ui.input("Password").props('outlined dense type=password prefix-icon="lock"').classes("w-full mb-2")
                error = ui.label("").classes("text-red-400 text-sm mb-1").style("min-height:1.25rem")
                ui.button("LOGIN", on_click=do_login).classes("w-full login-btn mt-3")
                ui.link("Forgot your password?", "/forgot-password").classes("text-sm text-gray-300 text-center mt-4")
        _footer()


    @ui.page("/forgot-password")
    def forgot_password_page():
        """Render the password reset email request form."""
        apply_auth_theme()

        async def send():
            """Request a reset email and display the non-disclosing confirmation."""
            email = email_inp.value or ""
            await run.io_bound(password_reset.request_reset, email)
            message.set_text("If this email belongs to an account, a mail with a reset link is on its way.")
            send_btn.disable()

        with ui.element("div").classes("login-bg"):
            with ui.element("div").classes("login-card"):
                ui.label("EPISERVE").classes("text-2xl font-bold text-white text-center w-full mb-4").style("display:block")
                ui.label("Enter your email address and we send you a link to choose a new password.").classes(
                    "text-sm text-gray-300 mb-4")
                email_inp = ui.input("Email").props('outlined dense prefix-icon="mail"').classes("w-full mb-2")
                message = ui.label("").classes("text-green-300 text-sm mb-1").style("min-height:1.25rem")
                send_btn = ui.button("SEND LINK", on_click=send).classes("w-full login-btn mt-3")
                ui.link("Back to login", "/login").classes("text-sm text-gray-300 text-center mt-4")
        _footer()


    @ui.page("/reset-password")
    def reset_password_page(token: str = ""):
        """Render the password replacement form for a signed reset token.

        Args:
            token: Bearer token included in the copied command.
        """
        apply_auth_theme()

        async def save():
            """Validate the password fields and consume the signed reset token."""
            if (password_inp.value or "") != (repeat_inp.value or ""):
                error.set_text("The two passwords differ.")
                return
            problem = await run.io_bound(password_reset.complete_reset, token, password_inp.value or "")
            if problem:
                error.set_text(problem)
                return
            ui.notify("Password changed. Please log in.", type="positive")
            ui.navigate.to("/login")

        with ui.element("div").classes("login-bg"):
            with ui.element("div").classes("login-card"):
                ui.label("EPISERVE").classes("text-2xl font-bold text-white text-center w-full mb-4").style("display:block")
                ui.label("Choose a new password.").classes("text-sm text-gray-300 mb-4")
                password_inp = ui.input("New password").props('outlined dense type=password prefix-icon="lock"').classes("w-full mb-4")
                repeat_inp = ui.input("Repeat password").props('outlined dense type=password prefix-icon="lock"').classes("w-full mb-2")
                error = ui.label("").classes("text-red-400 text-sm mb-1").style("min-height:1.25rem")
                ui.button("SAVE PASSWORD", on_click=save).classes("w-full login-btn mt-3")
                ui.link("Back to login", "/login").classes("text-sm text-gray-300 text-center mt-4")
        _footer()
