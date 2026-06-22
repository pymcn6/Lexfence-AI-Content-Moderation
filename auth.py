# -*- coding: utf-8 -*-
"""认证相关路由。"""

from urllib.parse import urlparse, urljoin

from flask import Blueprint, render_template, redirect, url_for, flash, request, session, current_app
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash

from app import limiter
from models import db, User
from forms import LoginForm, ChangePasswordForm
from i18n import _

auth_bp = Blueprint("auth", __name__)


def _is_safe_url(target: str) -> bool:
    """防止开放重定向：仅允许同站点相对/绝对路径。"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ("http", "https") and ref_url.netloc == test_url.netloc


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 50 per hour")
def login():
    if current_user.is_authenticated and not session.get("is_demo"):
        return redirect(url_for("web.index"))

    form = LoginForm()
    if form.validate_on_submit():
        import captcha
        if not captcha.verify(request.form, session):
            flash(_("Captcha verification failed"), "danger")
            return render_template("login.html", form=form, **_captcha_ctx())
        username = form.username.data.strip()
        password = form.password.data
        user = User.query.filter_by(username=username, active=True).first()

        if user and user.check_password(password):
            session.pop("is_demo", None)  # 清除可能残留的体验会话标志
            login_user(user, remember=False)
            next_page = request.args.get("next")
            # 防止开放重定向（含协议相对路径 //evil.com）
            if next_page and not _is_safe_url(next_page):
                next_page = None
            return redirect(next_page or url_for("web.index"))

        flash(_("Invalid credentials"), "danger")

    return render_template("login.html", form=form, **_captcha_ctx())


def _captcha_ctx():
    """为登录/注册页提供验证码上下文（图形/算术文本/Turnstile/hCaptcha/reCAPTCHA）。"""
    import captcha
    enabled = captcha.is_enabled()
    ctype = captcha.effective_type() if enabled else "image"
    ctx = {"captcha_enabled": enabled,
           "captcha_type": ctype,
           "captcha_image": None,
           "captcha_question": None,
           "captcha_site_key": captcha.site_key(),
           "registration_enabled": _registration_enabled()}
    if not enabled:
        return ctx
    if ctype == "image":
        try:
            data_uri, answer = captcha.generate_image()
            captcha.store_answer(session, answer)
            ctx["captcha_image"] = data_uri
        except Exception:
            # 图片生成失败（如缺 Pillow/字体）时降级为算术文本验证码，避免 500
            ctx["captcha_type"] = "text"
            q, answer = captcha.generate_text_challenge()
            captcha.store_answer(session, answer)
            ctx["captcha_question"] = q
    elif ctype == "text":
        q, answer = captcha.generate_text_challenge()
        captcha.store_answer(session, answer)
        ctx["captcha_question"] = q
    return ctx


def _registration_enabled() -> bool:
    import settings_store
    return settings_store.get_bool("registration_enabled")


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash(_("Logged out"), "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute")
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.old_password.data):
            flash(_("Old password is incorrect"), "danger")
        else:
            current_user.set_password(form.new_password.data)
            db.session.commit()
            flash(_("Password changed, please sign in again"), "success")
            logout_user()
            return redirect(url_for("auth.login"))
    return render_template("change_password.html", form=form)


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute; 30 per hour")
def register():
    import settings_store
    import captcha
    if not _registration_enabled():
        flash(_("Registration is closed"), "warning")
        return redirect(url_for("auth.login"))
    if current_user.is_authenticated and not session.get("is_demo"):
        return redirect(url_for("web.index"))

    verify_mode = settings_store.get_setting("registration_verify", "none")

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""

        if not captcha.verify(request.form, session):
            flash(_("Captcha verification failed"), "danger")
            return render_template("register.html", verify_mode=verify_mode, **_captcha_ctx())

        errors = []
        if not username or len(username) > 80:
            errors.append(_("Invalid username"))
        if len(password) < 6 or len(password) > 128:
            errors.append(_("Password must be 6-128 characters"))
        if password != password2:
            errors.append(_("Passwords do not match"))
        if verify_mode == "email" and ("@" not in email or len(email) > 255):
            errors.append(_("Invalid email"))
        if User.query.filter_by(username=username).first():
            errors.append(_("Username already exists"))
        if email and User.query.filter_by(email=email).first():
            errors.append(_("Email already registered"))
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("register.html", verify_mode=verify_mode, **_captcha_ctx())

        user = User(
            username=username,
            email=email or None,
            monthly_quota=settings_store.get_int("register_default_quota", 1000),
            quota=settings_store.get_int("register_default_quota", 1000),
            max_text_length=settings_store.get_int("register_default_max_text", 5000),
            prompt_quota=settings_store.get_int("register_default_prompt_quota", 10),
            is_admin=False,
        )
        user.set_password(password)

        if verify_mode == "email":
            import email_utils
            if not email_utils.smtp_configured():
                flash(_("Email verification is not configured; contact the admin"), "danger")
                return render_template("register.html", verify_mode=verify_mode, **_captcha_ctx())
            user.active = False
            user.email_verified = False
            db.session.add(user)
            db.session.commit()
            token = email_utils.make_token(email)
            verify_url = url_for("auth.verify_email", token=token, _external=True)
            email_utils.send_verification_async(current_app._get_current_object(), email, verify_url)
            flash(_("Verification email sent, please check your inbox"), "success")
            return redirect(url_for("auth.login"))
        elif verify_mode == "admin":
            user.active = False
            db.session.add(user)
            db.session.commit()
            flash(_("Registration submitted, waiting for admin approval"), "success")
            return redirect(url_for("auth.login"))
        else:
            user.active = True
            db.session.add(user)
            db.session.commit()
            flash(_("Registration successful, please sign in"), "success")
            return redirect(url_for("auth.login"))

    return render_template("register.html", verify_mode=verify_mode, **_captcha_ctx())


@auth_bp.route("/verify-email/<token>")
def verify_email(token):
    import email_utils
    email = email_utils.verify_token(token)
    if not email:
        flash(_("Verification link is invalid or expired"), "danger")
        return redirect(url_for("auth.login"))
    user = User.query.filter_by(email=email).first()
    if not user:
        flash(_("Account not found"), "danger")
        return redirect(url_for("auth.login"))
    user.email_verified = True
    user.active = True
    db.session.commit()
    flash(_("Email verified, your account is now active. Please sign in"), "success")
    return redirect(url_for("auth.login"))
