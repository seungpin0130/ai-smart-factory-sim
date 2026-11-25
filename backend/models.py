import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, JSON
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./m16_fab.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class SimulationRun(Base):
    """시뮬레이션 실행 세션 이력 테이블"""
    __tablename__ = "simulation_runs"

    id = Column(Integer, primary_key=True, index=True)
    run_code = Column(String(64), unique=True, index=True, nullable=False)
    name = Column(String(128), nullable=False)
    operator_id = Column(String(64), default="M16-ENG-2048")
    total_ticks = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    telemetries = relationship("TickTelemetry", back_populates="run", cascade="all, delete-orphan")


class TickTelemetry(Base):
    """틱별 설비 상태 및 공정 KPI 시계열 로그 테이블"""
    __tablename__ = "tick_telemetry"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("simulation_runs.id"), nullable=False)
    tick = Column(Integer, index=True, nullable=False)
    timestamp_label = Column(String(64), nullable=False)

    # KPI
    eds_yield = Column(Float, nullable=False)
    power_kw = Column(Float, nullable=False)
    unit_cost = Column(Float, nullable=False)

    # 설비별 상태 및 WIP JSON 저장
    wip_buffers = Column(JSON, nullable=False)
    equipments = Column(JSON, nullable=False)

    # AI 의사결정 결과
    trigger_reason = Column(String(128), nullable=True)
    debate = Column(JSON, nullable=True)
    final_action = Column(JSON, nullable=True)

    run = relationship("SimulationRun", back_populates="telemetries")


class AlarmAuditLog(Base):
    """설비 알람 및 PM/BM 정비 감사 로그 테이블"""
    __tablename__ = "alarm_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    alarm_code = Column(String(32), unique=True, index=True, nullable=False)
    tick = Column(Integer, nullable=False)
    time_label = Column(String(32), nullable=False)
    station_id = Column(String(32), index=True, nullable=False)
    alarm_type = Column(String(24), index=True, nullable=False)  # CRITICAL, PM, TUNED
    description = Column(Text, nullable=False)
    acknowledged = Column(Boolean, default=False)
    acknowledged_by = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)