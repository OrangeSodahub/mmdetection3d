_base_ = [
    '../_base_/models/hv_pointpillars_secfpn_once.py',
    '../_base_/datasets/once-3d-5class.py',
    '../_base_/schedules/cyclic_80e.py',
    '../_base_/default_runtime.py',
]