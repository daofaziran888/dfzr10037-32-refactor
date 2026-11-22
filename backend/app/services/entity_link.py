"""实体链接领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.entity_evidence import (
    EntityEvidence,
    ensure_evidence_for_ingests,
    entry_evidence_inputs,
    evidence_for_entity,
    evidence_for_ingest,
    evidence_inputs,
    evidence_rows,
    evidence_view,
)
from app.store import store

# 旧链接候选只记实体号，没有版本回指；补发时用它标记来源是恢复出来的。
RECOVERED_FROM = "无版本旧候选"


class EntityLinkService(BaseModuleService):
    MODULE = "entity_link"
    ENTITY = "链接规则"
    OPERATION_TERM = "归并"
    LIST_FIELDS = ["规则编号", "源实体", "目标实体", "链接类型", "置信度", "链接证据", "人工确认", "链接时间"]
    REQUIRED_FIELDS = ["规则编号", "源实体", "目标实体"]
    STATUS_ORDER = ["待确认", "已链接", "已驳回", "需复核"]
    ACTION_RULES = {"确认链接": "已链接", "驳回归并": "已驳回", "提交复核": "需复核"}
    NEGATIVE_ACTIONS = ["驳回归并"]

    def list_entries(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        """打开链接工作台时先恢复无版本旧候选，再按原接口返回列表。"""
        self.recover_candidates()
        return super().list_entries(**kwargs)

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        # 链路 L7: entity_link → disambig（确认链接后更新消岐融合状态）
        if entry.get("status") in ("已链接", "需复核"):
            disambig_msg = self._push_to_disambig(entry)
            message += "；" + disambig_msg
        return entry, message

    def _push_to_disambig(self, entry: dict[str, Any]) -> str:
        rows = store.rows("disambig")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        dis_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "配置编号": f"DISA-LINK-{next_id:04d}",
            "歧义类型": f"link_resolve_{entry.get('链接类型','')}",
            "候选实体": str(entry.get("目标实体", "")),
            "特征向量": str(entry.get("链接证据", "")),
            "消岐算法": "co-reference-resolution",
            "融合策略": "linked-entity-merge",
            "精度指标": str(entry.get("置信度", "0.7")),
            "最近执行": str(entry.get("链接时间", "2026-11-18")),
        }
        rows.append(dis_entry)
        return f"消岐融合受实体链接影响: {dis_entry['配置编号']}"

    # ===== 链接工作台：从共享实体证据恢复旧候选 =====
    def review_list(
        self,
        *,
        entity_number: str | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        """实体链接待复核清单：只读共享实体证据，不重算另一路已交付字段。"""
        rows = evidence_rows()
        if entity_number:
            rows = [row for row in rows if str(row.get("实体编号", "")) == str(entity_number)]
        if version:
            rows = [row for row in rows if str(row.get("实体版本", "")) == str(version)]
        entries: list[dict[str, Any]] = []
        for row in rows:
            evidence = evidence_view(row)
            if evidence is None:
                continue
            item = {"证据编号": evidence.evidence_id, "实体版本": evidence.version}
            item.update(evidence.link_fields())
            item.update(evidence.provenance_fields())
            item["入库记录编号"] = evidence.ingest_id
            item["待复核"] = True
            entries.append(item)
        return {
            "ok": True,
            "module": self.MODULE,
            "count": len(entries),
            "entries": entries,
            "inputs": [evidence_inputs(row) for row in rows],
        }

    def recover_candidates(self) -> dict[str, Any]:
        """重新打开链接工作台：给没有版本回指的旧候选补发证据。

        - 同一实体只有一笔入库：唯一对应，可以补发；
        - 同一实体有多笔入库但只有一版：仍然唯一，可以补发；
        - 多版且只记实体号：有歧义，保留在原候选中待核，不猜置信值。
        补发只补缺失的版本回指和置信值，不覆盖候选里已有的字段。
        """
        # 功能上线前就已入库的实体没有证据，按原入库记录补一份再恢复。
        ensure_evidence_for_ingests()
        recovered: list[dict[str, Any]] = []
        ambiguous: list[dict[str, Any]] = []
        untouched: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if row.get("实体版本") or row.get("证据编号"):
                continue
            if row.get("入库记录编号") not in (None, ""):
                # 存量记录里明确写了入库记录号，按它找对应证据。
                by_ingest = evidence_for_ingest(row.get("入库记录编号"))
                if len(by_ingest) > 1:
                    ambiguous.append(self._describe_candidate(row, by_ingest))
                    continue
                evidence_row = by_ingest[0] if by_ingest else None
            else:
                matches = evidence_for_entity(str(row.get("源实体", "")))
                by_other = self._match_by_other_evidence(row, matches)
                if not by_other and matches:
                    # 其他证据和所有版本都对不上，不能按实体号硬猜版本。
                    ambiguous.append(self._describe_candidate(row, matches))
                    continue
                versions = {str(item.get("实体版本", "")) for item in by_other}
                if len(versions) == 1:
                    evidence_row = by_other[0]
                elif len(versions) > 1:
                    ambiguous.append(self._describe_candidate(row, by_other))
                    continue
                else:
                    untouched.append(self._describe_candidate(row, matches))
                    continue
            if evidence_row is None:
                untouched.append(self._describe_candidate(row, []))
                continue
            evidence = evidence_view(evidence_row)
            if evidence is None:
                untouched.append(self._describe_candidate(row, []))
                continue
            changes = self._apply_recovery(row, evidence)
            recovered.append({
                "规则编号": row.get("规则编号", ""),
                "id": row.get("id"),
                "字段": dict(self._recovery_provenance(row)),
                "取值": changes,
                "恢复来源": RECOVERED_FROM,
            })
        return {
            "ok": True,
            "recovered": recovered,
            "ambiguous": ambiguous,
            "untouched": untouched,
            "counts": {
                "recovered": len(recovered),
                "ambiguous": len(ambiguous),
                "untouched": len(untouched),
            },
        }

    def _match_by_other_evidence(
        self, row: dict[str, Any], matches: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """用候选里除实体号和置信值之外的证据缩小版本范围。

        置信值正是可能要补发的字段，不能拿它反推版本；只有候选里明确写了
        目标实体、链接类型、链接证据或链接时间时才参与匹配。一个可用字段
        都没写的候选，等于只能按实体号匹配。
        """
        if not matches:
            return matches
        discriminators = {
            field: str(row[field])
            for field in ("目标实体", "链接类型", "链接证据", "链接时间")
            if str(row.get(field) or "").strip()
        }
        if not discriminators:
            return matches
        return [
            item for item in matches
            if all(
                str(EntityEvidence(dict(item)).link_fields().get(field, "")) == value
                for field, value in discriminators.items()
            )
        ]

    def _apply_recovery(
        self, row: dict[str, Any], evidence: EntityEvidence
    ) -> dict[str, Any]:
        """补发只填缺失的证据字段，已经存在的旧字段保持原值。

        返回实际补进去的取值，方便核对补发条目用了哪一版的证据；没被改动
        的旧字段不出现在返回值里。
        """
        added: dict[str, Any] = {}
        if not str(row.get("置信度") or "").strip():
            row["置信度"] = evidence.confidence
            added["置信度"] = row["置信度"]
        row.setdefault("来源候选", row.get("规则编号", ""))
        row["恢复状态"] = RECOVERED_FROM
        for field, value in evidence.provenance_fields().items():
            row[field] = value
            added[field] = value
        row["关联入库记录"] = evidence.provenance_fields()
        row["恢复输入"] = evidence.source_snapshot()
        return added

    def _recovery_provenance(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "证据编号": row.get("证据编号"),
            "入库记录编号": row.get("入库记录编号"),
            "实体版本": row.get("实体版本"),
        }

    def _describe_candidate(
        self, row: dict[str, Any], matches: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "id": row.get("id"),
            "规则编号": row.get("规则编号", ""),
            "源实体": row.get("源实体", ""),
            "候选版本": [
                {"证据编号": item.get("证据编号"), "实体版本": item.get("实体版本"),
                 "入库记录编号": item.get("入库记录编号"), "置信值": item.get("置信值")}
                for item in matches
            ],
            "原因": "同一实体存在多版入库，旧候选只记实体号，保留原候选待核",
        }

    def evidence_inputs_for(self, entry: Any, ref: Any | None = None) -> dict[str, Any] | None:
        return entry_evidence_inputs(entry, ref)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["disambig"]
        return base
