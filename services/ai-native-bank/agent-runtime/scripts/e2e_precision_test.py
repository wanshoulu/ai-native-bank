"""全面端到端精度测试：20条基础 + 10条同义表述（验证非硬编码泛化能力）
通过 ai-harbor 网关 API 测试，验证四层召回管线（词法+子图+语义向量+LLM裁决）。
"""
import json
import urllib.request
import urllib.parse
import sys

API_BASE = "http://host.docker.internal:3000/api/ai-bank/v1/intent-recall-probe"

# 基础20条
BASE_CASES = [
    ("我想申请信用卡", "ccb.credit.apply.faq"),
    ("怎么查余额", "ccb.account.balance.query"),
    ("信用卡额度不够用", "ccb.credit.card.query"),
    ("帮我转账", "ccb.transfer.create"),
    ("理财产品收益怎么样", "ccb.wealth.dragon-money.overview"),
    ("怎么查开户行", "ccb.home.account.apply"),
    ("信用卡年费多少", "ccb.credit.card.faq"),
    ("怎么开通手机银行", "ccb.life.mobile.apply"),
    ("龙财富怎么用", "ccb.wealth.dragon-money.buy"),
    ("信用卡怎么还款", "ccb.credit.bill.repay"),
    ("我要查交易明细", "ccb.account.transactions.query"),
    ("信用卡挂失", "ccb.credit.card.loss-report"),
    ("怎么提高信用卡额度", "ccb.credit.card.limit-change"),
    ("帮我激活信用卡", "ccb.credit.card.activation"),
    ("怎么购买理财", "ccb.wealth.wealth-product.buy"),
    ("理财赎回", "ccb.wealth.redeem"),
    ("查信用卡账单", "ccb.credit.bill.overview"),
    ("信用卡消费记录", "ccb.credit.card.transactions.query"),
    ("帮我选理财", "ccb.wealth.advisor.recommend"),
    ("我的账户信息", "ccb.account.detail.query"),
]

# 同义表述10条（不在alias列表中的变体表述，验证泛化能力）
SYNONYM_CASES = [
    ("如何提升信用卡额度", "ccb.credit.card.limit-change"),      # 同义但不在alias中
    ("信用卡想调高额度", "ccb.credit.card.limit-change"),          # 同义变体
    ("怎样查询账户余额", "ccb.account.balance.query"),              # 同义变体
    ("想办一张信用卡", "ccb.credit.apply.faq"),                    # 同义变体
    ("把钱转给别人", "ccb.transfer.create"),                       # 同义变体
    ("信用卡丢了怎么办", "ccb.credit.card.loss-report"),            # 同义变体
    ("信用卡账单怎么看", "ccb.credit.bill.overview"),              # 同义变体
    ("最近花了多少钱", "ccb.account.transactions.query"),           # 语义近似
    ("怎么存钱进去", "ccb.home.account.apply"),                    # 同义变体
    ("帮忙推荐个理财产品", "ccb.wealth.advisor.recommend"),         # 同义变体
]


def recall(message: str) -> dict:
    """调用意图召回探针API"""
    url = API_BASE + "?message=" + urllib.parse.quote(message)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_test(cases: list, label: str) -> tuple:
    """运行测试，返回(pass_count, total, details)"""
    print(f"\n{'='*60}")
    print(f"  {label}: {len(cases)} 条")
    print(f"{'='*60}")

    passed = 0
    for i, (msg, expected) in enumerate(cases, 1):
        try:
            data = recall(msg)
            candidates = data.get("data", {}).get("candidates", [])
            if not candidates:
                print(f"  [{i:2d}] FAIL  msg={msg!r}  ->  无候选")
                continue
            top1 = candidates[0]
            top1_id = top1.get("intentId", "")
            top1_score = top1.get("score", 0)
            top1_evidence = top1.get("evidence", "")
            ok = top1_id == expected
            status = "PASS" if ok else "FAIL"
            if ok:
                passed += 1
            print(
                f"  [{i:2d}] {status:4s}  msg={msg!r}  "
                f"top1={top1_id}  score={top1_score}  evidence={top1_evidence}"
            )
            if not ok:
                print(f"         expected={expected}")
        except Exception as exc:
            print(f"  [{i:2d}] ERROR msg={msg!r}  ->  {exc}")

    print(f"\n  {label} 结果: {passed}/{len(cases)} = {passed/len(cases)*100:.0f}%")
    return passed, len(cases)


if __name__ == "__main__":
    p1, t1 = run_test(BASE_CASES, "基础测试")
    p2, t2 = run_test(SYNONYM_CASES, "同义泛化测试")

    total_pass = p1 + p2
    total = t1 + t2
    rate = total_pass / total * 100

    print(f"\n{'='*60}")
    print(f"  总体精度: {total_pass}/{total} = {rate:.1f}%")
    print(f"  基础: {p1}/{t1}  同义: {p2}/{t2}")
    if rate >= 90:
        print(f"  >>> 精度达标 (>=90%)")
    else:
        print(f"  >>> 精度未达标 (<90%)")
    print(f"{'='*60}")

    sys.exit(0 if rate >= 90 else 1)
