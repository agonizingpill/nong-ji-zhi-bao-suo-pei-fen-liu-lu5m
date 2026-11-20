from datetime import datetime,timezone
from fastapi import HTTPException
from sqlalchemy import select,update
from .triage_models import TriageLimit,TriageModel,ClaimTriage
from .models import Claim,Machine
from .constants import DEFAULT_CAPS,PARTS
from .risk_model import build_line,recheck,claim_row,TriageLine
from .claim_service import audit

def now():return datetime.now(timezone.utc).isoformat()

def caps_view(row):
    return {p:{'miss_max':row.caps[p]['miss_max'],'teardown_max':row.caps[p]['teardown_max']} for p in PARTS}

def limit_view(row):
    return {c.name:getattr(row,c.name)for c in row.__table__.columns}|{'caps':caps_view(row),'source':'版本快照' if row.note=='seed' else '主管设定'}

def current_limit(s):
    row=s.scalar(select(TriageLimit).order_by(TriageLimit.version.desc()))
    if not row:raise HTTPException(409,'尚未设定分部件拆检限额')
    return row

def ensure_default_limit(s):
    if s.scalar(select(TriageLimit.id)):return
    s.add(TriageLimit(version=1,caps=DEFAULT_CAPS,created_by='system@gengchuan.local',created_at=now(),note='seed'));s.flush()

def set_limit(s,caps,u):
    ensure_default_limit(s)
    previous=current_limit(s)
    row=TriageLimit(version=previous.version+1,caps={p:{'miss_max':caps[p]['miss_max'],'teardown_max':caps[p]['teardown_max']} for p in PARTS},created_by=u['email'],created_at=now(),note='manual')
    s.add(row);s.flush()
    audit(s,u,'triage_limit_updated',f'v{row.version}:'+','.join(f"{p}>{caps[p]['miss_max']:.2f}/{caps[p]['teardown_max']:.2f}" for p in PARTS))
    withdrawn=[]
    for model in s.scalars(select(TriageModel).where(TriageModel.adopted==True)).all():
        if 'selection_evidence' not in (model.samples or {}):continue
        outcome=recheck(model.samples,model.threshold,caps_view(row))
        model.metrics=model.metrics|{'recheck':outcome,'recheck_limit_version':row.version}
        if not outcome['adopted']:
            model.adopted=False;model.state='rejected'
            if model.active:model.active=False
            withdrawn.append(model.id)
            audit(s,u,'triage_model_withdrawn',f'#{model.id}:新限额 v{row.version} 下选线或验线不再达标，已停用并转人工')
    return limit_view(row)|{'withdrawn_models':withdrawn,'affected_claims_keep_snapshot':True}

def model_view(s,row,brief=False):
    d={c.name:getattr(row,c.name)for c in row.__table__.columns}
    d['limit_version']=s.get(TriageLimit,row.limit_version_id).version if row.limit_version_id else None
    d['triage_count']=s.query(ClaimTriage).filter(ClaimTriage.model_id==row.id).count()
    if brief:d.pop('samples',None)
    return d

def train(s,u):
    history=s.scalars(select(Claim).where(Claim.historical==True).order_by(Claim.id)).all()
    machines={m.id:m for m in s.scalars(select(Machine)).all()}
    limit=current_limit(s)
    result=build_line(history,machines,caps_view(limit))
    model=TriageModel(state='adopted' if result['adopted'] else 'rejected',active=False,limit_version_id=limit.id,threshold=result['threshold'],adopted=result['adopted'],metrics=result['metrics'],samples=result['samples'],artifact=result['artifact'],created_by=u['email'],created_at=now(),note='选线与验线均达标才可采用' if result['adopted'] else '选线或验线未达标，转人工')
    s.add(model);s.flush()
    audit(s,u,'triage_model_trained',f'#{model.id}:{"adopted" if model.adopted else "rejected"}:threshold={model.threshold}:limit=v{limit.version}')
    return model_view(s,model)

def activate(s,id,u):
    row=s.get(TriageModel,id)
    if not row:raise HTTPException(404,'分流线不存在')
    if not row.adopted:raise HTTPException(409,'该分流线两项指标未全部达标，不能采用')
    s.execute(update(TriageModel).where(TriageModel.active==True).values(active=False))
    row.active=True
    audit(s,u,'triage_model_activated',f'#{row.id}:threshold={row.threshold}:v{row.limit_version_id}')
    return model_view(s,row)

def deactivate(s,id,u):
    row=s.get(TriageModel,id)
    if not row:raise HTTPException(404,'分流线不存在')
    if row.active:row.active=False;audit(s,u,'triage_model_deactivated',f'#{row.id}')
    return model_view(s,row)

