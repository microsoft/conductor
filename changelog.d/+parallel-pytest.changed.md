The default development and CI test commands now run the isolated pytest
suite in parallel, substantially reducing feedback time on multi-core
machines. Set `PYTEST_WORKERS=0` when a serial run is needed for debugging.
