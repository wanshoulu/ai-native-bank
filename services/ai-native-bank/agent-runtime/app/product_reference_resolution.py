"""Deterministic, fail-closed resolution for graph-declared wealth products."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any, Literal


ProductReferenceState = Literal["selected", "ambiguous", "empty"]

_MAX_VISIBLE_OPTIONS = 8
_INTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_PRODUCT_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
_UNSAFE_MARKERS = (
    "productid", "accountid", "忽略", "无视", "系统提示", "提示词",
    "指令", "密钥", "密码", "验证码", "token", "secret", "prompt",
)
_WEALTH_PRODUCT_CATEGORIES = frozenset({
    "DEPOSIT",
    "CERTIFICATE_OF_DEPOSIT",
    "CASH_MANAGEMENT",
    "FIXED_INCOME_WEALTH",
    "FUND",
})
_PURCHASABLE_STATUSES = frozenset({"ON_SALE", "LIMITED"})


class ProductReferenceResolutionError(ValueError):
    """Raised when provider product data cannot be projected safely."""


def _canonical(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value)
        if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    )


def _compact(value: Any) -> str:
    return "".join(
        character.casefold()
        for character in _canonical(value)
        if unicodedata.category(character)[0] not in {"P", "Z", "S", "M", "C"}
    )


def _safe_identifier(value: Any) -> str | None:
    normalized = _canonical(value).strip()
    return normalized if _INTERNAL_ID_RE.fullmatch(normalized) else None


def _safe_product_code(value: Any) -> str | None:
    normalized = _canonical(value).strip()
    return normalized if _PRODUCT_CODE_RE.fullmatch(normalized) else None


def _safe_product_name(value: Any) -> str:
    normalized = " ".join(_canonical(value).split()).strip()
    compact = _compact(normalized)
    if (
        not normalized
        or len(normalized) > 80
        or any(marker in compact for marker in _UNSAFE_MARKERS)
    ):
        raise ProductReferenceResolutionError("product name is invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class ProductOption:
    product_id: str = field(repr=False)
    name: str
    product_code: str
    risk_level: str
    category: str
    reply_message: str


@dataclass(frozen=True, slots=True)
class ProductReferenceResolution:
    state: ProductReferenceState
    options: tuple[ProductOption, ...]
    product_id: str | None = field(default=None, repr=False)
    semantic_reference: str | None = None
    total_count: int = 0
    options_truncated: bool = False


def _on_sale_products(products: Any) -> tuple[ProductOption, ...]:
    if not isinstance(products, list):
        raise ProductReferenceResolutionError("products must be a list")
    options: list[ProductOption] = []
    seen_ids: set[str] = set()
    for product in products:
        if not isinstance(product, dict):
            raise ProductReferenceResolutionError("product record must be an object")
        category = _canonical(product.get("category")).strip()
        # The provider capability intentionally exposes one mixed financial
        # catalogue.  Loan, card and service entries are valid catalogue rows,
        # but must never become candidates for a wealth purchase.
        if category not in _WEALTH_PRODUCT_CATEGORIES:
            continue
        product_id = _safe_identifier(product.get("id"))
        if product_id is None or product_id in seen_ids:
            raise ProductReferenceResolutionError("product identity is invalid")
        seen_ids.add(product_id)
        status = _canonical(product.get("status")).strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,31}", status):
            raise ProductReferenceResolutionError("product status is invalid")
        if status not in _PURCHASABLE_STATUSES:
            continue
        name = _safe_product_name(product.get("name"))
        code = _safe_product_code(product.get("productCode"))
        risk_level = _canonical(product.get("riskLevel")).strip()
        if code is None or not re.fullmatch(r"R[1-5]", risk_level):
            raise ProductReferenceResolutionError("product public metadata is invalid")
        if not category or len(category) > 40:
            raise ProductReferenceResolutionError("product category is invalid")
        options.append(ProductOption(
            product_id=product_id,
            name=name,
            product_code=code,
            risk_level=risk_level,
            category=category,
            reply_message=f"选择理财产品：{name}",
        ))
    return tuple(options)


def resolve_product_reference(
    source_message: str | None,
    products: Any,
) -> ProductReferenceResolution:
    """Resolve one on-sale product by its provider-owned public name or code."""

    if source_message is not None and not isinstance(source_message, str):
        raise ProductReferenceResolutionError("source message must be text or null")
    options = _on_sale_products(products)
    if not options:
        return ProductReferenceResolution("empty", (), total_count=0)
    message = _canonical(source_message)
    compact = _compact(message)
    unsafe = any(marker in compact for marker in _UNSAFE_MARKERS)
    matches = tuple(
        option for option in options
        if (
            _compact(option.name) in compact
            or option.product_code.casefold() in message.casefold()
        )
    )
    if not unsafe and len(matches) == 1:
        selected = matches[0]
        return ProductReferenceResolution(
            "selected",
            (selected,),
            product_id=selected.product_id,
            semantic_reference=selected.name,
            total_count=len(options),
        )
    visible = options[:_MAX_VISIBLE_OPTIONS]
    return ProductReferenceResolution(
        "ambiguous",
        visible,
        total_count=len(options),
        options_truncated=len(options) > len(visible),
    )


__all__ = [
    "ProductOption", "ProductReferenceResolution",
    "ProductReferenceResolutionError", "resolve_product_reference",
]
