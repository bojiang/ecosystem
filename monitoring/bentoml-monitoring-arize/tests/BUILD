load("//:requirements.bzl", "requirement")
load("//bazel:python_rules.bzl", "pytest_suite")

package(default_visibility = ["//visibility:public"])

pytest_suite(
    name = "package",
    args = [
        "--cov=bentoml.monitoring.arize",
        "--cov-report=term-missing",
    ],
)
