# -*- coding: utf-8 -*-
"""Legacy setuptools entry point.

Project metadata and dependencies live exclusively in ``pyproject.toml`` so
the Poetry/PEP 517 build and this compatibility entry point cannot drift.
"""

from setuptools import find_packages, setup


setup(
    packages=find_packages(exclude=["*.tests", "*.tests.*", "tests.*", "tests"]),
)
