# Copyright 2017, Ryan P. Kelly.

from setuptools import setup


setup(
    name="spindrift",
    version="0.1",
    description="package and deploy python applications to AWS Lambda",
    author="Ryan P. Kelly",
    author_email="ryan@ryankelly.us",
    url="https://github.com/f0rk/spindrift",
    install_requires=[
        "lambda-packages",
        "pip",
        "requests",
        "werkzeug",
    ],
    tests_require=[
        "pytest",
    ],
    package_dir={"": "lib"},
    packages=["spindrift"],
    include_package_data=True,
    zip_safe=False,
)
