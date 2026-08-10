# -*- coding: utf-8 -*-
import os

from setuptools import setup, find_packages

setup(
    name="qq-botpy",
    version=os.getenv("VERSION_NAME", "0.1.0"),
    author="Tencent",
    author_email="veehou@tencent.com",
    description="Async Python SDK for the QQ Bot Open Platform",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    # 项目主页
    url="https://github.com/tencent-connect/botpy",
    # 你要安装的包，通过 setuptools.find_packages 找到当前目录下有哪些包
    packages=find_packages(exclude=["*.tests", "*.tests.*", "tests.*", "tests"]),
    # 执照
    license="MIT",
    # 安装依赖
    install_requires=[
        "aiohttp>=3.14.3,<4",
        "PyYAML>=6.0.3,<7",
        "APScheduler>=3.11.3,<4",
        "cryptography>=50.0.0,<51",
    ],
    python_requires=">=3.10,<4",
    # 分类
    classifiers=[
        # 发展时期,常见的如下
        #   3 - Alpha
        #   4 - Beta
        #   5 - Production/Stable
        "Development Status :: 4 - Beta",
        # 开发的目标用户
        "Intended Audience :: Developers",
        # 属于什么类型
        "Topic :: Software Development",
        # 许可证信息
        "License :: OSI Approved :: MIT License",
        # 目标 Python 版本
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    ],
)
