"""Isolate standalone component tests before any app path is resolved."""
import os
import tempfile

_home = tempfile.TemporaryDirectory(prefix='rf-component-test-')
os.environ['RESOURCE_FINDER_HOME'] = _home.name
