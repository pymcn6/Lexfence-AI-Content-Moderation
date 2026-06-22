# -*- coding: utf-8 -*-
"""
体验模式（demo）独立数据库。

与真实业务【物理隔离】：使用独立的 SQLite 文件 instance/demo.db，
独立 engine + 独立 session，绝不触碰真实的 db.session（MySQL）。
模型类复用 models 中的定义，但所有读写只发生在 demo.db。
"""

import os
import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import scoped_session, sessionmaker

from config import INSTANCE_DIR
from models import db, User, ApiKey, DetectionLog, AppSetting, StatCounter, UserPrompt

DEMO_DB_PATH = os.path.join(INSTANCE_DIR, "demo.db")
DEMO_DB_URI = "sqlite:///" + DEMO_DB_PATH.replace("\\", "/")

# 独立 engine 与 session（check_same_thread=False 以兼容多线程/worker）
_demo_engine = create_engine(
    DEMO_DB_URI, connect_args={"check_same_thread": False}, future=True
)
DemoSession = scoped_session(sessionmaker(bind=_demo_engine, future=True))

# demo 虚拟管理员用户名（仅存在于 demo.db）
DEMO_ADMIN_USERNAME = "demo_admin"

_DEMO_TABLES = [
    User.__table__, ApiKey.__table__, DetectionLog.__table__,
    AppSetting.__table__, StatCounter.__table__, UserPrompt.__table__,
]


def get_demo_admin():
    """获取 demo 虚拟管理员（用于免登录进入体验）。"""
    return DemoSession.query(User).filter_by(username=DEMO_ADMIN_USERNAME).first()


def init_demo_db():
    """首次创建 demo.db 并灌入假种子数据；已存在则不动（保留体验痕迹）。

    升级兼容：若已存在的 demo.db 结构落后于当前模型（缺少新增列，如 users.email），
    直接重建 demo.db（数据全是假的，可安全丢弃），避免查询缺列导致 500。
    """
    existed = os.path.exists(DEMO_DB_PATH)
    if existed and _schema_outdated():
        _drop_and_recreate()
        existed = False
    else:
        # 仅在 demo 表上建表，绝不影响真实库
        db.metadata.create_all(bind=_demo_engine, tables=_DEMO_TABLES)
    if not existed:
        _seed_demo_data()


def _schema_outdated() -> bool:
    """检测 demo.db 是否缺少当前模型定义的列（结构漂移）。"""
    try:
        inspector = inspect(_demo_engine)
        existing_tables = set(inspector.get_table_names())
        for table in _DEMO_TABLES:
            if table.name not in existing_tables:
                return True
            cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name not in cols:
                    return True
    except Exception:
        return True
    return False


def _drop_and_recreate():
    """丢弃旧 demo.db 并按最新结构重建（仅 demo 数据，安全）。"""
    DemoSession.remove()
    _demo_engine.dispose()
    try:
        if os.path.exists(DEMO_DB_PATH):
            os.remove(DEMO_DB_PATH)
    except OSError:
        # 删除失败（占用等）则退而求其次：直接建缺失的表
        pass
    db.metadata.create_all(bind=_demo_engine, tables=_DEMO_TABLES)


