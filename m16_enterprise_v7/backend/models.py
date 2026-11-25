from pathlib import Path
from sqlalchemy.engine import make_url
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, Float, String, Boolean, DateTime, Text, JSON, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
import os

DATABASE_URL = make_url(os.getenv("DATABASE_URL", "sqlite:///./data/m16_fab_v7.db"))
if DATABASE_URL.drivername == "postgresql":
    DATABASE_URL = DATABASE_URL.set(drivername="postgresql+psycopg")
if DATABASE_URL.get_backend_name() == "sqlite" and DATABASE_URL.database not in (None, ":memory:"):
    Path(DATABASE_URL.database).parent.mkdir(parents=True, exist_ok=True)
connect_args = {"check_same_thread": False} if DATABASE_URL.get_backend_name() == "sqlite" else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class SimulationTickLog(Base):
    __tablename__ = "simulation_tick_logs"
    id = Column(Integer, primary_key=True, index=True)
    run_mode = Column(String(32), default="LIVE")  # LIVE, BATCH, AB_BASELINE, AB_AI
    tick = Column(Integer, index=True)
    eds_yield_pct = Column(Float)
    oee_pct = Column(Float)
    availability_pct = Column(Float)
    power_kw = Column(Float)
    unit_cost_usd = Column(Float)
    wip_foup_count = Column(Integer)
    completed_wafers = Column(Integer)
    q_time_violations = Column(Integer, default=0)
    oht_congestion_pct = Column(Float)
    oht_avg_delivery_sec = Column(Float)
    tools_snapshot = Column(JSON)
    created_at = Column(DateTime, default=utcnow)


class AiCouncilDecisionLog(Base):
    __tablename__ = "ai_council_decision_logs"
    id = Column(Integer, primary_key=True, index=True)
    tick = Column(Integer, index=True)
    trigger_reason = Column(Text)
    control_mode = Column(String(32))  # AUTO_PILOT or HITL
    gpt_opinion = Column(Text)
    gemini_opinion = Column(Text)
    agreed_action = Column(JSON)
    applied = Column(Boolean, default=False)
    approved_by = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=utcnow)


class AuditTrailLog(Base):
    __tablename__ = "audit_trail_logs"
    id = Column(Integer, primary_key=True, index=True)
    actor_id = Column(String(64), index=True)
    actor_role = Column(String(64))
    action_type = Column(String(64))  # PARAM_TUNE, PM_EXEC, BM_INJECT, AI_APPROVE, MODE_SWITCH
    target_tool = Column(String(64))
    detail = Column(Text)
    allowed = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)
