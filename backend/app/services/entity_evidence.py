"""共享实体证据：一次实体入库的抽取规则、来源、版本和置信值只转换一次。

实体抽取在确认入库时把源记录归一成一份证据，实体链接和关系建模都从这份
证据派生各自的旧接口字段。旧字段的默认值、缺值和显式写入值在证据里分开
记录，后续补发或追溯生成输入时不会把三者混为一谈。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from app.store import store

# 证据不是面向用户的新业务模块，放在下划线开头的内部表里，避免改变既有
# 模块清单和运营概览口径；启动时从种子数据里长出来的表也不会进入清单。
EVIDENCE_TABLE = "_entity_evidence"

# 旧接口字段在各模块里沿用过的默认值。只有源记录里没有这个键时才使用；
# 键存在但值为 None 表示“缺值”，键存在且为空字符串表示“明确写空”。
LEGACY_DEFAULTS: dict[str, str] = {
    "实体编号": "",
    "实体类型": "",
    "数据来源": "",
    "抽取规则": "",
    "同义词典": "",
    "消岐策略": "exact-match",
    "置信阈值": "0.6",
    "最后抽取": "2026-11-18",
}

ORIGIN_EXPLICIT = "explicit"
ORIGIN_DEFAULT = "default"
ORIGIN_ABSENT = "absent"
ORIGIN_MISSING = "missing"


def resolve_source_field(source: Mapping[str, Any], key: str) -> tuple[str, str]:
    """按旧接口语义解析一个字段，同时返回取值来源。

    - 键不存在：使用历史默认值（没有默认值则视为缺省）；
    - 键存在但为 None：缺值，保持空值，不回落到默认值；
    - 其余情况：保留明确写入的值（包括显式空字符串）。
    """
    default = LEGACY_DEFAULTS.get(key, "")
    if key not in source:
        return default, (ORIGIN_DEFAULT if default else ORIGIN_ABSENT)
    value = source[key]
    if value is None:
        return "", ORIGIN_MISSING
    return str(value), ORIGIN_EXPLICIT


def resolve_source_record(source: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """解析共享证据关心的全部源字段，返回取值和取值来源。"""
    values: dict[str, str] = {}
    origins: dict[str, str] = {}
    for key in LEGACY_DEFAULTS:
        values[key], origins[key] = resolve_source_field(source, key)
    return values, origins


def _next_row_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _next_version(entity_number: str) -> str:
    highest = 0
    for row in store.internal_rows(EVIDENCE_TABLE):
        if str(row.get("实体编号", "")) != entity_number:
            continue
        version = str(row.get("实体版本", ""))
        if version.startswith("v") and version[1:].isdigit():
            highest = max(highest, int(version[1:]))
    return f"v{highest + 1}"


@dataclass(frozen=True)
class EntityEvidence:
    """围绕一份已登记证据的只读视图，供两条下游链路共同使用。"""

    row: dict[str, Any]

    @property
    def evidence_id(self) -> str:
        return str(self.row.get("证据编号", ""))

    @property
    def row_id(self) -> int:
        return int(self.row.get("id", 0))

    @property
    def entity_number(self) -> str:
        return str(self.row.get("实体编号", ""))

    @property
    def version(self) -> str:
        return str(self.row.get("实体版本", ""))

    @property
    def ingest_id(self) -> Any:
        return self.row.get("入库记录编号")

    @property
    def entity_type(self) -> str:
        return str(self.row.get("实体类型", ""))

    @property
    def data_source(self) -> str:
        return str(self.row.get("数据来源", ""))

    @property
    def extract_rule(self) -> str:
        return str(self.row.get("抽取规则", ""))

    @property
    def disambiguation(self) -> str:
        return str(self.row.get("消岐策略", ""))

    @property
    def confidence(self) -> str:
        return str(self.row.get("置信值", self.row.get("置信阈值", "")))

    @property
    def extracted_at(self) -> str:
        return str(self.row.get("最后抽取", ""))

    def value(self, key: str) -> str:
        return str(self.row.get("字段取值", {}).get(key, ""))

    def origin(self, key: str) -> str:
        return str(self.row.get("字段来源", {}).get(key, ORIGIN_ABSENT))

    def relation_name_suffix(self) -> str:
        """关系名里针对实体类型的旧默认值：缺键为 unknown，缺值保持空。"""
        if self.origin("实体类型") == ORIGIN_ABSENT:
            return "unknown"
        return self.entity_type

    def link_fields(self) -> dict[str, str]:
        """实体链接旧接口里由实体证据决定的那部分字段。"""
        return {
            "源实体": self.entity_number,
            "目标实体": self.entity_type,
            "链接类型": "自动发现",
            "置信度": self.confidence,
            "链接证据": self.disambiguation,
            "链接时间": self.extracted_at,
        }

    def relation_fields(self) -> dict[str, str]:
        """关系建模旧接口里由实体证据决定的那部分字段。"""
        return {
            "关系名": f"auto_derive_{self.relation_name_suffix()}",
            "主体类型": self.entity_type,
            "客体类型": self.data_source,
            "关系权重": f"from_entity_{self.entity_number}",
            "推导规则": self.extract_rule,
        }

    def provenance_fields(self) -> dict[str, Any]:
        """两条链路共用的版本回指，用来核对字段来自哪笔入库。"""
        return {
            "证据编号": self.evidence_id,
            "入库记录编号": self.ingest_id,
            "实体版本": self.version,
        }

    def source_snapshot(self) -> dict[str, Any]:
        return {
            "证据编号": self.evidence_id,
            "入库记录编号": self.ingest_id,
            "实体版本": self.version,
            "源记录": self.row.get("源记录", {}),
            "字段取值": dict(self.row.get("字段取值", {})),
            "字段来源": dict(self.row.get("字段来源", {})),
        }


def build_entity_evidence(ingest_entry: Mapping[str, Any]) -> dict[str, Any]:
    """把一次“确认入库”的源记录固化成共享实体证据。

    同一条入库记录、同一份源字段重复确认时复用已有证据，不会凭空多出版本；
    换了源字段再确认才算同一实体的下一版。
    """
    values, origins = resolve_source_record(ingest_entry)
    entity_number = values["实体编号"]
    rows = store.internal_rows(EVIDENCE_TABLE)
    for existing in rows:
        if str(existing.get("入库记录编号")) != str(ingest_entry.get("id")):
            continue
        if existing.get("字段取值") == values and existing.get("字段来源") == origins:
            return existing
    row_id = _next_row_id(rows)
    explicit_version = str(ingest_entry.get("实体版本") or "").strip()
    version = explicit_version or _next_version(entity_number)
    evidence = {
        "id": row_id,
        "status": "有效",
        "pending": True,
        "abnormal": False,
        "证据编号": f"EVID-{row_id:04d}",
        "实体编号": entity_number,
        "实体版本": version,
        "实体类型": values["实体类型"],
        "数据来源": values["数据来源"],
        "抽取规则": values["抽取规则"],
        "同义词典": values["同义词典"],
        "消岐策略": values["消岐策略"],
        "置信阈值": values["置信阈值"],
        "置信值": values["置信阈值"],
        "最后抽取": values["最后抽取"],
        "入库记录编号": ingest_entry.get("id"),
        "字段取值": values,
        "字段来源": origins,
        "源记录": dict(ingest_entry),
        "生成时间": datetime.now().isoformat(timespec="seconds"),
    }
    rows.append(evidence)
    return evidence


def evidence_rows() -> list[dict[str, Any]]:
    return store.internal_rows(EVIDENCE_TABLE)


def evidence_view(row: Mapping[str, Any] | None) -> EntityEvidence | None:
    return EntityEvidence(dict(row)) if row is not None else None


def find_evidence(ref: Any) -> dict[str, Any] | None:
    """按行号、证据编号或已固化的证据映射查证据。"""
    if ref is None or ref == "":
        return None
    if isinstance(ref, Mapping):
        return dict(ref)
    text = str(ref).strip()
    for row in evidence_rows():
        if text == str(row.get("证据编号", "")):
            return row
    if text.isdigit():
        for row in evidence_rows():
            if int(row.get("id", 0)) == int(text):
                return row
    return None


def evidence_for_entity(entity_number: str) -> list[dict[str, Any]]:
    return [
        row for row in evidence_rows()
        if str(row.get("实体编号", "")) == str(entity_number)
    ]


def evidence_for_ingest(ingest_id: Any) -> list[dict[str, Any]]:
    return [
        row for row in evidence_rows()
        if str(row.get("入库记录编号")) == str(ingest_id)
    ]


def ensure_evidence_for_ingests() -> list[dict[str, Any]]:
    """给功能上线前就已入库、还没有证据的实体补一份证据。

    只补证据表，不改实体抽取记录，也不反向改写链接候选或关系草稿；
    存量旧候选要等重新打开链接工作台时才按这份证据恢复。
    """
    created: list[dict[str, Any]] = []
    for entry in store.rows("entity_extract"):
        if entry.get("status") != "已入库":
            continue
        if evidence_for_ingest(entry.get("id")):
            continue
        created.append(build_entity_evidence(entry))
    return created


def evidence_inputs(ref: Any) -> dict[str, Any] | None:
    row = find_evidence(ref)
    if row is None:
        return None
    return EntityEvidence(row).source_snapshot()


def entry_evidence_inputs(
    entry: Mapping[str, Any], ref: Any | None = None
) -> dict[str, Any] | None:
    """给任一路记录追溯它实际使用的实体证据输入。

    优先认显式传入的 ref，其次认记录自己的证据编号，再按入库记录号唯一
    对应；都不行时回落到记录里已经固化的生成/恢复输入。
    """
    row = find_evidence(ref) if ref not in (None, "") else None
    if row is None and entry.get("证据编号"):
        row = find_evidence(entry.get("证据编号"))
    if row is None and entry.get("入库记录编号") not in (None, ""):
        matches = evidence_for_ingest(entry.get("入库记录编号"))
        row = matches[0] if len(matches) == 1 else None
    if row is not None:
        return EntityEvidence(row).source_snapshot()
    for key in ("恢复输入", "生成输入"):
        snapshot = entry.get(key)
        if isinstance(snapshot, Mapping):
            return dict(snapshot)
    return None


def list_evidence(
    *,
    entity_number: str | None = None,
    ingest_id: Any | None = None,
    version: str | None = None,
) -> list[dict[str, Any]]:
    rows = list(evidence_rows())
    if entity_number:
        rows = [row for row in rows if str(row.get("实体编号", "")) == str(entity_number)]
    if ingest_id not in (None, ""):
        rows = [row for row in rows if str(row.get("入库记录编号")) == str(ingest_id)]
    if version:
        rows = [row for row in rows if str(row.get("实体版本", "")) == str(version)]
    return rows
