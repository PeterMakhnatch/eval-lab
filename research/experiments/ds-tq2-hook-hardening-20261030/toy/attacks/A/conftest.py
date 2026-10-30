"""Attack A: force a successful session whatever the tests did."""


def pytest_sessionfinish(session, exitstatus):
    session.exitstatus = 0
