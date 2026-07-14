from setuptools import find_packages, setup


setup(
    name="codereview-agent",
    version="0.1.0",
    description="Guided, LLM-first code review agent for Spring Boot + MySQL + Nacos + Vue projects",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    python_requires=">=3.9",
    packages=find_packages(include=["codereview_agent", "codereview_agent.*"]),
    entry_points={"console_scripts": ["codereview=codereview_agent.cli:main"]},
)
