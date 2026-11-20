import math
import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder,StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score,roc_auc_score

PARTS=('hydraulic','engine','transmission')
SEED=42
METHOD='OneHotEncoder + StandardScaler + LogisticRegression'
THRESHOLDS=[round(i/100,2) for i in range(5,96)]
TRAIN_RATIO=.6
SELECT_RATIO=.2
VERIFY_RATIO=.2

def claim_row(claim,machine):
    return [machine.model,claim.part,claim.hours_at_failure,claim.age_days,int(claim.leak_flag)]

class TriageLine:
    """可序列化的单条分流线：编码、标准化与 LogisticRegression 系数一次固定。"""
    def __init__(self,model_categories,part_categories,mean,scale,coef,intercept):
        self.model_categories=list(model_categories)
        self.part_categories=list(part_categories)
        self.mean=[float(x) for x in mean]
        self.scale=[float(x) for x in scale]
        self.coef=[float(x) for x in coef]
        self.intercept=float(intercept)
    @classmethod
    def fit(cls,rows,labels):
        pre=ColumnTransformer([('category',OneHotEncoder(handle_unknown='ignore'),[0,1]),('numeric',StandardScaler(),[2,3,4])])
        pipe=make_pipeline(pre,LogisticRegression(max_iter=1000,random_state=SEED))
        pipe.fit(np.array(rows,dtype=object),np.array(labels))
        encoder=pipe[0].named_transformers_['category']
        scaler=pipe[0].named_transformers_['numeric']
        model=pipe[1]
        return cls(encoder.categories_[0],encoder.categories_[1],scaler.mean_,scaler.scale_,model.coef_[0],model.intercept_[0])
    def _vector(self,row):
        values=[1.0 if row[0]==c else 0.0 for c in self.model_categories]
        values+=[1.0 if row[1]==c else 0.0 for c in self.part_categories]
        values+=[(float(row[i+2])-self.mean[i])/self.scale[i] for i in range(3)]
        return values
    def probability(self,row):
        z=self.intercept+sum(w*v for w,v in zip(self.coef,self._vector(row)))
        if z>=0:return 1/(1+math.exp(-z))
        e=math.exp(z)
        return e/(1+e)
    def to_dict(self):
        return {'model_categories':self.model_categories,'part_categories':self.part_categories,'mean':self.mean,'scale':self.scale,'coef':self.coef,'intercept':self.intercept}
    @classmethod
    def from_dict(cls,data):
        return cls(data['model_categories'],data['part_categories'],data['mean'],data['scale'],data['coef'],data['intercept'])

def split_history(history):
    """训练 / 选线 / 验线三份互不重叠；按部件×标签分层，固定种子。"""
    keys=[f'{c.part}:{int(bool(c.disputed))}' for c in history]
    index=np.arange(len(history))
    train,rest=train_test_split(index,test_size=SELECT_RATIO+VERIFY_RATIO,stratify=keys,random_state=SEED)
    selection,verification=train_test_split(rest,test_size=.5,stratify=[keys[i] for i in rest],random_state=SEED)
    groups={'train':sorted(int(i) for i in train),'selection':sorted(int(i) for i in selection),'verification':sorted(int(i) for i in verification)}
    seen=set()
    for name,rows in groups.items():
        if seen&set(rows):raise ValueError('训练、选线与验线样本发生重叠，不能建立分流线')
        seen|=set(rows)
    for part in PARTS:
        for name,rows in groups.items():
            if not any(history[i].part==part for i in rows):raise ValueError(f'{name} 样本缺少 {part} 部件记录，不能建立分流线')
    return groups

def part_metrics(evidence,threshold,caps,part):
    rows=[e for e in evidence if e['part']==part]
    positives=[e for e in rows if e['label']==1]
    misses=[e for e in positives if e['probability']<threshold]
    teardowns=[e for e in rows if e['probability']>=threshold]
    miss_rate=len(misses)/len(positives) if positives else None
    teardown_rate=len(teardowns)/len(rows) if rows else None
    cap=caps[part]
    complete=miss_rate is not None and teardown_rate is not None
    passed=complete and miss_rate<=cap['miss_max']+1e-9 and teardown_rate<=cap['teardown_max']+1e-9
    return {'part':part,'rows':len(rows),'positives':len(positives),'misses':len(misses),'teardowns':len(teardowns),'miss_rate':miss_rate,'teardown_rate':teardown_rate,'miss_max':cap['miss_max'],'teardown_max':cap['teardown_max'],'passed':passed}

