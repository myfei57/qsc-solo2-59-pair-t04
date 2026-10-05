"""配不齐时的缺口策略。

两种方向在"交付节奏"和"组内一致性"上的后果恰好相反，必须由调用方显式选定，
不允许存在"既想按时交货又不牺牲一致性"的默认含糊。

``HOLD_NEXT_BATCH``（宁缺毋滥，默认推荐）
    只交出全部合格的组，配不齐的零头留在待配池等下一批。一致性有保证、不返工，
    代价是本批少交整组、交付节奏顺延，且离群单体要在池子里积压。

``FILL_SUBOPTIMAL``（次优补位）
    合格组优先配满，再用最接近的次优单体把组凑出来，让本批组数尽量交足。交付
    节奏不掉，代价是补位组一致性下降、带豁免标记出厂，后续更可能被现场均衡或
    返工点名；单体硬限（规格/安全红线）即使在补位模式下也不放行。
"""

from __future__ import annotations

from enum import Enum


class GapPolicy(str, Enum):
    """缺口处理方向。"""

    HOLD_NEXT_BATCH = "hold_next_batch"
    FILL_SUBOPTIMAL = "fill_suboptimal"

    @classmethod
    def resolve(cls, value: "GapPolicy | str") -> "GapPolicy":
        """把字符串或枚举归一化成策略，非法值直接拒绝。"""

        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError as error:
            raise ValueError(f"未知缺口策略: {value!r}，可选 hold_next_batch / fill_suboptimal") from error

    @property
    def allows_suboptimal_fill(self) -> bool:
        """是否允许用次优单体补位。"""

        return self is GapPolicy.FILL_SUBOPTIMAL

    @property
    def consequence(self) -> str:
        """该策略在交付节奏与一致性上的代价说明。"""

        return _CONSEQUENCES[self]


_CONSEQUENCES: dict[GapPolicy, str] = {
    GapPolicy.HOLD_NEXT_BATCH: (
        "本批只交全部合格组，配不齐的零头留在待配池等下一批；组内一致性有保证、"
        "不返工，代价是少交整组、交付节奏顺延，离群单体持续积压。"
    ),
    GapPolicy.FILL_SUBOPTIMAL: (
        "合格组优先配满后用次优单体补位凑组，本批组数尽量交足、节奏不掉；"
        "代价是补位组一致性下降并带豁免出厂，现场均衡压力与返工概率上升，"
        "单体硬限（规格/安全红线）仍不放行。"
    ),
}
