ROLES = ('admin','assessor','viewer')
CLAIM_STATES = ('submitted','queued','assessed','manual','out_of_warranty','paid','denied','failed')
PARTS = ('hydraulic','engine','transmission')

RETURN_STATES=('dispatched','received','verified')
SETTLEMENT_STATES=('draft','approved','booked')

REPAIR_STATES=('reserved','running','completed')
ISSUE_STATES=('issued','returned','consumed')
FOLLOWUP_STATES=('review','rework','closed')

# 主管可按部件调整；液压件漏判代价高，默认给更紧的漏判上限。
DEFAULT_CAPS={
    'hydraulic':{'miss_max':.15,'teardown_max':.80},
    'engine':{'miss_max':.25,'teardown_max':.50},
    'transmission':{'miss_max':.40,'teardown_max':.50},
}
TRIAGE_ROUTES=('line','manual')