def triage_view(s,x):
    claim=s.get(Claim,x.claim_id)
    d={c.name:getattr(x,c.name)for c in x.__table__.columns}
    d['claim_code']=claim.code
    d['part']=claim.part
    d['limit_version']=s.get(TriageLimit,x.limit_version_id).version
    d['limit_caps']=caps_view(s.get(TriageLimit,x.limit_version_id))
    return d

def record_view(s,x,brief=False):
    claim=s.get(Claim,x.claim_id)
    limit=s.get(TriageLimit,x.limit_version_id)
    d=triage_view(s,x)|{'limit_created_at':limit.created_at,'limit_created_by':limit.created_by}
    if x.model_id:
        model=s.get(TriageModel,x.model_id)
        d['model']={'id':model.id,'threshold':model.threshold,'adopted':model.adopted,'active':model.active,'state':model.state,'created_at':model.created_at,'limit_version':s.get(TriageLimit,model.limit_version_id).version,'sample_counts':model.metrics.get('sample_counts'),'selection':model.metrics.get('selection'),'verification':model.metrics.get('verification')}
        if not brief:d['model']['samples']=model.samples
    else:
        d['model']=None
    return d

def triageable(s,c):
    if c.historical:raise HTTPException(409,'历史已结案索赔不能重新分流')
    if c.status not in ['submitted','failed']:raise HTTPException(409,'当前索赔状态不可分流；计算中请等待或使用重试')

def record_intake(s,c):
    """受理索赔时先固定限额版本；之后主管改限额也不影响这一单。"""
    ensure_default_limit(s)
    limit=current_limit(s)
    if s.scalar(select(ClaimTriage.id).where(ClaimTriage.claim_id==c.id)):return
    s.add(ClaimTriage(claim_id=c.id,model_id=None,limit_version_id=limit.id,state='intake',route='pending',requirements={},reason='等待分流',attempts=0))
    s.flush()

def mark_expired(s,c):
    row=s.scalar(select(ClaimTriage).where(ClaimTriage.claim_id==c.id).with_for_update())
    if not row:return
    row.state='done';row.route='expired';row.evaluated_at=now();row.reason='工时过保，不进入分流'
    row.requirements={'mode':'none','evidence_required':False,'reason':'工时过保，不能赔付','rule':'过保拦截先于评分'}

def snapshot(s,c,u):
    """首次分流固定分流线与受理时限额；已存在则沿用，重试不改换。"""
    existing=s.scalar(select(ClaimTriage).where(ClaimTriage.claim_id==c.id).with_for_update())
    if existing:
        if existing.state=='done':raise HTTPException(409,'该索赔已完成分流，不能重复评分')
        if existing.state=='pending':return existing
    limit=s.get(TriageLimit,existing.limit_version_id) if existing else current_limit(s)
    model=s.scalar(select(TriageModel).where(TriageModel.active==True))
    route_ok=bool(model and model.adopted)
    if not model:manual_reason='当前无已采用的分流线'
    elif not model.adopted:manual_reason='当前分流线两项指标未全部达标'
    else:manual_reason=''
    fields={'model_id':model.id if route_ok else None,'state':'pending' if route_ok else 'done','route':'line' if route_ok else 'manual','requirements':{'mode':'full','evidence_required':True,'limit_caps':caps_view(limit),'threshold':model.threshold if route_ok else None,'reason':'分流线评分后确定拆检要求' if route_ok else f'转人工：{manual_reason}','rule':'概率、分流线与拆检要求一起生效；签赔按该次要求放行'},'reason':'' if route_ok else manual_reason,'attempts':0}
    if existing:
        for k,v in fields.items():setattr(existing,k,v)
        if existing.state=='done':existing.evaluated_at=now()
        row=existing
    else:
        row=ClaimTriage(claim_id=c.id,limit_version_id=limit.id,**fields)
        if not route_ok:row.evaluated_at=now()
        s.add(row)
    s.flush()
    audit(s,u,'triage_snapshotted',f'{c.code}:{"line" if route_ok else "manual"}:limit=v{limit.version}')
    return row

def queue(s,c,u):
    triageable(s,c)
    m=s.get(Machine,c.machine_id)
    if c.hours_at_failure>m.warranty_hours:
        c.status='out_of_warranty';c.assessment_note='超出质保工时'
        mark_expired(s,c)
        audit(s,u,'warranty_rejected',c.code);return None
    row=snapshot(s,c,u)
    if row.route=='manual':
        c.status='manual';c.assessment_note=row.requirements.get('reason',row.reason);audit(s,u,'triage_manual',c.code);return row
    c.status='queued';audit(s,u,'risk_queued',c.code);return row

