from setuptools import find_packages, setup
from setuptools.dist import Distribution
import os

_dirname = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_readme_filename = os.path.join(_dirname, "README.md")
if not os.path.exists(_readme_filename):
    raise AssertionError("Expected: %s to exist." % (_readme_filename,))
README = open(_readme_filename, "r").read()

data_files = []


def accept_file(f):
    f = f.lower()
    if f.endswith(".so"):
        return True
    return False


for root, dirs, files in os.walk("robocorp_ls_core"):
    for d in dirs:
        accepted_files = [
            os.path.join(root, d, f)
            for f in os.listdir(os.path.join(root, d))
            if accept_file(f)
        ]
        if accepted_files:
            data_files.append((os.path.join(root, d), accepted_files))

if not data_files:
    raise AssertionError("Expected .so files to be found.")


class BinaryDistribution(Distribution):
    def is_pure(self):
        return False


setup(
    name="robocorp-python-ls-core",
    version="0.0.0",
    description="Base Python Language Server implementation",
    long_description=README,
    url="https://github.com/robocorp/robotframework-lsp",
    author="Fabio Zadrozny",
    license="Apache License, Version 2.0",
    copyright="Robocorp Technologies, Inc.",
    packages=find_packages(),
    long_description_content_type="text/markdown",
    data_files=data_files,
    include_package_data=True,
    zip_safe=False,
    distclass=BinaryDistribution,
    # List run-time dependencies here. These will be installed by pip when
    # your project is installed. For an analysis of "install_requires" vs pip's
    # requirements files see:
    # https://packaging.python.org/en/latest/requirements.html
    install_requires=[],
    # List additional groups of dependencies here (e.g. development
    # dependencies). You can install these using the following syntax,
    # for example:
    # $ pip install -e .[test]
    extras_require={
        "test": [
            "mock",
            "pytest",
            "pytest-regressions==1.0.6",
            "pytest-xdist",
            "pytest-timeout",
        ]
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: Microsoft :: Windows",
        "Operating System :: POSIX",
        "Programming Language :: Python",
        "Programming Language :: Python :: 2",
        "Programming Language :: Python :: 2.7",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.5",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Topic :: Text Editors",
        "Topic :: Text Editors :: Integrated Development Environments (IDE)",
        "Topic :: Software Development :: Debuggers",
    ],
)
