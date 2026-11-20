import os,hmac,logging
from contextlib import asynccontextmanager
from fastapi import FastAPI,Depends,HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select,text
from sqlalchemy.exc import IntegrityError
from redis import Redis
from .database import Base,engine,Session
from .models import User,Machine,Claim,Inspection,Audit
from .security import current,allow,password_hash,token_for
from .schemas import Login,MachineIn,ClaimIn,InspectionIn,DecisionIn
from .claim_service import obj,get,audit,claim_view,submit,inspect,adjudicate
from .middlewares import RequestTrace,Recovery
from .fulfilment_routes import router as fulfilment_router
from .workshop_routes import router as workshop_router
from .triage_routes import router as triage_router
from scripts.seed_demo import seed
logging.basicConfig(level=logging.INFO)
r=Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)
@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    if os.getenv('SEED_DEMO','true').lower()=='true':
        try:seed()
        except Exception:logging.exception('seed failed')
    yield
app=FastAPI(title='耕川 · 质保田间档案',lifespan=lifespan)
app.add_middleware(RequestTrace);app.add_middleware(Recovery)
app.include_router(fulfilment_router)
app.include_router(workshop_router)
app.include_router(triage_router)
@app.exception_handler(IntegrityError)
async def duplicate(request,e):return JSONResponse({'detail':'编号已存在或关联数据不合法'},status_code=409)
@app.get('/health')
def health():
    with Session() as s:s.execute(text('SELECT 1'))
    return {'status':'ok','redis':r.ping()}
@app.post('/auth/login')
def login(data:Login):
    with Session() as s:
        u=s.scalar(select(User).where(User.email==data.email))
        if not u or not hmac.compare_digest(u.password,password_hash(data.password)):raise HTTPException(401,'账号或密码不正确')
        return {'token':token_for(u),'role':u.role,'email':u.email}
@app.get('/machines')
def machines(u=Depends(current)):
    with Session() as s:return [obj(x) for x in s.scalars(select(Machine).order_by(Machine.id))]
@app.post('/machines')
def add_machine(data:MachineIn,u=Depends(current)):
    allow(u,'admin','assessor')
    with Session.begin() as s:
        x=Machine(**data.model_dump());s.add(x);s.flush();audit(s,u,'machine_registered',x.serial);return obj(x)
@app.put('/machines/{id}')
def update_machine(id:int,data:MachineIn,u=Depends(current)):
    allow(u,'admin')
    with Session.begin() as s:
        m=get(s,Machine,id)
        if data.hours<m.hours:raise HTTPException(409,'机具累计工时不可倒退')
        for k,v in data.model_dump().items():setattr(m,k,v)
        audit(s,u,'machine_updated',m.serial);return obj(m)
@app.get('/claims')
def claims(u=Depends(current)):
    with Session() as s:return [claim_view(s,c) for c in s.scalars(select(Claim).where(Claim.historical==False).order_by(Claim.id.desc()))]
@app.post('/claims')
def add_claim(data:ClaimIn,u=Depends(current)):
    allow(u,'admin','assessor')
    with Session.begin() as s:return submit(s,data,u)
@app.put('/claims/{id}/inspection')
def save_inspection(id:int,data:InspectionIn,u=Depends(current)):
    allow(u,'admin','assessor')
    with Session.begin() as s:return inspect(s,get(s,Claim,id),data,u)
@app.post('/claims/{id}/decision')
def decide(id:int,data:DecisionIn,u=Depends(current)):
    allow(u,'admin')
    with Session.begin() as s:return adjudicate(s,get(s,Claim,id),data.decision,u)
@app.get('/audit')
def audit_log(u=Depends(current)):
    with Session() as s:return [obj(x) for x in s.scalars(select(Audit).order_by(Audit.id.desc()).limit(40))]
