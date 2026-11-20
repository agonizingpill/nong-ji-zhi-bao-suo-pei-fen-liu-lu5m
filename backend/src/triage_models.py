from sqlalchemy import String,Integer,Float,Boolean,ForeignKey,CheckConstraint,JSON,Text
from sqlalchemy.orm import Mapped,mapped_column
from .database import Base

class TriageLimit(Base):
    """主管按部件设定的漏判率与完整拆检率上限；每次修改新增版本，旧版本保留。"""
    __tablename__='triage_limits'
    __table_args__=(CheckConstraint('version > 0'),CheckConstraint("note in ('seed','manual')"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    version:Mapped[int]=mapped_column(Integer,unique=True)
    caps:Mapped[dict]=mapped_column(JSON)
    created_by:Mapped[str]=mapped_column(String(120))
    created_at:Mapped[str]=mapped_column(String(40))
    note:Mapped[str]=mapped_column(String(20),default='manual')

class TriageModel(Base):
    """一条分流线：固定样本划分、阈值、逐部件选线/验线指标和不可变模型系数。"""
    __tablename__='triage_models'
    __table_args__=(CheckConstraint("state in ('adopted','rejected')"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    state:Mapped[str]=mapped_column(String(20))
    active:Mapped[bool]=mapped_column(Boolean,default=False)
    limit_version_id:Mapped[int]=mapped_column(ForeignKey('triage_limits.id'))
    threshold:Mapped[float]=mapped_column(Float)
    adopted:Mapped[bool]=mapped_column(Boolean)
    metrics:Mapped[dict]=mapped_column(JSON)
    samples:Mapped[dict]=mapped_column(JSON)
    artifact:Mapped[dict]=mapped_column(JSON)
    created_by:Mapped[str]=mapped_column(String(120))
    created_at:Mapped[str]=mapped_column(String(40))
    note:Mapped[str]=mapped_column(Text,default='')

class ClaimTriage(Base):
    """单份索赔的分流依据；首次评分时固定分流线、样本与受理时限额，重试不更换。"""
    __tablename__='claim_triage'
    __table_args__=(CheckConstraint("route in ('pending','line','manual','expired')"),CheckConstraint("state in ('intake','pending','done')"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    claim_id:Mapped[int]=mapped_column(ForeignKey('claims.id'),unique=True)
    model_id:Mapped[int|None]=mapped_column(ForeignKey('triage_models.id'),nullable=True)
    limit_version_id:Mapped[int]=mapped_column(ForeignKey('triage_limits.id'))
    state:Mapped[str]=mapped_column(String(20),default='pending')
    route:Mapped[str]=mapped_column(String(20),default='manual')
    probability:Mapped[float|None]=mapped_column(Float,nullable=True)
    threshold:Mapped[float|None]=mapped_column(Float,nullable=True)
    requirements:Mapped[dict]=mapped_column(JSON)
    reason:Mapped[str]=mapped_column(Text,default='')
    attempts:Mapped[int]=mapped_column(Integer,default=0)
    evaluated_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
