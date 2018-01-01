# Copyright 2017, Ryan P. Kelly.

from setuptools import setup


setup(
    name="spindrift",
    version="0.1",
    description="package python applications for AWS Lambda",
    author="Ryan P. Kelly",
    author_email="ryan@ryankelly.us",
    url="https://github.com/f0rk/spindrift",
    install_requires=[
        "lambda-packages",
        "pip",
        "pyyaml",
        "requests",
        "werkzeug",
    ],
    tests_require=[
        "pytest",
    ],
    package_dir={"": "lib"},
    packages=["spindrift"],
    scripts=["tools/spindrift"],
    include_package_data=True,
    zip_safe=False,
)
