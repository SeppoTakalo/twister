"""
Plugin generates variants of tests bases on specification from YAML file.
Test variants are generated for `platform` and `scenario`.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from twister2.helper import safe_load_yaml
from twister2.platform_specification import PlatformSpecification
from twister2.specification_processor import (
    TEST_SPEC_FILE_NAME,
    RegularSpecificationProcessor,
)
from twister2.yaml_test_specification import YamlTestSpecification

logger = logging.getLogger(__name__)


@dataclass
class Variant:
    """Keeps information about single test variant"""
    platform: PlatformSpecification
    scenario: str

    def __str__(self):
        return f'{self.platform.identifier}:{self.scenario}'


def get_scenarios_from_fixture(metafunc: pytest.Metafunc) -> list[str]:
    """Return scenarios selected by fixture `build_specification`."""
    if mark := metafunc.definition.get_closest_marker('build_specification'):
        scenarios = list(mark.args)
        if not scenarios:
            logger.warning(
                'At least one `scenario` should be added to `build_specification` decorator in test: %s',
                metafunc.definition.nodeid
            )
        return scenarios
    return []


def get_scenarios_from_yaml(spec_file: Path) -> list[str]:
    """Return all available scenarios from yaml specification."""
    data = safe_load_yaml(spec_file)
    try:
        return data['tests'].keys()
    except KeyError:
        return []


def pytest_generate_tests(metafunc: pytest.Metafunc):
    # generate parametrized tests for each selected platform for ordinary pytest tests
    # if `specification` fixture is used
    if 'specification' not in metafunc.fixturenames:
        return

    twister_config = metafunc.config.twister_config

    platforms_list: list[PlatformSpecification] = [
        platform for platform in twister_config.platforms
        if platform.identifier in twister_config.default_platforms
    ]
    spec_file_path: Path = Path(metafunc.definition.fspath.dirname) / TEST_SPEC_FILE_NAME
    scenarios = get_scenarios_from_fixture(metafunc)
    assert spec_file_path.exists(), f'There is no specification file for the test: {spec_file_path}'
    if not scenarios:
        scenarios = get_scenarios_from_yaml(spec_file_path)
    variants = itertools.product(platforms_list, scenarios)
    params: list[pytest.param] = []
    for variant in variants:
        v = Variant(*variant)
        params.append(
            pytest.param(v, marks=pytest.mark.platform(v.platform.identifier), id=str(v))
        )

    # using indirect=True to inject value from `specification` fixture instead of param
    metafunc.parametrize(
        'specification', params, scope='function', indirect=True
    )


@pytest.fixture(scope='function')
def specification() -> None:
    """Injects a test specification from yaml file to a test item"""
    # The body of this function is empty because it is only used to
    # inform pytest that we want to generate parametrized tests for
    # a test function which uses this fixture


def pytest_configure(config: pytest.Config):
    config.addinivalue_line(
        'markers', 'build_specification(names="scenario1,scenario2"): select scenarios to build'
    )


def generate_yaml_test_specification_for_item(item: pytest.Item, variant: Variant) -> YamlTestSpecification:
    """Add test specification from yaml file to test item."""
    logger.debug('Adding test specification to item "%s"', item.nodeid)
    scenario: str = variant.scenario
    platform: PlatformSpecification = variant.platform

    processor = RegularSpecificationProcessor(item)
    test_spec = processor.process(platform, scenario)
    return test_spec


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
):
    if not hasattr(session, 'specifications'):
        session.specifications = {}

    for item in items:
        # add YAML test specification to session for consistency with python tests
        if hasattr(item.function, 'spec') and item.nodeid not in session.specifications:
            session.specifications[item.nodeid] = item.function.spec
        # yaml test function has no `callspec`
        if not hasattr(item, 'callspec'):
            continue
        if variant := item.callspec.params.get('specification'):
            spec = generate_yaml_test_specification_for_item(item, variant)
            session.specifications[item.nodeid] = spec
