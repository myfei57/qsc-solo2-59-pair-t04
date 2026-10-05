"""组套台账：单体 → 组 → 组串 → 簇 的登记与沿革。

登记只增不改：每组挂到一条组串与一簇下，每次登记或拆下都按代际写一条历史。
一只单体同一时刻只能挂在一条组里，重配必须先从原组拆下再登记进新组；拆下
不抹历史，原组套关系按代际可查——"这只单体原来在哪组、现在挂在哪"双向都
能追到，且结构上不可能出现一只单体同时挂两组。
"""

from __future__ import annotations

from ..errors import NamespaceError


class GroupRegistry:
    """单体与组、组串、簇之间组套关系的台账。"""

    def __init__(self) -> None:
        self._cell_to_group: dict[str, str] = {}
        self._groups: dict[str, dict] = {}
        self._history: list[dict] = []
        self._epoch = 0

    def register(self, group_id: str, pack_id: str, rack_id: str, cell_ids: list[str]) -> dict:
        """把一组单体登记到组串与簇下，返回本次变更记录。"""

        if not group_id or not pack_id or not rack_id:
            raise NamespaceError("组标识、组串编号与簇标识都不能为空", component="matching")
        cells = [str(cell_id) for cell_id in cell_ids]
        if not cells:
            raise NamespaceError("组内至少要有一只单体", component="matching")
        if len(set(cells)) != len(cells):
            raise NamespaceError("组内单体编号重复", component="matching")
        if group_id in self._groups:
            raise NamespaceError(f"组标识已登记: {group_id}", component="matching")
        for cell_id in cells:
            if cell_id in self._cell_to_group:
                raise NamespaceError(
                    f"单体 {cell_id} 已挂在组 {self._cell_to_group[cell_id]}，不能同时挂两组",
                    component="matching",
                )
        self._epoch += 1
        self._groups[group_id] = {
            "group_id": group_id,
            "pack_id": pack_id,
            "rack_id": rack_id,
            "cells": list(cells),
            "epoch": self._epoch,
        }
        for cell_id in cells:
            self._cell_to_group[cell_id] = group_id
        change = {
            "epoch": self._epoch,
            "action": "register",
            "group_id": group_id,
            "pack_id": pack_id,
            "rack_id": rack_id,
            "cells": list(cells),
        }
        self._history.append(change)
        return dict(change)

    def detach(self, cell_id: str, reason: str = "manual") -> dict:
        """把单体从当前组拆下，原组套关系留进历史，返回变更记录。"""

        group_id = self._cell_to_group.get(cell_id)
        if group_id is None:
            raise NamespaceError(f"单体未在册: {cell_id}", component="matching")
        group = self._groups[group_id]
        group["cells"] = [item for item in group["cells"] if item != cell_id]
        del self._cell_to_group[cell_id]
        self._epoch += 1
        change = {
            "epoch": self._epoch,
            "action": "detach",
            "cell_id": cell_id,
            "group_id": group_id,
            "pack_id": group["pack_id"],
            "rack_id": group["rack_id"],
            "reason": reason,
        }
        self._history.append(change)
        return dict(change)

    def group_of(self, cell_id: str) -> dict:
        """返回单体当前所在的组、组串与簇。"""

        group_id = self._cell_to_group.get(cell_id)
        if group_id is None:
            raise NamespaceError(f"单体未在册: {cell_id}", component="matching")
        group = self._groups[group_id]
        return {
            "cell_id": cell_id,
            "group_id": group_id,
            "pack_id": group["pack_id"],
            "rack_id": group["rack_id"],
        }

    def members(self, group_id: str) -> list[str]:
        """返回某组当前在册的单体。"""

        group = self._groups.get(group_id)
        if group is None:
            raise NamespaceError(f"未登记的组: {group_id}", component="matching")
        return list(group["cells"])

    def history_of(self, cell_id: str) -> list[dict]:
        """返回一只单体的全部组套沿革（登记与拆下）。"""

        return [
            dict(change)
            for change in self._history
            if change.get("cell_id") == cell_id or cell_id in change.get("cells", [])
        ]

    def snapshot(self) -> dict:
        """返回当前台账：代际、各组在册单体与单体→组索引。"""

        return {
            "epoch": self._epoch,
            "groups": {
                group_id: {**group, "cells": list(group["cells"])}
                for group_id, group in sorted(self._groups.items())
            },
            "cells": {cell_id: self._cell_to_group[cell_id] for cell_id in sorted(self._cell_to_group)},
        }
