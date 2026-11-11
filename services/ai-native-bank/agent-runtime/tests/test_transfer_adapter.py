# coding: utf-8
"""转账域适配器歧义可投影性判定。"""
import unittest

from app.contracts import IntentAmbiguity
from app.domain_adapters.transfer import ambiguity_is_projectable


def _ambiguity(type_: str, task_ids, options):
    return IntentAmbiguity.model_validate({
        "type": type_,
        "task_ids": list(task_ids),
        "options": list(options),
    })


class TransferAmbiguityProjectabilityTests(unittest.TestCase):
    def test_multi_task_amount_scope_is_projectable(self):
        self.assertTrue(ambiguity_is_projectable(
            _ambiguity("amount_scope", ["t1", "t2"], ["per_task", "total"])
        ))

    def test_single_task_amount_scope_is_not_projectable(self):
        self.assertFalse(ambiguity_is_projectable(
            _ambiguity("amount_scope", ["t1"], ["per_task", "total"])
        ))

    def test_model_declared_non_amount_scope_types_are_not_projectable(self):
        # 投影契约只实现 amount_scope；模型 planner 偶发声明其他类型
        # （如 payee_ambiguity）没有下游消费方，跳过并落回常规槽位澄清，
        # 而不是让投影抛错把整轮崩成 blocked。
        self.assertFalse(ambiguity_is_projectable(
            _ambiguity("payee_ambiguity", ["t1"], ["zhang_shuai", "lisi"])
        ))


if __name__ == "__main__":
    unittest.main()
