"""Regression tests for work-board ship job counting.

Run: python -m unittest test_job_counts.py
"""
import os
import unittest
from unittest.mock import patch

# monitor.py validates the presence of these environment variables at import.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")
from monitor import numeric_sum, gang_job_sum, calculate_board


class JobCountTests(unittest.TestCase):
    def test_worker_quantities_are_counted(self):
        self.assertEqual(
            numeric_sum("1HT 1WD 41 DRIVERS 1GSP 1MECH 1HDM"), 46
        )
        self.assertEqual(numeric_sum("2HT 2WD 4SL 8HDM"), 16)
        self.assertEqual(numeric_sum("1 TOW 1 SIGNAL"), 2)

    def test_dispatch_times_are_not_jobs(self):
        for value in ["AT 5:30 START", "15:30 START", "3:30PM START"]:
            with self.subTest(value=value):
                self.assertEqual(numeric_sum(value), 0)

    def test_crew_descriptions_are_not_jobs(self):
        self.assertEqual(gang_job_sum("4 GANGS (3STEP)"), 0)
        self.assertEqual(gang_job_sum("2GANGS 1HT 14 LASHERS"), 15)
        self.assertEqual(numeric_sum("4 GANGS 3STEP"), 0)

    def test_ship_count_excludes_time_annotations(self):
        gb = {
            "work_board_430pm": {
                "modified_timestamp": "sample",
                "ships_in_port": [
                    {
                        "ship_name": "Test Auto",
                        "berth": "AAT 67",
                        "commodities": "AUTO",
                        "gangs": "1 GANG",
                        "jobs": "41 DRIVERS 1 MECH AT 5:30 START",
                    },
                    {
                        "ship_name": "Test Container",
                        "berth": "FSD",
                        "commodities": "CONTAINERS",
                        "gangs": "2 GANGS (3STEP)",
                        "jobs": "1HT 14 LASHERS",
                    },
                ],
                "fsd_jobs": [{"qty": "6"}],
                "dp_jobs": [{"qty": "7"}],
            }
        }
        result = calculate_board(gb, "work_board_430pm")
        self.assertEqual(result["total"], 70)
        self.assertEqual(result["rated_total"], 13)
        self.assertEqual(result["ship_jobs_total"], 57)
        self.assertEqual(result["gang_total"], 0)
        self.assertEqual(sum(x["total"] for x in result["ship_job_details"]), 57)


if __name__ == "__main__":
    unittest.main()