def evaluate(evidence,threshold,caps):
    metrics={part:part_metrics(evidence,threshold,caps,part) for part in PARTS}
    return metrics,all(m['passed'] for m in metrics.values())

def violation(metrics):
    total=0.0
    for m in metrics.values():
        if m['miss_rate'] is None:total+=1
        else:total+=max(0.0,m['miss_rate']-m['miss_max'])
        if m['teardown_rate'] is None:total+=1
        else:total+=max(0.0,m['teardown_rate']-m['teardown_max'])
    return round(total,6)

def choose_threshold(evidence,caps):
    """在选线样本上取“离全部部件两项上限余量最大”的阈值；无可行线时取越界最少者并标记不可采用。"""
    best_feasible=None
    for threshold in sorted(THRESHOLDS,reverse=True):
        metrics,passed=evaluate(evidence,threshold,caps)
        if not passed:continue
        slack=min(min(m['miss_max']-m['miss_rate'],m['teardown_max']-m['teardown_rate']) for m in metrics.values())
        if best_feasible is None or (slack,threshold)>(best_feasible[0],best_feasible[1]):best_feasible=(slack,threshold,metrics)
    if best_feasible:return best_feasible[1],best_feasible[2],True
    best=None
    for threshold in sorted(THRESHOLDS,reverse=True):
        metrics,_=evaluate(evidence,threshold,caps)
        key=(violation(metrics),-threshold)
        if best is None or key<best[0]:best=(key,threshold,metrics)
    _,threshold,metrics=best
    return threshold,metrics,False

def holdout_quality(evidence,threshold):
    """验线样本上的描述性指标；不是真实业务校准，仅供审阅。"""
    y=[e['label'] for e in evidence]
    p=[e['probability'] for e in evidence]
    metrics={'rows':len(y),'accuracy':None,'roc_auc':None,'high_risk_rows':sum(1 for x in p if x>=threshold)}
    if len(set(y))>1:
        metrics['roc_auc']=round(float(roc_auc_score(y,p)),4)
        metrics['accuracy']=round(float(accuracy_score(y,[1 if x>=threshold else 0 for x in p])),4)
    return metrics

def build_line(history,machines,caps):
    if len(history)<40:raise ValueError('已结案历史不足40条，不能训练分流线')
    groups=split_history(history)
    rows=[claim_row(c,machines[c.machine_id]) for c in history]
    labels=[int(bool(c.disputed)) for c in history]
    line=TriageLine.fit([rows[i] for i in groups['train']],[labels[i] for i in groups['train']])
    probabilities=[line.probability(row) for row in rows]
    def evidence(name):
        return [{'code':history[i].code,'part':history[i].part,'label':labels[i],'probability':round(probabilities[i],10)} for i in groups[name]]
    selection_evidence=evidence('selection')
    verification_evidence=evidence('verification')
    threshold,selection_metrics,selection_passed=choose_threshold(selection_evidence,caps)
    verification_metrics,verification_passed=evaluate(verification_evidence,threshold,caps)
    adopted=selection_passed and verification_passed
    metrics={'threshold':threshold,'selection':selection_metrics,'verification':verification_metrics,'selection_passed':selection_passed,'verification_passed':verification_passed,'adopted':adopted,'method':METHOD,'seed':SEED,'thresholds_tested':len(THRESHOLDS),'sample_counts':{k:len(v) for k,v in groups.items()},'samples_disjoint':True,'ratios':{'train':TRAIN_RATIO,'selection':SELECT_RATIO,'verification':VERIFY_RATIO},'holdout_quality':holdout_quality(verification_evidence,threshold)}
    samples={k:[history[i].code for i in rows] for k,rows in groups.items()}|{'selection_evidence':selection_evidence,'verification_evidence':verification_evidence}
    return {'threshold':threshold,'adopted':adopted,'metrics':metrics,'samples':samples,'artifact':line.to_dict()}

def recheck(samples,threshold,caps):
    """用已固定的选线/验线评分复核新限额；不重新训练、不改变样本划分。"""
    selection_metrics,selection_passed=evaluate(samples['selection_evidence'],threshold,caps)
    verification_metrics,verification_passed=evaluate(samples['verification_evidence'],threshold,caps)
    return {'threshold':threshold,'selection':selection_metrics,'verification':verification_metrics,'selection_passed':selection_passed,'verification_passed':verification_passed,'adopted':selection_passed and verification_passed}
