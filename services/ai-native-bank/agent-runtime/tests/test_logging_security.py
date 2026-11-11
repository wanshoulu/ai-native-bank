import logging
import unittest

from app.logging_security import SENSITIVE_FRAMEWORK_LOGGERS, harden_framework_logging


class LoggingSecurityTests(unittest.TestCase):
    def test_framework_loggers_are_hardened_before_agent_tool_execution(self):
        original_levels = {
            name: logging.getLogger(name).level for name in SENSITIVE_FRAMEWORK_LOGGERS
        }
        self.addCleanup(
            lambda: [
                logging.getLogger(name).setLevel(level)
                for name, level in original_levels.items()
            ]
        )
        for name in SENSITIVE_FRAMEWORK_LOGGERS:
            logging.getLogger(name).setLevel(logging.INFO)

        harden_framework_logging()

        self.assertTrue(
            all(
                logging.getLogger(name).level >= logging.WARNING
                for name in SENSITIVE_FRAMEWORK_LOGGERS
            )
        )


if __name__ == "__main__":
    unittest.main()
