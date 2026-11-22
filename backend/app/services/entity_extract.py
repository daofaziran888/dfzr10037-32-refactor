"""实体抽取领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.entity_evidence import (
    EntityEvidence,
    build_entity_evidence,
    evidence_view,
)
from app.store import store


class EntityExtractService(BaseModuleService):
    MODULE = "entity_extract"
    ENTITY = "实体定义"
    OPERATION_TERM = "入库"
    LIST_FIELDS = ["实体编号", "实体类型", "数据来源", "抽取规则", "同义词典", "消岐策略", "置信阈值", "最后抽取"]
    REQUIRED_FIELDS = ["实体编号", "实体类型", "数据来源"]
    STATUS_ORDER = ["草稿", "抽取中", "已入库", "低置信"]
    ACTION_RULES = {"启动抽取": "抽取中", "确认入库": "已入库", "标记低置信": "低置信"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记时保留显式写下的实体版本，供入库证据回指使用。"""
        entry, missing = super().create_entry(values)
        if entry is not None:
            version = values.get("实体版本") or values.get("版本")
            if version not in (None, ""):
                entry["实体版本"] = str(version)
        return entry, missing

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if entry.get("status") != "已入库":
            return entry, message
        # 入库结果先固化成一份共享实体证据；关系建模和实体链接都从这份
        # 证据转换各自旧接口字段，避免两边各写一套默认值/缺值规则。
        evidence = evidence_view(build_entity_evidence(entry))
        if evidence is None:
            return entry, message
        # 链路 L1: entity_extract → relation_model（实体入库后自动创建关系建模记录）
        rel_msg = self._push_to_relation_model(evidence)
        message += "；" + rel_msg
        # 链路 L2: entity_extract → entity_link（实体入库后预创建链接候选）
        link_msg = self._push_to_entity_link(evidence)
        message += "；" + link_msg
        return entry, message

    def _push_to_relation_model(self, evidence: EntityEvidence) -> str:
        relation_rows = store.rows("relation_model")
        next_id = max((int(r.get("id", 0)) for r in relation_rows), default=0) + 1
        rel_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "关系编号": f"RELA-AUTO-{next_id:04d}",
        }
        rel_entry.update(evidence.relation_fields())
        rel_entry["反关系"] = f"inv_{next_id}"
        rel_entry["使用次数"] = "0"
        rel_entry.update(evidence.provenance_fields())
        rel_entry["关联入库记录"] = evidence.provenance_fields()
        rel_entry["入库记录编号"] = evidence.ingest_id
        rel_entry["证据编号"] = evidence.evidence_id
        rel_entry["实体版本"] = evidence.version
        rel_entry["主体实体"] = evidence.entity_number
        rel_entry["生成输入"] = evidence.source_snapshot()
        relation_rows.append(rel_entry)
        return f"已向关系建模推送自动关系 {rel_entry['关系编号']}"

    def _push_to_entity_link(self, evidence: EntityEvidence) -> str:
        link_rows = store.rows("entity_link")
        next_id = max((int(r.get("id", 0)) for r in link_rows), default=0) + 1
        link_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待确认",
            "pending": True,
            "abnormal": False,
            "规则编号": f"ENTI-LINK-{next_id:04d}",
        }
        link_entry.update(evidence.link_fields())
        link_entry["人工确认"] = "待确认"
        link_entry.update(evidence.provenance_fields())
        link_entry["关联入库记录"] = evidence.provenance_fields()
        link_entry["入库记录编号"] = evidence.ingest_id
        link_entry["证据编号"] = evidence.evidence_id
        link_entry["实体版本"] = evidence.version
        link_entry["生成输入"] = evidence.source_snapshot()
        link_rows.append(link_entry)
        return f"已向实体链接推送候选 {link_entry['规则编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["relation_model", "entity_link"]
        return base
