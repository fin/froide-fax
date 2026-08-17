# pytest only honours `pytest_plugins` in the rootdir conftest. froide's
# conftest supplies the factories, the `world` fixture and the autouse mail
# mocks that these tests build on.
pytest_plugins = ["froide.conftest"]
