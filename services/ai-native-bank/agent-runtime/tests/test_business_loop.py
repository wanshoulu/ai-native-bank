import json
import re
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.business_loop import (
    BusinessLoopOutcome,
    business_session_id,
    business_workflow_id,
    load_openjiuwen_todo_snapshot,
    normalize_terminal_loop_submission,
    validate_goal_coverage,
)
from app.todo_ledger import TodoLedger, TodoLedgerConflict


class BusinessLoopTests(unittest.TestCase):
    def test_missing_workspace_todo_is_restored_from_persisted_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = TodoLedger(root=directory)
            restored = ledger.restore(
                session_id="business:conv_1:bwf_123",
                persisted_revision=4,
                persisted_items=[{
                    "id": "transfer_zhangsan",
                    "status": "in_progress",
                    "dependsOn": [],
                }],
            )

            loaded = ledger.load("business:conv_1:bwf_123", revision=4)

        self.assertEqual(restored, loaded)
        self.assertEqual(restored.revision, 4)
        self.assertEqual(restored.items[0].id, "transfer_zhangsan")

    def test_workspace_and_persisted_digest_conflict_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = TodoLedger(root=directory)
            restored = ledger.restore(
                session_id="business:conv_1:bwf_123",
                persisted_revision=5,
                persisted_items=[{
                    "id": "transfer_zhangsan",
                    "status": "pending",
                    "dependsOn": [],
                }],
            )

            with self.assertRaises(TodoLedgerConflict):
                ledger.reconcile(
                    "business:conv_1:bwf_123",
                    persisted_revision=5,
                    persisted_digest="0" * 64,
                )

        self.assertRegex(restored.digest, r"^[a-f0-9]{64}$")

    def test_todo_ledger_digest_excludes_model_authored_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = TodoLedger(root=directory)
            first = ledger.restore(
                "business:conv_1:bwf_123",
                2,
                [{"id": "task_1", "status": "pending", "dependsOn": []}],
            )
            target = Path(directory) / "todo" / "business:conv_1:bwf_123" / "todo.json"
            native = json.loads(target.read_text(encoding="utf-8"))
            native[0]["description"] = "sensitive model text 6217000000000000"
            target.write_text(json.dumps(native), encoding="utf-8")

            second = ledger.load("business:conv_1:bwf_123", revision=2)

        self.assertEqual(first.digest, second.digest)

    def test_future_missing_slot_cannot_replace_first_active_goal(self):
        from app.business_todo import GoalState, select_active_goal

        goals = (
            GoalState(
                task_id="transfer_zhangsan",
                sequence=1,
                status="ready",
            ),
            GoalState(
                task_id="transfer_lisi",
                sequence=2,
                status="awaiting_clarification",
            ),
        )

        selected = select_active_goal(
            goals,
            previous_active_task_id=None,
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.task_id, "transfer_zhangsan")

    def test_stable_workflow_and_session_ids(self):
        workflow_id = business_workflow_id("cust_1", "conv_1", "dlg_1")

        self.assertRegex(workflow_id, r"^bwf_[a-f0-9]{24}$")
        self.assertEqual(
            workflow_id,
            business_workflow_id("cust_1", "conv_1", "dlg_1"),
        )
        self.assertEqual(
            business_session_id("cust_1", workflow_id),
            f"business:cust_1:{workflow_id}",
        )
        self.assertEqual(
            business_session_id("cust_1", workflow_id),
            business_session_id("cust_1", workflow_id),
        )

    def test_workflow_id_avoids_digit_runs_that_trip_node_side_validator(self):
        # 回归样本：原始摘要前 24 位为 f4db36c1851266841211466b（含 16 位连续数字），
        # Node 侧敏感数据校验会视为卡号并拒绝持久化。派生须滑动取安全窗口。
        workflow_id = business_workflow_id("cust_t_4", "conv_t", "dlg_t")

        self.assertEqual(workflow_id, "bwf_266841211466b4c11d876449")
        self.assertIsNone(re.search(r"(?:\d[ -]?){13,19}", workflow_id[4:]))
        self.assertEqual(
            workflow_id,
            business_workflow_id("cust_t_4", "conv_t", "dlg_t"),
        )

    def test_business_session_id_rejects_ambiguous_or_unsafe_components(self):
        workflow_id = "bwf_" + "a" * 24
        for customer_id in (
            "cust:other",
            "../cust_other",
            "cust/other",
            "cust..other",
            "c" * 97,
        ):
            with self.subTest(customer_id=customer_id):
                with self.assertRaisesRegex(ValueError, "identity is invalid"):
                    business_session_id(customer_id, workflow_id)
        for invalid_workflow_id in (
            "bwf:other",
            "../bwf_other",
            "bwf/other",
            "bwf..other",
            "b" * 97,
        ):
            with self.subTest(workflow_id=invalid_workflow_id):
                with self.assertRaisesRegex(ValueError, "identity is invalid"):
                    business_session_id("cust_demo_001", invalid_workflow_id)

    def test_business_session_id_uses_the_shared_160_character_boundary(self):
        maximum = business_session_id("c" * 96, "w" * 54)

        self.assertEqual(len(maximum), 160)
        with tempfile.TemporaryDirectory() as directory:
            ledger = TodoLedger(directory)
            ledger.restore(
                maximum,
                1,
                [{"id": "task_one", "status": "pending", "dependsOn": []}],
            )
            self.assertEqual(len(ledger.load(maximum).items), 1)
            self.assertEqual(
                len((Path(directory) / "todo" / maximum).name),
                160,
            )

        for customer_id, workflow_id in (
            ("c" * 96, "w" * 55),
            ("c" * 96, "w" * 96),
            ("客户", "bwf_" + "a" * 24),
            ("cust..other", "bwf_" + "a" * 24),
        ):
            with self.subTest(
                customer_id=customer_id,
                workflow_id=workflow_id,
            ):
                with self.assertRaisesRegex(ValueError, "identity is invalid"):
                    business_session_id(customer_id, workflow_id)

    def test_authoritative_todo_ledger_accepts_up_to_128_items(self):
        def items(count):
            return [
                {
                    "id": f"task_{index:03d}",
                    "status": "pending",
                    "dependsOn": [],
                }
                for index in range(count)
            ]

        with tempfile.TemporaryDirectory() as directory:
            ledger = TodoLedger(directory)
            for count in (64, 65, 128):
                with self.subTest(count=count):
                    restored = ledger.restore(
                        f"business:cust:bwf_{count}",
                        count,
                        items(count),
                    )
                    self.assertEqual(len(restored.items), count)
                    self.assertEqual(
                        len(ledger.load(
                            f"business:cust:bwf_{count}", revision=count
                        ).items),
                        count,
                    )
            with self.assertRaisesRegex(ValueError, "snapshot is too large"):
                ledger.restore(
                    "business:cust:bwf_129",
                    129,
                    items(129),
                )

    def test_goal_coverage_uses_native_todo_ids_not_array_positions(self):
        self.assertEqual(
            validate_goal_coverage(
                {"task_zhang", "task_li"},
                {"task_zhang", "task_zhang:validate"},
            ),
            ("task_li",),
        )

    def test_previous_active_goal_is_sticky_until_terminal(self):
        from app.business_todo import GoalState, select_active_goal

        goals = (
            GoalState(
                task_id="transfer_zhangsan",
                sequence=1,
                status="awaiting_confirmation",
            ),
            GoalState(
                task_id="transfer_lisi",
                sequence=2,
                status="ready",
            ),
        )

        selected = select_active_goal(
            goals,
            previous_active_task_id="transfer_zhangsan",
        )

        self.assertEqual(selected.task_id, "transfer_zhangsan")

    def test_sequential_group_rejects_two_active_goals(self):
        from app.business_todo import GoalState, validate_sequential_goals

        with self.assertRaises(ValueError):
            validate_sequential_goals((
                GoalState(task_id="a", sequence=1, status="in_progress"),
                GoalState(task_id="b", sequence=2, status="waiting_external"),
            ))

    def test_sequential_progress_comes_from_active_goal_not_missing_slot_index(self):
        from app.business_todo import GoalState, sequential_progress

        progress = sequential_progress((
            GoalState(task_id="a", sequence=1, status="in_progress"),
            GoalState(task_id="b", sequence=2, status="pending"),
        ), "a")

        self.assertEqual(progress, {
            "mode": "sequential",
            "current": 1,
            "total": 2,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
        })
        self.assertEqual(
            validate_goal_coverage(
                {"task_zhang", "task_li"},
                {"task_zhang", "task_li", "task_li:confirm"},
            ),
            (),
        )

    def test_native_todo_snapshot_exposes_only_safe_structure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_id = "business:conv_1:bwf_123"
            todo_dir = root / "todo" / session_id
            todo_dir.mkdir(parents=True)
            (todo_dir / "todo.json").write_text(json.dumps([
                {
                    "id": "task_zhang",
                    "content": "向张帅转账100元",
                    "activeForm": "正在转账",
                    "description": "工资卡尾号6217",
                    "status": "in_progress",
                    "depends_on": [],
                    "result_summary": None,
                    "meta_data": None,
                    "selected_model_id": None,
                },
                {
                    "id": "task_li:confirm",
                    "content": "等待李四交易确认",
                    "activeForm": "等待确认",
                    "description": "不得公开",
                    "status": "pending",
                    "depends_on": ["task_zhang"],
                    "result_summary": None,
                    "meta_data": None,
                    "selected_model_id": None,
                },
            ], ensure_ascii=False), encoding="utf-8")

            snapshot = load_openjiuwen_todo_snapshot(root, session_id)

        self.assertEqual(snapshot[0].id, "task_zhang")
        self.assertEqual(snapshot[0].status, "in_progress")
        serialized = str([item.model_dump() for item in snapshot])
        self.assertNotIn("张帅", serialized)
        self.assertNotIn("6217", serialized)
        self.assertNotIn("description", serialized)

    def test_snapshot_rejects_path_traversal_and_invalid_native_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                load_openjiuwen_todo_snapshot(root, "../escape")
            todo_dir = root / "todo" / "business:conv:bwf_1"
            todo_dir.mkdir(parents=True)
            (todo_dir / "todo.json").write_text(
                '[{"id":"task_1","status":"authorized","depends_on":[]}]',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_openjiuwen_todo_snapshot(root, "business:conv:bwf_1")

    def test_business_loop_outcome_is_strict(self):
        outcome = BusinessLoopOutcome.model_validate({
            "schema_version": "2.0",
            "business_workflow_id": "bwf_1234567890abcdef12345678",
            "status": "awaiting_clarification",
            "covered_task_ids": ["task_zhang", "task_li"],
            "workflow_revision": 2,
            "todo_revision": 1,
            "active_goal_task_id": "task_zhang",
            "active_todo_id": "task_zhang:collect",
            "next_action": "collect_account",
        })
        self.assertEqual(outcome.workflow_revision, 2)
        self.assertEqual(outcome.todo_revision, 1)
        with self.assertRaises(ValidationError):
            BusinessLoopOutcome.model_validate({
                **outcome.model_dump(), "grantToken": "secret",
            })

    def test_non_terminal_business_loop_outcome_requires_active_goal(self):
        with self.assertRaises(ValidationError):
            BusinessLoopOutcome.model_validate({
                "schema_version": "2.0",
                "business_workflow_id": "bwf_1234567890abcdef12345678",
                "status": "awaiting_confirmation",
                "covered_task_ids": ["task_zhang"],
                "workflow_revision": 2,
                "todo_revision": 2,
                "active_goal_task_id": None,
                "active_todo_id": None,
            })

    def test_terminal_submission_retaining_active_pointers_is_normalized(self):
        # deepseek-chat 等模型在终态提交时会把刚完成的 Todo 投影进 active 指针
        # （2026-09-06 理财推荐澄清恢复 422 的根因）。归一化只清理这一条机械可证的
        # 矛盾，不触碰非终态提交，也不能用来修复未完成的账本。
        raw = {
            "schema_version": "2.0",
            "business_workflow_id": "bwf_1234567890abcdef12345678",
            "status": "completed",
            "covered_task_ids": ["wealth_recommend"],
            "workflow_revision": 2,
            "todo_revision": 2,
            "active_goal_task_id": "wealth_recommend",
            "active_todo_id": "wealth_recommend",
            "next_action": "present_recommendations",
        }
        normalized = normalize_terminal_loop_submission(raw)
        outcome = BusinessLoopOutcome.model_validate(normalized)
        self.assertEqual(outcome.status, "completed")
        self.assertIsNone(outcome.active_goal_task_id)
        self.assertIsNone(outcome.active_todo_id)
        self.assertIsNone(outcome.next_action)
        self.assertEqual(
            normalize_terminal_loop_submission(
                {**raw, "status": "awaiting_clarification"}
            )["active_goal_task_id"],
            "wealth_recommend",
        )
        self.assertIsNone(normalize_terminal_loop_submission(None))


if __name__ == "__main__":
    unittest.main()
