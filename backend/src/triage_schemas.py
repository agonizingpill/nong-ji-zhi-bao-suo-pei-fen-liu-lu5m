from pydantic import BaseModel,Field,model_validator
from .constants import PARTS

class PartCap(BaseModel):
    miss_max:float=Field(ge=0,le=1,allow_inf_nan=False)
    teardown_max:float=Field(ge=0,le=1,allow_inf_nan=False)

class LimitIn(BaseModel):
    caps:dict[str,PartCap]
    @model_validator(mode='after')
    def every_part(self):
        if set(self.caps)!=set(PARTS):raise ValueError(f'必须分别为 {", ".join(PARTS)} 设定上限')
        for part,cap in self.caps.items():
            if cap.miss_max>cap.teardown_max:raise ValueError(f'{part} 的漏判率上限不能高于完整拆检率上限')
        return self
