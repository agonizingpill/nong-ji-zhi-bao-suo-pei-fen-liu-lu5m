import json,time,os
from pathlib import Path
from redis import Redis
from sqlalchemy import select
from .database import Session
from .models import Claim
from .triage_models import ClaimTriage
from . import triage_service as triage
r=Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)
while True:
    Path('/artifacts/worker-heartbeat').write_text(str(time.time()))
    item=r.blpop('claim-risk',timeout=2)
    if not item:continue
    cid=int(item[1])
    try:
        with Session.begin() as s:triage.evaluate(s,cid)
    except Exception as e:
        with Session.begin() as s:
            attempts=triage.note_failure(s,cid,str(e))
            c=s.get(Claim,cid)
            if c:
                c.status='failed'
                c.assessment_note=f'第 {attempts} 次分流计算失败：{e}；重试沿用原分流线与原限额，不更换样本或分流线'
        continue
    try:
        with Session() as s:
            c=s.get(Claim,cid)
            row=s.scalar(select(ClaimTriage).where(ClaimTriage.claim_id==cid))
            if c and row and row.requirements:
                Path(f'/artifacts/claim-{cid}-triage.json').write_text(json.dumps({'claim_id':cid,'model_id':row.model_id,'limit_version_id':row.limit_version_id,'probability':row.probability,'threshold':row.threshold,'route':row.route,'requirements':row.requirements,'attempts':row.attempts},ensure_ascii=False))
    except Exception:
        pass
