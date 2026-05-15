"""
Authentication routes for the household budget.

Design choices:
- The app supports a hard maximum of MAX_USERS (=2) logins. This is enforced
  at the model layer and again in the user-creation routes.
- First-run flow: if there are zero users in the database, GET / redirects to
  /setup, which lets the very first visitor create the admin account. The
  admin then invites the second user from /users.
- There is NO public registration endpoint. Once /setup has run, the only way
  to add a user is for the admin to do it from inside the app.
- Logins are rate-limited to slow down credential stuffing.
- All POST forms are CSRF-protected by Flask-WTF.
"""
from datetime import datetime
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, redirect,
                   render_template, request, session, url_for)
from flask_login import (LoginManager, current_user, login_required,
                          login_user, logout_user)

from models import MAX_USERS, User, db

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please sign in to access the household budget."
login_manager.session_protection = "strong"

auth_bp = Blueprint("auth", __name__)


@login_manager.user_loader
def _load_user(user_id: str):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


def admin_required(view):
    """Decorator that 403s any non-admin user. Used on the user-management
    page so only the household admin can invite the second user."""
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def setup_required(view):
    """Redirect to /setup if no users exist yet (first-run)."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if User.query.count() == 0:
            return redirect(url_for("auth.setup"))
        return view(*args, **kwargs)
    return wrapper


# ---------- routes ----------

@auth_bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First-run: lets the very first visitor create the admin account.
    Disabled once any user exists, so this endpoint cannot be used to
    overwrite or add accounts later."""
    if User.query.count() > 0:
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        display = request.form.get("display_name", "").strip() or email.split("@")[0]
        err = _validate_credentials(email, password)
        if err:
            flash(err, "danger")
        else:
            user = User(email=email, display_name=display, is_admin=True)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user, remember=True)
            flash("Welcome! Your household budget is ready. Add your second user from the Users page.", "success")
            return redirect(url_for("dashboard"))
    return render_template("setup.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if User.query.count() == 0:
        return redirect(url_for("auth.setup"))

    if request.method == "POST":
        # Rate limit is applied externally via Flask-Limiter (see app.py).
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user is None or not user.check_password(password):
            # Deliberate vague error message — don't reveal whether email exists.
            flash("Invalid email or password.", "danger")
            return render_template("login.html"), 401
        login_user(user, remember=bool(request.form.get("remember")))
        user.last_login_at = datetime.utcnow()
        db.session.commit()
        nxt = request.args.get("next") or url_for("dashboard")
        # Avoid open-redirect: only allow same-host relative paths.
        if not nxt.startswith("/"):
            nxt = url_for("dashboard")
        return redirect(nxt)

    return render_template("login.html")


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Signed out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/users", methods=["GET", "POST"])
@admin_required
def users_list():
    """Admin-only page to invite the second household user."""
    if request.method == "POST":
        if User.query.count() >= MAX_USERS:
            flash(f"This budget is limited to {MAX_USERS} users.", "warning")
            return redirect(url_for("auth.users_list"))
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        display = request.form.get("display_name", "").strip() or email.split("@")[0]
        err = _validate_credentials(email, password)
        if err:
            flash(err, "danger")
        else:
            u = User(email=email, display_name=display, is_admin=False)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            flash(f"Invited {u.display_name} ({u.email}). Share the password securely.", "success")
            return redirect(url_for("auth.users_list"))

    users = User.query.order_by(User.id).all()
    return render_template("users.html", users=users, max_users=MAX_USERS,
                            can_add=User.query.count() < MAX_USERS)


@auth_bp.route("/users/<int:uid>/delete", methods=["POST"])
@admin_required
def user_delete(uid):
    user = db.session.get(User, uid)
    if user is None:
        abort(404)
    if user.is_admin:
        flash("You can't delete the admin account.", "danger")
        return redirect(url_for("auth.users_list"))
    db.session.delete(user)
    db.session.commit()
    flash(f"Removed {user.email}.", "warning")
    return redirect(url_for("auth.users_list"))


@auth_bp.route("/users/<int:uid>/password", methods=["POST"])
@login_required
def user_change_password(uid):
    """Each user can change their own password. Admin can change anyone's."""
    user = db.session.get(User, uid)
    if user is None:
        abort(404)
    if user.id != current_user.id and not current_user.is_admin:
        abort(403)
    new_pw = request.form.get("password", "")
    err = _validate_password(new_pw)
    if err:
        flash(err, "danger")
    else:
        user.set_password(new_pw)
        db.session.commit()
        flash("Password updated.", "success")
    return redirect(url_for("auth.users_list"))


# ---------- helpers ----------

def _validate_credentials(email: str, password: str) -> str | None:
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return "Please enter a valid email address."
    if User.query.filter_by(email=email).first():
        return "That email is already in use."
    return _validate_password(password)


def _validate_password(password: str) -> str | None:
    if not password or len(password) < 10:
        return "Password must be at least 10 characters."
    return None
