"""基于 Wang et al. (2026), Minerals 16(11), 1109 Table 2 的11项MARS简化公式。
DOI 10.3390/min16111109。
输入必须与论文的特征工程和单位一致；示例输入为假设，不代表金徽真实DCS。
预测功率 != 因果节能量；不能将模型差值直接当作已实现减排。
"""
from dataclasses import dataclass

def hinge(x):return max(0.0, x)

@dataclass(frozen=True)
class MillInputs:
    bond_e_sag: float
    j_sag: float
    j_bm: float
    x8_mean30: float
    x8_mean10: float
    p_hogg_bm: float
    x6_lag30: float
    p_hogg_sag: float
    x1_mean30: float
    x6_mean30: float


def mill_power_kw(v: MillInputs):
    # 11 terms: reference + 10 coefficient-weighted basis functions
    z=2148.01
    z+= -276672.1*hinge(v.bond_e_sag-.12)*hinge(v.j_sag-.521)
    z+= -390.4*hinge(v.j_bm-.399)*hinge(v.j_sag-.521)
    z+= 35.425*hinge(v.x8_mean30-6.64)
    z+= 26.114*hinge(v.x8_mean10-6.67)
    z+= 1.955*hinge(v.p_hogg_bm-297)
    z+= .4728*hinge(149-v.x6_lag30)
    z+= .3572*hinge(v.p_hogg_sag-221)
    z+= .2562*hinge(266-v.x1_mean30)
    z+= .1737*hinge(148-v.x6_mean30)
    z+= -.1730*hinge(v.x1_mean30-210)
    if not (2100<=z<=2380):
        raise ValueError('Computed prediction outside paper screening window 2100-2380 kW; require domain check')
    return z

# Only an assumed input record to demonstrate code execution
EXAMPLE = MillInputs(bond_e_sag=.10,j_sag=.50,j_bm=.38,x8_mean30=6.8,
    x8_mean10=6.8,p_hogg_bm=302,x6_lag30=146,p_hogg_sag=230,
    x1_mean30=240,x6_mean30=145)
