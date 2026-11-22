"""关系建模领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.entity_evidence import (
    ensure_evidence_for_ingests,
    entry_evidence_inputs,
    evidence_rows,
    evidence_view,
)
from app.store import store


class RelationModelService(BaseModuleService):
    MODULE = "relation_model"
    ENTITY = "关系类型"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["关系编号", "关系名", "主体类型", "客体类型", "关系权重", "推导规则", "反关系", "使用次数"]
    REQUIRED_FIELDS = ["关系编号", "关系名", "主体类型"]
    STATUS_ORDER = ["草稿", "已定义", "已弃用", "冲突待解"]
    ACTION_RULES = {"确认定义": "已定义", "弃用关系": "已弃用", "标记冲突": "冲突待解"}
    NEGATIVE_ACTIONS = []

    # ===== 关系建模待审清单 =====
    def pending_list(
        self,
        *,
        ingest_id: Any | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        """入库结果同步过来的待确认关系。

        草稿可以在链接侧已经补发之后继续评审：这里只读关系草稿自己记录的
        入库记录和抽取规则，不回看、也不改写实体链接已经交付的证据。
        """
        # 功能上线前就已入库的实体没有证据，按原入库记录补一份；再给还没有
        # 关系草稿的入库证据按同一份源记录写出待确认关系。
        ensure_evidence_for_ingests()
        awaiting = self._sync_relation_drafts()

        entries: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if row.get("status") != self.STATUS_ORDER[0]:
                continue
            if ingest_id not in (None, "") and str(row.get("入库记录编号")) != str(ingest_id):
                continue
            if version and str(row.get("实体版本", "")) != str(version):
                continue
            item = {
                "id": row.get("id"),
                "关系编号": row.get("关系编号", ""),
                "待确认": True,
                "证据编号": row.get("证据编号"),
                "入库记录编号": row.get("入库记录编号"),
                "实体版本": row.get("实体版本"),
                "关联入库记录": row.get("关联入库记录") or {
                    "证据编号": row.get("证据编号"),
                    "入库记录编号": row.get("入库记录编号"),
                    "实体版本": row.get("实体版本"),
                },
            }
            for field in self.LIST_FIELDS:
                item.setdefault(field, row.get(field, ""))
            item["生成输入"] = row.get("生成输入") or entry_evidence_inputs(row)
            entries.append(item)

        return {
            "ok": True,
            "module": self.MODULE,
            "count": len(entries),
            "entries": entries,
            "synced_drafts": awaiting,
        }

    def _sync_relation_drafts(self) -> list[dict[str, Any]]:
        """为还没有关系草稿的入库证据补写待确认关系。"""
        covered = {
            str(row.get("证据编号"))
            for row in store.rows(self.MODULE)
            if row.get("证据编号")
        }
        rows = store.rows(self.MODULE)
        created: list[dict[str, Any]] = []
        for evidence_row in evidence_rows():
            if str(evidence_row.get("证据编号")) in covered:
                continue
            evidence = evidence_view(evidence_row)
            if evidence is None:
                continue
            next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            draft: dict[str, Any] = {
                "id": next_id,
                "status": self.STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "关系编号": f"RELA-AUTO-{next_id:04d}",
            }
            draft.update(evidence.relation_fields())
            draft["反关系"] = f"inv_{next_id}"
            draft["使用次数"] = "0"
            draft.update(evidence.provenance_fields())
            draft["关联入库记录"] = evidence.provenance_fields()
            draft["入库记录编号"] = evidence.ingest_id
            draft["主体实体"] = evidence.entity_number
            draft["生成输入"] = evidence.source_snapshot()
            rows.append(draft)
            created.append({
                "关系编号": draft["关系编号"],
                "关联入库记录": draft["关联入库记录"],
                "关系草稿": evidence.relation_fields(),
                "生成输入": draft["生成输入"],
            })
        return created

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        # 链路 L1: relation_model → graph_build（关系确认后触发图谱构建）
        if entry.get("status") == "已定义":
            build_msg = self._push_to_graph_build(entry)
            message += "；" + build_msg
        # 链路 L2: relation_model → disambig（冲突待解时通知消岐融合）
        if entry.get("status") == "冲突待解":
            disambig_msg = self._push_to_disambig(entry)
            message += "；" + disambig_msg
        return entry, message

    def _push_to_graph_build(self, entry: dict[str, Any]) -> str:
        build_rows = store.rows("graph_build")
        next_id = max((int(r.get("id", 0)) for r in build_rows), default=0) + 1
        build_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待执行",
            "pending": True,
            "abnormal": False,
            "任务编号": f"GRAP-AUTO-{next_id:04d}",
            "数据范围": str(entry.get("客体类型", "")),
            "图类型": f"{entry.get('主体类型','')}-{entry.get('客体类型','')}-graph",
            "实体数量": 15,
            "关系数量": 8,
            "构建模式": "automatic",
            "增量标记": "true",
            "构建时间": "2026-11-18",
        }
        build_rows.append(build_entry)
        return f"已触图谱构建任务 {build_entry['任务编号']}"

    def _push_to_disambig(self, entry: dict[str, Any]) -> str:
        rows = store.rows("disambig")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        dis_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "配置编号": f"DISA-CONF-{next_id:04d}",
            "歧义类型": f"relation_conflict_{entry.get('关系名','')}",
            "候选实体": str(entry.get("主体类型", "")),
            "特征向量": str(entry.get("推导规则", "")),
            "消岐算法": "semantic-similarity",
            "融合策略": "auto-merge",
            "精度指标": "0.85",
            "最近执行": "2026-11-18",
        }
        rows.append(dis_entry)
        return f"已通知消岐融合配置 {dis_entry['配置编号']}"

    def evidence_inputs_for(self, entry: Any, ref: Any | None = None) -> dict[str, Any] | None:
        return entry_evidence_inputs(entry, ref)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["graph_build", "disambig"]
        return base