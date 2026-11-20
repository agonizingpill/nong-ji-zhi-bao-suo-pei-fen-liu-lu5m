import os
from fastapi import APIRouter,Depends
from redis import Redis
from sqlalchemy import select
from .database import Session
from .security import current,allow
from .models import Claim
from .triage_models import TriageLimit,TriageModel,ClaimTriage
from .triage_schemas import LimitIn
from . import triage_service as triage
from .claim_service import obj,get,claim_view
router=APIRouter()
r=Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)

@router.get('/triage-limits')
def limits(u=Depends(current)):
    with Session.begin() as s:
        triage.ensure_default_limit(s)
        rows=s.scalars(select(TriageLimit).order_by(TriageLimit.version.desc())).all()
        return {'current':triage.limit_view(rows[0]),'versions':[triage.limit_view(x) for x in rows]}

@router.post('/triage-limits')
def set_limits(d:LimitIn,u=Depends(current)):
    allow(u,'admin')
    with Session.begin() as s:
        return triage.set_limit(s,{k:v.model_dump() for k,v in d.caps.items()},u)

@router.get('/triage-models')
def models(u=Depends(current)):
    with Session() as s:
        active=s.scalar(select(TriageModel).where(TriageModel.active==True))
        rows=s.scalars(select(TriageModel).order_by(TriageModel.id.desc())).all()
        return {'active_id':active.id if active else None,'items':[triage.model_view(s,x,brief=True) for x in rows]}

@router.post('/triage-models')
def train_models(u=Depends(current)):
    allow(u,'admin')
    with Session.begin() as s:
        return triage.train(s,u)

@router.post('/triage-models/{id}/activate')
def activate(id:int,u=Depends(current)):
    allow(u,'admin')
    with Session.begin() as s:
        return triage.activate(s,id,u)

@router.post('/triage-models/{id}/deactivate')
def deactivate(id:int,u=Depends(current)):
    allow(u,'admin')
    with Session.begin() as s:
        return triage.deactivate(s,id,u)

@router.get('/triage-records')
def records(u=Depends(current)):
    with Session() as s:
        return [triage.record_view(s,x,brief=True) for x in s.scalars(select(ClaimTriage).order_by(ClaimTriage.id.desc()))]

@router.get('/claims/{id}/triage')
def claim_score(id:int,u=Depends(current)):
    with Session() as s:
        c=get(s,Claim,id)
        row=s.scalar(select(ClaimTriage).where(ClaimTriage.claim_id==id))
        return {'claim':obj(c),'triage':triage.record_view(s,row) if row else None,'requirements':triage.claim_requirements(s,c)}

@router.post('/claims/{id}/assess')
def assess(id:int,u=Depends(current)):
    allow(u,'admin','assessor')
    with Session.begin() as s:
        c=get(s,Claim,id)
        row=triage.queue(s,c,u)
        result=claim_view(s,c)
    if c.status=='queued':r.rpush('claim-risk',id)
    return result

@router.post('/claims/{id}/assess/retry')
def retry(id:int,u=Depends(current)):
    allow(u,'admin','assessor')
    with Session.begin() as s:
        row=triage.retry(s,id,u)
        c=get(s,Claim,id)
        queued=c.status=='queued'
        result=claim_view(s,c)
    if queued:r.rpush('claim-risk',id)
    return result
