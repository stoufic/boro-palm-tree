"""Keep test-owned worker-pool work on the test thread for exact contexts."""
import pickle
import weakref
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor

from coverage import Coverage

_current_nodeid = ""
_original_thread_submit = ThreadPoolExecutor.submit
_original_process_submit = ProcessPoolExecutor.submit
_initialized_process_pools = weakref.WeakSet()


def _run_in_context(function, args, kwargs):
    return function(*args, **kwargs)


def _submit_with_context(self, function, /, *args, **kwargs):
    if not _current_nodeid:
        original = (
            _original_process_submit
            if isinstance(self, ProcessPoolExecutor)
            else _original_thread_submit
        )
        return original(self, function, *args, **kwargs)
    # Execute test-owned work synchronously. coverage.py's manual contexts are
    # not reliably accumulated when a reused worker thread changes context.
    # A completed Future preserves the interface used by sqlglot's tests while
    # keeping every traced line under the exact pytest node id.
    future = Future()
    try:
        if isinstance(self, ProcessPoolExecutor):
            # Preserve the observable parts of ProcessPoolExecutor: its
            # initializer and the pickle round trips for calls and results.
            # sqlglot deliberately hooks expression pickling through serde,
            # so omitting this would make those dependencies disappear.
            if self not in _initialized_process_pools:
                if self._initializer:
                    self._initializer(*self._initargs)
                _initialized_process_pools.add(self)
            function, args, kwargs = pickle.loads(pickle.dumps((function, args, kwargs)))
            result = _run_in_context(function, args, kwargs)
            result = pickle.loads(pickle.dumps(result))
        else:
            result = _run_in_context(function, args, kwargs)
        future.set_result(result)
    except BaseException as error:
        future.set_exception(error)
    return future


ThreadPoolExecutor.submit = _submit_with_context
ProcessPoolExecutor.submit = _submit_with_context


def pytest_runtest_setup(item):
    global _current_nodeid
    _current_nodeid = item.nodeid
    current = Coverage.current()
    if current:
        current.switch_context(item.nodeid)


def pytest_runtest_teardown(item, nextitem):
    global _current_nodeid
    _current_nodeid = ""
    current = Coverage.current()
    if current:
        current.switch_context("")
