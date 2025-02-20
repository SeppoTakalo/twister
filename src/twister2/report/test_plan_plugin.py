"""
Plugin to generate test plan report
"""
from __future__ import annotations

import logging
from typing import List, Sequence

import pytest
from _pytest.terminal import TerminalReporter

from twister2.report.base_report_writer import BaseReportWriter
from twister2.report.helper import (
    get_item_build_only_status,
    get_item_platform,
    get_item_platform_allow,
    get_item_quarantine,
    get_item_runnable_status,
    get_item_tags,
    get_item_type,
    get_suite_name,
    get_test_name,
    get_test_path,
)
from twister2.report.test_plan_csv import CsvTestPlan
from twister2.report.test_plan_json import JsonTestPlan

logger = logging.getLogger(__name__)


class TestPlanPlugin:
    """Generate data for test plan."""

    def __init__(
        self,
        config: pytest.Config,
        writers: Sequence[BaseReportWriter]
    ):
        """
        :param config: pytest.Config
        :param writers: list of SpecReportInterface
        """
        self.config = config
        self.writers = writers

    def _item_as_dict(self, item: pytest.Item) -> dict:
        """Return test metadata as dictionary."""
        return dict(
            suite_name=get_suite_name(item),
            test_name=get_test_name(item),
            path=get_test_path(item),
            platform=get_item_platform(item),
            tags=get_item_tags(item),
            type=get_item_type(item),
            build_only=get_item_build_only_status(item),
            runnable=get_item_runnable_status(item),
            platform_allow=get_item_platform_allow(item),
            quarantine=get_item_quarantine(item),
        )

    def generate(self, items: List[pytest.Item]) -> dict:
        """Build test plan"""
        tests = [self._item_as_dict(item) for item in items]
        return dict(tests=tests)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
    ):
        # generate test plan and save
        data = self.generate(items)
        self._save_report(data)

    def pytest_terminal_summary(self, terminalreporter: TerminalReporter) -> None:
        # print summary to terminal
        terminalreporter.ensure_newline()
        for writer in self.writers:
            writer.print_summary(terminalreporter)

    def _save_report(self, data: dict) -> None:
        """Loop over all writers and save report."""
        for writer in self.writers:
            writer.write(data)


def pytest_addoption(parser: pytest.Parser):
    custom_reports = parser.getgroup('Twister reports')
    custom_reports.addoption(
        '--testplan-csv',
        dest='testplan_csv_path',
        metavar='PATH',
        action='store',
        default=None,
        help='generate test plan in CSV format'
    )
    custom_reports.addoption(
        '--testplan-json',
        dest='testplan_json_path',
        metavar='PATH',
        action='store',
        default=None,
        help='generate test plan in JSON format'
    )


def pytest_configure(config: pytest.Config):
    if hasattr(config, 'workerinput'):  # xdist worker
        return

    test_plan_writers: list[BaseReportWriter] = []
    if testplan_csv_path := config.getoption('testplan_csv_path'):
        test_plan_writers.append(CsvTestPlan(testplan_csv_path))
    if testplan_json_path := config.getoption('testplan_json_path'):
        test_plan_writers.append(JsonTestPlan(testplan_json_path))

    if test_plan_writers:
        config.pluginmanager.register(
            plugin=TestPlanPlugin(config=config, writers=test_plan_writers),
            name='testplan'
        )
