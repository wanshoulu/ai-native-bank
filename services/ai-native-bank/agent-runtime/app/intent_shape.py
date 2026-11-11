"""Domain-neutral, non-authoritative structural evidence for the User DeepAgent."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from app.contracts import IntentShape


AmountScope = Literal["per_task", "total"]

_CLAUSE_SEPARATOR = re.compile(r"\s*(?:并且|同时|另外|还要|以及|再|并|然后|之后|接着|还有)\s*")
_COMMA_SEPARATOR = re.compile(r"\s*[，,]\s*")
# 动作指示词：出现在子句开头附近时，表明该子句是独立祈使句而非称呼或修饰语。
_ACTION_LEADING = re.compile(r"^.{0,2}(?:查|转|汇|充|还|缴|办|买|卖|退|领|取|存|开|关|改|约|申|报|帮|请|给|看|查|搜|找|问|设|修|挂|解|冻|注|销)")
_MASKED_REFERENCE = re.compile(r"(?:尾号|末四位)\s*([0-9]{4})")
_EXPLICIT_PAIR = re.compile(r"(?:两|2)\s*(?:个|张|笔|位)")
_ENUMERATION_SEPARATOR = re.compile(r"[^，,。]{1,32}(?:和|与|及|、)[^，,。]{1,32}")
# 语言级金额分摊标记：形状层与域适配器共用同一词表，避免各自维护漂移。
DISTRIBUTION_MARKERS = ("每人", "每个", "各自", "各", "分别")
TOTAL_MARKERS = ("总共", "合计", "一共", "总计")
_AMOUNT = re.compile(r"(?<![0-9])[0-9]+(?:\.[0-9]{1,2})?\s*(?:元|块|人民币)")
_ACTION_AMOUNT = re.compile(r"(?:转账|转|汇款|汇|充值|充)\s*[0-9]+(?:\.[0-9]{1,2})?")
_PERSON_TARGET = re.compile(
    r"(?:给|向)([\u4e00-\u9fff]{4,8}?)(?=(?:分别|各自|各)?(?:转账|转|汇款|汇|充值|充))"
)
_SINGLE_CHARACTER_SURNAMES = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华"
    "金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花"
    "方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时"
    "傅皮卞齐康伍余元顾孟平黄和穆萧尹姚邵汪祁毛禹狄米贝明臧"
    "计伏成戴宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危"
    "江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯管卢莫"
    "经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉龚程嵇邢滑"
    "裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴"
    "弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武"
    "符刘景詹束龙叶司韶黎乔苍双闻莘党翟谭贡劳逄姬申扶堵冉宰"
    "郦雍却璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕"
    "连茹习艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广"
    "禄阙东欧沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾"
    "毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公"
)
_COMPOUND_SURNAMES = (
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫",
    "万俟", "闻人", "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台",
    "皇甫", "宗政", "濮阳", "公冶", "太叔", "申屠", "公孙", "慕容",
    "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于", "司空", "闾丘",
)


def _surname_length(value: str) -> int:
    if any(value.startswith(item) for item in _COMPOUND_SURNAMES):
        return 2
    return 1 if value and value[0] in _SINGLE_CHARACTER_SURNAMES else 0


def _is_plausible_person_name(value: str) -> bool:
    surname_length = _surname_length(value)
    return surname_length > 0 and 1 <= len(value) - surname_length <= 2


def _adjacent_person_entities(message: str) -> tuple[str, ...]:
    """Return a high-confidence pair of adjacent Chinese person references.

    The parser is a language-level shape hint, not a bank business rule.  It
    only becomes authoritative when exactly one valid name boundary exists.
    Ambiguous boundaries remain for the User DeepAgent to decide.
    """

    match = _PERSON_TARGET.search(message)
    if match is None:
        return ()
    target = match.group(1)
    boundaries = [
        index
        for index in range(2, len(target) - 1)
        if _is_plausible_person_name(target[:index])
        and _is_plausible_person_name(target[index:])
    ]
    if len(boundaries) != 1:
        return ()
    boundary = boundaries[0]
    return target[:boundary], target[boundary:]


@dataclass(frozen=True)
class ShapeAmbiguity:
    kind: Literal["amount_scope"]
    task_indexes: tuple[int, ...]
    options: tuple[AmountScope, ...]

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "taskIndexes": list(self.task_indexes),
            "options": list(self.options),
        }


@dataclass(frozen=True)
class IntentShapeAssessment:
    shape_hint: IntentShape
    segments: tuple[str, ...]
    entities: tuple[str, ...]
    entity_count_hint: int
    expected_task_count: int
    ambiguities: tuple[ShapeAmbiguity, ...]
    evidence: tuple[str, ...]
    authoritative: bool = False

    @property
    def shape(self) -> IntentShape:
        """Compatibility alias; callers must also inspect ``authoritative``."""

        return self.shape_hint

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "shape": self.shape_hint,
            "shapeHint": self.shape_hint,
            "authoritative": self.authoritative,
            "segments": list(self.segments),
            "entities": list(self.entities),
            "entityCountHint": self.entity_count_hint,
            "expectedTaskCount": self.expected_task_count,
            "ambiguities": [item.to_prompt_dict() for item in self.ambiguities],
            "evidence": list(self.evidence),
        }


def analyze_intent_shape(message: str) -> IntentShapeAssessment:
    """Return bounded structural hints without assigning an intent or task count."""

    normalized = " ".join(message.strip().split())
    if not normalized:
        return IntentShapeAssessment(
            shape_hint="non_task",
            segments=(),
            entities=(),
            entity_count_hint=0,
            expected_task_count=0,
            ambiguities=(),
            evidence=("empty_input",),
            authoritative=True,
        )

    if not any(
        unicodedata.category(character)[0] in {"L", "N"}
        for character in normalized
    ):
        return IntentShapeAssessment(
            shape_hint="non_task",
            segments=(),
            entities=(),
            entity_count_hint=0,
            expected_task_count=0,
            ambiguities=(),
            evidence=("no_semantic_signal",),
            authoritative=True,
        )

    split_segments = tuple(
        part.strip(" ，,。") for part in _CLAUSE_SEPARATOR.split(normalized) if part.strip(" ，,。")
    )

    # 显式连接词未拆分时，尝试逗号拆分：
    # 仅当每段都以动作指示词开头时才判定为多意图，避免"张三，帮我查余额"等称呼语被误拆。
    if len(split_segments) <= 1:
        comma_parts = [p.strip() for p in _COMMA_SEPARATOR.split(normalized) if p.strip()]
        if (
            len(comma_parts) >= 2
            and all(len(p) >= 2 for p in comma_parts)
            and sum(1 for p in comma_parts if _ACTION_LEADING.search(p)) >= 2
        ):
            split_segments = tuple(comma_parts)
    adjacent_entities = _adjacent_person_entities(normalized)
    references = adjacent_entities or tuple(
        dict.fromkeys(_MASKED_REFERENCE.findall(normalized))
    )
    evidence: list[str] = []
    entity_count = len(references)

    if len(split_segments) > 1:
        shape_hint: IntentShape = "multi_intent"
        segments = split_segments
        expected_count = len(split_segments)
        evidence.append("explicit_clause_boundary")
    else:
        segments = (normalized,)
        if entity_count > 1:
            evidence.append(
                "adjacent_person_entities"
                if adjacent_entities else "repeated_masked_reference"
            )
        elif _EXPLICIT_PAIR.search(normalized):
            entity_count = 2
            evidence.append("explicit_collective_quantity")
        elif _ENUMERATION_SEPARATOR.search(normalized) and (
            _AMOUNT.search(normalized)
            or any(marker in normalized for marker in (*DISTRIBUTION_MARKERS, *TOTAL_MARKERS))
        ):
            entity_count = 2
            evidence.append("explicit_enumeration")
        elif any(marker in normalized for marker in DISTRIBUTION_MARKERS):
            entity_count = 2
            evidence.append("distribution_marker")
        if entity_count > 1:
            shape_hint = "same_intent_multi_task"
            expected_count = entity_count
        else:
            shape_hint = "single_intent"
            expected_count = 1
            evidence.append("single_clause")

    ambiguities: tuple[ShapeAmbiguity, ...] = ()
    if shape_hint == "same_intent_multi_task" and (
        _AMOUNT.search(normalized) or _ACTION_AMOUNT.search(normalized)
    ):
        if any(marker in normalized for marker in TOTAL_MARKERS):
            evidence.append("amount_scope:total")
        elif any(marker in normalized for marker in DISTRIBUTION_MARKERS):
            evidence.append("amount_scope:per_task")
        else:
            ambiguities = (
                ShapeAmbiguity(
                    kind="amount_scope",
                    task_indexes=tuple(range(expected_count)),
                    options=("per_task", "total"),
                ),
            )
            evidence.append("amount_scope:unresolved")

    # 当检测到显式子句边界（连接词拆分或逗号+动作词拆分）时，
    # 形态判定是高置信的，应设为 authoritative=True，
    # 否则 recall_intent_constraints 不会采纳 multi_intent 形态。
    # adjacent_person_entities / repeated_masked_reference 等场景保持非权威，
    # 由 recall_intent_constraints 的 graph_authorizes_entity_cardinality 兜底，
    # 防止「给张三李四」类人名边界解析误判为多任务（需图谱 task_policy 明确支持）。
    is_authoritative = "explicit_clause_boundary" in evidence

    return IntentShapeAssessment(
        shape_hint=shape_hint,
        segments=segments,
        entities=references,
        entity_count_hint=entity_count,
        expected_task_count=expected_count,
        ambiguities=ambiguities,
        evidence=tuple(evidence),
        authoritative=is_authoritative,
    )
