"""Give coverage.py exact pytest node-id contexts."""
import coverage

def pytest_runtest_setup(item):
    current = coverage.Coverage.current()
    if current: current.switch_context(item.nodeid)

def pytest_runtest_teardown(item, nextitem):
    current = coverage.Coverage.current()
    if current: current.switch_context("")
