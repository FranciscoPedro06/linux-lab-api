from tests.labs.support import LAB_IMAGE, OCI_RUNTIME


def pytest_report_header() -> str:
    return f"lab runtime: {OCI_RUNTIME}, lab image: {LAB_IMAGE}"