def evaluate(s,claim_id):
    """worker 调用：使用首次快照的分流线与限额版本计算；失败只标记重试次数，不清空快照。"""
    claim=s.scalar(select(Claim).where(Claim.id==claim_id).with_for_update())
    if not claim:return
    row=s.scalar(select(ClaimTriage).where(ClaimTriage.claim_id==claim_id).with_for_update())
    if not row:
        if claim.status!='queued':return
        raise ValueError('分流快照缺失，拒绝评分')
    if row.state=='intake':
        if claim.status!='queued':return
        raise ValueError('分流快照未建立，拒绝评分')
    row.attempts+=1
    if claim.status!='queued':return
    if row.route=='manual' or not row.model_id:
        row.state='done';claim.status='manual'
        row.requirements=(row.requirements or {})|{'mode':'full','evidence_required':True,'reason':f'转人工：{row.reason or "分流线未达标"}，签赔前须补齐拆检证据'}
        claim.assessment_note=row.requirements['reason'];return
    model=s.get(TriageModel,row.model_id)
    limit=s.get(TriageLimit,row.limit_version_id)
    if not model:
        claim.status='failed';claim.assessment_note='原分流线已不可用，重试不能更换分流线，请转人工';return
    machine=s.get(Machine,claim.machine_id)
    line=TriageLine.from_dict(model.artifact)
    p=float(line.probability(claim_row(claim,machine)))
    threshold=float(model.threshold)
    caps=caps_view(limit)
    reason=''
    if claim.hours_at_failure>machine.warranty_hours:
        claim.status='out_of_warranty';claim.assessment_note='超出质保工时';row.state='done';return
    if p>=threshold:
        requirements={'mode':'full','evidence_required':True,'reason':'漏判风险高于分流线阈值，须完整拆检'}
    else:
        cap=caps[claim.part]
        if cap['miss_max']<=0:
            reason='该部件漏判上限为零，低于阈值的索赔也须完整拆检'
            requirements={'mode':'full','evidence_required':True,'reason':reason}
        else:
            requirements={'mode':'sample','evidence_required':False,'reason':f'低于阈值且部件漏判上限 {cap["miss_max"]:.0%}，按抽样拆检'}
    claim.risk=p
    claim.status='assessed'
    claim.assessment_note=requirements['reason'] or '已按分流线评分'
    row.state='done';row.probability=p;row.threshold=threshold
    row.requirements=requirements|{'limit_caps':caps,'threshold':threshold,'model_id':model.id}
    row.reason=reason
    row.evaluated_at=now()

def note_failure(s,claim_id,message):
    """evaluate 抛错时其事务已回滚，这里补记这次尝试次数与失败原因，快照本身不改。"""
    row=s.scalar(select(ClaimTriage).where(ClaimTriage.claim_id==claim_id).with_for_update())
    if not row:return 0
    row.attempts+=1
    row.reason=f'计算失败：{message}'
    return row.attempts

def retry(s,claim_id,u):
    claim=s.get(Claim,claim_id)
    if not claim:raise HTTPException(404,'索赔不存在')
    if claim.status not in ['failed','queued']:raise HTTPException(409,'仅计算中或计算失败的索赔可重试')
    row=s.scalar(select(ClaimTriage).where(ClaimTriage.claim_id==claim_id).with_for_update())
    if not row:raise HTTPException(409,'该索赔没有分流快照，不能重试')
    if row.route=='pending':row=snapshot(s,claim,u)
    if row.route=='manual':
        claim.status='manual';claim.assessment_note=row.requirements.get('reason',row.reason);audit(s,u,'triage_manual_retry',claim.code);return row
    if not row.model_id or not s.get(TriageModel,row.model_id):
        raise HTTPException(409,'原分流线已不可用；重试不得更换分流线或样本，请转人工处理')
    claim.status='queued';audit(s,u,'triage_retried',f'{claim.code}:model#{row.model_id}:limit=v{s.get(TriageLimit,row.limit_version_id).version}')
    return row

def claim_requirements(s,c):
    if c.status=='out_of_warranty':return {'mode':'none','evidence_required':False,'reason':'工时过保，不能赔付'}
    if c.status not in ['assessed','manual']:return {'mode':'none','evidence_required':False,'reason':'尚未完成分流'}
    row=s.scalar(select(ClaimTriage).where(ClaimTriage.claim_id==c.id))
    if not row:return {'mode':'full','evidence_required':True,'reason':'缺少分流快照，按最严证据要求处理'}
    if row.requirements:return row.requirements
    return {'mode':'full','evidence_required':True,'reason':'转人工：分流线未达标，签赔前须补齐拆检证据'}
