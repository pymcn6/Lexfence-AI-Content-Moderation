# -*- coding: utf-8 -*-
"""WTForms 表单定义（自带 CSRF 保护）。"""

from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, IntegerField, SubmitField, TextAreaField, BooleanField, SelectField, FloatField
from wtforms.validators import DataRequired, Length, EqualTo, NumberRange, Optional


class LoginForm(FlaskForm):
    username = StringField("用户名", validators=[DataRequired(), Length(min=1, max=80)])
    password = PasswordField("密码", validators=[DataRequired(), Length(min=1, max=128)])
    submit = SubmitField("登录")


class ChangePasswordForm(FlaskForm):
    old_password = PasswordField("原密码", validators=[DataRequired()])
    new_password = PasswordField("新密码", validators=[DataRequired(), Length(min=6, max=128)])
    confirm_password = PasswordField(
        "确认新密码",
        validators=[DataRequired(), EqualTo("new_password", message="两次输入密码不一致")],
    )
    submit = SubmitField("修改密码")


class UserForm(FlaskForm):
    username = StringField("用户名", validators=[DataRequired(), Length(min=1, max=80)])
    password = PasswordField("初始密码", validators=[DataRequired(), Length(min=6, max=128)])
    monthly_quota = IntegerField(
        "每月请求限额",
        default=1000,
        validators=[DataRequired(), NumberRange(min=0, message="配额不能为负数")],
    )
    max_text_length = IntegerField(
        "单次最大字数",
        default=5000,
        validators=[DataRequired(), NumberRange(min=1, max=100000, message="字数限制应在 1-100000 之间")],
    )
    prompt_quota = IntegerField(
        "提示词提交配额",
        default=10,
        validators=[DataRequired(), NumberRange(min=0, message="配额不能为负数")],
    )
    submit = SubmitField("创建用户")


class QuotaForm(FlaskForm):
    monthly_quota = IntegerField(
        "每月请求限额",
        validators=[DataRequired(), NumberRange(min=0, message="配额不能为负数")],
    )
    current_quota = IntegerField(
        "当前剩余配额（留空则等于每月限额）",
        validators=[Optional(), NumberRange(min=0, message="配额不能为负数")],
    )
    max_text_length = IntegerField(
        "单次最大字数",
        validators=[DataRequired(), NumberRange(min=1, max=100000, message="字数限制应在 1-100000 之间")],
    )
    prompt_quota = IntegerField(
        "提示词提交配额",
        validators=[DataRequired(), NumberRange(min=0, message="配额不能为负数")],
    )
    submit = SubmitField("更新配额")


class SettingsForm(FlaskForm):
    system_prompt = TextAreaField("Detection prompt", validators=[Optional(), Length(max=8000)])
    fail_open = BooleanField("Allow when all models fail (off = treat as violation)")
    fallback_allow = BooleanField("Allow when AI response category is unrecognized (off = treat as violation, classified as fallback)")
    default_max_tokens = IntegerField(
        "Default max output tokens (used when a model has no per-model setting)",
        validators=[DataRequired(), NumberRange(min=16, max=32768)],
    )
    log_keep_per_user = IntegerField("Logs kept per user", validators=[DataRequired(), NumberRange(min=10, max=5000)])
    token_reserve_text = IntegerField("Token reserve per text detection", validators=[DataRequired(), NumberRange(min=1, max=1000000)])
    token_reserve_image = IntegerField("Token reserve per image detection", validators=[DataRequired(), NumberRange(min=1, max=1000000)])
    token_reserve_video = IntegerField("Token reserve per video detection", validators=[DataRequired(), NumberRange(min=1, max=1000000)])
    bill_keep_days = IntegerField("Bill retention days (older detection records are deleted)", validators=[DataRequired(), NumberRange(min=1, max=365)])
    homepage_iframe_url = StringField("Homepage iframe URL (public; leave empty to redirect to login)", validators=[Optional(), Length(max=500)])
    recharge_iframe_url = StringField("Online recharge iframe URL (public)", validators=[Optional(), Length(max=500)])
    pricing_enabled = BooleanField("Show pricing page")
    pricing_text_per_m = FloatField("Text price per 1M tokens (base currency)", validators=[Optional(), NumberRange(min=0, max=1000000)])
    pricing_image_per_m = FloatField("Image price per 1M tokens (base currency)", validators=[Optional(), NumberRange(min=0, max=1000000)])
    pricing_video_per_m = FloatField("Video price per 1M tokens (base currency)", validators=[Optional(), NumberRange(min=0, max=1000000)])
    pricing_currencies = TextAreaField("Currencies (one per line: code,symbol,rate; first line is the base currency)", validators=[Optional(), Length(max=2000)])
    pricing_note = TextAreaField("Pricing note (optional)", validators=[Optional(), Length(max=1000)])
    default_max_api_keys = IntegerField("Default max API keys per user", validators=[Optional(), NumberRange(min=0, max=1000)])
    contact_info = StringField("Site contact (shown when a user needs more, e.g. email / phone)", validators=[Optional(), Length(max=200)])
    demo_enabled = BooleanField("Enable demo mode (/demomode read-only demo without login)")
    submit = SubmitField("Save settings")