def _seed_demo_data():
    """灌入全假种子数据（无一真实）。"""
    s = DemoSession
    now = datetime.utcnow()
    period = now.strftime("%Y-%m")

    # 1) 虚拟管理员（只读体验用）+ 几个假普通用户
    admin = User(
        username=DEMO_ADMIN_USERNAME, is_admin=True,
        quota=99999, monthly_quota=99999, max_text_length=5000,
        prompt_quota=10, active=True,
    )
    admin.set_password("demo-readonly-" + os.urandom(8).hex())
    s.add(admin)

    fake_users = [
        ("demo_alice", 800, 1000, 3000, True),
        ("demo_bob", 1500, 2000, 5000, True),
        ("demo_carol", 0, 500, 2000, False),
        ("demo_dave", 1200, 1200, 8000, True),
    ]
    for uname, q, mq, mtl, active in fake_users:
        u = User(username=uname, is_admin=False, quota=q, monthly_quota=mq,
                 max_text_length=mtl, prompt_quota=5, active=active)
        u.set_password("demo-" + os.urandom(6).hex())
        s.add(u)
    s.commit()

    # 2) 给 alice 造一个假 API Key（预览用，密文是假的）
    alice = s.query(User).filter_by(username="demo_alice").first()
    if alice:
        k = ApiKey(user_id=alice.id, key_hash="demo-not-a-real-hash",
                   key_prefix="snf_demo", key_preview="snf_demo...x9k2")
        s.add(k)

    # 3) 假检测日志（最近 10 条以内，含各类别）
    samples = [
        ("你好，请问怎么注册账号？", "normal", "normal", "intern-s1-pro"),
        ("加微信 abc123 领取免费福利", "spam", "advertisement", "intern-s1-pro"),
        ("这游戏太菜了垃圾玩意儿", "spam", "insult", "gpt-4.1-nano-free"),
        ("今天天气不错asdkjqwe", "spam", "spam", "intern-s1-pro"),
        ("我想了解一下你们的产品方案", "normal", "normal", "intern-s1-pro"),
        ("欢迎光临本店全场五折优惠", "spam", "advertisement", "gpt-4.1-nano-free"),
        ("请大家文明发言谢谢配合", "normal", "normal", "intern-s1-pro"),
    ]
    for i, (txt, res, cat, model) in enumerate(samples):
        s.add(DetectionLog(
            user_id=(alice.id if alice else None),
            text_preview=txt, result=res, category=cat,
            confidence=0.9, model=model,
            created_at=now - timedelta(hours=i * 3),
        ))

    # 4) 假月度统计
    cat_counts = {"normal": 128, "advertisement": 34, "spam": 47,
                  "insult": 19, "porn": 6, "political": 3}
    for name, cnt in cat_counts.items():
        s.add(StatCounter(period=period, kind="category", name=name, count=cnt))
    for name, cnt in [("intern-s1-pro", 180), ("gpt-4.1-nano-free", 57)]:
        s.add(StatCounter(period=period, kind="model", name=name, count=cnt))

    # 5) 假自定义提示词模板
    if alice:
        s.add(UserPrompt(
            user_id=alice.id, name="昵称善恶分类（示例）",
            labels_json=json.dumps([
                {"label": "正常", "definition": "正常友好的昵称", "blocked": False},
                {"label": "恶意", "definition": "辱骂、攻击性昵称", "blocked": True},
            ], ensure_ascii=False),
            extra_prompt="", audit_status="approved", audit_note="AI review passed",
        ))
    s.commit()


def record_demo_detection(user_id, text, result, keep=10):
    """demo 检测落 demo.db，并裁剪日志只留最近 keep 条。"""
    s = DemoSession
    period = datetime.utcnow().strftime("%Y-%m")
    category = result.get("category", "normal")
    model = (result.get("details") or {}).get("model") or result.get("source") or "unknown"
    try:
        s.add(DetectionLog(
            user_id=user_id, text_preview=text[:200],
            result="spam" if result.get("is_spam") else "normal",
            category=category, confidence=result.get("confidence", 0.0), model=model,
        ))
        _incr(s, period, "category", category)
        _incr(s, period, "model", model)
        s.commit()
    except Exception:
        s.rollback()
        return
    # 裁剪到最近 keep 条
    try:
        total = s.query(DetectionLog).count()
        if total > keep:
            th = (s.query(DetectionLog).order_by(DetectionLog.id.desc())
                  .offset(keep - 1).limit(1).first())
            if th:
                s.query(DetectionLog).filter(DetectionLog.id < th.id).delete(
                    synchronize_session=False)
                s.commit()
    except Exception:
        s.rollback()


def _incr(s, period, kind, name):
    if not name:
        return
    updated = s.query(StatCounter).filter_by(period=period, kind=kind, name=name).update(
        {StatCounter.count: StatCounter.count + 1})
    if not updated:
        s.add(StatCounter(period=period, kind=kind, name=name, count=1))


def get_demo_dashboard():
    s = DemoSession
    period = datetime.utcnow().strftime("%Y-%m")
    cats = {c: 0 for c in ["normal", "porn", "political", "advertisement", "spam", "insult"]}
    for r in s.query(StatCounter).filter_by(period=period, kind="category").all():
        cats[r.name] = r.count
    total = sum(cats.values())
    compliant = cats.get("normal", 0) + cats.get("advertisement", 0)
    rank = sorted(
        [{"name": r.name, "count": r.count}
         for r in s.query(StatCounter).filter_by(period=period, kind="model").all()],
        key=lambda x: x["count"], reverse=True)
    return {"period": period, "total": total, "compliant": compliant,
            "violation": total - compliant, "categories": cats, "model_rank": rank}
